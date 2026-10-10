#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 Sight 部署助手打包成双击即用的程序（客户电脑无需安装 Python）。

PyInstaller 不能跨平台打包：Windows 版要在 Windows 上打，Mac 版要在 Mac 上打。
平时不用自己打：打 tag（v*）时 GitHub Actions 会在 Windows / macOS 真机上自动打包并发布到 Releases。
  Mac:      bash build-mac.sh          → dist/sight-deploy-<版本>-mac-<arch>.zip
  Windows:  build-windows.bat          → dist\\sight-deploy-<版本>-windows-x64.exe
  （也可以直接 python3 build.py）

做的事：在 .build-venv 建独立虚拟环境 → 安装 paramiko + pyinstaller →
打包（内嵌 web/index.html）→ 冒烟自检（启动打包产物、请求 /api/state）→ 输出到 dist/。
产物文件名用英文（GitHub Release 对中文文件名支持不可靠）；程序本身（.app / 窗口标题）仍叫「Sight部署助手」。

环境变量：
  PIP_INDEX_URL       换 pip 源（如 https://pypi.tuna.tsinghua.edu.cn/simple）
  MAC_TARGET_ARCH     Mac 目标架构：arm64 / x86_64 / universal2（默认本机架构；universal2 需要 python.org 的通用版 Python）
  SKIP_SMOKE=1        跳过冒烟自检
"""
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "dist"
VENV = HERE / ".build-venv"
APP_NAME = "Sight部署助手"   # .app / exe 内部名称
ASSET = "sight-deploy"       # 发布文件名前缀（英文）
PYINSTALLER = "pyinstaller==6.11.1"
PARAMIKO = "paramiko>=3.4,<6"


def say(msg):
    print("== " + msg, flush=True)


def tool_version():
    m = re.search(r'^TOOL_VERSION = "([^"]+)"', (HERE / "sight_deploy.py").read_text(encoding="utf-8"), re.M)
    return m.group(1)


def venv_python():
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(cmd, **kw):
    print("  $ " + " ".join(str(c) for c in cmd), flush=True)
    subprocess.check_call([str(c) for c in cmd], **kw)


def ensure_venv():
    if not venv_python().exists():
        say("创建打包用虚拟环境 %s" % VENV)
        venv.EnvBuilder(with_pip=True, clear=True).create(VENV)
    py = venv_python()
    run([py, "-m", "pip", "install", "-q", "--disable-pip-version-check", "--upgrade", "pip"])
    run([py, "-m", "pip", "install", "-q", "--disable-pip-version-check", PARAMIKO, PYINSTALLER])
    return py


def build(py, ver):
    sysname = platform.system()
    work = Path(tempfile.mkdtemp(prefix="sight-deploy-build-"))
    sep = ";" if os.name == "nt" else ":"
    args = [py, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
            "--name", APP_NAME, "--distpath", work / "dist", "--workpath", work / "build", "--specpath", work,
            "--add-data", "%s%sweb" % (HERE / "web" / "index.html", sep),
            "--hidden-import", "paramiko", "--collect-submodules", "paramiko",
            "--exclude-module", "tkinter"]
    if sysname == "Darwin":
        args += ["--windowed", "--onedir", "--osx-bundle-identifier", "com.sight.deploy"]
        arch = os.environ.get("MAC_TARGET_ARCH")
        if arch:
            args += ["--target-arch", arch]
    elif sysname == "Windows":
        args += ["--onefile", "--noconsole"]
    else:
        args += ["--onefile"]
    args.append(HERE / "sight_deploy.py")
    say("PyInstaller 打包（%s）" % sysname)
    run(args, cwd=work)
    dist = work / "dist"
    if sysname == "Darwin":
        app = dist / (APP_NAME + ".app")
        plist = app / "Contents" / "Info.plist"
        # LSUIElement：不在 Dock 里显示图标（程序本身没有窗口，界面在浏览器里）
        _plist_set(plist, {"LSUIElement": True, "CFBundleShortVersionString": ver, "CFBundleVersion": ver,
                           "CFBundleDisplayName": APP_NAME})
        return work, app
    exe = dist / (APP_NAME + (".exe" if os.name == "nt" else ""))
    return work, exe


def _plist_set(plist, kv):
    import plistlib
    with open(plist, "rb") as f:
        d = plistlib.load(f)
    d.update(kv)
    with open(plist, "wb") as f:
        plistlib.dump(d, f)


def smoke(target):
    """启动打包产物（不开浏览器），确认能起服务、能读到界面和 /api/state。"""
    if os.environ.get("SKIP_SMOKE"):
        return
    say("冒烟自检")
    exe = target / "Contents" / "MacOS" / APP_NAME if target.suffix == ".app" else target
    home = Path(tempfile.mkdtemp(prefix="sight-deploy-smoke-"))
    env = dict(os.environ, SIGHT_DEPLOY_HOME=str(home))
    port = "17999"
    p = subprocess.Popen([str(exe), "--no-browser", "--port", port], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        inst = home / "instance.json"
        for _ in range(60):
            if inst.exists():
                break
            if p.poll() is not None:
                raise SystemExit("打包产物启动失败（退出码 %s）" % p.returncode)
            time.sleep(0.5)
        else:
            raise SystemExit("打包产物 30 秒内没有起来")
        token = json.loads(inst.read_text(encoding="utf-8"))["token"]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        html = opener.open("http://127.0.0.1:%s/?t=%s" % (port, token), timeout=10).read().decode("utf-8")
        assert "Sight 部署助手" in html, "界面文件没有打进包里"
        req = urllib.request.Request("http://127.0.0.1:%s/api/state" % port, headers={"X-Token": token})
        st = json.loads(opener.open(req, timeout=10).read().decode("utf-8"))
        assert st.get("version"), st
        req = urllib.request.Request("http://127.0.0.1:%s/api/shutdown" % port, data=b"{}", method="POST",
                                     headers={"X-Token": token, "Content-Type": "application/json"})
        opener.open(req, timeout=10).read()
        p.wait(timeout=15)
        print("  界面、接口、退出均正常", flush=True)
    finally:
        if p.poll() is None:
            p.kill()
        shutil.rmtree(home, ignore_errors=True)


def package(target, ver):
    OUT.mkdir(parents=True, exist_ok=True)
    sysname = platform.system()
    if sysname == "Darwin":
        arch = os.environ.get("MAC_TARGET_ARCH") or platform.machine()
        out = OUT / ("%s-%s-mac-%s.zip" % (ASSET, ver, arch))
        if out.exists():
            out.unlink()
        # 用 ditto 打 zip：保留 .app 的符号链接与权限（Python zipfile 会弄坏 .app）
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", target, out])
    elif sysname == "Windows":
        out = OUT / ("%s-%s-windows-x64.exe" % (ASSET, ver))
        shutil.copy2(target, out)
    else:
        out = OUT / ("%s-%s-linux-%s" % (ASSET, ver, platform.machine()))
        shutil.copy2(target, out)
        out.chmod(0o755)
    return out


def main():
    ver = tool_version()
    say("Sight 部署助手 %s 打包" % ver)
    py = ensure_venv()
    work, target = build(py, ver)
    try:
        smoke(target)
        out = package(target, ver)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    size = out.stat().st_size / 1024 / 1024
    say("完成：%s（%.1f MB）" % (out, size))
    if os.environ.get("GITHUB_OUTPUT"):  # CI：把产物路径交给后续步骤
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write("asset=%s\nversion=%s\n" % (out, ver))
    if platform.system() == "Darwin":
        print("  提示：程序未签名，客户第一次打开需「右键 → 打开」（或在 系统设置 › 隐私与安全性 里点「仍要打开」）")
    if platform.system() == "Windows":
        print("  提示：程序未签名，个别杀毒软件可能误报；正式分发建议做代码签名")


if __name__ == "__main__":
    main()
