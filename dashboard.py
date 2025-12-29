import os
import asyncio
import json
import logging
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
import uvicorn

# Загружаем переменные окружения в начале
load_dotenv()

# Импортируем существующую логику
from main import AccountManager, MANAGER_CONFIG, ACCOUNTS_DIR, load_json_file

app = FastAPI(title="Telegram Assistant Dashboard")

# Монтируем статику
app.mount("/static", StaticFiles(directory="static"), name="static")

manager = AccountManager()

class AccountName(BaseModel):
    name: str

@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/status")
async def get_status():
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
async def auth_start(req: Request):
    data = await req.json()
    name = data.get("name")
    if not name:
        return JSONResponse({"status": "error", "message": "Name required"}, status_code=400)
    
    # Проверяем, не запущен ли уже этот бот (чтобы избежать database is locked)
    acc_dir = ACCOUNTS_DIR / name
    status_file = acc_dir / "status.json"
    if status_file.exists():
        import time
        status_info = load_json_file(status_file, {})
        if status_info.get("status") == "online" and (time.time() - status_info.get("last_seen", 0) < 30):
            return JSONResponse({"status": "error", "message": "Бот уже запущен в основной программе. Сначала остановите его или удалите."}, status_code=400)

    result = await manager.add_account_web_start(name)
    return result

@app.get("/api/accounts/auth/check/{name}")
async def auth_check(name: str):
    result = await manager.add_account_web_check(name)
    return result

@app.post("/api/accounts/auth/2fa")
async def auth_2fa(req: Request):
    data = await req.json()
    name = data.get("name")
    password = data.get("password")
    result = await manager.add_account_web_2fa(name, password)
    return result

@app.get("/api/logs")
async def get_logs():
    log_file = Path("bot.log")
    if not log_file.exists():
        return {"logs": ["Log file not found."]}
    
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            return {"logs": [l.strip() for l in lines[-50:]]}
    except Exception as e:
        return {"logs": [f"Error reading logs: {e}"]}

@app.post("/api/accounts/reset/{name}")
async def reset_account(name: str):
    acc_dir = ACCOUNTS_DIR / name
    if not acc_dir.exists():
        raise HTTPException(status_code=404, detail="Account not found")
        
    try:
        # Сбрасываем файлы данных напрямую
        empty_stats = {"blocked_files": 0, "blocked_scams": 0, "total_unknown": 0}
        from main import atomic_save_json
        atomic_save_json(acc_dir / "stats.json", empty_stats)
        atomic_save_json(acc_dir / "history.json", {})
        atomic_save_json(acc_dir / "states.json", {})
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.delete("/api/accounts/{name}")
async def delete_account(name: str):
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

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
