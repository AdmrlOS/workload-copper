#!/usr/bin/env python3
"""Read-only dashboard and lifecycle supervisor for the real Copper engine."""
import json
import platform
import re
import shutil
import threading
import time
from pathlib import Path
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote
import tarfile
import tempfile
import os
import signal
import subprocess
import sys
import uuid
import stat
from contextlib import contextmanager


class StateStore:
    def __init__(self, steps, revision, copper_version):
        self.lock = threading.RLock()
        self.started = self.updated = time.monotonic()
        self.data = dict(status="starting", error=None, engine="Copper",
                         copper_version=copper_version, simulation=True, target_hz=20,
                         architecture=platform.machine(), revision=revision, run_id=None,
                         frame=None, run=dict(steps=steps), recordings=[], latest_replay=None)

    def begin(self, run_id):
        with self.lock:
            self.data.update(status="starting", error=None, run_id=run_id, frame=None)
            self.data["run"]["started_at"] = datetime.now(timezone.utc).isoformat()
            self.updated = time.monotonic()

    def publish(self, frame):
        with self.lock:
            self.data.update(frame=frame, status="live")
            self.updated = time.monotonic()

    def fail(self, error):
        with self.lock:
            self.data.update(status="error", error=str(error))

    def snapshot(self):
        with self.lock:
            return dict(self.data, uptime_s=time.monotonic() - self.started,
                        frame_age_s=time.monotonic() - self.updated)

    def ready(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            status = self.data["status"]
            if status == "error":
                return False
            if status == "starting":
                return now - self.updated < 30
            if status == "verifying":
                return now - self.updated < 120
            return self.data["frame"] is not None and now - self.updated < 3


RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")


class RecordingStore:
    """Bounded app-owned recordings; never follows arbitrary client paths."""
    def __init__(self, root, max_runs=4, max_bytes=128 * 1024 * 1024):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_runs = max_runs
        self.max_bytes = max_bytes
        self.lock = threading.RLock()

    def directory(self, run_id, create=False):
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("invalid recording id")
        path = self.root / run_id
        if path.is_symlink():
            raise ValueError("symlink recordings are not allowed")
        if create:
            path.mkdir()
        if not path.is_dir():
            raise FileNotFoundError(run_id)
        return path

    def directories(self):
        return sorted(p for p in self.root.iterdir()
                      if RUN_ID.fullmatch(p.name) and p.is_dir() and not p.is_symlink())

    @staticmethod
    def size(path):
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())

    def summaries(self):
        with self.lock:
            rows = []
            for path in reversed(self.directories()):
                try:
                    report = self.replay(path.name)["verification"]
                except (ValueError, OSError):
                    report = {}
                rows.append(dict(id=path.name, cycles=report.get("cycles", 0),
                                 verified=report.get("verified") is True,
                                 mismatches=report.get("mismatches"), bytes=self.size(path),
                                 created_at=path.name[:16]))
            return rows

    @contextmanager
    def open_run(self, run_id):
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("invalid recording id")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        root = os.open(self.root, flags)
        try:
            run = os.open(run_id, flags, dir_fd=root)
            try:
                yield run
            finally:
                os.close(run)
        finally:
            os.close(root)

    @staticmethod
    def open_artifact(run, name):
        # Nonblocking prevents a substituted FIFO from hanging before fstat.
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=run)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("recording artifacts must be regular files")
        return os.fdopen(fd, "rb")

    def read_replay(self, run, run_id):
        try:
            with self.open_artifact(run, "verification.json") as stream:
                report = json.load(stream)
            if not isinstance(report, dict) or report.keys() - {
                    "verified", "cycles", "mismatches", "engine", "detail"}:
                raise ValueError("invalid native verification report fields")
            if report.get("verified") is not True or \
                    type(report.get("mismatches")) is not int or report["mismatches"] != 0 or \
                    type(report.get("cycles")) is not int or report["cycles"] < 1:
                raise ValueError("native replay has not passed verification")
            with self.open_artifact(run, "replay.jsonl") as stream:
                frames = [json.loads(line) for line in stream if line.strip()]
            if len(frames) != report["cycles"] or any(not isinstance(f, dict) for f in frames):
                raise ValueError("replay frames do not match verified cycles")
            return dict(id=run_id, verification=report, frames=frames)
        except (OSError, ValueError, RecursionError) as error:
            raise ValueError(f"invalid or incomplete native replay: {error}") from error

    def replay(self, run_id):
        with self.lock:
            try:
                with self.open_run(run_id) as run:
                    return self.read_replay(run, run_id)
            except FileNotFoundError:
                raise
            except OSError as error:
                raise ValueError("unsafe recording directory") from error

    def archive_into(self, run_id, archive):
        with self.lock, self.open_run(run_id) as run:
            self.read_replay(run, run_id)  # Verify the same directory descriptor being archived.
            with tarfile.open(fileobj=archive, mode="w:gz") as tar:
                included = set()
                for name in sorted(os.listdir(run)):
                    if not (name.endswith((".copper", ".jsonl")) or name in {
                            "verification.json", "record.log", "replay.log"}):
                        continue
                    try:
                        stream = self.open_artifact(run, name)
                    except (OSError, ValueError):
                        continue  # No arbitrary symlinks, directories, or special files.
                    with stream:
                        info = tarfile.TarInfo(f"{run_id}/{name}")
                        info.size = os.fstat(stream.fileno()).st_size
                        info.mode = 0o640
                        tar.addfile(info, stream)
                        included.add(name)
                try:
                    target = os.readlink("mission.copper", dir_fd=run)
                except OSError:
                    return
                target = Path(target)
                if target.is_absolute():
                    if target.parent != self.root / run_id:
                        return
                    target = Path(target.name)
                if len(target.parts) == 1 and target.name in included and target.name.endswith(".copper"):
                    info = tarfile.TarInfo(f"{run_id}/mission.copper")
                    info.type = tarfile.SYMTYPE
                    info.linkname = target.name  # Portable sibling alias, never an absolute host path.
                    tar.addfile(info)

    def prune(self, protect=None):
        with self.lock:
            paths = self.directories()
            sizes = {p: self.size(p) for p in paths}
            for path in paths[:]:
                if len(sizes) <= self.max_runs and sum(sizes.values()) <= self.max_bytes:
                    break
                if path.name != protect:
                    shutil.rmtree(path)
                    del sizes[path]
            if sum(sizes.values()) > self.max_bytes:
                raise RuntimeError("active recording exceeded the disk budget")


def make_server(state, recordings, web_root, host="0.0.0.0", port=8080):
    web_root = Path(web_root)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass

        def response_headers(self, status, content_type, size):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                             "style-src 'self' 'unsafe-inline'; object-src 'none'; "
                             "base-uri 'none'; frame-ancestors 'self'")

        def json_response(self, status, body):
            content = json.dumps(body, allow_nan=False).encode()
            self.response_headers(status, "application/json; charset=utf-8", len(content))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            path = unquote(urlsplit(self.path).path)
            try:
                if path == "/api/state":
                    return self.json_response(200, state.snapshot())
                if path == "/healthz":
                    return self.json_response(200 if state.ready() else 503,
                                              dict(ok=state.ready(), status=state.snapshot()["status"]))
                if path.startswith("/api/replay/"):
                    return self.json_response(200, recordings.replay(path.removeprefix("/api/replay/")))
                if path.startswith("/api/recordings/") and path.endswith("/download"):
                    run_id = path.removeprefix("/api/recordings/").removesuffix("/download")
                    with tempfile.TemporaryFile(dir=recordings.root) as archive:
                        recordings.archive_into(run_id, archive)
                        size = archive.tell()
                        archive.seek(0)
                        self.response_headers(200, "application/gzip", size)
                        self.send_header("Content-Disposition", f'attachment; filename="{run_id}.tar.gz"')
                        self.end_headers()
                        # Only the immutable snapshot remains open while a client reads.
                        shutil.copyfileobj(archive, self.wfile)
                    return
                name = "index.html" if path == "/" else path.removeprefix("/")
                # Exact offline asset inventory: never serve by suffix or directory.
                static_types = {
                    "index.html": "text/html; charset=utf-8",
                    "app.js": "text/javascript; charset=utf-8",
                    "model.js": "text/javascript; charset=utf-8",
                    "style.css": "text/css; charset=utf-8",
                    "assets/copper-logo.webp": "image/webp",
                    "assets/admiral-logo-ink.svg": "image/svg+xml",
                    "assets/paper-tile.webp": "image/webp",
                    "assets/Archivo.woff2": "font/woff2",
                    "assets/IBMPlexMono-Regular.woff2": "font/woff2",
                    "assets/IBMPlexMono-Medium.woff2": "font/woff2",
                }
                if name not in static_types:
                    return self.json_response(404, dict(error="not found"))
                content = (web_root / name).read_bytes()
                content_type = static_types[name]
                self.response_headers(200, content_type, len(content))
                self.end_headers()
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except FileNotFoundError:
                self.json_response(404, dict(error="recording or resource not found"))
            except ValueError as error:
                self.json_response(400, dict(error=str(error)))
            except Exception as error:
                self.log_error("request failed: %s", error)
                self.json_response(500, dict(error="resource could not be read"))

    return ThreadingHTTPServer((host, port), Handler)


class MissionSupervisor:
    def __init__(self, state, recordings, engine, steps, stop):
        self.state, self.recordings, self.engine = state, recordings, str(engine)
        self.steps, self.stop = steps, stop
        self.startup_timeout, self.inactivity_timeout = 30, 3

    def command(self, args, run, timeout, on_poll=None):
        phase = args[0]
        with (run / f"{phase}.log").open("wb") as log:
            process = subprocess.Popen([self.engine, *args], stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            started = time.monotonic()
            try:
                while process.poll() is None:
                    if self.stop.is_set():
                        raise InterruptedError("workload stopping")
                    if time.monotonic() - started > timeout:
                        raise TimeoutError(f"Copper {phase} exceeded {timeout}s")
                    if on_poll:
                        on_poll()
                    self.stop.wait(0.05)
                if on_poll:
                    on_poll()
                if process.returncode:
                    raise RuntimeError(f"Copper {phase} exited {process.returncode}; see {run.name}/{phase}.log")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)

    def retain(self, protect=None):
        with self.recordings.lock:
            try:
                self.recordings.prune(protect=protect)
            finally:
                available = {p.name for p in self.recordings.directories()}
                with self.state.lock:
                    if {row["id"] for row in self.state.data["recordings"]} != available:
                        self.state.data["recordings"] = self.recordings.summaries()
                    latest = self.state.data["latest_replay"]
                    if latest and latest["id"] not in available:
                        self.state.data["latest_replay"] = None

    def episode(self, run_id, realtime=True):
        run = self.recordings.directory(run_id, create=True)
        self.retain(protect=run_id)
        self.state.begin(run_id)
        live = run / "live.jsonl"
        live.touch()
        count = 0
        buffered = ""
        last_frame = time.monotonic()
        with live.open() as stream:
            def update():
                nonlocal count, buffered, last_frame
                buffered += stream.read()
                while "\n" in buffered:
                    line, buffered = buffered.split("\n", 1)
                    frame = json.loads(line)
                    if frame["tick"] != count:
                        raise RuntimeError("non-contiguous Copper telemetry")
                    count += 1
                    self.state.publish(frame)
                    last_frame = time.monotonic()
                deadline = self.inactivity_timeout if count else self.startup_timeout
                if time.monotonic() - last_frame > deadline:
                    phase = "frame inactivity" if count else "startup"
                    raise TimeoutError(f"Copper record {phase} deadline exceeded ({deadline}s)")
                self.retain(protect=run_id)

            args = ["record", "--log-base", str(run / "mission.copper"),
                    "--telemetry", str(live), "--steps", str(self.steps)]
            if realtime:
                args.append("--realtime")
            self.command(args, run, timeout=self.steps / 20 + 30 if realtime else 90, on_poll=update)
        if count != self.steps or buffered:
            raise RuntimeError(f"recorded {count} frames; expected {self.steps}")
        with self.state.lock:
            self.state.data["status"] = "verifying"
            self.state.updated = time.monotonic()
        self.command(["replay", "--log-base", str(run / "mission.copper"),
                      "--telemetry", str(run / "replay.jsonl"), "--expected", str(live),
                      "--report", str(run / "verification.json")], run, timeout=120,
                     on_poll=lambda: self.retain(protect=run_id))
        replay = self.recordings.replay(run_id)
        if len(replay["frames"]) != self.steps:
            raise RuntimeError("native replay did not cover every recorded cycle")
        report = replay["verification"]
        self.recordings.prune(protect=run_id)
        with self.state.lock:
            self.state.data["latest_replay"] = dict(id=run_id, **report)
            self.state.data["recordings"] = self.recordings.summaries()
        print(json.dumps(dict(event="native_replay_verified", run_id=run_id, **report)), flush=True)
        return report

    def run(self):
        try:
            self.recordings.prune()
            with self.state.lock:
                self.state.data["recordings"] = self.recordings.summaries()
                for item in self.state.data["recordings"]:
                    if item["verified"]:
                        replay = self.recordings.replay(item["id"])
                        self.state.data["latest_replay"] = dict(id=item["id"], **replay["verification"])
                        break
            while not self.stop.is_set():
                run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
                self.episode(run_id)
        except InterruptedError:
            pass
        except Exception as error:
            self.state.fail(error)
            print(f"Copper workload failed: {error}", file=sys.stderr, flush=True)
            self.stop.set()


def main():
    steps = int(os.environ.get("COPPER_STEPS", "800"))
    max_runs = int(os.environ.get("COPPER_KEEP_RUNS", "4"))
    max_bytes = int(os.environ.get("COPPER_MAX_BYTES", str(128 * 1024 * 1024)))
    if not 20 <= steps <= 5000 or not 1 <= max_runs <= 16 or not 16 * 1024 * 1024 <= max_bytes <= 1024**3:
        raise ValueError("invalid COPPER_STEPS (20..5000), COPPER_KEEP_RUNS (1..16), or COPPER_MAX_BYTES (16MiB..1GiB)")
    root = Path(__file__).resolve().parent
    state = StateStore(steps, os.environ.get("IMAGE_REVISION", "development"),
                       os.environ.get("COPPER_VERSION", "see Cargo.lock"))
    recordings = RecordingStore(Path(os.environ.get("COPPER_DATA_DIR", "/data")) / "runs", max_runs, max_bytes)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    server = make_server(state, recordings, root / "www", port=int(os.environ.get("PORT", "8080")))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    supervisor = MissionSupervisor(state, recordings, os.environ.get("COPPER_ENGINE", "/usr/local/bin/copper-rover"), steps, stop)
    worker = threading.Thread(target=supervisor.run, daemon=True)
    worker.start()
    print(f"Copper simulator dashboard listening on {server.server_address}; persistent data: {recordings.root}", flush=True)
    stop.wait()
    worker.join(timeout=12)
    server.shutdown()
    server.server_close()
    return 1 if state.snapshot()["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
