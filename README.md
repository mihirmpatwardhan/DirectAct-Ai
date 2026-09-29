<div align="center">

# ⚡ DirectAct-AI

### *You talk. It acts. Safely.*

**DirectAct-AI turns plain English into real desktop & web actions — with a built-in security firewall so your OS stays protected.**

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-19-61DAFB?style=flat-square&logo=react&logoColor=black)](https://react.dev)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.0-3178C6?style=flat-square&logo=typescript&logoColor=white)](https://typescriptlang.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](LICENSE)

</div>

---

## 🤔 What is DirectAct-AI?

Most AI agents are scary — they generate raw shell scripts and run them directly on your machine.  
**DirectAct-AI is different.** It acts like a trusted, cautious coworker:

> *"Open Chrome → Go to Google Flights → Search SFO to JFK → Tell me the cheapest flight"*

You say that. The AI plans it. **A 8-stage security guard** checks every single step before touching your OS. You watch it happen live. You approve risky steps. Everything is logged forever.

---

## 🏗️ Architecture

### How a Request Flows Through the System

```
┌─────────────────────────────────────────────────────────────────┐
│                        YOU  (Browser UI)                        │
│          Type: "Book cheapest flight SFO → JFK next Friday"     │
└───────────────────────────┬─────────────────────────────────────┘
                            │  WebSocket / REST
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                     FastAPI Backend                             │
│   • JWT Auth  →  Task Router  →  LLM Planner (Gemini/OpenAI)   │
│   • Breaks goal into typed steps: [OpenURL, Click, FillInput…]  │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│              🛡️  8-STAGE SECURITY GUARD (Non-LLM)              │
│                                                                 │
│  1 ──► Policy Deny Check    (blocks rm-rf, format, shutdown)    │
│  2 ──► Static Code Analysis (blocks hidden scripts/payloads)    │
│  3 ──► Privilege Gate       (blocks sudo / UAC bypass)          │
│  4 ──► Windows AMSI Scan    (live antivirus on every command)   │
│  5 ──► Sandbox Decision     (routes unknown scripts to VM)      │
│  6 ──► Directory Whitelist  (no writes to C:\Windows etc.)      │
│  7 ──► Human Approval Gate  (YOU click Approve for risky steps) │
│  8 ──► SHA-256 Audit Log    (tamper-proof chain of every event) │
│                                                                 │
│              PASS ──────────────────► EXECUTE                   │
│              FAIL ──────────────────► BLOCKED & LOGGED          │
└───────────────────────────┬─────────────────────────────────────┘
                            │
               ┌────────────┴────────────┐
               ▼                         ▼
   ┌─────────────────┐        ┌──────────────────────┐
   │  💻 Desktop OS  │        │  🌐 Browser Engine   │
   │  PowerShell 7   │        │  Playwright Chromium │
   │  Win32 APIs     │        │  + Live Chrome Bridge│
   │  GDI Screenshot │        │  (Your real session) │
   └────────┬────────┘        └──────────┬───────────┘
            │                            │
            └─────────────┬──────────────┘
                          ▼
        Live stream → Your Dashboard → 📺 You watch it happen
```

---

## 📦 Project Structure (Quick Map)

```
DirectAct-AI/
│
├── 🖥️  frontend/          →  React + TypeScript dashboard (what you see)
│       └── src/
│           ├── components/   ChatPanel, Timeline, LiveViewport, Sidebar
│           ├── pages/        Landing, Login, Signup
│           └── store/        Zustand state (sessions, events, auth)
│
├── ⚡  backend/            →  FastAPI Python server (the brain)
│       └── app/
│           ├── api/          REST + WebSocket routes
│           ├── services/
│           │   ├── 🛡️ security_guard.py   ← 8-stage guard
│           │   ├── 🔄 orchestrator.py      ← runs your goal step-by-step
│           │   ├── 💻 os_engine.py         ← Windows automation
│           │   ├── 🌐 web_engine.py        ← Playwright browser
│           │   ├── 🔐 vault_service.py     ← encrypted personal data
│           │   └── 📜 audit_log.py         ← tamper-proof history
│           └── core/         Config, auth, database, websockets
│
├── 🧩  chrome-extension/  →  Live Chrome bridge (control YOUR Chrome)
│
└── 📋  policies/          →  OPA Rego security policy rules
```

---

## 🔒 Security At a Glance

| Feature | How It Works |
|---|---|
| **No raw code execution** | LLM outputs typed schemas, never raw shell scripts |
| **8-Stage Guard** | Every command passes a non-LLM deterministic filter chain |
| **AMSI Antivirus** | Native Windows Defender scan on every command in memory |
| **Human-in-the-Loop** | Risky steps pause and show you an Approve / Decline card |
| **Encrypted Vault** | Personal data stored AES-256-GCM, key lives in OS Keychain |
| **Audit Chain** | SHA-256 linked log — tampering breaks the chain |
| **No password leaks** | Chrome bridge uses `chrome.debugger` API, never reads cookies |

---

## 🚀 Getting Started

### Prerequisites
- Windows 10/11
- Python 3.10+
- Node.js 18+
- Google Chrome (for Live Bridge mode)

### ⚡ One-Click Start (Recommended)

```powershell
.\start.ps1
```

That's it. The script installs all dependencies, sets up `.env`, installs Playwright's browser, and starts both servers.

| Service | URL |
|---|---|
| Dashboard | http://localhost:5173 |
| API Docs | http://localhost:8000/docs |

---

### Manual Setup

**1. Backend**
```powershell
cd backend
python -m venv .venv && .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env        # then add your GEMINI_API_KEY
uvicorn app.main:app --port 8000 --reload
```

**2. Frontend**
```powershell
cd frontend
npm install
npm run dev
```

---

### 🧩 Chrome Extension (Optional — Live Session Mode)

Control websites using *your real logged-in Chrome* (Gmail, GitHub, etc.) — without sharing passwords.

1. Go to `chrome://extensions` → enable **Developer mode**
2. Click **Load unpacked** → select the `chrome-extension/` folder
3. Check `http://localhost:8000/api/v1/chrome/bridge-status` → should say `connected: true`

---

## ⚙️ Configuration

Copy `backend/.env.example` → `backend/.env` and fill in:

```env
GEMINI_API_KEY=your_key_here        # Required — get one at aistudio.google.com
LLM_PROVIDER=gemini                 # or "openai"
OPENAI_API_KEY=optional_fallback    # Optional
SECRET_KEY=change-this-in-prod      # JWT signing key
BROWSER_HEADLESS=false              # true = invisible browser
```

> **Never commit `.env`** — it's already in `.gitignore`.

---

## 🛠️ Tech Stack

| Layer | Stack |
|---|---|
| **Frontend** | React 19, TypeScript, Vite, Tailwind CSS, Zustand |
| **Backend** | Python 3.10, FastAPI, AsyncIO, SQLAlchemy 2.0 |
| **AI / LLM** | Google Gemini 2.0 Flash, OpenAI GPT-4o (with Circuit Breaker) |
| **Browser** | Playwright Chromium, Chrome DevTools Protocol (CDP) |
| **OS Automation** | PowerShell 7, Win32 APIs, pywinauto |
| **Security** | Windows AMSI, Open Policy Agent (OPA), AES-256-GCM |
| **Database** | SQLite (async), SHA-256 hash-chained JSONL audit ledger |

---

## 📄 License

MIT — see [LICENSE](LICENSE)

---

<div align="center">
Made with ❤️ by <a href="https://github.com/mihirmpatwardhan">Mihir M. Patwardhan</a>
</div>
