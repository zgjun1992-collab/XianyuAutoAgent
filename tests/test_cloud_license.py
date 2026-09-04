import os
import tempfile
import unittest

from cloud_license.store import LicenseError, LicenseStore


class CloudLicenseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LicenseStore(os.path.join(self.temp.name, "license.db"))
        self.user = self.store.create_user("buyer@example.com", "strong-pass-123", "测试买家")

    def tearDown(self):
        self.temp.cleanup()

    def test_login_requires_active_subscription(self):
        with self.assertRaisesRegex(LicenseError, "套餐未开通"):
            self.store.login("buyer@example.com", "strong-pass-123", "device-0001")

    def test_monthly_subscription_login_and_verify(self):
        entitlement = self.store.grant_subscription(self.user["id"], "monthly")
        self.assertTrue(entitlement["active"])
        login = self.store.login("buyer@example.com", "strong-pass-123", "device-0001", "Office PC")
        verified = self.store.verify_token(login["token"], "device-0001")
        self.assertEqual("monthly", verified["entitlement"]["plan_code"])
        self.assertEqual(48, verified["entitlement"]["offline_grace_hours"])

    def test_renewal_extends_existing_expiry(self):
        first = self.store.grant_subscription(self.user["id"], "monthly")
        second = self.store.grant_subscription(self.user["id"], "monthly")
        self.assertGreater(second["expires_at"], first["expires_at"])

    def test_device_limit_and_admin_revoke(self):
        self.store.grant_subscription(self.user["id"], "monthly")
        first = self.store.login("buyer@example.com", "strong-pass-123", "device-0001")
        with self.assertRaisesRegex(LicenseError, "设备数量"):
            self.store.login("buyer@example.com", "strong-pass-123", "device-0002")
        self.store.revoke_device(self.user["id"], "device-0001")
        with self.assertRaises(LicenseError):
            self.store.verify_token(first["token"], "device-0001")

    def test_freezing_user_revokes_session(self):
        self.store.grant_subscription(self.user["id"], "yearly")
        login = self.store.login("buyer@example.com", "strong-pass-123", "device-0001")
        self.store.set_user_status(self.user["id"], "frozen")
        with self.assertRaises(LicenseError):
            self.store.verify_token(login["token"], "device-0001")


if __name__ == "__main__":
    unittest.main()
