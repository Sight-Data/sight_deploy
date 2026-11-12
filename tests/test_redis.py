# -*- coding: utf-8 -*-
"""集群的 Redis 检查与同机本机 Redis 的配置脚本（不连服务器：脚本在本机 bash 里跑）。

见 sight-data 仓库 docs/集群部署与集群入口-设计.md §8.3 第 2 步与 §17。运行：python3 -m unittest discover -s tests -v
本机有 redis-server 时用真实的 Redis 验证；没有就只跑假 Redis（RESP 子集）与纯脚本部分。
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sight_deploy as sd  # noqa: E402

SERVERS = [b for b in (shutil.which("redis-server"), shutil.which("valkey-server")) if b]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class LocalRemote:
    """用本机 bash 代替 SSH，只实现 redis_check 用到的 run_raw。"""

    def run_raw(self, cmd, stdin=None, pty=False, timeout=600, on_line=None, wait_for=None, cancellable=False,
                secret_out=False):
        p = subprocess.run(["bash", "-c", cmd], input=(stdin or "").encode(), capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode(), p.stderr.decode()


class FakeRedis(threading.Thread):
    """只认 AUTH / PING / INFO / QUIT 的假 Redis。password=None 表示没设密码；reply 可整体覆盖（模拟不是 Redis）。"""

    def __init__(self, password=None, raw_reply=None, protected=False):
        super().__init__(daemon=True)
        self.password, self.raw_reply, self.protected = password, raw_reply, protected
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]

    def run(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(c,), daemon=True).start()

    def handle(self, c):
        try:
            if self.raw_reply is not None:
                c.recv(4096)
                c.sendall(self.raw_reply)
                c.close()
                return
            if self.protected:
                c.recv(4096)
                c.sendall(b"-DENIED Redis is running in protected mode because protected mode is enabled\r\n")
                c.close()
                return
            data = b""
            c.settimeout(3)
            while b"QUIT" not in data:
                chunk = c.recv(4096)
                if not chunk:
                    break
                data += chunk
            authed = self.password is None
            out = b""
            for cmd in parse_resp(data):
                name = cmd[0].upper()
                if name == "AUTH":
                    if self.password is None:
                        out += b"-ERR AUTH <password> called without any password configured for the default user.\r\n"
                    elif cmd[-1] == self.password:
                        authed = True
                        out += b"+OK\r\n"
                    else:
                        out += b"-WRONGPASS invalid username-password pair or user is disabled.\r\n"
                elif not authed and name != "QUIT":
                    out += b"-NOAUTH Authentication required.\r\n"
                elif name == "PING":
                    out += b"+PONG\r\n"
                elif name == "INFO":
                    body = "# Server\r\nredis_version:7.0.15\r\nredis_mode:standalone\r\n"
                    out += ("$%d\r\n%s\r\n" % (len(body), body)).encode()
                elif name == "QUIT":
                    out += b"+OK\r\n"
            c.sendall(out)
            c.close()
        except OSError:
            pass


def parse_resp(data):
    cmds, parts, i = [], data.split(b"\r\n"), 0
    while i < len(parts):
        if parts[i].startswith(b"*"):
            n = int(parts[i][1:])
            args = []
            i += 1
            for _ in range(n):
                args.append(parts[i + 1].decode())
                i += 2
            cmds.append(args)
        else:
            i += 1
    return cmds


class Addr(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(sd.parse_redis_addr("10.0.0.20"), ("10.0.0.20", 6379))
        self.assertEqual(sd.parse_redis_addr(" redis.corp:6380 "), ("redis.corp", 6380))
        for bad in ("", ":6379", "a b", "h:0", "h:70000", "h:x", "a;rm -rf /", "$(x)"):
            with self.assertRaises(sd.DeployError, msg=bad):
                sd.parse_redis_addr(bad)

    def test_script_rejects_injection(self):
        with self.assertRaises(sd.DeployError):
            sd.redis_check_script("x; rm -rf /", 6379)
        with self.assertRaises(sd.DeployError):
            sd.redis_check_script("127.0.0.1", "6379; id")


class CheckAgainstFake(unittest.TestCase):
    def check(self, fake, pw=""):
        fake.start()
        self.addCleanup(fake.srv.close)
        return sd.redis_check(LocalRemote(), "127.0.0.1", fake.port, pw)

    def test_ok_with_password(self):
        r = self.check(FakeRedis("s3cret"), "s3cret")
        self.assertEqual((r["state"], r["ok"], r["version"]), ("OK", True, "7.0.15"))
        self.assertFalse(r["nopass"])

    def test_special_characters_in_password(self):
        pw = "a'b\"c $x`y\\z;&|"
        self.assertTrue(self.check(FakeRedis(pw), pw)["ok"])

    def test_no_password_server(self):
        r = self.check(FakeRedis(None), "")
        self.assertTrue(r["ok"])

    def test_password_given_but_server_has_none(self):
        r = self.check(FakeRedis(None), "whatever")
        self.assertTrue(r["ok"])
        self.assertTrue(r["nopass"], "要提示「没设密码」")

    def test_needs_password(self):
        r = self.check(FakeRedis("pw"), "")
        self.assertEqual((r["state"], r["ok"]), ("NOAUTH", False))
        self.assertTrue(r["hint"])

    def test_wrong_password(self):
        r = self.check(FakeRedis("pw"), "nope")
        self.assertEqual((r["state"], r["ok"]), ("WRONGPASS", False))

    def test_protected_mode(self):
        r = self.check(FakeRedis(protected=True), "")
        self.assertEqual(r["state"], "PROTECTED")

    def test_not_redis(self):
        r = self.check(FakeRedis(raw_reply=b"HTTP/1.1 400 Bad Request\r\n\r\n"), "")
        self.assertEqual((r["state"], r["ok"]), ("BADREPLY", False))

    def test_refused(self):
        r = sd.redis_check(LocalRemote(), "127.0.0.1", free_port(), "")
        self.assertEqual((r["state"], r["ok"]), ("NOCONN", False))

    def test_tls_only_checks_port(self):
        f = FakeRedis("pw")
        f.start()
        self.addCleanup(f.srv.close)
        r = sd.redis_check(LocalRemote(), "127.0.0.1", f.port, "", tls=True)
        self.assertEqual((r["state"], r["ok"]), ("TCPONLY", True))

    def test_password_not_in_command_line(self):
        seen = {}

        class Spy(LocalRemote):
            def run_raw(self, cmd, stdin=None, **kw):
                seen["cmd"], seen["stdin"] = cmd, stdin
                return super().run_raw(cmd, stdin=stdin, **kw)

        f = FakeRedis("TopSecret123")
        f.start()
        self.addCleanup(f.srv.close)
        self.assertTrue(sd.redis_check(Spy(), "127.0.0.1", f.port, "TopSecret123")["ok"])
        self.assertNotIn("TopSecret123", seen["cmd"])
        self.assertEqual(seen["stdin"], "TopSecret123\n")


@unittest.skipUnless(SERVERS, "本机没有 redis-server / valkey-server")
class RealRedis(unittest.TestCase):
    """真 Redis：检查脚本 + 本机 Redis 配置脚本（追加的配置块要真的生效）。"""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.terminate()
            p.wait(5)

    def start(self, conf, server=None):
        p = subprocess.Popen([server or SERVERS[0], conf], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=self.d)
        self.procs.append(p)
        return p

    def wait_up(self, port):
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        self.fail("redis 没起来")

    def apply(self, conf, port, pw, bind="127.0.0.1"):
        seed = os.path.join(self.d, "seed")
        with open(seed, "w") as f:
            f.write(pw + "\n")
        os.chmod(seed, 0o600)
        p = subprocess.run(["bash", "-c", sd.redis_apply_script(conf, seed, port, bind)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(os.path.exists(seed), "种子文件用完即删")

    def test_apply_block_takes_effect_and_is_idempotent(self):
        for server in SERVERS:
            with self.subTest(server=os.path.basename(server)):
                self.apply_and_check(server)

    def apply_and_check(self, server):
        port = free_port()
        conf = os.path.join(self.d, "redis-%d.conf" % port)
        with open(conf, "w") as f:
            f.write("bind 0.0.0.0\nprotected-mode no\nport 7777\nappendonly no\nrequirepass oldpass\ndir %s\n" % self.d)
        self.apply(conf, port, "first-pass")
        self.apply(conf, port, "second-pass")
        with open(conf) as f:
            text = f.read()
        self.assertEqual(text.count(sd.REDIS_BLOCK_BEGIN), 1, "重复执行只保留一段")
        self.assertIn("requirepass oldpass", text, "原有内容不动")
        with open(conf + ".sight.bak") as f:
            bak = f.read()
        self.assertIn("requirepass oldpass", bak, "备份是改动前的原始配置")
        self.assertNotIn(sd.REDIS_BLOCK_BEGIN, bak, "备份里没有托管配置块")
        self.start(conf, server)
        self.wait_up(port)
        rem = LocalRemote()
        self.assertEqual(sd.redis_check(rem, "127.0.0.1", port, "second-pass")["state"], "OK")
        self.assertEqual(sd.redis_check(rem, "127.0.0.1", port, "first-pass")["state"], "WRONGPASS")
        self.assertEqual(sd.redis_check(rem, "127.0.0.1", port, "")["state"], "NOAUTH")
        self.assertTrue(sd.redis_check(rem, "127.0.0.1", port, "second-pass")["version"][0].isdigit())

    def test_multi_address_bind(self):
        """多机集群：监听 127.0.0.1 与内网地址（这里用回环的 127.0.0.2 代替）；没列出的地址连不上。"""
        for server in SERVERS:
            with self.subTest(server=os.path.basename(server)):
                port = free_port()
                conf = os.path.join(self.d, "multi-%d.conf" % port)
                with open(conf, "w") as f:
                    f.write("bind 0.0.0.0\ndir %s\nsave \"\"\n" % self.d)
                self.apply(conf, port, "pw1", bind="127.0.0.1 127.0.0.2")
                self.start(conf, server)
                self.wait_up(port)
                rem = LocalRemote()
                self.assertTrue(sd.redis_check(rem, "127.0.0.1", port, "pw1")["ok"])
                self.assertTrue(sd.redis_check(rem, "127.0.0.2", port, "pw1")["ok"])
                self.assertEqual(sd.redis_check(rem, "127.0.0.3", port, "pw1")["state"], "NOCONN", "没列出的地址不监听")

    def test_real_redis_without_password(self):
        port = free_port()
        conf = os.path.join(self.d, "plain.conf")
        with open(conf, "w") as f:
            f.write("bind 127.0.0.1\nport %d\nsave \"\"\ndir %s\n" % (port, self.d))
        self.start(conf)
        self.wait_up(port)
        r = sd.redis_check(LocalRemote(), "127.0.0.1", port, "")
        self.assertEqual((r["state"], r["ok"]), ("OK", True))


class Scripts(unittest.TestCase):
    def test_scripts_parse(self):
        for name, body in (("check", sd.redis_check_script("10.0.0.1", 6379)),
                           ("probe", sd.REDIS_PROBE_SH.replace("@PORT@", "6379")),
                           ("apply", sd.redis_apply_script("/etc/redis/redis.conf", "/tmp/.seed", 6379)),
                           ("valkey-install", sd.BUNDLED_VALKEY_INSTALL_SH % {"tgz": "/tmp/x.tgz", "dir": "/opt/sight-valkey"})):
            p = subprocess.run(["bash", "-n"], input=body.encode(), capture_output=True)
            self.assertEqual(p.returncode, 0, "%s: %s" % (name, p.stderr.decode()))

    def test_apply_only_listens_locally(self):
        s = sd.redis_apply_script("/etc/redis/redis.conf", "/tmp/.seed", 6379)
        self.assertIn("'bind 127.0.0.1'", s)
        self.assertIn("protected-mode yes", s)
        self.assertNotIn("0.0.0.0", s)

    def test_bind_must_be_ipv4_list(self):
        self.assertIn("'bind 127.0.0.1 10.0.0.20'", sd.redis_apply_script("/c", "/s", 6379, "127.0.0.1 10.0.0.20"))
        for bad in ("0.0.0.0 ; rm -rf /", "", "::1", "host.name", "10.0.0.1\necho x"):
            with self.assertRaises(sd.DeployError, msg=bad):
                sd.redis_apply_script("/c", "/s", 6379, bad)

    def test_fw_script_only_for_known_firewalls_and_ipv4_sources(self):
        self.assertIsNone(sd.redis_fw_script(None, 6379, ["10.0.0.1"]))
        self.assertIsNone(sd.redis_fw_script("firewalld", 6379, []))
        self.assertIsNone(sd.redis_fw_script("firewalld", 6379, ["a.example.com"]))
        f = sd.redis_fw_script("firewalld", 6379, ["10.0.0.1", "10.0.0.2"])
        self.assertIn("source address=$S port port=$P", f)
        self.assertIn("10.0.0.1 10.0.0.2", f)
        self.assertNotIn("--add-port", f, "不能对所有来源开放端口")
        self.assertEqual(subprocess.run(["bash", "-n"], input=f.encode()).returncode, 0)
        u = sd.redis_fw_script("ufw", 6379, ["10.0.0.1"])
        self.assertIn('ufw allow from "$S" to any port', u)
        self.assertEqual(subprocess.run(["bash", "-n"], input=u.encode()).returncode, 0)

    def test_probe_output_collects_ips(self):
        d = sd.parse_kv_lines("bin=/usr/bin/valkey-server\nip=10.0.0.5\nip=172.17.0.1\nfw=firewalld\narch=x86_64\n")
        self.assertEqual(d["ips"], ["10.0.0.5", "172.17.0.1"])
        self.assertEqual((d["fw"], d["arch"]), ("firewalld", "x86_64"))

    def test_parse_kv(self):
        d = sd.parse_kv_lines("bin=/usr/bin/redis-server\nlisten=0\nunit=redis-server\nnoise\nsystemd=1\n")
        self.assertEqual(d, {"bin": "/usr/bin/redis-server", "listen": "0", "unit": "redis-server", "systemd": "1"})


class ClusterRecord(unittest.TestCase):
    def test_install_unknown_host_rejected(self):
        class Ctx:
            class store:
                lock = threading.RLock()

                @staticmethod
                def snapshot():
                    return {"hosts": {"a": {"host": "10.0.0.1"}}, "clusters": {"c": {"product": "sight-report", "instances": [
                        {"host": "a", "product": "sight-report", "port": 9091}]}}}

        with self.assertRaises(sd.DeployError) as cm:
            sd.job_cluster_redis_install(Ctx, {"cluster": "c", "host": "nope"})
        self.assertIn("不存在", str(cm.exception))

    def test_install_empty_cluster_rejected(self):
        class Ctx:
            class store:
                lock = threading.RLock()

                @staticmethod
                def snapshot():
                    return {"hosts": {}, "clusters": {"c": {"product": "sight-report", "instances": []}}}

        with self.assertRaises(sd.DeployError):
            sd.job_cluster_redis_install(Ctx, {"cluster": "c"})


def make_valkey_pkg(d, name="sight-valkey-9.0.7.1-linux-amd64.tar.gz", machine=0x3E, extra=None, drop=None, magic=b"\x7fELF"):
    import io
    import tarfile
    elf = magic + bytes([2, 1, 1]) + bytes(9) + (b"\x03\x00") + machine.to_bytes(2, "little") + bytes(40)
    files = {"sight-valkey/bin/valkey-server": elf, "sight-valkey/bin/valkey-cli": elf,
             "sight-valkey/conf/valkey.conf.default": b"dir /var/lib/sight-valkey\n",
             "sight-valkey/conf/sight-valkey.service": b"[Unit]\n", "sight-valkey/licenses/NOTICE": b"x",
             "sight-valkey/VERSION": b"package=9.0.7.1\nvalkey=9.0.7\narch=amd64\n"}
    for k in (drop or ()):
        files.pop(k)
    files.update(extra or {})
    path = os.path.join(d, name)
    with tarfile.open(path, "w:gz") as tf:
        for n, data in files.items():
            ti = tarfile.TarInfo(n)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return path


class ValkeyPackage(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_ok(self):
        m = sd.inspect_valkey_pkg(make_valkey_pkg(self.d), "amd64")
        self.assertEqual((m["version"], m["valkey"], m["arch"]), ("9.0.7.1", "9.0.7", "amd64"))

    def test_wrong_arch_for_server(self):
        with self.assertRaises(sd.DeployError) as cm:
            sd.inspect_valkey_pkg(make_valkey_pkg(self.d), "arm64")
        self.assertIn("ARM64", str(cm.exception))

    def test_renamed_package_detected_by_elf_header(self):
        p = make_valkey_pkg(self.d, name="sight-valkey-9.0.7.1-linux-arm64.tar.gz", machine=0x3E)
        with self.assertRaises(sd.DeployError) as cm:
            sd.inspect_valkey_pkg(p, "arm64")
        self.assertIn("架构", str(cm.exception))

    def test_not_elf(self):
        with self.assertRaises(sd.DeployError):
            sd.inspect_valkey_pkg(make_valkey_pkg(self.d, magic=b"#!/b"), "amd64")

    def test_incomplete(self):
        with self.assertRaises(sd.DeployError) as cm:
            sd.inspect_valkey_pkg(make_valkey_pkg(self.d, drop=["sight-valkey/conf/sight-valkey.service"]), "amd64")
        self.assertIn("不完整", str(cm.exception))

    def test_path_traversal_and_foreign_paths(self):
        for bad in ("../evil", "/etc/passwd", "other/x"):
            with self.subTest(bad=bad):
                with self.assertRaises(sd.DeployError):
                    sd.inspect_valkey_pkg(make_valkey_pkg(self.d, extra={bad: b"x"}), "amd64")

    def test_bad_name(self):
        p = os.path.join(self.d, "valkey.tar.gz")
        shutil.copy(make_valkey_pkg(self.d), p)
        with self.assertRaises(sd.DeployError):
            sd.inspect_valkey_pkg(p)

    def test_arch_mapping(self):
        self.assertEqual(sd.valkey_pkg_arch("x86_64"), "amd64")
        self.assertEqual(sd.valkey_pkg_arch("aarch64"), "arm64")
        self.assertIsNone(sd.valkey_pkg_arch("loongarch64"))


class FakeStore:
    def __init__(self, clusters):
        self.lock = threading.RLock()
        self.data = {"clusters": clusters, "hosts": {}}
        self.saved = 0

    def snapshot(self):
        import json
        return json.loads(json.dumps(self.data))

    def save(self):
        self.saved += 1


class CheckJob(unittest.TestCase):
    """job_cluster_redis_check / 扩容前检查：问地址 → 问密码 → 每台服务器检查 → 不通就报错。"""

    def setUp(self):
        self.fake = FakeRedis("pw")
        self.fake.start()
        self.addCleanup(self.fake.srv.close)
        self.opened = []
        self.answers = {"addr": "127.0.0.1:%d" % self.fake.port, "pw": "pw"}
        self.saved = {n: getattr(sd, n) for n in ("open_host", "ask", "ask_secret_optional", "step", "ok", "warn", "info")}

        class R(LocalRemote):
            def close(self_):
                pass

        sd.open_host = lambda store, hn: (self.opened.append(hn), R())[1]
        sd.ask = lambda *a, **k: self.answers["addr"]
        sd.ask_secret_optional = lambda *a, **k: self.answers["pw"]
        for n in ("step", "ok", "warn", "info"):
            setattr(sd, n, lambda *a, **k: None)
        self.addCleanup(lambda: [setattr(sd, k, v) for k, v in self.saved.items()])
        inst = [{"host": "a", "product": "sight-report", "port": 9091}, {"host": "b", "product": "sight-report", "port": 9091},
                {"host": "a", "product": "sight-report@b", "port": 9092}]
        self.ctx = type("C", (), {})()
        self.ctx.store = FakeStore({"c": {"product": "sight-report", "instances": inst}})

    def test_asks_address_saves_it_and_checks_each_host_once(self):
        res = sd.job_cluster_redis_check(self.ctx, {"cluster": "c"})
        self.assertEqual(self.opened, ["a", "b"], "同一台上的多个实例只检查一次")
        self.assertTrue(all(x["ok"] for x in res["results"]))
        self.assertEqual(self.ctx.store.data["clusters"]["c"]["redis"],
                         {"host": "127.0.0.1", "port": self.fake.port, "tls": False})
        self.assertNotIn("pw", str(self.ctx.store.data), "密码不落盘")

    def test_remembered_address_is_not_asked_again(self):
        self.ctx.store.data["clusters"]["c"]["redis"] = {"host": "127.0.0.1", "port": self.fake.port, "tls": False}
        sd.ask = lambda *a, **k: self.fail("不该再问地址")
        self.assertEqual(sd.job_cluster_redis_check(self.ctx, {"cluster": "c"})["redis"]["host"], "127.0.0.1")

    def test_wrong_password_fails_with_hint(self):
        self.answers["pw"] = "bad"
        with self.assertRaises(sd.DeployError) as cm:
            sd.job_cluster_redis_check(self.ctx, {"cluster": "c"})
        self.assertIn("连不上 Redis", str(cm.exception))
        self.assertIn("密码", cm.exception.hint)

    def test_skip_when_no_address(self):
        self.answers["addr"] = ""
        tg, res = sd.redis_preflight(self.ctx, "c", self.ctx.store.data["clusters"]["c"], ["a"])
        self.assertEqual((tg, res), (None, None))
        self.assertEqual(self.opened, [])
        self.assertNotIn("redis", self.ctx.store.data["clusters"]["c"])


class SaveRecord(unittest.TestCase):
    """保存集群时的 redis 字段：带地址就校验并记下，带空串就清掉，没带（老页面）就沿用。"""

    def store(self):
        st = FakeStore({})
        st.data["hosts"] = {"a": {"host": "10.0.0.1", "products": {"sight-report": {"port": 9091, "db_fp": "f1"}}}}
        return st

    def body(self, **kw):
        b = {"name": "prod", "product": "sight-report", "instances": [{"host": "a", "product": "sight-report"}]}
        b.update(kw)
        return b

    def test_redis_field_lifecycle(self):
        st = self.store()
        sd.save_cluster(st, self.body(redis="10.0.0.20:6380"))
        self.assertEqual(st.data["clusters"]["prod"]["redis"], {"host": "10.0.0.20", "port": 6380, "tls": False})
        sd.save_cluster(st, self.body(old_name="prod"))
        self.assertEqual(st.data["clusters"]["prod"]["redis"]["host"], "10.0.0.20", "没带这个字段：沿用")
        sd.save_cluster(st, self.body(old_name="prod", redis=""))
        self.assertIsNone(st.data["clusters"]["prod"]["redis"], "带空串：清掉")

    def test_bad_address_rejected(self):
        with self.assertRaises(sd.DeployError):
            sd.save_cluster(self.store(), self.body(redis="a b:99999"))


if __name__ == "__main__":
    unittest.main()
