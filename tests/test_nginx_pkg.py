# -*- coding: utf-8 -*-
"""集群入口的离线 nginx 包：包校验、架构识别、官网条目解析；可选的容器内真实安装测试。

离线包由 sight-data 仓库 scripts/nginx/build-release.sh 构建。运行：python3 -m unittest discover -s tests -v
设置 SIGHT_TEST_DOCKER=1 且 SIGHT_NGINX_PKG=<amd64 离线包路径> 时，另外在 rockylinux:9 / debian:12 容器里
（当作没有 nginx、也连不上软件源的入口服务器）走一遍：选安装方式 → 软件源装不上 → 改用离线包 → 安装 →
生成集群配置并重载 → 经入口访问；再走一遍升级。
"""
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sight_deploy as sd  # noqa: E402

ELF_AMD64 = b"\x7fELF\x02\x01\x01" + b"\0" * 11 + (0x3E).to_bytes(2, "little") + b"\0" * 64
ELF_ARM64 = b"\x7fELF\x02\x01\x01" + b"\0" * 11 + (0xB7).to_bytes(2, "little") + b"\0" * 64


def make_pkg(d, name, files):
    path = os.path.join(d, name)
    with tarfile.open(path, "w:gz") as tf:
        for n, data in files.items():
            ti = tarfile.TarInfo(n)
            if data is None:
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            elif isinstance(data, tuple):  # ("symlink", target)
                ti.type, ti.linkname = tarfile.SYMTYPE, data[1]
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return path


def good_files(elf=ELF_AMD64):
    return {"sight-nginx": None, "sight-nginx/sbin/nginx": elf, "sight-nginx/conf/nginx.conf.default": b"x",
            "sight-nginx/conf/sight-nginx.service": b"x", "sight-nginx/conf/mime.types": b"x",
            "sight-nginx/VERSION": b"package=1.30.6.1\nnginx=1.30.6\n"}


class PackageCheck(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_good(self):
        p = make_pkg(self.d, "sight-nginx-1.30.6.1-linux-amd64.tar.gz", good_files())
        self.assertEqual(sd.inspect_nginx_pkg(p, "amd64"), {"version": "1.30.6.1", "nginx": "1.30.6", "arch": "amd64"})

    def test_rejects(self):
        cases = [
            ("sight-nginx-1.30.6.1-linux-amd64.tar.gz", good_files(ELF_ARM64), "amd64", "不是这个架构"),
            ("sight-nginx-1.30.6.1-linux-arm64.tar.gz", good_files(ELF_ARM64), "amd64", "入口服务器是 x86_64"),
            ("nginx-1.30.6.tar.gz", good_files(), None, "文件名"),
            ("sight-nginx-1.30.6.1-linux-amd64.tar.gz", dict(good_files(), **{"sight-nginx/../../etc/x": b"x"}), None, "路径"),
            ("sight-nginx-1.30.6.1-linux-amd64.tar.gz", dict(good_files(), **{"etc/cron.d/x": b"x"}), None, "路径"),
            ("sight-nginx-1.30.6.1-linux-amd64.tar.gz", dict(good_files(), **{"sight-nginx/l": ("l", "/etc/passwd")}), None, "链接"),
            ("sight-nginx-1.30.6.1-linux-amd64.tar.gz", {k: v for k, v in good_files().items() if "service" not in k}, None, "缺少"),
            ("sight-nginx-1.30.6.1-linux-amd64.tar.gz", dict(good_files(), **{"sight-nginx/sbin/nginx": b"#!/bin/sh\n" * 5}), None, "可执行"),
        ]
        for i, (name, files, arch, msg) in enumerate(cases):
            sub = os.path.join(self.d, str(i))
            os.mkdir(sub)
            with self.assertRaises(sd.DeployError, msg=msg) as cm:
                sd.inspect_nginx_pkg(make_pkg(sub, name, files), arch)
            self.assertIn(msg, str(cm.exception))
        bad = os.path.join(self.d, "sight-nginx-1.30.6.1-linux-amd64.tar.gz")
        with open(bad, "wb") as f:
            f.write(b"not a tarball")
        with self.assertRaises(sd.DeployError):
            sd.inspect_nginx_pkg(bad)

    def test_arch(self):
        self.assertEqual(sd.nginx_pkg_arch("x86_64\n"), "amd64")
        self.assertEqual(sd.nginx_pkg_arch("aarch64"), "arm64")
        self.assertIsNone(sd.nginx_pkg_arch("loongarch64"), "只出主流架构")
        self.assertIsNone(sd.nginx_pkg_arch(""))

    def test_info_parse(self):
        d = sd.parse_nginx_info("arch=x86_64\nnginx=/opt/sight-nginx/sbin/nginx\nbundled=1\nversion=1.30.6\ntest=ok\n")
        self.assertEqual((d["arch"], d["bundled"], d["version"]), ("x86_64", "1", "1.30.6"))


class OnlineCatalog(unittest.TestCase):
    def setUp(self):
        self._h = sd._http_json
        sd._http_json = lambda url: [
            {"id": "dl-nginx-linux-amd64", "version": "1.30.6.1", "localFile": "x", "fileName": "sight-nginx-1.30.6.1-linux-amd64.tar.gz"},
            {"id": "dl-nginx-linux-arm64", "version": "1.30.6.1", "localFile": None},  # 没有文件：当作没有
        ]

    def tearDown(self):
        sd._http_json = self._h

    def test_nginx_variants(self):
        _, prods, nginx = sd._online_from_site()
        self.assertEqual(sorted(nginx), ["amd64"])
        self.assertTrue(nginx["amd64"]["url"].endswith("/downloads/file/dl-nginx-linux-amd64"))
        self.assertNotIn("nginx", prods)


# ------------------------------------------------------------------ 容器里真实安装（可选）

class DockerHost:
    """把一个容器当作入口服务器：远端脚本经 docker exec 执行，上传用 docker cp。"""
    def __init__(self, ctr):
        self.ctr, self.alias, self.host, self.user, self.is_root = ctr, "entry", "127.0.0.1", "root", True

    def label(self):
        return "容器 " + self.ctr

    def run(self, script, sudo=False, timeout=600, check=False, on_line=None, what=None, **kw):
        p = subprocess.run(["docker", "exec", self.ctr, "bash", "-c", script], capture_output=True, text=True, timeout=timeout)
        if check and p.returncode != 0:
            raise sd.DeployError("%s失败：%s" % (what or "命令", (p.stderr or p.stdout)[-300:]))
        return p.returncode, p.stdout, p.stderr

    def upload(self, local, remote, label):
        subprocess.run(["docker", "cp", local, "%s:%s" % (self.ctr, remote)], check=True, capture_output=True)

    def close(self):
        pass


class _Up(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


PKG = os.environ.get("SIGHT_NGINX_PKG")
# 容器里不一定有 curl：用 bash 的 /dev/tcp 发请求，输出状态码
GET = ("exec 3<>/dev/tcp/127.0.0.1/8088 && printf 'GET %s HTTP/1.0\\r\\nHost: 127.0.0.1\\r\\n\\r\\n' >&3 "
       "&& head -1 <&3 | cut -d' ' -f2")


@unittest.skipUnless(os.environ.get("SIGHT_TEST_DOCKER") == "1" and PKG and os.path.isfile(PKG) and shutil.which("docker"),
                     "设置 SIGHT_TEST_DOCKER=1 与 SIGHT_NGINX_PKG=<amd64 离线包> 时才测（会拉起容器）")
class InstallInContainer(unittest.TestCase):
    IMAGES = ("rockylinux:9", "debian:12")

    def setUp(self):
        self.up = http.server.ThreadingHTTPServer(("0.0.0.0", 0), _Up)
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        self.saved = {k: getattr(sd, k) for k in ("choose", "confirm", "obtain_nginx_pkg")}
        self.asked = []
        self.pick = "pkg"
        sd.choose = lambda title, opts, **kw: self.asked.append([o[1] for o in opts]) or self.pick
        sd.confirm = lambda *a, **k: True
        sd.obtain_nginx_pkg = lambda arch: PKG

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(sd, k, v)
        self.up.shutdown()

    def one(self, image, pick):
        self.pick = pick
        ctr = "sd-nginx-" + uuid.uuid4().hex[:8]
        subprocess.run(["docker", "run", "-d", "--name", ctr, "--add-host", "host.docker.internal:host-gateway",
                        image, "sleep", "600"], check=True, capture_output=True)
        try:
            r = DockerHost(ctr)
            d = sd.ensure_nginx(r, "c1")
            self.assertEqual(d.get("bundled"), "1", d)
            self.assertEqual(d.get("version"), "1.30.6")
            self.assertEqual(self.asked[-1], ["pkg", "bundled"], "软件源和离线包两种方式都给用户选")
            port = self.up.server_address[1]
            rec = {"product": "sight-report", "entry": {"host": "entry", "port": 8088, "server_name": ""},
                   "instances": [{"host": "n1", "addr": "host.docker.internal", "product": "sight-report", "port": port}]}
            sd.apply_nginx(r, "c1", rec, {"entry": {"host": "127.0.0.1"}})
            self.assertEqual(sd._entry_probe(r, 8088, "_"), "200", "经入口访问正常（容器里没有 curl 时走 /dev/tcp）")
            code, o, _ = r.run(GET % "/api/machine/x")
            self.assertEqual(o.strip(), "404", "机器接口不经入口暴露")
            # 升级（同一个包当新版本）：程序换掉、配置不动、继续服务
            sd.install_bundled_nginx(r, PKG, "amd64")
            code, o, _ = r.run("test -f /etc/nginx/conf.d/sight-c1.conf && " + GET % "/api/health/live" + "; id sight-nginx >/dev/null && echo user")
            self.assertEqual(o.split(), ["200", "user"], "升级后配置还在、照常服务")
        finally:
            subprocess.run(["docker", "rm", "-f", ctr], capture_output=True)

    def test_rocky9_offline_directly(self):
        self.one("rockylinux:9", "bundled")

    def test_debian12_repo_fails_then_offline(self):
        self.one("debian:12", "pkg")  # 容器里没有软件源索引：apt 装不上 → 改用离线包


if __name__ == "__main__":
    unittest.main()
