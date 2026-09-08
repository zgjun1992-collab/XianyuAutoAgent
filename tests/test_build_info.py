import unittest

from build_info import APP_EDITION, APP_VERSION


class BuildInfoTests(unittest.TestCase):
    def test_v35_release_identity(self):
        self.assertEqual("V3.5", APP_EDITION)
        self.assertEqual("0.9.27", APP_VERSION)


if __name__ == "__main__":
    unittest.main()
