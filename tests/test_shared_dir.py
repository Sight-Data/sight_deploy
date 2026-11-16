# -*- coding: utf-8 -*-
"""集群共享目录（MR_SHARED_DIR）的迁移：扫描 / 合并脚本用真 bash 跑，校验合并语义（新的为准、冲突列清单、原目录不删）。

见 sight-data 仓库 docs/集群部署与集群入口-设计.md §5.5「迁移」。运行：python3 -m unittest discover -s tests -v
"""
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sight_deploy as sd  # noqa: E402


def write(path, text, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mtime:
        os.utime(path, (mtime, mtime))


def run(script):
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


class Validate(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(sd.validate_shared_dir("/mnt/sight-shared/prod/")[0], "/mnt/sight-shared/prod")
        for bad in ("", "rel/dir", "/", "/opt", "/mnt", "/a b", "/a/../etc", "/a;rm"):
            self.assertIsNone(sd.validate_shared_dir(bad)[0], bad)

    def test_subdirs_match_product_mapping(self):
        """与产品 SharedPaths.MAPPING 一致（temp/export 是临时文件，故意不迁移）。"""
        names = [b for _, b in sd.SHARED_SUBDIRS]
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("export", names)
        self.assertIn(("data/backups", "backups"), sd.SHARED_SUBDIRS)
        self.assertIn(("data/work/screen-assets", "screen-assets"), sd.SHARED_SUBDIRS)


class Scripts(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="sd-shared-")
        self.inst = os.path.join(self.t, "opt", "sight-report")
        self.shared = os.path.join(self.t, "shared")
        write(os.path.join(self.inst, "sight-common.sh"), "#")  # 安装包把 bin/ 下的脚本解到安装目录根上

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def scan(self):
        code, o, e = run(sd.SHARED_SCAN_SH % {"dir": shlex.quote(self.inst), "svc": "sight-report-nonexistent-unit",
                                               "pairs": sd._shared_pairs()})
        self.assertEqual(code, 0, e)
        return sd.parse_shared_scan(o)

    def merge(self, old=""):
        code, o, e = run(sd.SHARED_MERGE_SH % {"dir": shlex.quote(self.inst), "shared": shlex.quote(self.shared),
                                                "old": shlex.quote(old), "owner": "root", "pairs": sd._shared_pairs()})
        self.assertEqual(code, 0, e + o)
        return sd.parse_shared_merge(o)

    def test_scan_counts_local_dirs_and_reads_env(self):
        write(os.path.join(self.inst, "data/fonts/a.ttf"), "A" * 2000)
        write(os.path.join(self.inst, "data/geo/sub/x.json"), "{}")
        write(os.path.join(self.inst, "data/cache/skip.bin"), "x")      # 节点自己的，不迁移
        write(os.path.join(self.inst, "plugins/p.jar"), "x")             # 插件不迁移
        sc = self.scan()
        self.assertTrue(sc["env_ok"])
        self.assertEqual(sc["cur"], "")
        self.assertEqual({d[0]: d[1] for d in sc["dirs"]}, {"fonts": 1, "geo": 1})
        write(os.path.join(self.inst, "sight.env"), "SERVER_PORT=9092\nMR_SHARED_DIR=\"/mnt/old\"\n")
        self.assertEqual(self.scan()["cur"], "/mnt/old")

    def test_scan_old_install_cannot_read_env(self):
        os.remove(os.path.join(self.inst, "sight-common.sh"))
        self.assertFalse(self.scan()["env_ok"])

    def test_merge_newer_wins_conflicts_listed_source_kept(self):
        now = time.time()
        write(os.path.join(self.inst, "data/fonts/same.ttf"), "same")
        write(os.path.join(self.shared, "fonts/same.ttf"), "same")
        write(os.path.join(self.inst, "data/fonts/newer-local.ttf"), "local", now)
        write(os.path.join(self.shared, "fonts/newer-local.ttf"), "shared", now - 3600)
        write(os.path.join(self.inst, "data/drivers/older-local.jar"), "local", now - 3600)
        write(os.path.join(self.shared, "drivers/older-local.jar"), "shared", now)
        write(os.path.join(self.inst, "data/work/screen-assets/a b/图.png"), "img")   # 空格、中文文件名
        write(os.path.join(self.inst, "data/backups/b1.zip"), "zip")
        mg = self.merge()
        self.assertEqual(mg["errors"], [])
        self.assertEqual(sorted(mg["conflicts"]), ["drivers/older-local.jar", "fonts/newer-local.ttf"])
        rd = lambda p: open(os.path.join(self.shared, p)).read()  # noqa: E731
        self.assertEqual(rd("fonts/newer-local.ttf"), "local")
        self.assertEqual(rd("drivers/older-local.jar"), "shared")
        self.assertEqual(rd("screen-assets/a b/图.png"), "img")
        self.assertEqual(rd("backups/b1.zip"), "zip")
        self.assertTrue(os.path.exists(os.path.join(self.inst, "data/fonts/newer-local.ttf")), "原目录不删")
        # 再跑一次：幂等，没有新冲突
        self.assertEqual(self.merge()["conflicts"], ["drivers/older-local.jar"])

    def test_merge_from_previous_shared_dir(self):
        old = os.path.join(self.t, "old-shared")
        write(os.path.join(old, "geo/c.json"), "c")
        write(os.path.join(self.inst, "data/geo/local-ignored.json"), "l")  # 已在用旧共享目录：本地目录不是来源
        mg = self.merge(old)
        self.assertTrue(os.path.exists(os.path.join(self.shared, "geo/c.json")))
        self.assertFalse(os.path.exists(os.path.join(self.shared, "geo/local-ignored.json")))
        self.assertEqual(mg["merged"], [("geo", 1)])

    def test_merge_into_same_dir_is_noop(self):
        write(os.path.join(self.shared, "fonts/a.ttf"), "a")
        mg = self.merge(self.shared)
        self.assertEqual(mg["merged"], [])

    def test_member_status_reads_shared_dir(self):
        import base64
        env = base64.b64encode(b"MR_SHARED_DIR=/mnt/s/\nSIGHTDATA_AI_KEY=k\n").decode()
        res = sd.parse_instance_secrets("@@envfile %s\n" % env)
        self.assertEqual(res["shared_dir"], "/mnt/s")
        self.assertNotIn("MR_SHARED_DIR", res["env"], "MR_SHARED_DIR 不随机密一起拷：扩容用集群记录里的值")


if __name__ == "__main__":
    unittest.main()
