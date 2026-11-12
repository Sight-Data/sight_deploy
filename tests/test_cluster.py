# -*- coding: utf-8 -*-
"""集群：库指纹归组、nginx 配置生成、机密拷贝的远端脚本、滚动升级判定、集群记录校验（不连服务器）。

见 sight-data 仓库 docs/集群部署与集群入口-设计.md §4.3、§6.1、§8。运行：python3 -m unittest discover -s tests -v
本机装了 nginx 且以 root 运行、并设置 SIGHT_TEST_NGINX=1 时，另外用真实 nginx 校验配置并实测「503 换一台」。
"""
import base64
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sight_deploy as sd  # noqa: E402

HOSTS = {"node-01": {"host": "10.0.0.11", "products": {}}, "node-02": {"host": "10.0.0.12", "products": {}},
         "entry": {"host": "10.0.0.10", "products": {}}}


def rec(**kw):
    r = {"product": "sight-report", "entry": {"host": "entry", "port": 80, "server_name": "", "public_url": ""},
         "instances": [{"host": "node-01", "product": "sight-report", "port": 9091},
                       {"host": "node-02", "product": "sight-report", "port": 9091, "down": True}]}
    r.update(kw)
    return r


class Fingerprint(unittest.TestCase):
    def test_same_db_same_fp(self):
        a = sd.db_fingerprint("jdbc:postgresql://DB.Example.com:5432/sight?ssl=true&currentSchema=rpt")
        b = sd.db_fingerprint("jdbc:postgresql://db.example.com:5432/sight/?currentSchema=rpt&connectTimeout=5")
        self.assertEqual(a, b, "大小写、末尾斜杠、无关参数不影响")
        self.assertNotEqual(a, sd.db_fingerprint("jdbc:postgresql://db.example.com:5432/sight?currentSchema=other"),
                            "模式不同是不同的库")
        self.assertNotEqual(a, sd.db_fingerprint("jdbc:postgresql://db.example.com:5432/sight2?currentSchema=rpt"))
        self.assertEqual(len(a), 16)

    def test_h2_and_empty(self):
        self.assertIsNone(sd.db_fingerprint("jdbc:h2:file:./data/db"))
        self.assertIsNone(sd.db_fingerprint(""))
        self.assertIsNone(sd.db_fingerprint(None))

    def test_sqlserver_style(self):
        a = sd.db_fingerprint("jdbc:sqlserver://10.0.0.5:1433;databaseName=sight;encrypt=false")
        self.assertEqual(a, sd.db_fingerprint("jdbc:sqlserver://10.0.0.5:1433;encrypt=true;databaseName=sight"))
        self.assertNotEqual(a, sd.db_fingerprint("jdbc:sqlserver://10.0.0.5:1433;databaseName=other"))


class Suggestions(unittest.TestCase):
    def snap(self):
        return {"hosts": {
            "a": {"products": {"sight-report": {"db_fp": "f1", "port": 9091, "cluster_mode": True},
                               "sight-report@b": {"db_fp": "f1", "port": 9092, "cluster_mode": True}}},
            "b": {"products": {"sight-report": {"db_fp": "f1", "port": 9091},
                               "sight-wall": {"db_fp": "f1", "port": 9090}}},
            "c": {"products": {"sight-report": {"db_fp": "f2", "port": 9091}, "sight-datalink": {"db_fp": "f3"}}}},
            "clusters": {}}

    def test_groups_by_product_and_fp(self):
        res = sd.cluster_suggestions(self.snap())
        self.assertEqual(len(res), 1, "wall 只有一个、f2 只有一个，不成组")
        g = res[0]
        self.assertEqual(g["product"], "sight-report")
        self.assertEqual([(m["host"], m["product"]) for m in g["instances"]],
                         [("a", "sight-report"), ("a", "sight-report@b"), ("b", "sight-report")])
        self.assertFalse(g["cluster_mode"], "b 还没开集群")

    def test_already_clustered_excluded(self):
        s = self.snap()
        s["clusters"] = {"x": {"instances": [{"host": "a", "product": "sight-report"}, {"host": "b", "product": "sight-report"}]}}
        self.assertEqual(sd.cluster_suggestions(s), [], "归过组的不再建议；剩下一个不成组")


class NginxConf(unittest.TestCase):
    def test_upstream_and_locations(self):
        conf, proxy = sd.nginx_conf("report-prod", rec(), HOSTS)
        self.assertIn("upstream sight_report_prod {", conf)
        self.assertIn("server 10.0.0.11:9091 max_fails=2 fail_timeout=10s;", conf)
        self.assertIn("server 10.0.0.12:9091 max_fails=2 fail_timeout=10s down;", conf, "升级中的实例标 down 而不是删掉")
        self.assertIn("proxy_next_upstream error timeout http_503;", conf)
        self.assertIn("proxy_next_upstream error timeout http_404 http_503;", conf, "/assets/ 在新旧版本间换一台")
        self.assertIn("location /api/machine/ { return 404; }", conf)
        self.assertIn("map $http_upgrade $sight_conn_report_prod", conf, "map 变量按集群区分，同一入口多个集群不冲突")
        self.assertIn("access_log /var/log/nginx/sight-report-prod-ws.log sight_noargs_report_prod;", conf)
        self.assertEqual(conf.count("include /etc/nginx/sight/report-prod-proxy.conf;"), 3,
                         "location 里写了 proxy_set_header 就不继承上层，每个 location 都要 include")
        self.assertIn("server_name 10.0.0.10;", conf)
        self.assertIn("X-Forwarded-Proto $scheme", proxy)

    def test_same_host_uses_loopback(self):
        r = rec(entry={"host": "node-01", "port": 8080, "server_name": "report.example.com"})
        r["instances"].append({"host": "node-01", "product": "sight-report@b", "port": 9092})
        conf, _ = sd.nginx_conf("c1", r, HOSTS)
        self.assertIn("server 127.0.0.1:9091", conf)
        self.assertIn("server 127.0.0.1:9092", conf)
        self.assertIn("server 10.0.0.12:9091", conf)
        self.assertIn("listen 8080;", conf)
        self.assertIn("server_name report.example.com 10.0.0.11;", conf)

    def test_public_url_host_in_server_name(self):
        r = rec(entry={"host": "entry", "public_url": "https://bi.corp.cn/"})
        self.assertEqual(sd.nginx_server_names(r, HOSTS), ["bi.corp.cn", "10.0.0.10"])

    def test_parse_info(self):
        d = sd.parse_nginx_info("nginx=/usr/sbin/nginx\nversion=1.20.1\ntest=ok\nconfd=1\nwst=0\nrunning=1\nconf=1\n"
                                "up=server 10.0.0.11:9091 max_fails=2 fail_timeout=10s;   # node-01 sight-report\n"
                                "up=server 10.0.0.12:9091 max_fails=2 fail_timeout=10s down;   # node-02 sight-report\npkg=yum\n")
        self.assertEqual(d["version"], "1.20.1")
        self.assertEqual(d["upstream"], [{"addr": "10.0.0.11", "port": 9091, "down": False},
                                         {"addr": "10.0.0.12", "port": 9091, "down": True}])


class Secrets(unittest.TestCase):
    def out(self, envfile=None, unit=None, dbconf=None):
        def b(x):
            return base64.b64encode(x.encode() if isinstance(x, str) else x).decode()
        s = ""
        if envfile is not None:
            s += "@@envfile %s\n" % b(envfile)
        if unit is not None:
            s += "@@unit %s\n" % b(unit)
        if dbconf is not None:
            s += "@@dbconf %s\n" % b(dbconf)
        return s

    def test_merge_and_filter(self):
        unit = ("[Service]\nEnvironment=PATH=/usr/bin\nEnvironment=\"SIGHTDATA_SERVER_KEY=from-unit\" SIGHTDATA_AI_KEY=ai1\n"
                "Environment=JVM_OPTS=-Xmx4g\n")
        envf = "# x\nSERVER_PORT=9092\nSIGHTDATA_SERVER_KEY='from-env'\nMR_DB_PASSWORD=p@ss=w0rd\n"
        r = sd.parse_instance_secrets(self.out(envf, unit, '{"url":"jdbc:postgresql://x/y"}'))
        self.assertEqual(r["env"], {"SIGHTDATA_SERVER_KEY": "from-env", "SIGHTDATA_AI_KEY": "ai1",
                                    "MR_DB_PASSWORD": "p@ss=w0rd"}, "sight.env 覆盖 Environment=；端口、PATH 不拷")
        self.assertEqual(r["dbconf"], b'{"url":"jdbc:postgresql://x/y"}')
        self.assertFalse(r["jvm_props"])

    def test_jvm_props_detected(self):
        r = sd.parse_instance_secrets(self.out(unit="[Service]\nEnvironment=\"JVM_OPTS=-Dsightdata.server.key=k\"\n"))
        self.assertTrue(r["jvm_props"])
        self.assertIsNone(r["dbconf"])

    def test_fingerprint(self):
        a = sd.crypto_fingerprint({"SIGHTDATA_SERVER_KEY": "k"})
        self.assertEqual(a, sd.crypto_fingerprint({"SIGHTDATA_SERVER_KEY": "k", "MR_DB_URL": "ignored"}))
        self.assertNotEqual(a, sd.crypto_fingerprint({"SIGHTDATA_SERVER_KEY": "k2"}))
        self.assertNotEqual(a, sd.crypto_fingerprint({}))

    def test_payload_rejects_bad(self):
        with self.assertRaises(sd.DeployError):
            sd.seed_payload({"bad key": "x"}, None)
        with self.assertRaises(sd.DeployError):
            sd.seed_payload({"A": "x\ny"}, None)

    def test_seed_apply_script_runs(self):
        """远端合并脚本用真 bash 跑一遍：覆盖同名变量、保留其他行、写 database.config、删掉临时文件、权限 600。"""
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "sight.env"), "w") as f:
                f.write("SERVER_PORT=9092\nSIGHTDATA_AI_KEY=old\n")
            seed = os.path.join(d, "seed")
            with open(seed, "w") as f:
                f.write(sd.seed_payload({"SIGHTDATA_AI_KEY": "n$e'w\"", "MR_DB_PASSWORD": "a b=c"}, b'{"url":"u"}'))
            subprocess.run(["bash", "-c", sd.seed_apply_script(seed, d, "root")], check=True)
            with open(os.path.join(d, "sight.env")) as f:
                env = f.read().splitlines()
            self.assertEqual(sorted(env), sorted(["SERVER_PORT=9092", "SIGHTDATA_AI_KEY=n$e'w\"", "MR_DB_PASSWORD=a b=c"]))
            with open(os.path.join(d, "data/work/database.config"), "rb") as f:
                self.assertEqual(f.read(), b'{"url":"u"}')
            self.assertFalse(os.path.exists(seed), "临时文件要删掉")
            self.assertEqual(oct(os.stat(os.path.join(d, "sight.env")).st_mode & 0o777), "0o600")
            self.assertEqual(oct(os.stat(os.path.join(d, "data/work/database.config")).st_mode & 0o777), "0o600")
        finally:
            shutil.rmtree(d)


class Rolling(unittest.TestCase):
    def test_decision(self):
        self.assertFalse(sd.rolling_decision(None, ["2.0.117"])[0])
        self.assertTrue(sd.rolling_decision("2.0.116", ["2.0.116", "2.0.117"])[0])
        ok_, why = sd.rolling_decision("2.0.116", ["2.0.115", "2.0.117"])
        self.assertFalse(ok_)
        self.assertIn("2.0.115", why)
        self.assertFalse(sd.rolling_decision("2.0.116", ["", "2.0.117"])[0], "读不到版本按不能混跑处理")


class FakeStore(sd.Store):
    def __init__(self, data):  # noqa: 不读写磁盘
        self.data = data
        self.lock = threading.RLock()
        self.saved = 0

    def save(self):
        self.saved += 1


class SaveCluster(unittest.TestCase):
    def store(self):
        return FakeStore({"hosts": {
            "a": {"host": "10.0.0.1", "products": {"sight-report": {"port": 9091, "db_fp": "f1", "install_dir": "/opt/sight-report"},
                                                   "sight-report@b": {"port": 9092, "db_fp": "f1"}}},
            "b": {"host": "10.0.0.2", "products": {"sight-report": {"port": 9091, "db_fp": "f1"},
                                                   "sight-wall": {"port": 9090}}},
            "c": {"host": "10.0.0.3", "products": {"sight-report": {"port": 9091, "db_fp": "f2"}}}}})

    def body(self, **kw):
        b = {"name": "prod", "product": "sight-report",
             "instances": [{"host": "a", "product": "sight-report"}, {"host": "a", "product": "sight-report@b"},
                           {"host": "b", "product": "sight-report"}],
             "entry": {"host": "a", "port": 80}}
        b.update(kw)
        return b

    def test_create(self):
        st = self.store()
        self.assertEqual(sd.save_cluster(st, self.body()), "prod")
        c = st.data["clusters"]["prod"]
        self.assertEqual([(m["host"], m["product"], m["port"]) for m in c["instances"]],
                         [("a", "sight-report", 9091), ("a", "sight-report@b", 9092), ("b", "sight-report", 9091)])
        self.assertEqual(c["db_fp"], "f1")
        self.assertEqual(c["entry"], {"host": "a", "port": 80, "server_name": "", "public_url": ""})

    def test_rejects(self):
        st = self.store()
        for body, msg in [
                (self.body(name="Bad_Name"), "集群名"),
                (self.body(instances=[{"host": "a", "product": "sight-report"}, {"host": "c", "product": "sight-report"}]),
                 "同一个数据库"),
                (self.body(instances=[{"host": "b", "product": "sight-wall"}]), "不是"),
                (self.body(instances=[{"host": "zz", "product": "sight-report"}]), "不存在"),
                (self.body(entry={"host": "a", "port": 9091}), "端口"),
                (self.body(entry={"host": "a", "public_url": "bi.corp.cn"}), "http"),
                (self.body(product="sight-datalink"), "暂不支持"),
                (self.body(product="sight-wall", instances=[{"host": "b", "product": "sight-wall"}]), "暂不支持"),
                (self.body(shared_dir="mnt/x"), "绝对路径")]:
            with self.assertRaises(sd.DeployError, msg=msg) as cm:
                sd.save_cluster(st, body)
            self.assertIn(msg, str(cm.exception))

    def test_member_in_two_clusters(self):
        st = self.store()
        sd.save_cluster(st, self.body(instances=[{"host": "a", "product": "sight-report"}]))
        with self.assertRaises(sd.DeployError) as cm:
            sd.save_cluster(st, self.body(name="other", instances=[{"host": "a", "product": "sight-report"}]))
        self.assertIn("已经在集群「prod」里", str(cm.exception))

    def test_edit_keeps_down_and_renames(self):
        st = self.store()
        sd.save_cluster(st, self.body())
        st.data["clusters"]["prod"]["instances"][2]["down"] = True
        sd.save_cluster(st, self.body(name="prod2", old_name="prod"))
        self.assertNotIn("prod", st.data["clusters"])
        self.assertTrue(st.data["clusters"]["prod2"]["instances"][2]["down"], "编辑时保留摘下状态")

    def test_cluster_of(self):
        st = self.store()
        sd.save_cluster(st, self.body())
        self.assertEqual(sd.cluster_of(st.data["clusters"], "a", "sight-report@b")[0], "prod")
        self.assertIsNone(sd.cluster_of(st.data["clusters"], "c", "sight-report")[0])


class Notices(unittest.TestCase):
    def test_scoped_to_cluster_gateway(self):
        hosts = {"dmz1": {"products": {sd.GATEWAY: {"version": "2.0.100"}}},
                 "dmz2": {"products": {sd.GATEWAY: {"version": "2.0.100"}}}}
        self.assertEqual(len(sd.gateway_sync_notices(hosts, "2.0.117")), 2)
        self.assertEqual([n["host"] for n in sd.gateway_sync_notices({"dmz2": hosts["dmz2"]}, "2.0.117")], ["dmz2"])


# ------------------------------------------------------------------ 真实 nginx（可选）

class _Upstream(http.server.BaseHTTPRequestHandler):
    status = 200
    name = "?"

    def do_GET(self):
        body = json.dumps({"node": self.name, "path": self.path}).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve(name, status):
    cls = type("U_" + name, (_Upstream,), {"name": name, "status": status})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class LocalRunner:
    """在本机执行 apply_nginx 的远端脚本（测试用）。"""
    def run(self, script, sudo=False, timeout=600, **kw):
        p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr


@unittest.skipUnless(os.environ.get("SIGHT_TEST_NGINX") == "1" and shutil.which("nginx") and os.geteuid() == 0,
                     "设置 SIGHT_TEST_NGINX=1 且本机有 nginx、以 root 运行时才测（会改 /etc/nginx）")
class RealNginx(unittest.TestCase):
    NAME = "sdtest"

    def setUp(self):
        self.up_ok, self.up_503 = _serve("ok", 200), _serve("busy", 503)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()

    def tearDown(self):
        self.up_ok.shutdown()
        self.up_503.shutdown()
        n = sd.nginx_names(self.NAME)
        for f in (n["conf"], n["proxy"], n["conf"] + ".bak", n["proxy"] + ".bak"):
            if os.path.exists(f):
                os.remove(f)
        subprocess.run(["nginx", "-s", "reload"], capture_output=True)

    def get(self, path):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), headers={"Host": "sd.test"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, {}

    def test_apply_retry_and_rollback(self):
        hosts = {"e": {"host": "127.0.0.1"}}
        r = {"product": "sight-report", "entry": {"host": "e", "port": self.port, "server_name": "sd.test"},
             "instances": [{"host": "x", "addr": "127.0.0.1", "product": "sight-report", "port": self.up_503.server_address[1]},
                           {"host": "y", "addr": "127.0.0.1", "product": "sight-report@b", "port": self.up_ok.server_address[1]}]}
        sd.apply_nginx(LocalRunner(), self.NAME, r, hosts)
        time.sleep(1)
        for _ in range(6):
            st, j = self.get("/api/health/live")
            self.assertEqual((st, j.get("node")), (200, "ok"), "未就绪（503）的实例自动换一台")
        self.assertEqual(self.get("/api/machine/x")[0], 404, "机器接口不经入口暴露")
        # 坏配置：nginx -t 不过 → 恢复上一份，旧配置继续生效
        bad = json.loads(json.dumps(r))
        bad["entry"]["server_name"] = "bad;name"
        with self.assertRaises(sd.DeployError):
            sd.apply_nginx(LocalRunner(), self.NAME, bad, hosts)
        time.sleep(0.5)
        self.assertEqual(self.get("/api/health/live")[0], 200)
        # 把好的那台摘下：只剩 503 的那台
        r["instances"][1]["down"] = True
        sd.apply_nginx(LocalRunner(), self.NAME, r, hosts)
        time.sleep(1)
        self.assertEqual(self.get("/api/health/live")[0], 503)


class UpgradeOrchestration(unittest.TestCase):
    """集群升级的编排顺序（替换掉连服务器的部分）：滚动 = 逐个「摘下 → 升级 → 放回」；失败停在那台、不放回；
    不能滚动 = 先全停再逐个升级。"""

    def setUp(self):
        self.calls = []
        self.store = FakeStore({"hosts": {"a": {"host": "10.0.0.1", "products": {"sight-report": {"port": 9091}}},
                                          "b": {"host": "10.0.0.2", "products": {"sight-report": {"port": 9091}}},
                                          "e": {"host": "10.0.0.9", "products": {}}}})
        sd.save_cluster(self.store, {"name": "c1", "product": "sight-report", "entry": {"host": "e", "port": 80},
                                     "instances": [{"host": "a", "product": "sight-report"}, {"host": "b", "product": "sight-report"}]})
        self.ctx = sd.Ctx(self.store)
        self.vers = {"a": "2.0.116", "b": "2.0.116"}
        self.fail_on = None
        self.pkg = type("Pkg", (), {"product": "sight-report", "version": "2.0.117", "name": "SightReport",
                                    "rolling_from": "2.0.116"})()
        self.patches = {
            "load_package": lambda path: self.pkg,
            "_member_status": lambda ctx, m: {"version": self.vers[m["host"]]},
            "confirm": lambda *a, **k: True,
            "entry_apply": self.fake_entry,
            "_upgrade_member": self.fake_upgrade,
            "_stop_member": lambda ctx, m: self.calls.append(("stop", m["host"])),
            "open_host": self.fake_open,
        }
        self.saved = {k: getattr(sd, k) for k in self.patches}
        for k, v in self.patches.items():
            setattr(sd, k, v)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(sd, k, v)

    def fake_entry(self, ctx, name, rec=None, install=False, quiet=False):
        rec = rec or ctx.store.data["clusters"][name]
        self.calls.append(("nginx", tuple(sorted(m["host"] for m in rec["instances"] if m.get("down")))))
        return True

    def fake_upgrade(self, ctx, pkg, m, rolling):
        if m["host"] == self.fail_on:
            raise sd.DeployError("没起来")
        self.calls.append(("upgrade", m["host"], rolling))

    def fake_open(self, store, alias):
        raise AssertionError("不应该连服务器：%s" % alias)

    def test_rolling_order(self):
        res = sd.job_cluster_upgrade(self.ctx, {"cluster": "c1", "package": "x"})
        self.assertTrue(res["rolling"])
        self.assertEqual(self.calls, [("nginx", ("a",)), ("upgrade", "a", True), ("nginx", ()),
                                      ("nginx", ("b",)), ("upgrade", "b", True), ("nginx", ())])

    def test_failure_stays_down(self):
        self.fail_on = "b"
        with self.assertRaises(sd.DeployError) as cm:
            sd.job_cluster_upgrade(self.ctx, {"cluster": "c1", "package": "x"})
        self.assertIn("停在 b", str(cm.exception))
        self.assertEqual(self.calls[-1], ("nginx", ("b",)), "失败的实例不放回入口")
        self.assertTrue(self.store.data["clusters"]["c1"]["instances"][1]["down"])
        self.assertFalse(self.store.data["clusters"]["c1"]["instances"][0]["down"], "已升级的照常在入口")

    def test_entry_failure_before_stop_reverts(self):
        def boom(ctx, name, rec=None, install=False, quiet=False):
            raise sd.DeployError("nginx -t 不通过")
        sd.entry_apply = boom
        with self.assertRaises(sd.DeployError):
            sd.job_cluster_upgrade(self.ctx, {"cluster": "c1", "package": "x"})
        self.assertEqual(self.calls, [], "入口没摘成就不停服务")
        self.assertFalse(any(m["down"] for m in self.store.data["clusters"]["c1"]["instances"]), "记录也改回去")

    def test_full_stop_when_too_old(self):
        self.vers["b"] = "2.0.110"
        res = sd.job_cluster_upgrade(self.ctx, {"cluster": "c1", "package": "x"})
        self.assertFalse(res["rolling"])
        self.assertEqual(self.calls, [("stop", "a"), ("stop", "b"), ("upgrade", "a", False), ("upgrade", "b", False)],
                         "整体停机：先全停，再逐个升级，不动入口")

    def test_skips_up_to_date_and_refuses_downgrade(self):
        self.vers["a"] = "2.0.117"
        sd.job_cluster_upgrade(self.ctx, {"cluster": "c1", "package": "x"})
        self.assertEqual([c for c in self.calls if c[0] == "upgrade"], [("upgrade", "b", True)])
        self.vers["a"] = "2.0.118"
        with self.assertRaises(sd.DeployError):
            sd.job_cluster_upgrade(self.ctx, {"cluster": "c1", "package": "x"})


class LocalRemote(LocalRunner):
    """当作「入口服务器」的本机（真实 nginx + curl）。"""
    alias, host, user, is_root = "e", "127.0.0.1", "root", True

    def label(self):
        return "本机"

    def close(self):
        pass


@unittest.skipUnless(os.environ.get("SIGHT_TEST_NGINX") == "1" and shutil.which("nginx") and os.geteuid() == 0,
                     "设置 SIGHT_TEST_NGINX=1 且本机有 nginx、以 root 运行时才测（会改 /etc/nginx）")
class EntryJob(unittest.TestCase):
    """cluster_entry 任务走一遍：检查 nginx → 生成配置 → 校验重载 → 经入口探测；放回 / 摘下实例。"""

    def setUp(self):
        self.up = _serve("ok", 200)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.store = FakeStore({"hosts": {"e": {"host": "127.0.0.1", "products": {}},
                                          "n": {"host": "127.0.0.1", "products": {"sight-report": {"port": self.up.server_address[1]}}}}})
        sd.save_cluster(self.store, {"name": "sdjob", "product": "sight-report", "entry": {"host": "e", "port": self.port},
                                     "instances": [{"host": "n", "product": "sight-report"}]})
        self.ctx = sd.Ctx(self.store)
        self.saved = {k: getattr(sd, k) for k in ("open_host", "probe_env", "confirm")}
        sd.open_host = lambda store, alias: LocalRemote()
        sd.probe_env = lambda *a, **k: {}
        sd.confirm = lambda *a, **k: True

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(sd, k, v)
        self.up.shutdown()
        n = sd.nginx_names("sdjob")
        for f in (n["conf"], n["proxy"], n["conf"] + ".bak", n["proxy"] + ".bak"):
            if os.path.exists(f):
                os.remove(f)
        subprocess.run(["nginx", "-s", "reload"], capture_output=True)

    def test_entry_job(self):
        res = sd.job_cluster_entry(self.ctx, {"cluster": "sdjob"})
        self.assertEqual(res["url"], "http://127.0.0.1:%d/" % self.port)
        time.sleep(0.5)
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health/live" % self.port, timeout=5) as r:
            self.assertEqual(json.loads(r.read())["node"], "ok")
        sd.job_cluster_entry(self.ctx, {"cluster": "sdjob", "down": ["n|sight-report"]})
        self.assertTrue(self.store.data["clusters"]["sdjob"]["instances"][0]["down"])
        with open(sd.nginx_names("sdjob")["conf"]) as f:
            self.assertIn(" down;", f.read())
        info_ = sd.nginx_info(LocalRemote(), "sdjob")
        self.assertEqual([u["down"] for u in info_["upstream"]], [True])
        sd.job_cluster_entry(self.ctx, {"cluster": "sdjob", "up": ["n|sight-report"]})
        self.assertFalse(self.store.data["clusters"]["sdjob"]["instances"][0]["down"])


if __name__ == "__main__":
    unittest.main()
