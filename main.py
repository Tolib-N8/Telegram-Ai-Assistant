import os
import asyncio
import json
import re
import logging
from logging.handlers import RotatingFileHandler
import time
import datetime
import aiohttp
from pathlib import Path
from collections import deque
from telethon import TelegramClient, events, Button
from telethon.tl.types import User
from telethon.tl.functions.contacts import GetContactsRequest
from google import genai
from dotenv import load_dotenv
import qrcode
import argparse
import sys
import signal

try:
    import fcntl  # Unix-only, used for a real process lock
except Exception:
    fcntl = None

# ================= Конфигурация и Константы =================

def setup_logging():
    """
    Configure logging for the supervisor process.
    Important: dashboard.py imports main.py, so we must not configure file logging on import.
    """
    root = logging.getLogger()
    if any(getattr(h, "_tg_assistant_handler", False) for h in root.handlers):
        return

    level_name = str(os.getenv("LOG_LEVEL", "INFO")).upper().strip()
    level = getattr(logging, level_name, logging.INFO)
    root.setLevel(level)

    fmt = logging.Formatter("%(asctime)s - [%(name)s] - %(levelname)s - %(message)s")

    log_file = os.getenv("BOT_LOG_FILE", "bot.log")
    max_mb = int(os.getenv("BOT_LOG_MAX_MB", "20") or 20)
    backups = int(os.getenv("BOT_LOG_BACKUPS", "5") or 5)

    if max_mb > 0 and backups > 0:
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_mb * 1024 * 1024,
            backupCount=backups,
            encoding="utf-8",
        )
    else:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")

    stream_handler = logging.StreamHandler()
    for h in (file_handler, stream_handler):
        h.setFormatter(fmt)
        h._tg_assistant_handler = True
        root.addHandler(h)

logger = logging.getLogger("Manager")

# Тексты (резервные)
AUTO_REPLY_TEXT = (
    "Здравствуйте. Ваш номер не сохранен в моем списке контактов. "
    "Пожалуйста, укажите причину вашего обращения. Спасибо."
)
SECOND_REPLY_TEXT = (
    "Спасибо. Если причина стоящая, я скоро выйду с Вами на связь."
)

BLOCKED_EXTENSIONS = {'.apk', '.exe', '.bat', '.cmd', '.vbs', '.scr', '.js', '.com', '.msi'}

# Global guardrails for generated replies.
# These are appended to every AI task to keep behavior consistent even if per-account prompt changes.
AI_REPLY_RULES = (
    "Критично: отвечай на том же языке, на котором написал собеседник в MESSAGE/CONVERSATION_HISTORY. "
    "Не используй шаблонное обращение 'Имя Пользователя' и не выдумывай имя владельца. "
    "Если имя неизвестно, обращайся нейтрально без имени. "
    "Пиши коротко, вежливо и по делу."
)

ACCOUNTS_DIR = Path("accounts")
MANAGER_CONFIG = ACCOUNTS_DIR / "manager.json"
MANAGER_STATUS = ACCOUNTS_DIR / "manager_status.json"

# Prevent starting multiple supervisor instances that would contend on Telethon sqlite sessions.
SUPERVISOR_LOCK = ACCOUNTS_DIR / ".supervisor.lock"


def acquire_process_lock(lock_path: Path):
    """
    Cross-process lock (best-effort).
    On Linux, uses fcntl.flock for an actual advisory lock.
    Fallback: creates the file and keeps it open (not a real lock, but still helpful in most setups).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+", encoding="utf-8")
    try:
        f.seek(0)
        f.truncate(0)
        f.write(f"pid={os.getpid()}\nstarted_at={int(time.time())}\n")
        f.flush()
    except Exception:
        pass

    if fcntl is None:
        return f

    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        try:
            f.close()
        except Exception:
            pass
        raise RuntimeError(f"Another instance is already running (lock: {lock_path}).")

# ================= Утилиты =================

def load_json_file(filename, default):
    if not os.path.exists(filename):
        return default
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:
                return default
            return json.loads(content)
    except Exception as e:
        logger.error(f"Ошибка загрузки {filename}: {e}")
        return default

def atomic_save_json(filename, data):
    temp_file = str(filename) + '.tmp'
    try:
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, filename)
    except Exception as e:
        logger.error(f"Критическая ошибка при сохранении {filename}: {e}")
        if os.path.exists(temp_file):
            os.remove(temp_file)

async def expand_url(url):
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.head(url, allow_redirects=True) as response:
                if str(response.url) != url:
                    return str(response.url)
    except:
        pass
    return url

# ================= Классы =================

class UserRateLimiter:
    def __init__(self, limit=5, period=60):
        self.limit = limit
        self.period = period
        self.user_history = {}

    def is_allowed(self, user_id):
        now = time.time()
        if user_id not in self.user_history:
            self.user_history[user_id] = deque()
        history = self.user_history[user_id]
        while history and history[0] < now - self.period:
            history.popleft()
        if len(history) < self.limit:
            history.append(now)
            return True
        return False

class AIRateLimiter:
    """Ограничитель запросов к Gemini API."""
    def __init__(self, rpm_limit=15):
        self.rpm_limit = rpm_limit
        self.history = deque()
        self.cooldown_until = 0

    def can_request(self):
        now = time.time()
        if now < self.cooldown_until:
            return False, int(self.cooldown_until - now)
        
        # Очистка старой истории
        while self.history and self.history[0] < now - 60:
            self.history.popleft()
            
        if len(self.history) >= self.rpm_limit:
            return False, 60 - int(now - self.history[0])
            
        return True, 0

    def log_request(self):
        self.history.append(time.time())

    def set_cooldown(self, seconds=60):
        self.cooldown_until = time.time() + seconds

class TelegramAssistant:
    def __init__(self, session_name, api_id, api_hash, ai_client, gemini_prompt, phone=None):
        self.name = session_name
        self.api_id = api_id
        self.api_hash = api_hash
        self.ai_client = ai_client
        self.gemini_prompt = gemini_prompt
        self.phone = phone
        self.manager = None # Будет установлена менеджером
        
        self.logger = logging.getLogger(f"Bot_{session_name}")
        self.data_dir = ACCOUNTS_DIR / session_name
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        self.settings = self.load_settings()
        self.gemini_prompt = self.settings.get("gemini_prompt", gemini_prompt)
        self.ai_enabled = self.settings.get("ai_enabled", True)
        self.ai_model = self.settings.get("gemini_model", os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
        self.owner_name = str(self.settings.get("owner_name", "") or "").strip()
        
        # Файлы данных
        self.stats_file = self.data_dir / "stats.json"
        self.states_file = self.data_dir / "states.json"
        self.whitelist_file = self.data_dir / "whitelist.json"
        self.blacklist_file = self.data_dir / "blacklist.json"
        self.history_file = self.data_dir / "history.json"
        self.status_file = self.data_dir / "status.json"
        self.token_file = self.data_dir / "token_usage.json"
        
        # Загрузка данных
        self.stats = load_json_file(self.stats_file, {"blocked_files": 0, "blocked_scams": 0, "total_unknown": 0})
        # Обеспечение наличия всех ключей
        for key in ["blocked_files", "blocked_scams", "total_unknown"]:
            if key not in self.stats:
                self.stats[key] = 0
        
        self.user_states = load_json_file(self.states_file, {})
        # Преобразование ключей состояний в int
        self.user_states = {int(k): v for k, v in self.user_states.items()}
        
        self.whitelist = set(load_json_file(self.whitelist_file, []))
        self.blacklist = set(load_json_file(self.blacklist_file, []))
        self.history = load_json_file(self.history_file, {}) # {user_id: [messages]}
        self.history = {int(k): v for k, v in self.history.items()}
        
        self.client = TelegramClient(str(self.data_dir / session_name), api_id, api_hash)
        self.rate_limiter = UserRateLimiter(limit=5, period=60)
        self.ai_limiter = AIRateLimiter(rpm_limit=15)
        self.contact_ids = set()
        self.my_id = None

    # --- Работа с данными ---
    def save_data(self, key):
        if key == 'stats':
            atomic_save_json(self.stats_file, self.stats)
        elif key == 'states':
            atomic_save_json(self.states_file, self.user_states)
        elif key == 'whitelist':
            atomic_save_json(self.whitelist_file, list(self.whitelist))
        elif key == 'blacklist':
            atomic_save_json(self.blacklist_file, list(self.blacklist))
        elif key == 'history':
            atomic_save_json(self.history_file, self.history)

    def load_settings(self):
        settings_file = self.data_dir / "settings.json"
        if settings_file.exists():
            try:
                with open(settings_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                self.logger.error(f"Ошибка загрузки настроек для {self.name}: {e}")
        return {}

    def reset_account_data(self):
        """Полный сброс статистики и истории для этого аккаунта (Dev мод)"""
        self.stats = {"blocked_files": 0, "blocked_scams": 0, "total_unknown": 0}
        self.history = {}
        self.user_states = {}
        self.save_data('stats')
        self.save_data('history')
        self.save_data('states')
        self.logger.info("Данные аккаунта сброшены.")

    def update_stats(self, key):
        self.stats[key] = self.stats.get(key, 0) + 1
        self.save_data('stats')

    def add_to_history(self, user_id, role, text):
        if user_id not in self.history:
            self.history[user_id] = []
        self.history[user_id].append({"role": role, "text": text, "time": time.time()})
        # Храним последние 20 сообщений
        if len(self.history[user_id]) > 20:
            self.history[user_id] = self.history[user_id][-20:]
        self.save_data('history')

    def get_history_formatted(self, user_id):
        hist = self.history.get(user_id, [])
        formatted = ""
        for m in hist:
            role_name = "User" if m['role'] == 'user' else "Assistant"
            formatted += f"{role_name}: {m['text']}\n"
        return formatted

    # --- AI Логика ---
    # --- AI Логика с защитой от 429 ---
    def _report_token_usage(self, usage):
        """Запись использования токенов Gemini."""
        if not usage: return
        
        try:
            date_str = datetime.date.today().isoformat()
            data = load_json_file(self.token_file, {})
            
            day_data = data.get(date_str, {"input": 0, "output": 0})
            day_data["input"] += usage.prompt_token_count
            day_data["output"] += usage.candidates_token_count
            
            data[date_str] = day_data
            
            with open(self.token_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            self.logger.error(f"Error reporting token usage: {e}")

    async def _safe_ai_call(self, func, *args, **kwargs):
        """Обертка для AI вызовов с обработкой лимитов и ретраями."""
        if not self.ai_enabled or not self.ai_client:
            return None

        for attempt in range(3):
            allowed, wait_time = self.ai_limiter.can_request()
            if not allowed:
                self.logger.warning(f"AI Rate Limit. Waiting {wait_time}s...")
                await asyncio.sleep(min(wait_time, 5))
                if wait_time > 10: return None # Слишком долго ждать

            try:
                self.ai_limiter.log_request()
                return await func(*args, **kwargs)
            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                    backoff = (attempt + 1) * 20
                    self.logger.warning(f"AI Quota hit. Attempt {attempt+1}, backoff {backoff}s...")
                    self.ai_limiter.set_cooldown(backoff)
                    await asyncio.sleep(backoff)
                else:
                    self.logger.error(f"AI Call failed: {e}")
                    break
        return None

    async def get_ai_response(self, user_message, instruction, user_id=None):
        async def _call():
            history_str = ""
            if user_id:
                history_str = f"CONVERSATION_HISTORY:\n{self.get_history_formatted(user_id)}\n"
            owner_context = (
                f"ACCOUNT_OWNER_NAME: {self.owner_name}\n"
                if self.owner_name
                else "ACCOUNT_OWNER_NAME: UNKNOWN (do not invent)\n"
            )

            full_prompt = (
                f"SYSTEM_INSTRUCTIONS: {self.gemini_prompt}\n"
                f"GLOBAL_REPLY_RULES: {AI_REPLY_RULES}\n"
                f"{owner_context}"
                f"{history_str}"
                f"CURRENT_TASK: {instruction} "
                f"(обязательно отвечай на языке пользователя)\n"
                f"MESSAGE: {user_message}\n"
                f"Respond as defined in SYSTEM_INSTRUCTIONS."
            )
            response = await self.ai_client.aio.models.generate_content(
                model=self.ai_model, contents=full_prompt
            )
            self._report_token_usage(response.usage_metadata)
            return response.text.strip()
        
        return await self._safe_ai_call(_call)

    async def get_ai_voice_response(self, voice_path, instruction, user_id=None):
        async def _call():
            history_str = ""
            if user_id:
                history_str = f"CONVERSATION_HISTORY:\n{self.get_history_formatted(user_id)}\n"
            owner_context = (
                f"ACCOUNT_OWNER_NAME: {self.owner_name}\n"
                if self.owner_name
                else "ACCOUNT_OWNER_NAME: UNKNOWN (do not invent)\n"
            )

            with open(voice_path, 'rb') as f:
                uploaded_file = await self.ai_client.aio.files.upload(file=f)
            
            full_prompt = [
                f"SYSTEM_INSTRUCTIONS: {self.gemini_prompt}\n"
                f"GLOBAL_REPLY_RULES: {AI_REPLY_RULES}\n"
                f"{owner_context}"
                f"{history_str}"
                f"TASK: {instruction} (ответ на языке распознанной речи пользователя)",
                uploaded_file
            ]
            response = await self.ai_client.aio.models.generate_content(
                model=self.ai_model, contents=full_prompt
            )
            self._report_token_usage(response.usage_metadata)
            return response.text.strip()

        return await self._safe_ai_call(_call)

    async def is_scam_attempt(self, text):
        # Быстрая проверка без ИИ
        scam_keywords = ['crypto', 'invest', 'cashout', 'profit', 'giveaway', 'pump', 'dump']
        if any(kw in text.lower() for kw in scam_keywords) and ('http' in text or 't.me/' in text):
            self.logger.info("Local scam detection triggered.")
            return True, 90

        async def _call():
            prompt = f"Analyze for scam/spam. Return JSON: {{\"is_scam\": bool, \"confidence\": int}}. Message: {text}"
            response = await self.ai_client.aio.models.generate_content(
                model=self.ai_model, contents=prompt
            )
            self._report_token_usage(response.usage_metadata)
            match = re.search(r'\{.*\}', response.text, re.DOTALL)
            if match:
                data = json.loads(match.group())
                return data.get('is_scam', False), data.get('confidence', 0)
            return False, 0
            
        res = await self._safe_ai_call(_call)
        return res if res else (False, 0)

    # --- Авторизация ---
    async def auth(self, interactive=False):
        try:
            # Увеличиваем количество попыток подключения
            await self.client.connect()
        except Exception as e:
            if "database is locked" in str(e):
                self.logger.error(f"Critical connection error: database is locked. Possibly another process is using this session.")
            else:
                self.logger.error(f"Critical connection error: {e}")
            return False
        
        if await self.client.is_user_authorized():
            self.logger.info("Уже авторизован.")
            return True

        if not interactive:
            self.logger.warning(f"Аккаунт {self.name} не авторизован. Требуется вход через Dashboard.")
            return False

        print(f"\n--- Авторизация аккаунта: {self.name} ---")
        print("1. Телефон")
        print("2. QR-код")
        choice = input("Выбор: ")
        
        if choice == '2':
            qr = await self.client.qr_login()
            print("\nОтсканируйте QR:\n")
            qrc = qrcode.QRCode()
            qrc.add_data(qr.url)
            qrc.print_ascii(invert=True)
            
            user = None
            while not user:
                try:
                    user = await qr.wait(timeout=60)
                except asyncio.TimeoutError:
                    await qr.recreate()
                    qrc.clear()
                    qrc.add_data(qr.url)
                    qrc.print_ascii(invert=True)
                except Exception as e:
                    from telethon.errors import SessionPasswordNeededError
                    if isinstance(e, SessionPasswordNeededError):
                        pw = input("2FA Password: ")
                        user = await self.client.sign_in(password=pw)
                    else:
                        print(f"Error: {e}")
                        return False
            print(f"Успех: {user.first_name}")
            return True
        else:
            phone = self.phone or input("Phone: ")
            await self.client.start(phone=phone)
            return True

    # --- Обработчики ---
    def register_handlers(self):
        @self.client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def main_handler(event):
            sender = await event.get_sender()
            if not isinstance(sender, User) or sender.bot or sender.id == self.my_id:
                return

            if sender.id in self.blacklist: return
            if sender.id in self.contact_ids: return
            
            if not self.rate_limiter.is_allowed(sender.id):
                self.logger.warning(f"Rate limit: {sender.id}")
                return

            # Файлы
            if event.message.file:
                ext = os.path.splitext((event.message.file.name or "").lower())[1]
                if ext in BLOCKED_EXTENSIONS:
                    if sender.id not in self.whitelist:
                        await event.delete()
                        self.update_stats("blocked_files")
                        await self.client.send_message('me', f"🛑 Блок файла `{event.message.file.name}` от {sender.id}. /allow_{sender.id}")
                        return

            # Текст / Ссылки / Скам
            msg_text = event.message.message or "" # Гарантируем строку
            
            # Предварительная обработка медиа для истории и AI
            if msg_text:
                history_text = msg_text
            elif event.message.voice:
                history_text = "[Голосовое сообщение]"
            elif event.message.video_note:
                history_text = "[Видео-сообщение (кружок)]"
            elif event.message.file:
                history_text = f"[Файл: {event.message.file.name or 'без имени'}]"
            else:
                history_text = "[Медиа-сообщение]"

            if msg_text:
                urls = re.findall(r'(https?://\S+)', msg_text)
                expanded_urls = []
                for url in urls:
                    expanded = await expand_url(url)
                    if expanded != url:
                        expanded_urls.append(expanded)
                
                is_scam, conf = await self.is_scam_attempt(msg_text)
                if not is_scam and expanded_urls:
                    for e_url in expanded_urls:
                        is_scam, conf = await self.is_scam_attempt(f"Link redirects to: {e_url}")
                        if is_scam: break

                if is_scam and conf > 70:
                    self.update_stats("blocked_scams")
                    
                    if conf > 80:
                        # Агрессивная защита
                        try:
                            await event.delete()
                            self.blacklist.add(sender.id)
                            self.save_data('blacklist')
                            await self.client.send_message('me', 
                                f"🛡️ **Агрессивная защита сработала!**\n"
                                f"👤 От пользователя: `{sender.id}`\n"
                                f"📊 Уверенность AI: `{conf}%`\n"
                                f"🚫 Сообщение удалено, пользователь заблокирован.\n"
                                f"📝 Сообщение: `{msg_text}`"
                            )
                            return # Прекращаем обработку
                        except Exception as e:
                            self.logger.error(f"Error during aggressive scam block: {e}")
                    
                    # Если уверенность меньше 80%, просто уведомляем владельца
                    await self.client.send_message('me', f"🕵️‍♂️ СКАМ ({conf}%): {sender.id}\n{msg_text}")
                    if conf > 90: return

            # Добавляем во входящую историю
            self.add_to_history(sender.id, "user", history_text)

            state = self.user_states.get(sender.id, 0)
            if state == 0:
                self.update_stats("total_unknown")

            # Проверяем, включен ли AI в настройках
            self.settings = self.load_settings() # Reload settings to get latest
            self.gemini_prompt = self.settings.get("gemini_prompt", self.gemini_prompt)
            self.ai_model = self.settings.get("gemini_model", self.ai_model)
            self.owner_name = str(self.settings.get("owner_name", self.owner_name) or "").strip()
            if not self.settings.get("ai_enabled", True):
                # AI выключен, отправляем стандартный ответ
                final_reply = AUTO_REPLY_TEXT
                await event.reply(final_reply)
                self.add_to_history(sender.id, "assistant", final_reply)
                self.user_states[sender.id] = 1
                self.save_data('states')
                return # Завершаем обработку, если AI выключен

            if state == 0:
                async with self.client.action(sender.id, 'typing'):
                    await asyncio.sleep(2)
                instr = "Первый контакт. Спроси кто это и зачем пишет. Будь вежлив."
                if event.message.voice or event.message.audio:
                    path = await event.message.download_media()
                    reply = await self.get_ai_voice_response(path, instr, user_id=sender.id)
                    if os.path.exists(path): os.remove(path)
                else:
                    reply = await self.get_ai_response(history_text, instr, user_id=sender.id)
                
                final_reply = reply or AUTO_REPLY_TEXT
                await event.reply(final_reply)
                self.add_to_history(sender.id, "assistant", final_reply)
                
                self.user_states[sender.id] = 1
                self.save_data('states')
            
            elif state == 1:
                await self.client.send_message('me', f"❗️ Ответ от {sender.id}:\n{history_text}")
                async with self.client.action(sender.id, 'typing'):
                    await asyncio.sleep(2)
                instr = "Пользователь ответил. Поблагодари и скажи что передашь владельцу."
                reply = await self.get_ai_response(history_text, instr, user_id=sender.id)
                
                final_reply = reply or SECOND_REPLY_TEXT
                await event.reply(final_reply)
                self.add_to_history(sender.id, "assistant", final_reply)

                self.user_states[sender.id] = 2
                self.save_data('states')
            
            else:
                async with self.client.action(sender.id, 'typing'):
                    await asyncio.sleep(1)
                instr = "Диалог уже был уведомлен владельцу. Просто вежливо ответь на сообщение."
                reply = await self.get_ai_response(history_text, instr, user_id=sender.id)
                if reply:
                    await event.reply(reply)
                    self.add_to_history(sender.id, "assistant", reply)

        # Команды управления
        @self.client.on(events.NewMessage(pattern=r'/(?:allow|unblock)_(\d+)', from_users='me'))
        async def cmd_allow(event):
            uid = int(event.pattern_match.group(1))
            self.blacklist.discard(uid)
            self.whitelist.add(uid)
            self.save_data('blacklist')
            self.save_data('whitelist')
            await event.reply(f"✅ Пользователь `{uid}` разрешен и удален из ЧС.")

        @self.client.on(events.NewMessage(pattern=r'/block_(\d+)', from_users='me'))
        async def cmd_block(event):
            uid = int(event.pattern_match.group(1))
            self.whitelist.discard(uid)
            self.blacklist.add(uid)
            self.save_data('blacklist')
            self.save_data('whitelist')
            await event.reply(f"🚫 Пользователь `{uid}` в черном списке.")

        @self.client.on(events.NewMessage(pattern='/stats', from_users='me'))
        async def cmd_stats(event):
            await event.reply(
                f"📊 **Статистика [{self.name}]:**\n"
                f"• Блокировано файлов: `{self.stats.get('blocked_files', 0)}`\n"
                f"• Выявлено скама: `{self.stats.get('blocked_scams', 0)}`\n"
                f"• Новых контактов: `{self.stats.get('total_unknown', 0)}`"
            )

        @self.client.on(events.NewMessage(pattern='/whitelist', from_users='me'))
        async def cmd_list_whitelist(event):
            if not self.whitelist:
                await event.reply("Белый список пуст.")
                return
            text = f"**Белый список [{self.name}]:**\n\n"
            for uid in self.whitelist:
                text += f"• `{uid}`\n"
            text += "\nУдалить: `/unallow ID`"
            await event.reply(text)

        @self.client.on(events.NewMessage(pattern=r'/unallow (\d+)', from_users='me'))
        async def cmd_unallow(event):
            uid = int(event.pattern_match.group(1))
            if uid in self.whitelist:
                self.whitelist.remove(uid)
                self.save_data('whitelist')
                await event.reply(f"✅ `{uid}` удален из белого списка.")
            else:
                await event.reply(f"❌ `{uid}` не найден в белом списке.")

        @self.client.on(events.NewMessage(pattern='/reset'))
        async def reset_handler(event):
            if not await self.is_admin(event): return
            self.reset_account_data()
            await event.reply("✅ История и статистика этого аккаунта очищены.")

        @self.client.on(events.NewMessage(pattern='/panel', from_users='me'))
        async def cmd_panel(event):
            if not self.manager:
                await event.reply("Ошибка: Менеджер не инициализирован.")
                return
            
            status_text = "🖥 **Панель управления Telegram Assistant**\n\n"
            for acc in self.manager.config['accounts']:
                status_text += f"👤 **{acc['name']}**: ✅ Работает\n"
            
            status_text += f"\nАктивный аккаунт: `{self.name}`\n"
            status_text += "Доступные команды:\n"
            status_text += "• `/stats` — общая статистика\n"
            status_text += "• `/whitelist` — список разрешенных\n"
            status_text += "• `/help` — список всех команд"
            
            await event.reply(status_text)

        @self.client.on(events.NewMessage(pattern='/help', from_users='me'))
        async def cmd_help(event):
            help_text = (
                "🆘 **Справка по командам:**\n\n"
                "• `/panel` — статус всех аккаунтов\n"
                "• `/stats` — статистика текущего бота\n"
                "• `/whitelist` — белый список\n"
                "• `/block_ID` — добавить ID в ЧС\n"
                "• `/allow_ID` — добавить ID в белый список\n"
                "• `/unallow ID` — удалить из белого списка\n"
            )
            await event.reply(help_text)

    async def is_admin(self, event):
        """Проверка, является ли отправитель владельцем аккаунта."""
        sender_id = event.sender_id
        return sender_id == self.my_id

    async def refresh_contacts(self):
        while True:
            try:
                res = await self.client(GetContactsRequest(hash=0))
                self.contact_ids = {u.id for u in res.users}
                self.logger.info(f"Contacts updated: {len(self.contact_ids)}")
            except: pass
            await asyncio.sleep(1800)

    async def _heartbeat(self):
        """Фоновая задача для обновления статуса в JSON для дашборда"""
        import time
        while True:
            try:
                status_data = {
                    "status": "online",
                    "last_seen": time.time(),
                    "pid": os.getpid()
                }
                atomic_save_json(self.status_file, status_data)
            except Exception:
                pass
            await asyncio.sleep(10)

    async def run(self, interactive=False):
        # Очищаем старый статус при запуске
        atomic_save_json(self.status_file, {"status": "starting", "last_seen": 0})
        
        if not await self.auth(interactive=interactive): return False
        
        # Запускаем heartbeat в фоне
        heartbeat_task = asyncio.create_task(self._heartbeat())
        
        try:
            self.my_id = (await self.client.get_me()).id
            res = await self.client(GetContactsRequest(hash=0))
            self.contact_ids = {u.id for u in res.users}
            self.register_handlers()
            asyncio.create_task(self.refresh_contacts())
            self.logger.info("Запущен.")
            await self.client.run_until_disconnected()
            return True
        except asyncio.CancelledError:
            return True # Штатное завершение
        except Exception as e:
            if "database is locked" in str(e):
                self.logger.error(f"Error in bot execution: database is locked. Ensure no other scripts are using this session ({self.name}).")
            else:
                self.logger.error(f"Error in bot execution: {e}")
            return False
        finally:
            heartbeat_task.cancel()
            atomic_save_json(self.status_file, {"status": "offline", "last_seen": 0})
            try:
                await self.client.disconnect()
            except (Exception, RuntimeError, asyncio.CancelledError):
                pass
            self.logger.info("Отключен.")

# ================= Менеджер Аккаунтов =================

class AccountManager:
    def __init__(self):
        ACCOUNTS_DIR.mkdir(exist_ok=True)
        self.config = load_json_file(MANAGER_CONFIG, {"accounts": []})
        
        # Глобальный AI клиент
        self.ai_client = None
        key = os.getenv('GEMINI_API_KEY')
        if key:
            self.ai_client = genai.Client(api_key=key)
        self.prompt = os.getenv('GEMINI_PROMPT', "Ты — ассистент.")
        
        # Для веб-авторизации
        # session_name: {client, qr|phone_code_hash, status, started_at, type, bot_instance, ...}
        # Important: any connected Telethon client keeps the sqlite session file open.
        # If we don't disconnect it explicitly, the supervisor may later fail with "database is locked".
        self.active_auths = {}
        
        # Активные экземпляры ботов
        self.bots = {} # session_name: TelegramAssistant

    def _reload_config(self):
        self.config = load_json_file(MANAGER_CONFIG, {"accounts": []})

    def _auth_marker_path(self, name: str) -> Path:
        # Marker used to coordinate between dashboard process and supervisor process.
        return (ACCOUNTS_DIR / name / "auth_in_progress.json")

    def _restart_marker_path(self, name: str) -> Path:
        # Cross-process restart request marker (created by dashboard, consumed by supervisor).
        return (ACCOUNTS_DIR / name / "restart_requested.json")

    def _consume_restart_marker(self, name: str) -> bool:
        """Return True if a restart was requested for this account (and consume the marker)."""
        try:
            marker = self._restart_marker_path(name)
            if marker.exists():
                marker.unlink()
                return True
        except Exception:
            pass
        return False

    def _write_auth_marker(self, name: str, auth_type: str):
        try:
            marker = self._auth_marker_path(name)
            marker.parent.mkdir(parents=True, exist_ok=True)
            atomic_save_json(
                marker,
                {
                    "started_at": time.time(),
                    "pid": os.getpid(),
                    "type": auth_type,
                },
            )
        except Exception:
            pass

    def _remove_auth_marker(self, name: str):
        try:
            marker = self._auth_marker_path(name)
            if marker.exists():
                marker.unlink()
        except Exception:
            pass

    async def _safe_disconnect(self, client):
        """Best-effort disconnect. Prevents sqlite session locks lingering in the dashboard process."""
        if not client:
            return
        try:
            await client.disconnect()
        except (Exception, RuntimeError, asyncio.CancelledError):
            pass

    async def _cleanup_auth(self, name):
        """Disconnect and remove an auth session if it exists."""
        auth = self.active_auths.get(name)
        if not auth:
            return
        client = auth.get("client")
        await self._safe_disconnect(client)
        self.active_auths.pop(name, None)
        self._remove_auth_marker(name)

    async def _cleanup_stale_auths(self, ttl_seconds=300):
        """Disconnect stale auth sessions to avoid session DB locks."""
        now = time.time()
        stale = []
        for name, auth in list(self.active_auths.items()):
            started_at = auth.get("started_at") or 0
            if started_at and (now - started_at) > ttl_seconds:
                stale.append(name)
        for name in stale:
            await self._cleanup_auth(name)

    def save_config(self):
        atomic_save_json(MANAGER_CONFIG, self.config)

    async def add_account(self):
        """Интерактивный метод для консоли"""
        if not sys.stdin.isatty():
            print("❌ Этот метод только для интерактивного режима. Используйте веб-панель.")
            return

        name = input("Введите имя для этого аккаунта (латиница): ").strip()
        if not name: return
        
        if any(acc['name'] == name for acc in self.config['accounts']):
            print(f"❌ Аккаунт с именем {name} уже существует.")
            return

        api_id = os.getenv('API_ID')
        api_hash = os.getenv('API_HASH')
        
        bot = TelegramAssistant(name, api_id, api_hash, self.ai_client, self.prompt)
        if await bot.auth():
            if not any(acc['name'] == name for acc in self.config['accounts']):
                self.config['accounts'].append({"name": name})
                self.save_config()
            print(f"✅ Аккаунт {name} успешно добавлен!")
            await bot.client.disconnect()

    async def add_account_web_start(self, name):
        """Начало процесса QR-авторизации для веба"""
        await self._cleanup_stale_auths()
        self._reload_config()
        if any(acc['name'] == name for acc in self.config['accounts']):
            return {"status": "error", "message": "Account exists"}

        api_id = os.getenv('API_ID')
        api_hash = os.getenv('API_HASH')
        
        bot = TelegramAssistant(name, api_id, api_hash, self.ai_client, self.prompt)
        try:
            await bot.client.connect()
        except Exception as e:
            await self._safe_disconnect(bot.client)
            return {"status": "error", "message": str(e)}
        
        # Если уже авторизован (сессия жива)
        if await bot.client.is_user_authorized():
            if not any(acc['name'] == name for acc in self.config['accounts']):
                self.config['accounts'].append({"name": name})
                self.save_config()
            await self._safe_disconnect(bot.client)
            return {"status": "success", "message": "Already authorized"}

        # Начинаем QR
        qr = await bot.client.qr_login()
        self.active_auths[name] = {
            "client": bot.client,
            "qr": qr,
            "status": "waiting_qr",
            "bot_instance": bot,
            "started_at": time.time(),
            "type": "qr",
        }
        self._write_auth_marker(name, "qr")
        return {"status": "qr", "url": qr.url}

    async def add_account_web_check(self, name):
        """Проверка статуса QR-авторизации"""
        await self._cleanup_stale_auths()
        self._reload_config()
        if name not in self.active_auths:
            return {"status": "not_found"}
        
        auth = self.active_auths[name]
        qr = auth['qr']
        client = auth['client']
        
        try:
            user = await qr.wait(timeout=2)
            if user:
                if not any(acc['name'] == name for acc in self.config['accounts']):
                    self.config['accounts'].append({"name": name})
                    self.save_config()
                await self._cleanup_auth(name)
                return {"status": "success", "user": user.first_name}
        except asyncio.TimeoutError:
            return {"status": "waiting"}
        except Exception as e:
            from telethon.errors import SessionPasswordNeededError
            if isinstance(e, SessionPasswordNeededError):
                auth['status'] = 'waiting_2fa'
                return {"status": "2fa_needed"}
            await self._cleanup_auth(name)
            return {"status": "error", "message": str(e)}
        
        return {"status": "waiting"}

    async def add_account_web_2fa(self, name, password):
        """Ввод 2FA пароля для веба"""
        await self._cleanup_stale_auths()
        self._reload_config()
        if name not in self.active_auths:
            return {"status": "not_found"}
        
        auth = self.active_auths[name]
        client = auth['client']
        try:
            # Check if it is a QR login or Phone login
            if auth.get('type') == 'phone':
                 user = await client.sign_in(password=password)
            else:
                 user = await client.sign_in(password=password)

            if user:
                if not any(acc['name'] == name for acc in self.config['accounts']):
                    self.config['accounts'].append({"name": name})
                    self.save_config()
                await self._cleanup_auth(name)
                return {"status": "success", "user": user.first_name}
        except Exception as e:
            await self._cleanup_auth(name)
            return {"status": "error", "message": str(e)}

    async def add_account_phone_start(self, name, phone):
        """Start phone authentication"""
        await self._cleanup_stale_auths()
        self._reload_config()
        if any(acc['name'] == name for acc in self.config['accounts']):
            return {"status": "error", "message": "Account exists"}
        
        api_id = os.getenv('API_ID')
        api_hash = os.getenv('API_HASH')
        
        bot = TelegramAssistant(name, api_id, api_hash, self.ai_client, self.prompt)
        try:
            await bot.client.connect()
        except Exception as e:
            await self._safe_disconnect(bot.client)
            return {"status": "error", "message": str(e)}
        
        if await bot.client.is_user_authorized():
            if not any(acc['name'] == name for acc in self.config['accounts']):
                self.config['accounts'].append({"name": name})
                self.save_config()
            await self._safe_disconnect(bot.client)
            return {"status": "success", "message": "Already authorized"}
            
        try:
            sent_code = await bot.client.send_code_request(phone)
            self.active_auths[name] = {
                "client": bot.client,
                "phone": phone,
                "phone_code_hash": sent_code.phone_code_hash,
                "type": "phone",
                "status": "waiting_code",
                "bot_instance": bot,
                "started_at": time.time(),
            }
            self._write_auth_marker(name, "phone")
            return {"status": "sent"}
        except Exception as e:
            await self._safe_disconnect(bot.client)
            return {"status": "error", "message": str(e)}

    async def add_account_phone_verify(self, name, code, phone):
        """Verify phone code"""
        await self._cleanup_stale_auths()
        self._reload_config()
        if name not in self.active_auths:
             return {"status": "error", "message": "Session expired"}
             
        auth = self.active_auths[name]
        client = auth['client']
        phone_code_hash = auth['phone_code_hash']
        
        try:
            user = await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            if user:
                if not any(acc['name'] == name for acc in self.config['accounts']):
                    self.config['accounts'].append({"name": name})
                    self.save_config()
                await self._cleanup_auth(name)
                return {"status": "success", "user": user.first_name}
        except Exception as e:
            from telethon.errors import SessionPasswordNeededError
            if isinstance(e, SessionPasswordNeededError):
                 return {"status": "2fa_needed"}
            await self._cleanup_auth(name)
            return {"status": "error", "message": str(e)}
        return {"status": "error", "message": "Unknown error"}

    async def run_all(self, stop_event: asyncio.Event | None = None):
        """Бесконечный цикл-супервизор для управления всеми ботами"""
        running_tasks = {} # name: task_object
        
        print("🚀 Менеджер запущен в режиме супервизора.")
        
        try:
            while True:
                if stop_event and stop_event.is_set():
                    break
                # Перезагружаем конфиг, чтобы увидеть новые аккаунты
                self.config = load_json_file(MANAGER_CONFIG, {"accounts": []})
                current_names = {acc['name'] for acc in self.config['accounts']}

                # Heartbeat for dashboard/monitoring.
                try:
                    atomic_save_json(
                        MANAGER_STATUS,
                        {
                            "status": "running",
                            "pid": os.getpid(),
                            "ts": time.time(),
                            "accounts_configured": len(current_names),
                            "bots_running": len(running_tasks),
                        },
                    )
                except Exception:
                    pass
                
                # 1. Останавливаем удаленные аккаунты
                to_stop = set(running_tasks.keys()) - current_names
                for name in to_stop:
                    logger.info(f"🛑 Останавливаем аккаунт: {name}")
                    running_tasks[name].cancel()
                    del running_tasks[name]
                    if name in self.bots: del self.bots[name]

                # 2. Запускаем новые аккаунты
                for name in current_names:
                    # If the dashboard requested a restart for this account, stop it first (if running).
                    if self._consume_restart_marker(name) and name in running_tasks:
                        logger.info(f"🔁 [{name}] Запрошен перезапуск аккаунта.")
                        try:
                            running_tasks[name].cancel()
                            await asyncio.gather(running_tasks[name], return_exceptions=True)
                        except Exception:
                            pass
                        del running_tasks[name]
                        if name in self.bots:
                            del self.bots[name]

                    if name not in running_tasks:
                        # If the dashboard is currently authenticating this account, the session sqlite file is
                        # very likely to be locked. Skip starting until auth completes (marker removed).
                        marker = self._auth_marker_path(name)
                        if marker.exists():
                            marker_data = load_json_file(marker, {})
                            started_at = marker_data.get("started_at", 0) or 0
                            # If marker is stale, remove it and try starting.
                            if started_at and (time.time() - started_at) < 15 * 60:
                                logger.info(f"⏳ [{name}] Авторизация в процессе (Dashboard). Запуск отложен.")
                                continue
                            self._remove_auth_marker(name)

                        api_id = os.getenv('API_ID')
                        api_hash = os.getenv('API_HASH')
                        bot = TelegramAssistant(name, api_id, api_hash, self.ai_client, self.prompt)
                        bot.manager = self
                        self.bots[name] = bot
                        
                        logger.info(f"✨ Обнаружен новый аккаунт: {name}. Запуск...")
                        task = asyncio.create_task(self._safe_run(bot))
                        running_tasks[name] = task

                # 3. Очистка завершенных задач
                finished = [n for n, t in running_tasks.items() if t.done()]
                for n in finished:
                    try:
                        running_tasks[n].result() # Проверка на ошибки
                    except Exception as e:
                        logger.error(f"❌ Критическая ошибка в боте {n}: {e}")
                    del running_tasks[n]
                    if n in self.bots: del self.bots[n]

                if stop_event and stop_event.is_set():
                    break

                if not running_tasks:
                    # Если совсем пусто, просто ждем
                    await asyncio.sleep(5)
                else:
                    await asyncio.sleep(10) # Проверяем конфиг каждые 10 секунд
                    
        except asyncio.CancelledError:
            pass
        finally:
            # Ensure all running bots are asked to stop.
            for t in running_tasks.values():
                t.cancel()
            await asyncio.gather(*running_tasks.values(), return_exceptions=True)
            try:
                atomic_save_json(
                    MANAGER_STATUS,
                    {
                        "status": "stopped",
                        "pid": os.getpid(),
                        "ts": time.time(),
                        "bots_running": 0,
                    },
                )
            except Exception:
                pass

    async def _safe_run(self, bot):
        """Безопасный запуск одного бота с перезапуском при сбоях"""
        retry_delay = 5
        try:
            while True:
                try:
                    success = await bot.run(interactive=False)
                    if not success:
                        # Если не авторизован или ошибка — ждем дольше, чтобы не спамить в БД
                        logger.info(f"[{bot.name}] Ожидание авторизации или ошибка подключения. Пауза 60с...")
                        await asyncio.sleep(60)
                        continue
                    
                    # Если run() завершился сам (штатное отключение клиента)
                    await asyncio.sleep(retry_delay)
                except Exception as e:
                    err_msg = str(e)
                    if "database is locked" in err_msg:
                        logger.warning(f"[{bot.name}] БД заблокирована другим процессом. Ждем 45с...")
                        await asyncio.sleep(45)
                    elif "Connection to" in err_msg and "failed" in err_msg:
                         logger.warning(f"[{bot.name}] Ошибка сети. Ждем {retry_delay}с...")
                         await asyncio.sleep(retry_delay)
                         retry_delay = min(retry_delay * 2, 60)
                    else:
                        logger.error(f"[{bot.name}] Сбой: {e}. Перезапуск через {retry_delay}с...")
                        await asyncio.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, 300)
        except asyncio.CancelledError:
            pass

def parse_arguments():
    load_dotenv()
    
    parser = argparse.ArgumentParser(description="Telegram Multi-Assistant")
    parser.add_argument('--daemon', action='store_true', help='Запустить всех ботов в фоновом режиме (без меню)')
    args = parser.parse_args()
    return args

async def main():
    load_dotenv()
    setup_logging()
    args = parse_arguments()
    manager = AccountManager()

    # Ensure single instance to avoid sqlite session locks ("database is locked").
    try:
        lock_file = acquire_process_lock(SUPERVISOR_LOCK)
    except Exception as e:
        logger.error(str(e))
        return

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(signame: str):
        logger.info(f"🛑 Получен сигнал {signame}. Остановка...")
        stop_event.set()

    for sig, name in ((signal.SIGTERM, "SIGTERM"), (signal.SIGINT, "SIGINT")):
        try:
            loop.add_signal_handler(sig, _request_stop, name)
        except (NotImplementedError, RuntimeError):
            # Windows / some embedded environments
            try:
                signal.signal(sig, lambda *_: _request_stop(name))
            except Exception:
                pass
    
    # Если запущен в фоне (systemd) или передан флаг --daemon
    if args.daemon or not sys.stdin.isatty():
        logger.info("Запуск в неинтерактивном режиме...")
        try:
            await manager.run_all(stop_event=stop_event)
        finally:
            try:
                lock_file.close()
            except Exception:
                pass
        return

    # В main.py больше не будет меню, оно переезжает в control_panel.py
    # Но для обратной совместимости или удобства, если запущен интерактивно без флагов:
    print("Подсказка: Для управления аккаунтами используйте 'python control_panel.py'")
    try:
        await manager.run_all(stop_event=stop_event)
    finally:
        try:
            lock_file.close()
        except Exception:
            pass

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Остановка...")
