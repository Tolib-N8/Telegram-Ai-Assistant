# 🤖 Telegram AI Assistant (v1.0)

Интеллектуальный Telegram-ассистент для **личного аккаунта**, который автоматически превращает ваш профиль в защищенный и умный коммуникационный хаб. Он фильтрует спам, отвечает незнакомым людям через ИИ и управляется через современную веб-панель.

---

## ✨ Ключевые Особенности

### 🎨 Premium Web Dashboard
- **Glass UI**: Современный дизайн, адаптированный под desktop и mobile.
- **Micro-Animations**: Живой интерфейс с пульсирующими статусами, вращающимися иконками и плавным появлением карточек.
- **Advanced Log Viewer**: Поток логов в реальном времени с поддержкой цветовой подсветки, авто-скролла и гибкой фильтрации.
- **Log Export**: Возможность мгновенного скачивания полного файла системного журнала (`bot.log`).
- **Side History Drawer**: Просмотр истории переписки в удобной боковой панели, не перегружающей основной интерфейс.
- **Статус в реальном времени**: Индикаторы подключения (Online/Offline) с heartbeat-механизмом.
- **Быстрая настройка**: Добавление новых аккаунтов через QR-код или телефон прямо из веб-интерфейса.
- **Passkeys (WebAuthn)**: Вход по Passkey (можно без ввода логина, если ключ discoverable).
- **Отмена авторизации**: Можно отменить/сбросить процесс авторизации аккаунта, чтобы освободить сессию.

### 🧠 Интеллект и Защита
- **Google Gemini AI**: Умные и контекстные ответы, имитирующие стиль владельца.
- **🛡️ Anti-Scam**: Автоматическая проверка сообщений на мошенничество и вредоносные ссылки.
- **🚫 File Shield**: Блокировка потенциально опасных файлов (EXE, APK и др.) от незнакомцев.
- **🎤 Голосовой ИИ**: Распознавание входящих голосовых сообщений.

### ⚙️ Профессиональный Менеджмент
- **Multi-Account Supervisor**: Один процесс `main.py` может управлять десятками аккаунтов одновременно.
- **Dynamic Reloading**: Добавление или удаление аккаунтов на лету без перезагрузки сервера.
- **Systemd Ready**: Идеально подходит для работы на VPS в фоновом режиме.

---

## 🛠️ Технологический Стек

- **Backend**: Python 3.10+, FastAPI (Dashboard API)
- **Telegram Engine**: Telethon (MTProto)
- **AI Core**: Google Generative AI (Gemini Flash)
- **Frontend**: Vanilla JS, Modern CSS (No frameworks, pure performance)

---

## 📦 Быстрый Старт

1. **Клонируйте репозиторий**:
   ```bash
   git clone https://github.com/Tolib-N8/Telegram-Ai-Assistant.git
   cd Telegram-Ai-Assistant
   ```

2. **Настройте окружение**:
   Создайте `.env` файл на основе `.env.example`:
   ```bash
   cp .env.example .env
   ```

3. **Установите зависимости**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Запустите систему**:
   ```bash
   # Запуск веб-панели (в другом терминале)
   python dashboard.py

   # Запуск супервизора (можно и через кнопку Start в Dashboard)
   python main.py --daemon
   ```

5. **Откройте панель**:
   Перейдите по адресу `http://localhost:8000` и добавьте свой первый аккаунт!

---

## 🚀 Деплой на VPS (Ubuntu) + systemd

1. **Обновить код на сервере**:
   ```bash
   cd ~/Telegram-Ai-Assistant/Telegram-Ai-Assistant
   git pull
   ```

2. **Виртуальное окружение и зависимости**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **systemd для Dashboard (пример)**:
   Создайте файл `/etc/systemd/system/tg-assistant-dashboard.service`:
   ```ini
   [Unit]
   Description=Telegram AI Assistant Dashboard
   After=network.target

   [Service]
   Type=simple
   User=ubuntu
   WorkingDirectory=/home/ubuntu/Telegram-Ai-Assistant/Telegram-Ai-Assistant
   EnvironmentFile=/home/ubuntu/Telegram-Ai-Assistant/Telegram-Ai-Assistant/.env
   ExecStart=/home/ubuntu/Telegram-Ai-Assistant/Telegram-Ai-Assistant/.venv/bin/python dashboard.py
   Restart=always
   RestartSec=3

   [Install]
   WantedBy=multi-user.target
   ```

   Применить:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now tg-assistant-dashboard.service
   sudo systemctl status tg-assistant-dashboard.service --no-pager
   ```

4. **(Опционально) systemd для Supervisor (пример)**:
   Создайте `/etc/systemd/system/tg-assistant.service`:
   ```ini
   [Unit]
   Description=Telegram AI Assistant Supervisor
   After=network.target

   [Service]
   Type=simple
   User=ubuntu
   WorkingDirectory=/home/ubuntu/Telegram-Ai-Assistant/Telegram-Ai-Assistant
   EnvironmentFile=/home/ubuntu/Telegram-Ai-Assistant/Telegram-Ai-Assistant/.env
   ExecStart=/home/ubuntu/Telegram-Ai-Assistant/Telegram-Ai-Assistant/.venv/bin/python main.py --daemon
   Restart=always
   RestartSec=10

   [Install]
   WantedBy=multi-user.target
   ```

---

## 🌐 Cloudflare Tunnel (quick tunnel) и ошибка 1033

Если вы запускаете cloudflared как **quick tunnel** (`cloudflared tunnel --url http://localhost:8000`), то:
- при каждом рестарте cloudflared создается **новый** `https://*.trycloudflare.com` URL
- старый URL перестает работать и может показывать **Error 1033**

Текущий URL можно увидеть так:
```bash
sudo journalctl -u cloudflared-dashboard.service -n 50 --no-pager
```

Для стабильного домена используйте **named tunnel** (Cloudflare account + `config.yml`), а не quick tunnel.

---

## 🧩 Если “на сервере криво”, а на ПК нормально

Чаще всего это кеш **Service Worker** (PWA):
- откройте сайт в приватном окне (Incognito) и сравните
- либо удалите “данные сайта” (storage) для домена (особенно `trycloudflare.com`)

Также убедитесь, что на сервере реально обновлен код (`git pull`) и перезапущены сервисы dashboard/supervisor.

---

## 🩺 Health / Metrics

- `GET /healthz` всегда отвечает `200` если процесс dashboard жив.
- `GET /readyz` отвечает `200` когда настроены критичные переменные (`API_ID`, `API_HASH`) и доступна папка `accounts/`.
- `GET /metrics` (Prometheus) включается переменной `ENABLE_METRICS=1`.

## 🔐 Переменные окружения (.env)

Обязательные:
- `API_ID`, `API_HASH` (Telegram)
- `GEMINI_API_KEY` (Gemini)

Рекомендуемые:
- `DASHBOARD_USER`, `DASHBOARD_PASSWORD` (логин/пароль в Dashboard)
- `DASHBOARD_SECRET` (подпись cookie-сессий)
- `GEMINI_MODEL` (например: `gemini-2.5-flash`)

Passkeys (WebAuthn), опционально:
- `EXPECTED_ORIGIN` (например `https://your-domain.example`)
- `RP_ID` (например `your-domain.example`)

---

## 🛡️ Безопасность
Все сессии хранятся локально в папке `accounts/`. Программа никогда не передает ваши ключи или данные сессий на сторонние сервера, кроме официальных серверов Telegram (MTProto).

---
*Разработано с ❤️ для повышения продуктивности.*
