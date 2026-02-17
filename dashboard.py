import os
import asyncio
import aiohttp
import json
import logging
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from dotenv import load_dotenv
import secrets
import uvicorn
import psutil
import subprocess
import sys
import time
import signal
import datetime
from itsdangerous import URLSafeSerializer
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials, APIKeyCookie
import auth_utils
from collections import deque

try:
    from starlette.middleware.proxy_headers import ProxyHeadersMiddleware
except Exception:
    ProxyHeadersMiddleware = None

# Загружаем переменные окружения в начале
load_dotenv()

# Импортируем существующую логику
from main import AccountManager, MANAGER_CONFIG, ACCOUNTS_DIR, load_json_file

app = FastAPI(title="Telegram Assistant Dashboard", version="1.2")
VERSION = "v1.2"
APP_STARTED_AT = time.time()

ENABLE_METRICS = str(os.getenv("ENABLE_METRICS", "0")).strip().lower() in {"1", "true", "yes", "y", "on"}
try:
    if ENABLE_METRICS:
        from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
    else:
        Counter = Gauge = Histogram = generate_latest = CONTENT_TYPE_LATEST = None
except Exception:
    Counter = Gauge = Histogram = generate_latest = CONTENT_TYPE_LATEST = None
    ENABLE_METRICS = False

if ENABLE_METRICS:
    HTTP_REQUESTS = Counter(
        "tg_assistant_http_requests_total",
        "Total HTTP requests",
        ["method", "path", "status"],
    )
    HTTP_LATENCY = Histogram(
        "tg_assistant_http_request_duration_seconds",
        "HTTP request duration in seconds",
        ["method", "path"],
        buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    )
    ACCOUNTS_CONFIGURED = Gauge("tg_assistant_accounts_configured", "Accounts configured in manager.json")
    ACCOUNTS_ONLINE = Gauge("tg_assistant_accounts_online", "Accounts online according to heartbeat status.json")
    AUTH_ACTIVE = Gauge("tg_assistant_auth_active", "Active auth flows in dashboard process")
    UPTIME_SECONDS = Gauge("tg_assistant_dashboard_uptime_seconds", "Dashboard process uptime in seconds")

TRUST_PROXY = str(os.getenv("TRUST_PROXY", "0")).strip().lower() in {"1", "true", "yes", "y", "on"}
if TRUST_PROXY and ProxyHeadersMiddleware is not None:
    # Needed when running behind Cloudflare Tunnel / reverse proxies to get proper scheme and client IP.
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")


def _get_request_scheme(request: Request) -> str:
    xf_proto = request.headers.get("x-forwarded-proto")
    if xf_proto:
        return xf_proto.split(",")[0].strip().lower()
    cf_visitor = request.headers.get("cf-visitor")
    if cf_visitor:
        try:
            data = json.loads(cf_visitor)
            scheme = str(data.get("scheme", "")).lower().strip()
            if scheme:
                return scheme
        except Exception:
            pass
    return (request.url.scheme or "http").lower()


def _get_client_ip(request: Request) -> str:
    # Cloudflare Tunnel and many proxies send the real IP in these headers.
    for h in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
        v = request.headers.get(h)
        if not v:
            continue
        # X-Forwarded-For may contain a list.
        return v.split(",")[0].strip()
    return getattr(getattr(request, "client", None), "host", "") or "unknown"


def _is_https(request: Request) -> bool:
    return _get_request_scheme(request) == "https"


def _set_session_cookie(response: Response, username: str, request: Request):
    rec = get_user_record(username) or {"role": "viewer"}
    token = serializer.dumps({"user": username, "role": rec.get("role") or "viewer", "ts": time.time()})
    max_age = int(os.getenv("DASHBOARD_SESSION_MAX_AGE", str(60 * 60 * 24 * 7)))  # 7 days
    secure = str(os.getenv("DASHBOARD_COOKIE_SECURE", "")).strip().lower() in {"1", "true", "yes", "y", "on"}
    secure = secure or _is_https(request)
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=max_age,
        path="/",
    )


class InMemoryRateLimiter:
    def __init__(self, max_hits: int, window_seconds: int):
        self.max_hits = max_hits
        self.window_seconds = window_seconds
        self._hits = {}  # key -> deque[timestamps]

    def check(self, key: str) -> tuple[bool, int]:
        now = time.time()
        q = self._hits.get(key)
        if q is None:
            q = deque()
            self._hits[key] = q
        while q and q[0] < now - self.window_seconds:
            q.popleft()
        if len(q) >= self.max_hits:
            retry_after = int(max(1, self.window_seconds - (now - q[0])))
            return False, retry_after
        q.append(now)
        return True, 0


AUTH_RL = InMemoryRateLimiter(
    max_hits=int(os.getenv("AUTH_RL_MAX", "12")),
    window_seconds=int(os.getenv("AUTH_RL_WINDOW", "60")),
)


@app.middleware("http")
async def _security_headers_mw(request: Request, call_next):
    response = await call_next(request)

    # Basic hardening headers.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        "geolocation=(), microphone=(), camera=(), payment=(), usb=()",
    )

    # Only set HSTS when the request is actually HTTPS (or forced).
    force_hsts = str(os.getenv("FORCE_HSTS", "0")).strip().lower() in {"1", "true", "yes", "y", "on"}
    if force_hsts or _is_https(request):
        response.headers.setdefault("Strict-Transport-Security", "max-age=15552000; includeSubDomains")

    # Optional CSP (off by default because the dashboard uses inline styles and CDN scripts).
    enable_csp = str(os.getenv("ENABLE_CSP", "0")).strip().lower() in {"1", "true", "yes", "y", "on"}
    if enable_csp and "Content-Security-Policy" not in response.headers:
        csp = (
            "default-src 'self'; "
            "base-uri 'self'; "
            "object-src 'none'; "
            "frame-ancestors 'none'; "
            "img-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; "
        )
        response.headers["Content-Security-Policy"] = csp

    return response


@app.middleware("http")
async def _metrics_middleware(request: Request, call_next):
    if not ENABLE_METRICS:
        return await call_next(request)

    t0 = time.time()
    response = None
    try:
        response = await call_next(request)
        return response
    finally:
        try:
            route = request.scope.get("route")
            path = getattr(route, "path", request.url.path)
            status = str(getattr(response, "status_code", 500))
            HTTP_REQUESTS.labels(request.method, path, status).inc()
            HTTP_LATENCY.labels(request.method, path).observe(time.time() - t0)
        except Exception:
            pass


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "version": VERSION,
        "ts": time.time(),
        "uptime_s": int(time.time() - APP_STARTED_AT),
    }


@app.get("/readyz")
async def readyz():
    missing_critical = []
    missing_optional = []

    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    if not api_id:
        missing_critical.append("API_ID")
    if not api_hash:
        missing_critical.append("API_HASH")

    if not os.getenv("GEMINI_API_KEY"):
        missing_optional.append("GEMINI_API_KEY")

    accounts_dir_ok = ACCOUNTS_DIR.exists()
    ok = accounts_dir_ok and not missing_critical

    payload = {
        "ok": ok,
        "version": VERSION,
        "accounts_dir": accounts_dir_ok,
        "missing_critical": missing_critical,
        "missing_optional": missing_optional,
    }
    return JSONResponse(payload, status_code=200 if ok else 503)


@app.get("/metrics")
async def metrics():
    if not ENABLE_METRICS or generate_latest is None:
        raise HTTPException(status_code=404, detail="Metrics disabled")

    # Update gauges on scrape.
    try:
        config = load_json_file(MANAGER_CONFIG, {"accounts": []})
        names = [a.get("name") for a in (config.get("accounts") or []) if a.get("name")]
        ACCOUNTS_CONFIGURED.set(len(names))

        online = 0
        for name in names:
            status_info = load_json_file(ACCOUNTS_DIR / name / "status.json", {"status": "offline", "last_seen": 0})
            if status_info.get("status") == "online" and (time.time() - (status_info.get("last_seen", 0) or 0)) < 30:
                online += 1
        ACCOUNTS_ONLINE.set(online)
    except Exception:
        pass

    try:
        AUTH_ACTIVE.set(len(getattr(manager, "active_auths", {}) or {}))
    except Exception:
        pass

    try:
        UPTIME_SECONDS.set(time.time() - APP_STARTED_AT)
    except Exception:
        pass

    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

# Сессии и Безопасность
SECRET_KEY = os.getenv("DASHBOARD_SECRET", secrets.token_urlsafe(32))
serializer = URLSafeSerializer(SECRET_KEY)
session_cookie = APIKeyCookie(name="session", auto_error=False)

# Глобальное хранилище для вызовов WebAuthn (в памяти, так как процесс один)
webauthn_challenges = {} # session_id: challenge

def get_session_user(session: str = Depends(session_cookie)):
    if not session:
        return None
    try:
        data = serializer.loads(session)
        return {
            "user": data.get("user"),
            "role": data.get("role") or "admin",
        }
    except:
        return None

def authenticate(session: dict = Depends(get_session_user)):
    if not session or not session.get("user"):
        # Для API возвращаем 401, для страниц можно редирект, 
        # но FastAPI Depends лучше работает с исключениями
        raise HTTPException(status_code=401, detail="Not authenticated")
    return session

def require_admin(session: dict = Depends(authenticate)):
    if (session.get("role") or "viewer") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return session

async def optional_authenticate(session: dict = Depends(get_session_user)):
    return session


def _parse_users_env():
    """
    Extra users in env:
      DASHBOARD_USERS="user1:pass1:viewer,user2:pass2:viewer"
    Role defaults to viewer.
    """
    raw = str(os.getenv("DASHBOARD_USERS", "")).strip()
    users = {}
    if not raw:
        return users
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        seg = part.split(":")
        if len(seg) < 2:
            continue
        u = seg[0].strip()
        p = seg[1]
        r = (seg[2].strip().lower() if len(seg) >= 3 else "viewer") or "viewer"
        if u and p:
            users[u] = {"password": p, "role": "admin" if r == "admin" else "viewer"}
    return users


def get_user_record(username: str):
    admin_user = os.getenv("DASHBOARD_USER", "admin")
    admin_pass = os.getenv("DASHBOARD_PASSWORD")
    if not admin_pass:
        admin_pass = secrets.token_urlsafe(16)
        logging.warning("DASHBOARD_PASSWORD is not set. Generated a random one for this run.")

    users = {admin_user: {"password": admin_pass, "role": "admin"}}
    users.update(_parse_users_env())
    return users.get(username)

# Монтируем статику (саму папку статики не защищаем, чтобы CSS/JS грузились до логина? 
# Нет, лучше защитить всё приложение)

# Монтируем статику
app.mount("/static", StaticFiles(directory="static"), name="static")

manager = AccountManager()

@app.on_event("startup")
async def _startup_background_cleanup():
    # Periodically cleanup stale auth sessions to avoid holding Telethon sqlite session locks.
    async def _loop():
        while True:
            try:
                # Implemented in main.py AccountManager; safe no-op if it ever changes.
                if hasattr(manager, "_cleanup_stale_auths"):
                    await manager._cleanup_stale_auths()
            except Exception:
                pass
            await asyncio.sleep(60)

    asyncio.create_task(_loop())

# Allow HEAD so `curl -I http://127.0.0.1:8000/login` works and basic health probes don't fail.
@app.api_route("/login", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def login_page(user: str = Depends(get_session_user)):
    if user:
        return RedirectResponse(url="/")
    with open("static/login.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/auth/login")
async def api_login(req: Request):
    ip = _get_client_ip(req)
    ok, retry_after = AUTH_RL.check(f"{ip}:password_login")
    if not ok:
        return JSONResponse(
            {"status": "error", "message": "Too many attempts. Try later."},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    data = await req.json()
    username = data.get("username")
    password = data.get("password")
    
    rec = get_user_record(username or "")
    if rec and secrets.compare_digest(str(password or ""), str(rec.get("password") or "")):
        response = JSONResponse({"status": "success"})
        _set_session_cookie(response, username, req)
        return response
    
    return JSONResponse({"status": "error", "message": "Invalid credentials"}, status_code=401)

@app.post("/api/auth/logout")
async def api_logout(req: Request):
    response = JSONResponse({"status": "success"})
    response.delete_cookie("session", path="/")
    return response

@app.get("/", response_class=HTMLResponse)
async def get_dashboard(user: str = Depends(get_session_user)):
    if not user or not user.get("user"):
        return RedirectResponse(url="/login")
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/me")
async def me(session: dict = Depends(authenticate)):
    return {"user": session.get("user"), "role": session.get("role") or "viewer", "version": VERSION}

@app.get("/api/auth/register/options")
async def register_options(request: Request, user: dict = Depends(authenticate)):
    ip = _get_client_ip(request)
    ok, retry_after = AUTH_RL.check(f"{ip}:passkey_register_options")
    if not ok:
        return JSONResponse(
            {"status": "error", "message": "Too many attempts. Try later."},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    # derive origin and rp_id from the incoming request so origin matches
    origin = os.getenv("EXPECTED_ORIGIN") or str(request.base_url).rstrip('/')
    rp_id = os.getenv("RP_ID") or request.url.hostname or "localhost"

    username = user.get("user")
    options = auth_utils.get_webauthn_registration_options(username, username, rp_id=rp_id)
    # Convert to JSON-friendly structure and store challenge (string) in memory
    options_json = auth_utils.options_to_json(options)
    # options_to_json may return a JSON string or a dict depending on library version
    if isinstance(options_json, str):
        try:
            options_json = json.loads(options_json)
        except Exception:
            pass

    webauthn_challenges[username] = {
        'challenge': options_json.get('challenge') if isinstance(options_json, dict) else None,
        'expected_origin': origin,
        'expected_rp_id': rp_id,
    }
    return JSONResponse(options_json)

@app.post("/api/auth/register/verify")
async def register_verify(req: Request, user: dict = Depends(authenticate)):
    ip = _get_client_ip(req)
    ok, retry_after = AUTH_RL.check(f"{ip}:passkey_register_verify")
    if not ok:
        return JSONResponse(
            {"status": "error", "message": "Too many attempts. Try later."},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    data = await req.json()
    username = user.get("user")
    stored = webauthn_challenges.get(username)
    if not stored:
        return JSONResponse({"status": "error", "message": "Challenge not found"}, status_code=400)

    # Attach expected values so verifier can use correct origin and rp_id
    if isinstance(data, dict):
        data['_expected_origin'] = stored.get('expected_origin')
        data['_expected_rp_id'] = stored.get('expected_rp_id')

    success = auth_utils.verify_webauthn_registration(username, data, stored.get('challenge'))
    if success:
        # One-time use challenge
        webauthn_challenges.pop(username, None)
        return {"status": "success"}
    return JSONResponse({"status": "error", "message": "Verification failed"}, status_code=400)

@app.post("/api/auth/login/options")
async def login_options(request: Request):
    ip = _get_client_ip(request)
    ok, retry_after = AUTH_RL.check(f"{ip}:passkey_login_options")
    if not ok:
        return JSONResponse(
            {"status": "error", "message": "Too many attempts. Try later."},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    data = await request.json()
    rp_id = os.getenv("RP_ID") or request.url.hostname or "localhost"
    username = data.get("username") if isinstance(data, dict) else None

    if username:
        options = auth_utils.get_webauthn_authentication_options(username, rp_id=rp_id)
        if not options:
            return JSONResponse({"status": "error", "message": "No passkeys found for this user"}, status_code=404)
    else:
        # Username-less flow (discoverable credentials)
        options = auth_utils.get_webauthn_authentication_options_any(rp_id=rp_id)

    # Store challenge by username for login (use JSON form to ensure string)
    options_json = auth_utils.options_to_json(options)
    if isinstance(options_json, str):
        try:
            options_json = json.loads(options_json)
        except Exception:
            pass

    origin = os.getenv("EXPECTED_ORIGIN") or str(request.base_url).rstrip('/')
    login_id = secrets.token_urlsafe(16)
    webauthn_challenges[f"login_{login_id}"] = {
        'challenge': options_json.get('challenge') if isinstance(options_json, dict) else None,
        'expected_origin': origin,
        'expected_rp_id': rp_id,
        'username': username,
    }
    if isinstance(options_json, dict):
        options_json["login_id"] = login_id
    return JSONResponse(options_json)

@app.post("/api/auth/login/verify")
async def login_verify(req: Request):
    ip = _get_client_ip(req)
    ok, retry_after = AUTH_RL.check(f"{ip}:passkey_login_verify")
    if not ok:
        return JSONResponse(
            {"status": "error", "message": "Too many attempts. Try later."},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    data = await req.json()
    username = data.get("username") if isinstance(data, dict) else None
    login_id = data.get("login_id") if isinstance(data, dict) else None

    if not login_id:
        return JSONResponse({"status": "error", "message": "login_id required"}, status_code=400)

    stored_key = f"login_{login_id}"
    stored = webauthn_challenges.get(stored_key)

    if not stored:
        return JSONResponse({"status": "error", "message": "Challenge not found"}, status_code=400)

    if isinstance(data, dict):
        data['_expected_origin'] = stored.get('expected_origin')
        data['_expected_rp_id'] = stored.get('expected_rp_id')

    # Prefer stored username if provided; otherwise use discoverable credential flow.
    stored_username = stored.get("username") or username
    if stored_username:
        success = auth_utils.verify_webauthn_authentication(stored_username, data, stored.get('challenge'))
        authed_user = stored_username if success else None
    else:
        authed_user = auth_utils.verify_webauthn_authentication_any(data, stored.get('challenge'))

    if authed_user:
        # One-time use challenge
        webauthn_challenges.pop(stored_key, None)
        response = JSONResponse({"status": "success"})
        _set_session_cookie(response, authed_user, req)
        return response

    return JSONResponse({"status": "error", "message": "Verification failed"}, status_code=401)

@app.get("/api/status")
async def get_status(session: dict = Depends(authenticate)):
    import time
    accounts = []
    config = load_json_file(MANAGER_CONFIG, {"accounts": []})
    
    for acc in config['accounts']:
        acc_dir = ACCOUNTS_DIR / acc['name']
        stats = load_json_file(acc_dir / "stats.json", {})
        status_info = load_json_file(acc_dir / "status.json", {"status": "offline", "last_seen": 0})
        
        # Проверка "живости" по времени
        current_status = status_info.get("status", "offline")
        last_seen = status_info.get("last_seen", 0)
        
        if current_status == "online" and (time.time() - last_seen > 30):
            current_status = "offline" # Бот завис или упал
        
        accounts.append({
            "name": acc["name"],
            "status": current_status.capitalize(),
            "stats": {
                "blocked_files": stats.get("blocked_files", 0),
                "blocked_scams": stats.get("blocked_scams", 0),
                "total_unknown": stats.get("total_unknown", 0)
            }
        })
    return {"accounts": accounts}

@app.post("/api/accounts/auth/start")
async def auth_start(req: Request, session: dict = Depends(require_admin)):
    data = await req.json()
    name = data.get("name")
    if not name:
        return JSONResponse({"status": "error", "message": "Name required"}, status_code=400)
    
    # Проверяем, не запущен ли уже этот бот (чтобы избежать database is locked)
    acc_dir = ACCOUNTS_DIR / name
    status_file = acc_dir / "status.json"
    
    # Check if supervisor (main.py) is running
    main_proc = find_main_process()
    if main_proc:
        # If main.py is running, it might have the session locked
        if status_file.exists():
            status_info = load_json_file(status_file, {})
            if status_info.get("status") == "online" and (time.time() - status_info.get("last_seen", 0) < 45):
                return JSONResponse({"status": "error", "message": "Бот активен в основном процессе. Остановите бота перед авторизацией."}, status_code=400)

    logging.info(f"Starting web auth for account: {name}")
    result = await manager.add_account_web_start(name)
    return result

@app.get("/api/accounts/auth/check/{name}")
async def auth_check(name: str, session: dict = Depends(require_admin)):
    result = await manager.add_account_web_check(name)
    return result

@app.post("/api/accounts/auth/2fa")
async def auth_2fa(req: Request, session: dict = Depends(require_admin)):
    data = await req.json()
    name = data.get("name")
    password = data.get("password")
    result = await manager.add_account_web_2fa(name, password)
    return result

@app.post("/api/accounts/auth/cancel")
async def auth_cancel(req: Request, session: dict = Depends(require_admin)):
    data = await req.json()
    name = data.get("name")
    if not name:
        return JSONResponse({"status": "error", "message": "Name required"}, status_code=400)

    # Best-effort cleanup: disconnect the Telethon client to release sqlite session locks.
    try:
        if hasattr(manager, "_cleanup_auth"):
            await manager._cleanup_auth(name)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    return {"status": "success"}

@app.post("/api/accounts/auth/phone/start")
async def auth_phone_start(req: Request, session: dict = Depends(require_admin)):
    data = await req.json()
    name = data.get("name")
    phone = data.get("phone")
    if not name or not phone:
        return JSONResponse({"status": "error", "message": "Name and Phone required"}, status_code=400)
    
    # Check running
    main_proc = find_main_process()
    if main_proc:
        return JSONResponse({"status": "error", "message": "Stop bot first"}, status_code=400)
        
    result = await manager.add_account_phone_start(name, phone)
    return result

@app.post("/api/accounts/auth/phone/verify")
async def auth_phone_verify(req: Request, session: dict = Depends(require_admin)):
    data = await req.json()
    name = data.get("name")
    code = data.get("code")
    phone = data.get("phone")
    result = await manager.add_account_phone_verify(name, code, phone)
    return result

# --- Global Config Editor ---
@app.get("/api/config")
async def get_global_config(session: dict = Depends(require_admin)):
    env_file = Path(".env")
    if not env_file.exists():
        return Response(content="", media_type="text/plain")
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="text/plain")
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/config")
async def save_global_config(req: Request, session: dict = Depends(require_admin)):
    try:
        body = await req.body()
        content = body.decode("utf-8")
        env_file = Path(".env")
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(content)
        return {"status": "success"}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

# --- AI Playground ---
@app.post("/api/ai/test")
async def ai_test(req: Request, session: dict = Depends(require_admin)):
    try:
        data = await req.json()
        prompt = data.get('prompt')
        model_name = data.get('model', 'gemini-1.5-flash')
        
        # Try to use any active bot's client
        bot = None
        if manager.bots:
            bot = list(manager.bots.values())[0]
            
        if not bot:
            from google import genai
            api_key = os.getenv('GEMINI_API_KEY')
            if not api_key:
                return {"status": "error", "message": "API Key not configured in environment"}
            client = genai.Client(api_key=api_key)
        else:
            client = bot.ai_client

        response = await client.aio.models.generate_content(
            model=model_name,
            contents=prompt
        )
        if bot:
            bot._report_token_usage(response.usage_metadata)
        return {"status": "success", "response": response.text}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/logs")
async def get_logs(limit: int = 100, session: dict = Depends(authenticate)):
    log_file = Path("bot.log")
    if not log_file.exists():
        return {"logs": ["Log file not found."]}
    
    # Cap limit for safety
    limit = min(max(10, limit), 2000)
    
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            return {"logs": [l.strip() for l in lines[-limit:]]}
    except Exception as e:
        return {"logs": [f"Error reading logs: {e}"]}

@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    # Authenticate using the same signed session cookie as the rest of the dashboard.
    session = websocket.cookies.get("session") if hasattr(websocket, "cookies") else None
    user = None
    if session:
        try:
            data = serializer.loads(session)
            user = data.get("user")
        except Exception:
            user = None

    if not user:
        # 1008 = Policy Violation
        await websocket.close(code=1008)
        return

    await websocket.accept()
    log_file = Path("bot.log")
    
    if not log_file.exists():
        await websocket.send_text("Log file not found.")
        await websocket.close()
        return

    try:
        # Start by sending last 50 lines
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            for line in lines[-50:]:
                await websocket.send_text(line.strip())
        
        # Now tail the file
        last_size = log_file.stat().st_size
        while True:
            await asyncio.sleep(0.5)
            current_size = log_file.stat().st_size
            if current_size > last_size:
                with open(log_file, "r", encoding="utf-8") as f:
                    f.seek(last_size)
                    new_data = f.read()
                    if new_data:
                        for line in new_data.splitlines():
                            if line.strip():
                                await websocket.send_text(line.strip())
                last_size = current_size
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logging.error(f"WebSocket error: {e}")
    finally:
        try:
            await websocket.close()
        except:
            pass

@app.get("/api/logs/export")
async def export_logs(session: dict = Depends(authenticate)):
    from fastapi.responses import FileResponse
    log_file = Path("bot.log")
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="Log file not found")
    
    return FileResponse(
        path=log_file,
        filename=f"bot_logs_{int(time.time())}.txt",
        media_type="text/plain"
    )

@app.post("/api/logs/clear")
async def clear_logs(session: dict = Depends(require_admin)):
    log_file = Path("bot.log")
    try:
        with open(log_file, "w", encoding="utf-8") as f:
            f.truncate(0)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/ai/token-stats")
async def get_token_stats(session: dict = Depends(authenticate)):
    """Агрегация статистики использования токенов со всех аккаунтов."""
    aggregated = {} # date: {input, output}
    
    try:
        if ACCOUNTS_DIR.exists():
            for acc_dir in ACCOUNTS_DIR.iterdir():
                if acc_dir.is_dir():
                    token_file = acc_dir / "token_usage.json"
                    if token_file.exists():
                        data = load_json_file(token_file, {})
                        for date_str, usage in data.items():
                            if date_str not in aggregated:
                                aggregated[date_str] = {"input": 0, "output": 0}
                            aggregated[date_str]["input"] += usage.get("input", 0)
                            aggregated[date_str]["output"] += usage.get("output", 0)
        
        # Сортировка по дате
        sorted_dates = sorted(aggregated.keys())
        return {
            "dates": sorted_dates,
            "input": [aggregated[d]["input"] for d in sorted_dates],
            "output": [aggregated[d]["output"] for d in sorted_dates]
        }
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/accounts/reset/{name}")
async def reset_account(name: str, session: dict = Depends(require_admin)):
    acc_dir = ACCOUNTS_DIR / name
    if not acc_dir.exists():
        raise HTTPException(status_code=404, detail="Account not found")
    
    try:
        # Удаляем базу бота и статистику, но не сессию
        for f in ["bot.db", "stats.json"]:
            path = acc_dir / f
            if path.exists():
                path.unlink()
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/accounts/restart/{name}")
async def restart_account(name: str, session: dict = Depends(require_admin)):
    """
    Request a restart of a single account inside the supervisor (main.py).
    Implemented via a cross-process marker file consumed by AccountManager.run_all().
    """
    config = load_json_file(MANAGER_CONFIG, {"accounts": []})
    if not any(a.get("name") == name for a in (config.get("accounts") or [])):
        raise HTTPException(status_code=404, detail="Account not found")

    if not find_main_process():
        return JSONResponse({"status": "error", "message": "Supervisor (main.py) is not running"}, status_code=409)

    acc_dir = ACCOUNTS_DIR / name
    acc_dir.mkdir(parents=True, exist_ok=True)
    marker = acc_dir / "restart_requested.json"
    try:
        from main import atomic_save_json
        atomic_save_json(marker, {"ts": time.time(), "requested_by": username, "pid": os.getpid()})
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/accounts/history/{name}")
async def get_account_history(name: str, session: dict = Depends(authenticate)):
    acc_dir = ACCOUNTS_DIR / name
    history_file = acc_dir / "history.json"
    if not history_file.exists():
        return {"history": []}
    
    try:
        data = load_json_file(history_file, {})
        # Flatten and sort history from all users
        all_messages = []
        for user_id, messages in data.items():
            for msg in messages:
                msg['user_id'] = user_id
                all_messages.append(msg)
        
        # Sort by time descending
        all_messages.sort(key=lambda x: x.get('time', 0), reverse=True)
        return {"history": all_messages[:20]} # Return latest 20
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/accounts/lists/{name}")
async def get_account_lists(name: str, session: dict = Depends(require_admin)):
    acc_dir = ACCOUNTS_DIR / name
    return {
        "whitelist": load_json_file(acc_dir / "whitelist.json", []),
        "blacklist": load_json_file(acc_dir / "blacklist.json", [])
    }

class ListUpdate(BaseModel):
    whitelist: list
    blacklist: list

@app.post("/api/accounts/lists/{name}")
async def save_account_lists(name: str, data: ListUpdate, session: dict = Depends(require_admin)):
    acc_dir = ACCOUNTS_DIR / name
    from main import atomic_save_json
    try:
        atomic_save_json(acc_dir / "whitelist.json", data.whitelist)
        atomic_save_json(acc_dir / "blacklist.json", data.blacklist)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/accounts/settings/{name}")
async def get_account_settings(name: str, session: dict = Depends(require_admin)):
    acc_dir = ACCOUNTS_DIR / name
    if not acc_dir.exists():
        raise HTTPException(status_code=404, detail="Account not found")
    
    settings_file = acc_dir / "settings.json"
    default_settings = {
        "gemini_prompt": os.getenv("GEMINI_PROMPT", "Ты личный ассистент..."),
        "ai_enabled": True,
        "gemini_model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        "owner_name": "",
    }
    
    if settings_file.exists():
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                # Backward compatibility for old settings files.
                for k, v in default_settings.items():
                    loaded.setdefault(k, v)
                return loaded
        except:
            return default_settings
    return default_settings

@app.post("/api/accounts/settings/{name}")
async def save_account_settings(name: str, req: Request, session: dict = Depends(require_admin)):
    acc_dir = ACCOUNTS_DIR / name
    if not acc_dir.exists():
        raise HTTPException(status_code=404, detail="Account not found")
    
    data = await req.json()
    # Store only known keys (avoid accidental garbage in settings.json).
    allowed = {
        "gemini_prompt": str(data.get("gemini_prompt", "")),
        "ai_enabled": bool(data.get("ai_enabled", True)),
        "gemini_model": str(data.get("gemini_model", os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))),
        "owner_name": str(data.get("owner_name", "")).strip(),
    }
    settings_file = acc_dir / "settings.json"
    
    try:
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump(allowed, f, ensure_ascii=False, indent=4)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.delete("/api/accounts/{name}")
async def delete_account(name: str, session: dict = Depends(require_admin)):
    config = load_json_file(MANAGER_CONFIG, {"accounts": []})
    new_accounts = [a for a in config['accounts'] if a['name'] != name]
    
    if len(new_accounts) == len(config['accounts']):
        raise HTTPException(status_code=404, detail="Account not found")
        
    # Удаляем физическую папку аккаунта
    acc_dir = ACCOUNTS_DIR / name
    if acc_dir.exists():
        import shutil
        try:
            shutil.rmtree(acc_dir)
        except Exception as e:
            logging.error(f"Error deleting directory {acc_dir}: {e}")

    config['accounts'] = new_accounts
    from main import atomic_save_json
    atomic_save_json(MANAGER_CONFIG, config)
    return {"status": "success"}

# --- Управление основным процессом (main.py) ---

def find_main_process():
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cmdline = proc.info.get('cmdline') or []
            # Ищем процесс, где в аргументах есть main.py и задействован python
            is_python = any('python' in arg.lower() for arg in cmdline)
            is_main = any('main.py' in arg for arg in cmdline)
            if is_python and is_main and proc.pid != os.getpid():
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None

@app.get("/api/system/stats")
async def get_system_stats(session: dict = Depends(authenticate)):
    try:
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        return {"cpu": cpu, "ram": ram}
    except Exception as e:
        return {"cpu": 0, "ram": 0, "error": str(e)}

@app.get("/api/main/status")
async def get_main_status(session: dict = Depends(authenticate)):
    proc = find_main_process()
    if proc:
        try:
            with proc.oneshot():
                return {
                    "running": True,
                    "pid": proc.pid,
                    "memory": proc.memory_info().rss // (1024 * 1024),
                    "started": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(proc.create_time()))
                }
        except:
            pass
    return {"running": False}

@app.post("/api/main/start")
async def start_main(session: dict = Depends(require_admin)):
    if find_main_process():
        return {"status": "error", "message": "Бот уже запущен"}

    # Prevent starting supervisor while an auth flow holds the sqlite session file open.
    if getattr(manager, "active_auths", None):
        if len(manager.active_auths) > 0:
            return {"status": "error", "message": "Завершите/отмените авторизацию аккаунта в Dashboard перед запуском."}
    
    try:
        # Запускаем как отдельный процесс
        subprocess.Popen(
            [sys.executable, "main.py", "--daemon"],
            stdout=None,
            stderr=None,
            stdin=None,
            start_new_session=True 
        )
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/main/stop")
async def stop_main(session: dict = Depends(require_admin)):
    proc = find_main_process()
    if not proc:
        return {"status": "error", "message": "Бот не запущен"}
    
    try:
        proc.terminate()
        for _ in range(5):
            if not proc.is_running():
                break
            await asyncio.sleep(1)
        
        if proc.is_running():
            proc.kill() 
            
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/main/restart")
async def restart_main(session: dict = Depends(require_admin)):
    await stop_main()
    await asyncio.sleep(2)
    return await start_main()

@app.get("/api/system/systemd")
async def get_systemd_config(session: dict = Depends(require_admin)):
    import os
    working_dir = os.getcwd()
    user = os.getlogin() if hasattr(os, 'getlogin') else 'root'
    executable = sys.executable
    
    config = f"""[Unit]
Description=Telegram AI Assistant Supervisor
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={working_dir}
ExecStart={executable} main.py --daemon
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    return {"config": config, "path": "/etc/systemd/system/tg-assistant.service"}

if __name__ == "__main__":
    ssl_config = {}
    if os.path.exists("cert.pem") and os.path.exists("key.pem"):
        print("INFO: SSL Certificates found. Starting with HTTPS.")
        ssl_config = {
            "ssl_keyfile": "key.pem",
            "ssl_certfile": "cert.pem"
        }
    else:
        print("WARNING: No SSL certificates found. Passkeys will not work on remote devices.")
    
    # IMPORTANT: `reload=True` is dev-only. In production it can cause restarts/resets which break Cloudflare Tunnel.
    reload = str(os.getenv("DASHBOARD_RELOAD", "0")).strip().lower() in {"1", "true", "yes", "y", "on"}
    uvicorn.run("dashboard:app", host="0.0.0.0", port=8000, reload=reload, **ssl_config)
