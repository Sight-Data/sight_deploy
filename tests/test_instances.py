# -*- coding: utf-8 -*-
"""同机多实例：带实例名的产品键、探测过滤、安装包能力（只用标准库，不连服务器）。

见 sight-data 仓库 docs/集群部署与集群入口-设计.md §3.2。运行：python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sight_deploy as sd  # noqa: E402
from test_products import make_zip  # noqa: E402

REPORT_MF = {"Product-Version": "2.0.117", "Java-Version": "21", "Start-Class": "com.sight.SightDataApplication"}


class InstanceKeys(unittest.TestCase):
    def test_derived_product(self):
        P = sd.PRODUCTS["sight-report@b"]
        self.assertEqual(P["service"], "sight-report-b")
        self.assertEqual(P["dir"], "/opt/sight-report-b")
        self.assertEqual(P["instance"], "b")
        self.assertEqual(P["base"], "sight-report")
        self.assertIn("实例 b", P["name"])
        self.assertEqual(P["jar"], "sight-report.jar")
        self.assertNotIn("instance", sd.PRODUCTS["sight-report"], "基础产品描述不能被派生污染")

    def test_membership_and_iteration(self):
        self.assertIn("sight-wall@a1", sd.PRODUCTS)
        self.assertNotIn("sight-mobile-gateway@b", sd.PRODUCTS, "移动网关不支持实例")
        self.assertNotIn("sight-report@Bad_Name", sd.PRODUCTS)
        self.assertNotIn("nope@b", sd.PRODUCTS)
        self.assertEqual(list(sd.PRODUCTS.keys()), ["sight-report", "sight-wall", "sight-datalink", "sight-mobile-gateway"],
                         "遍历只看到基础产品（/api/state 的 products 不变）")
        self.assertIsNone(sd.PRODUCTS.get("sight-report@"))
        self.assertEqual(json.loads(json.dumps(sd.PRODUCTS))["sight-report"]["port"], 9091)

    def test_split(self):
        self.assertEqual(sd.split_instance("sight-report@b"), ("sight-report", "b"))
        self.assertEqual(sd.split_instance("sight-report"), ("sight-report", ""))
        self.assertEqual(sd.instance_key("sight-report", ""), "sight-report")

    def test_backup_and_staging_dirs_follow_service_name(self):
        self.assertEqual(sd.default_backup_dir("/opt/sight-report", "sight-report"), "/opt/sight-backups/sight-report",
                         "基础产品与老版本一致")
        self.assertEqual(sd.default_backup_dir("/opt/sight-report-b", "sight-report@b"), "/opt/sight-backups/sight-report-b")


class Detection(unittest.TestCase):
    OUT = "\n".join([
        "cand=/opt/sight-report|unit|",
        "cand=/opt/sight-report|proc|",
        "cand=/opt/sight-report-b|proc|b",
        "cand=/opt/sight-report-c|dir|c",
        "cand=/old/style|dir",  # 老版本输出没有第三段：按基础产品
    ])

    def test_candidates_filtered_by_instance(self):
        self.assertEqual(sd.parse_install_candidates(self.OUT, ""), [("/opt/sight-report", "unit"), ("/old/style", "dir")])
        self.assertEqual(sd.parse_install_candidates(self.OUT, "b"), [("/opt/sight-report-b", "proc")])
        self.assertEqual(sd.parse_install_candidates(self.OUT, "z"), [])

    def test_parse_instances(self):
        out = "instance=b|/opt/sight-report-b/\ninstance=c|/data/c\ninstance=Bad|/x\n"
        self.assertEqual(sd.parse_instances(out), {"b": "/opt/sight-report-b", "c": "/data/c"})


class PackageCapabilities(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def pkg(self, extra, release=None):
        p = os.path.join(self.d, "sight-report-2.0.117.zip")
        make_zip(p, "sight-report-2.0.117", "sight-report.jar", REPORT_MF, extra=extra)
        if release is not None:
            with zipfile.ZipFile(p, "a") as zf:
                zf.writestr("sight-report-2.0.117/release.json", json.dumps(release))
        pkg = sd.Package(p)
        self.addCleanup(pkg.cleanup)
        return pkg

    def test_new_package_supports_instances_and_release(self):
        pkg = self.pkg(("install.sh", "start.sh", "sight-common.sh"),
                       {"product": "sight-report", "version": "2.0.117", "rollingFrom": "2.0.116"})
        self.assertTrue(pkg.supports_instances)
        self.assertEqual(pkg.rolling_from, "2.0.116")
        pkg.use_instance("b")
        self.assertEqual(pkg.product, "sight-report@b")
        self.assertIn("实例 b", pkg.name)
        pkg.use_instance("")
        self.assertEqual(pkg.product, "sight-report@b", "空实例名不改")

    def test_old_package_cannot_install_instance(self):
        pkg = self.pkg(("install.sh", "start.sh"))
        self.assertFalse(pkg.supports_instances)
        self.assertIsNone(pkg.rolling_from)
        with self.assertRaises(sd.DeployError) as cm:
            pkg.use_instance("b")
        self.assertIn("2.0.117", cm.exception.hint)

    def test_bad_instance_name(self):
        pkg = self.pkg(("install.sh", "sight-common.sh"))
        with self.assertRaises(sd.DeployError):
            pkg.use_instance("B_1")


if __name__ == "__main__":
    unittest.main()
