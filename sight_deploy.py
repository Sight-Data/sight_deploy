#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Sight 部署助手 —— SightReport / SightWall 远程安装与升级（网页界面）

开源地址：https://github.com/Sight-Data/sight_deploy    产品官网：https://www.sightdata.top

双击运行（打包后的 Sight部署助手.exe / .app），或 `python3 sight_deploy.py`：
程序在本机 127.0.0.1 起一个小服务并自动打开浏览器，所有操作都在网页里完成。
界面文件：同目录 web/index.html（打包时内嵌）。前后端接口见 docs/API.md。

  能做的事：添加服务器（密码加密保存）、查看状态、首次安装（环境检查 / 上传 JDK / 注册系统服务）、
  升级（自动备份 + 健康检查 + 失败回滚）、回滚、启停、看日志。

保存的信息：
  ~/.sight-deploy/hosts.json   服务器列表；密码等敏感字段加密保存
  ~/.sight-deploy/.key         加密密钥（与本机绑定：配置拷到别的电脑无法解密）
  ~/.sight-deploy/logs/        每次操作的详细日志（不含密码），出问题时发给技术支持
  设环境变量 SIGHT_DEPLOY_PASSPHRASE 可额外加一层口令（之后每次运行都要设同样的值）。

服务器端约定（与产品包里的 install.sh / start.sh 一致）：
  安装目录     默认 /opt/sight-report、/opt/sight-wall
  系统服务     systemd 服务 sight-report / sight-wall（由包内 install.sh 注册）
  升级备份     默认 <安装目录的上级>/sight-backups/<产品>/，含程序文件 + data/
               （不含 logs、data/cache*、data/temp、data/backups）
  非默认端口   写在 /etc/systemd/system/<服务>.service.d/10-sight-deploy.conf

开发调试：python3 sight_deploy.py --no-browser --port 17880（只起服务，不开浏览器）；说明见同目录 README.md
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import os
import re
import secrets as _secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

TOOL_VERSION = "2.0.0"
CONF_DIR = Path(os.environ.get("SIGHT_DEPLOY_HOME") or (Path.home() / ".sight-deploy"))
FROZEN = getattr(sys, "frozen", False)
# 程序所在目录：打包后是 exe/.app 所在目录（用来找同目录下的安装包），源码运行是本文件目录
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
if FROZEN and sys.platform == "darwin" and ".app/Contents/MacOS" in str(APP_DIR):
    APP_DIR = APP_DIR.parents[2]  # xxx.app 所在目录
SCRIPT_DIR = APP_DIR
# 内嵌资源（web/index.html）目录：PyInstaller 解压目录或源码目录
RES_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

PRODUCTS = {
    "sight-report": {"name": "SightReport", "port": 9091, "jar": "sight-report.jar",
                     "start_class": "SightDataApplication"},
    "sight-wall": {"name": "SightWall", "port": 9090, "jar": "sight-wall.jar",
                   "start_class": "SightWallApplication"},
}
MIN_JAVA = 21
DROPIN_NAME = "10-sight-deploy.conf"
MARKER = ".sight-deploy.json"
# 备份时排除的内容（相对安装目录，tar 成员名形如 ./logs）
BACKUP_EXCLUDES = ["./logs", "./data/backups", "./data/cache*", "./data/temp", "./.sight-staging",
                   "./*.pid", "./*.bak.*", "./*.jar.new"]

paramiko = None  # 由 ensure_deps() 延迟导入


# =============================================================================
# 任务输出与交互（桥接到网页：事件流 + 弹窗提问）
# =============================================================================

class UserAbort(Exception):
    pass


class DeployError(Exception):
    def __init__(self, msg, hint=None):
        super().__init__(msg)
        self.hint = hint


class HostKeyError(DeployError):
    pass


class JobCancelled(KeyboardInterrupt):
    """用户在网页上点了「停止」。继承 KeyboardInterrupt：沿用各处「停止等待」的处理分支。"""


_tl = threading.local()


def current_job():
    return getattr(_tl, "job", None)


def check_cancel():
    j = current_job()
    if j and j.cancel_flag.is_set():
        raise JobCancelled()


def _emit(ev):
    j = current_job()
    if j:
        j.emit(ev)
    LOG.write(_log_line(ev))


def _log_line(ev):
    t = ev.get("t")
    if t == "step":
        return "\n▶ " + ev["text"]
    if t in ("ok", "warn", "fail"):
        return "  %s %s" % ({"ok": "√", "warn": "!", "fail": "×"}[t], ev["text"])
    if t == "kv":
        return "  [%s] " % ev["title"] + "; ".join("%s=%s" % (a, b) for a, b in ev["items"])
    if t == "log":
        return "      " + ev["text"]
    if t in ("progress", "live"):
        return ""
    return "    " + ev.get("text", "")


# 文字样式函数：网页里不需要终端颜色，保留函数名只为少改调用处
def bold(s): return s
def dim(s): return s
def cyan(s): return s
def green(s): return s
def yellow(s): return s
def red(s): return s


def out(msg=""):
    msg = str(msg).strip()
    if msg:
        _emit({"t": "info", "text": msg})


def step(msg): _emit({"t": "step", "text": msg})
def ok(msg): _emit({"t": "ok", "text": msg})
def warn(msg): _emit({"t": "warn", "text": msg})
def fail(msg): _emit({"t": "fail", "text": msg})
def info(msg): out(msg)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(t):
    return _ANSI.sub("", t)


def logline(msg): _emit({"t": "log", "text": strip_ansi(str(msg)).rstrip()})


def kv(title, items):
    _emit({"t": "kv", "title": title, "items": [[str(a), str(b)] for a, b in items]})


def live(lid, text):
    j = current_job()
    if j:
        j.emit({"t": "live", "id": lid, "text": text})


def _question(q):
    """把问题交给网页并阻塞等回答。返回 (cancelled, value)。"""
    j = current_job()
    if not j:
        raise DeployError("内部错误：没有可交互的任务")
    LOG.write("  [询问] %s" % q.get("title"))
    return j.ask(q)


def ask(prompt, default=None, validator=None, allow_empty=False, detail=None, placeholder=None):
    """文本输入。validator 返回 None 表示通过，返回字符串表示错误提示。"""
    err = None
    while True:
        cancelled, v = _question({"kind": "ask", "title": prompt, "label": prompt, "detail": detail,
                                  "default": "" if default is None else str(default),
                                  "placeholder": placeholder or "", "error": err})
        if cancelled:
            raise UserAbort()
        v = str(v or "").strip()
        if not v and default not in (None, ""):
            v = str(default)
        if not v and not allow_empty:
            err = "不能为空"
            continue
        if validator:
            err = validator(v)
            if err:
                continue
        LOG.write("  [输入] %s = %s" % (prompt, v))
        return v


def ask_secret(prompt, detail=None):
    while True:
        cancelled, v = _question({"kind": "secret", "title": prompt, "label": prompt, "detail": detail})
        if cancelled:
            raise UserAbort()
        if v:
            return v


def confirm(prompt, default=True, detail=None, danger=False):
    cancelled, v = _question({"kind": "confirm", "title": prompt, "default": bool(default),
                              "danger": bool(danger), "detail": detail})
    res = bool(v) and not cancelled
    LOG.write("  [确认] %s → %s" % (prompt, "是" if res else "否"))
    return res


def confirm_typed(prompt, word, detail=None):
    """高风险操作：要求原样输入一个词。"""
    cancelled, v = _question({"kind": "typed", "title": prompt, "word": word, "detail": detail})
    return not cancelled and str(v or "").strip() == word


def choose(title, options, default=1, allow_back=True, detail=None):
    """options: [(label, value)] 或 [(label, value, desc)]；返回 value；取消返回 None（allow_back）或中止。"""
    opts = [{"label": o[0], "desc": o[2] if len(o) > 2 else ""} for o in options]
    cancelled, v = _question({"kind": "choose", "title": title, "options": opts,
                              "default": max(0, min(default - 1, len(options) - 1)), "detail": detail})
    if cancelled:
        if allow_back:
            return None
        raise UserAbort()
    try:
        i = int(v)
        assert 0 <= i < len(options)
    except Exception:
        raise UserAbort()
    LOG.write("  [选择] %s → %s" % (title, options[i][0]))
    return options[i][1]


def ask_form(title, fields, validate=None, detail=None):
    """一次填多项。fields: [{key,label,default,placeholder,help,type}]；validate(values) 返回错误字符串或 None。"""
    err = None
    vals = {f["key"]: str(f.get("default", "")) for f in fields}
    while True:
        fs = [dict(f, default=vals.get(f["key"], "")) for f in fields]
        cancelled, v = _question({"kind": "form", "title": title, "fields": fs, "error": err, "detail": detail})
        if cancelled:
            raise UserAbort()
        v = v if isinstance(v, dict) else {}
        vals = {f["key"]: str(v.get(f["key"], "") if v.get(f["key"]) is not None else "").strip() for f in fields}
        err = validate(vals) if validate else None
        if not err:
            LOG.write("  [表单] %s = %s" % (title, {k: x for k, x in vals.items() if "pass" not in k}))
            return vals


def ask_file(title, file_kind, label=None, detail=None):
    err = None
    while True:
        cancelled, v = _question({"kind": "file", "title": title, "file_kind": file_kind,
                                  "label": label or title, "detail": detail, "error": err})
        if cancelled:
            raise UserAbort()
        p = norm_path_input(str(v or ""))
        if p and os.path.isfile(p):
            return p
        err = "文件不存在或无法读取：%s" % (p or "（未选择）")


def norm_path_input(p):
    p = p.strip().strip('"').strip("'")
    return os.path.expanduser(p)


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1024


def fmt_secs(s):
    s = int(s)
    return "%d:%02d" % (s // 60, s % 60)


class Progress:
    _seq = 0

    def __init__(self, label, total, start=0):
        Progress._seq += 1
        self.id = "p%d" % Progress._seq
        self.label, self.total, self.t0 = label, max(total, 1), time.time()
        self.start, self.last = start, 0
        self.done = start
        self.update(start, force=True)

    def update(self, done, force=False):
        check_cancel()
        self.done = done
        now = time.time()
        if not force and now - self.last < 0.25 and done < self.total:
            return
        self.last = now
        speed = (done - self.start) / max(now - self.t0, 0.001)
        eta = (self.total - done) / speed if speed > 0 else 0
        j = current_job()
        if j:
            j.emit({"t": "progress", "id": self.id, "label": self.label, "done": done, "total": self.total,
                    "speed": int(speed), "eta": int(eta)})

    def finish(self):
        self.update(self.total, force=True)
        LOG.write("    %s 完成（%s），用时 %s" % (self.label, human_size(self.total), fmt_secs(time.time() - self.t0)))


class _Log:
    """本地操作日志（不记录密码）。"""

    def __init__(self):
        self.fh = None
        self.path = None
        self.lock = threading.Lock()

    def open(self):
        try:
            d = CONF_DIR / "logs"
            d.mkdir(parents=True, exist_ok=True)
            self.path = d / ("sight-deploy-%s.log" % _dt.date.today().strftime("%Y%m%d"))
            self.fh = open(self.path, "a", encoding="utf-8")
            self.write("\n===== %s  Sight 部署助手 %s 启动 =====" % (
                _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), TOOL_VERSION))
        except Exception:
            self.fh = None

    def write(self, s):
        if self.fh and s:
            with self.lock:
                try:
                    self.fh.write(s.rstrip("\n") + "\n")
                    self.fh.flush()
                except Exception:
                    pass


LOG = _Log()


# =============================================================================
# 依赖
# =============================================================================

def ensure_deps():
    global paramiko
    import logging
    logging.getLogger("paramiko").addHandler(logging.NullHandler())
    logging.getLogger("paramiko").propagate = False  # 端口探测失败等属正常情况，不要打到屏幕上
    try:
        import paramiko as _p  # noqa
        paramiko = _p
        return
    except ImportError:
        pass
    # 只有「源码运行」才会走到这里（打包版已内置 paramiko），此时有控制台，直接在控制台提示
    print("需要 Python 库 paramiko（SSH 连接用），正在自动安装 …", flush=True)
    base = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    user = [] if in_venv else ["--user"]
    attempts = []
    vendor = SCRIPT_DIR / "vendor"
    if vendor.is_dir():
        attempts.append(base + user + ["--no-index", "--find-links", str(vendor), "paramiko"])
    attempts.append(base + user + ["paramiko"])
    attempts.append(base + user + ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple", "paramiko"])
    for cmd in attempts:
        print("  $ " + " ".join(cmd), flush=True)
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        if r.returncode != 0 and "externally-managed" in (r.stdout or ""):
            cmd2 = cmd + ["--break-system-packages"]
            if "--user" not in cmd2:
                cmd2.insert(len(base), "--user")
            print("  $ " + " ".join(cmd2), flush=True)
            r = subprocess.run(cmd2, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        if r.returncode == 0:
            break
        print("\n".join((r.stdout or "").strip().splitlines()[-3:]), flush=True)
    try:
        import site
        import importlib
        importlib.invalidate_caches()
        up = site.getusersitepackages()
        if up and up not in sys.path:
            sys.path.append(up)
        import paramiko as _p  # noqa
        paramiko = _p
        print("paramiko 安装完成", flush=True)
    except ImportError:
        raise DeployError("paramiko 安装失败",
                          "请手动执行：%s -m pip install paramiko\n"
                          "    离线电脑：把 paramiko 及依赖的 wheel 放进 %s 后重试" % (sys.executable, vendor))


# =============================================================================
# 加密保存服务器信息
# =============================================================================

def _machine_id():
    try:
        if sys.platform.startswith("linux"):
            for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                if os.path.exists(p):
                    v = open(p).read().strip()
                    if v:
                        return v
        elif sys.platform == "darwin":
            o = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                               stdout=subprocess.PIPE, universal_newlines=True, timeout=5).stdout
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', o or "")
            if m:
                return m.group(1)
        elif os.name == "nt":
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                               0, winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0))
            return winreg.QueryValueEx(k, "MachineGuid")[0]
    except Exception:
        pass
    return "node-%x" % uuid.getnode()


class Store:
    def __init__(self):
        self.path = CONF_DIR / "hosts.json"
        self.keyfile = CONF_DIR / ".key"
        self.data = {"version": 1, "hosts": {}}
        self._fernet = None
        self.lock = threading.RLock()  # 网页请求线程与任务线程会同时读写
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                self.data.setdefault("hosts", {})
            except Exception:
                bak = self.path.with_suffix(".broken.json")
                shutil.copy(self.path, bak)
                warn("服务器配置文件损坏，已另存为 %s，将重新开始" % bak)

    def fernet(self):
        if self._fernet:
            return self._fernet
        from cryptography.fernet import Fernet
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        if not self.keyfile.exists():
            self.keyfile.write_bytes(os.urandom(32))
            try:
                os.chmod(self.keyfile, 0o600)
            except Exception:
                pass
        material = self.keyfile.read_bytes() + _machine_id().encode() + \
            os.environ.get("SIGHT_DEPLOY_PASSPHRASE", "").encode()
        k = hashlib.pbkdf2_hmac("sha256", material, b"sight-deploy-v1", 100000)
        self._fernet = Fernet(base64.urlsafe_b64encode(k))
        return self._fernet

    def save(self):
        with self.lock:
            CONF_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except Exception:
                pass
            os.replace(tmp, self.path)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def hosts(self):
        return self.data["hosts"]

    def get(self, alias):
        return self.data["hosts"].get(alias)

    def secrets(self, alias):
        h = self.get(alias) or {}
        tok = h.get("secret")
        if not tok:
            return {}
        try:
            return json.loads(self.fernet().decrypt(tok.encode()).decode())
        except Exception:
            warn("无法解密已保存的密码（换了电脑或口令变了），需要重新输入")
            return {}

    def put(self, alias, meta, secrets=None):
        with self.lock:
            h = self.data["hosts"].setdefault(alias, {})
            h.update(meta)
            if secrets is not None:
                clean = {k: v for k, v in secrets.items() if v}
                h["secret"] = self.fernet().encrypt(json.dumps(clean).encode()).decode()
            h["last_used"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
            self.save()

    def set_product(self, alias, product, info_):
        with self.lock:
            h = self.data["hosts"].setdefault(alias, {})
            h.setdefault("products", {}).setdefault(product, {}).update(info_)
            self.save()

    def drop_product(self, alias, product):
        with self.lock:
            h = self.data["hosts"].get(alias) or {}
            if (h.get("products") or {}).pop(product, None) is not None:
                self.save()

    def update_settings(self, kv):
        with self.lock:
            self.data.setdefault("settings", {}).update(kv)
            self.save()

    def remove(self, alias):
        with self.lock:
            self.data["hosts"].pop(alias, None)
            self.save()


# =============================================================================
# 安装包
# =============================================================================

def parse_version(v):
    """'2.0.71' → (2, 0, 71)；只取开头的数字段，'1.3.0-rc1' 按 (1, 3, 0) 比较；无法识别返回 ()。"""
    m = re.match(r"\d+(?:\.\d+)*", (v or "").strip().lstrip("vV"))
    return tuple(int(x) for x in m.group(0).split(".")) if m else ()


def read_manifest(fileobj):
    with zipfile.ZipFile(fileobj) as zf:
        raw = zf.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    mf, last = {}, None
    for line in raw.splitlines():
        if line.startswith(" ") and last:
            mf[last] += line[1:]
        elif ":" in line:
            k, v = line.split(":", 1)
            last = k.strip()
            mf[last] = v.strip()
    return mf


def sha256_file(path, label=None):
    h = hashlib.sha256()
    size = os.path.getsize(path)
    prog = Progress(label, size) if label and size > 50 * 1024 * 1024 else None
    done = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            done += len(b)
            if prog:
                prog.update(done)
    if prog:
        prog.finish()
    return h.hexdigest()


class Package:
    """产品安装包：sight-<产品>-<版本>.zip（完整包），或单独的 sight-<产品>.jar（只能用于升级）。"""

    def __init__(self, path):
        self.path = Path(path)
        self.tmp = None
        self.files = []  # [(相对路径, 本地路径, 大小, sha256)]
        self.full = False
        try:
            self._load()
        except BaseException:  # 包括用户点「停止」：解压出来的临时文件（几百 MB）要清掉
            self.cleanup()
            raise

    def _load(self):
        p = self.path
        if not p.is_file():
            raise DeployError("找不到安装包：%s" % p)
        if p.suffix.lower() == ".jar":
            jar_local, rel_root = p, None
            self.full = False
            members = [(p.name, p)]
        elif p.suffix.lower() == ".zip":
            self.tmp = Path(tempfile.mkdtemp(prefix="sight-deploy-"))
            with zipfile.ZipFile(p) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")
                         and "__MACOSX" not in n and not n.endswith(".DS_Store")]
                jars = [n for n in names if re.search(r"(^|/)sight-[a-z]+\.jar$", n)]
                if not jars:
                    raise DeployError("这个 zip 里没有 sight-*.jar，不是 SightReport / SightWall 安装包：%s" % p.name)
                jar_name = sorted(jars, key=len)[0]
                rel_root = jar_name[: -len(Path(jar_name).name)]
                out(dim("    解压安装包 …"))
                members = []
                for n in names:
                    if not n.startswith(rel_root):
                        continue
                    rel = n[len(rel_root):]
                    if rel.startswith("/") or ".." in Path(rel).parts:
                        continue
                    dest = self.tmp / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(n) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                    members.append((rel, dest))
            jar_local = self.tmp / Path(jar_name).name
            self.full = any(r == "install.sh" for r, _ in members)
        else:
            raise DeployError("不支持的安装包格式：%s（需要 .zip 或 .jar）" % p.name)

        self.jar = jar_local.name
        m = re.match(r"(sight-[a-z]+)\.jar$", self.jar)
        self.product = m.group(1) if m else None
        if self.product not in PRODUCTS:
            raise DeployError("无法识别的产品：%s" % self.jar)
        try:
            mf = read_manifest(str(jar_local))
        except Exception as e:
            raise DeployError("读取 jar 失败（文件可能不完整）：%s" % e)
        self.version = mf.get("Product-Version") or (re.findall(r"-(\d+(?:\.\d+)+)\.zip$", p.name) or ["?"])[0]
        self.java_req = int(re.sub(r"\D", "", mf.get("Java-Version", "") or "") or MIN_JAVA)
        sc = mf.get("Start-Class", "")
        if sc and PRODUCTS[self.product]["start_class"] not in sc:
            warn("jar 名与主类不一致（%s / %s），请确认安装包没拿错" % (self.jar, sc))
        for rel, local in members:
            size = os.path.getsize(local)
            sha = sha256_file(local, "校验 %s" % rel if size > 50 * 1024 * 1024 else None)
            self.files.append((rel.replace("\\", "/"), str(local), size, sha))
        self.files.sort(key=lambda x: (x[0] == self.jar, x[0]))  # jar 最后传

    @property
    def name(self):
        return PRODUCTS[self.product]["name"]

    @property
    def jar_sha(self):
        return [f for f in self.files if f[0] == self.jar][0][3]

    def total_size(self):
        return sum(f[2] for f in self.files)

    def cleanup(self):
        if self.tmp and self.tmp.exists():
            shutil.rmtree(self.tmp, ignore_errors=True)


def search_dirs():
    home = Path.home()
    ds = [CONF_DIR / "uploads", APP_DIR, APP_DIR / "packages", APP_DIR.parent.parent / "release", Path.cwd(),
          home / "Downloads", home / "Desktop", home / "下载", home / "桌面", home]
    seen, res = set(), []
    for d in ds:
        try:
            r = d.resolve()
        except Exception:
            continue
        if r not in seen and r.is_dir():
            seen.add(r)
            res.append(r)
    return res


def find_packages():
    found = {}
    for d in search_dirs():
        for pat in ("sight-report*.zip", "sight-wall*.zip"):
            for f in d.glob(pat):
                if f.is_file():
                    found[str(f.resolve())] = f
    return sorted(found.values(), key=lambda f: f.stat().st_mtime, reverse=True)[:12]


def load_package(path):
    step("读取安装包 %s" % os.path.basename(path))
    pkg = Package(norm_path_input(path))
    ok("%s %s（%s，%d 个文件）" % (pkg.name, pkg.version, human_size(pkg.total_size()), len(pkg.files)))
    return pkg


# ---------------------------------------------------------------- JDK 包

def inspect_jdk(path):
    """读取 JDK/JRE tar.gz 包里的 release 文件，返回 {top, major, version, arch, os}。"""
    res = {"path": path}
    try:
        with tarfile.open(path, "r:*") as tf:
            has_java = False
            for m in tf:
                parts = m.name.lstrip("./").split("/")
                if len(parts) == 2 and parts[1] == "release" and m.isfile():
                    res["top"] = parts[0]
                    txt = tf.extractfile(m).read().decode("utf-8", "replace")
                    kv = dict(re.findall(r'^(\w+)="?([^"\n]*)"?', txt, re.M))
                    res["version"] = kv.get("JAVA_VERSION", "")
                    res["arch"] = kv.get("OS_ARCH", "")
                    res["os"] = kv.get("OS_NAME", "")
                    res["vendor"] = kv.get("IMPLEMENTOR", "")
                if len(parts) == 3 and parts[1] == "bin" and parts[2] == "java":
                    has_java = True
                    res.setdefault("top", parts[0])
                if has_java and "version" in res:
                    break
            res["has_java"] = has_java
    except Exception as e:
        raise DeployError("无法读取 JDK 包 %s：%s" % (os.path.basename(path), e))
    v = res.get("version", "")
    res["major"] = java_major(v) if v else 0
    return res


def java_major(v):
    nums = re.findall(r"\d+", v or "")
    if not nums:
        return 0
    if nums[0] == "1" and len(nums) > 1:
        return int(nums[1])
    return int(nums[0])


ARCH_ALIASES = {"x86_64": {"x86_64", "amd64", "x64"}, "aarch64": {"aarch64", "arm64"}}


def find_jdk_archives():
    found = {}
    pats = ["*jdk*21*.tar.gz", "*jre*21*.tar.gz", "*JDK*21*.tar.gz", "OpenJDK21*.tar.gz",
            "*jdk*.tar.gz", "*jre*.tar.gz", "*jdk*.tgz"]
    ds = search_dirs() + [SCRIPT_DIR / "jdk"]
    for d in ds:
        if not d.is_dir():
            continue
        for pat in pats:
            for f in d.glob(pat):
                low = f.name.lower()
                if f.is_file() and not any(x in low for x in ("windows", "mac", "darwin", "osx")):
                    found[str(f.resolve())] = f
    return sorted(found.values(), key=lambda f: f.stat().st_mtime, reverse=True)[:10]


# =============================================================================
# SSH 连接
# =============================================================================

SUDO_PROMPT = "SIGHT_SUDO_PW:"
PRELUDE = 'export LC_ALL=C PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/sbin:/usr/bin:/bin"\n'
NET_ERRORS = (socket.error, EOFError, OSError)


class Remote:
    def __init__(self, store, alias, cfg, secrets):
        self.store, self.alias, self.cfg, self.secrets = store, alias, cfg, dict(secrets or {})
        self.t = None
        self._sftp = None
        self.is_root = False
        self.sudo_mode = None
        self.no_forward = False
        self.secrets_dirty = False

    # ------------------------------------------------------------ 连接
    @property
    def host(self):
        return self.cfg["host"]

    @property
    def user(self):
        return self.cfg.get("user", "root")

    def label(self):
        uh = "%s@%s" % (self.user, self.host)
        return uh if self.alias in (None, self.host, uh) else "%s（%s）" % (self.alias, uh)

    def connect(self, interactive=True):
        for attempt in range(3):
            try:
                self._connect_once()
                break
            except paramiko.AuthenticationException:
                is_key = self.cfg.get("auth") == "key"
                if not interactive or is_key or attempt == 2:
                    raise DeployError("登录 %s 失败：用户名或%s不对" % (self.host, "密钥" if is_key else "密码"),
                                      "可点服务器页上的「修改密码」重新输入")
                fail("登录失败：用户名或密码不对")
                self.secrets["password"] = ask_secret("请重新输入 %s@%s 的密码" % (self.user, self.host))
                self.secrets.pop("sudo_password", None)
                self.secrets_dirty = True
        code, o, _ = self.run_raw("id -u; id -un", timeout=20)
        lines = o.split()
        self.is_root = bool(lines) and lines[0] == "0"
        return self

    def _connect_once(self):
        host, port = self.cfg["host"], int(self.cfg.get("port", 22))
        try:
            sock = socket.create_connection((host, port), timeout=15)
        except socket.timeout:
            raise DeployError("连接 %s:%s 超时" % (host, port), "检查 IP/端口是否正确、网络是否通、防火墙是否放行 SSH")
        except OSError as e:
            raise DeployError("无法连接 %s:%s（%s）" % (host, port, e), "检查 IP/端口是否正确、sshd 是否在运行")
        t = paramiko.Transport(sock)
        t.banner_timeout = 30
        try:
            t.start_client(timeout=30)
        except paramiko.SSHException as e:
            raise DeployError("SSH 握手失败：%s" % e)
        try:
            self._check_host_key(t.get_remote_server_key())
            if self.cfg.get("auth") == "key":
                t.auth_publickey(self.user, load_pkey(self.cfg["key_file"], self.secrets.get("key_passphrase")))
            else:
                pw = self.secrets.get("password")
                if not pw:
                    pw = self.secrets["password"] = ask_secret("%s@%s 的密码" % (self.user, host))
                    self.secrets_dirty = True
                try:
                    t.auth_password(self.user, pw)
                except paramiko.BadAuthenticationType as e:
                    allowed = getattr(e, "allowed_types", []) or []
                    if "keyboard-interactive" in allowed:
                        t.auth_interactive(self.user, lambda title, instr, prompts: [pw for _ in prompts])
                    else:
                        raise DeployError("服务器不允许密码登录（允许的方式：%s）" % ", ".join(allowed),
                                          "请删除这台服务器后重新添加，登录方式选「密钥文件」")
            if not t.is_authenticated():
                raise paramiko.AuthenticationException("not authenticated")
        except BaseException:
            t.close()
            raise
        t.set_keepalive(15)
        self.t = t
        self._sftp = None

    def _check_host_key(self, key):
        fp = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        cur = "%s %s" % (key.get_name(), key.get_base64())
        saved = self.cfg.get("host_key")
        if saved == cur:
            return
        if saved:
            fail("服务器 %s 的身份指纹和上次不一样！" % self.host)
            info("上次：%s" % self.cfg.get("host_key_fp", "?"))
            info("这次：%s" % fp)
            info("可能是服务器重装过系统；也可能连到了别的机器（有被冒充的风险）。")
            if not confirm_typed("确认是服务器重装导致、继续连接", "继续"):
                raise HostKeyError("已中止：服务器指纹不符")
        else:
            out("    首次连接 %s，服务器指纹 %s" % (self.host, dim(fp)))
        self.cfg["host_key"], self.cfg["host_key_fp"] = cur, fp
        self.secrets_dirty = True  # 触发保存

    def reconnect(self):
        try:
            self.close()
        except Exception:
            pass
        for i in range(10):
            check_cancel()
            try:
                self._connect_once()
                return
            except (HostKeyError, paramiko.AuthenticationException):
                raise  # 指纹不符 / 密码被改：重试没有意义
            except (DeployError, paramiko.SSHException, *NET_ERRORS) as e:
                warn("重连失败（%s），%d 秒后重试 …" % (e, 5 + i * 3))
                for _ in range(10 + i * 6):
                    check_cancel()
                    time.sleep(0.5)
        raise DeployError("与服务器的连接断开且无法恢复")

    def close(self):
        if self._sftp:
            try:
                self._sftp.close()
            except Exception:
                pass
        if self.t:
            self.t.close()
        self.t, self._sftp = None, None

    def save_if_dirty(self):
        if self.secrets_dirty and self.alias:
            meta = {k: v for k, v in self.cfg.items() if k not in ("secret", "products")}
            self.store.put(self.alias, meta, self.secrets)
            self.secrets_dirty = False

    @property
    def sftp(self):
        if self._sftp is None:
            self._sftp = paramiko.SFTPClient.from_transport(self.t)
            self._sftp.get_channel().settimeout(90)  # 网络假死时 SFTP 读写别无限期卡住
        return self._sftp

    # ------------------------------------------------------------ 命令执行
    def run_raw(self, cmd, stdin=None, pty=False, timeout=600, on_line=None, wait_for=None, cancellable=False):
        """执行一条命令。wait_for：先等输出里出现这个提示再发送 stdin（pty 下的 sudo 密码，避免被回显）。
        cancellable：用户点「停止」时立即断开（只用于看日志这类可随时中断的命令）。"""
        check_cancel()  # 用户点了「停止」：在两条命令之间中止（不会打断正在执行的命令）
        LOG.write("  [%s] $ %s" % (self.host, cmd if len(cmd) < 600 else cmd[:600] + " …"))
        if not self.t or not self.t.is_active():
            self.reconnect()
        secret = stdin.strip() if stdin else None
        ch = self.t.open_session()
        if pty:
            ch.get_pty(width=200)
        ch.exec_command(cmd)
        pending = stdin
        if pending and not wait_for:
            ch.sendall(pending.encode())
            pending = None
        if not pty and not pending:
            ch.shutdown_write()
        outb, errb, buf = [], [], b""
        t0 = time.time()

        def clean(text):
            if wait_for:
                text = text.replace(wait_for, "")
            if secret:
                text = text.replace(secret, "***")
            return text

        while True:
            got = False
            if ch.recv_ready():
                d = ch.recv(65536)
                outb.append(d)
                got = True
                if pending and wait_for.encode() in b"".join(outb):
                    ch.sendall(pending.encode())
                    pending = None
                    if not pty:
                        ch.shutdown_write()
                if on_line:
                    buf += d
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = clean(line.decode("utf-8", "replace").rstrip("\r"))
                        if line.strip():
                            on_line(line)
            if ch.recv_stderr_ready():
                errb.append(ch.recv_stderr(65536))
                got = True
                if pending and wait_for.encode() in b"".join(errb):
                    ch.sendall(pending.encode())
                    pending = None
                    if not pty:
                        ch.shutdown_write()
            if ch.exit_status_ready() and not ch.recv_ready() and not ch.recv_stderr_ready():
                break
            if timeout and time.time() - t0 > timeout:
                ch.close()
                raise DeployError("服务器命令超时（%d 秒）" % timeout)
            if cancellable:
                j = current_job()
                if j and j.cancel_flag.is_set():
                    ch.close()
                    raise JobCancelled()
            if not got:
                time.sleep(0.03)
        if on_line and buf.strip():
            on_line(clean(buf.decode("utf-8", "replace").rstrip("\r")))
        code = ch.recv_exit_status()
        ch.close()
        o = clean(b"".join(outb).decode("utf-8", "replace"))
        e = clean(b"".join(errb).decode("utf-8", "replace"))
        if pty:
            o = o.replace("\r\n", "\n")
        LOG.write(_mask("    → exit %s%s%s" % (code, ("\n    " + o.strip()[-1500:].replace("\n", "\n    ")) if o.strip() else "",
                                                ("\n    [stderr] " + e.strip()[-800:]) if e.strip() else "")))
        return code, o, e

    def run(self, script, sudo=False, timeout=600, check=False, on_line=None, what=None, cancellable=False):
        inner = "bash -c " + shlex.quote(PRELUDE + script)
        stdin, pty = None, False
        if sudo and not self.is_root:
            self._ensure_sudo()
            if self.sudo_mode == "nopass":
                cmd = "sudo -n -- " + inner
            else:
                # -k：忽略缓存的 sudo 凭据，保证 sudo 一定会读走 stdin 里的密码（不会漏给后面的命令）
                pty = self.sudo_mode == "pass_pty"
                cmd = ("stty -echo 2>/dev/null; sudo -k -S -p %s -- " % SUDO_PROMPT if pty
                       else "sudo -k -S -p '' -- ") + inner
                stdin = self.secrets.get("sudo_password") + "\n"
        else:
            cmd = inner
        code, o, e = self.run_raw(cmd, stdin=stdin, pty=pty, timeout=timeout, on_line=on_line,
                                  wait_for=SUDO_PROMPT if pty else None, cancellable=cancellable)
        if check and code != 0:
            msg = (e.strip() or o.strip()).splitlines()[-5:]
            raise DeployError("%s失败（退出码 %s）%s" % (what or "服务器命令", code,
                                                    ("：\n      " + "\n      ".join(msg)) if msg else ""))
        return code, o, e

    def _ensure_sudo(self):
        if self.sudo_mode:
            return
        code, _, _ = self.run_raw("sudo -n true", timeout=20)
        if code == 0:
            self.sudo_mode = "nopass"
            return
        pw = self.secrets.get("sudo_password") or self.secrets.get("password")
        for attempt in range(3):
            if pw:
                code, o, e = self.run_raw("sudo -k -S -p '' true", stdin=pw + "\n", timeout=30)
                msg = (o + e).lower()
                if code != 0 and "tty" in msg:
                    code, o, e = self.run_raw("stty -echo 2>/dev/null; sudo -k -S -p %s true" % SUDO_PROMPT,
                                              stdin=pw + "\n", pty=True, timeout=30, wait_for=SUDO_PROMPT)
                    msg = (o + e).lower()
                    if code == 0:
                        self.sudo_mode = "pass_pty"
                elif code == 0:
                    self.sudo_mode = "pass"
                if code == 0:
                    if self.secrets.get("sudo_password") != pw:
                        self.secrets["sudo_password"] = pw
                        self.secrets_dirty = True
                    return
                if "not in the sudoers" in msg or "may not run sudo" in msg or "not allowed" in msg:
                    raise DeployError("账号 %s 没有 sudo 权限，无法安装系统服务" % self.user,
                                      "请改用 root 账号，或请管理员把 %s 加入 sudo（wheel）组" % self.user)
                if attempt:
                    fail("sudo 密码不对")
            pw = ask_secret("请输入 %s 的 sudo 密码" % self.user)
        raise DeployError("sudo 认证失败")

    # ------------------------------------------------------------ 文件
    def home(self):
        return self.sftp.normalize(".")

    def mkdirs(self, path):
        parts, cur = path.strip("/").split("/"), ""
        for p in parts:
            cur += "/" + p
            try:
                self.sftp.stat(cur)
            except IOError:
                self.sftp.mkdir(cur)

    def read_file(self, path, sudo=False):
        if not sudo:
            try:
                with self.sftp.open(path, "r") as f:
                    return f.read().decode("utf-8", "replace")
            except IOError as e:
                # 只把「文件不存在 / 没权限」当作读不到；连接断开等错误要往上抛，交给调用方重连
                if getattr(e, "errno", None) in (2, 13) and self.t and self.t.is_active():
                    return None
                raise
        code, o, _ = self.run("cat %s 2>/dev/null" % shlex.quote(path), sudo=True)
        return o if code == 0 else None

    def write_file(self, path, text, mode=0o644):
        with self.sftp.open(path, "w") as f:
            f.write(text.encode("utf-8"))
        self.sftp.chmod(path, mode)

    def remote_sha256(self, paths, sudo=False):
        q = " ".join(shlex.quote(p) for p in paths)
        code, o, _ = self.run("if command -v sha256sum >/dev/null; then sha256sum %s; "
                              "elif command -v openssl >/dev/null; then for f in %s; do "
                              "echo \"$(openssl dgst -sha256 \"$f\" | awk '{print $NF}')  $f\"; done; "
                              "else exit 9; fi" % (q, q), sudo=sudo, timeout=600)
        if code == 9:
            return None
        res = {}
        for line in o.splitlines():
            m = re.match(r"([0-9a-f]{64})\s+\*?(.+)$", line.strip())
            if m:
                res[m.group(2)] = m.group(1)
        return res

    def upload(self, local, remote_path, label):
        size = os.path.getsize(local)
        attempts = 0
        part = remote_path + ".part"
        while True:
            try:
                try:
                    done = self.sftp.stat(part).st_size
                except IOError:
                    done = 0
                if done > size:
                    self.sftp.remove(part)
                    done = 0
                if done:
                    info(dim("断点续传：已有 %s，从中断处继续" % human_size(done)))
                prog = Progress(label, size, start=done)
                with open(local, "rb") as lf, self.sftp.open(part, "r+b" if done else "wb") as rf:
                    rf.set_pipelined(True)
                    if done:
                        rf.seek(done)
                        lf.seek(done)
                    while True:
                        b = lf.read(1 << 18)
                        if not b:
                            break
                        rf.write(b)
                        done += len(b)
                        prog.update(done)
                prog.finish()
                try:
                    self.sftp.remove(remote_path)
                except IOError:
                    pass
                self.sftp.rename(part, remote_path)
                return
            except (paramiko.SSHException, *NET_ERRORS) as e:
                if isinstance(e, IOError) and getattr(e, "errno", None) in (13,):
                    raise DeployError("没有权限写入 %s" % remote_path)
                if self.t and self.t.is_active() and isinstance(e, IOError) and not isinstance(e, socket.timeout):
                    # 连接还在却写失败：多半是服务器磁盘满了，重试没用
                    raise DeployError("写入服务器失败：%s（%s）" % (remote_path, e), "检查服务器磁盘空间：df -h")
                attempts += 1
                if attempts > 8:
                    raise DeployError("上传多次中断，已放弃：%s" % e, "网络稳定后重新运行，会从中断处续传")
                warn("传输中断（%s），第 %d 次重连 …" % (e or type(e).__name__, attempts))
                time.sleep(min(3 * attempts, 15))
                self.reconnect()

    # ------------------------------------------------------------ HTTP（经 SSH 隧道访问 127.0.0.1）
    def http_get(self, port, path, timeout=6):
        if not self.no_forward:
            try:
                ch = self.t.open_channel("direct-tcpip", ("127.0.0.1", int(port)), ("127.0.0.1", 0), timeout=timeout)
                ch.settimeout(timeout)
                ch.sendall(("GET %s HTTP/1.0\r\nHost: 127.0.0.1:%s\r\nConnection: close\r\n\r\n"
                            % (path, port)).encode())
                data = b""
                while True:
                    try:
                        d = ch.recv(65536)
                    except socket.timeout:
                        break
                    if not d:
                        break
                    data += d
                ch.close()
                return _parse_http(data)
            except paramiko.ChannelException as e:
                if getattr(e, "code", None) == 1:  # 服务器禁止端口转发
                    self.no_forward = True
                else:
                    return 0, ""
            except (paramiko.SSHException, *NET_ERRORS):
                if not self.t or not self.t.is_active():
                    self.reconnect()
                return 0, ""
        script = ("if command -v curl >/dev/null; then curl -s --noproxy '*' -m %d -i http://127.0.0.1:%s%s; "
                  "else exec 3<>/dev/tcp/127.0.0.1/%s && printf 'GET %s HTTP/1.0\\r\\nHost: 127.0.0.1\\r\\n\\r\\n' >&3 "
                  "&& timeout %d cat <&3; fi" % (timeout, port, path, port, path, timeout))
        try:
            code, o, _ = self.run(script, timeout=timeout + 10)
        except DeployError:
            return 0, ""
        return _parse_http(o.encode("utf-8", "replace"))

    def health(self, port):
        st, body = self.http_get(port, "/api/health")
        if st != 200:
            return None
        try:
            j = json.loads(body)
        except Exception:
            return None
        if isinstance(j, dict) and "status" not in j and isinstance(j.get("data"), dict):
            j = j["data"]  # 兼容被统一响应体包装的情况
        return j if isinstance(j, dict) else None

    def app_version(self, port):
        st, body = self.http_get(port, "/api/app/version")
        if st != 200:
            return None
        try:
            return _find_key(json.loads(body), "version")
        except Exception:
            return None


def _mask(s):
    """日志脱敏：数据库密码、各类 PASSWORD 环境变量。"""
    s = re.sub(r'("password"\s*:\s*")[^"]*(")', r"\1***\2", s, flags=re.I)
    return re.sub(r"((?:PASSWORD|SECRET|TOKEN)[A-Z_]*=)\S+", r"\1***", s, flags=re.I)


def _parse_http(data):
    if not data:
        return 0, ""
    i, j = data.find(b"\r\n\r\n"), data.find(b"\n\n")
    if i >= 0 and (j < 0 or i <= j):
        head, body = data[:i], data[i + 4:]
    elif j >= 0:
        head, body = data[:j], data[j + 2:]
    else:
        head, body = data, b""
    m = re.match(rb"HTTP/\d(?:\.\d)?\s+(\d+)", head)
    return (int(m.group(1)) if m else 0), body.decode("utf-8", "replace")


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], (str, int, float)):
            return str(obj[key])
        for v in obj.values():
            r = _find_key(v, key)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r:
                return r
    return None


def load_pkey(path, passphrase=None):
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise DeployError("私钥文件不存在：%s" % path)
    if hasattr(paramiko.PKey, "from_path"):
        for kw in ("password", "passphrase"):
            try:
                return paramiko.PKey.from_path(path, **{kw: passphrase})
            except TypeError:
                continue
            except paramiko.PasswordRequiredException:
                raise DeployError("私钥有密码保护，请在服务器管理里填写私钥密码")
            except Exception:
                break
    for name in ("Ed25519Key", "ECDSAKey", "RSAKey"):
        cls = getattr(paramiko, name, None)
        if not cls:
            continue
        try:
            return cls.from_private_key_file(path, password=passphrase)
        except paramiko.PasswordRequiredException:
            raise DeployError("私钥有密码保护，请在服务器管理里填写私钥密码")
        except Exception:
            continue
    raise DeployError("无法读取私钥：%s" % path)


# =============================================================================
# 服务器选择 / 管理
# =============================================================================

def _meta_of(r):
    return {k: v for k, v in r.cfg.items() if k not in ("secret", "products")}


def add_host(store, p):
    """网页「添加服务器」表单 → 连接测试（可能追问 sudo 密码）→ 加密保存。"""
    host = str(p.get("host") or "").strip()
    if not host:
        raise DeployError("请填写服务器地址")
    port = _int(p.get("port") or 22)
    if not 0 < port < 65536:
        raise DeployError("SSH 端口不对：%s" % p.get("port"))
    user = str(p.get("user") or "root").strip()
    auth = "key" if p.get("auth") == "key" else "password"
    cfg = {"host": host, "port": port, "user": user, "auth": auth}
    secrets = {}
    if auth == "key":
        kf = norm_path_input(str(p.get("key_file") or ""))
        if not os.path.isfile(kf):
            raise DeployError("私钥文件不存在：%s" % kf)
        cfg["key_file"] = kf
        secrets["key_passphrase"] = p.get("key_passphrase") or ""
    else:
        secrets["password"] = p.get("password") or ""
        if not secrets["password"]:
            raise DeployError("请填写密码")
    default_name = host if user == "root" else "%s@%s" % (user, host)
    name = str(p.get("name") or "").strip() or default_name
    step("连接 %s@%s:%d" % (user, host, port))
    r = Remote(store, name, cfg, secrets)
    try:
        r.connect()
        ok("连接成功（%s）" % ("root 账号" if r.is_root else "普通账号，将通过 sudo 执行需要管理员权限的操作"))
        if not r.is_root:
            r._ensure_sudo()
            ok("sudo 可用")
        code, o, _ = r.run("uname -sm; . /etc/os-release 2>/dev/null; echo \"$PRETTY_NAME\"")
        info(" / ".join(x for x in o.strip().splitlines() if x))
        old = store.get(name)
        if old and (old.get("host"), old.get("user")) != (host, user):
            raise DeployError("名字「%s」已被另一台服务器使用，请换一个名字" % name)
        store.put(name, _meta_of(r), r.secrets)
        ok("已保存为「%s」（密码加密保存在本机）" % name)
    finally:
        r.close()
    return {"name": name}


def open_host(store, alias):
    cfg = store.get(alias)
    if not cfg:
        raise DeployError("没有名为「%s」的服务器" % alias)
    r = Remote(store, alias, dict(cfg), store.secrets(alias))
    out("连接 %s …" % r.label())
    r.connect()
    r.secrets_dirty = True  # 顺带刷新「最近使用」时间与可能更新的密码 / 指纹
    r.save_if_dirty()
    return r


def edit_host(store, p):
    name = p.get("name")
    cfg = store.get(name)
    if not cfg:
        raise DeployError("没有名为「%s」的服务器" % name)
    r = Remote(store, name, dict(cfg), {})
    if cfg.get("auth") == "key":
        r.secrets["key_passphrase"] = p.get("key_passphrase") or ""
    else:
        r.secrets["password"] = p.get("password") or ""
        if not r.secrets["password"]:
            raise DeployError("请填写新密码")
    step("用新密码连接 %s" % r.label())
    try:
        r.connect(interactive=False)
        if not r.is_root:
            r._ensure_sudo()
        r.secrets_dirty = True
        r.save_if_dirty()
        ok("已更新并验证通过")
    finally:
        r.close()
    return {"name": name}


def test_host(store, name):
    step("测试连接")
    r = open_host(store, name)
    try:
        if not r.is_root:
            r._ensure_sudo()
        ok("连接正常（%s）" % ("root" if r.is_root else "sudo 账号"))
        return {"root": r.is_root}
    finally:
        r.close()


# =============================================================================
# 服务器环境探测
# =============================================================================

def parse_kv(text):
    res, multi = {}, {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            if k in res:
                multi.setdefault(k, [res[k]]).append(v.strip())
            res[k] = v.strip()
    for k, vs in multi.items():
        res[k] = vs
    return res


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


PROBE_SH = r'''
D=__DIR__; P=__PORT__; SVC=__SVC__; JAR=__JAR__
echo "os=$(uname -s)"; echo "arch=$(uname -m)"
if [ -r /etc/os-release ]; then . /etc/os-release; echo "distro=${PRETTY_NAME:-$ID}"; echo "distro_id=$ID"; fi
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then echo systemd=1; else echo systemd=0; fi
echo "mem_mb=$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo 2>/dev/null)"
echo "cpus=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null)"
d="$D"; while [ ! -d "$d" ]; do d=$(dirname "$d"); done
df -Pk "$d" 2>/dev/null | awk 'NR==2{print "disk_free_mb=" int($4/1024); print "disk_mount=" $6}'
seen=" "
for j in "${JAVA_HOME:+$JAVA_HOME/bin/java}" "$(command -v java 2>/dev/null)" /opt/sight-jdk/*/bin/java \
         /usr/lib/jvm/*/bin/java /usr/lib/jvm/*/jre/bin/java /usr/java/*/bin/java /usr/local/*/bin/java \
         /opt/*/bin/java /opt/*/*/bin/java; do
  [ -n "$j" ] && [ -x "$j" ] || continue
  r=$(readlink -f "$j" 2>/dev/null || echo "$j")
  case "$seen" in *" $r "*) continue;; esac; seen="$seen$r "
  v=$("$r" -version 2>&1 | grep -i -m1 'version' | sed -E 's/.*version "([^"]+)".*/\1/')
  echo "java=$r|$v"
done
echo "java_path=$(readlink -f "$(command -v java 2>/dev/null)" 2>/dev/null)"
if ldconfig -p 2>/dev/null | grep -q 'libfreetype\.so'; then echo freetype=1; else echo freetype=0; fi
if ldconfig -p 2>/dev/null | grep -q 'libfontconfig\.so'; then echo fontconfig=1; else echo fontconfig=0; fi
busy=0
if command -v ss >/dev/null 2>&1; then ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$P\$" && busy=1
elif command -v netstat >/dev/null 2>&1; then netstat -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$P\$" && busy=1
elif (exec 3<>/dev/tcp/127.0.0.1/$P) 2>/dev/null; then busy=1; fi
echo "port_busy=$busy"
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  echo fw=firewalld; firewall-cmd --query-port=$P/tcp >/dev/null 2>&1 && echo fw_open=1 || echo fw_open=0
elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q 'Status: active'; then
  echo fw=ufw; ufw status 2>/dev/null | grep -Eq "^$P(/tcp)?[[:space:]].*ALLOW" && echo fw_open=1 || echo fw_open=0
else echo fw=none; fi
for t in tar gzip sha256sum setsid; do command -v $t >/dev/null 2>&1 && echo "tool_$t=1" || echo "tool_$t=0"; done
for pm in dnf yum apt-get zypper; do command -v $pm >/dev/null 2>&1 && { echo "pkg=$pm"; break; }; done
[ -e "$D" ] && echo dir_exists=1 && echo "dir_entries=$(ls -A "$D" 2>/dev/null | wc -l)"
[ -f "$D/$JAR" ] && echo dir_has_jar=1
if [ -f "/etc/systemd/system/$SVC.service" ]; then
  echo unit=1; echo "unit_dir=$(sed -n 's/^WorkingDirectory=//p' "/etc/systemd/system/$SVC.service" | head -1)"
fi
true
'''

DETECT_SH = r'''
SVC=__SVC__; JAR=__JAR__
u="/etc/systemd/system/$SVC.service"
if [ -f "$u" ]; then w=$(sed -n 's/^WorkingDirectory=//p' "$u" | head -1); [ -n "$w" ] && echo "cand=$w|unit"; fi
for p in $(pgrep -f -- "$JAR" 2>/dev/null); do
  e=$(readlink "/proc/$p/exe" 2>/dev/null); case "$e" in */java) ;; *) continue;; esac
  c=$(readlink "/proc/$p/cwd" 2>/dev/null)
  a=$(tr '\0' '\n' < "/proc/$p/cmdline" 2>/dev/null | grep -m1 -- "$JAR")
  case "$a" in /*) echo "cand=$(dirname "$a")|proc";; *) [ -n "$c" ] && echo "cand=$c|proc";; esac
done
for d in /opt/$SVC /opt/sight/$SVC /opt/sight-data/$SVC /usr/local/$SVC /data/$SVC /data/sight/$SVC /app/$SVC \
         /srv/$SVC /root/$SVC /home/*/$SVC /opt/*/$SVC; do
  [ -f "$d/$JAR" ] && echo "cand=$d|dir"
done
true
'''

INSPECT_SH = r'''
D=__DIR__; SVC=__SVC__; JAR=__JAR__
R=$(readlink -f "$D")
echo "owner=$(stat -c %U "$D/$JAR" 2>/dev/null)"
# 只有 unit 的 WorkingDirectory 就是这个目录时，才算「这套安装是系统服务」（同机另一套手工启动的不能误停生产服务）
U="/etc/systemd/system/$SVC.service"; mine=0
if [ -f "$U" ]; then
  w=$(sed -n 's/^WorkingDirectory=//p' "$U" | head -1); [ "$(readlink -f "$w" 2>/dev/null)" = "$R" ] && mine=1
  [ $mine = 0 ] && echo "other_unit_dir=$w"
fi
echo unit=$mine
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  echo systemd=1
  if [ $mine = 1 ]; then
    echo "active=$(systemctl is-active "$SVC" 2>/dev/null)"
    systemctl cat "$SVC" 2>/dev/null | grep -E '^(Environment|ExecStart)=' | sed 's/^/unitline=/'
  fi
else echo systemd=0; fi
[ -L "$D/data" ] && echo "data_link=$(readlink -f "$D/data")"
for p in $(pgrep -f -- "$JAR" 2>/dev/null); do
  e=$(readlink "/proc/$p/exe" 2>/dev/null); case "$e" in */java) ;; *) continue;; esac
  c=$(readlink -f "/proc/$p/cwd" 2>/dev/null); a=$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)
  case "$a" in *"$R/$JAR"*|*"$D/$JAR"*) ;; *) [ "$c" = "$R" ] || continue;; esac
  echo "pid=$p"; echo "pid_java=$e"; echo "pid_args=$a"
  tr '\0' '\n' < "/proc/$p/environ" 2>/dev/null \
    | grep -E '^(SERVER_PORT|CHECK_PORT|JVM_XMX|JVM_XMS|JVM_RAM_PCT|JVM_OPTS|SPRING_PROFILES_ACTIVE|MR_[A-Z_]+)=' \
    | sed 's/^/pid_env_/'
  if command -v ss >/dev/null 2>&1; then ss -ltnp 2>/dev/null | grep "pid=$p," | awk '{print $4}'
  else netstat -ltnp 2>/dev/null | grep " $p/" | awk '{print $4}'; fi | sed -E 's/.*[:.]([0-9]+)$/pid_port=\1/'
done
if [ -f "$D/data/work/database.config" ]; then echo "dbconf=$(tr -d '\n' < "$D/data/work/database.config")"; fi
[ -f "$D/__MARKER__" ] && echo "marker=$(tr -d '\n' < "$D/__MARKER__")"
d="$D"; df -Pk "$d" 2>/dev/null | awk 'NR==2{print "disk_free_mb=" int($4/1024)}'
true
'''


def render(sh, **kw):
    for k, v in kw.items():
        sh = sh.replace("__%s__" % k.upper(), shlex.quote(str(v)) if k.upper() not in ("MARKER",) else v)
    return sh


def probe_env(r, product, install_dir, port):
    p = PRODUCTS[product]
    code, o, _ = r.run(render(PROBE_SH, dir=install_dir, port=port, svc=product, jar=p["jar"]),
                       sudo=True, timeout=120)
    kv = parse_kv(o)
    javas = []
    for item in as_list(kv.get("java")):
        path, _, ver = item.partition("|")
        javas.append({"path": path, "version": ver, "major": java_major(ver)})
    kv["javas"] = javas
    return kv


def find_installs(r, product):
    p = PRODUCTS[product]
    code, o, _ = r.run(render(DETECT_SH, svc=product, jar=p["jar"]), sudo=True, timeout=60)
    cands = []
    for item in as_list(parse_kv(o).get("cand")):
        d, _, src = item.partition("|")
        d = d.rstrip("/")
        if d and d not in [c[0] for c in cands]:
            cands.append((d, src))
    return cands


def inspect_install(r, product, install_dir):
    p = PRODUCTS[product]
    code, o, _ = r.run(render(INSPECT_SH, dir=install_dir, svc=product, jar=p["jar"], marker=MARKER),
                       sudo=True, timeout=60)
    kv = parse_kv(o)
    owner = kv.get("owner") or "root"
    if owner == "UNKNOWN":  # 文件属主的 uid 在系统里没有对应账号
        warn("%s 的属主账号不存在，按 root 处理" % posix_join(install_dir, p["jar"]))
        owner = "root"
    inst = {"dir": install_dir, "product": product, "owner": owner,
            "other_unit_dir": kv.get("other_unit_dir"), "data_link": kv.get("data_link"),
            "listen_ports": sorted(set(_int(x) for x in as_list(kv.get("pid_port")) if _int(x))),
            "unit": kv.get("unit") == "1", "systemd": kv.get("systemd") == "1",
            "active": kv.get("active", ""), "pids": as_list(kv.get("pid")),
            "pid_java": as_list(kv.get("pid_java"))[:1], "disk_free_mb": _int(kv.get("disk_free_mb"))}
    try:
        inst["marker"] = json.loads(kv.get("marker") or "{}")
    except Exception:
        inst["marker"] = {}
    # 运行中进程的环境变量（非系统服务方式启动时，重启要沿用这些）
    proc_env = {}
    for k, v in kv.items():
        if k.startswith("pid_env_"):
            proc_env[k[8:]] = as_list(v)[0]
    inst["proc_env"] = proc_env
    args0 = (as_list(kv.get("pid_args")) or [""])[0]
    m = re.search(r"--spring\.profiles\.active=(\S+)", args0)
    inst["profile"] = inst["marker"].get("profile") or (m.group(1) if m else "prod")
    # 端口：进程环境 > unit/drop-in > 进程参数 > 标记文件 > 默认
    port = None
    texts = as_list(kv.get("unitline")) + as_list(kv.get("pid_args"))
    if proc_env.get("SERVER_PORT"):
        port = _int(proc_env["SERVER_PORT"])
    if not port:
        for t in texts:
            m = re.search(r"(?:SERVER_PORT=|server\.port=)(\d+)", t)
            if m:
                port = int(m.group(1))
                break
    inst["port"] = port or inst["marker"].get("port") or p["port"]
    # 数据库
    inst["db"] = classify_db(kv.get("dbconf"), texts, install_dir)
    # 版本（读 jar 的 MANIFEST，经 SFTP 随机读取，几 KB 流量）
    inst["version"], inst["java_req"] = remote_jar_version(r, posix_join(install_dir, p["jar"]))
    inst["java"] = None
    if inst["pid_java"]:
        code, o, _ = r.run('"%s" -version 2>&1 | grep -i -m1 version' % inst["pid_java"][0])
        m = re.search(r'version "([^"]+)"', o)
        inst["java"] = {"path": inst["pid_java"][0], "version": m.group(1) if m else "",
                        "major": java_major(m.group(1)) if m else 0}
    return inst


def start_env_of(inst):
    """非系统服务方式启动时要带的环境变量：标记文件 > 当前进程环境；并确保 PATH 里有它在用的 java。"""
    if inst["unit"]:
        return {}
    env = dict(inst["marker"].get("env") or {})
    if not env:
        env = dict(inst.get("proc_env") or {})
        if inst["port"] != PRODUCTS[inst["product"]]["port"]:
            env.setdefault("SERVER_PORT", str(inst["port"]))
            env.setdefault("CHECK_PORT", str(inst["port"]))
    if "PATH" not in env and inst.get("java"):
        env["PATH"] = posixdir(inst["java"]["path"]) + ":/usr/local/bin:/usr/bin:/bin"
    return env


def _int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return 0


def posix_join(*a):
    return "/".join([a[0].rstrip("/")] + [x.strip("/") for x in a[1:]])


def classify_db(dbconf, unit_texts, install_dir):
    url = None
    for t in unit_texts:
        m = re.search(r"MR_DB_URL=([^\s\"']+)", t)
        if m:
            url = m.group(1)
    if not url and dbconf:
        try:
            url = json.loads(dbconf).get("url")
        except Exception:
            m = re.search(r'"url"\s*:\s*"([^"]+)"', dbconf)
            url = m.group(1) if m else None
    if not url:
        return {"kind": "none", "desc": "尚未配置（还没走 /setup 向导）"}
    low = url.lower()
    if low.startswith("jdbc:h2:") and not low.startswith("jdbc:h2:tcp") and not low.startswith("jdbc:h2:ssl"):
        path = url[8:].split(";")[0]
        path = re.sub(r"^(file:|mem:)", "", path)
        inside = not path.startswith(("/", "~")) or path.startswith(install_dir.rstrip("/") + "/")
        return {"kind": "h2", "url": url, "inside": inside,
                "desc": "内置 H2 数据库" + ("（在安装目录内，随 data/ 一起备份）" if inside
                                         else "（文件在安装目录之外：%s，不会被自动备份）" % path)}
    m = re.match(r"jdbc:([a-z0-9]+):(?:thin:@)?/*([^/;?:]+)?", low)
    kind = m.group(1) if m else "db"
    hostpart = m.group(2) if m and m.group(2) else ""
    return {"kind": "external", "url": url, "desc": "外部数据库 %s%s" % (kind, (" @ " + hostpart) if hostpart else "")}


def remote_jar_version(r, path):
    try:
        with r.sftp.open(path, "rb") as f:
            mf = read_manifest(f)
            return mf.get("Product-Version", "未知"), int(re.sub(r"\D", "", mf.get("Java-Version", "") or "") or 0)
    except Exception:
        pass
    code, o, _ = r.run("command -v unzip >/dev/null && unzip -p %s META-INF/MANIFEST.MF" % shlex.quote(path),
                       sudo=True)
    m = re.search(r"Product-Version:\s*(\S+)", o or "")
    return (m.group(1) if m else "未知"), 0


# =============================================================================
# 健康检查
# =============================================================================

def _n_restarts(r, product):
    code, o, _ = r.run("systemctl show -p NRestarts %s 2>/dev/null" % shlex.quote(product))
    m = re.search(r"NRestarts=(\d+)", o)
    return int(m.group(1)) if m else None


def wait_healthy(r, product, inst_dir, port, need_bootstrapped, timeout, use_unit):
    t0 = time.time()
    last_check = 0
    restarts0 = _n_restarts(r, product) if use_unit else None
    lid = "wait%d" % int(t0 * 1000)
    msg = ""

    def done(res, h, text=None, good=True):
        live(lid, "")
        if text:
            (ok if good else fail)(text)
        return res, h

    while True:
        check_cancel()
        el = time.time() - t0
        try:
            h = r.health(port)
        except DeployError:
            h = None
        if h and h.get("status") == "UP":
            if not need_bootstrapped or h.get("bootstrapped"):
                return done(True, h, "服务已就绪（用时 %s）" % fmt_secs(el))
            msg = "已响应，正在初始化 / 升级数据库结构 …"
            if h.get("dbConfigured") and h.get("dbReachable") is False:
                msg = "已响应，但连不上数据库 …"
        else:
            msg = "等待服务启动 …"
        if el - last_check > 10:
            last_check = el
            if use_unit:
                code, o, _ = r.run("systemctl is-active %s" % shlex.quote(product))
                n = _n_restarts(r, product) if restarts0 is not None else None
                crashing = n is not None and n - restarts0 >= 2  # systemd 在反复自动拉起：启动即崩溃
                if o.strip() == "failed" or crashing:
                    return done(False, h, "服务启动失败（%s）" % (
                        "systemd 已自动重启 %d 次仍失败" % (n - restarts0) if crashing else "systemd 状态 failed"), False)
            elif el > 20:
                # 只认 /proc/<pid>/exe 是 java 的进程（pgrep -f 会匹配到执行这条命令的 shell 自己）
                code, o, _ = r.run('for p in $(pgrep -f -- %s); do case "$(readlink /proc/$p/exe)" in '
                                   '*/java) echo alive;; esac; done' %
                                   shlex.quote(posix_join(inst_dir, PRODUCTS[product]["jar"])), sudo=True)
                if "alive" not in o:
                    return done(False, h, "Java 进程已退出", False)
        if el > timeout:
            return done(False, h, "等待 %d 秒仍未就绪：%s" % (timeout, msg), False)
        live(lid, "%s %s" % (msg, fmt_secs(el)))
        for _ in range(6):
            check_cancel()
            time.sleep(0.5)


def fetch_logs(r, product, inst_dir, unit, n=200):
    """取最近日志：systemd 日志 + 应用日志文件（错误日志优先）。返回 [(标题, 文本)]。"""
    parts = []
    if unit:
        code, o, _ = r.run("journalctl -u %s -n %d --no-pager 2>/dev/null | tail -n %d" % (
            shlex.quote(product), n, n), sudo=True)
        if o.strip():
            parts.append(("journalctl -u %s" % product, strip_ansi(o.rstrip())))
    for f in ("logs/sight-data-error.log", "logs/console.out", "logs/sight-data-info.log"):
        path = posix_join(inst_dir, f)
        code, o, _ = r.run("[ -s %s ] && tail -n %d %s" % (shlex.quote(path), n, shlex.quote(path)), sudo=True)
        if o.strip():
            parts.append((f, strip_ansi(o.rstrip())))
    return parts


def show_logs(r, product, inst_dir, unit, n=60):
    step("最近日志（点开「服务器输出」查看）")
    parts = fetch_logs(r, product, inst_dir, unit, n)
    if not parts:
        info("没有找到日志")
    for title, text in parts[:2]:
        info("—— %s ——" % title)
        for line in text.splitlines()[-n:]:
            logline(line)


# =============================================================================
# 服务器端脚本片段
# =============================================================================

def svc_functions(product, inst_dir, owner, unit, profile, env):
    """生成 stop_svc / start_svc / jar_pids 三个 bash 函数（升级、回滚、启停共用）。"""
    jar = PRODUCTS[product]["jar"]
    env_s = " ".join(shlex.quote("%s=%s" % (k, v)) for k, v in sorted((env or {}).items()))
    start_cmd = "cd %s || exit 1; $SS nohup env %s bash ./start.sh %s >> logs/console.out 2>&1 < /dev/null &" % (
        shlex.quote(inst_dir), env_s, shlex.quote(profile or "prod"))
    return r'''
D=%(d)s; J=%(j)s; SVC=%(svc)s; UNIT=%(unit)s; OWNER=%(owner)s
TAROPT=""; tar --version 2>/dev/null | grep -q GNU && TAROPT="--anchored --no-wildcards-match-slash"
say(){ echo ">> $*"; }
jar_pids(){
  R=$(readlink -f "$D")
  for p in $(pgrep -f -- "$J" 2>/dev/null); do
    e=$(readlink "/proc/$p/exe" 2>/dev/null); case "$e" in */java) ;; *) continue;; esac
    c=$(readlink -f "/proc/$p/cwd" 2>/dev/null); a=$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)
    case "$a" in *"$R/$J"*|*"$D/$J"*) echo "$p";; *) [ "$c" = "$R" ] && echo "$p";; esac
  done
}
stop_svc(){
  if [ "$UNIT" = 1 ]; then systemctl stop "$SVC" || return 1; fi
  ps_=$(jar_pids)
  if [ -n "$ps_" ]; then
    kill $ps_ 2>/dev/null
    i=0; while [ -n "$(jar_pids)" ] && [ $i -lt 90 ]; do sleep 1; i=$((i+1)); done
    ps_=$(jar_pids); [ -n "$ps_" ] && { say "90 秒内未退出，强制结束"; kill -9 $ps_ 2>/dev/null; sleep 2; }
  fi
  rm -f "$D/${J%%.jar}.pid"
  [ -z "$(jar_pids)" ]
}
start_svc(){
  if [ "$UNIT" = 1 ]; then systemctl daemon-reload 2>/dev/null; systemctl start "$SVC"; return $?; fi
  mkdir -p "$D/logs"; chown "$OWNER": "$D/logs" 2>/dev/null
  if [ "$(id -u)" = 0 ] && [ "$OWNER" != root ] && [ -n "$OWNER" ]; then
    su -s /bin/bash "$OWNER" -c %(start_q)s
  else
    bash -c %(start_q)s
  fi
}
''' % {"d": shlex.quote(inst_dir), "j": shlex.quote(jar), "svc": shlex.quote(product),
       "unit": "1" if unit else "0", "owner": shlex.quote(owner or "root"),
       "start_q": shlex.quote(start_cmd.replace("$SS", "$(command -v setsid)"))}


def tar_excludes():
    # $TAROPT（GNU tar 下为 --anchored --no-wildcards-match-slash）必须写在 --exclude 之前：
    # 让 ./*.pid 之类只匹配安装目录顶层，不会误伤 data/ 深处同名后缀的用户文件
    return "$TAROPT " + " ".join("--exclude=%s" % shlex.quote(x) for x in BACKUP_EXCLUDES)


def ensure_no_remote_task(r, staging):
    """服务器上还有上一次的升级 / 回滚在后台跑（比如用户点了「停止等待」）：拒绝再动这套安装。"""
    code, o, _ = r.run('for st in %s/*.status; do [ -f "$st" ] || continue; [ "$(cat "$st")" = RUNNING ] || continue; '
                       'p=$(cat "$st.pid" 2>/dev/null); [ -n "$p" ] && kill -0 "$p" 2>/dev/null && echo "busy=$st"; done; true'
                       % shlex.quote(staging), sudo=True)
    m = re.search(r"busy=(\S+)", o)
    if m:
        name = m.group(1).rsplit("/", 1)[-1].replace(".status", "")
        raise DeployError("服务器上的%s任务还在后台执行，请等它结束后再操作" % {"upgrade": "升级", "rollback": "回滚"}.get(name, ""),
                          "通常几分钟内结束，稍后点「刷新」查看；执行日志：%s" % m.group(1).replace(".status", ".log"))


def run_detached(r, staging, name, script, timeout=7200):
    """把脚本传到服务器后台执行（与 SSH 会话脱钩：网络断了也会跑完），本地轮询日志。"""
    sp, lp, stp = (posix_join(staging, name + x) for x in (".sh", ".log", ".status"))
    ensure_no_remote_task(r, staging)
    full = "#!/bin/bash\n" + PRELUDE + "ST=%s\necho RUNNING > \"$ST\"\necho $$ > \"$ST.pid\"\n" % \
        shlex.quote(stp) + script
    r.run("rm -f %s %s %s" % (shlex.quote(sp), shlex.quote(lp), shlex.quote(stp)), sudo=True)
    r.write_file(sp, full, 0o700)
    # 有 systemd 时用 systemd-run 跑成临时单元：彻底脱离 SSH 会话（即便 logind 配了 KillUserProcesses 也不会被杀）；
    # KillMode=process：脚本结束时不连带杀掉它拉起的进程（非系统服务方式启动的 java）。
    inner = "exec bash %s > %s 2>&1 < /dev/null" % (shlex.quote(sp), shlex.quote(lp))
    r.run(r'''cd %(stg)s
if [ -d /run/systemd/system ] && command -v systemd-run >/dev/null 2>&1 && \
   { systemd-run --collect --unit=sight-deploy-%(name)s-$(date +%%s) -p KillMode=process /bin/bash -c %(inner)s \
     || systemd-run --unit=sight-deploy-%(name)s-$(date +%%s)b -p KillMode=process /bin/bash -c %(inner)s; } >/dev/null 2>&1; then
  exit 0
fi
if command -v setsid >/dev/null 2>&1; then setsid nohup bash -c %(inner)s >/dev/null 2>&1 < /dev/null &
else nohup bash -c %(inner)s >/dev/null 2>&1 < /dev/null & fi
exit 0''' % {"stg": shlex.quote(staging), "name": name, "inner": shlex.quote(inner)},
          sudo=True, check=True, what="启动后台任务")
    t0 = time.time()
    time.sleep(1)
    try:
        return _poll_task(r, lp, stp, t0, timeout)
    except KeyboardInterrupt:
        raise DeployError("已停止等待（服务器上的任务仍在继续执行，不受影响）",
                          "稍后点「刷新」确认结果；服务器上的执行日志：%s" % lp)


def _poll_task(r, lp, stp, t0, timeout):
    offset, last_alive = 0, time.time()
    while True:
        check_cancel()
        try:
            if not r.t or not r.t.is_active():
                raise EOFError("connection lost")
            text = r.read_file(lp) or ""
            new = text[offset:]
            if new:
                complete = new.rsplit("\n", 1)
                if len(complete) == 2:
                    for line in complete[0].splitlines():
                        _print_task_line(line)
                    offset += len(complete[0]) + 1
            status = (r.read_file(stp) or "").strip()
            if status.startswith("OK") or status.startswith("FAIL"):
                rest = (r.read_file(lp) or "")[offset:]
                for line in rest.splitlines():
                    _print_task_line(line)
                return status
            if time.time() - last_alive > 20:
                last_alive = time.time()
                code, o, _ = r.run('p=$(cat %s 2>/dev/null); [ -n "$p" ] && kill -0 "$p" 2>/dev/null && echo alive'
                                   % shlex.quote(stp + ".pid"), sudo=True)
                if "alive" not in o and not (r.read_file(stp) or "").strip().startswith(("OK", "FAIL")):
                    return "FAIL:killed"
        except (paramiko.SSHException, *NET_ERRORS):
            warn("与服务器的连接中断，服务器上的任务会继续执行；正在重连 …")
            r.reconnect()
        if time.time() - t0 > timeout:
            raise DeployError("后台任务超过 %d 分钟未结束，请登录服务器查看 %s" % (timeout // 60, lp))
        time.sleep(1.5)


def _print_task_line(line):
    line = line.rstrip()
    if not line:
        return
    if line.startswith(">> "):
        info(line[3:])
    else:
        logline(line)


def staging_dir(r, product, inst_dir):
    if r.is_root:
        return posix_join(inst_dir, ".sight-staging")
    return posix_join(r.home(), ".sight-staging", product)


def upload_package(r, pkg, staging):
    """上传安装包到 staging/pkg/（断点续传 + sha256 校验；已传过的同一文件直接跳过）。"""
    step("上传安装包（%s）" % human_size(pkg.total_size()))
    if r.is_root:
        r.run("mkdir -p %s" % shlex.quote(posix_join(staging, "pkg")), check=True)
    else:
        r.mkdirs(posix_join(staging, "pkg"))
    code, o, _ = r.run('d=%s; while [ ! -d "$d" ]; do d=$(dirname "$d"); done; df -Pk "$d" | awk \'NR==2{print $4}\''
                       % shlex.quote(staging))
    free_kb = _int(o.strip().splitlines()[-1] if o.strip() else 0)
    if free_kb and free_kb * 1024 < pkg.total_size() * 1.1:
        raise DeployError("上传暂存目录 %s 所在磁盘剩余 %s，放不下安装包（%s）" % (
            staging, human_size(free_kb * 1024), human_size(pkg.total_size())), "清理该磁盘后重试")
    remote_paths = {rel: posix_join(staging, "pkg", rel) for rel, _, _, _ in pkg.files}
    existing = r.remote_sha256([p for p in remote_paths.values()]) or {}
    for rel, local, size, sha in pkg.files:
        rp = remote_paths[rel]
        if existing.get(rp) == sha:
            info(dim("%s 已在服务器上（校验一致），跳过" % rel))
            continue
        parent = rp.rsplit("/", 1)[0]
        r.mkdirs(parent)
        if size > 2 * 1024 * 1024:
            r.upload(local, rp, rel)
        else:
            r.sftp.put(local, rp)
    sums = r.remote_sha256(list(remote_paths.values()))
    if sums is None:
        for rel, local, size, sha in pkg.files:
            if r.sftp.stat(remote_paths[rel]).st_size != size:
                raise DeployError("%s 上传后大小不一致" % rel)
        warn("服务器没有 sha256sum，只核对了文件大小")
    else:
        bad = [rel for rel, _, _, sha in pkg.files if sums.get(remote_paths[rel]) != sha]
        if bad:
            for rel in bad:
                try:
                    r.sftp.remove(remote_paths[rel])
                except IOError:
                    pass
            raise DeployError("上传后校验不一致：%s" % ", ".join(bad), "已删除损坏的文件，请重新运行")
    ok("上传完成，sha256 校验一致")


def copy_files_script(pkg, staging, inst_dir, owner):
    lines = []
    for rel, _, _, _ in pkg.files:
        src = posix_join(staging, "pkg", rel)
        dst = posix_join(inst_dir, rel)
        parent = dst.rsplit("/", 1)[0]
        if rel == pkg.jar:
            continue
        lines.append("mkdir -p %s && cp -f %s %s" % (shlex.quote(parent), shlex.quote(src), shlex.quote(dst)))
    jsrc, jdst = posix_join(staging, "pkg", pkg.jar), posix_join(inst_dir, pkg.jar)
    lines.append("cp -f %s %s.new && mv -f %s.new %s" % (shlex.quote(jsrc), shlex.quote(jdst),
                                                         shlex.quote(jdst), shlex.quote(jdst)))
    lines.append("{ chmod +x %s/*.sh 2>/dev/null || true; }" % shlex.quote(inst_dir))
    if owner and owner != "root":
        for rel, _, _, _ in pkg.files:
            lines.append("chown %s: %s" % (shlex.quote(owner), shlex.quote(posix_join(inst_dir, rel))))
    # 全程用 && 串起来、不出现 ;：任何一步失败整串就失败（注意 if/set -e 对子 shell 的豁免规则）
    return " && \\\n  ".join(lines)


def marker_json(product, version, port, profile, env, java_home, prev=None):
    m = dict(prev or {})
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m.setdefault("installed_at", now)
    m.update({"product": product, "version": version, "port": port, "profile": profile or "prod",
              "env": env or {}, "updated_at": now, "tool": "sight-deploy " + TOOL_VERSION})
    if java_home:
        m["java_home"] = java_home
    return json.dumps(m, ensure_ascii=False)


def heredoc(path, content):
    tag = "SIGHT_EOF_%s" % uuid.uuid4().hex[:8]
    return "cat > %s <<'%s'\n%s\n%s\n" % (shlex.quote(path), tag, content, tag)


# =============================================================================
# Java 处理
# =============================================================================

def resolve_java(r, env, min_major, jdk_arg=None):
    """确保服务器上有 >= min_major 的 Java。返回 java 可执行文件路径。"""
    good = [j for j in env["javas"] if j["major"] >= min_major]
    if good:
        cur = env.get("java_path")
        pick = next((j for j in good if j["path"] == cur), good[0])
        return pick["path"]
    have = ", ".join("%s（%s）" % (j["path"], j["version"]) for j in env["javas"]) or "无"
    fail("服务器上没有 Java %d+（现有：%s）" % (min_major, have))
    arch = env.get("arch", "")
    while True:
        if jdk_arg:
            choice, jdk_arg_used = "upload", jdk_arg
        else:
            jdk_arg_used = None
            opts = [("上传本机的 JDK 包（推荐，服务器不用联网）", "upload")]
            if env.get("pkg"):
                opts.append(("用服务器的 %s 在线安装 OpenJDK %d（服务器需能访问软件源）" % (env["pkg"], min_major), "online"))
            opts.append(("服务器上已装在别处，我来输入 java 路径", "manual"))
            choice = choose("服务器上没有 Java %d+，如何安装？" % min_major, opts, 1)
            if choice is None:
                raise UserAbort()
        try:
            if choice == "upload":
                return upload_jdk(r, arch, min_major, jdk_arg_used)
            if choice == "online":
                return online_install_java(r, env, min_major)
            if choice == "manual":
                p = ask("java 可执行文件路径或 JAVA_HOME 目录")
                cand = p.rstrip("/")
                code, o, _ = r.run('j=%s; [ -d "$j" ] && j="$j/bin/java"; readlink -f "$j"; "$j" -version 2>&1 | '
                                   "grep -i -m1 version" % shlex.quote(cand))
                lines = o.strip().splitlines()
                m = re.search(r'version "([^"]+)"', o)
                if code == 0 and m and java_major(m.group(1)) >= min_major:
                    ok("Java %s：%s" % (m.group(1), lines[0]))
                    return lines[0]
                fail("这个路径不是 Java %d+：%s" % (min_major, o.strip()[:200]))
        except DeployError as e:
            fail(str(e))
            if e.hint:
                info(e.hint)
            if jdk_arg:
                raise
        jdk_arg = None


def upload_jdk(r, arch, min_major, path=None):
    if not path:
        x = "x64" if arch == "x86_64" else arch
        path = ask_file("选择 JDK 安装包", "jdk", "JDK / JRE 压缩包（.tar.gz）", detail=[
            "需要 Linux %s 版、Java %d 或更高的 JDK / JRE 压缩包（.tar.gz），例如 Eclipse Temurin：" % (x, min_major),
            "https://adoptium.net/zh-CN/temurin/releases/?version=%d&os=linux&arch=%s" % (min_major, x),
            "国内镜像：https://mirrors.tuna.tsinghua.edu.cn/Adoptium/%d/jdk/%s/linux/" % (min_major, x)])
    out("检查 JDK 包 …")
    j = inspect_jdk(path)
    if not j.get("has_java") or not j.get("top"):
        raise DeployError("%s 不是 JDK/JRE 包（里面没有 bin/java）" % os.path.basename(path))
    if j.get("os") and j["os"].lower() != "linux":
        raise DeployError("这是 %s 版的 JDK，服务器需要 Linux 版" % j["os"])
    want = ARCH_ALIASES.get(arch, {arch})
    if j.get("arch") and j["arch"].lower() not in want:
        raise DeployError("JDK 是 %s 架构，服务器是 %s，不匹配" % (j["arch"], arch),
                          "请下载 Linux %s 版本" % ("x64" if arch == "x86_64" else arch))
    if j["major"] and j["major"] < min_major:
        raise DeployError("JDK 版本 %s 太低，需要 %d+" % (j["version"], min_major))
    ok("JDK %s（%s %s %s）" % (j.get("version") or "?", j.get("vendor", ""), j.get("os", ""), j.get("arch", "")))
    staging = posix_join(r.home(), ".sight-staging") if not r.is_root else "/root/.sight-staging"
    r.mkdirs(staging)
    remote_tar = posix_join(staging, os.path.basename(path))
    r.upload(path, remote_tar, os.path.basename(path))
    home = posix_join("/opt/sight-jdk", j["top"])
    step("解压 JDK 到 %s" % home)
    r.run("mkdir -p /opt/sight-jdk && rm -rf %s && tar -xzf %s -C /opt/sight-jdk && chmod -R a+rX %s && rm -f %s"
          % (shlex.quote(home), shlex.quote(remote_tar), shlex.quote(home), shlex.quote(remote_tar)),
          sudo=True, check=True, what="解压 JDK", timeout=900)
    code, o, _ = r.run('%s/bin/java -version 2>&1 | grep -i -m1 version' % shlex.quote(home))
    m = re.search(r'version "([^"]+)"', o)
    if not m:
        raise DeployError("JDK 解压后无法运行：%s" % o.strip()[:300],
                          "可能是架构不匹配，或服务器 glibc 太旧（如 musl/Alpine 需要专用 JDK）")
    ok("Java %s 已就绪：%s/bin/java" % (m.group(1), home))
    return home + "/bin/java"


def online_install_java(r, env, min_major):
    pm = env.get("pkg")
    pkgs = {"dnf": "java-%d-openjdk-headless", "yum": "java-%d-openjdk-headless",
            "apt-get": "openjdk-%d-jre-headless", "zypper": "java-%d-openjdk-headless"}
    name = pkgs[pm] % min_major
    cmd = {"dnf": "dnf install -y %s", "yum": "yum install -y %s",
           "apt-get": "DEBIAN_FRONTEND=noninteractive apt-get install -y %s || "
                      "(apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y %s)",
           "zypper": "zypper --non-interactive install %s"}[pm]
    cmd = cmd.replace("%s", name)
    step("在线安装 %s（可能需要几分钟）" % name)
    code, o, e = r.run(cmd, sudo=True, timeout=900, on_line=logline)
    if code != 0:
        raise DeployError("在线安装失败（服务器可能无法访问软件源）", "改用「上传本机的 JDK 包」")
    env2 = probe_env(r, "sight-report", "/opt", 1)
    good = [j for j in env2["javas"] if j["major"] >= min_major]
    if not good:
        raise DeployError("安装完成但没找到 Java %d" % min_major)
    ok("Java %s：%s" % (good[0]["version"], good[0]["path"]))
    return good[0]["path"]


def java_home_of(java_bin):
    return java_bin[: -len("/bin/java")] if java_bin.endswith("/bin/java") else posixdir(posixdir(java_bin))


def posixdir(p):
    return p.rsplit("/", 1)[0] if "/" in p else p


# =============================================================================
# 首次安装
# =============================================================================

def do_install(ctx, r, pkg):
    product = pkg.product
    P = PRODUCTS[product]
    if not pkg.full:
        raise DeployError("首次安装需要完整安装包 sight-%s-版本.zip（单独的 jar 只能用于升级）" %
                          product.split("-")[1])
    step("首次安装 %s %s → %s" % (pkg.name, pkg.version, r.label()))

    def validate(v):
        if not v["dir"].startswith("/") or " " in v["dir"]:
            return "安装目录请填不含空格的绝对路径，如 /opt/%s" % product
        if not v["port"].isdigit() or not 0 < int(v["port"]) < 65536:
            return "端口请填 1~65535 的数字"
        if v["xmx"] and not re.match(r"^\d+[gGmM]$", v["xmx"]):
            return "最大堆内存格式如 4g、8g、8192m，或留空"
        return None
    vals = ask_form("安装 %s %s 到 %s" % (pkg.name, pkg.version, r.label()), [
        {"key": "dir", "label": "安装目录", "default": "/opt/%s" % product},
        {"key": "port", "label": "服务端口", "default": str(P["port"]), "type": "number"},
        {"key": "xmx", "label": "最大堆内存", "default": "", "placeholder": "留空 = 按服务器内存自适应",
         "help": "如 4g、8g。留空时程序按服务器可用内存的 75% 自动设置"},
    ], validate)
    inst_dir, port, xmx = vals["dir"].rstrip("/"), int(vals["port"]), vals["xmx"]
    out("检查服务器环境 …")
    env = probe_env(r, product, inst_dir, port)
    while env.get("port_busy") == "1":
        fail("端口 %d 已被占用" % port)
        port = int(ask("端口 %d 已被占用，换一个端口" % port, str(port + 2),
                       validator=lambda s: None if s.isdigit() and 0 < int(s) < 65536 else "端口请填 1~65535 的数字"))
        env = probe_env(r, product, inst_dir, port)

    if env.get("unit") == "1" and env.get("unit_dir", "").rstrip("/") not in ("", inst_dir):
        raise DeployError("这台服务器已有 %s 系统服务，指向 %s" % (product, env["unit_dir"]),
                          "同一台服务器每个产品只能注册一个系统服务。要升级它请直接用「升级」；"
                          "确实要另装一套，请先在原目录执行 sudo ./uninstall.sh")
    blocking = env_report(env, inst_dir, port, pkg)
    if blocking:
        raise DeployError("环境不满足：" + "；".join(blocking))

    java_bin = resolve_java(r, env, max(MIN_JAVA, pkg.java_req))
    java_home = java_home_of(java_bin)

    if env.get("fontconfig") == "0" or env.get("freetype") == "0":
        if env.get("pkg") and confirm("尝试在线安装字体库 fontconfig（服务器需能访问软件源；装不上也不影响启动）？", False):
            install_fonts(r, env["pkg"])

    if xmx and env.get("mem_mb") and _mem_of(xmx) > _int(env["mem_mb"]) * 0.9:
        warn("堆内存 %s 接近或超过服务器总内存 %s MB" % (xmx, env["mem_mb"]))
    use_systemd = env.get("systemd") == "1"
    owner = "root" if r.is_root else r.user
    env_vars = {}
    if port != P["port"]:
        env_vars.update({"SERVER_PORT": str(port), "CHECK_PORT": str(port)})

    step("确认安装计划")
    plan = [("产品", "%s %s" % (pkg.name, pkg.version)), ("服务器", r.label()), ("安装目录", inst_dir),
            ("端口", str(port)), ("Java", java_bin), ("内存", xmx or "自适应（可用内存的 75%）"),
            ("运行方式", "系统服务 %s（开机自启、崩溃自动拉起），运行账号 %s" % (product, owner) if use_systemd
             else "后台进程（服务器没有 systemd，不会开机自启）")]
    kv("安装计划", plan)
    if not confirm("开始安装？", True, detail=["%s：%s" % x for x in plan]):
        raise UserAbort()

    staging = staging_dir(r, product, inst_dir)
    if r.is_root:
        r.run("mkdir -p %s" % shlex.quote(staging), check=True)
    upload_package(r, pkg, staging)

    step("安装")
    marker = marker_json(product, pkg.version, port, "prod", env_vars, java_home)
    s = "set -e\n"
    s += "mkdir -p %s\n" % shlex.quote(inst_dir)
    s += "{ " + copy_files_script(pkg, staging, inst_dir, owner) + "; } || { echo '复制程序文件失败' >&2; exit 1; }\n"
    s += heredoc(posix_join(inst_dir, MARKER), marker)
    s += "mkdir -p %s/logs %s/data\n" % (shlex.quote(inst_dir), shlex.quote(inst_dir))
    s += "chmod 600 %s\n" % shlex.quote(posix_join(inst_dir, MARKER))
    if owner != "root":
        s += "chown -R %s: %s\n" % (shlex.quote(owner), shlex.quote(inst_dir))
    if use_systemd:
        dropin_dir = "/etc/systemd/system/%s.service.d" % product
        if env_vars:
            s += "mkdir -p %s\n" % dropin_dir
            s += heredoc(posix_join(dropin_dir, DROPIN_NAME), dropin_content(env_vars))
        else:
            s += "rm -f %s\n" % posix_join(dropin_dir, DROPIN_NAME)
        jvm_env = ("JVM_XMX=%s " % shlex.quote(xmx)) if xmx else ""
        s += "cd %s && env PATH=%s:\"$PATH\" %sbash ./install.sh install prod\n" % (
            shlex.quote(inst_dir), shlex.quote(java_home + "/bin"), jvm_env)
        r.run(s, sudo=True, check=True, what="安装", timeout=300, on_line=logline)
    else:
        start_env = dict(env_vars)
        start_env["PATH"] = java_home + "/bin:/usr/local/bin:/usr/bin:/bin"
        if xmx:
            start_env["JVM_XMX"] = xmx
        # 非 systemd：启动参数记在标记文件里，之后启停 / 升级都按它来
        s += heredoc(posix_join(inst_dir, MARKER), marker_json(product, pkg.version, port, "prod", start_env, java_home))
        s += svc_functions(product, inst_dir, owner, False, "prod", start_env)
        s += "start_svc\n"
        r.run(s, sudo=True, check=True, what="安装", timeout=300, on_line=logline)
        warn("服务器没有 systemd：已用后台进程启动，服务器重启后需要手动运行 %s/start.sh" % inst_dir)
    r.run("rm -rf %s" % shlex.quote(posix_join(staging, "pkg")), sudo=True)

    step("等待服务启动（首次启动约 30~90 秒）")
    good, h = wait_healthy(r, product, inst_dir, port, False, ctx.settings["health_timeout"], use_systemd)
    if r.alias:
        ctx.store.set_product(r.alias, product, {"install_dir": inst_dir, "port": port, "version": pkg.version,
                                                 "updated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M")})
    if not good:
        show_logs(r, product, inst_dir, use_systemd)
        raise DeployError("服务没能正常启动", "看上面的日志；修正后可在产品卡上点「启动」或「重启」")
    open_firewall(r, env, port)
    url_path = "/" if h and h.get("bootstrapped") else "/setup"
    url = "http://%s:%d%s" % (r.host, port, url_path)
    reach = client_can_reach(r.host, port)
    step("安装完成")
    ok("%s %s 已安装并启动" % (pkg.name, pkg.version))
    info("访问地址：%s" % url)
    if url_path == "/setup":
        info("首次访问请按向导配置「数据库连接 + 管理员账号」")
    if not reach:
        warn("从本机连不上 %s:%d —— 服务在服务器上已正常，可能是网络/安全组/防火墙没放行" % (r.host, port))
    return {"url": url}


def _mem_of(x):
    n = int(re.sub(r"\D", "", x))
    return n * 1024 if x.lower().endswith("g") else n


def dropin_content(env_vars):
    lines = ["# 由 sight-deploy 生成（端口等覆盖项）；改完执行 systemctl daemon-reload && systemctl restart <服务>",
             "[Service]"]
    for k, v in sorted(env_vars.items()):
        lines.append("Environment=%s=%s" % (k, v))
    return "\n".join(lines)


def env_report(env, inst_dir, port, pkg):
    step("环境检查")
    blocking = []
    osname, arch = env.get("os", "?"), env.get("arch", "?")
    if osname != "Linux":
        fail("操作系统 %s —— 本工具只支持 Linux 服务器（Windows 服务器请用安装包里的 install.bat）" % osname)
        blocking.append("非 Linux")
    else:
        ok("系统      %s  %s" % (env.get("distro", "Linux"), arch))
    if arch not in ("x86_64", "aarch64"):
        warn("CPU 架构 %s 未经验证" % arch)
    if env.get("systemd") == "1":
        ok("systemd   可用（将注册为开机自启服务）")
    else:
        warn("systemd   不可用：将以后台进程运行，不会开机自启")
    good = [j for j in env["javas"] if j["major"] >= max(MIN_JAVA, pkg.java_req)]
    if good:
        ok("Java      %s（%s）" % (good[0]["version"], good[0]["path"]))
    else:
        others = ", ".join(j["version"] for j in env["javas"])
        warn("Java      未找到 %d+%s —— 下一步处理" % (max(MIN_JAVA, pkg.java_req), ("（现有 %s）" % others) if others else ""))
    if env.get("fontconfig") == "1" and env.get("freetype") == "1":
        ok("字体库    fontconfig / freetype 已安装")
    else:
        warn("字体库    缺少 fontconfig 或 freetype：服务能启动，但导出 PDF / 图片可能报错")
    mem = _int(env.get("mem_mb"))
    (ok if mem >= 4000 else warn)("内存      %.1f GB%s，CPU %s 核" % (mem / 1024.0, "" if mem >= 4000 else "（建议 4 GB 以上）",
                                                            env.get("cpus", "?")))
    free = _int(env.get("disk_free_mb"))
    need = pkg.total_size() // (1024 * 1024) * 2 + 1024
    if free < need:
        fail("磁盘      %s 剩余 %s，不够（至少需要 %s）" % (env.get("disk_mount", ""), human_size(free << 20),
                                                    human_size(need << 20)))
        blocking.append("磁盘空间不足")
    else:
        (ok if free > 5 * 1024 else warn)("磁盘      %s 剩余 %s" % (env.get("disk_mount", ""), human_size(free << 20)))
    ok("端口      %d 空闲" % port)
    fw = env.get("fw")
    if fw in ("firewalld", "ufw"):
        if env.get("fw_open") == "1":
            ok("防火墙    %s 已放行 %d" % (fw, port))
        else:
            warn("防火墙    %s 未放行 %d（安装完成后可自动放行）" % (fw, port))
    if env.get("dir_has_jar") == "1":
        warn("目录      %s 里已有程序（将覆盖程序文件，data/ 保留）" % inst_dir)
    elif env.get("dir_exists") == "1" and _int(env.get("dir_entries")) > 0:
        warn("目录      %s 已存在且非空" % inst_dir)
    for t in ("tar", "gzip"):
        if env.get("tool_" + t) == "0":
            fail("缺少命令 %s" % t)
            blocking.append("缺少 %s" % t)
    return blocking


def install_fonts(r, pm):
    pk = {"dnf": "fontconfig freetype", "yum": "fontconfig freetype", "apt-get": "fontconfig libfreetype6",
          "zypper": "fontconfig freetype2"}[pm]
    cmd = {"dnf": "dnf install -y", "yum": "yum install -y",
           "apt-get": "DEBIAN_FRONTEND=noninteractive apt-get install -y", "zypper": "zypper --non-interactive install"}[pm]
    code, o, e = r.run("timeout 300 %s %s" % (cmd, pk), sudo=True, timeout=330)
    (ok if code == 0 else warn)("字体库安装%s" % ("完成" if code == 0 else "失败（不影响启动，之后可请运维安装 %s）" % pk))


def open_firewall(r, env, port):
    fw = env.get("fw")
    if fw not in ("firewalld", "ufw") or env.get("fw_open") == "1":
        return
    if not confirm("服务器防火墙（%s）还没放行端口 %d，现在放行？" % (fw, port), True):
        return
    if fw == "firewalld":
        cmd = "firewall-cmd --permanent --add-port=%d/tcp && firewall-cmd --reload" % port
    else:
        cmd = "ufw allow %d/tcp" % port
    code, o, e = r.run(cmd, sudo=True)
    (ok if code == 0 else warn)("防火墙放行 %d %s" % (port, "完成" if code == 0 else "失败：" + (e or o).strip()[:200]))


def client_can_reach(host, port):
    try:
        with socket.create_connection((host, port), timeout=4):
            return True
    except Exception:
        return False


# =============================================================================
# 升级
# =============================================================================

def backup_dir_for(ctx, r, product, inst_dir):
    """上次记录（且是同一个安装目录）> <安装目录上级>/sight-backups/<产品>。"""
    saved = ctx.host_product(r, product) or {}
    if saved.get("backup_dir") and saved.get("install_dir") == inst_dir:
        return saved["backup_dir"]
    return default_backup_dir(inst_dir, product)


def default_backup_dir(inst_dir, product):
    parent = posixdir(inst_dir.rstrip("/")) or "/"
    return posix_join(parent if parent else "/", "sight-backups", product)


def do_upgrade(ctx, r, pkg, inst):
    product = pkg.product
    P = PRODUCTS[product]
    inst_dir, port = inst["dir"], inst["port"]
    step("升级 %s：%s → %s" % (pkg.name, inst["version"], pkg.version))
    ensure_no_remote_task(r, staging_dir(r, product, inst_dir))
    cur_v, new_v = parse_version(inst["version"]), parse_version(pkg.version)
    if cur_v and new_v and new_v == cur_v:
        code, o, _ = r.run("sha256sum %s 2>/dev/null | cut -c1-64" % shlex.quote(posix_join(inst_dir, P["jar"])),
                           sudo=True)
        same = o.strip() == pkg.jar_sha
        info("服务器上已经是 %s%s" % (pkg.version, "（文件完全相同）" if same else "（但文件不同，可能是补丁构建）"))
        if not confirm("仍然重新覆盖安装？", False):
            return None
    elif cur_v and new_v and new_v < cur_v:
        fail("你选的版本 %s 比服务器上的 %s 更旧（降级）" % (pkg.version, inst["version"]))
        if not confirm_typed("仍然要降级到 %s？" % pkg.version, "降级", detail=[
                "新版本可能已经升级过数据库结构，旧版本不一定能正常工作。",
                "推荐的做法是用「回滚」恢复升级前的备份（程序和数据一起恢复）。"]):
            return None

    # 现场信息
    h_before = r.health(port)
    was_running = bool(inst["pids"]) or inst["active"] == "active"
    if was_running and not h_before:
        # 端口推断错了（比如端口写在外部配置文件里）：按进程实际监听的端口再试
        for lp_ in inst.get("listen_ports") or []:
            if lp_ != port:
                h_before = r.health(lp_)
                if h_before:
                    info("检测到服务实际端口为 %d" % lp_)
                    port = inst["port"] = lp_
                    break
    if was_running and not h_before:
        raise DeployError("服务在运行，但在端口 %d 上健康检查没有响应，已中止（服务器未做任何改动）" % port,
                          "先点「日志」确认服务正常后再升级")
    if inst.get("other_unit_dir"):
        warn("本机的 %s 系统服务指向另一个目录 %s；本次只操作 %s（后台进程方式）" % (
            product, inst["other_unit_dir"], inst_dir))
    if inst.get("data_link"):
        warn("data/ 是指向 %s 的链接：升级备份只会记录这个链接，不包含其中的数据" % inst["data_link"])
        if not confirm("请确认 %s 已另行备份，继续？" % inst["data_link"], False):
            raise DeployError("已取消：data/ 的数据未备份")
    db = inst["db"]

    # Java 版本是否满足新包
    need_java = max(MIN_JAVA, pkg.java_req)
    java_now = inst.get("java")
    new_java_home = None
    if java_now and java_now["major"] and java_now["major"] < need_java:
        warn("新版本需要 Java %d，当前服务用的是 %s" % (need_java, java_now["version"]))
        env = probe_env(r, product, inst_dir, port)
        new_java_home = java_home_of(resolve_java(r, env, need_java))

    # 备份规划（备份盘不够时让用户换个目录，比如挂载的数据盘）
    bdir = backup_dir_for(ctx, r, product, inst_dir)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    while True:
        est = estimate_backup(r, inst_dir, bdir)
        if est["free_kb"] >= est["size_kb"] // 2 + pkg.total_size() // 1024 + 200 * 1024:
            break
        fail("备份目录 %s 所在磁盘剩余 %s，不够存放备份（最多 %s）" % (
            bdir, human_size(est["free_kb"] * 1024), human_size(est["size_kb"] * 1024)))
        bdir = ask("换一个空间充足的备份目录（服务器上的绝对路径）", bdir,
                   validator=lambda v: None if v.startswith("/") and " " not in v else "请填不含空格的绝对路径").rstrip("/")
    bfile = posix_join(bdir, "%s-%s-%s.tar.gz" % (product, re.sub(r"[^\w.]", "_", inst["version"]), stamp))

    plan = [("产品", pkg.name), ("版本", "%s → %s" % (inst["version"], pkg.version)),
            ("服务器", r.label()), ("安装目录", "%s（运行账号 %s）" % (inst_dir, inst["owner"])),
            ("服务", "%s，端口 %d，%s" % ("systemd 服务 %s" % product if inst["unit"] else "后台进程",
                                     port, "运行中" if was_running else "未运行")),
            ("数据库", db["desc"]),
            ("升级前备份", "%s（不超过 %s，备份盘剩余 %s）" % (bfile, human_size(est["size_kb"] * 1024),
                                                    human_size(est["free_kb"] * 1024)))]
    if not pkg.full:
        plan.append(("说明", "单独 jar 升级：只替换 %s，启停脚本保持不变" % P["jar"]))
    kv("升级计划", plan)

    if db["kind"] == "external":
        warn("使用的是外部数据库：本工具只备份程序和 data/ 目录，不备份数据库本身")
        if not confirm("已备份数据库，继续升级？", False, danger=True, detail=[
                "新版本启动时可能会升级数据库结构，这一步不可逆。",
                "升级前请先备份数据库，或在 SightReport / SightWall 管理台「系统管理 › 备份」创建一次完整备份。"]):
            return None
    elif db["kind"] == "h2" and not db.get("inside"):
        warn(db["desc"])
        if not confirm("H2 数据库文件不在安装目录，不会被备份。继续？", False, danger=True):
            return None

    if not confirm("开始升级？", True, detail=["%s：%s" % x for x in plan] +
                   ["", "升级过程中服务会停止约 1~3 分钟；失败时可一键回滚。"]):
        return None

    staging = staging_dir(r, product, inst_dir)
    if r.is_root:
        r.run("mkdir -p %s" % shlex.quote(staging), check=True)
    upload_package(r, pkg, staging)  # 服务仍在运行，缩短停机时间

    # ---------------- 后台执行：停 → 备份 → 替换 → 启
    profile = inst["profile"]
    start_env = start_env_of(inst)
    dropin_env = None
    if new_java_home:
        if inst["unit"]:
            dropin_env = read_dropin(r, product)
            dropin_env["PATH"] = new_java_home + "/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        else:
            start_env["PATH"] = new_java_home + "/bin:/usr/local/bin:/usr/bin:/bin"
    marker = marker_json(product, pkg.version, port, profile, start_env,
                         new_java_home or inst["marker"].get("java_home"), inst["marker"])
    meta = json.dumps({"product": product, "version": inst["version"], "upgraded_to": pkg.version,
                       "install_dir": inst_dir, "created": stamp, "db": db["kind"],
                       "tool": "sight-deploy " + TOOL_VERSION}, ensure_ascii=False)
    s = svc_functions(product, inst_dir, inst["owner"], inst["unit"], profile, start_env)
    s += r'''
B=%(b)s; BD=%(bd)s
finish(){ echo "$1" > "$ST"; }
restore_old(){ tar $TAROPT -xzf "$B" -C "$D" --exclude=./data; start_svc; }
say "停止服务 …"
if ! stop_svc; then say "服务停止失败（服务未改动）"; start_svc; finish FAIL:stop; exit 1; fi
say "服务已停止，开始备份（程序 + data/）…"
mkdir -p "$BD"
( umask 077; exec tar -czf "$B.tmp" -C "$D" %(ex)s . ); rc=$?
if [ $rc -gt 1 ] || [ ! -s "$B.tmp" ]; then
  rm -f "$B.tmp"; say "备份失败（tar 退出码 $rc），正在重新启动旧版本 …"; start_svc; finish FAIL:backup; exit 1
fi
mv -f "$B.tmp" "$B"
%(meta)s
say "备份完成：$(du -h "$B" | cut -f1)"
say "替换程序文件 …"
if ! { %(copy)s; }; then
  say "替换失败（磁盘满或权限问题？），从备份恢复程序文件并启动旧版本 …"
  restore_old; finish FAIL:swap; exit 1
fi
%(marker)s
chmod 600 "$D/%(markername)s"
%(dropin)s
say "启动新版本 …"
if ! start_svc; then
  say "启动命令失败，恢复旧版本程序文件并启动 …"; stop_svc; restore_old; finish FAIL:start; exit 1
fi
finish OK
''' % {"b": shlex.quote(bfile), "bd": shlex.quote(bdir), "ex": tar_excludes(),
       "meta": heredoc(bfile + ".json", meta),
       "copy": copy_files_script(pkg, staging, inst_dir, inst["owner"]),
       "marker": heredoc(posix_join(inst_dir, MARKER), marker), "markername": MARKER,
       "dropin": ("mkdir -p /etc/systemd/system/%s.service.d\n" % product +
                  heredoc("/etc/systemd/system/%s.service.d/%s" % (product, DROPIN_NAME), dropin_content(dropin_env)))
       if dropin_env else ""}
    step("执行升级（在服务器后台运行，网络中断也会继续）")
    status = run_detached(r, staging, "upgrade", s)
    if status != "OK":
        fail("升级步骤失败：%s" % status)
        if status in ("FAIL:stop", "FAIL:backup", "FAIL:swap", "FAIL:start"):
            info("已恢复为旧版本 %s 并重新启动，数据未改动。" % inst["version"])
        else:
            # 后台任务被意外终止：新版本还没启动过，数据没变；恢复旧程序文件（若备份已完成）并启动
            code, o, _ = r.run("[ -s %s ] && echo yes" % shlex.quote(bfile), sudo=True)
            if "yes" in o:
                warn("后台任务意外中止，正在用刚做的备份恢复旧版本程序 …")
                do_rollback_file(ctx, r, product, inst, bfile, full=False, snapshot=False)
            else:
                warn("后台任务在备份阶段中止，程序文件未改动，正在重新启动服务 …")
                r.run(svc_functions(product, inst_dir, inst["owner"], inst["unit"], profile, start_env) +
                      "start_svc\n", sudo=True)
        show_logs(r, product, inst_dir, inst["unit"])
        raise DeployError("升级未完成（%s）" % status)

    step("等待新版本就绪（会自动升级数据库结构，可能需要几分钟）")
    need_boot = bool(h_before and h_before.get("bootstrapped"))
    try:
        good, h = wait_healthy(r, product, inst_dir, port, need_boot, ctx.settings["health_timeout"], inst["unit"])
    except KeyboardInterrupt:
        raise DeployError("已停止等待：新版本 %s 已替换完毕，服务正在启动" % pkg.version,
                          "稍后点「刷新」确认状态；如有问题用「回滚」。升级前备份：%s" % bfile)
    ver_ok = True
    if good:
        v = r.app_version(port)
        if v and parse_version(pkg.version) and parse_version(v) != parse_version(pkg.version):
            warn("服务报告的版本是 %s，预期 %s" % (v, pkg.version))
            ver_ok = False
        elif v:
            ok("版本确认：%s" % v)
    if not good or not ver_ok:
        show_logs(r, product, inst_dir, inst["unit"])
        info("升级前备份：%s" % bfile)
        if confirm("新版本没有正常运行，回滚到升级前？", True, detail=[
                "将用升级前的备份恢复程序和数据（data/），服务恢复到 %s。" % inst["version"],
                "备份文件：%s" % bfile, "日志见上方「最近日志」。"]):
            do_rollback_file(ctx, r, product, inst, bfile, full=True, snapshot=False)
            raise DeployError("升级失败，已回滚到 %s" % inst["version"],
                              "把本地日志 %s 发给技术支持排查" % LOG.path if LOG.path else None)
        raise DeployError("升级后服务未就绪", "稍后点「刷新」「日志」查看；需要时用「回滚」")

    prune_backups(r, bdir, product, ctx.settings["keep_backups"], keep=bfile)
    r.run("rm -rf %s" % shlex.quote(posix_join(staging, "pkg")), sudo=True)
    if r.alias:
        ctx.store.set_product(r.alias, product, {"install_dir": inst_dir, "port": port, "version": pkg.version,
                                                 "backup_dir": bdir,
                                                 "updated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M")})
    url = "http://%s:%d/" % (r.host, port)
    step("升级完成")
    ok("%s %s → %s" % (pkg.name, inst["version"], pkg.version))
    info("访问地址：%s" % url)
    info("升级前备份：%s" % bfile)
    return {"url": url}


def read_dropin(r, product):
    t = r.read_file("/etc/systemd/system/%s.service.d/%s" % (product, DROPIN_NAME), sudo=True) or ""
    return dict(re.findall(r"^Environment=([A-Z_]+)=(.*)$", t, re.M))


def estimate_backup(r, inst_dir, bdir):
    ex = " ".join(shlex.quote(posix_join(inst_dir, x[2:])) for x in BACKUP_EXCLUDES if "*" not in x)
    s = r'''
D=%s; BD=%s
t=$(du -sk "$D" 2>/dev/null | awk '{print $1}'); x=0
for p in %s "$D"/data/cache*; do [ -e "$p" ] && x=$((x + $(du -sk "$p" 2>/dev/null | awk '{print $1}'))); done
echo "size_kb=$((t - x))"
d="$BD"; while [ ! -d "$d" ]; do d=$(dirname "$d"); done
df -Pk "$d" | awk 'NR==2{print "free_kb=" $4}'
''' % (shlex.quote(inst_dir), shlex.quote(bdir), ex)
    code, o, _ = r.run(s, sudo=True, timeout=300)
    kv = parse_kv(o)
    return {"size_kb": max(_int(kv.get("size_kb")), 0), "free_kb": _int(kv.get("free_kb"))}


def list_backups(r, bdir, product):
    code, o, _ = r.run("ls -1t %s/%s-*.tar.gz 2>/dev/null | while read f; do echo \"$f|$(du -k \"$f\" | cut -f1)\"; done"
                       % (shlex.quote(bdir), product), sudo=True)
    res = []
    for line in o.splitlines():
        f, _, kb = line.partition("|")
        m = re.search(r"-(\d[\w.]*)-(\d{8}-\d{6})\.tar\.gz$", f)
        res.append({"file": f, "size": _int(kb) * 1024, "version": m.group(1) if m else "?",
                    "time": _dt.datetime.strptime(m.group(2), "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M")
                    if m else "?"})
    return res


def prune_backups(r, bdir, product, keep_n, keep):
    bs = list_backups(r, bdir, product)
    old = [b["file"] for b in bs[keep_n:] if b["file"] != keep]
    if old:
        r.run("rm -f %s" % " ".join(shlex.quote(f) + " " + shlex.quote(f + ".json") for f in old), sudo=True)
        info(dim("已清理 %d 份较早的备份（保留最近 %d 份）" % (len(old), keep_n)))


# =============================================================================
# 回滚
# =============================================================================

def do_rollback_file(ctx, r, product, inst, bfile, full, snapshot=True):
    """用备份恢复。full=True 连同 data/ 一起恢复；snapshot=True 时先把当前状态另存一份（回滚也能撤销）。"""
    step("回滚：%s（%s）" % (bfile.rsplit("/", 1)[-1], "程序 + 数据" if full else "仅程序"))
    staging = staging_dir(r, product, inst["dir"])
    if r.is_root:
        r.run("mkdir -p %s" % shlex.quote(staging), check=True)
    else:
        r.mkdirs(staging)
    snap = ""
    if full and snapshot:
        snap = posix_join(posixdir(bfile), "%s-%s-%s.tar.gz" % (
            product, re.sub(r"[^\w.]", "_", inst["version"]), _dt.datetime.now().strftime("%Y%m%d-%H%M%S")))
    s = svc_functions(product, inst["dir"], inst["owner"], inst["unit"], inst["profile"], start_env_of(inst))
    s += r'''
B=%(b)s; SNAP=%(snap)s
finish(){ echo "$1" > "$ST"; }
[ -s "$B" ] || { say "备份文件不存在：$B"; finish FAIL:nofile; exit 1; }
say "停止服务 …"
stop_svc || { say "服务停止失败（未做改动）"; start_svc; finish FAIL:stop; exit 1; }
if [ -n "$SNAP" ]; then
  say "先把当前状态另存一份：$SNAP"
  ( umask 077; exec tar -czf "$SNAP.tmp" -C "$D" %(ex_all)s . ); rc=$?
  if [ $rc -gt 1 ] || [ ! -s "$SNAP.tmp" ]; then
    rm -f "$SNAP.tmp"; say "另存失败（磁盘空间不足？），已取消回滚并重新启动服务"; start_svc; finish FAIL:snapshot; exit 1
  fi
  mv -f "$SNAP.tmp" "$SNAP"
fi
say "从备份恢复 …"
if ! tar $TAROPT -xzf "$B" -C "$D" %(ex)s; then say "解压失败，尝试启动服务 …"; start_svc; finish FAIL:extract; exit 1; fi
say "启动服务 …"
start_svc || { finish FAIL:start; exit 1; }
finish OK
''' % {"b": shlex.quote(bfile), "snap": shlex.quote(snap), "ex_all": tar_excludes(),
       "ex": "" if full else "--exclude=./data"}
    status = run_detached(r, staging, "rollback", s)
    if status != "OK":
        show_logs(r, product, inst["dir"], inst["unit"])
        raise DeployError("回滚失败：%s" % status, "当前状态已另存为 %s" % snap if snap else None)
    ver, _ = remote_jar_version(r, posix_join(inst["dir"], PRODUCTS[product]["jar"]))
    good, _ = wait_healthy(r, product, inst["dir"], inst["port"], False, ctx.settings["health_timeout"], inst["unit"])
    if good:
        ok("已回滚到 %s" % ver)
        if snap:
            info("回滚前的状态已另存为 %s（需要时可再用「回滚」恢复回来）" % snap)
        if r.alias:
            ctx.store.set_product(r.alias, product, {"version": ver})
    else:
        show_logs(r, product, inst["dir"], inst["unit"])
        raise DeployError("回滚后服务未就绪")


# =============================================================================
# 定位已安装的产品
# =============================================================================

def locate_install(ctx, r, product, quiet=False, ask_user=True):
    """返回 inst dict 或 None（未安装）。"""
    P = PRODUCTS[product]
    saved = (ctx.host_product(r, product) or {}).get("install_dir")
    cands = find_installs(r, product)
    if saved:
        code, o, _ = r.run("[ -f %s ] && echo yes" % shlex.quote(posix_join(saved, P["jar"])), sudo=True)
        if "yes" in o and saved not in [c[0] for c in cands]:
            cands.append((saved, "saved"))  # 系统服务 / 运行中的优先
    if not cands:
        return None
    if len(cands) == 1:
        d = cands[0][0]
    elif saved and saved in [c[0] for c in cands] and not ask_user:
        d = saved
    elif not ask_user:
        d = cands[0][0]
    else:
        src_name = {"unit": "系统服务", "proc": "运行中", "dir": "目录", "saved": "上次记录"}
        d = choose("服务器上找到多个 %s 安装，选择要操作的" % P["name"],
                   [(c[0], c[0], src_name.get(c[1], c[1])) for c in cands], 1)
        if d is None:
            raise UserAbort()
    if not quiet:
        out("读取安装信息 …")
    return inspect_install(r, product, d)




# =============================================================================
# 任务（网页上的每个操作 = 一个后台任务）
# =============================================================================

class Ctx:
    def __init__(self, store):
        self.store = store

    @property
    def settings(self):
        st = self.store.snapshot().get("settings") or {}
        return {"keep_backups": max(1, _int(st.get("keep_backups") or 3)),
                "health_timeout": max(60, _int(st.get("health_timeout") or 300))}

    def host_product(self, r, product):
        if not r.alias:
            return None
        return ((self.store.get(r.alias) or {}).get("products") or {}).get(product)


def _product_of(p):
    prod = p.get("product")
    if prod not in PRODUCTS:
        raise DeployError("未知产品：%s" % prod)
    return prod


def _get_install(ctx, r, product):
    inst = locate_install(ctx, r, product, quiet=True, ask_user=False)
    if not inst:
        raise DeployError("%s 上没有找到已安装的 %s" % (r.host, PRODUCTS[product]["name"]), "请先点「刷新」确认状态")
    return inst


def job_status(ctx, p):
    r = open_host(ctx.store, p.get("host"))
    try:
        code, o, _ = r.run("uname -sm; . /etc/os-release 2>/dev/null; echo \"$PRETTY_NAME\"; uptime -p 2>/dev/null")
        res = {"host": r.alias, "address": "%s@%s" % (r.user, r.host),
               "os": " / ".join(x for x in o.strip().splitlines() if x), "products": []}
        for prod, P in PRODUCTS.items():
            inst = locate_install(ctx, r, prod, quiet=True, ask_user=False)
            if not inst:
                res["products"].append({"product": prod, "name": P["name"], "installed": False})
                continue
            h = r.health(inst["port"])
            running = bool(inst["pids"]) or inst["active"] == "active"
            bdir = backup_dir_for(ctx, r, prod, inst["dir"])
            bs = list_backups(r, bdir, prod)
            boot = bool(h and h.get("bootstrapped"))
            item = {"product": prod, "name": P["name"], "installed": True, "version": inst["version"],
                    "dir": inst["dir"], "owner": inst["owner"],
                    "service": "systemd" if inst["unit"] else "process", "active": inst["active"] or "",
                    "running": running, "healthy": bool(h), "bootstrapped": boot,
                    # 还没配库（没走 /setup）时「连不上」是正常的，不报错
                    "db_reachable": h.get("dbReachable") if h and h.get("dbConfigured") else None,
                    "port": inst["port"],
                    "url": "http://%s:%d/" % (r.host, inst["port"]),
                    "reachable": client_can_reach(r.host, inst["port"]) if h else None,
                    "java": "%s（%s）" % (inst["java"]["version"], inst["java"]["path"]) if inst.get("java") else "",
                    "db": inst["db"]["desc"], "db_kind": inst["db"]["kind"],
                    "disk_free": inst["disk_free_mb"] << 20, "backups": len(bs),
                    "last_backup": "%s（%s）" % (bs[0]["time"], bs[0]["version"]) if bs else ""}
            if h and h.get("mode") == "CLUSTER":
                item["cluster_nodes"] = h.get("activeNodes")
            res["products"].append(item)
            ctx.store.set_product(r.alias, prod, {"install_dir": inst["dir"], "port": inst["port"],
                                                  "version": inst["version"]})
        # 服务器上已经没有的产品，从本地缓存里去掉
        for it in res["products"]:
            if not it["installed"]:
                ctx.store.drop_product(r.alias, it["product"])
        return res
    finally:
        r.close()


def job_deploy(ctx, p):
    hosts = p.get("hosts") or []
    if not hosts:
        raise DeployError("请选择至少一台服务器")
    pkg = load_package(p.get("package") or "")
    results = []
    try:
        for i, name in enumerate(hosts, 1):
            if len(hosts) > 1:
                step("第 %d/%d 台：%s" % (i, len(hosts), name))
            r = None
            try:
                r = open_host(ctx.store, name)
                out("检查 %s 是否已安装 …" % pkg.name)
                inst = locate_install(ctx, r, pkg.product)
                res = do_upgrade(ctx, r, pkg, inst) if inst else do_install(ctx, r, pkg)
                if not res and len(hosts) == 1:
                    raise UserAbort()  # 用户在确认框里选了「取消」：整个任务算取消，而不是「完成」
                results.append({"host": name, "status": "完成" if res else "跳过", "url": (res or {}).get("url")})
            except (DeployError, UserAbort) as e:
                results.append({"host": name, "status": "失败" if isinstance(e, DeployError) else "取消"})
                if len(hosts) == 1:
                    raise
                if isinstance(e, DeployError):
                    fail("%s：%s" % (name, e))
                    if e.hint:
                        info(e.hint)
                if i < len(hosts) and (isinstance(e, UserAbort) or not confirm(
                        "%s 没有成功，继续处理剩下的 %d 台？" % (name, len(hosts) - i), False)):
                    break
            finally:
                if r:
                    r.close()
        if len(hosts) > 1:
            step("汇总")
            for x in results:
                (ok if x["status"] == "完成" else warn if x["status"] == "跳过" else fail)("%s  %s" % (x["host"], x["status"]))
            if any(x["status"] in ("失败", "取消") for x in results):
                raise DeployError("有 %d 台没有完成" % sum(1 for x in results if x["status"] in ("失败", "取消")),
                                  "看上方时间线里对应服务器的说明")
        return {"results": results}
    finally:
        pkg.cleanup()


def job_service(ctx, p):
    product, action = _product_of(p), p.get("action")
    if action not in ("start", "stop", "restart"):
        raise DeployError("未知操作：%s" % action)
    r = open_host(ctx.store, p.get("host"))
    try:
        inst = _get_install(ctx, r, product)
        ensure_no_remote_task(r, staging_dir(r, product, inst["dir"]))
        name = {"start": "启动", "stop": "停止", "restart": "重启"}[action]
        s = svc_functions(product, inst["dir"], inst["owner"], inst["unit"], inst["profile"], start_env_of(inst))
        body = {"start": "start_svc", "stop": "stop_svc && say 已停止", "restart": "stop_svc && start_svc"}[action]
        step("%s %s" % (name, PRODUCTS[product]["name"]))
        r.run(s + body + "\n", sudo=True, check=True, what=name, timeout=300, on_line=_print_task_line)
        if action == "stop":
            ok("已停止")
            return {}
        good, _ = wait_healthy(r, product, inst["dir"], inst["port"], False, ctx.settings["health_timeout"],
                               inst["unit"])
        if not good:
            show_logs(r, product, inst["dir"], inst["unit"])
            raise DeployError("服务没能正常启动", "看上方「最近日志」")
        return {}
    finally:
        r.close()


def job_logs(ctx, p):
    product = _product_of(p)
    r = open_host(ctx.store, p.get("host"))
    try:
        inst = _get_install(ctx, r, product)
        n = min(max(_int(p.get("lines") or 200), 20), 5000)
        parts = fetch_logs(r, product, inst["dir"], inst["unit"], n)
        text = "\n\n".join("===== %s =====\n%s" % (t, x) for t, x in parts) or "（没有找到日志）"
        return {"text": text}
    finally:
        r.close()


def job_follow_logs(ctx, p):
    product = _product_of(p)
    r = open_host(ctx.store, p.get("host"))
    try:
        inst = _get_install(ctx, r, product)
        f = posix_join(inst["dir"], "logs/sight-data-info.log")
        step("实时日志 %s" % f)
        try:
            r.run("tail -n 30 -F %s" % shlex.quote(f), sudo=True, timeout=0, on_line=logline, cancellable=True)
        except JobCancelled:
            return {}
        return {}
    finally:
        r.close()


def job_list_backups(ctx, p):
    product = _product_of(p)
    r = open_host(ctx.store, p.get("host"))
    try:
        inst = _get_install(ctx, r, product)
        bdir = backup_dir_for(ctx, r, product, inst["dir"])
        return {"backup_dir": bdir, "current_version": inst["version"], "db_kind": inst["db"]["kind"],
                "backups": list_backups(r, bdir, product)}
    finally:
        r.close()


def job_rollback(ctx, p):
    product = _product_of(p)
    r = open_host(ctx.store, p.get("host"))
    try:
        inst = _get_install(ctx, r, product)
        bfile = str(p.get("file") or "")
        bdir = backup_dir_for(ctx, r, product, inst["dir"])
        if not bfile.startswith(bdir + "/") or not bfile.endswith(".tar.gz") or "/.." in bfile:
            raise DeployError("备份文件不在备份目录里：%s" % bfile)
        full = bool(p.get("full"))
        if full and inst["db"]["kind"] == "external":
            warn("外部数据库不在备份里：这次只恢复程序和 data/ 目录，数据库需要另行恢复")
        do_rollback_file(ctx, r, product, inst, bfile, full)
        return {}
    finally:
        r.close()


JOB_HANDLERS = {
    "add_host": lambda ctx, p: add_host(ctx.store, p),
    "edit_host": lambda ctx, p: edit_host(ctx.store, p),
    "test_host": lambda ctx, p: test_host(ctx.store, p.get("name")),
    "status": job_status,
    "deploy": job_deploy,
    "service": job_service,
    "logs": job_logs,
    "follow_logs": job_follow_logs,
    "list_backups": job_list_backups,
    "rollback": job_rollback,
}
JOB_TITLES = {"add_host": "添加服务器", "edit_host": "修改密码", "test_host": "测试连接", "status": "查询状态",
              "deploy": "安装 / 升级", "service": "启停服务", "logs": "查看日志", "follow_logs": "实时日志",
              "list_backups": "读取备份列表", "rollback": "回滚"}


class Job:
    def __init__(self, kind, params):
        self.id = uuid.uuid4().hex[:12]
        self.kind, self.params = kind, params
        self.events = []
        self.base = 0  # 已丢弃的早期事件数（实时日志这类长任务只保留最近的事件）
        self.cond = threading.Condition()
        self.cancel_flag = threading.Event()
        self.done, self.ok, self.result, self.error, self.hint = False, None, None, None, None
        self.question, self._answer, self._qseq = None, None, 0
        self.started = time.time()
        self.last_poll = time.time()

    MAX_EVENTS = 4000

    def emit(self, ev):
        with self.cond:
            self.events.append(ev)
            if len(self.events) > self.MAX_EVENTS:
                drop = len(self.events) - self.MAX_EVENTS // 2
                del self.events[:drop]
                self.base += drop
            self.cond.notify_all()

    def ask(self, q):
        """返回 (用户点了取消, 值)。任务已被「停止」时直接中止（不能当成用户答了「否」）。"""
        if self.cancel_flag.is_set():
            raise JobCancelled()
        with self.cond:
            self._qseq += 1
            qid = "q%d" % self._qseq
            self.question = dict({k: v for k, v in q.items() if v is not None}, qid=qid)
            self._answer = None
            self.cond.notify_all()
            while True:
                if self._answer and self._answer[0] == qid:
                    _, cancelled, value = self._answer
                    self.question, self._answer = None, None
                    self.cond.notify_all()
                    return cancelled, value
                if self.cancel_flag.is_set():
                    self.question = None
                    self.cond.notify_all()
                    raise JobCancelled()
                self.cond.wait(0.5)

    def answer(self, qid, value, cancelled):
        with self.cond:
            if not self.question or self.question.get("qid") != qid:
                return False
            self._answer = (qid, bool(cancelled), value)
            self.cond.notify_all()
            return True

    def cancel(self):
        self.cancel_flag.set()
        with self.cond:
            self.cond.notify_all()

    def finish(self, ok_, result=None, error=None, hint=None):
        with self.cond:
            self.done, self.ok, self.result, self.error, self.hint = True, ok_, result, error, hint
            self.question = None
            self.cond.notify_all()

    def snapshot(self, since, wait=1.0):
        self.last_poll = time.time()
        with self.cond:
            since = max(since, self.base)
            if self.base + len(self.events) <= since and not self.done:
                self.cond.wait(wait)
            since = max(since, self.base)
            return {"events": self.events[since - self.base:], "next": self.base + len(self.events),
                    "done": self.done, "ok": self.ok,
                    "result": self.result, "error": self.error, "hint": self.hint, "question": self.question,
                    "cancelled": self.done and self.error == "已取消"}


class App:
    def __init__(self):
        self.store = Store()
        self.ctx = Ctx(self.store)
        self.jobs = {}
        self.current = None
        self.lock = threading.Lock()
        self.token = _secrets.token_urlsafe(24)
        self.last_beat = time.time()
        self.port = None
        self.httpd = None

    def busy(self):
        j = self.current
        return j.id if j and not j.done else None

    def start_job(self, kind, params):
        if kind not in JOB_HANDLERS:
            raise DeployError("未知任务：%s" % kind)
        with self.lock:
            if self.busy():
                return None
            job = Job(kind, params or {})
            self.jobs[job.id] = job
            self.current = job
            # 只保留最近的任务记录
            for old in list(self.jobs)[:-20]:
                if self.jobs[old].done:
                    self.jobs.pop(old, None)
        t = threading.Thread(target=self._run, args=(job,), daemon=True)
        t.start()
        return job

    def _run(self, job):
        _tl.job = job
        LOG.write("\n##### 任务：%s %s" % (JOB_TITLES.get(job.kind, job.kind), _mask(json.dumps(
            {k: v for k, v in job.params.items() if "pass" not in k}, ensure_ascii=False))))
        try:
            res = JOB_HANDLERS[job.kind](self.ctx, job.params)
            job.finish(True, res or {})
            LOG.write("##### 任务完成")
        except (JobCancelled, UserAbort):
            job.finish(False, error="已取消")
            LOG.write("##### 任务已取消")
        except DeployError as e:  # 错误原因由网页的失败横幅展示，时间线里不再重复
            job.finish(False, error=str(e), hint=e.hint)
            LOG.write("##### 任务失败：%s" % e)
        except Exception as e:  # 兜底：完整堆栈写日志，网页上给人话
            import traceback
            LOG.write(traceback.format_exc())
            net = isinstance(e, (socket.error, EOFError)) or "SSHException" in type(e).__name__
            msg = "与服务器的连接中断了" if net else "意外错误：%s: %s" % (type(e).__name__, e)
            job.finish(False, error=msg, hint="请检查网络后重试；仍有问题请把本地日志 %s 发给技术支持" % LOG.path)
        finally:
            _tl.job = None


# =============================================================================
# 本机网页服务
# =============================================================================

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from urllib.parse import urlparse, parse_qs, unquote  # noqa: E402

APP = None
PREFERRED_PORT = 17880


def _guess_pkg(name):
    m = re.match(r"(sight-(?:report|wall))(?:-(\d[\w.]*?))?\.(?:zip|jar)$", name)
    return (m.group(1), m.group(2)) if m else (None, None)


def _file_item(f):
    st = f.stat()
    return {"path": str(f), "name": f.name, "size": st.st_size, "mtime": int(st.st_mtime), "dir": str(f.parent)}


def api_packages():
    res = []
    for f in find_packages():
        it = _file_item(f)
        it["product"], it["version"] = _guess_pkg(f.name)
        res.append(it)
    return res


def _fs_shortcuts(kind):
    home = Path.home()
    sc = []
    for name, p in (("下载", home / "Downloads"), ("桌面", home / "Desktop"), ("文档", home / "Documents"),
                    ("主目录", home), ("程序所在目录", APP_DIR)):
        if p.is_dir():
            sc.append({"name": name, "path": str(p)})
    if kind == "key" and (home / ".ssh").is_dir():
        sc.insert(0, {"name": ".ssh", "path": str(home / ".ssh")})
    if os.name == "nt":
        sc.append({"name": "此电脑", "path": "::drives"})
    return sc


def api_fs(qs):
    kind = (qs.get("kind") or ["any"])[0]
    d = (qs.get("dir") or [""])[0]
    if not d:
        d = str(Path.home() / "Downloads") if (Path.home() / "Downloads").is_dir() else str(Path.home())
    if d == "::drives":
        import string
        try:  # 用位掩码列盘符：逐个 exists() 在断开的网络映射盘上会卡很久
            import ctypes
            mask = ctypes.windll.kernel32.GetLogicalDrives()
            drives = ["%s:\\" % c for i, c in enumerate(string.ascii_uppercase) if mask >> i & 1]
        except Exception:
            drives = ["C:\\"]
        return {"dir": "", "parent": None, "shortcuts": _fs_shortcuts(kind),
                "entries": [{"name": x, "path": x, "is_dir": True} for x in drives]}
    p = Path(d).expanduser()
    if not p.is_dir():
        raise DeployError("文件夹不存在：%s" % d)
    exts = {"package": (".zip", ".jar"), "jdk": (".tar.gz", ".tgz")}.get(kind)
    entries = []
    try:
        items = list(p.iterdir())
    except OSError as e:
        raise DeployError("无法读取文件夹：%s" % e)
    for f in items:
        try:
            if f.name.startswith(".") and kind != "key":
                continue
            if f.is_dir():
                entries.append({"name": f.name, "path": str(f), "is_dir": True})
            elif f.is_file() and (not exts or f.name.lower().endswith(exts)):
                it = _file_item(f)
                it["is_dir"] = False
                entries.append(it)
        except OSError:
            continue
    parent = str(p.parent) if p.parent != p else ("::drives" if os.name == "nt" else None)
    return {"dir": str(p), "parent": parent, "shortcuts": _fs_shortcuts(kind), "entries": entries[:2000]}


def api_state():
    hosts = []
    for name, c in sorted(APP.store.snapshot()["hosts"].items(), key=lambda kv: kv[1].get("last_used", ""), reverse=True):
        hosts.append({"name": name, "host": c.get("host"), "port": c.get("port", 22), "user": c.get("user"),
                      "auth": c.get("auth", "password"), "last_used": c.get("last_used", ""),
                      "products": {k: v for k, v in (c.get("products") or {}).items() if k in PRODUCTS}})
    return {"version": TOOL_VERSION, "busy": APP.busy(), "settings": APP.ctx.settings, "hosts": hosts,
            "products": {k: {"name": v["name"], "port": v["port"]} for k, v in PRODUCTS.items()},
            "log_path": str(LOG.path or "")}


class Handler(BaseHTTPRequestHandler):
    server_version = "SightDeploy/" + TOOL_VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        if os.environ.get("SIGHT_DEPLOY_DEBUG"):  # 排查用：记录每个请求
            LOG.write("  [http] %s %s" % (self.client_address[0], fmt % a))

    # ---------------------------------------------------------------- 工具
    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _err(self, msg, code=400):
        # 出错时请求体可能没读完：关掉这条连接，免得残留的请求体被当成下一个请求
        self.close_connection = True
        self._json({"error": msg}, code)

    def _body_json(self):
        n = _int(self.headers.get("Content-Length") or 0)
        if n > 1024 * 1024:
            raise DeployError("请求太大")
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            raise DeployError("请求格式不对")
        if not isinstance(body, dict):
            raise DeployError("请求格式不对")
        return body

    def _host_ok(self):
        # 防 DNS rebinding：只接受 127.0.0.1 / localhost 访问
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def _origin_ok(self):
        """挡住浏览器里其他网页发来的跨站请求（含同机其他端口的页面）。"""
        port = APP.port
        origin = self.headers.get("Origin")
        if origin and origin not in ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port):
            return False
        sfs = self.headers.get("Sec-Fetch-Site")
        return sfs in (None, "same-origin", "none")

    def _token_ok(self, page=False, qs=None):
        """接口只认 X-Token 请求头（跨站请求带不上它）；只有打开页面本身才认地址里的 ?t= 和 cookie。"""
        t = self.headers.get("X-Token") or ""
        if page and not t and qs:
            t = (qs.get("t") or [""])[0]
        if page and not t:
            ck = self.headers.get("Cookie") or ""
            m = re.search(r"(?:^|;\s*)sd_token=([\w-]+)", ck)
            t = m.group(1) if m else ""
        return bool(t) and _secrets.compare_digest(t, APP.token)

    # ---------------------------------------------------------------- 路由
    def do_GET(self):
        self._safe(self._get)

    def do_POST(self):
        self._safe(self._post)

    def _safe(self, fn):
        try:
            fn()
        except DeployError as e:
            self._err(str(e))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            import traceback
            LOG.write(traceback.format_exc())
            try:
                self._err("程序内部错误，详见本地日志", 500)
            except Exception:
                pass

    def _get(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._host_ok():
            return self._err("forbidden", 403)
        if u.path in ("/", "/index.html"):
            return self._index(qs)
        if u.path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self._token_ok() or not self._origin_ok():
            return self._err("会话已失效，请重新打开 Sight 部署助手", 403)
        APP.last_beat = time.time()
        try:
            if u.path == "/api/ping":
                return self._json({"ok": True, "app": "sight-deploy"})
            if u.path == "/api/state":
                return self._json(api_state())
            if u.path == "/api/packages":
                return self._json(api_packages())
            if u.path == "/api/jdks":
                return self._json([_file_item(f) for f in find_jdk_archives()])
            if u.path == "/api/fs":
                return self._json(api_fs(qs))
            m = re.match(r"^/api/jobs/(\w+)/events$", u.path)
            if m:
                job = APP.jobs.get(m.group(1))
                if not job:
                    return self._err("任务不存在", 404)
                return self._json(job.snapshot(_int((qs.get("since") or ["0"])[0])))
            return self._err("not found", 404)
        except DeployError as e:
            return self._err(str(e))

    def _post(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._host_ok() or not self._token_ok() or not self._origin_ok():
            return self._err("会话已失效，请重新打开 Sight 部署助手", 403)
        APP.last_beat = time.time()
        try:
            if u.path == "/api/upload":
                return self._upload(qs)
            body = self._body_json()
            if u.path == "/api/heartbeat":
                return self._json({"ok": True, "busy": APP.busy()})
            if u.path == "/api/settings":
                kb, ht = _int(body.get("keep_backups")), _int(body.get("health_timeout"))
                if not 1 <= kb <= 50:
                    return self._err("备份保留份数请填 1~50")
                if not 60 <= ht <= 3600:
                    return self._err("等待时间请填 60~3600 秒")
                APP.store.update_settings({"keep_backups": kb, "health_timeout": ht})
                return self._json({"ok": True})
            if u.path == "/api/hosts/delete":
                if APP.busy():
                    return self._err("有任务正在执行，请稍后再删", 409)
                APP.store.remove(body.get("name"))
                return self._json({"ok": True})
            if u.path == "/api/shutdown":
                if APP.busy():
                    return self._err("有任务正在执行，请等它结束（或先在任务面板点「停止」）再退出", 409)
                self._json({"ok": True})
                threading.Thread(target=_shutdown, daemon=True).start()
                return
            if u.path == "/api/jobs":
                job = APP.start_job(body.get("kind"), body.get("params") or {})
                if not job:
                    return self._err("有任务正在执行，请等它结束", 409)
                return self._json({"job_id": job.id})
            m = re.match(r"^/api/jobs/(\w+)/(answer|cancel)$", u.path)
            if m:
                job = APP.jobs.get(m.group(1))
                if not job:
                    return self._err("任务不存在", 404)
                if m.group(2) == "cancel":
                    job.cancel()
                    return self._json({"ok": True})
                okk = job.answer(body.get("qid"), body.get("value"), body.get("cancel"))
                return self._json({"ok": okk}) if okk else self._err("这个问题已经过期", 409)
            return self._err("not found", 404)
        except DeployError as e:
            return self._err(str(e))

    def _index(self, qs):
        if not self._token_ok(page=True, qs=qs):
            b = ("<!doctype html><meta charset=utf-8><title>Sight 部署助手</title>"
                 "<body style='font-family:sans-serif;padding:40px'>会话已失效，请重新打开 Sight 部署助手。</body>").encode()
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        try:
            b = (RES_DIR / "web" / "index.html").read_bytes()
        except OSError:
            return self._err("界面文件缺失（web/index.html）", 500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", "sd_token=%s; Path=/; HttpOnly; SameSite=Strict" % APP.token)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(b)

    def _upload(self, qs):
        name = os.path.basename(unquote((qs.get("name") or [""])[0]).replace("\\", "/")) or "upload.bin"
        name = re.sub(r"[^\w.\-()（）一-龥]", "_", name)
        if not name or name.startswith("."):
            return self._err("文件名不对")
        n = _int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self._err("没有收到文件")
        d = CONF_DIR / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / name
        tmp = d / (name + ".part")
        left = n
        with open(tmp, "wb") as f:
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            tmp.unlink()
            return self._err("文件没有传完整")
        os.replace(tmp, dest)
        LOG.write("  [上传] %s（%s）→ %s" % (name, human_size(n), dest))
        return self._json({"path": str(dest)})


def _shutdown():
    time.sleep(0.3)
    LOG.write("===== 退出 =====")
    try:  # 只删自己写的（防止两个实例同时启动时互删）
        f = CONF_DIR / "instance.json"
        if json.loads(f.read_text(encoding="utf-8")).get("pid") == os.getpid():
            f.unlink()
    except Exception:
        pass
    os._exit(0)


def _cleanup_uploads():
    """拖进页面的安装包会复制到 ~/.sight-deploy/uploads；超过 7 天的清掉，免得越攒越多。"""
    d = CONF_DIR / "uploads"
    try:
        for f in d.iterdir():
            if f.is_file() and time.time() - f.stat().st_mtime > 7 * 86400:
                f.unlink()
    except OSError:
        pass


def _watchdog():
    """浏览器关掉 10 分钟后（且没有任务在跑）自动退出，免得后台一直挂着。
    没人看的任务也要收尾：实时日志 30 秒没人取就停；等用户回答的问题 10 分钟没人看就取消。"""
    while True:
        time.sleep(5)
        j = APP.current
        if j and not j.done:
            idle = time.time() - j.last_poll
            if (j.kind == "follow_logs" and idle > 30) or (j.question and idle > 600):
                LOG.write("  [自动停止] 任务 %s 已 %d 秒无人查看" % (j.kind, idle))
                j.cancel()
        # 10 分钟：留足余量，浏览器把后台标签页的定时器降频 / 休眠时不至于误退出
        if not APP.busy() and time.time() - APP.last_beat > 600:
            _shutdown()


def _existing_instance():
    """已经开着一个助手：返回它的地址，直接打开它，不再起第二个。"""
    f = CONF_DIR / "instance.json"
    try:
        info_ = json.loads(f.read_text(encoding="utf-8"))
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:%d/api/ping" % info_["port"],
                                     headers={"X-Token": info_["token"]})
        with urllib.request.urlopen(req, timeout=2) as resp:
            if json.loads(resp.read().decode()).get("app") == "sight-deploy":
                return "http://127.0.0.1:%d/?t=%s" % (info_["port"], info_["token"])
    except Exception:
        pass
    return None


def _open_browser(url):
    import webbrowser
    opened = False
    try:
        opened = webbrowser.open(url, new=1)
    except Exception:
        pass
    if not opened and os.name == "nt":
        try:
            os.startfile(url)  # noqa
            opened = True
        except Exception:
            pass
    if not opened and os.name == "nt" and FROZEN:
        # 无控制台的 exe：打不开浏览器时，至少弹个框把地址告诉用户
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "请把下面的地址复制到浏览器打开：\n\n" + url, "Sight 部署助手", 0x40)
        except Exception:
            pass


def main():
    global APP
    if sys.stdout is None:  # Windows 无控制台打包时 stdout 为 None
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    no_browser = "--no-browser" in sys.argv
    port_arg = None
    if "--port" in sys.argv:
        port_arg = _int(sys.argv[sys.argv.index("--port") + 1])
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    url = _existing_instance()
    if url:
        print("Sight 部署助手已在运行：%s" % url, flush=True)
        if not no_browser:
            _open_browser(url)
        return 0
    LOG.open()
    ensure_deps()
    _cleanup_uploads()
    APP = App()
    httpd = None
    for port in ([port_arg] if port_arg else [PREFERRED_PORT, PREFERRED_PORT + 1, PREFERRED_PORT + 2, 0]):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if not httpd:
        print("无法启动本机服务（端口被占用）", flush=True)
        return 1
    httpd.daemon_threads = True
    APP.port = httpd.server_address[1]
    APP.httpd = httpd
    url = "http://127.0.0.1:%d/?t=%s" % (APP.port, APP.token)
    inst_file = CONF_DIR / "instance.json"
    inst_file.write_text(json.dumps({"port": APP.port, "token": APP.token, "pid": os.getpid()}), encoding="utf-8")
    try:
        os.chmod(inst_file, 0o600)
    except OSError:
        pass
    LOG.write("网页地址：http://127.0.0.1:%d/" % APP.port)
    print("Sight 部署助手 %s 已启动：%s" % (TOOL_VERSION, url), flush=True)
    print("（浏览器没自动打开的话，把上面的地址复制到浏览器；关闭浏览器 10 分钟后程序自动退出）", flush=True)
    threading.Thread(target=_watchdog, daemon=True).start()
    if not no_browser:
        threading.Timer(0.5, _open_browser, args=(url,)).start()
    LOG.write("开始处理网页请求")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    _shutdown()


if __name__ == "__main__":
    sys.exit(main())
