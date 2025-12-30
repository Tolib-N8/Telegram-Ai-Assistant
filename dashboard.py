import os
import asyncio
import json
import logging
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
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

# Загружаем переменные окружения в начале
load_dotenv()

# Импортируем существующую логику
from main import AccountManager, MANAGER_CONFIG, ACCOUNTS_DIR, load_json_file

app = FastAPI(title="Telegram Assistant Dashboard")

# Настройка безопасности
security = HTTPBasic()

def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = os.getenv("DASHBOARD_USER", "admin")
    correct_password = os.getenv("DASHBOARD_PASSWORD", "password123")
    
    is_correct_username = secrets.compare_digest(credentials.username, correct_username)
    is_correct_password = secrets.compare_digest(credentials.password, correct_password)
    
    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=401,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# Монтируем статику (саму папку статики не защищаем, чтобы CSS/JS грузились до логина? 
# Нет, лучше защитить всё приложение)

# Монтируем статику
app.mount("/static", StaticFiles(directory="static"), name="static")

manager = AccountManager()

@app.get("/", response_class=HTMLResponse)
async def get_dashboard(username: str = Depends(authenticate)):
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/status")
async def get_status(username: str = Depends(authenticate)):
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

@app.get("/api/logs")
async def get_logs(limit: int = 100, username: str = Depends(authenticate)):
    log_file = Path("bot.log")
    if not log_file.exists():
        return {"logs": ["Log file not found."]}
    
    # Cap limit for safety
    limit = min(max(10, limit), 2000)
    
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            # Efficiently read last N lines
            # For simplicity with relatively small log files, we can use readlines()
            # If the file becomes huge, we'd need a seek-based approach.
            lines = f.readlines()
            return {"logs": [l.strip() for l in lines[-limit:]]}
    except Exception as e:
        return {"logs": [f"Error reading logs: {e}"]}

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
        "gemini_prompt": os.getenv("GEMINI_PROMPT", "Ты личный ассистент..."),
        "ai_enabled": True
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
    uvicorn.run(app, host="127.0.0.1", port=8000)
