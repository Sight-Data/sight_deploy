# -*- coding: utf-8 -*-
"""「应用窗口」（像桌面客户端一样打开界面）的单元测试：只验证命令生成与降级逻辑，不真的启动浏览器。

运行：python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sight_deploy as sd  # noqa: E402

URL = "http://127.0.0.1:17880/?t=abc"


class AppWindowCmd(unittest.TestCase):
    def test_no_browser_returns_none(self):
        with mock.patch.object(sd, "_app_browser_candidates", return_value=["/no/such/browser"]):
            self.assertIsNone(sd._app_window_cmd(URL))

    def test_picks_first_existing_candidate(self):
        with tempfile.NamedTemporaryFile() as f:
            with mock.patch.object(sd, "_app_browser_candidates", return_value=["/no/such/browser", f.name, "/other"]):
                cmd = sd._app_window_cmd(URL)
        self.assertEqual(cmd[0], f.name)

    def test_uses_app_mode_and_private_profile(self):
        cmd = sd._app_window_cmd(URL, exe="/x/chrome")
        self.assertIn("--app=" + URL, cmd)
        prof = [a for a in cmd if a.startswith("--user-data-dir=")]
        self.assertEqual(len(prof), 1)
        self.assertTrue(prof[0].startswith("--user-data-dir=" + str(sd.CONF_DIR)))


class OpenFallback(unittest.TestCase):
    def test_env_forces_plain_browser(self):
        with mock.patch.dict(os.environ, {"SIGHT_DEPLOY_BROWSER": "1"}), \
                mock.patch.object(sd, "_app_window_cmd") as cmd, \
                mock.patch.object(sd, "_open_in_browser") as plain:
            sd._open_browser(URL)
        cmd.assert_not_called()
        plain.assert_called_once_with(URL)

    def test_no_chromium_falls_back_to_default_browser(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(sd, "_app_window_cmd", return_value=None), \
                mock.patch.object(sd, "_open_in_browser") as plain:
            os.environ.pop("SIGHT_DEPLOY_BROWSER", None)
            sd._open_browser(URL)
        plain.assert_called_once_with(URL)

    def test_launch_error_falls_back(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(sd, "_app_window_cmd", return_value=["/x/chrome"]), \
                mock.patch.object(sd, "CONF_DIR", sd.Path(tempfile.mkdtemp())), \
                mock.patch.object(sd.subprocess, "Popen", side_effect=OSError("boom")), \
                mock.patch.object(sd, "_open_in_browser") as plain, \
                mock.patch.object(sd, "LOG"):
            os.environ.pop("SIGHT_DEPLOY_BROWSER", None)
            sd._open_browser(URL)
        plain.assert_called_once_with(URL)


if __name__ == "__main__":
    unittest.main()
