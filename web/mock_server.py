#!/usr/bin/env python3
"""Dev-only mock of the Sight 部署助手 API (see ../API.md). Not shipped.

usage: python3 mock_server.py [port] [--empty] [--offline]
  --empty    start with no saved hosts (to see the first-run guide)
  --offline  官网查询失败（看「无法连接官网」的提示）
"""
import json, os, sys, threading, time, tempfile, uuid, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = "mocktoken123"
PORT = int(next((a for a in sys.argv[1:] if a.isdigit()), "8765"))
EMPTY = "--empty" in sys.argv
UPLOAD_DIR = tempfile.mkdtemp(prefix="sd-mock-upload-")

LOCK = threading.RLock()
STATE = {
    "version": "2.0.0",
    "settings": {"keep_backups": 3, "health_timeout": 300, "auto_check": True},
    "hosts": [] if EMPTY else [
        {"name": "生产-1", "host": "10.0.0.5", "port": 22, "user": "root", "auth": "password",
         "last_used": "2026-10-10 01:16",
         "products": {"sight-report": {"install_dir": "/opt/sight-report", "port": 9091, "version": "2.0.71"}}},
        {"name": "备用-2", "host": "10.0.0.6", "port": 22, "user": "deploy", "auth": "key",
         "last_used": "2026-10-08 17:40", "products": {}},
        {"name": "测试-离线", "host": "10.0.9.9", "port": 2222, "user": "root", "auth": "password",
         "last_used": "2026-09-30 09:02", "products": {}},
        {"name": "数据-3", "host": "10.0.0.7", "port": 22, "user": "root", "auth": "password",
         "last_used": "2026-10-12 15:20",
         "products": {"sight-datalink": {"install_dir": "/opt/sight-datalink", "port": 18680, "version": "0.2.0"}}},
    ],
    "products": {"sight-report": {"name": "SightReport", "port": 9091, "dir": "/opt/sight-report", "zip_prefix": "sight-report",
                                  "setup_path": "/setup", "setup_token": False, "rolling_upgrade": True, "schema_versioned": False},
                 "sight-wall": {"name": "SightWall", "port": 9090, "dir": "/opt/sight-wall", "zip_prefix": "sight-wall",
                                "setup_path": "/setup", "setup_token": False, "rolling_upgrade": True, "schema_versioned": False},
                 "sight-datalink": {"name": "Sight DataLink", "port": 18680, "dir": "/opt/sight-datalink", "zip_prefix": "sight-datalink",
                                "setup_path": "/", "setup_token": True, "rolling_upgrade": False, "schema_versioned": True}},
    "log_path": "/Users/demo/.sight-deploy/logs/sight-deploy-20261010.log",
}
BUSY = {"id": None}
JOBS = {}


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, kind, params):
        self.id = uuid.uuid4().hex[:10]
        self.kind, self.params = kind, params
        self.events = []
        self.cond = threading.Condition()
        self.done = False
        self.ok = None
        self.result = self.error = self.hint = None
        self.question = None
        self.answer = None
        self.answer_evt = threading.Event()
        self.cancelled = False
        self.qn = 0

    # -- emit helpers
    def emit(self, **ev):
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    def step(self, t): self.emit(t="step", text=t)
    def ok_(self, t): self.emit(t="ok", text=t)
    def warn(self, t): self.emit(t="warn", text=t)
    def info(self, t): self.emit(t="info", text=t)
    def log(self, t): self.emit(t="log", text=t)
    def fail(self, t): self.emit(t="fail", text=t)

    def sleep(self, s):
        end = time.time() + s
        while time.time() < end:
            if self.cancelled:
                raise Cancelled()
            time.sleep(0.05)

    def ask(self, q):
        """block until answered; returns value; raises Cancelled"""
        while True:
            self.qn += 1
            q = dict(q, qid="q%d" % self.qn)
            q.setdefault("error", None)
            self.answer_evt.clear()
            with self.cond:
                self.question = q
                self.cond.notify_all()
            while not self.answer_evt.wait(0.1):
                if self.cancelled:
                    raise Cancelled()
            with self.cond:
                self.question = None
                self.cond.notify_all()
            a = self.answer
            if a.get("cancel"):
                raise Cancelled()
            return a.get("value")

    def snapshot(self, since):
        with self.cond:
            if len(self.events) <= since and not self.done:
                self.cond.wait(1.0)
            evs = self.events[since:]
            return {"events": evs, "next": len(self.events), "done": self.done, "ok": self.ok,
                    "result": self.result, "error": self.error, "hint": self.hint,
                    "question": self.question}


class JobFail(Exception):
    def __init__(self, msg, hint=None):
        super().__init__(msg)
        self.hint = hint


def run_job(job):
    try:
        fn = SCRIPTS[job.kind]
        job.result = fn(job) or {}
        job.ok = True
    except Cancelled:
        job.ok, job.error = False, "已取消"
        job.info("已取消。")
    except JobFail as e:
        job.ok, job.error, job.hint = False, str(e), e.hint
    except Exception as e:  # noqa
        job.ok, job.error = False, "内部错误：%r" % e
    finally:
        with job.cond:
            job.done = True
            job.question = None
            job.cond.notify_all()
        with LOCK:
            if BUSY["id"] == job.id:
                BUSY["id"] = None


def find_host(name):
    for h in STATE["hosts"]:
        if h["name"] == name:
            return h
    return None


# ---------------------------------------------------------------- scripts
def s_status(job):
    name = job.params.get("host")
    h = find_host(name) or {"host": "?", "user": "root"}
    job.step("连接服务器")
    job.sleep(0.6)
    if h["host"] == "10.0.9.9":
        job.fail("连接 10.0.9.9:2222 超时")
        raise JobFail("无法连接到 root@10.0.9.9:2222（连接超时）",
                      "请确认服务器已开机、地址与端口正确，并且本机能访问该网络（VPN / 内网）。")
    if h["host"] == "10.0.0.6" and not job.params.get("_pw"):
        pw = job.ask({"kind": "secret", "title": "需要 SSH 密钥口令",
                      "label": "请输入私钥口令（仅用于本次连接，不会保存）", "detail": ["备用-2 使用密钥登录，密钥受口令保护。"]})
        job.params["_pw"] = pw
    job.ok_("连接成功")
    job.step("读取产品状态")
    job.sleep(0.6)
    prods = []
    if h["host"] == "10.0.0.5":
        prods.append({"product": "sight-report", "name": "SightReport", "installed": True,
                      "version": "2.0.71", "dir": "/opt/sight-report", "owner": "root",
                      "service": "systemd", "active": "active", "running": True, "healthy": True,
                      "bootstrapped": True, "db_reachable": True, "port": 9091,
                      "url": "http://10.0.0.5:9091/", "reachable": True,
                      "java": "21.0.4（/usr/lib/jvm/java-21-openjdk/bin/java）",
                      "db": "内置 H2 数据库（/opt/sight-report/data/h2）", "db_kind": "h2",
                      "disk_free": 31876000000, "backups": 2, "last_backup": "2026-10-10 01:16（2.0.69）"})
        prods.append({"product": "sight-wall", "name": "SightWall", "installed": False})
        prods.append({"product": "sight-datalink", "name": "Sight DataLink", "installed": False})
    elif h["host"] == "10.0.0.7":
        prods = [{"product": "sight-report", "name": "SightReport", "installed": False},
                 {"product": "sight-wall", "name": "SightWall", "installed": False},
                 {"product": "sight-datalink", "name": "Sight DataLink", "installed": True, "version": "0.2.0",
                  "dir": "/opt/sight-datalink", "owner": "root", "service": "systemd", "active": "active",
                  "running": True, "healthy": True, "bootstrapped": False, "db_reachable": None, "mode": "setup",
                  "port": 18680, "url": "http://10.0.0.7:18680/", "setup_url": "http://10.0.0.7:18680/", "reachable": True,
                  "java": "21.0.4（/usr/lib/jvm/java-21-openjdk/bin/java）", "db": "尚未配置（还没走安装向导）", "db_kind": "none",
                  "disk_free": 52000000000, "backups": 0, "last_backup": ""}]
    elif h["host"] == "10.0.0.6":
        prods.append({"product": "sight-report", "name": "SightReport", "installed": True,
                      "version": "2.0.69", "dir": "/data/sight/report", "owner": "sight",
                      "service": "process", "active": "inactive", "running": True, "healthy": False,
                      "bootstrapped": False, "db_reachable": False, "port": 9091,
                      "url": "http://10.0.0.6:9091/", "reachable": False,
                      "java": "17.0.9（/usr/bin/java）",
                      "db": "外部 MySQL 数据库（192.168.1.20:3306/sight）", "db_kind": "external",
                      "disk_free": 4200000000, "backups": 0, "last_backup": None})
        prods.append({"product": "sight-wall", "name": "SightWall", "installed": True, "version": "1.8.3",
                      "dir": "/data/sight/wall", "owner": "sight", "service": "systemd", "active": "inactive",
                      "running": False, "healthy": False, "bootstrapped": True, "db_reachable": True,
                      "port": 9090, "url": "http://10.0.0.6:9090/", "reachable": False,
                      "java": "17.0.9（/usr/bin/java）", "db": "内置 H2 数据库", "db_kind": "h2",
                      "disk_free": 4200000000, "backups": 1, "last_backup": "2026-09-01 10:00（1.8.1）"})
        prods.append({"product": "sight-datalink", "name": "Sight DataLink", "installed": True, "version": "0.1.9",
                      "dir": "/data/sight-datalink", "owner": "sight", "service": "systemd", "active": "active",
                      "running": True, "healthy": True, "bootstrapped": False, "db_reachable": False, "mode": "waiting",
                      "port": 18680, "url": "http://10.0.0.6:18680/", "setup_url": "http://10.0.0.6:18680/", "reachable": True,
                      "java": "21.0.4（/opt/sight-jdk/jdk-21.0.4+7/bin/java）", "db": "外部数据库 postgresql @ 192.168.1.30",
                      "db_kind": "external", "disk_free": 4200000000, "backups": 2, "last_backup": "2026-10-01 09:10（0.1.8）"})
    else:
        prods = [{"product": "sight-report", "name": "SightReport", "installed": False},
                 {"product": "sight-wall", "name": "SightWall", "installed": False},
                 {"product": "sight-datalink", "name": "Sight DataLink", "installed": False}]
    job.ok_("状态已更新")
    return {"host": name, "address": "%s@%s" % (h["user"], h["host"]),
            "os": "Linux x86_64 / CentOS 7.9 / up 3 days", "products": prods}


MOCK_TOKEN = "q8Zt3VfK1mWcR7xYpL2nB0sDuE9gHaJ4"


def s_deploy_datalink(job, hosts, pkg):
    """Sight DataLink 首次安装：安装向导 + 初始化令牌（看安装完成页）。"""
    results = []
    for hn in hosts:
        h = find_host(hn) or {"host": "?", "user": "root"}
        job.step("读取安装包 %s" % os.path.basename(pkg))
        job.ok_("Sight DataLink 0.2.0（212.4 MB，9 个文件）")
        job.info("检查 Sight DataLink 是否已安装 …")
        job.step("首次安装 Sight DataLink 0.2.0 → root@%s" % h["host"])
        vals = job.ask({"kind": "form", "title": "安装 Sight DataLink 0.2.0 到 root@%s" % h["host"], "error": None, "fields": [
            {"key": "dir", "label": "安装目录", "default": "/opt/sight-datalink"},
            {"key": "port", "label": "服务端口", "default": "18680", "type": "number"},
            {"key": "xmx", "label": "最大堆内存", "default": "", "placeholder": "留空 = 按服务器内存自适应",
             "help": "如 4g、8g。留空时程序按服务器可用内存的 75% 自动设置"}]})
        port = int(vals.get("port") or 18680)
        job.step("环境检查")
        for t in ("系统      Rocky Linux 9.4  x86_64", "systemd   可用（将注册为开机自启服务）", "Java      21.0.4（/usr/lib/jvm/java-21-openjdk/bin/java）",
                  "内存      15.4 GB，CPU 8 核", "磁盘      / 剩余 52.0 GB", "端口      %d 空闲" % port):
            job.ok_(t)
        job.step("确认安装计划")
        plan = [["产品", "Sight DataLink 0.2.0"], ["服务器", "root@%s" % h["host"]], ["安装目录", vals.get("dir") or "/opt/sight-datalink"],
                ["端口", str(port)], ["Java", "/usr/lib/jvm/java-21-openjdk/bin/java"], ["内存", "自适应（可用内存的 75%）"],
                ["运行方式", "系统服务 sight-datalink（开机自启、崩溃自动拉起），运行账号 root"]]
        job.emit(t="kv", title="安装计划", items=plan)
        if not job.ask({"kind": "confirm", "title": "开始安装？", "default": True, "detail": ["%s：%s" % tuple(x) for x in plan]}):
            raise Cancelled()
        job.step("上传安装包（212.4 MB）")
        total = 212_400_000
        for i in range(1, 9):
            job.sleep(0.15)
            job.emit(t="progress", id="up", label="上传 sight-datalink.jar", done=total * i // 8, total=total, speed=48_000_000, eta=(8 - i) * 0.5)
        job.ok_("上传完成，sha256 校验一致")
        job.step("安装")
        job.log("写入 systemd unit：/etc/systemd/system/sight-datalink.service")
        job.log("服务已安装并启动：sight-datalink（profile=prod，已设开机自启）")
        job.step("等待服务启动（首次启动约 30~90 秒）")
        for i in range(3):
            job.emit(t="live", id="wait", text="等待服务启动 … 0:%02d" % (i * 3 + 2))
            job.sleep(0.4)
        job.emit(t="live", id="wait", text="")
        job.ok_("服务已就绪（用时 0:11）")
        job.step("安装完成")
        job.ok_("Sight DataLink 0.2.0 已安装并启动")
        url = "http://%s:%d/" % (h["host"], port)
        job.info("访问地址：%s" % url)
        job.info("首次访问请按安装向导配置元数据库，之后「初始化管理员」时仍需要同一个初始化令牌")
        job.emit(t="kv", title="初始化令牌（一次性凭据，只显示在本次操作里）",
                 items=[["初始化令牌", MOCK_TOKEN], ["说明", "安装向导和之后的「初始化管理员」都需要这个令牌，请妥善转交给实施人员"]])
        results.append({"host": hn, "status": "完成", "url": url, "mode": "setup", "setup_token": MOCK_TOKEN})
        with LOCK:
            h.setdefault("products", {})["sight-datalink"] = {"install_dir": vals.get("dir") or "/opt/sight-datalink", "port": port, "version": "0.2.0"}
    return {"results": results}


def s_deploy(job):
    hosts = job.params.get("hosts") or []
    pkg = job.params.get("package", "")
    if "sight-datalink" in os.path.basename(pkg):
        return s_deploy_datalink(job, hosts, pkg)
    broken = "broken" in pkg
    results = []
    for hi, hn in enumerate(hosts):
        h = find_host(hn) or {"host": "?", "user": "root"}
        if len(hosts) > 1:
            job.step("处理服务器 %s（%d/%d）" % (hn, hi + 1, len(hosts)))
        job.step("检查服务器环境")
        job.emit(t="live", id="conn", text="正在连接 %s …" % h["host"])
        job.sleep(0.8)
        job.emit(t="live", id="conn", text="")
        job.ok_("SSH 连接正常，已获得 root 权限")
        job.log("$ uname -a\nLinux app01 3.10.0-1160.el7.x86_64 #1 SMP x86_64 GNU/Linux")
        job.log("$ df -h /opt")
        job.log("/dev/vda1   100G   69G   31G  70% /")
        job.warn("磁盘剩余 29.7 GB，建议保持在 10 GB 以上。")
        job.emit(t="kv", title="环境信息", items=[["系统", "CentOS 7.9 x86_64"], ["Java", "21.0.4"],
                                                  ["已装版本", "2.0.71"], ["磁盘剩余", "29.7 GB"]])
        job.sleep(0.4)
        job.step("生成升级计划")
        job.emit(t="kv", title="升级计划", items=[
            ["产品", "SightReport"], ["版本变化", "2.0.71 → 2.0.72"], ["安装目录", "/opt/sight-report"],
            ["备份位置", "/opt/sight-report-backup/"], ["预计中断", "约 1 分钟"]])
        job.info("升级会先备份当前程序和内置数据库，再替换文件并重启服务。")
        if hi == 0:
            ok = job.ask({"kind": "confirm", "title": "按以上计划升级 SightReport？", "default": True, "danger": False,
                          "detail": ["服务会停止约 1 分钟，正在使用的用户会被断开。",
                                     "升级前会自动备份，出问题可以在「回滚」里恢复。"]})
            if not ok:
                raise JobFail("已取消：未做任何改动。")
            idx = job.ask({"kind": "choose", "title": "选择升级方式", "default": 0,
                           "options": [{"label": "标准升级（推荐）", "desc": "备份后替换程序，保留全部配置与数据"},
                                       {"label": "仅替换程序文件", "desc": "不备份数据库，适合数据库在外部的情况"},
                                       {"label": "全新安装到另一个目录", "desc": "不动现有版本，适合先试用新版本"}]})
            job.info("已选择：%s" % ["标准升级", "仅替换程序文件", "全新安装"][idx])
            err = None
            while True:
                vals = job.ask({"kind": "form", "title": "确认服务参数", "error": err,
                                "detail": ["保持默认即可，如需修改请联系实施负责人。"],
                                "fields": [
                                    {"key": "port", "label": "访问端口", "default": "9091", "type": "number",
                                     "help": "浏览器访问时使用的端口，范围 1-65535"},
                                    {"key": "heap", "label": "最大内存", "default": "2g", "placeholder": "例如 2g、4096m",
                                     "type": "text"},
                                    {"key": "token", "label": "管理口令（可留空）", "default": "", "type": "password"}]})
                p = str(vals.get("port", ""))
                if p.isdigit() and 1 <= int(p) <= 65535:
                    break
                err = "端口必须是 1-65535 之间的数字。"
            job.ok_("服务参数已确认：端口 %s，内存 %s" % (vals["port"], vals["heap"]))
            jdk = job.ask({"kind": "file", "file_kind": "jdk", "title": "服务器上没有找到可用的 Java",
                           "label": "请选择 JDK 压缩包（.tar.gz），将一并上传并安装",
                           "detail": ["SightReport 2.0 需要 Java 17 或更高版本。"]})
            job.ok_("将使用 JDK 包：%s" % os.path.basename(jdk))
            word = job.ask({"kind": "typed", "title": "覆盖现有配置文件", "word": "覆盖",
                            "detail": ["/opt/sight-report/config/application.yml 已被手工修改过。",
                                       "继续会用新版默认配置覆盖，原文件会备份为 application.yml.bak。"]})
            job.info("已确认覆盖配置（输入：%s）" % word)
        job.step("上传安装包（28.6 MB）")
        total = 28_600_000
        done = 0
        while done < total:
            job.sleep(0.25)
            done = min(total, done + 3_400_000)
            spd = 6_800_000 + (done % 5) * 250_000
            job.emit(t="progress", id="up", label="上传 %s" % os.path.basename(pkg), done=done, total=total,
                     speed=spd, eta=max(0, (total - done) / spd))
        job.ok_("上传完成，校验通过（sha256 已匹配）")
        job.step("备份当前版本")
        for i in range(4):
            job.log("tar: adding /opt/sight-report/lib/part-%d.jar" % i)
            job.sleep(0.15)
        job.ok_("已备份到 /opt/sight-report-backup/sight-report-2.0.71-20261010-0116.tar.gz（29.8 MB）")
        if broken:
            job.step("替换程序文件")
            job.log("tar: Unexpected EOF in archive")
            job.log("tar: Error is not recoverable: exiting now")
            job.sleep(0.5)
            job.fail("解压安装包失败：压缩包不完整")
            raise JobFail("安装包已损坏，无法解压。",
                          "当前版本未被改动。请重新下载安装包（核对文件大小），然后再试一次。")
        job.step("替换程序并启动服务")
        job.ok_("程序文件已替换")
        for i in range(4):
            job.emit(t="live", id="wait", text="等待服务启动 … 0:%02d" % (i * 2 + 1))
            job.sleep(0.5)
        job.emit(t="live", id="wait", text="")
        job.ok_("服务已启动，健康检查通过")
        results.append({"host": hn, "status": "完成", "url": "http://%s:9091/" % h["host"]})
    return {"results": results}


def s_service(job):
    p = job.params
    names = {"start": "启动", "stop": "停止", "restart": "重启"}
    job.step("%s服务" % names.get(p.get("action"), "操作"))
    job.sleep(0.8)
    if p.get("host") == "备用-2" and p.get("action") == "restart":
        job.fail("systemctl restart sight-report 失败")
        job.log("Job for sight-report.service failed because the control process exited with error code.")
        raise JobFail("服务重启失败。", "请先打开「日志」查看原因（常见：端口被占用、Java 版本不符）。")
    job.ok_("已%s" % names.get(p.get("action"), "完成"))
    return {}


def s_logs(job):
    job.sleep(0.5)
    lines = []
    for i in range(200):
        lvl = "INFO"
        msg = "request /api/report/%d handled in %d ms" % (1000 + i, 12 + i % 40)
        if i % 37 == 5:
            lvl, msg = "WARN", "slow query detected: %d ms" % (900 + i)
        if i == 150:
            lvl, msg = "ERROR", "java.sql.SQLException: connection refused (retrying)"
        lines.append("2026-10-10 01:%02d:%02d.%03d %-5s [http-nio-9091-exec-%d] c.s.report.Handler - %s" %
                     (i // 60, i % 60, (i * 37) % 1000, lvl, i % 8, msg))
    return {"text": "\n".join(lines)}


def s_follow_logs(job):
    n = 0
    while True:
        job.sleep(1.0)
        n += 1
        lvl = "WARN" if n % 5 == 0 else "INFO"
        job.log("2026-10-10 02:%02d:%02d.000 %-5s [scheduler-%d] c.s.report.Tick - heartbeat #%d"
                % (n // 60, n % 60, lvl, n % 3, n))


def s_list_backups(job):
    job.sleep(0.6)
    if job.params.get("product") == "sight-datalink":
        ext = (find_host(job.params.get("host")) or {}).get("host") == "10.0.0.6"
        return {"backup_dir": "/data/sight-backups/sight-datalink", "current_version": "0.1.9", "db_kind": "external" if ext else "h2",
                "current_schema_version": "1.24",
                "backups": [{"file": "/data/sight-backups/sight-datalink/sight-datalink-0.1.8-20261001-091000.tar.gz", "version": "0.1.8",
                             "time": "2026-10-01 09:10", "size": 61_200_000, "schema_version": "1.21"},
                            {"file": "/data/sight-backups/sight-datalink/sight-datalink-0.1.9-20261012-150200.tar.gz", "version": "0.1.9",
                             "time": "2026-10-12 15:02", "size": 63_900_000, "schema_version": "1.24"}]}
    h = find_host(job.params.get("host")) or {}
    ext = h.get("host") == "10.0.0.6"
    return {"backup_dir": "/opt/sight-report-backup", "current_version": "2.0.71",
            "db_kind": "external" if ext else "h2",
            "backups": [] if job.params.get("host") == "测试-离线" else [
                {"file": "sight-report-2.0.71-20261010-0116.tar.gz", "version": "2.0.71", "time": "2026-10-10 01:16", "size": 29_800_000},
                {"file": "sight-report-2.0.69-20260921-1030.tar.gz", "version": "2.0.69", "time": "2026-09-21 10:30", "size": 27_400_000},
                {"file": "sight-report-2.0.65-20260802-0905.tar.gz", "version": "2.0.65", "time": "2026-08-02 09:05", "size": 26_100_000}]}


def s_rollback(job):
    job.step("停止服务")
    job.sleep(0.6)
    job.ok_("服务已停止")
    job.step("恢复备份 %s" % job.params.get("file"))
    job.emit(t="progress", id="rb", label="解压备份", done=10_000_000, total=29_800_000, speed=20_000_000, eta=1)
    job.sleep(0.8)
    job.emit(t="progress", id="rb", label="解压备份", done=29_800_000, total=29_800_000, speed=20_000_000, eta=0)
    job.ok_("程序%s已恢复" % ("和数据" if job.params.get("full") else ""))
    job.step("启动服务")
    job.sleep(0.8)
    job.ok_("服务已启动")
    return {}


def s_add_host(job):
    p = job.params
    job.step("测试连接 %s@%s:%s" % (p.get("user"), p.get("host"), p.get("port")))
    job.sleep(0.8)
    if p.get("host") in ("bad", "10.0.9.9"):
        job.fail("连接超时")
        raise JobFail("无法连接到 %s" % p.get("host"), "请检查地址、端口以及网络。")
    job.ok_("连接成功")
    if p.get("user") != "root":
        pw = job.ask({"kind": "secret", "title": "需要 sudo 密码",
                      "label": "%s 不是 root，请输入 sudo 密码" % p.get("user"),
                      "detail": ["密码只用于本次检查，并加密保存在本机。"]})
        if not pw:
            raise JobFail("未输入 sudo 密码")
        job.ok_("sudo 验证通过")
    else:
        pw = job.ask({"kind": "secret", "title": "需要 sudo 密码", "label": "请输入 sudo 密码（mock 演示：任意非空）"})
    job.step("保存服务器信息")
    name = p.get("name") or p.get("host")
    with LOCK:
        STATE["hosts"].append({"name": name, "host": p["host"], "port": int(p.get("port") or 22),
                               "user": p.get("user", "root"), "auth": p.get("auth", "password"),
                               "last_used": time.strftime("%Y-%m-%d %H:%M"), "products": {}})
    job.ok_("已保存为「%s」" % name)
    return {"name": name}


def s_edit_host(job):
    job.step("验证新密码")
    job.sleep(0.7)
    if not (job.params.get("password") or job.params.get("key_passphrase")):
        raise JobFail("密码不能为空")
    job.ok_("连接验证通过，已更新")
    return {"name": job.params.get("name")}


def s_test_host(job):
    job.step("测试连接")
    job.sleep(0.8)
    job.ok_("SSH 登录成功")
    return {"root": True}


ONLINE = {
    "checked_at": 0, "source": "site", "error": None, "site": "https://www.sightdata.top",
    "tool": {"latest": "2.1.0", "current": "2.0.0", "platform": "mac-arm64", "update": True, "can_download": True,
             "file_name": "Sight部署助手-2.1.0-mac.zip", "size_text": "14.3 MB", "url": "x",
             "page": "https://www.sightdata.top/downloads"},
    "products": {"sight-report": {"latest": "2.0.73", "file_name": "sight-report-2.0.73.zip", "size_text": "258M", "url": "x"},
                 "sight-wall": {"latest": "2.0.70", "file_name": "sight-wall-2.0.70.zip", "size_text": "293M", "url": "x"},
                 "sight-datalink": {"latest": "0.2.0", "file_name": "sight-datalink-0.2.0.zip", "size_text": "203M", "url": "x"}},
}


def s_download_package(job):
    pid = job.params.get("product")
    o = ONLINE["products"][pid]
    job.step("查询官网上 %s 的最新版本" % pid)
    job.sleep(0.5)
    job.ok_("最新版 %s（%s）" % (o["latest"], o["size_text"]))
    job.step("下载安装包 %s" % o["file_name"])
    total = 40_000_000
    for i in range(1, 21):
        job.sleep(0.12)
        job.emit(t="progress", id="p1", label="下载 %s %s" % (pid, o["latest"]), done=total * i // 20, total=total, speed=9_000_000, eta=(20 - i) // 4)
    job.step("校验安装包完整性")
    job.sleep(0.4)
    job.ok_("安装包完整（38.1 MB）")
    return {"path": UPLOAD_DIR + "/" + o["file_name"], "name": o["file_name"], "size": total, "product": pid,
            "version": o["latest"], "mtime": int(time.time()), "dir": UPLOAD_DIR, "reused": False}


def s_download_tool(job):
    t = ONLINE["tool"]
    job.step("下载 %s" % t["file_name"])
    for i in range(1, 11):
        job.sleep(0.1)
        job.emit(t="progress", id="p2", label="下载 Sight 部署助手 %s" % t["latest"], done=i * 1_430_000, total=14_300_000, speed=5_000_000, eta=(10 - i) // 3)
    return {"path": "/Users/demo/Downloads/" + t["file_name"], "name": t["file_name"], "dir": "/Users/demo/Downloads", "version": t["latest"], "can_apply": "--win" in sys.argv,
            "howto": ["先退出当前的部署助手（界面左下角「退出程序」）", "解压 zip，用新的「Sight部署助手.app」替换旧的；第一次打开仍需右键 → 打开"]}


SCRIPTS = {"download_package": s_download_package, "download_tool": s_download_tool, "status": s_status, "deploy": s_deploy, "service": s_service, "logs": s_logs,
           "follow_logs": s_follow_logs, "list_backups": s_list_backups, "rollback": s_rollback,
           "add_host": s_add_host, "edit_host": s_edit_host, "test_host": s_test_host}


# ---------------------------------------------------------------- files
HOME = os.path.expanduser("~")
FAKE_PACKAGES = [
    ("/Users/demo/Downloads", "sight-report-2.0.72.zip", 28_600_000, "sight-report", "2.0.72", 0),
    ("/Users/demo/Downloads", "sight-report-2.0.72-broken.zip", 12_400_000, "sight-report", "2.0.72", 3600),
    ("/Users/demo/Downloads", "sight-wall-1.9.0.zip", 142_000_000, "sight-wall", "1.9.0", 86400),
    ("/Users/demo/Downloads", "sight-datalink-0.2.0.zip", 212_400_000, "sight-datalink", "0.2.0", 1800),
    ("/Users/demo/Desktop", "sight-report-2.0.71.zip", 258_824_866, "sight-report", "2.0.71", 5 * 86400),
    ("/Users/demo/Desktop", "sight-deploy-2.0.0-extra.zip", 1_900_000_000, None, None, 12 * 86400),
]


def packages():
    now = int(time.time())
    out = [{"path": d + "/" + n, "name": n, "size": s, "mtime": now - ago, "dir": d, "product": p, "version": v}
           for d, n, s, p, v, ago in FAKE_PACKAGES]
    return out


def jdks():
    now = int(time.time())
    return [{"path": "/Users/demo/Downloads/OpenJDK21U-jdk_x64_linux_hotspot_21.0.4_7.tar.gz",
             "name": "OpenJDK21U-jdk_x64_linux_hotspot_21.0.4_7.tar.gz", "size": 206_100_000, "mtime": now - 7200,
             "dir": "/Users/demo/Downloads"}]


def fs_list(dirp, kind):
    if not dirp:
        dirp = HERE
    dirp = os.path.abspath(dirp)
    entries = []
    try:
        names = sorted(os.listdir(dirp), key=str.lower)
    except Exception:
        names = []
    for n in names:
        if n.startswith("."):
            continue
        fp = os.path.join(dirp, n)
        if os.path.isdir(fp):
            entries.append({"name": n, "path": fp, "is_dir": True})
        else:
            ext_ok = (kind == "any" or kind == "key" or
                      (kind == "package" and n.endswith((".zip", ".tar.gz", ".tgz"))) or
                      (kind == "jdk" and n.endswith((".tar.gz", ".tgz", ".zip"))))
            if ext_ok:
                try:
                    st = os.stat(fp)
                    entries.append({"name": n, "path": fp, "is_dir": False, "size": st.st_size, "mtime": int(st.st_mtime)})
                except OSError:
                    pass
    parent = os.path.dirname(dirp)
    return {"dir": dirp, "parent": parent if parent != dirp else None,
            "shortcuts": [{"name": "下载", "path": os.path.join(HOME, "Downloads")},
                          {"name": "桌面", "path": os.path.join(HOME, "Desktop")},
                          {"name": "主目录", "path": HOME}],
            "entries": entries}


# ---------------------------------------------------------------- http
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, obj, ctype="application/json; charset=utf-8"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json(self):
        raw = self._body()
        return json.loads(raw.decode("utf-8")) if raw else {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        self.route("GET", u)

    def do_POST(self):
        self.route("POST", urllib.parse.urlparse(self.path))

    def route(self, method, u):
        if not u.path.startswith("/api/"):
            return self._send(404, {"error": "未找到"})
        if self.headers.get("X-Token") != TOKEN:
            self._body()
            return self._send(403, {"error": "会话已失效"})
        q = urllib.parse.parse_qs(u.query)
        p = u.path
        try:
            if p == "/api/heartbeat":
                self._body()
                return self._send(200, {"ok": True})
            if p == "/api/state" and method == "GET":
                with LOCK:
                    return self._send(200, dict(STATE, busy=BUSY["id"]))
            if p == "/api/online" and method == "GET":
                if "--offline" in sys.argv:
                    return self._send(200, {"error": "无法连接官网（官网：域名解析失败，电脑可能没有联网）", "tool": None, "products": {}, "site": ONLINE["site"]})
                time.sleep(1.2)
                return self._send(200, dict(ONLINE, checked_at=int(time.time())))
            if p == "/api/apply_update":
                self._body()
                return self._send(200, {"ok": True})
            if p == "/api/reveal":
                self._body()
                return self._send(200, {"ok": True})
            if p == "/api/settings":
                d = self._json()
                with LOCK:
                    STATE["settings"].update({"keep_backups": int(d["keep_backups"]), "health_timeout": int(d["health_timeout"]),
                                              "auto_check": d.get("auto_check") is not False})
                return self._send(200, {"ok": True})
            if p == "/api/hosts/delete":
                d = self._json()
                with LOCK:
                    STATE["hosts"] = [h for h in STATE["hosts"] if h["name"] != d.get("name")]
                return self._send(200, {"ok": True})
            if p == "/api/shutdown":
                self._body()
                self._send(200, {"ok": True})
                threading.Timer(0.3, lambda: os._exit(0)).start()
                return
            if p == "/api/packages":
                return self._send(200, packages())
            if p == "/api/jdks":
                return self._send(200, jdks())
            if p == "/api/fs":
                return self._send(200, fs_list((q.get("dir") or [""])[0], (q.get("kind") or ["any"])[0]))
            if p == "/api/upload":
                name = os.path.basename((q.get("name") or ["upload.bin"])[0])
                dest = os.path.join(UPLOAD_DIR, name)
                n = int(self.headers.get("Content-Length") or 0)
                left = n
                with open(dest, "wb") as f:
                    while left > 0:
                        chunk = self.rfile.read(min(65536, left))
                        if not chunk:
                            break
                        f.write(chunk)
                        left -= len(chunk)
                        time.sleep(0.01)
                return self._send(200, {"path": dest})
            if p == "/api/jobs" and method == "POST":
                d = self._json()
                with LOCK:
                    if BUSY["id"]:
                        return self._send(409, {"error": "已有任务正在进行，请等它结束后再试。"})
                    kind = d.get("kind")
                    if kind not in SCRIPTS:
                        return self._send(400, {"error": "不支持的任务类型：%s" % kind})
                    job = Job(kind, d.get("params") or {})
                    JOBS[job.id] = job
                    BUSY["id"] = job.id
                threading.Thread(target=run_job, args=(job,), daemon=True).start()
                return self._send(200, {"job_id": job.id})
            parts = p.strip("/").split("/")
            if len(parts) == 4 and parts[1] == "jobs":
                job = JOBS.get(parts[2])
                if not job:
                    return self._send(404, {"error": "任务不存在"})
                act = parts[3]
                if act == "events":
                    return self._send(200, job.snapshot(int((q.get("since") or ["0"])[0])))
                if act == "answer":
                    d = self._json()
                    cur = job.question
                    if not cur or cur["qid"] != d.get("qid"):
                        return self._send(409, {"error": "这个问题已经不需要回答了"})
                    job.answer = d
                    job.answer_evt.set()
                    return self._send(200, {"ok": True})
                if act == "cancel":
                    self._body()
                    job.cancelled = True
                    return self._send(200, {"ok": True})
            return self._send(404, {"error": "未找到接口"})
        except Exception as e:  # noqa
            return self._send(500, {"error": "mock 内部错误：%r" % e})


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.daemon_threads = True
    print("mock: http://127.0.0.1:%d/?t=%s" % (PORT, TOKEN), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
