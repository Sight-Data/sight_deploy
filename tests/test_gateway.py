# -*- coding: utf-8 -*-
"""Sight 移动网关（sight-mobile-gateway）接入的单元测试（只用标准库，不连服务器）。

覆盖：安装包识别、CPU 架构匹配、init / show 输出解析、升级 / 回滚的命令序列（并用假的 systemctl 和假程序
在本机临时目录里真正跑一遍脚本）、版本配套提醒、令牌不进日志、官网下载项。

运行：python3 -m unittest discover -s tests -v
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sight_deploy as sd  # noqa: E402

TOKEN = "dPGMGLJ7eO9wI5f5dPx3lsf6YZ51T-EuvmaDdtkU6-E"
FP = "BA:DE:71:83:A3:59:EF:D4:8F:19:1B:9A:F0:B6:D3:7E:ED:E7:F3:8A:77:22:A2:64:AE:09:37:39:29:4D:B5:D2"
# 网关 `init` 的真实输出（main.go 的 cmdInit / printSecrets）
INIT_OUT = """已生成配置与证书：
  配置文件: /opt/sight-mobile-gateway/gateway.yaml
  隧道证书: /opt/sight-mobile-gateway/data/tunnel.crt

请复制到 SightReport「网络与部署」页：
  证书指纹 (SHA-256): %s
  接入令牌          : %s

下一步：把以上两项填到 SightReport「网络与部署」页，然后执行 install 与 start。
""" % (FP, TOKEN)
SHOW_OUT = "\n请复制到 SightReport「网络与部署」页：\n  证书指纹 (SHA-256): %s\n  接入令牌          : %s\n" % (FP, TOKEN)
SHOW_NOTLS = "隧道未启用 TLS（tunnel.tls: false），无证书指纹。\n  接入令牌: %s\n" % TOKEN

ELF_MACHINE = {"amd64": 0x3E, "arm64": 0xB7, "loong64": 0x102}


def fake_elf(machine, body=b"x" * 64):
    """最小的 ELF 头：魔数 + 小端 + e_machine。"""
    h = bytearray(b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8))  # 16 字节 e_ident
    h += (2).to_bytes(2, "little") + machine.to_bytes(2, "little")
    return bytes(h) + body


def make_gw_zip(path, version, arch, machine=None, binary=None, extra=None, top=None):
    top = top or "sight-mobile-gateway-%s-linux-%s" % (version, arch)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(top + "/", "")
        zf.writestr(top + "/README.md", "# readme\n")
        zf.writestr(top + "/" + sd.GATEWAY_BIN, fake_elf(ELF_MACHINE[arch] if machine is None else machine) if binary is None else binary)
        for n, c in (extra or {}).items():
            zf.writestr(top + "/" + n, c)


class PackageRecognition(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def load(self, p):
        pkg = sd.Package(p)
        self.addCleanup(pkg.cleanup)
        return pkg

    def test_three_arches(self):
        for arch in ("amd64", "arm64", "loong64"):
            p = os.path.join(self.d, "sight-mobile-gateway-2.0.96-linux-%s.zip" % arch)
            make_gw_zip(p, "2.0.96", arch)
            pkg = self.load(p)
            self.assertEqual((pkg.product, pkg.version, pkg.arch, pkg.full, pkg.java_req), (sd.GATEWAY, "2.0.96", arch, True, 0))
            self.assertEqual(pkg.name, "Sight 移动网关")
            self.assertEqual([f[0] for f in pkg.files], ["README.md", sd.GATEWAY_BIN])  # 可执行文件最后传
            self.assertEqual(pkg.jar, sd.GATEWAY_BIN)

    def test_only_binary_and_readme_are_taken(self):
        """zip 里就算夹带了配置文件，也不会被上传（gateway.yaml / data 绝不能被覆盖）。"""
        p = os.path.join(self.d, "sight-mobile-gateway-2.0.96-linux-amd64.zip")
        make_gw_zip(p, "2.0.96", "amd64", extra={"gateway.yaml": "token: evil\n", "data/tunnel.key": "k"})
        pkg = self.load(p)
        self.assertEqual(sorted(f[0] for f in pkg.files), ["README.md", sd.GATEWAY_BIN])

    def test_elf_must_match_name(self):
        p = os.path.join(self.d, "sight-mobile-gateway-2.0.96-linux-amd64.zip")
        make_gw_zip(p, "2.0.96", "amd64", machine=0xB7)  # 名字说 amd64，里面是 arm64
        with self.assertRaises(sd.DeployError) as cm:
            sd.Package(p)
        self.assertIn("arm64", str(cm.exception))

    def test_not_elf_rejected(self):
        p = os.path.join(self.d, "sight-mobile-gateway-2.0.96-linux-amd64.zip")
        make_gw_zip(p, "2.0.96", "amd64", binary=b"MZ this is windows")
        with self.assertRaises(sd.DeployError):
            sd.Package(p)

    def test_windows_and_bad_names_rejected(self):
        for name in ("sight-mobile-gateway-2.0.96-windows-amd64.zip", "sight-mobile-gateway-2.0.96.zip",
                     "sight-mobile-gateway-2.0.96-linux-mips.zip"):
            p = os.path.join(self.d, name)
            make_gw_zip(p, "2.0.96", "amd64", top="x")
            with self.assertRaises(sd.DeployError, msg=name):
                sd.Package(p)

    def test_zip_without_binary(self):
        p = os.path.join(self.d, "sight-mobile-gateway-2.0.96-linux-amd64.zip")
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("a/README.md", "x")
        with self.assertRaises(sd.DeployError):
            sd.Package(p)

    def test_guess_and_check_zip(self):
        self.assertEqual(sd._guess_pkg("sight-mobile-gateway-2.0.96-linux-arm64.zip"), (sd.GATEWAY, "2.0.96"))
        self.assertEqual(sd._guess_pkg("sight-report-2.0.71.zip"), ("sight-report", "2.0.71"))
        self.assertEqual(sd._guess_pkg("sight-mobile-gateway-2.0.96-windows-amd64.zip"), (None, None))
        p = os.path.join(self.d, "sight-mobile-gateway-2.0.96-linux-amd64.zip")
        make_gw_zip(p, "2.0.96", "amd64")
        sd._check_zip_product(p, sd.GATEWAY)
        with self.assertRaises(sd.DeployError):
            sd._check_zip_product(p, "sight-report")

    def test_java_products_unaffected(self):
        """Java 产品的包识别不受影响；移动网关没有 jar，不会被当成 Java 产品。"""
        self.assertIsNone(sd.product_by_jar("sight-mobile-gateway"))
        self.assertEqual(sd.product_by_jar("sight-report.jar"), "sight-report")
        self.assertNotIn(sd.GATEWAY, sd.SITE_PRODUCT_IDS)

    @unittest.skipUnless(os.path.isfile("/home/claude/sight-data/release/gateway/sight-mobile-gateway-2.0.96-linux-amd64.zip"),
                         "没有真实安装包")
    def test_real_packages(self):
        for arch in ("amd64", "arm64", "loong64"):
            pkg = self.load("/home/claude/sight-data/release/gateway/sight-mobile-gateway-2.0.96-linux-%s.zip" % arch)
            self.assertEqual((pkg.version, pkg.arch), ("2.0.96", arch))


class Descriptor(unittest.TestCase):
    def test_gateway_descriptor(self):
        P = sd.PRODUCTS[sd.GATEWAY]
        self.assertTrue(sd.is_gateway(sd.GATEWAY))
        self.assertFalse(sd.is_gateway("sight-report"))
        self.assertEqual((P["name"], P["dir"], P["service"], P["port"], P["tunnel_port"]),
                         ("Sight 移动网关", "/opt/sight-mobile-gateway", "sight-mobile-gateway", 8080, 7443))
        self.assertEqual({a: s["site_id"] for a, s in sd.GATEWAY_ARCHES.items()},
                         {"amd64": "dl-gateway-linux-amd64", "arm64": "dl-gateway-linux-arm64", "loong64": "dl-gateway-linux-loong64"})

    def test_tool_version(self):
        self.assertGreaterEqual(tuple(int(x) for x in sd.TOOL_VERSION.split(".")), (2, 3, 0))


class ArchMatching(unittest.TestCase):
    def test_uname_to_arch(self):
        self.assertEqual(sd.gateway_arch_of("x86_64"), "amd64")
        self.assertEqual(sd.gateway_arch_of("aarch64\n"), "arm64")
        self.assertEqual(sd.gateway_arch_of("loongarch64"), "loong64")
        self.assertIsNone(sd.gateway_arch_of("riscv64"))
        self.assertIsNone(sd.gateway_arch_of(""))

    def test_match(self):
        self.assertIsNone(sd.gateway_arch_error("x86_64", "amd64"))
        self.assertIsNone(sd.gateway_arch_error("aarch64", "arm64"))
        self.assertIsNone(sd.gateway_arch_error("loongarch64", "loong64"))

    def test_mismatch_message(self):
        e = sd.gateway_arch_error("aarch64", "amd64")
        self.assertIn("arm64", e)
        self.assertIn("amd64", e)
        self.assertIn("riscv64", sd.gateway_arch_error("riscv64", "amd64"))


class Parsing(unittest.TestCase):
    def test_init_output(self):
        self.assertEqual(sd.parse_gateway_secrets(INIT_OUT), {"fingerprint": FP, "token": TOKEN})

    def test_show_output(self):
        self.assertEqual(sd.parse_gateway_secrets(SHOW_OUT), {"fingerprint": FP, "token": TOKEN})

    def test_show_without_tls(self):
        self.assertEqual(sd.parse_gateway_secrets(SHOW_NOTLS), {"fingerprint": None, "token": TOKEN})

    def test_token_with_dash_and_underscore(self):
        self.assertEqual(sd.parse_gateway_secrets("  接入令牌          : a-b_c" + "x" * 30)["token"], "a-b_c" + "x" * 30)

    def test_garbage(self):
        self.assertEqual(sd.parse_gateway_secrets("错误: 配置已存在"), {"fingerprint": None, "token": None})
        self.assertEqual(sd.parse_gateway_secrets(None), {"fingerprint": None, "token": None})

    def test_version(self):
        self.assertEqual(sd.parse_gateway_version("sight-mobile-gateway 2.0.96\n"), "2.0.96")
        self.assertEqual(sd.parse_gateway_version("sight-mobile-gateway dev"), "dev")
        self.assertIsNone(sd.parse_gateway_version("command not found"))

    def test_healthz(self):
        self.assertEqual(sd.parse_gateway_healthz('{"agents":2}'), {"agents": 2})
        self.assertEqual(sd.parse_gateway_healthz('{"agents":0}'), {"agents": 0})
        self.assertIsNone(sd.parse_gateway_healthz('{"status":"UP"}'))  # 端口上是别的程序
        self.assertIsNone(sd.parse_gateway_healthz("<html>"))
        self.assertIsNone(sd.parse_gateway_healthz('{"agents":true}'))

    def test_healthz_command(self):
        c = sd.gateway_healthz_cmd(7443, True)
        self.assertIn("curl -sk", c)
        self.assertIn("--noproxy", c)  # 服务器有全局代理时老 curl 不认 no_proxy 的 CIDR，必须显式绕过
        self.assertIn("--no-proxy", c)  # wget 分支同理
        self.assertIn("https://127.0.0.1:7443/healthz", c)
        self.assertIn("http://127.0.0.1:7000/healthz", sd.gateway_healthz_cmd(7000, False))

    def test_config(self):
        kv = sd.parse_kv('cfg_mobile=listen: ":9000"\ncfg_tunnel=listen: ":7443"  # 隧道\ncfg_tunnel_tls=tls: false\ncfg_host=a.example.com\ncfg_host="10.0.0.1"')
        self.assertEqual(sd.parse_gateway_config(kv), {"mobile_port": 9000, "tunnel_port": 7443, "tunnel_tls": False,
                                                       "hostnames": ["a.example.com", "10.0.0.1"]})
        self.assertEqual(sd.parse_gateway_config({}), {"mobile_port": 8080, "tunnel_port": 7443, "tunnel_tls": True, "hostnames": []})
        kv = sd.parse_kv("cfg_mobile=listen: 0.0.0.0:443\ncfg_tunnel_tls=tls: true")
        self.assertEqual(sd.parse_gateway_config(kv)["mobile_port"], 443)

    def test_hostnames(self):
        self.assertEqual(sd.parse_gateway_hostnames("gw.example.com, 203.0.113.10;gw.example.com"),
                         (["gw.example.com", "203.0.113.10"], None))
        self.assertEqual(sd.parse_gateway_hostnames("2001:db8::1")[0], ["2001:db8::1"])
        for bad in ("", "  ", "http://a.com", "a.com:443", "bad host!", "-flag", "999.1.1.1", "a;rm -rf /"):
            self.assertTrue(sd.parse_gateway_hostnames(bad)[1] or not sd.parse_gateway_hostnames(bad)[0], bad)
        self.assertTrue(sd.parse_gateway_hostnames("$(id)")[1])

    def test_inspect_script_never_prints_token(self):
        """读配置的脚本只取 listen / tls / hostnames，不能把 gateway.yaml 整个读出来。"""
        sh = sd.GW_INSPECT_SH
        self.assertNotIn("cat \"$D/gateway.yaml\"", sh)
        self.assertNotIn("token", sh)
        for line in sh.splitlines():
            if "gateway.yaml" in line:
                self.assertTrue(any(k in line for k in ("grep -E '^listen:", "grep -E '^[[:space:]]+listen:", "grep -E '^[[:space:]]+tls:",
                                                       "hostnames:", "[ -f", "-f \"$D/gateway.yaml\"")), line)

    def test_tunnel_url(self):
        self.assertEqual(sd.gateway_tunnel_url("10.0.0.1", ["gw.a.cn", "1.2.3.4"], 7443), "wss://gw.a.cn:7443/tunnel")
        self.assertEqual(sd.gateway_tunnel_url("10.0.0.1", [], 7443), "wss://10.0.0.1:7443/tunnel")
        self.assertEqual(sd.gateway_tunnel_url("h", ["2001:db8::1"], 7443), "wss://[2001:db8::1]:7443/tunnel")
        self.assertEqual(sd.gateway_tunnel_url("h", [], 7443, tls=False), "ws://h:7443/tunnel")


class EnvFindings(unittest.TestCase):
    BASE = {"os": "Linux", "arch": "x86_64", "distro": "Rocky 9", "systemd": "1", "disk_free_mb": "20000", "disk_mount": "/",
            "mobile_busy": "0", "tunnel_busy": "0", "http_tool": "1", "fw": "none"}

    def run_f(self, **over):
        env = dict(self.BASE, **over)
        return sd.gateway_env_findings(env, "amd64", 8080, 7443, "/opt/sight-mobile-gateway", 100)

    def fails(self, **over):
        return [t for lv, t in self.run_f(**over) if lv == "fail"]

    def test_all_good(self):
        self.assertEqual(self.fails(), [])

    def test_arch_mismatch_blocks(self):
        self.assertTrue(any("CPU 架构" in t for t in self.fails(arch="aarch64")))
        self.assertEqual(self.fails(arch="x86_64"), [])

    def test_no_systemd_blocks(self):
        self.assertTrue(any("systemd" in t for t in self.fails(systemd="0")))

    def test_ports_busy(self):
        self.assertTrue(any("手机侧端口 8080" in t for t in self.fails(mobile_busy="1")))
        self.assertTrue(any("隧道端口 7443" in t for t in self.fails(tunnel_busy="1")))

    def test_disk(self):
        self.assertTrue(any("磁盘" in t for t in self.fails(disk_free_mb="50")))

    def test_existing_config_blocks_overwrite(self):
        self.assertTrue(any("令牌和证书指纹" in t for t in self.fails(dir_exists="1", dir_has_cfg="1")))

    def test_other_unit_dir_blocks(self):
        self.assertTrue(self.fails(unit="1", unit_dir="/srv/other"))
        self.assertEqual(self.fails(unit="1", unit_dir="/opt/sight-mobile-gateway/"), [])

    def test_no_curl_warns_only(self):
        res = self.run_f(http_tool="0")
        self.assertTrue(any(lv == "warn" and "curl" in t for lv, t in res))
        self.assertEqual([t for lv, t in res if lv == "fail"], [])

    def test_firewall_warn(self):
        res = self.run_f(fw="firewalld", fw_open="0")
        self.assertTrue(any(lv == "warn" and "firewalld" in t for lv, t in res))


class SyncNotices(unittest.TestCase):
    HOSTS = {"前置机": {"products": {sd.GATEWAY: {"version": "2.0.95"}}},
             "前置机2": {"products": {sd.GATEWAY: {"version": "2.0.96"}}},
             "生产": {"products": {"sight-report": {"version": "2.0.96"}}},
             "空": {}}

    def test_older_gateway_is_reminded(self):
        n = sd.gateway_sync_notices(self.HOSTS, "2.0.96")
        self.assertEqual([x["host"] for x in n], ["前置机"])
        self.assertEqual((n[0]["gateway"], n[0]["target"]), ("2.0.95", "2.0.96"))
        self.assertIn("移动网关需同步升级到 2.0.96", n[0]["text"])

    def test_same_version_no_notice(self):
        self.assertEqual(sd.gateway_sync_notices(self.HOSTS, "2.0.95")[0]["host"], "前置机2")  # 2.0.96 的网关比 2.0.95 的 SightReport 新
        self.assertEqual(sd.gateway_sync_notices({"a": {"products": {sd.GATEWAY: {"version": "2.0.96"}}}}, "2.0.96"), [])

    def test_newer_gateway_wording(self):
        n = sd.gateway_sync_notices({"a": {"products": {sd.GATEWAY: {"version": "2.0.99"}}}}, "2.0.96")
        self.assertEqual(len(n), 1)
        self.assertNotIn("需同步升级到", n[0]["text"])
        self.assertIn("核对", n[0]["text"])

    def test_unknown_versions_ignored(self):
        self.assertEqual(sd.gateway_sync_notices(self.HOSTS, ""), [])
        self.assertEqual(sd.gateway_sync_notices({"a": {"products": {sd.GATEWAY: {}}}}, "2.0.96"), [])
        self.assertEqual(sd.gateway_sync_notices(None, "2.0.96"), [])


# ---------------------------------------------------------------------------------------------
# 升级 / 回滚脚本：命令序列 + 在本机临时目录里用假程序、假 systemctl 真正执行
# ---------------------------------------------------------------------------------------------

def _pos(text, needle, start=0):
    i = text.find(needle, start)
    assert i >= 0, "脚本里没有：%s" % needle
    return i


class UpgradeScriptSequence(unittest.TestCase):
    def script(self, registered=True):
        return sd.gateway_upgrade_script("/opt/sight-mobile-gateway", "/opt/sight-backups/sight-mobile-gateway/sight-mobile-gateway-2.0.95-20261031-101500.bin",
                                         '{"version":"2.0.95"}', "/opt/.sight-staging/pkg/sight-mobile-gateway",
                                         "/opt/.sight-staging/pkg/README.md", '{"version":"2.0.96"}', registered)

    def test_order_stop_backup_swap_start(self):
        s = self.script()
        i_stop = _pos(s, '"$BIN" stop')
        i_bak = _pos(s, 'cp -p "$BIN" "$B.tmp"')
        i_swap = _pos(s, 'mv -f "$BIN.new" "$BIN"', i_bak)
        i_start = _pos(s, '"$BIN" start', i_swap)
        i_ok = _pos(s, "finish OK")
        self.assertTrue(i_stop < i_bak < i_swap < i_start < i_ok)

    def test_never_touches_config_or_data(self):
        s = self.script()
        self.assertNotIn("gateway.yaml", s)
        self.assertNotIn("/data", s)
        self.assertNotIn("data/", s)
        self.assertNotIn("init", s.replace("finish", ""))   # 不会重新 init（会换令牌和指纹）
        self.assertNotIn("-force", s)
        self.assertNotIn("rm -rf", s)

    def test_failure_paths_restore_old_binary(self):
        s = self.script()
        for tag in ("FAIL:stop", "FAIL:backup", "FAIL:swap", "FAIL:start"):
            self.assertIn(tag, s)
        self.assertIn("restore_old", s)

    def test_unregistered_runs_install_before_start(self):
        s = self.script(registered=False)
        self.assertIn('"$BIN" install && "$BIN" start', s)
        self.assertNotIn('"$BIN" stop >/dev/null 2>&1 || true', s)

    def test_backup_naming(self):
        self.assertEqual(sd.gateway_backup_name("2.0.95", "20261031-101500"), "sight-mobile-gateway-2.0.95-20261031-101500.bin")
        self.assertEqual(sd.gateway_backup_version("/x/sight-mobile-gateway-2.0.95-20261031-101500.bin"), "2.0.95")
        self.assertIsNone(sd.gateway_backup_version("/x/sight-report-2.0.95-20261031-101500.tar.gz"))


@unittest.skipUnless(shutil.which("bash"), "需要 bash")
class UpgradeScriptRuns(unittest.TestCase):
    """用假的程序和 systemctl，在临时目录里真正执行升级 / 回滚脚本（不碰系统）。"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.inst = os.path.join(self.root, "opt", "sight-mobile-gateway")
        self.state = os.path.join(self.root, "state")
        self.fakebin = os.path.join(self.root, "fakebin")
        self.bdir = os.path.join(self.root, "opt", "sight-backups", "sight-mobile-gateway")
        for d in (self.inst, os.path.join(self.inst, "data"), self.state, self.fakebin):
            os.makedirs(d)
        with open(os.path.join(self.fakebin, "systemctl"), "w") as f:
            f.write('#!/bin/bash\n[ "$1" = is-active ] && [ -f "$STATE/active" ] && exit 0\nexit 3\n')
        os.chmod(os.path.join(self.fakebin, "systemctl"), 0o755)
        self.write_bin(os.path.join(self.inst, sd.GATEWAY_BIN), "2.0.95")
        self.cfg = os.path.join(self.inst, "gateway.yaml")
        self.write(self.cfg, "tunnel:\n  token: %s\n" % TOKEN)
        self.key = os.path.join(self.inst, "data", "tunnel.key")
        self.write(self.key, "PRIVATE KEY")
        self.write(os.path.join(self.state, "active"), "")
        self.before = {p: self.sha(p) for p in (self.cfg, self.key)}

    def tearDown(self):
        shutil.rmtree(self.root)

    @staticmethod
    def sha(p):
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    @staticmethod
    def read(p):
        with open(p, encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def write(p, text):
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    @staticmethod
    def write_bin(path, ver):
        with open(path, "w") as f:
            f.write('#!/bin/bash\nVER=%s\ncase "$1" in\n version) echo "sight-mobile-gateway $VER";;\n'
                    ' stop) rm -f "$STATE/active";;\n start) [ -f "$STATE/failstart_$VER" ] && exit 1; touch "$STATE/active";;\n'
                    ' install) touch "$STATE/installed";;\nesac\n' % ver)
        os.chmod(path, 0o755)

    def run_script(self, script, name="upgrade"):
        st = os.path.join(self.root, name + ".status")
        env = dict(os.environ, PATH=self.fakebin + os.pathsep + os.environ["PATH"], STATE=self.state)
        p = subprocess.run(["bash", "-c", "ST=%s\n%s" % (sd.shlex.quote(st), script)], env=env, capture_output=True, text=True, timeout=60)
        status = self.read(st).strip() if os.path.exists(st) else None
        return status, p.stdout + p.stderr

    def version(self):
        return subprocess.run([os.path.join(self.inst, sd.GATEWAY_BIN), "version"], capture_output=True, text=True,
                              env=dict(os.environ, STATE=self.state)).stdout.strip()

    def upgrade_script(self, new_ver="2.0.96", registered=True):
        new = os.path.join(self.root, "staged-" + new_ver)
        self.write_bin(new, new_ver)
        readme = os.path.join(self.root, "README.md")
        self.write(readme, "new readme")
        bfile = os.path.join(self.bdir, sd.gateway_backup_name("2.0.95", "20261031-101500"))
        s = sd.gateway_upgrade_script(self.inst, bfile, json.dumps({"version": "2.0.95"}), new, readme,
                                      json.dumps({"version": new_ver}), registered)
        return s, bfile

    def test_upgrade_success_keeps_config_and_data(self):
        s, bfile = self.upgrade_script()
        status, log = self.run_script(s)
        self.assertEqual(status, "OK", log)
        self.assertEqual(self.version(), "sight-mobile-gateway 2.0.96")
        self.assertTrue(os.path.isfile(bfile), "旧程序没有备份")
        self.assertTrue(os.path.isfile(bfile + ".json"))
        self.assertEqual({p: self.sha(p) for p in (self.cfg, self.key)}, self.before)  # 令牌 / 私钥一字未动
        self.assertTrue(os.path.exists(os.path.join(self.state, "active")))
        self.assertEqual(json.loads(self.read(os.path.join(self.inst, sd.MARKER)))["version"], "2.0.96")
        self.assertEqual(self.read(os.path.join(self.inst, "README.md")), "new readme")
        # 备份的是旧程序
        out = subprocess.run([bfile, "version"], capture_output=True, text=True, env=dict(os.environ, STATE=self.state)).stdout
        self.assertIn("2.0.95", out)

    def test_start_failure_restores_old_binary(self):
        self.write(os.path.join(self.state, "failstart_2.0.96"), "")
        s, bfile = self.upgrade_script()
        status, log = self.run_script(s)
        self.assertEqual(status, "FAIL:start", log)
        self.assertEqual(self.version(), "sight-mobile-gateway 2.0.95")  # 已自动放回旧程序
        self.assertTrue(os.path.exists(os.path.join(self.state, "active")), "旧版本应已重新启动")
        self.assertEqual({p: self.sha(p) for p in (self.cfg, self.key)}, self.before)

    def test_backup_failure_leaves_everything_and_restarts(self):
        s, bfile = self.upgrade_script()
        os.makedirs(self.bdir)
        os.chmod(self.bdir, 0o500)  # 备份目录不可写
        try:
            if os.geteuid() == 0:
                self.skipTest("root 不受目录权限限制")
            status, log = self.run_script(s)
            self.assertEqual(status, "FAIL:backup", log)
            self.assertEqual(self.version(), "sight-mobile-gateway 2.0.95")
            self.assertTrue(os.path.exists(os.path.join(self.state, "active")))
        finally:
            os.chmod(self.bdir, 0o700)

    def test_unregistered_service_gets_installed(self):
        os.remove(os.path.join(self.state, "active"))
        s, _ = self.upgrade_script(registered=False)
        status, log = self.run_script(s)
        self.assertEqual(status, "OK", log)
        self.assertTrue(os.path.exists(os.path.join(self.state, "installed")))
        self.assertTrue(os.path.exists(os.path.join(self.state, "active")))

    def test_rollback_restores_and_snapshots(self):
        s, bfile = self.upgrade_script()
        self.assertEqual(self.run_script(s)[0], "OK")
        snap = os.path.join(self.bdir, sd.gateway_backup_name("2.0.96", "20261031-110000"))
        rs = sd.gateway_rollback_script(self.inst, bfile, snap, json.dumps({"version": "2.0.96"}))
        status, log = self.run_script(rs, "rollback")
        self.assertEqual(status, "OK", log)
        self.assertEqual(self.version(), "sight-mobile-gateway 2.0.95")
        self.assertTrue(os.path.isfile(snap), "回滚前的程序应先另存一份")
        self.assertEqual({p: self.sha(p) for p in (self.cfg, self.key)}, self.before)
        self.assertNotIn("gateway.yaml", rs)
        self.assertNotIn("data", rs.replace("update", ""))

    def test_rollback_missing_backup(self):
        rs = sd.gateway_rollback_script(self.inst, os.path.join(self.bdir, "nope.bin"), "", "")
        status, _ = self.run_script(rs, "rollback")
        self.assertEqual(status, "FAIL:nofile")
        self.assertEqual(self.version(), "sight-mobile-gateway 2.0.95")


# ---------------------------------------------------------------------------------------------
# 首次安装的流程：用记录命令的假服务器，核对序列与「令牌不进日志」
# ---------------------------------------------------------------------------------------------

class FakeStore:
    def __init__(self):
        self.products = {}

    def set_product(self, alias, product, info_):
        self.products.setdefault((alias, product), {}).update(info_)

    def snapshot(self):
        return {"settings": {}, "hosts": {}}


class FakeCtx:
    def __init__(self):
        self.store = FakeStore()
        self.settings = {"keep_backups": 3, "health_timeout": 120, "auto_check": True}

    def host_product(self, r, product):
        return None


class RecordingRemote:
    host, alias, user, is_root = "10.8.0.10", "前置机", "root", True

    def __init__(self, env_over=None):
        self.cmds = []
        self.env = {"os": "Linux", "arch": "x86_64", "distro": "Rocky 9", "systemd": "1", "disk_free_mb": "20000", "disk_mount": "/",
                    "mobile_busy": "0", "tunnel_busy": "0", "http_tool": "1", "fw": "none"}
        self.env.update(env_over or {})

    def label(self):
        return "root@10.8.0.10"

    def home(self):
        return "/root"

    def run(self, script, sudo=False, timeout=600, check=False, on_line=None, what=None, cancellable=False, secret_out=False):
        self.cmds.append({"script": script, "secret_out": secret_out, "check": check})
        if "echo \"os=$(uname -s)\"" in script:
            return 0, "".join("%s=%s\n" % kv for kv in self.env.items()), ""
        if "version" in script and "chmod 755" in script:
            return 0, "sight-mobile-gateway 2.0.96\n", ""
        if " init -hostnames " in script:
            return 0, INIT_OUT, ""
        return 0, "", ""


class InstallFlow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        p = os.path.join(self.d, "sight-mobile-gateway-2.0.96-linux-amd64.zip")
        make_gw_zip(p, "2.0.96", "amd64")
        self.pkg = sd.Package(p)
        self.addCleanup(self.pkg.cleanup)
        self.addCleanup(shutil.rmtree, self.d)
        self.saved = {k: getattr(sd, k) for k in ("ask_form", "confirm", "upload_package", "open_firewall", "gateway_wait_ready", "ask")}
        self.form = {"dir": "/opt/sight-mobile-gateway", "hostnames": "gw.example.com, 203.0.113.10", "port": "8080"}
        sd.ask_form = lambda title, fields, validate=None, detail=None: (self.assertIsNone(validate(dict(self.form))), dict(self.form))[1]
        sd.confirm = lambda *a, **k: True
        sd.upload_package = lambda r, pkg, staging: None
        sd.open_firewall = lambda r, env, port: None
        sd.gateway_wait_ready = lambda r, d, tp, tls, timeout: (True, {"agents": 0})
        self.logged = []
        self._log = sd.LOG
        outer = self

        class L:
            path = None

            def write(self, s):
                outer.logged.append(s)
        sd.LOG = L()

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(sd, k, v)
        sd.LOG = self._log

    def install(self, **kw):
        r = RecordingRemote(**kw)
        ctx = FakeCtx()
        return r, ctx, sd.do_gateway_install(ctx, r, self.pkg)

    def scripts(self, r):
        return [c["script"] for c in r.cmds]

    def test_sequence_and_result(self):
        r, ctx, res = self.install()
        s = self.scripts(r)
        i_init = next(i for i, x in enumerate(s) if " init -hostnames gw.example.com,203.0.113.10" in x)
        i_inst = next(i for i, x in enumerate(s) if "./sight-mobile-gateway install && ./sight-mobile-gateway start" in x)
        i_copy = next(i for i, x in enumerate(s) if "mv -f /opt/sight-mobile-gateway/sight-mobile-gateway.new" in x)
        i_trial = next(i for i, x in enumerate(s) if "chmod 755" in x and " version" in x and "mv -f" not in x)
        self.assertTrue(i_trial < i_copy < i_init < i_inst)
        self.assertEqual((res["gateway"]["token"], res["gateway"]["fingerprint"]), (TOKEN, FP))
        self.assertEqual(res["gateway"]["tunnel_url"], "wss://gw.example.com:7443/tunnel")
        self.assertEqual((res["gateway"]["mobile_port"], res["gateway"]["tunnel_port"]), (8080, 7443))
        self.assertEqual(res["gateway"]["hostnames"], ["gw.example.com", "203.0.113.10"])
        self.assertIsNone(res["url"])
        self.assertEqual(ctx.store.products[("前置机", sd.GATEWAY)]["version"], "2.0.96")

    def test_default_port_not_edited_custom_port_is(self):
        r, _, _ = self.install()
        self.assertFalse(any("sed -i" in x for x in self.scripts(r)))
        self.form["port"] = "8443"
        r, ctx, res = self.install()
        edit = [x for x in self.scripts(r) if "sed -i" in x]
        self.assertEqual(len(edit), 1)
        self.assertIn('listen: ":8443"', edit[0])
        self.assertEqual(res["gateway"]["mobile_port"], 8443)
        self.assertEqual(ctx.store.products[("前置机", sd.GATEWAY)]["port"], 8443)
        # init 先于改端口，改端口先于 install
        s = self.scripts(r)
        self.assertTrue(next(i for i, x in enumerate(s) if " init " in x) < next(i for i, x in enumerate(s) if "sed -i" in x)
                        < next(i for i, x in enumerate(s) if " install " in x))

    def test_never_force_never_java(self):
        r, _, _ = self.install()
        for x in self.scripts(r):
            self.assertNotIn("-force", x)
            self.assertNotIn("install.sh", x)
            self.assertNotIn("java", x.lower())

    def test_secret_output_flag_and_no_token_in_log(self):
        r, _, res = self.install()
        init = next(c for c in r.cmds if " init -hostnames " in c["script"])
        self.assertTrue(init["secret_out"], "init 输出含令牌，必须不记录")
        self.assertEqual([c for c in r.cmds if c["secret_out"] and " init " not in c["script"]], [])
        blob = "\n".join(self.logged)
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(FP, blob)

    def test_busy_tunnel_port_blocks_before_any_change(self):
        with self.assertRaises(sd.DeployError):
            self.install(env_over={"tunnel_busy": "1"})

    def test_arch_mismatch_blocks(self):
        with self.assertRaises(sd.DeployError) as cm:
            self.install(env_over={"arch": "aarch64"})
        self.assertIn("环境不满足", str(cm.exception))

    def test_existing_config_never_overwritten(self):
        with self.assertRaises(sd.DeployError):
            self.install(env_over={"dir_exists": "1", "dir_has_cfg": "1"})

    def test_init_failure_reports_without_leaking(self):
        r = RecordingRemote()
        orig = r.run

        def bad(script, **kw):
            if " init -hostnames " in script:
                r.cmds.append({"script": script, "secret_out": kw.get("secret_out"), "check": False})
                return 1, "错误: gateway.yaml 已存在\n", ""
            return orig(script, **kw)
        r.run = bad
        with self.assertRaises(sd.DeployError) as cm:
            sd.do_gateway_install(FakeCtx(), r, self.pkg)
        self.assertIn("生成网关配置和证书失败", str(cm.exception))

    def test_not_started_raises_with_show_hint(self):
        sd.gateway_wait_ready = lambda r, d, tp, tls, timeout: (False, None)
        sd.show_logs = lambda *a, **k: None
        with self.assertRaises(sd.DeployError) as cm:
            self.install()
        self.assertIn("查看接入信息", cm.exception.hint)


class FakeChannel:
    def __init__(self, out):
        self.out, self.sent = out, False

    def get_pty(self, **k): pass
    def exec_command(self, cmd): pass
    def sendall(self, d): pass
    def shutdown_write(self): pass
    def recv_ready(self): return not self.sent
    def recv(self, n):
        self.sent = True
        return self.out.encode()
    def recv_stderr_ready(self): return False
    def exit_status_ready(self): return self.sent
    def recv_exit_status(self): return 0
    def close(self): pass


class FakeTransport:
    def __init__(self, out): self.out = out
    def is_active(self): return True
    def open_session(self): return FakeChannel(self.out)


class TokenNeverLogged(unittest.TestCase):
    def setUp(self):
        self.logged = []
        self._log = sd.LOG
        outer = self

        class L:
            path = None

            def write(self, s):
                outer.logged.append(s)
        sd.LOG = L()

    def tearDown(self):
        sd.LOG = self._log

    def remote(self, out):
        r = object.__new__(sd.Remote)
        r.cfg, r.t = {"host": "h"}, FakeTransport(out)
        return r

    def test_secret_out_skips_output_in_log(self):
        code, o, _ = self.remote(INIT_OUT).run_raw("cd /x && ./sight-mobile-gateway init", secret_out=True)
        self.assertEqual(sd.parse_gateway_secrets(o)["token"], TOKEN)  # 调用方拿得到
        blob = "\n".join(self.logged)
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(FP, blob)

    def test_mask_covers_access_token_even_without_flag(self):
        self.remote(SHOW_OUT).run_raw("whatever")
        self.assertNotIn(TOKEN, "\n".join(self.logged))

    def test_mask_helper(self):
        self.assertNotIn(TOKEN, sd._mask("  接入令牌          : %s" % TOKEN))
        self.assertNotIn(TOKEN, sd._mask("tunnel:\n  token: %s\n" % TOKEN))
        self.assertEqual(sd._mask("端口 8080 正常"), "端口 8080 正常")


class OnlineSite(unittest.TestCase):
    def setUp(self):
        self._h = sd._http_json
        self.items = [
            {"id": "dl-report", "version": "2.0.96", "localFile": True, "fileName": "sight-report-2.0.96.zip", "fileSize": "258M"},
            {"id": "dl-gateway-linux-amd64", "version": "2.0.96", "localFile": True, "fileName": "sight-mobile-gateway-2.0.96-linux-amd64.zip", "fileSize": "3.5M"},
            {"id": "dl-gateway-linux-arm64", "version": "2.0.95", "localFile": True, "fileName": "sight-mobile-gateway-2.0.95-linux-arm64.zip", "fileSize": "3.2M"},
            {"id": "dl-gateway-linux-loong64", "version": "2.0.96", "localFile": False},
        ]
        sd._http_json = lambda url, timeout=8: self.items

    def tearDown(self):
        sd._http_json = self._h

    def test_gateway_variants(self):
        tool, prods = sd._online_from_site()
        g = prods[sd.GATEWAY]
        self.assertEqual(sorted(g["variants"]), ["amd64", "arm64"])  # 没有安装包文件的条目当作没有
        self.assertEqual(g["variants"]["amd64"]["url"], sd.SITE_BASE + "/downloads/file/dl-gateway-linux-amd64")
        self.assertEqual(g["variants"]["arm64"]["latest"], "2.0.95")
        self.assertEqual(g["latest"], "2.0.96")  # 顶层是最高版本
        self.assertEqual(prods["sight-report"]["latest"], "2.0.96")

    def test_no_gateway_items(self):
        self.items = [self.items[0]]
        self.assertNotIn(sd.GATEWAY, sd._online_from_site()[1])

    def test_download_needs_arch(self):
        orig = sd.check_online
        sd.check_online = lambda force=False: {"error": None, "products": sd._online_from_site()[1]}
        try:
            with self.assertRaises(sd.DeployError) as cm:
                sd.job_download_package(None, {"product": sd.GATEWAY})
            self.assertIn("CPU 架构", str(cm.exception))
            with self.assertRaises(sd.DeployError) as cm:
                sd.job_download_package(None, {"product": sd.GATEWAY, "arch": "loong64"})
            self.assertIn("没有找到", str(cm.exception))
        finally:
            sd.check_online = orig


if __name__ == "__main__":
    unittest.main()
