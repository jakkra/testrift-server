"""Authentication, sessions, lockout, and permission checks."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote, urlparse

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.websockets import WebSocket

from . import database

logger = logging.getLogger(__name__)
UTC = timezone.utc

SESSION_COOKIE_NAME = "testrift_session"
INGEST_TOKEN_HEADER = "x-testrift-ingest-token"
LOGIN_ERROR_MESSAGE = "Invalid username or password."
INGEST_TOKEN_ERROR_MESSAGE = "Invalid ingest token."
INGEST_TLS_REQUIRED_MESSAGE = "This endpoint requires HTTPS on the ingest listener."
VALID_ROLES = frozenset({"member", "admin"})
PERM_RUNS_READ = "runs.read"
PERM_CATALOG_WRITE = "catalog.write"
PERM_ADMIN_ACCESS = "admin.access"

ROLE_PERMISSIONS = {
    "admin": frozenset({PERM_RUNS_READ, PERM_CATALOG_WRITE, PERM_ADMIN_ACCESS}),
    "member": frozenset({PERM_RUNS_READ, PERM_CATALOG_WRITE}),
}

# Used by tests to skip the lockout delay.
LOCKOUT_DELAY_RANGE = (0.2, 0.5)

_password_hasher = PasswordHasher()
_username_locks: dict[str, asyncio.Lock] = {}
_username_locks_guard = asyncio.Lock()
_auth_context: ContextVar[dict[str, Any]] = ContextVar("auth_context", default=None)
_cached_session_secret: Optional[str] = None

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_PUBLIC_EXACT = frozenset({
    ("GET", "/health"),
    ("GET", "/login"),
    ("POST", "/login"),
    ("GET", "/logout"),
    ("POST", "/logout"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/server-info"),
    ("GET", "/ca.crt"),
    ("POST", "/api/admin/shutdown"),
    ("GET", "/auth/oidc/login"),
    ("GET", "/auth/oidc/callback"),
})
_PUBLIC_PREFIXES = ("/static/",)
_AUTH_ONLY_EXACT = frozenset({
    "/login",
    "/logout",
    "/users",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
})
_AUTH_ONLY_PREFIXES = ("/api/users", "/auth/oidc")
_ADMIN_EXACT = frozenset({
    "/settings",
    "/logs",
    "/users",
    "/api/logs",
    "/api/migrate-data",
    "/api/settings/email-recipients",
    "/api/settings/ai-usage",
})
_ADMIN_PREFIXES = ("/api/users",)
_CATALOG_PREFIXES = ("/api/targets", "/api/collections", "/api/profiles")


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    return dt.isoformat() + "Z"


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.rstrip("Z"))


def auth_config() -> dict:
    from . import config as config_mod
    return config_mod.AUTH_CONFIG


def is_auth_enabled() -> bool:
    return bool(auth_config().get("enabled"))


def permissions_for_role(role: str) -> frozenset[str]:
    """Map a stored role string to permissions. Unknown roles are treated as member."""
    if role in ROLE_PERMISSIONS:
        return ROLE_PERMISSIONS[role]
    logger.warning("Unknown user role %r; treating as member", role)
    return ROLE_PERMISSIONS["member"]


def has_permission(permissions: Optional[frozenset[str]], permission: str) -> bool:
    return bool(permissions) and permission in permissions


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, ValueError, TypeError):
        return False


def public_user(user: dict) -> dict:
    """User fields safe to return in APIs and templates."""
    return {
        "id": user["id"],
        "display_name": user.get("display_name") or user.get("username") or "",
        "email": user.get("email"),
        "username": user.get("username"),
        "role": user.get("role"),
        "enabled": bool(user.get("enabled")),
        "auth_source": user.get("auth_source"),
        "last_login_at": user.get("last_login_at"),
    }


def template_auth_context() -> dict[str, Any]:
    ctx = _auth_context.get()
    if ctx:
        return ctx
    return {
        "auth_enabled": is_auth_enabled(),
        "current_user": None,
        "show_admin_nav": not is_auth_enabled(),
    }


def _set_template_context(user: Optional[dict], permissions: Optional[frozenset[str]]) -> None:
    enabled = is_auth_enabled()
    if not enabled:
        _auth_context.set({
            "auth_enabled": False,
            "current_user": None,
            "show_admin_nav": True,
        })
        return
    _auth_context.set({
        "auth_enabled": True,
        "current_user": public_user(user) if user else None,
        "show_admin_nav": has_permission(permissions, PERM_ADMIN_ACCESS),
    })


def required_permission(method: str, path: str) -> Optional[str]:
    """Return the permission required for a route, or None if it is public."""
    method = method.upper()
    if (method, path) in _PUBLIC_EXACT:
        return None
    if any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES):
        return None
    if method == "POST" and _is_ingest_post(path):
        return None
    if path in _ADMIN_EXACT or any(path == prefix or path.startswith(prefix + "/") for prefix in _ADMIN_PREFIXES):
        return PERM_ADMIN_ACCESS
    if method in _WRITE_METHODS and any(path == prefix or path.startswith(prefix + "/") for prefix in _CATALOG_PREFIXES):
        return PERM_CATALOG_WRITE
    return PERM_RUNS_READ


def admin_protected_paths() -> frozenset[str]:
    """Exact HTML/API paths that must require admin.access (for tests)."""
    return _ADMIN_EXACT | frozenset({"/api/users"})


def _is_ingest_post(path: str) -> bool:
    if re.match(r"^/api/attachments/[^/]+/[^/]+/upload$", path):
        return True
    if re.match(r"^/api/runs/[^/]+/commits$", path):
        return True
    if re.match(r"^/api/runs/[^/]+/kpis$", path):
        return True
    return False


def ingest_token_configured() -> str:
    """Return the configured ingest token, or empty if ingest stays open."""
    return (auth_config().get("ingest_token") or "").strip()


def extract_ingest_token(headers) -> str:
    """Read the ingest token from request or WebSocket headers."""
    if headers is None:
        return ""
    custom = (headers.get(INGEST_TOKEN_HEADER) or "").strip()
    if custom:
        return custom
    authorization = (headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def ingest_token_matches(provided: str) -> bool:
    expected = ingest_token_configured()
    if not expected:
        return True
    provided = provided or ""
    return hmac.compare_digest(
        hashlib.sha256(provided.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def _ingest_unauthorized_response() -> JSONResponse:
    return JSONResponse({"success": False, "error": INGEST_TOKEN_ERROR_MESSAGE}, status_code=401)


def _ingest_tls_response() -> JSONResponse:
    return JSONResponse({"success": False, "error": INGEST_TLS_REQUIRED_MESSAGE}, status_code=400)


def ingest_tls_denied(request) -> Optional[Response]:
    """Reject test-client ingest HTTP when ingest TLS is on but the request is not HTTPS."""
    from .tls_certs import ingest_tls_enabled
    if not ingest_tls_enabled():
        return None
    scheme = (getattr(request, "scheme", None) or "http").lower()
    if scheme == "https":
        return None
    method = (request.method or "GET").upper()
    path = request.path or "/"
    if method == "POST" and _is_ingest_post(path):
        return _ingest_tls_response()
    return None


def ingest_request_denied(request) -> Optional[Response]:
    """Reject test-client ingest HTTP when a token is configured and missing/wrong."""
    if not ingest_token_configured():
        return None
    method = (request.method or "GET").upper()
    path = request.path or "/"
    if not (method == "POST" and _is_ingest_post(path)):
        return None
    if ingest_token_matches(extract_ingest_token(getattr(request, "headers", None))):
        return None
    return _ingest_unauthorized_response()


def _is_auth_only_path(path: str) -> bool:
    if path in _AUTH_ONLY_EXACT:
        return True
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _AUTH_ONLY_PREFIXES)


def _not_found_response(request) -> Response:
    if _is_html_request(request):
        return Response(content="Not found", status_code=404, media_type="text/plain")
    return JSONResponse({"success": False, "error": "Not found"}, status_code=404)


def _is_html_request(request) -> bool:
    path = request.path or ""
    if path.startswith("/api/"):
        return False
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept and "text/html" not in accept:
        return False
    return True


def _session_secret() -> str:
    global _cached_session_secret
    if _cached_session_secret:
        return _cached_session_secret
    from . import config as config_mod
    path = config_mod.DATA_DIR / ".session_secret"
    try:
        if path.exists():
            secret = path.read_text(encoding="utf-8").strip()
            if secret:
                _cached_session_secret = secret
                return secret
        path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_hex(32)
        path.write_text(secret, encoding="utf-8")
        _cached_session_secret = secret
        return secret
    except OSError:
        _cached_session_secret = secrets.token_hex(32)
        return _cached_session_secret


def reset_session_secret_cache() -> None:
    global _cached_session_secret
    _cached_session_secret = None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _sign_token(token: str) -> str:
    signature = hmac.new(
        _session_secret().encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{token}.{signature}"


def _parse_signed_token(value: str) -> Optional[str]:
    if not value or "." not in value:
        return None
    token, signature = value.rsplit(".", 1)
    expected = hmac.new(
        _session_secret().encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    return token


def safe_next_url(value: Optional[str]) -> str:
    if not value:
        return "/"
    candidate = value.strip()
    if not candidate.startswith("/") or candidate.startswith("//") or "\\" in candidate:
        return "/"
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return "/"
    return candidate


async def _username_lock(username: str) -> asyncio.Lock:
    async with _username_locks_guard:
        lock = _username_locks.get(username)
        if lock is None:
            lock = asyncio.Lock()
            _username_locks[username] = lock
        return lock


async def _lockout_delay() -> None:
    low, high = LOCKOUT_DELAY_RANGE
    if high <= 0:
        return
    delay = low if low == high else secrets.SystemRandom().uniform(low, high)
    await asyncio.sleep(delay)


def _lockout_window_start() -> str:
    minutes = float(auth_config().get("lockout_minutes") or 15)
    return _iso(_utcnow() - timedelta(minutes=minutes))


async def username_lockout_info(username: str) -> Optional[dict]:
    """Return lockout info for Admins, or None if the username is not locked."""
    cfg = auth_config()
    limit = int(cfg.get("lockout_failures") or 0)
    if limit <= 0:
        return None
    normalized = normalize_username(username)
    if not normalized:
        return None
    since = _lockout_window_start()
    count = await database.db.count_login_failures(username=normalized, since=since)
    if count < limit:
        return None
    earliest = await database.db.earliest_login_failure(username=normalized, since=since)
    until = None
    if earliest:
        until = _iso(_parse_iso(earliest) + timedelta(minutes=float(cfg.get("lockout_minutes") or 15)))
    return {"locked": True, "until": until, "failures": count}


async def _is_locked_out(username: str, client_ip: str) -> bool:
    cfg = auth_config()
    since = _lockout_window_start()
    user_limit = int(cfg.get("lockout_failures") or 0)
    if user_limit > 0 and username:
        if await database.db.count_login_failures(username=username, since=since) >= user_limit:
            return True
    ip_limit = int(cfg.get("ip_lockout_failures") or 0)
    if ip_limit > 0 and client_ip:
        if await database.db.count_login_failures(client_ip=client_ip, since=since) >= ip_limit:
            return True
    return False


async def bootstrap_admin_if_needed() -> None:
    """Create the configured bootstrap Admin when auth is on and none exists."""
    cfg = auth_config()
    if not cfg.get("enabled"):
        return
    if await database.db.count_enabled_admins() > 0:
        return
    password = (cfg.get("bootstrap_admin") or {}).get("password") or ""
    username = normalize_username((cfg.get("bootstrap_admin") or {}).get("username") or "admin")
    if not password:
        from .oidc import oidc_can_provision_admin
        if oidc_can_provision_admin():
            logger.info("No local Admin yet; the first SSO user mapped to admin will become Admin")
            return
        raise RuntimeError(
            "auth.enabled is true but no Admin user exists and auth.bootstrap_admin.password is empty. "
            "Set TESTRIFT_BOOTSTRAP_ADMIN_PASSWORD, or enable OIDC with a role_map entry for admin."
        )
    min_length = int(cfg.get("password_min_length") or 8)
    if len(password) < min_length:
        raise RuntimeError(
            f"auth.bootstrap_admin.password must be at least {min_length} characters."
        )
    now = _iso(_utcnow())
    await database.db.create_user(
        display_name=username,
        email=None,
        username=username,
        password_hash=hash_password(password),
        role="admin",
        enabled=True,
        auth_source="local",
        created_at=now,
    )
    logger.info("Created bootstrap Admin user %s", username)


async def create_session_cookie_value(user_id: int) -> str:
    cfg = auth_config()
    now = _utcnow()
    token = secrets.token_urlsafe(32)
    await database.db.create_session(
        token_hash=_token_hash(token),
        user_id=user_id,
        created_at=_iso(now),
        last_seen_at=_iso(now),
        expires_at=_iso(now + timedelta(days=float(cfg.get("session_max_days") or 7))),
    )
    return _sign_token(token)


def apply_session_cookie(response: Response, cookie_value: str) -> None:
    cfg = auth_config()
    max_age = int(float(cfg.get("session_max_days") or 7) * 24 * 60 * 60)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        cookie_value,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        path="/",
        secure=_cookie_secure(),
    )


def _cookie_secure() -> bool:
    from .tls_certs import ui_tls_enabled
    return ui_tls_enabled()


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


async def resolve_session_user(cookie_value: Optional[str]) -> Optional[dict]:
    token = _parse_signed_token(cookie_value or "")
    if not token:
        return None
    session = await database.db.get_session_by_token_hash(_token_hash(token))
    if not session:
        return None
    now = _utcnow()
    try:
        expires_at = _parse_iso(session["expires_at"])
        last_seen = _parse_iso(session["last_seen_at"])
    except (TypeError, ValueError):
        await database.db.delete_session(session["token_hash"])
        return None
    idle_hours = float(auth_config().get("session_idle_hours") or 12)
    if now >= expires_at or now >= last_seen + timedelta(hours=idle_hours):
        await database.db.delete_session(session["token_hash"])
        return None
    user = await database.db.get_user_by_id(session["user_id"])
    if not user or not user.get("enabled"):
        await database.db.delete_session(session["token_hash"])
        return None
    await database.db.touch_session(session["token_hash"], _iso(now))
    return user


async def destroy_request_session(cookie_value: Optional[str]) -> None:
    token = _parse_signed_token(cookie_value or "")
    if not token:
        return
    await database.db.delete_session(_token_hash(token))


async def local_login(username: str, password: str, client_ip: str) -> tuple[Optional[dict], Optional[str]]:
    """Attempt local login. Returns (user, cookie_value) or (None, None) on failure."""
    cfg = auth_config()
    if not cfg.get("allow_local", True):
        await _lockout_delay()
        return None, None
    normalized = normalize_username(username)
    password = password or ""
    lock = await _username_lock(normalized or client_ip or "unknown")
    async with lock:
        if await _is_locked_out(normalized, client_ip or ""):
            await database.db.record_login_attempt(
                attempted_at=_iso(_utcnow()),
                username=normalized or None,
                client_ip=client_ip or "",
                username_existed=False,
            )
            await _lockout_delay()
            return None, None

        user = await database.db.get_user_by_username(normalized) if normalized else None
        existed = bool(user and user.get("auth_source") == "local" and user.get("password_hash"))
        ok = False
        if existed and user.get("enabled") and verify_password(user["password_hash"], password):
            ok = True

        if not ok:
            await database.db.record_login_attempt(
                attempted_at=_iso(_utcnow()),
                username=normalized or None,
                client_ip=client_ip or "",
                username_existed=existed,
            )
            return None, None

        await database.db.clear_login_attempts_for_username(normalized)
        await database.db.update_user(user["id"], last_login_at=_iso(_utcnow()))
        cookie_value = await create_session_cookie_value(user["id"])
        return user, cookie_value


def _origin_allowed(request) -> bool:
    """Same-origin check for mutating cookie-authenticated APIs."""
    method = (request.method or "GET").upper()
    if method not in _WRITE_METHODS:
        return True
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("host") or ""
    try:
        origin_host = urlparse(origin).netloc
    except ValueError:
        return False
    return origin_host == host


def _unauthenticated_response(request, next_url: Optional[str] = None) -> Response:
    if _is_html_request(request):
        target = next_url or request.path or "/"
        query = request.rel_url.query_string if getattr(request, "rel_url", None) else ""
        if query:
            target = f"{target}?{query}"
        login = "/login"
        if target and target != "/login":
            login = f"/login?next={quote(target, safe='/')}"
        return RedirectResponse(url=login, status_code=302)
    return JSONResponse({"success": False, "error": "Authentication required"}, status_code=401)


def _forbidden_response(request) -> Response:
    if _is_html_request(request):
        from .handlers import render_template
        html = render_template("forbidden.html", active_nav="")
        return Response(content=html, status_code=403, media_type="text/html")
    return JSONResponse({"success": False, "error": "Forbidden"}, status_code=403)


def _attach_user(request, user: Optional[dict]) -> frozenset[str]:
    if user:
        permissions = permissions_for_role(user.get("role") or "member")
    else:
        permissions = frozenset()
    request["user"] = user
    request["permissions"] = permissions
    request.user = user
    request.permissions = permissions
    _set_template_context(user, permissions)
    return permissions


async def enforce(request) -> Optional[Response]:
    """Return a denial response, or None if the request may proceed."""
    path = request.path or "/"
    tls_denied = ingest_tls_denied(request)
    if tls_denied is not None:
        return tls_denied
    ingest_denied = ingest_request_denied(request)
    if ingest_denied is not None:
        return ingest_denied
    if not is_auth_enabled():
        if _is_auth_only_path(path):
            return _not_found_response(request)
        request["user"] = None
        request["permissions"] = ROLE_PERMISSIONS["admin"]
        request.user = None
        request.permissions = ROLE_PERMISSIONS["admin"]
        _set_template_context(None, ROLE_PERMISSIONS["admin"])
        return None

    cookie = None
    cookies = getattr(request, "cookies", None)
    if cookies is not None:
        cookie = cookies.get(SESSION_COOKIE_NAME)
    user = await resolve_session_user(cookie)
    permissions = _attach_user(request, user)

    needed = required_permission(request.method or "GET", request.path or "/")
    if needed is None:
        return None
    if user is None:
        return _unauthenticated_response(request)
    if not _origin_allowed(request):
        return JSONResponse({"success": False, "error": "Forbidden"}, status_code=403)
    if not has_permission(permissions, needed):
        return _forbidden_response(request)
    return None


async def enforce_websocket(websocket: WebSocket, path: str) -> bool:
    """Return True if the WebSocket may continue."""
    if path == "/ws/nunit":
        from .tls_certs import ingest_tls_enabled
        if ingest_tls_enabled():
            scheme = (str(websocket.url.scheme) or "").lower()
            if scheme not in ("https", "wss"):
                return False
        if ingest_token_configured() and not ingest_token_matches(
            extract_ingest_token(websocket.headers)
        ):
            return False
        return True
    if not is_auth_enabled():
        return True
    user = await resolve_session_user(websocket.cookies.get(SESSION_COOKIE_NAME))
    if user is None:
        return False
    return has_permission(permissions_for_role(user.get("role") or "member"), PERM_RUNS_READ)


async def would_remove_last_admin(user: dict, *, new_role: Optional[str] = None, new_enabled: Optional[bool] = None) -> bool:
    if user.get("role") != "admin" or not user.get("enabled"):
        return False
    demoting = new_role is not None and new_role != "admin"
    disabling = new_enabled is False
    if not demoting and not disabling:
        return False
    return await database.db.count_enabled_admins() <= 1
