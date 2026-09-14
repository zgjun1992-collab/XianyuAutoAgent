import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError


UTC = timezone.utc
ADMIN_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=2,
    hash_len=32,
    salt_len=16,
)


def utcnow():
    return datetime.now(UTC).replace(microsecond=0)


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_time(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def hash_secret(value, salt=None):
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(value.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_secret(value, encoded):
    try:
        _, salt, expected = encoded.split("$", 2)
        actual = hash_secret(value, bytes.fromhex(salt)).split("$", 2)[2]
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


class LicenseError(ValueError):
    pass


class LicenseStore:
    """SQLite implementation for private testing; the API boundary permits PostgreSQL later."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._initialize()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def connection(self):
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self):
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plans (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    duration_days INTEGER NOT NULL,
                    max_devices INTEGER NOT NULL DEFAULT 1,
                    max_xianyu_accounts INTEGER NOT NULL DEFAULT 1,
                    features TEXT NOT NULL DEFAULT 'desktop_ai',
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    plan_code TEXT NOT NULL REFERENCES plans(code),
                    starts_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_subscriptions_user
                    ON subscriptions(user_id, expires_at DESC);
                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    device_id TEXT NOT NULL,
                    device_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(user_id, device_id)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    device_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER NOT NULL REFERENCES admins(id),
                    token_hash TEXT NOT NULL UNIQUE,
                    csrf_hash TEXT NOT NULL,
                    ip_address TEXT NOT NULL DEFAULT '',
                    user_agent TEXT NOT NULL DEFAULT '',
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_admin_sessions_admin
                    ON admin_sessions(admin_id, expires_at DESC);
            """)
            conn.executemany(
                "INSERT OR IGNORE INTO plans(code,name,duration_days,max_devices,max_xianyu_accounts) VALUES(?,?,?,?,?)",
                [
                    ("trial", "3天体验版", 3, 1, 1),
                    ("weekly", "周卡", 7, 1, 1),
                    ("monthly", "月卡", 30, 1, 1),
                    ("quarterly", "季卡", 90, 1, 1),
                    ("yearly", "年卡", 365, 1, 1),
                ],
            )

    def _audit(self, conn, actor, action, target="", detail=""):
        conn.execute(
            "INSERT INTO audit_logs(actor,action,target,detail,created_at) VALUES(?,?,?,?,?)",
            (actor, action, target, detail, iso(utcnow())),
        )

    @staticmethod
    def _normalize_admin_username(username):
        username = str(username or "").strip().lower()
        if len(username) < 3 or len(username) > 80:
            raise LicenseError("管理员账号长度应为3至80位")
        return username

    @staticmethod
    def _validate_admin_password(password):
        password = str(password or "")
        categories = sum(
            (
                any(char.islower() for char in password),
                any(char.isupper() for char in password),
                any(char.isdigit() for char in password),
                any(not char.isalnum() for char in password),
            )
        )
        if len(password) < 14 or categories < 3:
            raise LicenseError("管理员密码至少14位，并包含大小写字母、数字、符号中的至少三类")
        return password

    def admin_count(self):
        with self.connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0])

    def create_admin(self, username, password, actor="bootstrap"):
        username = self._normalize_admin_username(username)
        password = self._validate_admin_password(password)
        now = iso(utcnow())
        try:
            with self.connection() as conn:
                cur = conn.execute(
                    "INSERT INTO admins(username,password_hash,created_at,updated_at) VALUES(?,?,?,?)",
                    (username, ADMIN_PASSWORD_HASHER.hash(password), now, now),
                )
                self._audit(conn, actor, "admin.create", str(cur.lastrowid), username)
        except sqlite3.IntegrityError as exc:
            raise LicenseError("管理员账号已存在") from exc
        return {"id": cur.lastrowid, "username": username, "status": "active", "created_at": now}

    def admin_login(self, username, password, ip_address="", user_agent="", session_hours=8):
        username = str(username or "").strip().lower()
        now = utcnow()
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
            if row and row["locked_until"] and parse_time(row["locked_until"]) > now:
                self._audit(conn, username or "unknown", "admin.login.blocked", detail=f"ip={ip_address[:64]}")
                raise LicenseError("登录失败，请稍后重试")
            verified = False
            if row and row["status"] == "active":
                try:
                    verified = ADMIN_PASSWORD_HASHER.verify(row["password_hash"], str(password or ""))
                except (VerifyMismatchError, InvalidHashError):
                    verified = False
            if not verified:
                if row:
                    failures = int(row["failed_attempts"] or 0) + 1
                    locked_until = iso(now + timedelta(minutes=15)) if failures >= 5 else None
                    conn.execute(
                        "UPDATE admins SET failed_attempts=?,locked_until=?,updated_at=? WHERE id=?",
                        (0 if locked_until else failures, locked_until, iso(now), row["id"]),
                    )
                self._audit(conn, username or "unknown", "admin.login.failed", detail=f"ip={ip_address[:64]}")
                raise LicenseError("管理员账号或密码错误")
            if ADMIN_PASSWORD_HASHER.check_needs_rehash(row["password_hash"]):
                conn.execute(
                    "UPDATE admins SET password_hash=?,updated_at=? WHERE id=?",
                    (ADMIN_PASSWORD_HASHER.hash(str(password)), iso(now), row["id"]),
                )
            conn.execute(
                "UPDATE admins SET failed_attempts=0,locked_until=NULL,updated_at=? WHERE id=?",
                (iso(now), row["id"]),
            )
            token = secrets.token_urlsafe(48)
            csrf_token = secrets.token_urlsafe(32)
            expires_at = iso(now + timedelta(hours=max(1, min(int(session_hours), 24))))
            conn.execute(
                """
                INSERT INTO admin_sessions(
                    admin_id,token_hash,csrf_hash,ip_address,user_agent,
                    expires_at,last_seen_at,created_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    row["id"],
                    hashlib.sha256(token.encode()).hexdigest(),
                    hashlib.sha256(csrf_token.encode()).hexdigest(),
                    str(ip_address or "")[:64],
                    str(user_agent or "")[:300],
                    expires_at,
                    iso(now),
                    iso(now),
                ),
            )
            self._audit(conn, username, "admin.login.success", detail=f"ip={ip_address[:64]}")
        return {
            "session_token": token,
            "csrf_token": csrf_token,
            "expires_at": expires_at,
            "admin": {"id": row["id"], "username": row["username"]},
        }

    def authenticate_admin(self, session_token, csrf_token=None, require_csrf=False, touch=True):
        token_hash = hashlib.sha256(str(session_token or "").encode()).hexdigest()
        now = iso(utcnow())
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT s.*,a.username,a.status AS admin_status
                FROM admin_sessions s JOIN admins a ON a.id=s.admin_id
                WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>?
                """,
                (token_hash, now),
            ).fetchone()
            if not row or row["admin_status"] != "active":
                raise PermissionError("管理员登录已失效")
            if require_csrf:
                supplied = hashlib.sha256(str(csrf_token or "").encode()).hexdigest()
                if not csrf_token or not hmac.compare_digest(supplied, row["csrf_hash"]):
                    raise PermissionError("CSRF校验失败")
            if touch:
                conn.execute("UPDATE admin_sessions SET last_seen_at=? WHERE id=?", (now, row["id"]))
        return {"id": row["admin_id"], "username": row["username"], "session_id": row["id"]}

    def rotate_admin_csrf(self, session_token):
        admin = self.authenticate_admin(session_token, touch=False)
        csrf_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(str(session_token).encode()).hexdigest()
        with self.connection() as conn:
            conn.execute(
                "UPDATE admin_sessions SET csrf_hash=?,last_seen_at=? WHERE token_hash=?",
                (hashlib.sha256(csrf_token.encode()).hexdigest(), iso(utcnow()), token_hash),
            )
        return admin, csrf_token

    def revoke_admin_session(self, session_token, actor="admin"):
        token_hash = hashlib.sha256(str(session_token or "").encode()).hexdigest()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT id,admin_id FROM admin_sessions WHERE token_hash=? AND revoked_at IS NULL",
                (token_hash,),
            ).fetchone()
            if row:
                conn.execute("UPDATE admin_sessions SET revoked_at=? WHERE id=?", (iso(utcnow()), row["id"]))
                self._audit(conn, actor, "admin.logout", str(row["admin_id"]))

    def change_admin_password(self, admin_id, current_password, new_password, actor):
        new_password = self._validate_admin_password(new_password)
        with self.connection() as conn:
            row = conn.execute("SELECT password_hash FROM admins WHERE id=?", (admin_id,)).fetchone()
            try:
                valid = bool(row) and ADMIN_PASSWORD_HASHER.verify(row["password_hash"], str(current_password or ""))
            except (VerifyMismatchError, InvalidHashError):
                valid = False
            if not valid:
                raise LicenseError("当前管理员密码错误")
            now = iso(utcnow())
            conn.execute(
                "UPDATE admins SET password_hash=?,updated_at=? WHERE id=?",
                (ADMIN_PASSWORD_HASHER.hash(new_password), now, admin_id),
            )
            conn.execute(
                "UPDATE admin_sessions SET revoked_at=? WHERE admin_id=? AND revoked_at IS NULL",
                (now, admin_id),
            )
            self._audit(conn, actor, "admin.password.change", str(admin_id))

    def create_user(self, username, password, display_name="", actor="admin"):
        username = str(username or "").strip().lower()
        if len(username) < 3 or len(password or "") < 8:
            raise LicenseError("账号至少3位，密码至少8位")
        now = iso(utcnow())
        try:
            with self.connection() as conn:
                cur = conn.execute(
                    "INSERT INTO users(username,password_hash,display_name,created_at) VALUES(?,?,?,?)",
                    (username, hash_secret(password), str(display_name or "").strip(), now),
                )
                user_id = cur.lastrowid
                self._audit(conn, actor, "user.create", str(user_id), username)
        except sqlite3.IntegrityError as exc:
            raise LicenseError("账号已存在") from exc
        return self.get_user(user_id)

    def get_user(self, user_id):
        with self.connection() as conn:
            row = conn.execute(
                "SELECT id,username,display_name,status,created_at FROM users WHERE id=?", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_users(self):
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT u.id,u.username,u.display_name,u.status,u.created_at,
                       s.plan_code,s.expires_at,s.status AS subscription_status
                FROM users u LEFT JOIN subscriptions s ON s.id=(
                    SELECT id FROM subscriptions WHERE user_id=u.id ORDER BY expires_at DESC LIMIT 1
                ) ORDER BY u.id DESC
            """).fetchall()
        return [dict(row) for row in rows]

    def set_user_status(self, user_id, status, actor="admin"):
        if status not in {"active", "frozen"}:
            raise LicenseError("用户状态无效")
        with self.connection() as conn:
            if not conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise LicenseError("用户不存在")
            conn.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))
            if status == "frozen":
                conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (iso(utcnow()), user_id))
            self._audit(conn, actor, f"user.{status}", str(user_id))
        return self.get_user(user_id)

    def grant_subscription(self, user_id, plan_code, days=None, note="", actor="admin"):
        now = utcnow()
        with self.connection() as conn:
            user = conn.execute("SELECT status FROM users WHERE id=?", (user_id,)).fetchone()
            plan = conn.execute("SELECT * FROM plans WHERE code=? AND enabled=1", (plan_code,)).fetchone()
            if not user:
                raise LicenseError("用户不存在")
            if not plan:
                raise LicenseError("套餐不存在或已停用")
            duration = int(days or plan["duration_days"])
            if duration < 1 or duration > 3660:
                raise LicenseError("开通天数无效")
            latest = conn.execute(
                "SELECT expires_at FROM subscriptions WHERE user_id=? AND status='active' ORDER BY expires_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            start = max(now, parse_time(latest["expires_at"])) if latest else now
            expires = start + timedelta(days=duration)
            stamp = iso(now)
            cur = conn.execute("""
                INSERT INTO subscriptions(user_id,plan_code,starts_at,expires_at,status,note,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)
            """, (user_id, plan_code, iso(start), iso(expires), "active", str(note or ""), stamp, stamp))
            self._audit(conn, actor, "subscription.grant", str(cur.lastrowid), f"user={user_id};plan={plan_code};days={duration}")
        return self.entitlement_for_user(user_id)

    def entitlement_for_user(self, user_id):
        with self.connection() as conn:
            row = conn.execute("""
                SELECT s.id,s.plan_code,s.starts_at,s.expires_at,s.status,p.name,p.max_devices,
                       p.max_xianyu_accounts,p.features
                FROM subscriptions s JOIN plans p ON p.code=s.plan_code
                WHERE s.user_id=? AND s.status='active' AND s.expires_at>?
                ORDER BY s.expires_at DESC LIMIT 1
            """, (user_id, iso(utcnow()))).fetchone()
        if not row:
            return {"active": False, "reason": "subscription_expired"}
        result = dict(row)
        result.update({"active": True, "offline_grace_hours": 48})
        return result

    def login(self, username, password, device_id, device_name=""):
        username = str(username or "").strip().lower()
        device_id = str(device_id or "").strip()
        if len(device_id) < 8:
            raise LicenseError("设备标识无效")
        with self.connection() as conn:
            user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if not user or not verify_secret(str(password or ""), user["password_hash"]):
                self._audit(conn, username or "unknown", "login.failed", device_id)
                raise LicenseError("账号或密码错误")
            if user["status"] != "active":
                raise LicenseError("账号已被冻结")
            entitlement = self.entitlement_for_user(user["id"])
            if not entitlement["active"]:
                raise LicenseError("套餐未开通或已到期")
            existing = conn.execute(
                "SELECT * FROM devices WHERE user_id=? AND device_id=?", (user["id"], device_id)
            ).fetchone()
            active_count = conn.execute(
                "SELECT COUNT(*) FROM devices WHERE user_id=? AND status='active'", (user["id"],)
            ).fetchone()[0]
            if not existing and active_count >= int(entitlement["max_devices"]):
                raise LicenseError("设备数量已达套餐上限，请先联系管理员解绑")
            now = iso(utcnow())
            if existing:
                if existing["status"] != "active":
                    raise LicenseError("当前设备已被冻结")
                conn.execute("UPDATE devices SET device_name=?,last_seen_at=? WHERE id=?", (device_name, now, existing["id"]))
            else:
                conn.execute(
                    "INSERT INTO devices(user_id,device_id,device_name,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                    (user["id"], device_id, str(device_name or "")[:120], now, now),
                )
            token = secrets.token_urlsafe(48)
            conn.execute("""
                INSERT INTO sessions(user_id,device_id,token_hash,expires_at,created_at,last_seen_at)
                VALUES(?,?,?,?,?,?)
            """, (user["id"], device_id, hashlib.sha256(token.encode()).hexdigest(), iso(utcnow() + timedelta(days=30)), now, now))
            self._audit(conn, username, "login.success", device_id)
        return {"token": token, "user": self.get_user(user["id"]), "entitlement": entitlement}

    def verify_token(self, token, device_id, refresh=True):
        token_hash = hashlib.sha256(str(token or "").encode()).hexdigest()
        with self.connection() as conn:
            row = conn.execute("""
                SELECT s.*,u.username,u.display_name,u.status AS user_status
                FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.device_id=? AND s.revoked_at IS NULL AND s.expires_at>?
            """, (token_hash, str(device_id or ""), iso(utcnow()))).fetchone()
            if not row or row["user_status"] != "active":
                raise LicenseError("登录已失效，请重新登录")
            device = conn.execute(
                "SELECT status FROM devices WHERE user_id=? AND device_id=?", (row["user_id"], device_id)
            ).fetchone()
            if not device or device["status"] != "active":
                raise LicenseError("设备授权已失效")
            entitlement = self.entitlement_for_user(row["user_id"])
            if not entitlement["active"]:
                raise LicenseError("套餐已到期")
            now = iso(utcnow())
            conn.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (now, row["id"]))
            conn.execute("UPDATE devices SET last_seen_at=? WHERE user_id=? AND device_id=?", (now, row["user_id"], device_id))
        return {
            "user": {"id": row["user_id"], "username": row["username"], "display_name": row["display_name"]},
            "entitlement": entitlement,
            "checked_at": iso(utcnow()),
        }

    def revoke_device(self, user_id, device_id, actor="admin"):
        now = iso(utcnow())
        with self.connection() as conn:
            existing = conn.execute(
                "SELECT 1 FROM devices WHERE user_id=? AND device_id=?", (user_id, device_id)
            ).fetchone()
            if not existing:
                raise LicenseError("设备不存在")
            conn.execute("UPDATE devices SET status='revoked' WHERE user_id=? AND device_id=?", (user_id, device_id))
            conn.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND device_id=? AND revoked_at IS NULL", (now, user_id, device_id))
            self._audit(conn, actor, "device.revoke", device_id, f"user={user_id}")

    def list_devices(self, user_id):
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id,device_id,device_name,status,first_seen_at,last_seen_at
                FROM devices WHERE user_id=? ORDER BY id DESC
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def purge_expired_sessions(self):
        now = iso(utcnow())
        with self.connection() as conn:
            client = conn.execute("DELETE FROM sessions WHERE expires_at<?", (now,)).rowcount
            admin = conn.execute("DELETE FROM admin_sessions WHERE expires_at<?", (now,)).rowcount
            self._audit(conn, "system", "sessions.purge", detail=f"client={client};admin={admin}")
        return {"client_sessions": client, "admin_sessions": admin}

    def audit_logs(self, limit=200):
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (min(int(limit), 500),)).fetchall()
        return [dict(row) for row in rows]
