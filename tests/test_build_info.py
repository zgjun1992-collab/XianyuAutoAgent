import unittest

from build_info import APP_EDITION, APP_VERSION


class BuildInfoTests(unittest.TestCase):
    def test_v36_release_identity(self):
        self.assertEqual("V3.6", APP_EDITION)
        self.assertEqual("0.11.3", APP_VERSION)


if __name__ == "__main__":
    unittest.main()
