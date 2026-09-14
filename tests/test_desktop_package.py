import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop_v2"


class DesktopPackageTests(unittest.TestCase):
    def test_customer_documents_are_packaged(self):
        package = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
        resources = package["build"]["extraResources"]
        expected = {
            "docs/阿里云百炼APIKey获取图文教程.pdf",
            "docs/闲鱼卡券AI客服-V3.6-客户使用说明书-0.11.6.pdf",
        }
        packaged = {entry["from"] for entry in resources}
        self.assertTrue(expected.issubset(packaged))
        for relative_path in expected:
            document = DESKTOP / relative_path
            self.assertTrue(document.is_file(), relative_path)
            self.assertGreater(document.stat().st_size, 10_000, relative_path)

    def test_auto_update_channel_is_configured(self):
        package = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
        self.assertEqual("0.11.6", package["version"])
        self.assertIn("electron-updater", package["dependencies"])
        self.assertEqual(
            [{"provider": "generic", "url": "https://download.yituan123.com/v3.6"}],
            package["build"]["publish"],
        )
        self.assertIn("Windows 系统托盘", package["build"]["releaseInfo"]["releaseNotes"])
        self.assertIn("esbuild electron/updater-entry.cjs", package["scripts"]["build:updater"])
        self.assertTrue((DESKTOP / "electron" / "updater-entry.cjs").is_file())
        self.assertTrue((DESKTOP / "electron" / "update-policy.cjs").is_file())

    def test_r2_release_script_preserves_safe_upload_order(self):
        script = (ROOT / "deploy" / "deploy-r2-release.ps1").read_text(encoding="utf-8")
        installer = script.index("$installerPath")
        blockmap = script.index("$blockmapPath", installer)
        latest = script.index("$latestPath", blockmap)
        self.assertLess(installer, blockmap)
        self.assertLess(blockmap, latest)
        self.assertIn('CacheControl = "no-store, max-age=0"', script)

    def test_v36_ui_has_no_waiting_payment_auto_notice_rule(self):
        source = (DESKTOP / "src" / "App.vue").read_text(encoding="utf-8")
        self.assertNotIn("order_payment_notice_enabled", source)
        self.assertNotIn("order_notice_enabled", source)
        self.assertNotIn("拍下未付款", source)


if __name__ == "__main__":
    unittest.main()
