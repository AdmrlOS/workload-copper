use cu29::prelude::*;
use std::fs;
use std::io::{Seek, SeekFrom, Write};
use std::path::PathBuf;
use std::process::Command;

fn cli(args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_copper-rover"))
        .args(args)
        .output()
        .unwrap()
}

struct Recording {
    dir: tempfile::TempDir,
    log: PathBuf,
    live: PathBuf,
}

impl Recording {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("mission.copper");
        let live = dir.path().join("live.jsonl");
        let recorded = cli(&[
            "record",
            "--log-base",
            log.to_str().unwrap(),
            "--telemetry",
            live.to_str().unwrap(),
            "--steps",
            "8",
        ]);
        assert!(recorded.status.success(), "record: {recorded:?}");
        Self { dir, log, live }
    }

    fn reader(&self) -> UnifiedLoggerRead {
        let UnifiedLogger::Read(reader) = UnifiedLoggerBuilder::new()
            .file_base_name(&self.log)
            .build()
            .unwrap()
        else {
            panic!("expected a read logger")
        };
        reader
    }

    fn sections(&self) -> Vec<(u64, SectionHeader, Vec<u8>)> {
        let mut reader = self.reader();
        let mut sections = Vec::new();
        while reader.position().0 != 0 {
            let offset = reader.position().0;
            let (header, bytes) = reader.raw_read_section().unwrap();
            sections.push((offset, header, bytes));
        }
        sections
    }

    fn slab(&self, index: u64) -> PathBuf {
        self.dir.path().join(format!("mission_{index}.copper"))
    }

    fn write_at(&self, offset: u64, bytes: &[u8]) {
        let slab_size = fs::metadata(self.slab(0)).unwrap().len();
        let mut file = fs::OpenOptions::new()
            .write(true)
            .open(self.slab(offset / slab_size))
            .unwrap();
        assert!(offset % slab_size + bytes.len() as u64 <= slab_size);
        file.seek(SeekFrom::Start(offset % slab_size)).unwrap();
        file.write_all(bytes).unwrap();
    }

    fn write_header(&self, offset: u64, header: &impl bincode::Encode) {
        let bytes = bincode::encode_to_vec(header, bincode::config::standard()).unwrap();
        assert!(bytes.len() <= 512);
        self.write_at(offset, &bytes);
    }

    fn assert_rejected(&self) {
        let output = self.dir.path().join("replay.jsonl");
        let report = self.dir.path().join("report.json");
        let replayed = cli(&[
            "replay",
            "--log-base",
            self.log.to_str().unwrap(),
            "--telemetry",
            output.to_str().unwrap(),
            "--expected",
            self.live.to_str().unwrap(),
            "--report",
            report.to_str().unwrap(),
        ]);
        let report: serde_json::Value = serde_json::from_slice(&fs::read(report).unwrap()).unwrap();
        assert!(
            !replayed.status.success(),
            "corrupt log accepted: {replayed:?}; report={report}"
        );
        assert_eq!(report["verified"], false, "{report}");
        assert!(
            report["detail"]
                .as_str()
                .unwrap()
                .starts_with("Replay failed:"),
            "{report}"
        );
    }
}

#[test]
fn native_replay_rejects_malformed_trailing_copperlist_bytes() {
    let recording = Recording::new();
    let (offset, mut header, _) = recording
        .sections()
        .into_iter()
        .find(|(_, h, _)| h.entry_type == UnifiedLogType::CopperList)
        .unwrap();
    recording.write_at(offset + 512 + header.used as u64, &[0xff]);
    header.used += 1;
    recording.write_header(offset, &header);
    recording.assert_rejected();
}

#[test]
fn native_replay_rejects_unclean_main_header() {
    let recording = Recording::new();
    let mut header = recording.reader().raw_main_header().clone();
    header.clean_close = false;
    recording.write_header(0, &header);
    recording.assert_rejected();
}

#[test]
fn native_replay_rejects_open_copperlist_section() {
    let recording = Recording::new();
    let (offset, mut header, _) = recording
        .sections()
        .into_iter()
        .find(|(_, h, _)| h.entry_type == UnifiedLogType::CopperList)
        .unwrap();
    header.is_open = true;
    recording.write_header(offset, &header);
    recording.assert_rejected();
}

#[test]
fn native_replay_rejects_corrupt_final_section_header() {
    let recording = Recording::new();
    let (offset, mut header, _) = recording.sections().pop().unwrap();
    assert_ne!(header.entry_type, UnifiedLogType::CopperList);
    header.magic[0] ^= 0xff;
    recording.write_header(offset, &header);
    recording.assert_rejected();
}

#[test]
fn native_replay_rejects_partial_final_copperlist() {
    let recording = Recording::new();
    let (offset, mut header, _) = recording
        .sections()
        .into_iter()
        .rev()
        .find(|(_, h, _)| h.entry_type == UnifiedLogType::CopperList)
        .unwrap();
    header.used -= 1;
    recording.write_header(offset, &header);
    // Match the seven complete lists, so cycle counts alone cannot detect the
    // partial eighth list silently discarded by the former export iterator.
    let expected = fs::read_to_string(&recording.live)
        .unwrap()
        .lines()
        .take(7)
        .map(|line| format!("{line}\n"))
        .collect::<String>();
    fs::write(&recording.live, expected).unwrap();
    recording.assert_rejected();
}

#[test]
fn native_replay_rejects_open_final_non_copperlist_section() {
    let recording = Recording::new();
    let (offset, mut header, _) = recording.sections().pop().unwrap();
    assert_ne!(header.entry_type, UnifiedLogType::CopperList);
    header.is_open = true;
    recording.write_header(offset, &header);
    recording.assert_rejected();
}

#[test]
fn native_replay_rejects_missing_slab() {
    for index in [0, 1, 2] {
        let recording = Recording::new();
        assert!(recording.slab(2).exists(), "fixture must span three slabs");
        fs::remove_file(recording.slab(index)).unwrap();
        recording.assert_rejected();
    }
}

#[test]
fn native_replay_rejects_truncated_slab() {
    for index in [0, 1, 2] {
        let recording = Recording::new();
        let slab = recording.slab(index);
        let length = fs::metadata(&slab).unwrap().len();
        fs::OpenOptions::new()
            .write(true)
            .open(slab)
            .unwrap()
            .set_len(length - 512)
            .unwrap();
        recording.assert_rejected();
    }
}

#[test]
fn native_record_replay_roundtrip() {
    for steps in [800u64, 1200] {
        verify_mission(steps);
    }
}

fn verify_mission(steps: u64) {
    let dir = tempfile::tempdir().unwrap();
    let log = dir.path().join("mission.copper");
    let live = dir.path().join("live.jsonl");
    let replay = dir.path().join("replay.jsonl");
    let report = dir.path().join("verification.json");
    let r = cli(&[
        "record",
        "--log-base",
        log.to_str().unwrap(),
        "--telemetry",
        live.to_str().unwrap(),
        "--steps",
        &steps.to_string(),
    ]);
    assert!(
        r.status.success(),
        "record: {}",
        String::from_utf8_lossy(&r.stderr)
    );
    let frames: Vec<serde_json::Value> = fs::read_to_string(&live)
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect();
    assert_eq!(frames.len(), steps as usize);
    for (tick, f) in frames.iter().enumerate() {
        assert_eq!(f["tick"], tick as u64);
        assert_eq!(f["collisions"], 0);
        assert!(f["command"]["v"].as_f64().unwrap() <= 1.0);
        assert!(f["command"]["omega"].as_f64().unwrap().abs() <= 2.2);
    }
    assert!(
        frames.last().unwrap()["goals_completed"].as_u64().unwrap()
            >= if steps == 800 { 1 } else { 2 }
    );
    let slabs: Vec<_> = fs::read_dir(dir.path())
        .unwrap()
        .map(|e| e.unwrap())
        .filter(|e| e.file_name().to_string_lossy().ends_with(".copper"))
        .collect();
    assert!(!slabs.is_empty());
    for slab in slabs {
        assert!(slab.metadata().unwrap().len() <= 8 * 1024 * 1024);
    }
    assert_eq!(frames[0]["tick"], 0);
    let r = cli(&[
        "replay",
        "--log-base",
        log.to_str().unwrap(),
        "--telemetry",
        replay.to_str().unwrap(),
        "--expected",
        live.to_str().unwrap(),
        "--report",
        report.to_str().unwrap(),
    ]);
    assert!(
        r.status.success(),
        "replay stderr: {}; stdout: {}",
        String::from_utf8_lossy(&r.stderr),
        String::from_utf8_lossy(&r.stdout)
    );
    let v: serde_json::Value = serde_json::from_str(&fs::read_to_string(&report).unwrap()).unwrap();
    assert_eq!(v["verified"], true);
    assert_eq!(v["cycles"], steps);
    assert_eq!(v["mismatches"], 0);
    assert_eq!(v["engine"], "Copper native replay");
    let mut corrupt = frames.clone();
    corrupt[10]["command"]["v"] = serde_json::json!(0.123456789);
    let bad = dir.path().join("corrupt.jsonl");
    fs::write(
        &bad,
        corrupt
            .iter()
            .map(|f| serde_json::to_string(f).unwrap() + "\n")
            .collect::<String>(),
    )
    .unwrap();
    let r = cli(&[
        "replay",
        "--log-base",
        log.to_str().unwrap(),
        "--telemetry",
        replay.to_str().unwrap(),
        "--expected",
        bad.to_str().unwrap(),
        "--report",
        report.to_str().unwrap(),
    ]);
    assert!(
        !r.status.success(),
        "corrupted expected controls must fail verification"
    );
    let v: serde_json::Value = serde_json::from_str(&fs::read_to_string(&report).unwrap()).unwrap();
    assert_eq!(v["verified"], false);
    assert_eq!(v["cycles"], steps);
    assert_eq!(v["mismatches"], 1);
    println!(
        "validated {steps} Copper cycles: clean replay=0 mismatches; changed control=1 mismatch; goals={}",
        frames.last().unwrap()["goals_completed"]
    );
}

#[test]
fn cli_rejects_zero_steps_and_documents_both_commands() {
    let help = cli(&["--help"]);
    assert!(help.status.success());
    let help = String::from_utf8_lossy(&help.stdout);
    assert!(help.contains("record") && help.contains("replay"));
    assert!(
        !cli(&[
            "record",
            "--log-base",
            "unused.copper",
            "--telemetry",
            "unused.jsonl",
            "--steps",
            "0"
        ])
        .status
        .success()
    );
}

#[test]
fn realtime_flushes_each_frame_before_process_exit() {
    use std::time::{Duration, Instant};
    let dir = tempfile::tempdir().unwrap();
    let log = dir.path().join("paced.copper");
    let live = dir.path().join("paced.jsonl");
    let started = Instant::now();
    let mut child = Command::new(env!("CARGO_BIN_EXE_copper-rover"))
        .args([
            "record",
            "--log-base",
            log.to_str().unwrap(),
            "--telemetry",
            live.to_str().unwrap(),
            "--steps",
            "20",
            "--realtime",
        ])
        .stdout(std::process::Stdio::null())
        .spawn()
        .unwrap();
    loop {
        if fs::read_to_string(&live).is_ok_and(|s| s.contains('\n')) {
            break;
        }
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "first JSONL frame was not flushed"
        );
        assert!(
            child.try_wait().unwrap().is_none(),
            "record exited before streaming telemetry"
        );
        std::thread::sleep(Duration::from_millis(5));
    }
    assert!(
        child.try_wait().unwrap().is_none(),
        "streaming requires output before exit"
    );
    assert!(child.wait().unwrap().success());
    assert!(started.elapsed() >= Duration::from_millis(950));
    assert_eq!(fs::read_to_string(&live).unwrap().lines().count(), 20);
}
