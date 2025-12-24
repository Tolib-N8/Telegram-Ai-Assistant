import os
import asyncio
from telethon import TelegramClient, events
from telethon.tl.types import User
from telethon.tl.functions.contacts import GetContactsRequest
import google.generativeai as genai
from dotenv import load_dotenv
import json
import os
from telethon import Button

# Загрузка переменных окружения
load_dotenv()

# Получение переменных из .env
api_id = os.getenv('API_ID')
api_hash = os.getenv('API_HASH')
phone_number = os.getenv('PHONE')
gemini_key = os.getenv('GEMINI_API_KEY')
gemini_prompt = os.getenv('GEMINI_PROMPT', (
    "Ты — умный автоответчик в Telegram. Ты работаешь от имени владельца аккаунта. "
    "Твоя цель — фильтровать входящие сообщения от незнакомцев. "
    "Стиль общения: вежливый, сдержанный, но не роботизированный."
))

if not api_id or not api_hash:
    print("Ошибка: API_ID и API_HASH должны быть указаны в файле .env")
    exit(1)

# Преобразование api_id в int
try:
    api_id = int(api_id)
except ValueError:
    print("Ошибка: API_ID должен быть числом")
    exit(1)

# Текст автоответчика (резерв)
AUTO_REPLY_TEXT = (
    "Здравствуйте. Ваш номер не сохранен в моем списке контактов. "
    "Пожалуйста, укажите причину вашего обращения. Спасибо."
)

SECOND_REPLY_TEXT = (
    "Спасибо. Если причина стоящая, я скоро выйду с Вами на связь."
)

# ... (переменные окружения) ...

# Список расширений, которые считаются опасными (вирусы/скрипты)
BLOCKED_EXTENSIONS = {'.apk', '.exe', '.bat', '.cmd', '.vbs', '.scr', '.js', '.com', '.msi'}

# Файл для хранения состояний
STATES_FILE = 'states.json'
WHITELIST_FILE = 'whitelist.json'

def load_whitelist():
    if not os.path.exists(WHITELIST_FILE):
        return set()
    try:
        with open(WHITELIST_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return set(data)
    except Exception as e:
        print(f"Ошибка загрузки белого списка: {e}")
        return set()

def save_to_whitelist(user_id):
    whitelist = load_whitelist()
    whitelist.add(user_id)
    try:
        with open(WHITELIST_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(whitelist), f)
    except Exception as e:
        print(f"Ошибка сохранения в белый список: {e}")

def remove_from_whitelist(user_id):
    whitelist = load_whitelist()
    if user_id in whitelist:
        whitelist.remove(user_id)
        try:
            with open(WHITELIST_FILE, 'w', encoding='utf-8') as f:
                json.dump(list(whitelist), f)
            return True
        except Exception as e:
            print(f"Ошибка удаления из белого списка: {e}")
    return False

def load_states():
    if not os.path.exists(STATES_FILE):
        return {}
    try:
        with open(STATES_FILE, 'r', encoding='utf-8') as f:
            # Загружаем и конвертируем ключи в int (JSON хранит ключи как строки)
            data = json.load(f)
            return {int(k): v for k, v in data.items()}
    except Exception as e:
        print(f"Ошибка загрузки состояний: {e}")
        return {}

def save_state(user_id, state):
    states = load_states()
    states[user_id] = state
    try:
        with open(STATES_FILE, 'w', encoding='utf-8') as f:
            json.dump(states, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка сохранения состояния: {e}")

# Создание клиента Telegram
client = TelegramClient('anon_session', api_id, api_hash)

# Настройка Gemini
model = None
if gemini_key:
    genai.configure(api_key=gemini_key)
    try:
        model = genai.GenerativeModel('gemini-2.5-flash')
    except Exception as e:
        print(f"Ошибка инициализации модели: {e}")
        model = None
else:
    print("⚠️ Предупреждение: GEMINI_API_KEY не найден. Бот будет использовать старые фразы.")

async def get_ai_response(user_message, instruction):
    if not model:
        return None
    try:
        # Формируем полный промпт
        full_prompt = (
            f"{gemini_prompt}\n\n"
            f"СИТУАЦИЯ: {instruction}\n\n"
            f"СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ: {user_message}\n\n"
            f"ТВОЙ ОТВЕТ:"
        )
        
        response = await model.generate_content_async(full_prompt)
        return response.text
    except Exception as e:
        print(f"Ошибка Gemini: {e}")
        return None

async def get_ai_voice_response(voice_path, instruction):
    if not model:
        return None
    try:
        # Загрузка файла в Gemini
        print(f"Загружаю аудио: {voice_path}")
        uploaded_file = genai.upload_file(voice_path)
        
        # Ожидание обработки (обычно быстро для аудио, но на всякий случай)
        # В версии 1.5/2.5 flash это почти мгновенно
        
        full_prompt = [
            f"{gemini_prompt}\n\nСИТУАЦИЯ: {instruction}\n\nПользователь прислал голосовое сообщение. Прослушай его и ответь текстом.",
            uploaded_file
        ]
        
        response = await model.generate_content_async(full_prompt)
        
        # Удаляем файл из облака (опционально, но хорошая практика)
        # uploaded_file.delete() # Если библиотека поддерживает, или оставим авто-клинпап
        
        return response.text
    except Exception as e:
        print(f"Ошибка Gemini Voice: {e}")
        return None

async def main():
    print("Запуск ассистента...")
    await client.start(phone=phone_number)
    print("Ассистент запущен и слушает входящие сообщения.")

    # Получаем список контактов при старте, чтобы не делать запросы постоянно
    # Примечание: телеграм кэширует контакты, но лучше иметь локальный set ID для скорости
    contact_ids = set()
    # Используем прямой запрос к API, так как client.iter_contacts может быть недоступен
    result = await client(GetContactsRequest(hash=0))
    contact_ids = {u.id for u in result.users}
    
    # Также добавляем свой ID, чтобы не отвечать самому себе (хотя event.out проверяется отдельно)
    me = await client.get_me()
    my_id = me.id

    print(f"Загружено {len(contact_ids)} контактов.")

    # Загружаем состояния диалогов из файла
    user_states = load_states()
    print(f"Загружено {len(user_states)} активных диалогов.")

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def handler(event):
        sender = await event.get_sender()
        
        # Проверяем, что отправитель - это пользователь, а не бот или что-то еще
        if not isinstance(sender, User):
            return

        # Игнорируем ботов
        if sender.bot:
            return

        # Если сообщение от нас самих (уже отфильтровано incoming=True, но на всякий случай)
        if sender.id == my_id:
            return

        # --- ЗАЩИТА ОТ ВРЕДОНОСНЫХ ФАЙЛОВ ---
        if event.message.file:
            filename = event.message.file.name or ""
            extension = os.path.splitext(filename.lower())[1]
            
            if extension in BLOCKED_EXTENSIONS:
                # Проверяем белый список
                whitelist = load_whitelist()
                if sender.id in whitelist:
                    print(f"✅ Файл {filename} от {sender.id} разрешен (в белом списке).")
                else:
                    print(f"🛑 ОБНАРУЖЕН ОПАСНЫЙ ФАЙЛ: {filename} от {sender.id}. Удаляю...")
                    
                    # Удаляем сообщение
                    await event.delete()
                    
                    # Уведомляем владельца с командой для разрешения
                    user_link = f"[{sender.first_name}](tg://user?id={sender.id})"
                    alert_text = (
                        f"⚠️ **Внимание: Заблокирована потенциальная угроза!**\n"
                        f"Пользователь {user_link} (`{sender.id}`) отправил подозрительный файл: `{filename}`.\n"
                        f"Сообщение было удалено. Чтобы разрешить этому пользователю отправлять файлы, нажмите:\n\n"
                        f"/allow_{sender.id}"
                    )
                    
                    await client.send_message('me', alert_text)
                    return
        # ----------------------------------

        # Если контакт известен, ничего не делаем
        if sender.id in contact_ids:
            return

        # Логика для неизвестных контактов
        state = user_states.get(sender.id, 0)

        if state == 0:
            # Имитация печати (или записи голосового, можно улучшить потом)
            async with client.action(sender.id, 'typing'):
                await asyncio.sleep(2) 
            
            instruction = (
                "Это ПЕРВОЕ сообщение от неизвестного контакта. "
                "ТВОЯ ЗАДАЧА: Вежливо сообщи, что пользователя нет в списке контактов. "
                "Спроси, кто он и по какому вопросу пишет."
            )
            
            ai_text = None
            if event.message.voice or event.message.audio:
                # Обработка голосового
                print("Получено голосовое сообщение (шаг 1). Скачиваю...")
                path = await event.message.download_media()
                ai_text = await get_ai_voice_response(path, instruction)
                if os.path.exists(path):
                    os.remove(path)
            else:
                # Текстовое сообщение
                ai_text = await get_ai_response(event.message.message, instruction)

            response_text = ai_text if ai_text else AUTO_REPLY_TEXT

            print(f"Первый ответ неизвестному ({'AI' if ai_text else 'Static'}): {sender.first_name} (ID: {sender.id})")
            await event.reply(response_text)
            
            user_states[sender.id] = 1
            save_state(sender.id, 1)
        
        elif state == 1:
            # Пользователь ответил на первое сообщение
            
            # Уведомляем владельца в Избранное
            user_link = f"[{sender.first_name}](tg://user?id={sender.id})"
            msg_content = "🎵 [Голосовое сообщение]" if (event.message.voice or event.message.audio) else event.message.message
            
            notification_text = (
                f"❗️ **Внимание!**\n"
                f"Неизвестный контакт {user_link} (ID: `{sender.id}`) ответил ассистенту.\n\n"
                f"**Сообщение:**\n{msg_content}"
            )
            await client.send_message('me', notification_text)
            
            # Имитация печати
            async with client.action(sender.id, 'typing'):
                await asyncio.sleep(2)
            
            instruction = (
                "Пользователь ответил на твой вопрос о причине обращения. "
                "ТВОЯ ЗАДАЧА: Поблагодари за ответ. Скажи, что передашь информацию владельцу аккаунта и он свяжется, если заинтересуется (даже если пользователь прислал голосовое). "
                "Заверши диалог вежливо."
            )
            
            ai_text = None
            if event.message.voice or event.message.audio:
                # Обработка голосового
                print("Получено голосовое сообщение (шаг 2). Скачиваю...")
                path = await event.message.download_media()
                ai_text = await get_ai_voice_response(path, instruction)
                if os.path.exists(path):
                    os.remove(path)
            else:
                ai_text = await get_ai_response(event.message.message, instruction)

            response_text = ai_text if ai_text else SECOND_REPLY_TEXT

            print(f"Второй ответ неизвестному ({'AI' if ai_text else 'Static'}): {sender.first_name} (ID: {sender.id})")
            await event.reply(response_text)
            
            user_states[sender.id] = 2
            save_state(sender.id, 2)
            
        else:
            # State == 2, диалог завершен.
            pass
            
            
    # --- ОБРАБОТКА КОМАНДЫ «РАЗРЕШИТЬ» ---
    @client.on(events.NewMessage(pattern=r'/allow_(\d+)', from_users='me'))
    async def allow_handler(event):
        try:
            user_id = int(event.pattern_match.group(1))
            save_to_whitelist(user_id)
            await event.reply(f"✅ Пользователь `{user_id}` добавлен в белый список. Теперь его файлы не будут удаляться.")
        except Exception as e:
            await event.reply(f"❌ Ошибка: {e}")

    # --- УПРАВЛЕНИЕ БЕЛЫМ СПИСКОМ (в Избранном) ---
    @client.on(events.NewMessage(pattern='/whitelist', from_users='me'))
    async def list_whitelist(event):
        whitelist = load_whitelist()
        if not whitelist:
            await event.reply("Белый список пуст.")
            return
        
        text = "**Белый список (ID пользователей):**\n\n"
        for uid in whitelist:
            text += f"• `{uid}`\n"
        text += "\nЧтобы удалить, используйте: `/unallow ID`"
        await event.reply(text)

    @client.on(events.NewMessage(pattern='/unallow (\\d+)', from_users='me'))
    async def unallow_user(event):
        user_id = int(event.pattern_match.group(1))
        if remove_from_whitelist(user_id):
            await event.reply(f"✅ Пользователь `{user_id}` удален из белого списка.")
        else:
            await event.reply(f"❌ Пользователь `{user_id}` не найден в списке.")

    # Также стоит обновлять список контактов периодически или при событии, 
    # но для MVP достаточно загрузки при старте.

    await client.run_until_disconnected()

if __name__ == '__main__':
    import sqlite3
    try:
        with client:
            client.loop.run_until_complete(main())
    except sqlite3.OperationalError as e:
        if 'database is locked' in str(e):
            print("\n❌ Ошибка: База данных заблокирована.")
            print("Вероятно, скрипт уже запущен в другом окне или предыдущий процесс не завершился корректно.")
            print("Попробуйте найти и завершить процесс python или перезагрузить терминал.")
        else:
            raise e
    except KeyboardInterrupt:
        print("\n🛑 Программа остановлена пользователем.")
