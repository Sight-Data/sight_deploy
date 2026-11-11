# -*- coding: utf-8 -*-
"""部署助手自更新的单元测试：判断能不能自更新；macOS 的更新脚本用假的 ditto / open 在本机真跑一遍。

运行：python3 -m unittest discover -s tests -v
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sight_deploy as sd  # noqa: E402

MAC_EXE = "/Applications/Sight部署助手.app/Contents/MacOS/Sight部署助手"


class CanSelfUpdate(unittest.TestCase):
    def test_not_frozen(self):
        with mock.patch.object(sd, "FROZEN", False):
            self.assertFalse(sd.can_self_update())

    def test_mac_bundle(self):
        self.assertEqual(sd._mac_app_bundle(MAC_EXE), "/Applications/Sight部署助手.app")
        self.assertIsNone(sd._mac_app_bundle("/usr/bin/python3"))
        self.assertIsNone(sd._mac_app_bundle("/private/var/folders/x/AppTranslocation/ABC/d/Sight部署助手.app/Contents/MacOS/Sight部署助手"))

    def test_mac_frozen_from_app(self):
        with mock.patch.object(sd, "FROZEN", True), mock.patch.object(sd.os, "name", "posix"), \
                mock.patch.object(sd.sys, "platform", "darwin"), mock.patch.object(sd.sys, "executable", MAC_EXE):
            self.assertTrue(sd.can_self_update())

    def test_mac_translocated_cannot(self):
        exe = "/private/var/folders/x/AppTranslocation/ABC/d/Sight部署助手.app/Contents/MacOS/Sight部署助手"
        with mock.patch.object(sd, "FROZEN", True), mock.patch.object(sd.os, "name", "posix"), \
                mock.patch.object(sd.sys, "platform", "darwin"), mock.patch.object(sd.sys, "executable", exe):
            self.assertFalse(sd.can_self_update())


@unittest.skipUnless(shutil.which("bash") and shutil.which("unzip"), "需要 bash 和 unzip")
class MacUpdateScript(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        bin_ = self.d / "bin"
        bin_.mkdir()
        self.log = self.d / "open.log"
        self._fake(bin_, "ditto", 'unzip -q "$3" -d "$4"')       # ditto -x -k ZIP DEST
        self._fake(bin_, "open", 'echo "$@" >> "%s"' % self.log)
        self._fake(bin_, "xattr", "true")
        self.env = dict(os.environ, PATH="%s:%s" % (bin_, os.environ["PATH"]))
        self.apps = self.d / "Applications"
        self.apps.mkdir()
        self.dl = self.d / "Downloads"
        self.dl.mkdir()

    def _fake(self, bin_, name, body):
        f = bin_ / name
        f.write_text("#!/bin/bash\n" + body + "\n")
        f.chmod(f.stat().st_mode | stat.S_IXUSR)

    def _zip(self, ver, with_app=True):
        z = self.dl / "sight-deploy.zip"
        with zipfile.ZipFile(z, "w") as zf:
            if with_app:
                zf.writestr("Sight部署助手.app/Contents/version.txt", ver)
        return z

    def _app(self, ver):
        a = self.apps / "Sight部署助手.app"
        (a / "Contents").mkdir(parents=True)
        (a / "Contents" / "version.txt").write_text(ver)
        return a

    def _run(self, zip_path, app):
        script = self.d / "update.sh"
        script.write_text(sd._mac_update_script(99999999, zip_path, app, self.d / "work", self.d / "update.log", self.dl), encoding="utf-8")
        return subprocess.run(["bash", str(script)], env=self.env, timeout=60)

    def opened(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_replaces_app_and_opens_it(self):
        app = self._app("old")
        z = self._zip("new")
        self.assertEqual(self._run(z, app).returncode, 0)
        self.assertEqual((app / "Contents" / "version.txt").read_text(), "new")
        self.assertEqual(self.opened(), [str(app)])
        self.assertIn("已替换", (self.d / "update.log").read_text(encoding="utf-8"))
        self.assertFalse(z.exists(), "替换成功后新下载的 zip 应删除")
        self.assertEqual([p.name for p in self.apps.iterdir()], ["Sight部署助手.app"], "不应留下 .old 备份")

    def test_bad_zip_keeps_old_app(self):
        app = self._app("old")
        z = self._zip("new", with_app=False)  # 里面没有 .app
        self.assertNotEqual(self._run(z, app).returncode, 0)
        self.assertEqual((app / "Contents" / "version.txt").read_text(), "old")
        self.assertEqual(self.opened(), ["-R %s" % z])
        self.assertIn("保留旧版", (self.d / "update.log").read_text(encoding="utf-8"))

    def test_cannot_replace_falls_back_to_downloads(self):
        app = self.apps / "Sight部署助手.app"   # 旧 .app 不存在 → 挪不动 → 走兜底：新版放进 fallback_dir 并打开
        z = self._zip("new")
        z = z.rename(self.d / "hidden.zip")  # zip 在隐藏目录里也不影响新版放到「下载」
        self._run(z, app)
        dest = self.dl / "Sight部署助手.app"
        self.assertEqual((dest / "Contents" / "version.txt").read_text(), "new")
        self.assertEqual(self.opened(), [str(dest)])

    def test_quotes_paths_with_spaces_and_quotes(self):
        app = self.apps / "My App's.app"
        (app / "Contents").mkdir(parents=True)
        (app / "Contents" / "version.txt").write_text("old")
        z = self._zip("new")
        self.assertEqual(self._run(z, app).returncode, 0)
        self.assertEqual((app / "Contents" / "version.txt").read_text(), "new")


class CheckUpdateFile(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_good_exe(self):
        f = self.d / "a.exe"
        f.write_bytes(b"MZ" + b"\0" * (2 << 20))
        sd._check_update_file(f)

    def test_html_error_page_as_exe_rejected(self):
        f = self.d / "a.exe"
        f.write_bytes(b"<html>" + b"x" * (2 << 20))
        with self.assertRaises(sd.DeployError):
            sd._check_update_file(f)

    def test_tiny_exe_rejected(self):
        f = self.d / "a.exe"
        f.write_bytes(b"MZ" + b"\0" * 100)
        with self.assertRaises(sd.DeployError):
            sd._check_update_file(f)

    def test_zip_needs_app(self):
        good, bad, notzip = self.d / "g.zip", self.d / "b.zip", self.d / "n.zip"
        with zipfile.ZipFile(good, "w") as zf:
            zf.writestr("Sight部署助手.app/Contents/Info.plist", "x")
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("readme.txt", "x")
        notzip.write_text("<html>")
        sd._check_update_file(good)
        for f in (bad, notzip):
            with self.assertRaises(sd.DeployError):
                sd._check_update_file(f)


class SwapExe(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.target = self.d / "app.exe"
        self.new = self.d / "new.exe"
        self.target.write_bytes(b"old-version")
        self.new.write_bytes(b"new-version-longer")

    def test_swap(self):
        old = sd._swap_exe(self.target, self.new)
        self.assertEqual(self.target.read_bytes(), b"new-version-longer")
        self.assertEqual(old.read_bytes(), b"old-version")
        self.assertEqual(old.name, "app.exe.old")

    def test_stale_old_is_replaced(self):
        (self.d / "app.exe.old").write_bytes(b"ancient")
        old = sd._swap_exe(self.target, self.new)
        self.assertEqual(old.read_bytes(), b"old-version")

    def test_copy_failure_rolls_back(self):
        with mock.patch.object(sd.shutil, "copyfile", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                sd._swap_exe(self.target, self.new)
        self.assertEqual(self.target.read_bytes(), b"old-version")
        self.assertFalse((self.d / "app.exe.old").exists())

    def test_size_mismatch_rolls_back(self):
        def bad_copy(src, dst):
            Path(dst).write_bytes(b"trunc")
        with mock.patch.object(sd.shutil, "copyfile", side_effect=bad_copy):
            with self.assertRaises(OSError):
                sd._swap_exe(self.target, self.new)
        self.assertEqual(self.target.read_bytes(), b"old-version")


class ApplyWin(unittest.TestCase):
    """_apply_tool_update_win 的流程（Popen 用假的；文件操作是真的）。"""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.target = self.d / "app.exe"
        self.new = self.d / "updates" / "new.exe"
        self.new.parent.mkdir()
        self.target.write_bytes(b"old-version")
        self.new.write_bytes(b"new-version-longer")
        patches = [mock.patch.object(sd.sys, "executable", str(self.target)),
                   mock.patch.object(sd, "CONF_DIR", self.d / "conf"),
                   mock.patch.object(sd.Path, "home", return_value=self.d)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _log(self):
        return (self.d / "conf" / "update.log").read_text(encoding="utf-8")

    def test_success(self):
        with mock.patch.object(sd.subprocess, "Popen") as popen:
            sd._apply_tool_update_win(self.new)
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[0], str(self.target.resolve()))
        self.assertEqual(cmd[1], "--after-update")
        self.assertEqual(cmd[2], str(os.getpid()))
        env = popen.call_args[1]["env"]
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertFalse([k for k in env if k.startswith("_PYI")])
        self.assertEqual(self.target.read_bytes(), b"new-version-longer")
        self.assertFalse(self.new.exists(), "替换成功后下载的新 exe 应删除")
        self.assertTrue((self.d / "app.exe.old").exists())
        self.assertIn("已启动新版本", self._log())

    def test_swap_failure_runs_downloaded_exe(self):
        with mock.patch.object(sd, "_swap_exe", side_effect=PermissionError("denied")), \
                mock.patch.object(sd.subprocess, "Popen") as popen:
            sd._apply_tool_update_win(self.new)
        self.assertEqual(popen.call_args[0][0][0], str(self.new))
        self.assertEqual(self.target.read_bytes(), b"old-version")
        self.assertTrue(self.new.exists())
        self.assertIn("替换失败", self._log())

    def test_launch_failure_rolls_back(self):
        with mock.patch.object(sd.subprocess, "Popen", side_effect=OSError("blocked")):
            with self.assertRaises(sd.DeployError):
                sd._apply_tool_update_win(self.new)
        self.assertEqual(self.target.read_bytes(), b"old-version")
        self.assertFalse((self.d / "app.exe.old").exists())
        self.assertIn("已恢复旧版本", self._log())


class WaitPid(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "posix 实现")
    def test_waits_for_process_exit(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.4)"])
        import threading
        threading.Thread(target=p.wait, daemon=True).start()  # 回收子进程，免得变僵尸一直「存在」
        t0 = __import__("time").time()
        self.assertTrue(sd._wait_pid_exit(p.pid, 10))
        self.assertGreater(__import__("time").time() - t0, 0.2)

    @unittest.skipIf(os.name == "nt", "posix 实现")
    def test_timeout(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            self.assertFalse(sd._wait_pid_exit(p.pid, 0.3))
        finally:
            p.kill()
            p.wait()

    def test_gone_pid_returns_immediately(self):
        self.assertTrue(sd._wait_pid_exit(2 ** 22 - 7, 5))


if __name__ == "__main__":
    unittest.main()
