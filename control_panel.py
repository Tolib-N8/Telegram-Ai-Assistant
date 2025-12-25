import os
import asyncio
import sys
from pathlib import Path
from dotenv import load_dotenv
import logging

# Импортируем из main.py
try:
    from main import AccountManager, TelegramAssistant, load_json_file, MANAGER_CONFIG, ACCOUNTS_DIR
except ImportError:
    print("Ошибка: Не удалось найти main.py. Убедитесь, что скрипты в одной папке.")
    sys.exit(1)

# Настройка логирования для панели
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [ControlPanel] - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ControlPanel")

async def show_logs(n=20):
    log_file = Path("bot.log")
    if not log_file.exists():
        print("📁 Файл логов bot.log не найден.")
        return
    
    print(f"\n--- Последние {n} строк лога ---")
    with open(log_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
        for line in lines[-n:]:
            print(line.strip())
    print("----------------------------")

def check_service_status():
    # Простой способ проверить, запущен ли main.py
    # В Linux можно через pgrep
    import subprocess
    try:
        # Ищем процессы python, в которых упоминается main.py
        result = subprocess.run(['pgrep', '-af', 'python.*main.py'], capture_output=True, text=True)
        if result.stdout:
            print("\n✅ Бот запущен (найдены процессы):")
            print(result.stdout.strip())
        else:
            print("\n❌ Бот не запущен в фоновом режиме.")
    except Exception as e:
        print(f"\n⚠️ Не удалось проверить статус процессов: {e}")

async def control_panel_menu():
    load_dotenv()
    manager = AccountManager()
    
    while True:
        print("\n=== 🖥 Панель управления Telegram Assistant ===")
        print("1. Статус ботов")
        print("2. Добавить новый аккаунт")
        print("3. Удалить аккаунт (только из списка)")
        print("4. Посмотреть последние логи")
        print("0. Выход")
        
        try:
            choice = input("\nВыберите действие: ").strip()
        except EOFError:
            break
        
        if choice == '1':
            check_service_status()
            if manager.config['accounts']:
                print("\nСписок аккаунтов в конфиге:")
                for acc in manager.config['accounts']:
                    print(f" - {acc['name']}")
            else:
                print("\nАккаунтов нет.")
        
        elif choice == '2':
            print("\n--- Добавление нового аккаунта ---")
            await manager.add_account()
        
        elif choice == '3':
            accounts = [a['name'] for a in manager.config['accounts']]
            if not accounts:
                print("Список пуст.")
                continue
            print("\nСписок:", accounts)
            name = input("Имя для удаления: ").strip()
            manager.config['accounts'] = [a for a in manager.config['accounts'] if a['name'] != name]
            manager.save_config()
            print(f"✅ Удалено: {name}")
        
        elif choice == '4':
            try:
                n = int(input("Сколько строк показать? (по умолчанию 20): ") or 20)
            except ValueError:
                n = 20
            await show_logs(n)
            
        elif choice == '0':
            print("Выход из панели управления.")
            break
        else:
            print("❌ Неверный выбор.")

if __name__ == '__main__':
    try:
        asyncio.run(control_panel_menu())
    except KeyboardInterrupt:
        print("\n🛑 Панель управления закрыта.")
