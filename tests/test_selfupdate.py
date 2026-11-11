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
        script.write_text(sd._mac_update_script(99999999, zip_path, app, self.d / "work"), encoding="utf-8")
        return subprocess.run(["bash", str(script)], env=self.env, timeout=60)

    def opened(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_replaces_app_and_opens_it(self):
        app = self._app("old")
        z = self._zip("new")
        self.assertEqual(self._run(z, app).returncode, 0)
        self.assertEqual((app / "Contents" / "version.txt").read_text(), "new")
        self.assertEqual(self.opened(), [str(app)])
        self.assertFalse(z.exists(), "替换成功后新下载的 zip 应删除")
        self.assertEqual([p.name for p in self.apps.iterdir()], ["Sight部署助手.app"], "不应留下 .old 备份")

    def test_bad_zip_keeps_old_app(self):
        app = self._app("old")
        z = self._zip("new", with_app=False)  # 里面没有 .app
        self.assertNotEqual(self._run(z, app).returncode, 0)
        self.assertEqual((app / "Contents" / "version.txt").read_text(), "old")
        self.assertEqual(self.opened(), ["-R %s" % z])

    def test_cannot_replace_falls_back_next_to_zip(self):
        app = self.apps / "Sight部署助手.app"   # 旧 .app 不存在 → 挪不动 → 走兜底：新版放在 zip 旁边并打开
        z = self._zip("new")
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


if __name__ == "__main__":
    unittest.main()
