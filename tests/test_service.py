import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import StateStore, RecordingStore, make_server, MissionSupervisor


class StateTests(unittest.TestCase):
    def test_missing_and_stale_frames_are_not_reported_as_live(self):
        state = StateStore(steps=800, revision="test", copper_version="test")
        self.assertEqual(state.snapshot()["status"], "starting")
        self.assertFalse(state.ready(now=state.started + 31))
        state.begin("20261027T000000Z-1234abcd")
        state.publish({"tick": 1, "time_s": 0.05})
        self.assertTrue(state.ready())
        self.assertEqual(state.snapshot()["frame"]["tick"], 1)
        self.assertFalse(state.ready(now=state.updated + 4))
        state.fail("engine exited")
        self.assertEqual(state.snapshot()["status"], "error")
        self.assertFalse(state.ready())


class RecordingTests(unittest.TestCase):
    def test_corrupt_or_incomplete_reports_have_no_verified_cycle_count(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            store = RecordingStore(Path(tmp))
            run_id = "20261027T000000Z-1234abcd"
            run = store.directory(run_id, create=True)
            valid = json.dumps(dict(verified=True, cycles=2, mismatches=0))
            for report, replay in [("{", '{"tick":0}\n'), ("[]", '{"tick":0}\n'),
                                   ("null", '{"tick":0}\n'), (valid, '{"tick":0}\n'),
                                   (valid, "not-json\n"), (valid, "null\nnull\n"),
                                   (json.dumps(dict(verified=False, cycles=900, mismatches=1)), "")]:
                with self.subTest(report=report, replay=replay):
                    (run / "verification.json").write_text(report)
                    (run / "replay.jsonl").write_text(replay)
                    rows = store.summaries()
                    self.assertEqual(len(rows), 1)
                    self.assertFalse(rows[0]["verified"])
                    self.assertEqual(rows[0]["cycles"], 0)
                    with self.assertRaises(ValueError):
                        store.replay(run_id)

    def test_replay_never_follows_artifact_symlinks_or_open_time_swaps(self):
        import json
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RecordingStore(root / "runs")
            run_id = "20261027T000000Z-1234abcd"
            run = store.directory(run_id, create=True)
            report = json.dumps(dict(verified=True, cycles=1, mismatches=0))
            outside = root / "outside"
            outside.write_text(report)
            (run / "verification.json").symlink_to(outside)
            (run / "replay.jsonl").write_text('{"tick":0}\n')
            with self.assertRaises(ValueError):
                store.replay(run_id)
            (run / "verification.json").unlink()
            (run / "verification.json").write_text(report)
            (run / "replay.jsonl").unlink()
            outside.write_text('{"tick":0}\n')
            (run / "replay.jsonl").symlink_to(outside)
            with self.assertRaises(ValueError):
                store.replay(run_id)
            (run / "replay.jsonl").unlink()
            (run / "replay.jsonl").write_text('{"tick":0}\n')
            real_open = os.open
            def swap_at_open(name, flags, *args, **kwargs):
                if name == "replay.jsonl":
                    (run / name).unlink()
                    (run / name).symlink_to(outside)
                return real_open(name, flags, *args, **kwargs)
            with patch("os.open", side_effect=swap_at_open), self.assertRaises(ValueError):
                store.replay(run_id)

    def test_only_verified_native_replays_are_available(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            recordings = RecordingStore(Path(tmp), max_runs=4, max_bytes=100000)
            run_id = "20261027T000000Z-1234abcd"
            run = recordings.directory(run_id, create=True)
            (run / "replay.jsonl").write_text('{"tick":0}\n')
            (run / "verification.json").write_text(json.dumps(dict(
                verified=False, cycles=1, mismatches=1, engine="Copper native replay")))
            with self.assertRaises(ValueError):
                recordings.replay(run_id)
            (run / "verification.json").write_text(json.dumps(dict(
                verified=True, cycles=1, mismatches=0, engine="Copper native replay")))
            replay = recordings.replay(run_id)
            self.assertEqual(replay["frames"], [{"tick": 0}])
            self.assertEqual(recordings.summaries()[0]["cycles"], 1)
            with self.assertRaises(ValueError):
                recordings.directory("../secrets")
            with self.assertRaises(FileNotFoundError):
                recordings.directory("20261027T000000Z-00000000")

    def test_retention_preserves_active_run_and_unrelated_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "keep-me").mkdir()
            recordings = RecordingStore(root, max_runs=1, max_bytes=1000)
            old = "20261026T000000Z-1234abcd"
            active = "20261027T000000Z-1234abcd"
            recordings.directory(old, create=True).joinpath("log.copper").write_bytes(b"x" * 20)
            recordings.directory(active, create=True).joinpath("log.copper").write_bytes(b"x" * 20)
            recordings.prune(protect=active)
            self.assertFalse((root / old).exists())
            self.assertTrue((root / active).exists())
            self.assertTrue((root / "keep-me").exists())


class HttpTests(unittest.TestCase):
    def test_paused_socket_download_does_not_hold_recording_lock_and_times_out(self):
        import json
        import os
        import socket
        import threading
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RecordingStore(root / "runs")
            run_id = "20261027T000000Z-1234abcd"
            run = store.directory(run_id, create=True)
            (run / "verification.json").write_text(json.dumps(dict(verified=True, cycles=1, mismatches=0)))
            (run / "replay.jsonl").write_text('{"tick":0}\n')
            (run / "mission_0.copper").write_bytes(os.urandom(8 * 1024 * 1024))
            server = make_server(StateStore(1, "test", "test"), store, root, "127.0.0.1", 0)
            finished = threading.Event()
            original_finish = server.RequestHandlerClass.finish
            def finish(handler):
                try:
                    original_finish(handler)
                finally:
                    finished.set()
            server.RequestHandlerClass.finish = finish
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            client = socket.socket()
            client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
            client.settimeout(5)
            try:
                client.connect(("127.0.0.1", server.server_port))
                client.sendall(f"GET /api/recordings/{run_id}/download HTTP/1.0\r\n\r\n".encode())
                headers = b""
                while not headers.endswith(b"\r\n\r\n"):
                    headers += client.recv(1)
                self.assertIn(b"200 OK", headers)
                # The client deliberately never reads the body.
                acquired = store.lock.acquire(timeout=0.3)
                if acquired:
                    store.lock.release()
                self.assertTrue(acquired, "network backpressure held the recording/supervisor lock")
                self.assertTrue(finished.wait(7), "stalled download had no bounded write timeout")
            finally:
                client.close()
                server.shutdown()
                server.server_close()
                thread.join()

    def test_download_preserves_confined_copper_family_and_safe_base_alias(self):
        import io
        import json
        import tarfile
        import threading
        import urllib.request
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RecordingStore(root / "runs")
            run_id = "20261027T000000Z-1234abcd"
            run = store.directory(run_id, create=True)
            (run / "verification.json").write_text(json.dumps(dict(verified=True, cycles=1, mismatches=0)))
            for name, data in [("live.jsonl", b'{"tick":0}\n'), ("replay.jsonl", b'{"tick":0}\n'),
                               ("record.log", b"record engine"), ("replay.log", b"replay engine"),
                               ("mission_0.copper", b"first slab"), ("mission_1.copper", b"second slab")]:
                (run / name).write_bytes(data)
            (run / "mission.copper").symlink_to(run / "mission_0.copper")
            secret = root / "secret"
            secret.write_text("NEVER ARCHIVE THIS")
            (run / "outside.copper").symlink_to(secret)
            (run / "unrelated.txt").write_text("NEVER ARCHIVE THIS")
            (run / "nested").symlink_to(root, target_is_directory=True)
            server = make_server(StateStore(1, "test", "test"), store, root, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/recordings/{run_id}/download"
                with urllib.request.urlopen(url, timeout=5) as response:
                    data = response.read()
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                    names = {m.name for m in archive.getmembers()}
                    expected = {f"{run_id}/{name}" for name in ["mission.copper", "mission_0.copper",
                                "mission_1.copper", "live.jsonl", "replay.jsonl", "verification.json",
                                "record.log", "replay.log"]}
                    self.assertEqual(names, expected)
                    alias = archive.getmember(f"{run_id}/mission.copper")
                    self.assertTrue(alias.issym())
                    self.assertEqual(alias.linkname, "mission_0.copper")
                    self.assertEqual(archive.extractfile(f"{run_id}/mission_1.copper").read(), b"second slab")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_read_only_api_reports_failures_and_denies_arbitrary_files(self):
        import json
        import threading
        import urllib.request
        import urllib.error
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "index.html").write_text("<!doctype html><title>Copper</title>")
            state = StateStore(800, "test", "test")
            store = RecordingStore(root / "recordings")
            server = make_server(state, store, root, "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urllib.request.urlopen(base + "/api/state") as response:
                    self.assertTrue(json.load(response)["simulation"])
                    self.assertIn("nosniff", response.headers["X-Content-Type-Options"])
                with urllib.request.urlopen(base + "/healthz") as response:
                    self.assertEqual(response.status, 200)
                for path, code in [("/../service.py", 404), ("/api/replay/bad", 400)]:
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(base + path)
                    self.assertEqual(raised.exception.code, code)
                state.fail("engine died")
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(base + "/healthz")
                self.assertEqual(raised.exception.code, 503)
                request = urllib.request.Request(base + "/api/state", method="POST")
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request)
                self.assertEqual(raised.exception.code, 501)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


class SupervisorTests(unittest.TestCase):
    def test_startup_skips_deeply_nested_persisted_json(self):
        import json
        import threading
        depth = max(10000, sys.getrecursionlimit() * 10)
        nested = "[" * depth + "0" + "]" * depth
        report = dict(verified=True, cycles=1, mismatches=0,
                      engine="Copper native replay", detail="persisted test fixture")
        for artifact in ["verification.json", "replay.jsonl"]:
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as tmp:
                store = RecordingStore(Path(tmp))
                good_id, bad_id = "20261026T000000Z-1234abcd", "20261027T000000Z-1234abcd"
                good = store.directory(good_id, create=True)
                bad = store.directory(bad_id, create=True)
                for run in [good, bad]:
                    (run / "verification.json").write_text(json.dumps(report))
                    (run / "replay.jsonl").write_text('{"tick":0}\n')
                payload = ('{"verified":true,"cycles":1,"mismatches":0,"detail":' + nested + "}"
                           if artifact == "verification.json" else '{"tick":0,"nested":' + nested + "}\n")
                (bad / artifact).write_text(payload)
                stop = threading.Event()
                stop.set()  # Restore persisted files only; do not simulate native engine output.
                state = StateStore(20, "test", "test")
                MissionSupervisor(state, store, "unused", 20, stop).run()
                restored = state.snapshot()
                self.assertEqual(restored["status"], "starting")
                self.assertIsNone(restored["error"])
                self.assertEqual(restored["latest_replay"], dict(id=good_id, **report))
                rows = {row["id"]: row for row in restored["recordings"]}
                self.assertFalse(rows[bad_id]["verified"])
                self.assertEqual(rows[bad_id]["cycles"], 0)
                with self.assertRaises(ValueError):
                    store.replay(bad_id)

    def test_startup_skips_persisted_reports_with_unknown_or_reserved_keys(self):
        import json
        import threading
        report = dict(verified=True, cycles=1, mismatches=0, engine="Copper native replay")
        for field in ["id", "event", "run_id", "unexpected"]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                store = RecordingStore(Path(tmp))
                good_id, bad_id = "20261026T000000Z-1234abcd", "20261027T000000Z-1234abcd"
                good = store.directory(good_id, create=True)
                bad = store.directory(bad_id, create=True)
                (good / "verification.json").write_text(json.dumps(report))
                (bad / "verification.json").write_text(json.dumps(dict(report, **{field: "unexpected"})))
                for run in [good, bad]:
                    (run / "replay.jsonl").write_text('{"tick":0}\n')
                stop = threading.Event()
                stop.set()  # Exercise startup restoration, not a replacement mission engine.
                state = StateStore(20, "test", "test")
                MissionSupervisor(state, store, "unused", 20, stop).run()
                restored = state.snapshot()
                self.assertEqual(restored["status"], "starting")
                self.assertIsNone(restored["error"])
                self.assertEqual(restored["latest_replay"], dict(id=good_id, **report))
                rows = {row["id"]: row for row in restored["recordings"]}
                self.assertFalse(rows[bad_id]["verified"])
                self.assertEqual(rows[bad_id]["cycles"], 0)
                with self.assertRaises(ValueError):
                    store.replay(bad_id)

    def test_startup_skips_corrupt_persisted_run_without_permanent_failure(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            store = RecordingStore(Path(tmp))
            run = store.directory("20261026T000000Z-1234abcd", create=True)
            (run / "verification.json").write_text('{"verified":')
            stop = threading.Event()
            stop.set()  # Exercise only restoration, never substitute a mission engine.
            state = StateStore(20, "test", "test")
            MissionSupervisor(state, store, "unused", 20, stop).run()
            self.assertNotEqual(state.snapshot()["status"], "error")
            self.assertIsNone(state.snapshot()["latest_replay"])
            self.assertFalse(state.snapshot()["recordings"][0]["verified"])

    def test_both_command_pollers_prune_aggregate_budget_and_bound_active_run(self):
        import threading
        from unittest.mock import patch
        for phase in ["record", "replay"]:
            for overflow in [False, True]:
                with self.subTest(phase=phase, overflow=overflow), tempfile.TemporaryDirectory() as tmp:
                    store = RecordingStore(Path(tmp), max_runs=4, max_bytes=100)
                    old_id, new_id = "20261026T000000Z-1234abcd", "20261027T000000Z-1234abcd"
                    old = store.directory(old_id, create=True)
                    (old / "mission_0.copper").write_bytes(b"x" * 60)
                    supervisor = MissionSupervisor(StateStore(1, "test", "test"), store, "unused", 1, threading.Event())
                    def exercise_poll(args, run, timeout, on_poll=None):
                        self.assertIsNotNone(on_poll, f"{args[0]} has no retention poll callback")
                        if args[0] == "record":
                            (run / "live.jsonl").write_text('{"tick":0}\n')
                        if args[0] == phase:
                            (run / "mission_0.copper").write_bytes(b"x" * (110 if overflow else 60))
                            on_poll()
                            self.assertFalse(old.exists(), "aggregate budget left old and active runs over the limit")
                            self.assertLessEqual(sum(store.size(p) for p in store.directories()), 100)
                            raise InterruptedError("unit poll assertion completed; no native result simulated")
                        on_poll()
                    expected = RuntimeError if overflow else InterruptedError
                    with patch.object(supervisor, "command", side_effect=exercise_poll), self.assertRaises(expected):
                        supervisor.episode(new_id, realtime=False)

    def test_pruned_latest_replay_is_unpublished_before_next_command(self):
        import json
        import shutil
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            store = RecordingStore(Path(tmp), max_runs=1)
            old_id, new_id = "20261026T000000Z-1234abcd", "20261027T000000Z-1234abcd"
            old = store.directory(old_id, create=True)
            report = dict(verified=True, cycles=1, mismatches=0)
            (old / "verification.json").write_text(json.dumps(report))
            (old / "replay.jsonl").write_text('{"tick":0}\n')
            state = StateStore(1, "test", "test")
            state.data.update(latest_replay=dict(id=old_id, **report), recordings=store.summaries())
            supervisor = MissionSupervisor(state, store, shutil.which("false"), 1, threading.Event())
            with self.assertRaises(RuntimeError):
                supervisor.episode(new_id, realtime=False)
            self.assertFalse(old.exists())
            self.assertIsNone(state.snapshot()["latest_replay"])
            self.assertNotIn(old_id, [item["id"] for item in state.snapshot()["recordings"]])

    def test_stalled_real_process_is_killed_on_startup_or_frame_inactivity(self):
        import os
        import threading
        import time
        for emit_first in [False, True]:
            with self.subTest(emit_first=emit_first), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                pidfile = root / "pid"
                engine = root / "sleeping-process"
                engine.write_text(f"#!{sys.executable}\n" +
                    "import json,os,pathlib,sys,time\n" +
                    f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\n" +
                    ("pathlib.Path(sys.argv[sys.argv.index('--telemetry')+1]).write_text('{\"tick\":0}\\n')\n"
                     if emit_first else "") + "time.sleep(60)\n")
                engine.chmod(0o755)
                state = StateStore(20, "test", "test")
                stop = threading.Event()
                supervisor = MissionSupervisor(state, RecordingStore(root / "runs"), engine, 20, stop)
                supervisor.startup_timeout, supervisor.inactivity_timeout = 1, 0.15
                worker = threading.Thread(target=supervisor.run)
                started = time.monotonic()
                worker.start()
                try:
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive(), "stale readiness never terminated the engine")
                    self.assertLess(time.monotonic() - started, 2)
                    self.assertTrue(stop.is_set())
                    self.assertEqual(state.snapshot()["status"], "error")
                    self.assertIn("inactivity" if emit_first else "startup", state.snapshot()["error"])
                    self.assertTrue(pidfile.is_file())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(pidfile.read_text()), 0)
                finally:
                    stop.set()
                    worker.join(timeout=3)

    def test_real_failed_engine_does_not_produce_successful_recording(self):
        import shutil
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(800, "test", "test")
            recordings = RecordingStore(Path(tmp))
            supervisor = MissionSupervisor(state, recordings, shutil.which("false"), 800, threading.Event())
            with self.assertRaisesRegex(RuntimeError, "record exited"):
                supervisor.episode("20261027T000000Z-1234abcd", realtime=False)
            self.assertFalse(recordings.summaries()[0]["verified"])
            self.assertIsNone(state.snapshot()["latest_replay"])


class ImageAcceptanceTests(unittest.TestCase):
    def test_cadence_check_rejects_nominal_frames_published_too_slowly(self):
        from unittest.mock import patch
        import check_image
        self.assertTrue(hasattr(check_image, "check_cadence"), "optional wall-clock cadence check is missing")
        for ticks, expected in [(60, True), (30, False)]:
            with self.subTest(ticks=ticks):
                states = [{"run_id": "same-run", "frame": {"tick": 0}},
                          {"run_id": "same-run", "frame": {"tick": ticks}}]
                with patch.object(check_image, "get", side_effect=states), \
                        patch.object(check_image.time, "monotonic", side_effect=[10, 13]), \
                        patch.object(check_image.time, "sleep"):
                    if expected:
                        self.assertEqual(check_image.check_cadence("test"), 20)
                    else:
                        with self.assertRaises(AssertionError):
                            check_image.check_cadence("test")

    def test_security_probe_reads_effective_ids_and_no_new_privileges_for_both_processes(self):
        import check_image
        self.assertTrue(hasattr(check_image, "read_process_security"), "engine privilege probe is missing")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for pid, command in [("10", b"python3\0/opt/copper/service.py\0"),
                                 ("11", b"/usr/local/bin/copper-rover\0record\0")]:
                proc = root / pid
                proc.mkdir()
                (proc / "cmdline").write_bytes(command)
                (proc / "status").write_text("Uid:\t0\t10001\t10001\t10001\n"
                                             "Gid:\t0\t10001\t10001\t10001\nNoNewPrivs:\t1\n")
            result = check_image.read_process_security(root)
            self.assertEqual(set(result), {"service", "engine"})
            for item in result.values():
                self.assertEqual((item["uid"], item["gid"], item["no_new_privs"]), (10001, 10001, 1))

    def test_download_extraction_is_confined_and_preserves_original_log_base(self):
        import io
        import tarfile
        import check_image
        self.assertTrue(hasattr(check_image, "extract_download"), "downloaded archive is not validated/extracted")
        run_id = "20261027T000000Z-1234abcd"
        def archive(extra=None):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
                for name in ["mission_0.copper", "mission_1.copper", "live.jsonl", "replay.jsonl",
                             "verification.json", "record.log", "replay.log"]:
                    info = tarfile.TarInfo(f"{run_id}/{name}")
                    info.size = 3
                    tar.addfile(info, io.BytesIO(b"abc"))
                alias = tarfile.TarInfo(f"{run_id}/mission.copper")
                alias.type, alias.linkname = tarfile.SYMTYPE, "mission_0.copper"
                tar.addfile(alias)
                if extra:
                    tar.addfile(extra)
            return buffer.getvalue()
        with tempfile.TemporaryDirectory() as tmp:
            import os
            previous_umask = os.umask(0o077)
            try:
                extracted = check_image.extract_download(archive(), Path(tmp), run_id)
            finally:
                os.umask(previous_umask)
            self.assertEqual(extracted.stat().st_mode & 0o777, 0o755)
            self.assertEqual((extracted / "live.jsonl").stat().st_mode & 0o777, 0o644)
            self.assertEqual((extracted / "mission.copper").read_bytes(), b"abc")
            self.assertEqual((extracted / "mission_1.copper").read_bytes(), b"abc")
            self.assertEqual((extracted / "mission.copper").readlink(), Path("mission_0.copper"))
        bad = [tarfile.TarInfo(f"{run_id}/../escape"), tarfile.TarInfo("/absolute")]
        link = tarfile.TarInfo(f"{run_id}/external.copper")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        bad.append(link)
        for extra in bad:
            with self.subTest(name=extra.name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    check_image.extract_download(archive(extra), Path(tmp), run_id)
                self.assertEqual(list(Path(tmp).iterdir()), [], "invalid tar wrote files before validation")

    def test_restart_uses_fresh_latest_recording_and_refreshed_port_binding(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import patch
        import check_image
        self.assertTrue(hasattr(check_image, "check_restart"), "persistence check still uses stale initial replay")
        old_base, new_base = "http://127.0.0.1:1111", "http://127.0.0.1:2222"
        calls = []
        def get(base, path):
            calls.append((base, path))
            return {"latest_replay": {"id": "newest-before-stop"}}
        def run(*args, **kwargs):
            return SimpleNamespace(stdout=json.dumps([{"State": {"ExitCode": 0}}]))
        with patch.object(check_image, "get", side_effect=get), patch.object(check_image, "run", side_effect=run), \
                patch.object(check_image, "container_base", return_value=new_base):
            restored = check_image.check_restart("container", old_base)
        self.assertEqual(restored, "newest-before-stop")
        self.assertEqual(calls, [(old_base, "/api/state"), (new_base, "/api/state")])


if __name__ == "__main__":
    unittest.main()
