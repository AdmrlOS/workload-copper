#!/usr/bin/env python3
"""Exercise the final ARM64 image, actual native replay, HTTP UI, and persistence."""
import argparse
import json
import math
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import io
import inspect
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def run(*args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True)


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as response:
        return json.load(response)


def wait_for(fn, timeout=90):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            value = fn()
            if value:
                return value
        except (OSError, urllib.error.URLError, ValueError) as error:
            last = error
        time.sleep(0.2)
    raise AssertionError(f"acceptance timed out; last error: {last}")


def extract_download(data, destination, run_id):
    """Validate the whole flat artifact set before writing any extracted files."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        regular = set()
        seen = set()
        aliases = []
        for member in members:
            parts = member.name.split("/")
            if len(parts) != 2 or parts[0] != run_id or parts[1] in {"", ".", ".."} or \
                    member.name in seen:
                raise ValueError("unsafe or duplicate archive member")
            seen.add(member.name)
            name = parts[1]
            if member.isfile() and (name.endswith((".copper", ".jsonl")) or name in {
                    "verification.json", "record.log", "replay.log"}):
                regular.add(name)
            elif member.issym() and name == "mission.copper":
                target = PurePosixPath(member.linkname)
                if target.is_absolute() or len(target.parts) != 1 or not target.name.endswith(".copper"):
                    raise ValueError("unsafe Copper base alias")
                aliases.append(member)
            else:
                raise ValueError("unexpected or unsafe artifact type")
        for alias in aliases:
            if alias.linkname not in regular:
                raise ValueError("Copper base alias does not reference an archived slab")
        required = {"live.jsonl", "replay.jsonl", "verification.json", "record.log", "replay.log"}
        if not required <= regular or f"{run_id}/mission.copper" not in seen:
            raise ValueError("download is missing replay artifacts or original log-base")
        root = Path(destination) / run_id
        root.mkdir(mode=0o755)  # Must be new, never a preexisting directory/symlink.
        root.chmod(0o755)  # Docker cp owns files as root; the dropped engine must be able to read them.
        for member in members:
            if member.isfile():
                path = root / PurePosixPath(member.name).name
                with archive.extractfile(member) as source, path.open("xb") as target:
                    shutil.copyfileobj(source, target)
                path.chmod(0o644)
        for alias in aliases:
            (root / "mission.copper").symlink_to(alias.linkname)
        return root


def container_base(name):
    binding = json.loads(run("docker", "inspect", name).stdout)[0]["NetworkSettings"]["Ports"]["8080/tcp"][0]
    return f"http://127.0.0.1:{binding['HostPort']}"


def check_restart(name, base):
    # Other acceptance work can span multiple episodes: capture immediately before stop.
    expected = get(base, "/api/state")["latest_replay"]["id"]
    run("docker", "stop", "-t", "12", name)
    stopped = json.loads(run("docker", "inspect", name).stdout)[0]["State"]
    assert stopped["ExitCode"] == 0, stopped
    run("docker", "start", name)
    base = container_base(name)  # Docker may allocate a different ephemeral host port.
    restarted = wait_for(lambda: (s if (s := get(base, "/api/state"))["latest_replay"] else None), 20)
    assert restarted["latest_replay"]["id"] == expected
    return expected


def read_process_security(proc_root="/proc"):
    result = {}
    for path in Path(proc_root).iterdir():
        try:
            command = (path / "cmdline").read_bytes().split(b"\0")
            if len(command) > 1 and command[1] == b"/opt/copper/service.py":
                role = "service"
            elif command[0].endswith(b"/copper-rover"):
                role = "engine"
            else:
                continue
            fields = {line.split(":", 1)[0]: line.split(":", 1)[1].split()
                      for line in (path / "status").read_text().splitlines() if ":" in line}
            result[role] = dict(pid=int(path.name), uid=int(fields["Uid"][1]),
                                gid=int(fields["Gid"][1]), no_new_privs=int(fields["NoNewPrivs"][0]))
        except (OSError, ValueError, KeyError, IndexError):
            continue
    return result


def check_cadence(base, duration=3):
    first = get(base, "/api/state")
    started = time.monotonic()
    time.sleep(duration)
    last = get(base, "/api/state")
    elapsed = time.monotonic() - started
    assert first["run_id"] == last["run_id"], "episode changed during cadence sample"
    hz = (last["frame"]["tick"] - first["frame"]["tick"]) / elapsed
    assert 16 <= hz <= 24, f"live wall-clock cadence was {hz:.2f} Hz, expected approximately 20 Hz"
    return hz


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--check-cadence", action="store_true", help="measure live wall-clock cadence near 20 Hz")
    parser.add_argument("--report", type=Path, default=Path("artifacts/image-acceptance.json"))
    args = parser.parse_args()
    image = json.loads(run("docker", "image", "inspect", args.image).stdout)[0]
    assert image["Architecture"] == "arm64" and image["Os"] == "linux"
    suffix = uuid.uuid4().hex[:10]
    name, volume = f"copper-check-{suffix}", f"copper-check-data-{suffix}"
    summary = dict(image=args.image, image_id=image["Id"], architecture="linux/arm64")
    try:
        run("docker", "volume", "create", volume)
        run("docker", "run", "-d", "--name", name, "-p", "127.0.0.1::8080",
            "-v", f"{volume}:/data", "-e", "COPPER_STEPS=800", args.image)
        base = container_base(name)
        wait_for(lambda: get(base, "/healthz")["ok"], 20)
        first = wait_for(lambda: (s if (s := get(base, "/api/state"))["frame"] else None), 20)
        assert first["simulation"] is True and first["engine"] == "Copper"
        security_script = "import json\nfrom pathlib import Path\n" + inspect.getsource(read_process_security) + \
                          "\nprint(json.dumps(read_process_security()))"
        security = json.loads(run("docker", "exec", name, "python3", "-c", security_script).stdout)
        assert set(security) == {"service", "engine"}, security
        for role, process in security.items():
            assert (process["uid"], process["gid"], process["no_new_privs"]) == (10001, 10001, 1), (role, process)
        summary["process_security"] = security
        if args.check_cadence:
            summary["live_cadence_hz"] = check_cadence(base)
        with urllib.request.urlopen(base, timeout=5) as response:
            assert b"canvas" in response.read()
        state = wait_for(lambda: (s if (s := get(base, "/api/state"))["latest_replay"] else None), 90)
        verified = state["latest_replay"]
        assert verified["verified"] is True and verified["cycles"] == 800 and verified["mismatches"] == 0
        replay = get(base, "/api/replay/" + verified["id"])
        frames = replay["frames"]
        assert len(frames) == 800
        assert [f["tick"] for f in frames] == list(range(800))
        assert max(f["collisions"] for f in frames) == 0
        assert max(f["goals_completed"] for f in frames) >= 1
        assert any(f["path"] for f in frames)
        assert math.hypot(frames[0]["robot"]["x"] - frames[400]["robot"]["x"],
                          frames[0]["robot"]["y"] - frames[400]["robot"]["y"]) > 1
        with urllib.request.urlopen(base + "/api/recordings/" + verified["id"] + "/download", timeout=20) as response:
            assert response.headers["Content-Type"] == "application/gzip"
            downloaded = response.read()
        with tempfile.TemporaryDirectory(prefix="copper-download-") as tmp:
            extracted = extract_download(downloaded, Path(tmp), verified["id"])
            target = "/data/download-check"
            run("docker", "cp", str(extracted), f"{name}:{target}")
            run("docker", "exec", "--user", "10001:10001", name, "copper-rover", "replay",
                "--log-base", target + "/mission.copper", "--expected", target + "/live.jsonl",
                "--telemetry", "/data/download-replay.jsonl", "--report", "/data/download-report.json")
            downloaded_report = json.loads(run("docker", "exec", name, "python3", "-c",
                "import pathlib; print(pathlib.Path('/data/download-report.json').read_text())").stdout)
            assert downloaded_report["verified"] is True and downloaded_report["cycles"] == 800
            assert downloaded_report["mismatches"] == 0
            summary["download_native_replay"] = downloaded_report
        summary["live_and_replay"] = verified
        summary["goals_completed"] = max(f["goals_completed"] for f in frames)
        summary["collisions"] = max(f["collisions"] for f in frames)
        summary["service_uid"] = security["service"]["uid"]
        original = f"/data/runs/{verified['id']}"
        tamper = """import json,pathlib,sys
p=pathlib.Path(sys.argv[1]); rows=[json.loads(x) for x in (p/'live.jsonl').read_text().splitlines()]
rows[7]['command']['v'] += 0.2
pathlib.Path('/data/tampered.jsonl').write_text(''.join(json.dumps(x)+'\\n' for x in rows))
"""
        run("docker", "exec", name, "python3", "-c", tamper, original)
        bad = run("docker", "exec", name, "copper-rover", "replay", "--log-base", original + "/mission.copper",
                  "--expected", "/data/tampered.jsonl", "--telemetry", "/data/tampered-replay.jsonl",
                  "--report", "/data/tampered-report.json", check=False)
        assert bad.returncode != 0, "tampered expected controls were wrongly accepted"
        bad_report = json.loads(run("docker", "exec", name, "python3", "-c",
                                   "import pathlib; print(pathlib.Path('/data/tampered-report.json').read_text())").stdout)
        assert bad_report["verified"] is False and bad_report["mismatches"] > 0
        summary["tampered_expected_rejected"] = bad_report
        run("docker", "exec", name, "mkdir", "/data/long-run")
        run("docker", "exec", name, "copper-rover", "record", "--steps", "1200", "--log-base",
            "/data/long-run/mission.copper", "--telemetry", "/data/long-run/live.jsonl")
        run("docker", "exec", name, "copper-rover", "replay", "--log-base", "/data/long-run/mission.copper",
            "--expected", "/data/long-run/live.jsonl", "--telemetry", "/data/long-run/replay.jsonl",
            "--report", "/data/long-run/verification.json")
        long_report = json.loads(run("docker", "exec", name, "python3", "-c",
                                    "import pathlib; print(pathlib.Path('/data/long-run/verification.json').read_text())").stdout)
        assert long_report["verified"] is True and long_report["cycles"] == 1200
        summary["long_run"] = long_report
        summary["restart_restored_id"] = check_restart(name, base)
        summary["restart_restored_recording"] = True
        summary["passed"] = True
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2))
    except Exception:
        print(run("docker", "logs", name, check=False).stdout)
        raise
    finally:
        run("docker", "rm", "-f", name, check=False)
        run("docker", "volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
