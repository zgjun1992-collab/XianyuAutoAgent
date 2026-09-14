import argparse
import hashlib
import logging
import os
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from cloud_license.config import Settings
from cloud_license.rate_limit import SlidingWindowRateLimiter
from cloud_license.store import LicenseError, LicenseStore


LOGGER = logging.getLogger("xianyu-license")
COOKIE_NAME = "xya_admin_session"
ASSET_DIR = Path(__file__).resolve().parent / "admin_assets"
ADMIN_HTML = Path(__file__).resolve().parent / "admin.html"


class CustomerLogin(BaseModel):
    username: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=1, max_length=512)
    device_id: str = Field(min_length=8, max_length=200)
    device_name: str = Field(default="", max_length=120)


class LicenseVerify(BaseModel):
    token: str = Field(min_length=20, max_length=512)
    device_id: str = Field(min_length=8, max_length=200)


class AdminLogin(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=1, max_length=512)


class AdminPasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=8, max_length=512)


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=160)
    password: str = Field(min_length=8, max_length=512)
    display_name: str = Field(default="", max_length=120)


class SubscriptionGrant(BaseModel):
    plan_code: str = Field(pattern="^(trial|weekly|monthly|quarterly|yearly)$")
    days: int | None = Field(default=None, ge=1, le=3660)
    note: str = Field(default="", max_length=500)


class UserStatusChange(BaseModel):
    status: str = Field(pattern="^(active|frozen)$")


class DeviceRevoke(BaseModel):
    device_id: str = Field(min_length=8, max_length=200)


def envelope(data):
    return {"ok": True, "data": data}


def client_ip(request, settings):
    if settings.trust_proxy and request.client and request.client.host in {"127.0.0.1", "::1"}:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded[:64]
    return (request.client.host if request.client else "unknown")[:64]


def require_rate(app, bucket, key, limit, window_seconds):
    allowed, retry_after = app.state.rate_limiter.allow(bucket, key, limit, window_seconds)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后重试",
            headers={"Retry-After": str(retry_after)},
        )


def session_token(request):
    return request.cookies.get(COOKIE_NAME, "")


def admin_read(request: Request):
    return request.app.state.store.authenticate_admin(session_token(request))


def admin_write(request: Request):
    return request.app.state.store.authenticate_admin(
        session_token(request), request.headers.get("x-csrf-token", ""), require_csrf=True
    )


def create_app(store=None, settings=None):
    settings = settings or Settings.from_env()
    store = store or LicenseStore(settings.database_path)
    app = FastAPI(
        title="XianyuCardAI License Service",
        version="1.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.rate_limiter = SlidingWindowRateLimiter()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))

    @app.middleware("http")
    async def security_middleware(request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > settings.max_body_bytes:
                    return JSONResponse({"ok": False, "error": "请求内容过大"}, status_code=413)
            except ValueError:
                return JSONResponse({"ok": False, "error": "Content-Length无效"}, status_code=400)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/admin"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
        return response

    @app.exception_handler(LicenseError)
    async def license_error_handler(_request, exc):
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @app.exception_handler(PermissionError)
    async def permission_error_handler(_request, exc):
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request, _exc):
        return JSONResponse({"ok": False, "error": "请求参数无效"}, status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error_handler(_request, exc):
        response = JSONResponse({"ok": False, "error": str(exc.detail)}, status_code=exc.status_code)
        for name, value in (exc.headers or {}).items():
            response.headers[name] = value
        return response

    @app.get("/")
    async def root():
        return RedirectResponse("/admin", status_code=307)

    @app.get("/health")
    async def health():
        return envelope(
            {"status": "ok", "service": "xianyu-license", "version": "1.1.0", "database": "sqlite"}
        )

    @app.get("/admin", response_class=HTMLResponse)
    @app.get("/admin/", response_class=HTMLResponse)
    async def admin_page():
        return FileResponse(ADMIN_HTML, media_type="text/html; charset=utf-8")

    @app.get("/admin/assets/{filename}")
    async def admin_asset(filename: str):
        if filename not in {"admin.css", "admin.js"}:
            raise HTTPException(status_code=404, detail="资源不存在")
        media_type = "text/css" if filename.endswith(".css") else "application/javascript"
        return FileResponse(ASSET_DIR / filename, media_type=media_type)

    @app.post("/v1/auth/login")
    async def customer_login(payload: CustomerLogin, request: Request):
        ip = client_ip(request, settings)
        identity = f"{ip}:{payload.username.strip().lower()}"
        require_rate(app, "customer-login", identity, 10, 10 * 60)
        return envelope(store.login(payload.username, payload.password, payload.device_id, payload.device_name))

    @app.post("/v1/license/verify")
    async def verify_license(payload: LicenseVerify, request: Request):
        ip = client_ip(request, settings)
        token_key = hashlib.sha256(payload.token.encode()).hexdigest()[:16]
        require_rate(app, "license-verify", f"{ip}:{token_key}", 120, 60)
        return envelope(store.verify_token(payload.token, payload.device_id))

    @app.post("/v1/admin/auth/login")
    async def admin_login(payload: AdminLogin, request: Request, response: Response):
        ip = client_ip(request, settings)
        identity = f"{ip}:{payload.username.strip().lower()}"
        require_rate(app, "admin-login", identity, 5, 15 * 60)
        try:
            result = store.admin_login(
                payload.username,
                payload.password,
                ip_address=ip,
                user_agent=request.headers.get("user-agent", ""),
                session_hours=settings.admin_session_hours,
            )
        except LicenseError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        response.set_cookie(
            COOKIE_NAME,
            result.pop("session_token"),
            max_age=settings.admin_session_hours * 3600,
            secure=settings.cookie_secure,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return envelope(result)

    @app.get("/v1/admin/auth/me")
    async def admin_me(request: Request):
        admin, csrf_token = store.rotate_admin_csrf(session_token(request))
        return envelope({"admin": admin, "csrf_token": csrf_token})

    @app.post("/v1/admin/auth/logout")
    async def admin_logout(request: Request, response: Response, admin=Depends(admin_write)):
        store.revoke_admin_session(session_token(request), actor=f"admin:{admin['username']}")
        response.delete_cookie(
            COOKIE_NAME, path="/", secure=settings.cookie_secure, httponly=True, samesite="strict"
        )
        return envelope({"logged_out": True})

    @app.post("/v1/admin/auth/change-password")
    async def admin_change_password(
        payload: AdminPasswordChange,
        request: Request,
        response: Response,
        admin=Depends(admin_write),
    ):
        store.change_admin_password(
            admin["id"], payload.current_password, payload.new_password, actor=f"admin:{admin['username']}"
        )
        response.delete_cookie(
            COOKIE_NAME, path="/", secure=settings.cookie_secure, httponly=True, samesite="strict"
        )
        return envelope({"password_changed": True, "login_required": True})

    @app.get("/v1/admin/users")
    async def users_list(request: Request, _admin=Depends(admin_read)):
        require_rate(app, "admin-read", client_ip(request, settings), 180, 60)
        return envelope(store.list_users())

    @app.post("/v1/admin/users")
    async def users_create(payload: UserCreate, admin=Depends(admin_write)):
        actor = f"admin:{admin['username']}"
        return envelope(store.create_user(payload.username, payload.password, payload.display_name, actor=actor))

    @app.post("/v1/admin/users/{user_id}/subscription")
    async def subscription_grant(user_id: int, payload: SubscriptionGrant, admin=Depends(admin_write)):
        actor = f"admin:{admin['username']}"
        return envelope(
            store.grant_subscription(user_id, payload.plan_code, payload.days, payload.note, actor=actor)
        )

    @app.post("/v1/admin/users/{user_id}/status")
    async def user_status(user_id: int, payload: UserStatusChange, admin=Depends(admin_write)):
        actor = f"admin:{admin['username']}"
        return envelope(store.set_user_status(user_id, payload.status, actor=actor))

    @app.get("/v1/admin/users/{user_id}/devices")
    async def user_devices(user_id: int, _admin=Depends(admin_read)):
        return envelope(store.list_devices(user_id))

    @app.post("/v1/admin/users/{user_id}/devices/revoke")
    async def device_revoke(user_id: int, payload: DeviceRevoke, admin=Depends(admin_write)):
        actor = f"admin:{admin['username']}"
        store.revoke_device(user_id, payload.device_id, actor=actor)
        return envelope({"revoked": True})

    @app.get("/v1/admin/audit-logs")
    async def audit_logs(limit: int = Query(default=200, ge=1, le=500), _admin=Depends(admin_read)):
        return envelope(store.audit_logs(limit))

    return app


def main():
    parser = argparse.ArgumentParser(description="XianyuCardAI production license service")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.host or args.port or args.db:
        settings = Settings(
            host=args.host or settings.host,
            port=args.port or settings.port,
            database_path=args.db or settings.database_path,
            public_origin=settings.public_origin,
            allowed_hosts=settings.allowed_hosts,
            cookie_secure=settings.cookie_secure,
            admin_session_hours=settings.admin_session_hours,
            max_body_bytes=settings.max_body_bytes,
            trust_proxy=settings.trust_proxy,
        )
    store = LicenseStore(settings.database_path)
    if store.admin_count() == 0:
        LOGGER.warning("No administrator exists. Create one with: python -m cloud_license.admin_cli create-admin")
    uvicorn.run(
        create_app(store=store, settings=settings),
        host=settings.host,
        port=settings.port,
        proxy_headers=settings.trust_proxy,
        forwarded_allow_ips="127.0.0.1" if settings.trust_proxy else "",
        server_header=False,
        timeout_keep_alive=5,
        limit_concurrency=200,
        log_level=os.getenv("LICENSE_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
