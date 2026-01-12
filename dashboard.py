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

# Загружаем переменные окружения в начале
load_dotenv()

# Импортируем существующую логику
from main import AccountManager, MANAGER_CONFIG, ACCOUNTS_DIR, load_json_file

app = FastAPI(title="Telegram Assistant Dashboard", version="0.9.9G")
VERSION = "v0.9.9G"

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
        return data.get("user")
    except:
        return None

def authenticate(user: str = Depends(get_session_user)):
    if not user:
        # Для API возвращаем 401, для страниц можно редирект, 
        # но FastAPI Depends лучше работает с исключениями
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

async def optional_authenticate(user: str = Depends(get_session_user)):
    return user

# Монтируем статику (саму папку статики не защищаем, чтобы CSS/JS грузились до логина? 
# Нет, лучше защитить всё приложение)

# Монтируем статику
app.mount("/static", StaticFiles(directory="static"), name="static")

manager = AccountManager()

@app.get("/login", response_class=HTMLResponse)
async def login_page(user: str = Depends(get_session_user)):
    if user:
        return RedirectResponse(url="/")
    with open("static/login.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/auth/login")
async def api_login(req: Request):
    data = await req.json()
    username = data.get("username")
    password = data.get("password")
    
    correct_username = os.getenv("DASHBOARD_USER", "admin")
    correct_password = os.getenv("DASHBOARD_PASSWORD", "08908090") # default from .env
    
    if username == correct_username and password == correct_password:
        token = serializer.dumps({"user": username, "ts": time.time()})
        response = JSONResponse({"status": "success"})
        response.set_cookie(key="session", value=token, httponly=True, samesite="lax")
        return response
    
    return JSONResponse({"status": "error", "message": "Invalid credentials"}, status_code=401)

@app.post("/api/auth/logout")
async def api_logout():
    response = JSONResponse({"status": "success"})
    response.delete_cookie("session")
    return response

@app.get("/", response_class=HTMLResponse)
async def get_dashboard(user: str = Depends(get_session_user)):
    if not user:
        return RedirectResponse(url="/login")
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/auth/register/options")
async def register_options(request: Request, user: str = Depends(authenticate)):
    # derive origin and rp_id from the incoming request so origin matches
    origin = str(request.base_url).rstrip('/')
    rp_id = request.url.hostname or os.getenv('RP_ID', 'localhost')

    options = auth_utils.get_webauthn_registration_options(user, user, rp_id=rp_id)
    # Convert to JSON-friendly structure and store challenge (string) in memory
    options_json = auth_utils.options_to_json(options)
    # options_to_json may return a JSON string or a dict depending on library version
    if isinstance(options_json, str):
        try:
            options_json = json.loads(options_json)
        except Exception:
            pass

    webauthn_challenges[user] = {
        'challenge': options_json.get('challenge') if isinstance(options_json, dict) else None,
        'expected_origin': origin,
        'expected_rp_id': rp_id,
    }
    return JSONResponse(options_json)

@app.post("/api/auth/register/verify")
async def register_verify(req: Request, user: str = Depends(authenticate)):
    data = await req.json()
    stored = webauthn_challenges.get(user)
    if not stored:
        return JSONResponse({"status": "error", "message": "Challenge not found"}, status_code=400)

    # Attach expected values so verifier can use correct origin and rp_id
    if isinstance(data, dict):
        data['_expected_origin'] = stored.get('expected_origin')
        data['_expected_rp_id'] = stored.get('expected_rp_id')

    success = auth_utils.verify_webauthn_registration(user, data, stored.get('challenge'))
    if success:
        return {"status": "success"}
    return JSONResponse({"status": "error", "message": "Verification failed"}, status_code=400)

@app.post("/api/auth/login/options")
async def login_options(request: Request):
    data = await request.json()
    username = data.get("username")
    if not username:
        return JSONResponse({"status": "error", "message": "Username required"}, status_code=400)

    rp_id = request.url.hostname or os.getenv('RP_ID', 'localhost')
    options = auth_utils.get_webauthn_authentication_options(username, rp_id=rp_id)
    if not options:
        return JSONResponse({"status": "error", "message": "No passkeys found for this user"}, status_code=404)

    # Store challenge by username for login (use JSON form to ensure string)
    options_json = auth_utils.options_to_json(options)
    if isinstance(options_json, str):
        try:
            options_json = json.loads(options_json)
        except Exception:
            pass

    origin = str(request.base_url).rstrip('/')
    webauthn_challenges[f"login_{username}"] = {
        'challenge': options_json.get('challenge') if isinstance(options_json, dict) else None,
        'expected_origin': origin,
        'expected_rp_id': rp_id,
    }
    return JSONResponse(options_json)

@app.post("/api/auth/login/verify")
async def login_verify(req: Request):
    data = await req.json()
    username = data.get("username")
    stored = webauthn_challenges.get(f"login_{username}")

    if not stored:
        return JSONResponse({"status": "error", "message": "Challenge not found"}, status_code=400)

    if isinstance(data, dict):
        data['_expected_origin'] = stored.get('expected_origin')
        data['_expected_rp_id'] = stored.get('expected_rp_id')

    success = auth_utils.verify_webauthn_authentication(username, data, stored.get('challenge'))
    if success:
        token = serializer.dumps({"user": username, "ts": time.time()})
        response = JSONResponse({"status": "success"})
        response.set_cookie(key="session", value=token, httponly=True, samesite="lax")
        return response

    return JSONResponse({"status": "error", "message": "Verification failed"}, status_code=401)

@app.get("/api/status")
async def get_status(user: str = Depends(authenticate)):
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
async def auth_start(req: Request, username: str = Depends(authenticate)):
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
async def auth_check(name: str, username: str = Depends(authenticate)):
    result = await manager.add_account_web_check(name)
    return result

@app.post("/api/accounts/auth/2fa")
async def auth_2fa(req: Request, username: str = Depends(authenticate)):
    data = await req.json()
    name = data.get("name")
    password = data.get("password")
    result = await manager.add_account_web_2fa(name, password)
    return result

@app.post("/api/accounts/auth/phone/start")
async def auth_phone_start(req: Request, username: str = Depends(authenticate)):
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
async def auth_phone_verify(req: Request, username: str = Depends(authenticate)):
    data = await req.json()
    name = data.get("name")
    code = data.get("code")
    phone = data.get("phone")
    result = await manager.add_account_phone_verify(name, code, phone)
    return result

# --- Global Config Editor ---
@app.get("/api/config")
async def get_global_config(username: str = Depends(authenticate)):
    env_file = Path(".env")
    if not env_file.exists():
        return Response(content="", media_type="text/plain")
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="text/plain")
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/api/config")
async def save_global_config(req: Request, username: str = Depends(authenticate)):
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
async def ai_test(req: Request, username: str = Depends(authenticate)):
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
async def get_logs(limit: int = 100, username: str = Depends(authenticate)):
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
    # For simplicity, we don't authenticate WS in this basic version, 
    # but in production, we should check a token.
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
async def export_logs(username: str = Depends(authenticate)):
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
async def clear_logs(username: str = Depends(authenticate)):
    log_file = Path("bot.log")
    try:
        with open(log_file, "w", encoding="utf-8") as f:
            f.truncate(0)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/ai/token-stats")
async def get_token_stats(username: str = Depends(authenticate)):
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
async def reset_account(name: str, username: str = Depends(authenticate)):
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

@app.get("/api/accounts/history/{name}")
async def get_account_history(name: str, username: str = Depends(authenticate)):
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
async def get_account_lists(name: str, username: str = Depends(authenticate)):
    acc_dir = ACCOUNTS_DIR / name
    return {
        "whitelist": load_json_file(acc_dir / "whitelist.json", []),
        "blacklist": load_json_file(acc_dir / "blacklist.json", [])
    }

class ListUpdate(BaseModel):
    whitelist: list
    blacklist: list

@app.post("/api/accounts/lists/{name}")
async def save_account_lists(name: str, data: ListUpdate, username: str = Depends(authenticate)):
    acc_dir = ACCOUNTS_DIR / name
    from main import atomic_save_json
    try:
        atomic_save_json(acc_dir / "whitelist.json", data.whitelist)
        atomic_save_json(acc_dir / "blacklist.json", data.blacklist)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/accounts/settings/{name}")
async def get_account_settings(name: str, username: str = Depends(authenticate)):
    acc_dir = ACCOUNTS_DIR / name
    if not acc_dir.exists():
        raise HTTPException(status_code=404, detail="Account not found")
    
    settings_file = acc_dir / "settings.json"
    default_settings = {
        "gemini_prompt": os.getenv("GEMINI_PROMPT", "Ты личный ассистент. Отвечай кратко, вежливо и по делу."),
        "ai_enabled": True,
        "gemini_model": os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        "voice_mode": False,
        "voice_id": "ru-RU-SvetlanaNeural"
    }
    
    if settings_file.exists():
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return default_settings
    return default_settings

@app.post("/api/accounts/settings/{name}")
async def save_account_settings(name: str, req: Request, username: str = Depends(authenticate)):
    acc_dir = ACCOUNTS_DIR / name
    if not acc_dir.exists():
        raise HTTPException(status_code=404, detail="Account not found")
    
    data = await req.json()
    settings_file = acc_dir / "settings.json"
    
    try:
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.delete("/api/accounts/{name}")
async def delete_account(name: str, username: str = Depends(authenticate)):
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
async def get_system_stats(username: str = Depends(authenticate)):
    try:
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        return {"cpu": cpu, "ram": ram}
    except Exception as e:
        return {"cpu": 0, "ram": 0, "error": str(e)}

@app.get("/api/main/status")
async def get_main_status(username: str = Depends(authenticate)):
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
async def start_main(username: str = Depends(authenticate)):
    if find_main_process():
        return {"status": "error", "message": "Бот уже запущен"}
    
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
async def stop_main(username: str = Depends(authenticate)):
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
async def restart_main(username: str = Depends(authenticate)):
    await stop_main()
    await asyncio.sleep(2)
    return await start_main()

@app.get("/api/system/systemd")
async def get_systemd_config(username: str = Depends(authenticate)):
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
    
    uvicorn.run("dashboard:app", host="0.0.0.0", port=8000, reload=True, **ssl_config)
