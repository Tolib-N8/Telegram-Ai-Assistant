# Telegram AI Assistant (v1.2)

Telegram assistant for personal accounts with AI auto-replies, anti-scam checks, file protection, and a modern web dashboard.

## What It Does

- Multi-account Telegram supervisor (`main.py`)
- Web dashboard with live status, logs, charts, and account settings
- Passkey login (WebAuthn) + role-based access (`admin` / `viewer`)
- AI reply flow for unknown contacts (language-aware, controlled dialog)
- Scam detection + unsafe file blocking
- Cloud/VPS friendly (systemd + Cloudflare Tunnel)

## Quick Start

```bash
git clone https://github.com/Tolib-N8/Telegram-Ai-Assistant.git
cd Telegram-Ai-Assistant
cp .env.example .env
pip install -r requirements.txt
```

Then edit `.env` and set your real credentials (at minimum: `API_ID`, `API_HASH`, `GEMINI_API_KEY`, dashboard login/password).

Run in separate terminals:

```bash
python dashboard.py
python main.py --daemon
```

Open: `http://localhost:8000`

## Core Environment Variables

Required:

- `API_ID`, `API_HASH`
- `GEMINI_API_KEY`

Dashboard auth:

- `DASHBOARD_USER`, `DASHBOARD_PASSWORD`
- `DASHBOARD_USERS` (optional extra users), format:
  - `user1:pass1:viewer,user2:pass2:viewer`

Optional:

- `GEMINI_PROMPT`
- `GEMINI_MODEL` (default: `gemini-2.5-flash`)
- `DASHBOARD_SECRET`

Passkeys:

- `EXPECTED_ORIGIN` (example: `https://your-domain.example`)
- `RP_ID` (example: `your-domain.example`)

Security/production:

- `TRUST_PROXY=1`
- `AUTH_RL_MAX=12`
- `AUTH_RL_WINDOW=60`
- `DASHBOARD_SESSION_MAX_AGE=604800`
- `DASHBOARD_COOKIE_SECURE=1`
- `ENABLE_CSP=1` (optional)

## Health & Metrics

- `GET /healthz`
- `GET /readyz`
- `GET /metrics` (enable with `ENABLE_METRICS=1`)

## Deployment Notes

- For production, run dashboard and supervisor with `systemd`.
- If using Cloudflare quick tunnel (`trycloudflare`), URL changes after restart.
- Prefer a named tunnel for stable domain.

## Security Note

Session files and account data are stored locally in `accounts/`.
