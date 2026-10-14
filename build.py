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
  WIN7=1              打 Win7 兼容版（要求 Python 3.8；产物名 ...-windows7-x64.exe）
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
WIN7 = bool(os.environ.get("WIN7"))  # Win7 兼容版：必须用 Python 3.8 打（3.9 起不再支持 Win7）
PYINSTALLER = "pyinstaller==6.9.0" if WIN7 else "pyinstaller==6.11.1"
PARAMIKO = "paramiko>=3.4,<4" if WIN7 else "paramiko>=3.4,<6"
EXTRA_PIP = ["cryptography>=41,<43"] if WIN7 else []  # 旧版 cryptography 自带的 OpenSSL / 运行库对 Win7 更保险


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
    run([py, "-m", "pip", "install", "-q", "--disable-pip-version-check", PARAMIKO, PYINSTALLER, "certifi"] + EXTRA_PIP)  # certifi：在线检查更新用的 HTTPS 根证书（Mac 的 python.org 构建没有系统证书）
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
        # 改过 Info.plist 后必须整包重新签名，否则签名失效，用户下载后 macOS 会报「已损坏，无法打开」
        # （PyInstaller 在 Apple 芯片上做的是 ad-hoc 签名，改完 plist 就对不上了）。
        # 有 Developer ID 证书时设 MAC_SIGN_IDENTITY="Developer ID Application: xxx (TEAMID)"，否则用 ad-hoc。
        ident = os.environ.get("MAC_SIGN_IDENTITY") or "-"
        say("重新签名 .app（%s）" % ("Developer ID" if ident != "-" else "ad-hoc"))
        sign = ["codesign", "--force", "--deep", "--sign", ident]
        if ident != "-":
            sign += ["--options", "runtime", "--timestamp"]
        run(sign + [str(app)])
        run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])
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


def smoke(target=None, cmd=None, label="打包产物"):
    """启动程序（不开浏览器），确认能起服务、能读到界面和 /api/state、能正常退出。
    失败时把程序自己的运行日志打出来，方便在 CI 上排查。"""
    if os.environ.get("SKIP_SMOKE"):
        return
    say("冒烟自检（%s）" % label)
    if cmd is None:
        exe = target / "Contents" / "MacOS" / APP_NAME if target.suffix == ".app" else target
        cmd = [str(exe)]
    home = Path(tempfile.mkdtemp(prefix="sight-deploy-smoke-"))
    env = dict(os.environ, SIGHT_DEPLOY_HOME=str(home))
    port = "17999"
    p = subprocess.Popen([str(c) for c in cmd] + ["--no-browser", "--port", port], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = "http://127.0.0.1:%s" % port

    def get(path, token=None, data=None, timeout=15):
        req = urllib.request.Request(base + path, data=data, method="POST" if data is not None else "GET",
                                     headers=dict({"X-Token": token} if token else {},
                                                  **({"Content-Type": "application/json"} if data is not None else {})))
        return opener.open(req, timeout=timeout).read().decode("utf-8")

    def dump_log():
        for f in sorted((home / "logs").glob("*.log")) if (home / "logs").is_dir() else []:
            print("---- %s（最后 60 行）----" % f.name, flush=True)
            print("\n".join(f.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]), flush=True)
        print("---- 进程状态：%s ----" % ("运行中" if p.poll() is None else "已退出，退出码 %s" % p.returncode), flush=True)

    try:
        inst = home / "instance.json"
        t0 = time.time()
        while not inst.exists():
            if p.poll() is not None:
                dump_log()
                raise SystemExit("%s启动失败（退出码 %s）" % (label, p.returncode))
            if time.time() - t0 > 60:
                dump_log()
                raise SystemExit("%s 60 秒内没有起来" % label)
            time.sleep(0.5)
        token = json.loads(inst.read_text(encoding="utf-8"))["token"]
        last = None
        while time.time() - t0 < 120:  # 杀毒软件扫描等原因首次响应可能慢，给足时间
            try:
                get("/api/ping", token, timeout=10)
                break
            except Exception as e:
                last = e
                time.sleep(2)
        else:
            dump_log()
            raise SystemExit("%s的网页服务没有响应：%s" % (label, last))
        print("  服务已响应（%.1f 秒）" % (time.time() - t0), flush=True)
        html = get("/?t=%s" % token)
        assert "Sight 部署助手" in html, "界面文件没有打进包里"
        st = json.loads(get("/api/state", token))
        assert st.get("version"), st
        get("/api/shutdown", token, data=b"{}")
        p.wait(timeout=20)
        print("  界面、接口、退出均正常", flush=True)
    except (AssertionError, OSError, ValueError) as e:
        dump_log()
        raise SystemExit("%s冒烟自检失败：%s" % (label, e))
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
        out = OUT / ("%s-%s-windows%s-x64.exe" % (ASSET, ver, "7" if WIN7 else ""))
        shutil.copy2(target, out)
    else:
        out = OUT / ("%s-%s-linux-%s" % (ASSET, ver, platform.machine()))
        shutil.copy2(target, out)
        out.chmod(0o755)
    return out


def main():
    if WIN7 and (platform.system() != "Windows" or tuple(platform.python_version_tuple()[:2]) != ("3", "8")):
        raise SystemExit("WIN7=1 只能在 Windows 上用 Python 3.8 打包（当前 %s %s）" % (platform.system(), platform.python_version()))
    ver = tool_version()
    say("Sight 部署助手 %s 打包" % ver)
    py = ensure_venv()
    if os.environ.get("SMOKE_SOURCE"):  # 先用源码方式跑一遍，区分「代码问题」还是「打包问题」
        smoke(cmd=[py, HERE / "sight_deploy.py"], label="源码")
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
