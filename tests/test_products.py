# -*- coding: utf-8 -*-
"""产品描述与 DFDataLink 接入的单元测试（只用标准库，不连服务器）。

运行：python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sight_deploy as sd  # noqa: E402


def make_jar(path, manifest):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\n" +
                    "".join("%s: %s\r\n" % kv for kv in manifest.items()))
        zf.writestr("BOOT-INF/classes/app.txt", "x")


def make_zip(path, top, jar_name, manifest, extra=("install.sh", "start.sh", "README.txt")):
    d = tempfile.mkdtemp()
    try:
        jar = os.path.join(d, jar_name)
        make_jar(jar, manifest)
        with zipfile.ZipFile(path, "w") as zf:
            zf.write(jar, "%s/%s" % (top, jar_name))
            for f in extra:
                zf.writestr("%s/%s" % (top, f), "#!/bin/bash\n")
    finally:
        shutil.rmtree(d)


class PackageRecognition(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def load(self, path):
        pkg = sd.Package(path)
        self.addCleanup(pkg.cleanup)
        return pkg

    def test_three_products_zip(self):
        cases = [("sight-report", "sight-report-2.0.71.zip", "sight-report.jar", "com.sight.SightDataApplication", "2.0.71"),
                 ("sight-wall", "sight-wall-1.9.0.zip", "sight-wall.jar", "com.sight.SightWallApplication", "1.9.0"),
                 ("dfdatalink", "dfdatalink-0.2.0.zip", "dfdatalink.jar",
                  "com.dfdatalink.backend.DFDataLinkBackendApplication", "0.2.0")]
        for prod, zname, jar, sc, ver in cases:
            p = os.path.join(self.d, zname)
            make_zip(p, zname[:-4], jar, {"Product-Version": ver, "Java-Version": "21", "Start-Class": sc})
            pkg = self.load(p)
            self.assertEqual(pkg.product, prod)
            self.assertEqual(pkg.version, ver)
            self.assertEqual(pkg.java_req, 21)
            self.assertTrue(pkg.full)
            self.assertEqual(pkg.name, sd.PRODUCTS[prod]["name"])
            self.assertEqual(pkg.files[-1][0], jar)  # jar 最后传

    def test_single_jar_upgrade(self):
        p = os.path.join(self.d, "dfdatalink.jar")
        make_jar(p, {"Product-Version": "0.3.0", "Java-Version": "21",
                     "Start-Class": "com.dfdatalink.backend.DFDataLinkBackendApplication"})
        pkg = self.load(p)
        self.assertEqual((pkg.product, pkg.version, pkg.full), ("dfdatalink", "0.3.0", False))

    def test_version_from_file_name_when_manifest_lacks_it(self):
        p = os.path.join(self.d, "dfdatalink-0.4.1.zip")
        make_zip(p, "dfdatalink-0.4.1", "dfdatalink.jar", {"Start-Class": "x.DFDataLinkBackendApplication"})
        self.assertEqual(self.load(p).version, "0.4.1")

    def test_unknown_zip_rejected(self):
        p = os.path.join(self.d, "other-1.0.zip")
        make_zip(p, "other", "other-backend.jar", {"Product-Version": "1.0"})
        with self.assertRaises(sd.DeployError) as cm:
            sd.Package(p)
        self.assertIn("dfdatalink.jar", str(cm.exception))

    def test_other_jars_in_zip_ignored(self):
        # 包里另有驱动等 jar：只认产品 jar
        p = os.path.join(self.d, "dfdatalink-0.2.0.zip")
        make_zip(p, "dfdatalink-0.2.0", "dfdatalink.jar", {"Product-Version": "0.2.0"},
                 extra=("install.sh", "drivers/sight-x.jar"))
        self.assertEqual(self.load(p).product, "dfdatalink")

    def test_guess_pkg_and_zip_check(self):
        self.assertEqual(sd._guess_pkg("dfdatalink-0.2.0.zip"), ("dfdatalink", "0.2.0"))
        self.assertEqual(sd._guess_pkg("sight-report-2.0.71.zip"), ("sight-report", "2.0.71"))
        self.assertEqual(sd._guess_pkg("sight-wall.jar"), ("sight-wall", None))
        self.assertEqual(sd._guess_pkg("dfdatalink-backend-0.1.0.jar"), (None, None))
        p = os.path.join(self.d, "dfdatalink-0.2.0.zip")
        make_zip(p, "dfdatalink-0.2.0", "dfdatalink.jar", {"Product-Version": "0.2.0"})
        sd._check_zip_product(p, "dfdatalink")
        with self.assertRaises(sd.DeployError):
            sd._check_zip_product(p, "sight-report")


class Descriptors(unittest.TestCase):
    def test_required_keys(self):
        keys = set(sd.PRODUCTS["sight-report"])
        for k, v in sd.PRODUCTS.items():
            self.assertTrue(keys <= set(v), "%s 缺少 %s" % (k, keys - set(v)))

    def test_datalink(self):
        P = sd.PRODUCTS["dfdatalink"]
        self.assertEqual((P["name"], P["jar"], P["port"], P["dir"], P["service"]),
                         ("DFDataLink", "dfdatalink.jar", 18680, "/opt/dfdatalink", "dfdatalink"))
        self.assertEqual(P["health"], "/api/health")
        self.assertEqual(P["setup_path"], "/")
        self.assertTrue(P["setup_token"] and P["schema_versioned"])
        self.assertFalse(P["rolling_upgrade"])
        self.assertEqual(sd.SITE_PRODUCT_IDS["dfdatalink"], "dl-datalink")
        self.assertEqual(sd.product_by_jar("dfdatalink.jar"), "dfdatalink")
        self.assertEqual(sd.svc_of("dfdatalink"), "dfdatalink")

    def test_sight_unchanged(self):
        for k, port, site in (("sight-report", 9091, "dl-report"), ("sight-wall", 9090, "dl-wall")):
            P = sd.PRODUCTS[k]
            self.assertEqual((P["port"], P["jar"], P["dir"], P["service"], P["setup_path"]),
                             (port, k + ".jar", "/opt/" + k, k, "/setup"))
            self.assertEqual(sd.SITE_PRODUCT_IDS[k], site)
            self.assertIs(P["backup_excludes"], sd.BACKUP_EXCLUDES)
            self.assertTrue(P["rolling_upgrade"])
            self.assertFalse(P["schema_versioned"] or P["setup_token"])

    def test_datalink_ownership(self):
        class Pkg:
            product, jar = "dfdatalink", "dfdatalink.jar"
            files = [("start.sh", "", 1, ""), ("dfdatalink.jar", "", 1, "")]
        s = sd.copy_files_script(Pkg(), "/stg", "/opt/dfdatalink", "dl")
        self.assertNotIn("chown", s)  # 程序文件归 root
        Pkg.product, Pkg.jar = "sight-report", "sight-report.jar"
        Pkg.files = [("start.sh", "", 1, ""), ("sight-report.jar", "", 1, "")]
        self.assertIn("chown dl: /opt/dfdatalink/start.sh", sd.copy_files_script(Pkg(), "/stg", "/opt/dfdatalink", "dl"))

    def test_backup_excludes(self):
        self.assertIn("./data/cache*", sd.tar_excludes("sight-report"))
        dl = sd.tar_excludes("dfdatalink")
        self.assertNotIn("data/", dl)  # data/ 整个备份（向导配置、H2、驱动、受管文件）
        self.assertIn("./logs", dl)
        s = sd.svc_functions("dfdatalink", "/opt/dfdatalink", "root", True, "prod", {})
        self.assertNotIn("cache*", s)
        self.assertIn("SVC=dfdatalink", s)
        s = sd.svc_functions("sight-wall", "/opt/sight-wall", "root", True, "prod", {})
        self.assertIn('case "${f#data/}" in backups|cache*|temp) continue;; esac', s)


class DbDescription(unittest.TestCase):
    def test_properties_parsing(self):
        text = ("#DFDataLink setup wizard\n"
                "! comment\n"
                "DB_URL=jdbc\\:postgresql\\://10.0.0.8\\:5432/datalink?sslmode\\=require\n"
                "DB_USERNAME = dl\n"
                "DB_PASSWORD:p\\=a\\:s\\\\s\\u00e9\n"
                "FDL_X=a \\\n"
                "   b\n"
                "KEY\\ WITH\\ SPACE=v\n")
        p = sd.parse_java_properties(text)
        self.assertEqual(p["DB_URL"], "jdbc:postgresql://10.0.0.8:5432/datalink?sslmode=require")
        self.assertEqual(p["DB_USERNAME"], "dl")
        self.assertEqual(p["DB_PASSWORD"], "p=a:s\\sé")
        self.assertEqual(p["FDL_X"], "a b")
        self.assertEqual(p["KEY WITH SPACE"], "v")

    def test_datalink_sources(self):
        prop = ["DB_URL=jdbc\\:h2\\:file\\:/opt/dfdatalink/data/db/dfdatalink;AUTO_SERVER=FALSE"]
        d = sd.classify_db_datalink(prop, [], [], "/opt/dfdatalink")
        self.assertEqual(d["kind"], "h2")
        self.assertTrue(d["inside"])
        # 部署方的环境变量优先于向导文件
        d = sd.classify_db_datalink(prop, [], ["Environment=DB_URL=jdbc:postgresql://pg1:5432/dl"], "/opt/dfdatalink")
        self.assertEqual((d["kind"], d["url"]), ("external", "jdbc:postgresql://pg1:5432/dl"))
        self.assertIn("pg1", d["desc"])
        d = sd.classify_db_datalink(prop, ['DB_URL="jdbc:mysql://my1:3306/dl"'], [], "/opt/dfdatalink")
        self.assertEqual(d["url"], "jdbc:mysql://my1:3306/dl")
        # SPRING_DATASOURCE_URL / MR_DB_URL 不是 DFDataLink 的 DB_URL
        d = sd.classify_db_datalink([], [], ["Environment=MR_DB_URL=jdbc:mysql://x/y"], "/opt/dfdatalink")
        self.assertEqual(d["kind"], "none")
        self.assertIn("安装向导", d["desc"])

    def test_sight_db_unchanged(self):
        d = sd.classify_db('{"url":"jdbc:h2:file:./data/h2/sight"}', [], "/opt/sight-report")
        self.assertEqual((d["kind"], d["inside"]), ("h2", True))
        d = sd.classify_db(None, ["Environment=MR_DB_URL=jdbc:mysql://db1:3306/s"], "/opt/sight-report")
        self.assertEqual(d["desc"], "外部数据库 mysql @ db1")
        self.assertEqual(sd.classify_db(None, [], "/opt/x")["desc"], "尚未配置（还没走 /setup 向导）")

    def test_parse_pg_jdbc(self):
        self.assertEqual(sd.parse_pg_jdbc("jdbc:postgresql://pg:6432/dl?sslmode=require&x=1"),
                         {"host": "pg", "port": 6432, "db": "dl", "params": {"sslmode": "require", "x": "1"}})
        self.assertEqual(sd.parse_pg_jdbc("jdbc:postgresql://pg/dl")["port"], 5432)
        self.assertIsNone(sd.parse_pg_jdbc("jdbc:postgresql://a:5432,b:5432/dl"))
        self.assertIsNone(sd.parse_pg_jdbc("jdbc:mysql://a/dl"))


JOURNAL = """Oct 18 10:00:01 srv bash[901]: 2026-10-18 10:00:01.1 WARN 901 --- [main] c.d.setup.SetupMode :
Oct 18 10:00:01 srv bash[901]: ============================================================
Oct 18 10:00:01 srv bash[901]:  DataLink 尚未配置元数据库，已进入安装向导（setup mode）。
Oct 18 10:00:01 srv bash[901]:  浏览器打开 http://<本机地址>:18680/ 完成配置。
Oct 18 10:00:01 srv bash[901]:  初始化令牌：OLDtoken-aaaaaaaaaaaaaaaaaaaaaaaaaaa
Oct 18 10:05:12 srv bash[977]:  初始化令牌：\x1b[33mq8Zt3VfK1mWcR7xYpL2nB0sDuE9gHaJ4\x1b[0m
Oct 18 10:05:12 srv bash[977]:  配置将写入：/opt/dfdatalink/data/datalink.properties
"""


class SetupToken(unittest.TestCase):
    def test_extract_latest(self):
        self.assertEqual(sd.extract_setup_token(JOURNAL), ("q8Zt3VfK1mWcR7xYpL2nB0sDuE9gHaJ4", None))

    def test_extract_from_deployment(self):
        t, hint = sd.extract_setup_token(" 初始化令牌：（使用 FDL_SETUP_TOKEN 的值）\n")
        self.assertIsNone(t)
        self.assertEqual(hint, "令牌为部署时设置的 FDL_SETUP_TOKEN")

    def test_extract_none(self):
        self.assertEqual(sd.extract_setup_token("Started DFDataLinkBackendApplication\n"), (None, None))

    def test_mask(self):
        self.assertEqual(sd.mask_token("x 初始化令牌：abcDEF123 y\nz"), "x 初始化令牌：***\nz")
        self.assertNotIn("q8Zt3", sd._mask(JOURNAL))
        self.assertEqual(sd.mask_token(" 状态页：http://x:1/   令牌：KIUYOVzLfpNqvAaHW"), " 状态页：http://x:1/   令牌：***")

    def test_find_token_journal_then_files(self):
        calls = []

        class FakeR:
            def run(self, cmd, sudo=False, **kw):
                calls.append(cmd)
                if cmd.startswith("journalctl -u dfdatalink"):
                    return 0, "nothing here\n", ""
                if "logs/console.out" in cmd:
                    return 0, JOURNAL, ""
                return 1, "", ""
        tok, hint = sd.find_setup_token(FakeR(), "dfdatalink", "/opt/dfdatalink", True, tries=1)
        self.assertEqual(tok, "q8Zt3VfK1mWcR7xYpL2nB0sDuE9gHaJ4")
        self.assertTrue(any("journalctl -u dfdatalink -n 400 --no-pager" in c for c in calls))

    def test_token_not_in_local_log(self):
        buf = io.StringIO()
        old = sd.LOG.fh
        sd.LOG.fh = buf
        try:
            sd.info("初始化令牌：q8Zt3VfK1mWcR7xYpL2nB0sDuE9gHaJ4")
            sd.emit_token("q8Zt3VfK1mWcR7xYpL2nB0sDuE9gHaJ4")
        finally:
            sd.LOG.fh = old
        self.assertNotIn("q8Zt3", buf.getvalue())


class SchemaRollback(unittest.TestCase):
    def test_decision(self):
        D = sd.schema_rollback_decision
        self.assertEqual(D("external", "1.21", "1.21", False)[0], "ok")
        self.assertEqual(D("external", "1.21", "1.24", False)[0], "refuse")
        self.assertEqual(D("external", "1.21", "1.24", True)[0], "refuse")  # 外部库不在备份里，全量也不行
        self.assertEqual(D("external", None, "1.24", True)[0], "unknown")
        self.assertEqual(D("external", "1.21", None, True)[0], "unknown")
        self.assertEqual(D("h2", "1.21", "1.24", True)[0], "ok")    # H2 随 data/ 一起恢复
        self.assertEqual(D("h2", "1.21", "1.24", False)[0], "warn")
        self.assertEqual(D("none", None, None, False)[0], "ok")

    def _guard(self, meta, health, db_kind, full=False, product="dfdatalink"):
        class FakeR:
            def read_file(self, path, sudo=False):
                return json.dumps(meta) if path.endswith(".json") else None

            def health(self, port):
                return health
        inst = {"port": 18680, "db": {"kind": db_kind}}
        return sd.guard_schema_rollback(FakeR(), product, inst, "/opt/sight-backups/dfdatalink/x.tar.gz", full)

    def test_guard_refuses_external_mismatch(self):
        with self.assertRaises(sd.DeployError) as cm:
            self._guard({"schema_version": "1.21"}, {"status": "UP", "schemaVersion": "1.24"}, "external")
        self.assertIn("1.21", cm.exception.hint)

    def test_guard_allows(self):
        self._guard({"schema_version": "1.24"}, {"status": "UP", "schemaVersion": "1.24"}, "external")
        self._guard({"schema_version": "1.21"}, {"status": "UP", "schemaVersion": "1.24"}, "h2", full=True)
        # SightReport / SightWall 不核对
        self._guard({}, None, "external", product="sight-report")

    def test_guard_unknown_asks(self):
        asked = []
        old = sd.confirm
        sd.confirm = lambda *a, **k: asked.append(a[0]) or False
        try:
            with self.assertRaises(sd.UserAbort):
                self._guard({"schema_version": "1.21"}, None, "external")
        finally:
            sd.confirm = old
        self.assertEqual(len(asked), 1)


class FakeClock:
    """替代 sd.time：sleep 只推进假时钟。"""
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, n):
        self.t += n


class WaitingMode(unittest.TestCase):
    UP_WAIT = {"status": "UP", "mode": "waiting", "bootstrapped": True, "dbConfigured": True, "dbReachable": False}

    def test_descriptor_flag(self):
        self.assertTrue(sd.PRODUCTS["dfdatalink"]["waiting_mode"])
        self.assertFalse(sd.PRODUCTS["sight-report"]["waiting_mode"] or sd.PRODUCTS["sight-wall"]["waiting_mode"])
        self.assertEqual(sd.WAITING_FAIL_SECS, 60)

    def test_check(self):
        C, W = sd.waiting_mode_check, self.UP_WAIT
        self.assertEqual(C(W, None, 10, None), (None, False))          # 不启用（安装 / 其他产品）
        self.assertEqual(C(W, None, 10, 60), (10, False))              # 开始计时
        self.assertEqual(C(W, 10, 69, 60), (10, False))
        self.assertEqual(C(W, 10, 70, 60), (10, True))                 # 持续满 60 秒
        self.assertEqual(C(dict(W, mode="normal"), 10, 70, 60), (None, False))  # 中途恢复：重新计时
        self.assertEqual(C(None, 10, 70, 60), (None, False))           # 没响应不算等待模式
        self.assertEqual(C(dict(W, status="DOWN"), 10, 70, 60), (None, False))

    def test_sanitize(self):
        S = sd.sanitize_db_error
        t = S("FATAL: password authentication failed for user \"fdl\"; url=jdbc:postgresql://fdl:S3cret!@db:5432/fdl?password=S3cret!&ssl=true")
        self.assertNotIn("S3cret", t)
        self.assertIn("password authentication failed", t)
        self.assertIn("jdbc:postgresql://fdl:***@db", t)
        self.assertNotIn("S3cret", S("DB_PASSWORD=S3cret! pwd: S3cret!"))
        self.assertIn("using password: YES)", S("Access denied for user 'fdl'@'10.0.0.5' (using password: YES)"))
        self.assertEqual(S(None), "")
        self.assertTrue(S("x" * 1000).endswith(" …") and len(S("x" * 1000)) < 310)
        self.assertNotIn("\n", S("a\nb"))

    def test_failure_lines(self):
        L = sd.waiting_failure_lines(dict(self.UP_WAIT, lastError="Connection refused (password=abc)"), 61)
        self.assertIn("等待模式", L[0])
        self.assertIn("1:01", L[0])
        self.assertEqual(len(L), 2)
        self.assertNotIn("abc", L[1])
        self.assertEqual(len(sd.waiting_failure_lines(self.UP_WAIT, 61)), 1)

    def _wait(self, healths, limit, need_boot=True):
        """用假时钟跑 wait_healthy；healths(秒) → 健康检查结果。返回 (good, h, 用时, 失败信息)。"""
        clock = FakeClock()

        class FakeR:
            def health(self, port):
                return healths(clock.t - 1000.0)

            def run(self, cmd, sudo=False, **k):
                return 0, "alive\n", ""
        msgs = []
        saved = sd.time, sd.fail, sd.info, sd.ok, sd.warn
        sd.time = clock
        sd.fail, sd.info = (lambda m: msgs.append(("fail", m))), (lambda m: msgs.append(("info", m)))
        sd.ok = sd.warn = lambda m: None
        try:
            good, h = sd.wait_healthy(FakeR(), "dfdatalink", "/opt/dfdatalink", 18680, need_boot, 300, False,
                                      waiting_limit=limit)
        finally:
            sd.time, sd.fail, sd.info, sd.ok, sd.warn = saved
        return good, h, clock.t - 1000.0, msgs

    def test_upgrade_waiting_fails_after_limit(self):
        err = "Connection to db:5432 refused. url=jdbc:postgresql://fdl:pw123@db:5432/fdl"
        good, h, el, msgs = self._wait(
            lambda t: None if t < 20 else dict(self.UP_WAIT, lastError=err), 60)
        self.assertFalse(good)
        self.assertEqual(h["mode"], "waiting")
        self.assertTrue(80 <= el < 90, el)                       # 20 秒起动 + 60 秒等待模式，远早于 300 秒
        self.assertEqual(msgs[0][0], "fail")
        self.assertIn("连不上元数据库", msgs[0][1])
        self.assertIn("最近一次连接错误", msgs[1][1])
        self.assertNotIn("pw123", "".join(m for _, m in msgs))

    def test_upgrade_waiting_recovers(self):
        good, h, el, _ = self._wait(
            lambda t: dict(self.UP_WAIT) if t < 40 else {"status": "UP", "mode": "normal", "bootstrapped": True}, 60)
        self.assertTrue(good)
        self.assertEqual(h["mode"], "normal")

    def test_upgrade_waiting_not_bootstrapped_still_times_out_early(self):
        good, _, el, _ = self._wait(lambda t: dict(self.UP_WAIT, bootstrapped=False), 60)
        self.assertFalse(good)
        self.assertLess(el, 70)

    def test_install_waiting_is_ok(self):
        good, h, el, _ = self._wait(lambda t: dict(self.UP_WAIT, bootstrapped=False), None, need_boot=False)
        self.assertTrue(good)
        self.assertEqual(h["mode"], "waiting")
        self.assertLess(el, 1)


class DbDumpPrune(unittest.TestCase):
    def test_name(self):
        self.assertEqual(sd.db_dump_name("dfdatalink", "0.2.0-rc 1", "20261018-101500"),
                         "dfdatalink-db-0.2.0_rc_1-20261018-101500.dump")
        # 本工具自己起的名字一定能被清理逻辑认出来
        n = sd.db_dump_name("dfdatalink", "0.2.0-rc 1", "20261018-101500")
        self.assertEqual(sd.db_dumps_to_prune([n], "dfdatalink", 0), [n])

    def test_select(self):
        P = sd.db_dumps_to_prune
        names = ["dfdatalink-db-0.1.8-20261001-091000.dump", "dfdatalink-db-0.1.9-20261012-150200.dump",
                 "dfdatalink-db-0.2.0-20261018-101500.dump", "dfdatalink-db-0.1.7-20260901-080000.dump",
                 # 不是本工具的命名，一律不动
                 "dfdatalink-db-0.2.1-20261019-101500.dump.tmp", "dfdatalink-0.1.9-20261012-150200.tar.gz",
                 "dfdatalink-db-0.2.0-20261018-101500.dump.json", "my-dfdatalink-db-0.1-20200101-000000.dump",
                 "dfdatalink-db-手工-20200101-000000.dump", "dfdatalink-db-x-2020.dump", "sight-report-db-1-20200101-000000.dump",
                 "dfdatalink-db-../x-20200101-000000.dump"]
        self.assertEqual(P(names, "dfdatalink", 3),
                         ["dfdatalink-db-0.1.7-20260901-080000.dump"])
        self.assertEqual(P(names, "dfdatalink", 1),
                         ["dfdatalink-db-0.1.9-20261012-150200.dump", "dfdatalink-db-0.1.8-20261001-091000.dump",
                          "dfdatalink-db-0.1.7-20260901-080000.dump"])
        # 刚做的那份总是保留（带路径也行）
        self.assertNotIn("dfdatalink-db-0.1.7-20260901-080000.dump",
                         P(names, "dfdatalink", 1, keep="/opt/sight-backups/dfdatalink/dfdatalink-db-0.1.7-20260901-080000.dump"))
        self.assertEqual(P(names, "dfdatalink", 10), [])
        self.assertEqual(P([], "dfdatalink", 3), [])

    def test_prune_remote(self):
        class FakeR:
            def __init__(self):
                self.cmds = []

            def run(self, cmd, sudo=False, **k):
                self.cmds.append(cmd)
                if cmd.startswith("for f in"):
                    return 0, "dfdatalink-db-0.1.8-20261001-091000.dump\ndfdatalink-db-0.1.9-20261012-150200.dump\n" \
                              "dfdatalink-db-0.2.0-20261018-101500.dump\nnotes.txt\n", ""
                return 0, "", ""
        r = FakeR()
        saved = sd.info
        sd.info = lambda m: None
        try:
            sd.prune_db_dumps(r, "/data/sight backups/dfdatalink", "dfdatalink", 2,
                              keep="/data/sight backups/dfdatalink/dfdatalink-db-0.2.0-20261018-101500.dump")
        finally:
            sd.info = saved
        self.assertIn("'/data/sight backups/dfdatalink'/dfdatalink-db-*.dump", r.cmds[0])
        self.assertEqual(r.cmds[1], "rm -f -- '/data/sight backups/dfdatalink/dfdatalink-db-0.1.8-20261001-091000.dump'")


if __name__ == "__main__":
    unittest.main()
