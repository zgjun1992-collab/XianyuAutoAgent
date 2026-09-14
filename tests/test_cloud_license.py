import os
import tempfile
import unittest
from datetime import datetime

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

    def test_admin_password_allows_memorable_letters_and_digits(self):
        admin = self.store.create_admin("operator", "hetang2026")
        self.assertEqual("operator", admin["username"])

        with self.assertRaisesRegex(LicenseError, "至少8位"):
            self.store.create_admin("too-short", "abc123")
        with self.assertRaisesRegex(LicenseError, "字母和数字"):
            self.store.create_admin("digits-only", "12345678")

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

    def test_sale_plans_have_expected_names_and_durations(self):
        expected = {
            "weekly": ("周卡", 7),
            "monthly": ("月卡", 30),
            "quarterly": ("季卡", 90),
            "yearly": ("年卡", 365),
        }
        for index, (code, (name, days)) in enumerate(expected.items(), start=1):
            user = self.store.create_user(
                f"plan-{index}@example.com", "strong-pass-123", name
            )
            entitlement = self.store.grant_subscription(user["id"], code)
            starts_at = datetime.fromisoformat(entitlement["starts_at"])
            expires_at = datetime.fromisoformat(entitlement["expires_at"])
            self.assertEqual(name, entitlement["name"])
            self.assertEqual(days, (expires_at - starts_at).days)

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
