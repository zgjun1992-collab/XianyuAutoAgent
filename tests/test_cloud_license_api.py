import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from fastapi.testclient import TestClient

from cloud_license.config import Settings
from cloud_license.server import create_app
from cloud_license.store import LicenseStore


ADMIN_USERNAME = "license-admin"
ADMIN_PASSWORD = "Admin-Password#2026"


class CloudLicenseApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "license.db")
        self.store = LicenseStore(self.db_path)
        self.store.create_admin(ADMIN_USERNAME, ADMIN_PASSWORD)
        self.settings = Settings(
            database_path=self.db_path,
            allowed_hosts=("testserver",),
            cookie_secure=False,
            trust_proxy=False,
        )
        self.client = TestClient(create_app(store=self.store, settings=self.settings))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def login_admin(self):
        response = self.client.post(
            "/v1/admin/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
        )
        self.assertEqual(200, response.status_code, response.text)
        return response

    def test_health_and_admin_security_headers(self):
        health = self.client.get("/health")
        self.assertEqual(200, health.status_code)
        self.assertEqual("ok", health.json()["data"]["status"])
        self.assertEqual("nosniff", health.headers["x-content-type-options"])

        admin_page = self.client.get("/admin")
        self.assertEqual(200, admin_page.status_code)
        self.assertIn("default-src 'self'", admin_page.headers["content-security-policy"])
        self.assertEqual("DENY", admin_page.headers["x-frame-options"])

    def test_admin_password_is_argon2_hashed(self):
        with closing(sqlite3.connect(self.db_path)) as connection:
            stored = connection.execute(
                "SELECT password_hash FROM admins WHERE username=?", (ADMIN_USERNAME,)
            ).fetchone()[0]
        self.assertTrue(stored.startswith("$argon2id$"))
        self.assertNotIn(ADMIN_PASSWORD, stored)

    def test_admin_cookie_csrf_and_audit_flow(self):
        login = self.login_admin()
        cookie = login.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        csrf_token = login.json()["data"]["csrf_token"]

        rejected = self.client.post(
            "/v1/admin/users",
            json={"username": "buyer@example.com", "password": "buyer-pass-123", "display_name": "买家"},
        )
        self.assertEqual(403, rejected.status_code)
        self.assertEqual("CSRF校验失败", rejected.json()["error"])

        created = self.client.post(
            "/v1/admin/users",
            headers={"X-CSRF-Token": csrf_token},
            json={"username": "buyer@example.com", "password": "buyer-pass-123", "display_name": "买家"},
        )
        self.assertEqual(200, created.status_code, created.text)
        self.assertEqual("buyer@example.com", created.json()["data"]["username"])

        audit = self.client.get("/v1/admin/audit-logs")
        self.assertEqual(200, audit.status_code, audit.text)
        entries = audit.json()["data"]
        self.assertTrue(
            any(
                item["action"] == "user.create" and item["actor"] == f"admin:{ADMIN_USERNAME}"
                for item in entries
            )
        )

    def test_me_rotates_csrf_and_logout_revokes_session(self):
        first_csrf = self.login_admin().json()["data"]["csrf_token"]
        me = self.client.get("/v1/admin/auth/me")
        self.assertEqual(200, me.status_code, me.text)
        second_csrf = me.json()["data"]["csrf_token"]
        self.assertNotEqual(first_csrf, second_csrf)

        old_token = self.client.post(
            "/v1/admin/users",
            headers={"X-CSRF-Token": first_csrf},
            json={"username": "old-token-user", "password": "buyer-pass-123"},
        )
        self.assertEqual(403, old_token.status_code)

        logout = self.client.post(
            "/v1/admin/auth/logout", headers={"X-CSRF-Token": second_csrf}
        )
        self.assertEqual(200, logout.status_code, logout.text)
        self.assertEqual(403, self.client.get("/v1/admin/users").status_code)

    def test_admin_login_rate_limit(self):
        statuses = []
        for _ in range(6):
            response = self.client.post(
                "/v1/admin/auth/login",
                json={"username": ADMIN_USERNAME, "password": "wrong-password"},
            )
            statuses.append(response.status_code)
        self.assertEqual([401, 401, 401, 401, 401, 429], statuses)

    def test_customer_login_and_verify_remain_compatible(self):
        user = self.store.create_user("customer@example.com", "customer-pass-123")
        self.store.grant_subscription(user["id"], "monthly")

        login = self.client.post(
            "/v1/auth/login",
            json={
                "username": "customer@example.com",
                "password": "customer-pass-123",
                "device_id": "device-compat-0001",
                "device_name": "Office PC",
            },
        )
        self.assertEqual(200, login.status_code, login.text)
        token = login.json()["data"]["token"]

        verify = self.client.post(
            "/v1/license/verify",
            json={"token": token, "device_id": "device-compat-0001"},
        )
        self.assertEqual(200, verify.status_code, verify.text)
        self.assertEqual("monthly", verify.json()["data"]["entitlement"]["plan_code"])

    def test_password_change_invalidates_all_admin_sessions(self):
        csrf_token = self.login_admin().json()["data"]["csrf_token"]
        changed = self.client.post(
            "/v1/admin/auth/change-password",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "current_password": ADMIN_PASSWORD,
                "new_password": "Changed-Admin#Password2027",
            },
        )
        self.assertEqual(200, changed.status_code, changed.text)
        self.assertEqual(403, self.client.get("/v1/admin/users").status_code)


if __name__ == "__main__":
    unittest.main()
