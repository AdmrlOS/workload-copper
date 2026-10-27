import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class BootTests(unittest.TestCase):
    def boot(self):
        path = Path(__file__).resolve().parents[1] / "boot.py"
        self.assertTrue(path.is_file(), "safe fd-relative boot helper is missing")
        spec = importlib.util.spec_from_file_location("copper_boot", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_rejects_traversal_before_touching_the_test_root(self):
        boot = self.boot()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            root.mkdir()
            for request in ["/data/../opt/copper", "/data/./app", "/data/app/../../etc",
                            "/etc", "data/app", "/data//app"]:
                with self.subTest(request=request), self.assertRaises(ValueError):
                    boot.prepare_data(request, root=root)
            self.assertEqual(list(root.iterdir()), [])

    def test_symlink_components_cannot_escape_or_receive_ownership(self):
        boot = self.boot()
        with tempfile.TemporaryDirectory() as tmp, patch("os.fchown") as chown:
            root, outside = Path(tmp) / "data", Path(tmp) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "runs").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                boot.prepare_data("/data", root=root)
            chown.assert_not_called()
            (root / "runs").unlink()
            (root / "app").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                boot.prepare_data("/data/app", root=root)
            chown.assert_not_called()
            self.assertEqual(list(outside.iterdir()), [])
            alias = Path(tmp) / "alias"
            alias.symlink_to(root, target_is_directory=True)
            with self.assertRaises(OSError):
                boot.prepare_data("/data", root=alias)

    def test_only_app_leaf_and_runs_are_chowned_by_descriptor(self):
        boot = self.boot()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            root.mkdir()
            (root / "unrelated").write_text("untouched")
            owned = []
            def chown(fd, uid, gid):
                owned.append((os.fstat(fd).st_ino, uid, gid))
            with patch("os.geteuid", return_value=0), patch("os.fchown", side_effect=chown):
                previous_umask = os.umask(0o077)
                try:
                    boot.prepare_data("/data/team/app", root=root)
                finally:
                    os.umask(previous_umask)
            self.assertEqual(owned, [(os.stat(root / "team/app").st_ino, 10001, 10001),
                                     (os.stat(root / "team/app/runs").st_ino, 10001, 10001)])
            self.assertTrue((root / "team").stat().st_mode & 0o001,
                            "root-owned intermediate directory blocks the dropped app UID")
            self.assertEqual((root / "unrelated").read_text(), "untouched")

    def test_exec_drops_privileges_from_fixed_working_directory(self):
        boot = self.boot()
        with patch.dict(os.environ, {"COPPER_DATA_DIR": "/data/app"}), \
                patch.object(boot, "prepare_data") as prepare, \
                patch("os.chdir") as chdir, patch("os.execvp") as execute:
            boot.main()
        prepare.assert_called_once_with("/data/app")
        chdir.assert_called_once_with("/opt/copper")
        execute.assert_called_once_with("setpriv", [
            "setpriv", "--reuid=10001", "--regid=10001", "--clear-groups", "--no-new-privs",
            "python3", "/opt/copper/service.py"])


if __name__ == "__main__":
    unittest.main()
