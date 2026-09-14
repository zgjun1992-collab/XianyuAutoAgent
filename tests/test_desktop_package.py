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
            "docs/闲鱼卡券AI客服-V3.6-客户使用说明书-0.11.2.pdf",
        }
        packaged = {entry["from"] for entry in resources}
        self.assertTrue(expected.issubset(packaged))
        for relative_path in expected:
            document = DESKTOP / relative_path
            self.assertTrue(document.is_file(), relative_path)
            self.assertGreater(document.stat().st_size, 10_000, relative_path)


if __name__ == "__main__":
    unittest.main()
