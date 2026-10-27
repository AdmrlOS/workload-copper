//! The same generated Copper graph records and performs input-only native replay.
use crate::model::{Frame, Sensor};
use crate::tasks::{RunState, STATE};
use cu29::prelude::*;
#[cfg(test)]
use cu29_export::copperlists_reader;
use serde::Serialize;
use std::error::Error;
use std::fs::{self, File};
use std::io::{BufRead, BufReader, BufWriter, Write};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub type Result<T, E = Box<dyn Error>> = std::result::Result<T, E>;
const SLAB_SIZE: Option<usize> = Some(8 * 1024 * 1024);

#[copper_runtime(config = "copperconfig.ron", sim_mode = true)]
struct RoverApp {}

fn execute(_: default::SimStep<'_>) -> SimOverride {
    SimOverride::ExecuteByRuntime
}
fn parent(path: &Path) -> Result<()> {
    if let Some(p) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        fs::create_dir_all(p)?;
    }
    Ok(())
}
fn writer(path: &Path) -> Result<BufWriter<File>> {
    parent(path)?;
    Ok(BufWriter::new(File::create(path)?))
}
fn emit(writer: &mut BufWriter<File>, frame: &Frame) -> Result<()> {
    serde_json::to_writer(&mut *writer, frame)?;
    writer.write_all(b"\n")?;
    writer.flush()?;
    Ok(())
}

pub fn record(
    log: &Path,
    telemetry: &Path,
    steps: u64,
    realtime: bool,
) -> Result<serde_json::Value> {
    if steps == 0 || steps > 1_000_000 {
        return Err("steps must be in 1..=1000000".into());
    }
    parent(log)?;
    let output = writer(telemetry)?;
    STATE.with_borrow_mut(|s| {
        *s = RunState::default();
        s.writer = Some(output);
    });
    let (clock, mock) = RobotClock::mock();
    let app = RoverApp::builder()
        .with_clock(clock)
        .with_log_path(log, SLAB_SIZE)?
        .with_sim_callback(&mut execute)
        .build()?;
    let mut running = app.start(&mut execute)?;
    let start = Instant::now();
    for tick in 0..steps {
        mock.set_value(tick * 50_000_000);
        running.run_one_iteration(&mut execute)?;
        if realtime {
            let deadline = start + Duration::from_millis((tick + 1) * 50);
            if let Some(wait) = deadline.checked_duration_since(Instant::now()) {
                std::thread::sleep(wait);
            }
        }
    }
    drop(running.stop(&mut execute)?);
    let last = STATE
        .with_borrow_mut(|s| {
            s.writer.take();
            s.last_frame.take()
        })
        .ok_or("no telemetry produced")?;
    Ok(
        serde_json::json!({"cycles":steps,"goals_completed":last.goals_completed,"collisions":last.collisions,"engine":"Copper runtime","mode":"deterministic 2D simulation"}),
    )
}

#[derive(Debug, Serialize)]
pub struct Verification {
    pub verified: bool,
    pub cycles: u64,
    pub mismatches: u64,
    pub engine: &'static str,
    pub detail: String,
}
fn equivalent(a: &Frame, b: &Frame) -> bool {
    let mut a = a.clone();
    let mut b = b.clone();
    a.pipeline_us = 0.0;
    b.pipeline_us = 0.0;
    a == b
}

pub fn replay(
    log: &Path,
    telemetry: &Path,
    expected: &Path,
    report: &Path,
) -> Result<Verification> {
    let result = replay_inner(log, telemetry, expected);
    let verification = match result {
        Ok(v) => v,
        Err(e) => Verification {
            verified: false,
            cycles: 0,
            mismatches: 1,
            engine: "Copper native replay",
            detail: format!("Replay failed: {e}"),
        },
    };
    parent(report)?;
    let mut f = File::create(report)?;
    serde_json::to_writer_pretty(&mut f, &verification)?;
    f.write_all(b"\n")?;
    f.flush()?;
    Ok(verification)
}
// Entries never span native sections. Decode only committed bytes, and regard
// EOF as clean only at an entry boundary; the export iterator hides all errors.
fn read_copperlist(
    reader: &mut UnifiedLoggerRead,
    section: &mut Vec<u8>,
    offset: &mut usize,
) -> Result<Option<CopperList<default::CuStampedDataSet>>> {
    while *offset == section.len() {
        let Some(bytes) = reader.read_next_section_type(UnifiedLogType::CopperList)? else {
            return Ok(None);
        };
        *section = bytes;
        *offset = 0;
    }
    let (recorded, used) =
        bincode::decode_from_slice(&section[*offset..], bincode::config::standard())
            .map_err(|e| format!("Invalid CopperList at section byte {}: {e}", *offset))?;
    *offset += used;
    Ok(Some(recorded))
}

fn replay_inner(log: &Path, telemetry: &Path, expected: &Path) -> Result<Verification> {
    let expected_frames = BufReader::new(File::open(expected)?)
        .lines()
        .map(|line| -> Result<Frame> { Ok(serde_json::from_str(&line?)?) })
        .collect::<Result<Vec<_>>>()?;
    let UnifiedLogger::Read(mut reader) =
        UnifiedLoggerBuilder::new().file_base_name(log).build()?
    else {
        return Err("not a Copper read logger".into());
    };
    // Validate the entire native section chain and backing slabs before any
    // replay can be verified, including sections after the last CopperList.
    let beginning = reader.position();
    reader.end_of_log()?;
    reader.seek(beginning)?;
    let mut section = Vec::new();
    let mut offset = 0;
    let mut output = writer(telemetry)?;
    let (clock, mock) = RobotClock::mock();
    // No binary logging or external sink execution during replay.
    let noop = Arc::new(Mutex::new(NoopLogger::new()));
    let mut init = |step: default::SimStep<'_>| match step {
        default::SimStep::Sensor(_) | default::SimStep::Telemetry(_) => SimOverride::ExecutedBySim,
        _ => SimOverride::ExecuteByRuntime,
    };
    let app = RoverApp::builder()
        .with_clock(clock)
        .with_logger::<NoopSectionStorage, NoopLogger>(noop)
        .with_sim_callback(&mut init)
        .build()?;
    let mut running = app.start(&mut init)?;
    let mut cycles = 0u64;
    let mut mismatches = 0u64;
    let mut details = Vec::new();
    while let Some(recorded) = read_copperlist(&mut reader, &mut section, &mut offset)? {
        let source = recorded.msgs.get_sensor_output();
        let sensor: &Sensor = source.payload().ok_or("recorded source is absent")?;
        if sensor.tick != cycles {
            return Err(format!(
                "non-contiguous Copper source tick {} at {cycles}",
                sensor.tick
            )
            .into());
        }
        mock.set_value(sensor.tick * 50_000_000);
        let expected_plan = recorded.msgs.get_planner_output();
        let expected_control = recorded.msgs.get_controller_output();
        let mut cycle_mismatch = false;
        let mut planner_compared = false;
        let mut controller_compared = false;
        let mut generated = None;
        let started = Instant::now();
        let mut callback = |step: default::SimStep<'_>| -> SimOverride {
            match step {
                // Only the recorded external source CuMsg is injected.
                default::SimStep::Sensor(CuTaskCallbackState::Process(_, out)) => {
                    *out = source.clone();
                    SimOverride::ExecutedBySim
                }
                default::SimStep::Sensor(_) => SimOverride::ExecutedBySim,
                // Observe the planner's regenerated output at the controller input.
                // ProcessCompleted is only emitted by Copper's logstream feature.
                default::SimStep::Controller(CuTaskCallbackState::Process(input, _)) => {
                    planner_compared = true;
                    if input.payload() != expected_plan.payload() {
                        cycle_mismatch = true;
                    }
                    SimOverride::ExecuteByRuntime
                }
                default::SimStep::Telemetry(CuTaskCallbackState::Process(input, _)) => {
                    controller_compared = true;
                    if input.payload() != expected_control.payload() {
                        cycle_mismatch = true;
                    }
                    generated = input.payload().cloned();
                    SimOverride::ExecutedBySim
                }
                default::SimStep::Telemetry(_) => SimOverride::ExecutedBySim,
                _ => SimOverride::ExecuteByRuntime,
            }
        };
        running.run_one_iteration(&mut callback)?;
        if !planner_compared || !controller_compared {
            return Err("native replay did not compare every downstream payload".into());
        }
        let mut frame = generated.ok_or("native replay did not produce a control output")?;
        frame.pipeline_us = started.elapsed().as_secs_f64() * 1_000_000.0;
        if !expected_frames
            .get(cycles as usize)
            .is_some_and(|f| equivalent(&frame, f))
        {
            cycle_mismatch = true;
        }
        if cycle_mismatch {
            mismatches += 1;
            if details.len() < 8 {
                details.push(format!("cycle {cycles}"));
            }
        }
        emit(&mut output, &frame)?;
        cycles += 1;
    }
    drop(running.stop(&mut init)?);
    if expected_frames.len() as u64 > cycles {
        mismatches += expected_frames.len() as u64 - cycles;
        details.push("expected telemetry has additional frames".into());
    }
    if cycles == 0 {
        return Err("no CopperLists found in log".into());
    }
    let verified = mismatches == 0;
    let detail = if verified {
        "Re-executed the Copper planner and safety controller from recorded sensor CuMsgs; all planner and controller payloads and all expected telemetry fields match, excluding pipeline_us and runtime metadata.".into()
    } else {
        format!("Divergence: {}", details.join(", "))
    };
    Ok(Verification {
        verified,
        cycles,
        mismatches,
        engine: "Copper native replay",
        detail,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tasks::TEST_COUNTS;
    #[test]
    fn native_replay_executes_real_tasks_but_not_simulation_or_sink() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("mission.copper");
        let live = dir.path().join("live.jsonl");
        let output = dir.path().join("replay.jsonl");
        let report = dir.path().join("verification.json");
        TEST_COUNTS.set([0; 4]);
        record(&log, &live, 8, false).unwrap();
        assert_eq!(
            TEST_COUNTS.get(),
            [8, 8, 8, 8],
            "record must execute the full graph"
        );
        TEST_COUNTS.set([0; 4]);
        STATE.with_borrow_mut(|s| {
            s.simulation.tick = 91;
            s.simulation.robot.x = 2.5;
            s.last_frame = None;
        });
        assert!(replay(&log, &output, &live, &report).unwrap().verified);
        assert_eq!(
            TEST_COUNTS.get(),
            [0, 8, 8, 0],
            "replay must recompute, not copy downstream messages"
        );
        STATE.with_borrow(|s| {
            assert_eq!(s.simulation.tick, 91);
            assert_eq!(s.simulation.robot.x, 2.5);
            assert!(s.last_frame.is_none());
            assert!(s.writer.is_none());
        });
        // Forge only stored downstream payloads, keeping source messages and
        // expected JSONL unchanged. A copy-through replay would miss this.
        let UnifiedLogger::Read(r) = UnifiedLoggerBuilder::new()
            .file_base_name(&log)
            .build()
            .unwrap()
        else {
            panic!("reader")
        };
        let mut r = UnifiedLoggerIOReader::new(r, UnifiedLogType::CopperList);
        let mut lists: Vec<_> = copperlists_reader::<default::CuStampedDataSet>(&mut r).collect();
        let mut changed_plan = lists[2]
            .msgs
            .get_planner_output()
            .payload()
            .unwrap()
            .clone();
        changed_plan.status = "forged planner".into();
        lists[2].msgs.0.1.set_payload(changed_plan);
        let mut changed_control = lists[3]
            .msgs
            .get_controller_output()
            .payload()
            .unwrap()
            .clone();
        changed_control.command.v = 0.123456789;
        lists[3].msgs.0.2.set_payload(changed_control);
        let forged = dir.path().join("forged.copper");
        let UnifiedLogger::Write(w) = UnifiedLoggerBuilder::new()
            .file_base_name(&forged)
            .preallocated_size(8 * 1024 * 1024)
            .write(true)
            .create(true)
            .build()
            .unwrap()
        else {
            panic!("writer")
        };
        let mut stream = LogStream::<memmap::MmapSectionStorage, UnifiedLoggerWrite>::new(
            UnifiedLogType::CopperList,
            Arc::new(Mutex::new(w)),
            1024 * 1024,
        )
        .unwrap();
        for cl in lists {
            stream.log(&cl).unwrap();
        }
        drop(stream);
        let v = replay(&forged, &output, &live, &report).unwrap();
        assert!(!v.verified);
        assert_eq!(v.cycles, 8);
        assert_eq!(
            v.mismatches, 2,
            "must compare both native planner and control payloads"
        );
    }
}
