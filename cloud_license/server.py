import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from cloud_license.store import LicenseError, LicenseStore


class LicenseApi(BaseHTTPRequestHandler):
    store = None
    admin_key = ""

    def log_message(self, fmt, *args):
        print(fmt % args)

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, content):
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        length = min(int(self.headers.get("Content-Length", 0) or 0), 100_000)
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def require_admin(self):
        supplied = self.headers.get("X-Admin-Key", "")
        if not self.admin_key or not secrets_compare(supplied, self.admin_key):
            raise PermissionError("管理员凭证无效")

    def route(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        method = self.command
        body = self.body() if method in {"POST", "PUT", "PATCH"} else {}
        if method == "GET" and path == "/health":
            return {"status": "ok", "service": "xianyu-license", "version": "1.0.0-test"}
        if method == "POST" and path == "/v1/auth/login":
            return self.store.login(body.get("username"), body.get("password"), body.get("device_id"), body.get("device_name", ""))
        if method == "POST" and path == "/v1/license/verify":
            return self.store.verify_token(body.get("token"), body.get("device_id"))
        if method == "GET" and path == "/v1/admin/users":
            self.require_admin()
            return self.store.list_users()
        if method == "GET" and path == "/v1/admin/audit-logs":
            self.require_admin()
            return self.store.audit_logs()
        if method == "POST" and path == "/v1/admin/users":
            self.require_admin()
            return self.store.create_user(body.get("username"), body.get("password"), body.get("display_name", ""))
        if method == "POST" and path.startswith("/v1/admin/users/") and path.endswith("/subscription"):
            self.require_admin()
            user_id = int(path.split("/")[4])
            return self.store.grant_subscription(user_id, body.get("plan_code"), body.get("days"), body.get("note", ""))
        if method == "POST" and path.startswith("/v1/admin/users/") and path.endswith("/status"):
            self.require_admin()
            user_id = int(path.split("/")[4])
            return self.store.set_user_status(user_id, body.get("status"))
        if method == "POST" and path.startswith("/v1/admin/users/") and path.endswith("/devices/revoke"):
            self.require_admin()
            user_id = int(path.split("/")[4])
            self.store.revoke_device(user_id, body.get("device_id"))
            return {"revoked": True}
        raise FileNotFoundError("接口不存在")

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def handle_request(self):
        try:
            if self.command == "GET" and urlparse(self.path).path.rstrip("/") == "/admin":
                admin_path = os.path.join(os.path.dirname(__file__), "admin.html")
                with open(admin_path, "r", encoding="utf-8") as handle:
                    return self.send_html(handle.read())
            self.send_json(200, {"ok": True, "data": self.route()})
        except PermissionError as exc:
            self.send_json(403, {"ok": False, "error": str(exc)})
        except FileNotFoundError as exc:
            self.send_json(404, {"ok": False, "error": str(exc)})
        except (LicenseError, ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
        except Exception:
            self.send_json(500, {"ok": False, "error": "服务器内部错误"})


def secrets_compare(left, right):
    import hmac
    return hmac.compare_digest(str(left), str(right))


def main():
    parser = argparse.ArgumentParser(description="XianyuCardAI private subscription service")
    parser.add_argument("--host", default=os.getenv("LICENSE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("LICENSE_PORT", "8787")))
    parser.add_argument("--db", default=os.getenv("LICENSE_DB", "cloud-license.db"))
    args = parser.parse_args()
    admin_key = os.getenv("LICENSE_ADMIN_KEY", "")
    if len(admin_key) < 24:
        raise SystemExit("LICENSE_ADMIN_KEY must be at least 24 characters")
    LicenseApi.store = LicenseStore(args.db)
    LicenseApi.admin_key = admin_key
    server = ThreadingHTTPServer((args.host, args.port), LicenseApi)
    print(json.dumps({"ready": True, "host": args.host, "port": args.port, "db": os.path.abspath(args.db)}, ensure_ascii=False))
    server.serve_forever()


if __name__ == "__main__":
    main()
