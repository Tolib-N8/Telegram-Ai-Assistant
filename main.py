import os
import asyncio
import json
import re
import logging
import time
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

# ================= Конфигурация и Константы =================

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(name)s] - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
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

ACCOUNTS_DIR = Path("accounts")
MANAGER_CONFIG = ACCOUNTS_DIR / "manager.json"

# ================= Утилиты =================

def load_json_file(filename, default):
    if not os.path.exists(filename):
        return default
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
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
        
        # Файлы данных
        self.stats_file = self.data_dir / "stats.json"
        self.states_file = self.data_dir / "states.json"
        self.whitelist_file = self.data_dir / "whitelist.json"
        self.blacklist_file = self.data_dir / "blacklist.json"
        
        # Загрузка данных
        self.stats = load_json_file(self.stats_file, {"blocked_files": 0, "blocked_scams": 0, "total_unknown": 0})
        self.user_states = load_json_file(self.states_file, {})
        # Преобразование ключей состояний в int
        self.user_states = {int(k): v for k, v in self.user_states.items()}
        
        self.whitelist = set(load_json_file(self.whitelist_file, []))
        self.blacklist = set(load_json_file(self.blacklist_file, []))
        
        self.client = TelegramClient(str(self.data_dir / session_name), api_id, api_hash)
        self.rate_limiter = UserRateLimiter(limit=5, period=60)
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

    def update_stats(self, key):
        self.stats[key] = self.stats.get(key, 0) + 1
        self.save_data('stats')

    # --- AI Логика ---
    async def get_ai_response(self, user_message, instruction):
        if not self.ai_client: return None
        try:
            full_prompt = (
                f"SYSTEM_INSTRUCTIONS: {self.gemini_prompt}\n"
                f"CURRENT_TASK: {instruction}\n"
                f"MESSAGE: {user_message}\n"
                f"Respond as defined in SYSTEM_INSTRUCTIONS."
            )
            response = await self.ai_client.aio.models.generate_content(
                model='gemini-1.5-flash', contents=full_prompt
            )
            return response.text.strip()
        except Exception as e:
            self.logger.error(f"AI Error: {e}")
            return None

    async def get_ai_voice_response(self, voice_path, instruction):
        if not self.ai_client: return None
        try:
            with open(voice_path, 'rb') as f:
                uploaded_file = await self.ai_client.aio.files.upload(file=f)
            full_prompt = [
                f"{self.gemini_prompt}\n\nTASK: {instruction}",
                uploaded_file
            ]
            response = await self.ai_client.aio.models.generate_content(
                model='gemini-1.5-flash', contents=full_prompt
            )
            return response.text.strip()
        except Exception as e:
            self.logger.error(f"AI Voice Error: {e}")
            return None

    async def is_scam_attempt(self, text):
        if not self.ai_client: return False, 0
        try:
            prompt = f"Analyze for scam/spam. Return JSON: {{\"is_scam\": bool, \"confidence\": int}}. Message: {text}"
            response = await self.ai_client.aio.models.generate_content(
                model='gemini-1.5-flash', contents=prompt
            )
            match = re.search(r'\{.*\}', response.text, re.DOTALL)
            if match:
                data = json.loads(match.group())
                return data.get('is_scam', False), data.get('confidence', 0)
        except:
            pass
        return False, 0

    # --- Авторизация ---
    async def auth(self):
        await self.client.connect()
        if await self.client.is_user_authorized():
            self.logger.info("Уже авторизован.")
            return True

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
            if not sys.stdin.isatty():
                self.logger.error(f"Авторизация для {self.name} не удалась: Требуется ввод телефона/кода в интерактивном режиме.")
                return False
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
            msg_text = event.message.message
            if msg_text:
                urls = re.findall(r'(https?://\S+)', msg_text)
                for url in urls:
                    await expand_url(url) # Логируем раскрытие внутри если надо
                
                is_scam, conf = await self.is_scam_attempt(msg_text)
                if is_scam and conf > 70:
                    self.update_stats("blocked_scams")
                    await self.client.send_message('me', f"🕵️‍♂️ СКАМ ({conf}%): {sender.id}\n{msg_text}")
                    if conf > 90: return

            self.update_stats("total_unknown")
            state = self.user_states.get(sender.id, 0)

            if state == 0:
                async with self.client.action(sender.id, 'typing'):
                    await asyncio.sleep(2)
                instr = "Первый контакт. Спроси кто это и зачем пишет."
                if event.message.voice or event.message.audio:
                    path = await event.message.download_media()
                    reply = await self.get_ai_voice_response(path, instr)
                    if os.path.exists(path): os.remove(path)
                else:
                    reply = await self.get_ai_response(msg_text, instr)
                
                await event.reply(reply or AUTO_REPLY_TEXT)
                self.user_states[sender.id] = 1
                self.save_data('states')
            
            elif state == 1:
                await self.client.send_message('me', f"❗️ Ответ от {sender.id}:\n{msg_text or '🎵 Voice'}")
                async with self.client.action(sender.id, 'typing'):
                    await asyncio.sleep(2)
                instr = "Пользователь ответил. Поблагодари и скажи что передашь владельцу."
                reply = await self.get_ai_response(msg_text, instr)
                await event.reply(reply or SECOND_REPLY_TEXT)
                self.user_states[sender.id] = 2
                self.save_data('states')

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
                f"• Блокировано файлов: `{self.stats['blocked_files']}`\n"
                f"• Выявлено скама: `{self.stats['blocked_scams']}`\n"
                f"• Новых контактов: `{self.stats['total_unknown']}`"
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

    async def refresh_contacts(self):
        while True:
            try:
                res = await self.client(GetContactsRequest(hash=0))
                self.contact_ids = {u.id for u in res.users}
                self.logger.info(f"Contacts updated: {len(self.contact_ids)}")
            except: pass
            await asyncio.sleep(1800)

    async def run(self):
        if not await self.auth(): return
        self.my_id = (await self.client.get_me()).id
        res = await self.client(GetContactsRequest(hash=0))
        self.contact_ids = {u.id for u in res.users}
        self.register_handlers()
        asyncio.create_task(self.refresh_contacts())
        self.logger.info("Запущен.")
        await self.client.run_until_disconnected()

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

    def save_config(self):
        atomic_save_json(MANAGER_CONFIG, self.config)

    async def add_account(self):
        name = input("Введите имя для этого аккаунта (латиница): ").strip()
        if not name: return
        
        # Проверка на дубликаты
        if any(acc['name'] == name for acc in self.config['accounts']):
            print(f"❌ Аккаунт с именем {name} уже существует.")
            return

        api_id = os.getenv('API_ID')
        api_hash = os.getenv('API_HASH')
        
        bot = TelegramAssistant(name, api_id, api_hash, self.ai_client, self.prompt)
        if await bot.auth():
            self.config['accounts'].append({"name": name})
            self.save_config()
            print(f"✅ Аккаунт {name} успешно добавлен в список!")
            print("Теперь вы можете запустить всех ботов из главного меню.")
            await bot.client.disconnect()

    async def run_all(self):
        if not self.config['accounts']:
            print("Нет активных аккаунтов. Добавьте первый.")
            return

        api_id = os.getenv('API_ID')
        api_hash = os.getenv('API_HASH')
        
        tasks = []
        for acc in self.config['accounts']:
            bot = TelegramAssistant(acc['name'], api_id, api_hash, self.ai_client, self.prompt)
            bot.manager = self # Даем ссылку на менеджер для /panel
            tasks.append(bot.run())
        
        print(f"🚀 Запуск {len(tasks)} аккаунтов... (Ctrl+C для остановки)")
        try:
            await asyncio.gather(*tasks)
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
    args = parse_arguments()
    manager = AccountManager()
    
    # Если запущен в фоне (systemd) или передан флаг --daemon
    if args.daemon or not sys.stdin.isatty():
        logger.info("Запуск в неинтерактивном режиме...")
        await manager.run_all()
        return

    # В main.py больше не будет меню, оно переезжает в control_panel.py
    # Но для обратной совместимости или удобства, если запущен интерактивно без флагов:
    print("Подсказка: Для управления аккаунтами используйте 'python control_panel.py'")
    await manager.run_all()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Остановка...")
