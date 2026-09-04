import os
from dataclasses import dataclass


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8787
    database_path: str = "cloud-license.db"
    public_origin: str = "https://api.yituan123.com"
    allowed_hosts: tuple = ("api.yituan123.com", "127.0.0.1", "localhost", "testserver")
    cookie_secure: bool = True
    admin_session_hours: int = 8
    max_body_bytes: int = 64 * 1024
    trust_proxy: bool = False

    @classmethod
    def from_env(cls):
        allowed = tuple(
            item.strip()
            for item in os.getenv(
                "LICENSE_ALLOWED_HOSTS", "api.yituan123.com,127.0.0.1,localhost,testserver"
            ).split(",")
            if item.strip()
        )
        return cls(
            host=os.getenv("LICENSE_HOST", "127.0.0.1"),
            port=int(os.getenv("LICENSE_PORT", "8787")),
            database_path=os.getenv("LICENSE_DB", "cloud-license.db"),
            public_origin=os.getenv("LICENSE_PUBLIC_ORIGIN", "https://api.yituan123.com").rstrip("/"),
            allowed_hosts=allowed,
            cookie_secure=env_bool("LICENSE_COOKIE_SECURE", True),
            admin_session_hours=max(1, min(int(os.getenv("LICENSE_ADMIN_SESSION_HOURS", "8")), 24)),
            max_body_bytes=max(4096, min(int(os.getenv("LICENSE_MAX_BODY_BYTES", str(64 * 1024))), 1024 * 1024)),
            trust_proxy=env_bool("LICENSE_TRUST_PROXY", False),
        )

