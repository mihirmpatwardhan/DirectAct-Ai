# Comprehensive Technical & Architectural Report: DirectAct-AI (Lakshya-AI Engine)

---

## 1. Executive Summary & Project Vision

**DirectAct-AI** (also identified as **Lakshya-AI**) is a next-generation autonomous desktop and web agent platform designed to turn high-level human natural language goals into secure, autonomous execution streams on host operating systems. 

Unlike traditional LLM wrappers that execute arbitrary AI-generated code directly on the host OS, DirectAct-AI operates on a **Deterministic Zero-Trust AI Execution Model**. Every natural language prompt is mapped into a strictly typed, immutable Action Vocabulary schema, routed through a non-LLM 8-stage **Malware Guard Chain**, logged in a **tamper-evident hash-chained audit ledger**, and executed via deterministic native OS drivers (PowerShell 7, Win32 APIs, Playwright).

The architecture cleanly decouples prompt intelligence (LLM) from system execution (OS/Web Drivers) via a strict security boundary, ensuring that prompt injection attacks, LLM hallucinations, or compromised external models can never bypass system security controls or access non-whitelisted resources.

---

## 2. Complete Technology Stack Matrix

| Layer / Domain | Technology / Library | Version / Details | Purpose & Responsibilities |
| :--- | :--- | :--- | :--- |
| **Backend Framework** | Python / FastAPI | `fastapi==0.111.0`, `uvicorn[standard]==0.30.1` | High-performance asynchronous REST API & WebSocket server engine |
| **Async Runtime** | asyncio / Python | Python 3.10+ | Non-blocking execution of sagas, browser streams, and security pipelines |
| **Frontend Framework** | React / TypeScript | `react==19.2.7`, `typescript==6.0.2` | Modern declarative UI component rendering with strong static typing |
| **Frontend Build Tool** | Vite | `vite==8.1.1`, `@vitejs/plugin-react==6.0.3` | Ultra-fast HMR module bundling and dev environment |
| **Frontend Styling** | Tailwind CSS / Vanilla CSS | `@tailwindcss/vite==4.3.3`, `tailwindcss==4.3.3` | Custom cyber-dark dynamic theme, glassmorphism, responsive grid layout |
| **State Management** | Zustand / Immer | `zustand==5.0.14`, `immer==11.1.15` | Centralized immutable application state management for sessions, timeline & events |
| **Real-time Comms** | WebSockets | `websockets==12.0` (Py), Custom React Hook | Bidirectional real-time streaming of execution steps, logs & viewport frames |
| **Primary Database** | SQLite / SQLAlchemy | `sqlalchemy==2.0.30`, `aiosqlite==0.20.0` | Asynchronous ORM for persistence of sessions, messages, and action logs (`directact.db`) |
| **Encrypted Vault DB** | SQLite / Cryptography | `cryptography==42.0.8`, `keyring==25.2.1` | Purpose-bound AES-256-GCM field-encrypted personal data vault (`vault.db`) |
| **Audit Ledger** | JSONL / Hash-Chain | Custom SHA-256 Implementation | Tamper-evident append-only ledger linking security decisions (`audit_chain.jsonl`) |
| **App Inventory Cache** | SQLite | `sqlite3` | Local OS installed application discovery index with TTL (`app_inventory.db`) |
| **Antivirus Integration**| Windows AMSI via `ctypes` | `amsi.dll` (Windows Native) | Real-time buffer & file scanning leveraging host AV (Windows Defender / 3rd party AV) |
| **Secondary AV (Opt-in)**| VirusTotal API v3 | `httpx==0.27.0` | Hash lookup for unknown binaries (strictly opt-in, non-PII content) |
| **Web Automation** | Playwright | `playwright==1.44.0` | Headed/headless Chromium browser engine with live screenshot streaming & element flashing |
| **Web Agent Runtime** | Browser-Use | `browser-use==0.13.1` | Autonomous browser task execution agent runtime (used in standalone `main.py`) |
| **OS Automation Engine**| PowerShell 7 / Win32 | `pywin32==306`, `powershell.exe` | Native Windows command execution, registry scanning, window management & GDI screenshots |
| **LLM Integrations** | Google Gemini & OpenAI | `google-generativeai==0.7.2`, `openai==1.35.3` | Multi-provider streaming AI model integration with Circuit Breaker key rotation |
| **Policy Engine** | Open Policy Agent (OPA) | Rego Policies (`policies/base.rego`) | Declarative policy validation for role-based privileges & risk thresholds |
| **Standalone Portal** | Streamlit | `streamlit>=1.28.0` | Secondary GUI portal (`app.py`) for launching local subprocess automation runs |

---

## 3. Architecture Overview & Core Design Patterns

DirectAct-AI is built following clean architectural principles and industry-standard design patterns to guarantee resilience, security, and maintainability:

```
[ User UI (React + Zustand) ] 
          │ (WebSocket / REST)
          ▼
[ FastAPI Server / Router ]
          │
          ▼
[ Hybrid Task Router ] ──► (Fast Regex / LLM Fallback)
          │
          ▼
[ Saga Execution Orchestrator ] ◄──► [ LLM Provider Service (Circuit Breaker) ]
          │
          ▼  (Step-by-Step Typed Commands)
┌─────────────────────────────────────────────────────────────┐
│ 🛡️ MALWARE GUARD PIPELINE (Chain of Responsibility)        │
│ 1. Policy Validation  2. Static AST   3. Privilege Check    │
│ 4. AMSI File Scan     5. Sandbox Dec  6. Directory Access   │
│ 7. Confirmation Gate  8. Hash-Chained Audit Ledger          │
└─────────────────────────────────────────────────────────────┘
          │ (Pass Verdict)
          ▼
┌─────────────────────────────────────────────────────────────┐
│ ⚙️ EXECUTION ENGINES (Strategy Pattern)                     │
│  ├── Windows OS Engine (PowerShell / Win32)                 │
│  └── Web Automation Engine (Playwright Chromium Stream)     │
└─────────────────────────────────────────────────────────────┘
          │
          ▼
[ SQLite DB / Vault / Hash-Chained Audit File / WebSockets UI ]
```

### Key Design Patterns Implemented:
1. **Chain of Responsibility Pattern**: Implemented in `MalwareGuard` (`security_guard.py`). Security checks are ordered as independent steps (Steps 1 through 8). Any single step can short-circuit and HALT the execution pipeline immediately.
2. **Strategy Pattern**: 
   - **OS Automation Engine** (`os_engine.py`): Abstract base class `OSAutomationEngine` with concrete subclasses `WindowsAutomationEngine`, `LinuxAutomationEngine`, and `MacOSAutomationEngine`. Selected dynamically at runtime via `PlatformProbe`.
   - **LLM Provider** (`llm_service.py`): Abstract class `LLMProvider` with implementations `GeminiProvider`, `OpenAIProvider`, and `StubProvider`.
3. **Saga State Machine Pattern**: Implemented in `ExecutionOrchestrator` (`orchestrator.py`). Multi-step user goals are broken down into sequential sagas. If step $N$ fails, compensating actions for steps $1 \dots N-1$ are automatically triggered in reverse order to restore system state.
4. **Circuit Breaker Pattern**: Implemented in `KeyRotationManager` (`llm_service.py`). Each LLM API key has an isolated state machine (`CLOSED`, `OPEN`, `HALF_OPEN`). When a key hits quota limits (HTTP 429) or repeated errors, its circuit opens for a cooldown period (60s), seamlessly shifting traffic to healthy keys in the pool without user disruption.
5. **Observer / Event-Driven Pattern**: `EventBus` and `WebSocketManager` (`websocket_manager.py`) push real-time typed events (`TaskStarted`, `GuardCheckResult`, `ActionBlocked`, `ViewportScreenshot`) directly to connected frontend clients.
6. **Factory Pattern**: `create_os_engine()` and `detect_platform()` instantiate runtime-matched engines based on OS detection.

---

## 4. Database Architecture & Persistence Layer

DirectAct-AI uses a multi-database strategy tailored to specific security and durability requirements:

```
DirectAct-AI Data Storage Architecture
├── backend/directact.db          (SQLite - Async SQLAlchemy 2.0: Sessions, Messages, Action Logs)
├── backend/logs/vault.db         (SQLite - AES-256 Encrypted Personal Identity Store)
├── backend/logs/app_inventory.db (SQLite - Local OS Application Registry Index)
├── backend/logs/audit_chain.jsonl(JSONL - Hash-Chained Append-Only Security Audit Ledger)
└── %TEMP%/lakshya_run_meta.json  (JSON File - Subprocess Task Control for Streamlit Portal)
```

### 4.1 Primary Application Database (`directact.db`)
Managed via SQLAlchemy 2.0 Async ORM (`aiosqlite` driver):
- **`sessions` Table**: Tracks user interaction sessions.
  - Columns: `id` (UUID str, PK), `name` (str), `status` (`ACTIVE`, `PAUSED`, `COMPLETED`, `ERROR`), `created_at` (DateTime), `updated_at` (DateTime), `metadata` (JSON).
- **`messages` Table**: Conversation history.
  - Columns: `id` (UUID str, PK), `session_id` (FK -> `sessions.id`), `role` (`USER`, `ASSISTANT`, `SYSTEM`), `content` (Text), `tokens_used` (Int), `created_at` (DateTime), `metadata` (JSON).
- **`action_logs` Table**: Individual execution step history.
  - Columns: `id` (UUID str, PK), `session_id` (FK -> `sessions.id`), `action_type` (str: `web`, `desktop`, `system`), `description` (Text), `command` (Text), `status` (`PENDING`, `AWAITING_APPROVAL`, `APPROVED`, `DECLINED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`), `threat_level` (`NONE`, `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`), `requires_approval` (Bool), `approved_by` (str), `result` (JSON), `error_message` (Text), `duration_ms` (Float), `screenshot_path` (str), `created_at`, `completed_at`.

### 4.2 Purpose-Bound Encrypted Vault Database (`vault.db`)
Stores sensitive user data required for autonomous form filling or personal context:
- **Encryption**: AES-256-GCM authenticated encryption via `cryptography.hazmat.primitives.ciphers.aead`.
- **Master Key Security**: Master 256-bit key is stored in the host OS Keychain via `keyring` (Service: `directact-ai`, Key: `vault_master_key`), ensuring key separation from database files.
- **Sensitivity Tiers**:
  - **Tier 1 (Routine)**: `first_name`, `last_name`, `display_name`, `preferences`, `language`. Auto-approved.
  - **Tier 2 (Contextual)**: `email`, `phone`, `home_address`, `work_address`, `company`, `job_title`. Approved if task intent matches purpose.
  - **Tier 3 (Identity)**: `passport_reference`, `national_id_reference`, `drivers_license_reference`. Requires explicit human confirmation per request in all modes.
- **Forbidden Fields (Hard Block)**: `credit_card_number`, `cvv`, `pin`, `bank_account_number`, `ssn`, `password`, `api_key`. Rejected at write time.

### 4.3 Hash-Chained Audit Ledger (`audit_chain.jsonl`)
- **Structure**: Append-only log where every entry contains `sequence`, `timestamp`, `action_id`, `task_id`, `session_id`, `check_name`, `verdict`, `risk_level`, `reason`, `redacted_content_hash`, `prev_hash`, `entry_hash`.
- **Tamper Evident**: `entry_hash = SHA256(json_payload + prev_hash)`. Modifying any historical log breaks the chain validation.
- **Privacy Preservation**: Detected malicious strings/payloads are NEVER written verbatim to disk; they are redacted and replaced with `SHA256(content)`.

---

## 5. Comprehensive Analysis of Core Services

### 5.1 Malware Guard Pipeline (`backend/app/services/security_guard.py`)
The Malware Guard is the non-bypassable security gate between the LLM orchestrator and the host operating system. It enforces the rule: *"No AI-generated action reaches the OS without passing through a deterministic, non-LLM security gate."*

The 8 pipeline stages are:
1. **PolicyValidationCheck**: Hard-coded regex deny list executed in $<1\text{ms}$. Blocks recursive file deletion (`rm -rf`, `del /s`, `rd /s`), formatting drives (`format c:`), system shutdown (`shutdown /s`), registry destruction, PowerShell execution policy bypasses (`Set-ExecutionPolicy Unrestricted`, `IEX`, `Base64`), and writes to system directories (`C:\Windows`, `C:\Program Files`).
2. **StaticCommandAnalysisCheck**: Schema structure validation and AST-level static inspection of scripts for suspicious patterns (`AMSI bypass`, `Hidden Window`, `Certutil payload delivery`).
3. **PrivilegeEscalationCheck**: Hard deny on AI-driven privilege escalation (`sudo`, `runas`, `net localgroup administrators`). Escalation must occur through native OS UAC/sudo prompts initiated by the user.
4. **FileScanCheck**: Invokes AMSI buffer scanning for any script or binary target before execution.
5. **SandboxDecisionCheck**: Routes unverified executables or scripts to Windows Sandbox isolation (`Containers-DisposableClientVM`) when available.
6. **DirectoryAccessCheck**: Enforces an allowlist of writable base directories (User Home directory, `%TEMP%`, and explicitly declared user paths).
7. **ConfirmationGateCheck**: Forces human approval for `HIGH`/`CRITICAL` risk tasks, regardless of execution mode (Autonomous vs HITL).
8. **AuditLoggerCheck**: Writes the tamper-evident hash-chained audit entry for the decision.

### 5.2 AMSI Scanner & VirusTotal Integration (`backend/app/services/amsi_scanner.py`)
- **Native AMSI Binding**: Uses Python `ctypes` to load `amsi.dll` directly into the process, initializing an AMSI session (`AmsiInitialize`, `AmsiOpenSession`). Buffers are passed to `AmsiScanBuffer`. Returns `CLEAN`, `DETECTED` (result code $\ge 32768$), or `SUSPICIOUS`.
- **VirusTotal Integration**: Secondary opt-in hash lookup (`/api/v3/files/{hash}`). Triggered ONLY with explicit user consent and NEVER for files containing personal data.

### 5.3 Saga Execution Orchestrator (`backend/app/services/orchestrator.py`)
- Coordinates the execution flow: `User Input -> TaskRouter -> ActionPlan -> Orchestrator -> MalwareGuard -> OSEngine / WebEngine -> EventBus`.
- Parses structured `action_plan` blocks generated by the LLM into concrete typed commands (`LaunchApp`, `CreateFile`, `DeleteFile`, `MoveFile`, `OpenURL`, etc.).
- Manages human-in-the-loop permission requests via WebSocket events (`permission_requested`, `permission_granted`, `permission_denied`).
- Executes compensating actions in reverse order if a multi-step saga fails midway.

### 5.4 Hybrid Task Intent Router (`backend/app/services/task_router.py`)
- **Fast Rule-Based Path ($<5\text{ms}$)**: Evaluates high-confidence regex patterns and keyword matches to classify intent into `WEB`, `DESKTOP`, or `QUERY`.
- **LLM Reasoning Fallback**: If score confidence $<0.35$ (`AMBIGUOUS`), dispatches a structured intent query to Gemini/OpenAI to determine task parameters and risk.

### 5.5 Strategy OS Automation Engine (`backend/app/services/os_engine.py`)
- **Windows Automation Engine**: Executes commands via PowerShell 7 with `-NoProfile -NonInteractive -ExecutionPolicy Bypass`.
- **Registry App Map**: Automatically maps canonical app names (e.g. `chrome`, `vscode`, `word`, `excel`, `spotify`) to absolute executable paths via `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths` and well-known system paths.
- **GDI Screen Capture**: Uses PowerShell `System.Windows.Forms` & `System.Drawing.Graphics` to take full-screen primary monitor screenshots saved to `./logs/screenshots`.

### 5.6 Playwright Web Automation Engine (`backend/app/services/web_engine.py`)
- Manages persistent Chromium browser contexts via Playwright.
- **Profile Inheritance**: Attaches directly to the system's Google Chrome `User Data` directory (`AppData\Local\Google\Chrome\User Data`) if available, preserving user sessions, cookies, and saved logins.
- **Live Viewport Streaming**: Encodes screenshots as Base64 JPEG/WebP images and streams them to the UI over WebSockets (`viewport_screenshot` event).
- **Visual Element Highlighting**: Injects smooth red DOM outlines (`outline: 3px solid #ef4444`) around elements before clicking or typing to provide clear visual feedback during interaction.

### 5.7 Provider-Agnostic LLM Service & Circuit Breaker (`backend/app/services/llm_service.py`)
- Supports **Gemini** (`gemini-2.0-flash`, `gemini-1.5-flash`) and **OpenAI** (`gpt-4o-mini`).
- **Key Rotation & Circuit Breaker**: Wraps API keys in an isolated `KeyCircuitBreaker`. Automatically rotates to alternative keys if rate limits ($429$) or network failures occur. Fallbacks to `StubProvider` if all keys are unavailable.

### 5.8 Application Discovery Engine (`backend/app/services/app_discovery.py`)
- Scans Windows Registry Uninstall keys (`HKLM` & `HKCU`), App Paths, and Start Menu shortcuts to construct a normalized local application inventory.
- Caches inventory in `app_inventory.db` with a 30-minute TTL. Provides fuzzy app matching (e.g., matching "VS Code", "code", or "visual studio code" to `Code.exe`).

### 5.9 Platform Capability Probe (`backend/app/services/platform_probe.py`)
- Runs at system startup to detect OS version, processor architecture, admin privilege status (`IsUserAnAdmin`), AMSI presence, Windows Sandbox feature state (`Containers-DisposableClientVM`), and PowerShell version.

### 5.10 Policy Engine (`policies/base.rego` & `privilege.rego`)
- Implements declarative security policies using Open Policy Agent (OPA) REGO syntax. Evaluates execution context against allowed roles, command risk defaults, and privilege boundaries.

---

## 6. Standalone Subprocess Automation Engine (`main.py` & `app.py`)

In addition to the FastAPI/React web app, DirectAct-AI includes a standalone automation pipeline (`main.py`) and a Streamlit management portal (`app.py`):

- **`main.py`**:
  - Implements direct Playwright automation using `browser-use` and `langchain-google-genai`.
  - Rotates through a dedicated pool of Gemini API keys (`GEMINI_KEYS`).
  - Contains fast-path heuristics (`build_fast_task_plan`) to instantly launch direct URLs, Google/Amazon/YouTube searches without invoking LLM planning overhead.
  - Implements Windows Start Menu indexing (`Get-StartApps | Select-Object Name,AppID`) and process matching via `psutil` to close or launch Windows applications.
- **`app.py`**:
  - A Streamlit desktop UI that spawns `main.py` as an isolated background process (`subprocess.Popen`).
  - Writes task meta and logs to system `%TEMP%` (`lakshya_run_meta.json`, `lakshya_run.out.log`, `lakshya_run.err.log`).
  - Monitors PID status using `psutil.Process` and provides real-time log tailing and force-halt process termination.

---

## 7. Frontend Architecture (React 19 + TypeScript + Zustand)

The frontend is built using React 19, TypeScript, Vite, and Tailwind CSS. The interface is structured into four main operational panels:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                             DIRECTACT-AI HEADER                             │
├──────────────┬──────────────────────────────┬───────────────────────────────┤
│ SIDEBAR      │ CHAT PANEL                   │ VIEWPORT PANEL                │
│ - Sessions   │ - Natural Language Stream    │ - Live Playwright Browser Stream│
│ - Active OS  │ - Intent Mode Selector       │ - Element Highlight Preview   │
│ - Security   │ - Markdown / Code Rendering  │ - Fullscreen / Zoom Controls  │
│   Status     ├──────────────────────────────┴───────────────────────────────┤
│              │ TIMELINE & SECURITY PANEL                                    │
│              │ - Real-time Execution Steps  - Human Approval Cards           │
│              │ - 8-Step Guard Visualization - Hash-Chained Audit Ledger     │
└──────────────┴──────────────────────────────────────────────────────────────┘
```

### Frontend Code Structure:
- **`src/store/appStore.ts`**: Centralized Zustand store with `immer` middleware. Manages active session, sessions list, chat message thread, timeline action steps, approval queue, security audit log entries, live viewport screenshot frames, and global execution mode (`hitl` vs `autonomous`).
- **`src/hooks/useWebSocket.ts`**: Manages non-blocking WebSocket connection to `/ws/{session_id}`. Reconnects automatically with exponential backoff, sends ping heartbeats every 30s, and dispatches incoming WebSocket JSON payloads to the Zustand store.
- **`src/components/ChatPanel/ChatPanel.tsx`**: Renders message history with `react-markdown` and `remark-gfm`. Displays quick-intent prompts and engine target selectors (`Web`, `Desktop`, `Auto`).
- **`src/components/TimelinePanel/TimelinePanel.tsx`**: Visualizes step-by-step execution. Displays security check results, risk indicators (`SAFE`, `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`), human approval cards with Approve/Decline buttons, and tamper-evident audit ledger entries.
- **`src/components/ViewportPanel/ViewportPanel.tsx`**: Displays the live browser feed streamed from Playwright over WebSockets.
- **`src/components/Sidebar/Sidebar.tsx`**: Navigation sidebar displaying session history, system health indicators, and active security capabilities.

---

## 8. Security & Compliance Matrix

| Security Feature | Mechanism / Technology | Mitigation Impact |
| :--- | :--- | :--- |
| **Prompt Injection Protection** | Non-LLM Malware Guard Gate | AI output cannot execute arbitrary host commands; all commands must match typed vocabulary schemas and pass deterministic guard steps. |
| **Malware & Virus Defense** | Native Windows AMSI (`amsi.dll`) | Scans all scripts and file payloads using local host AV before execution. |
| **Destructive Command Block** | Regex Deny List + OPA Rego | Hard-blocks disk format, recursive system deletion, and shutdown commands at $<1\text{ms}$. |
| **Privilege Escalation Gate** | Deterministic Privilege Check | Prevents silent elevation (`sudo`, `runas`). Forces elevation through native OS UAC/Polkit prompts. |
| **Audit Integrity** | SHA-256 Hash-Chained JSONL | Provides a tamper-evident, append-only security log for forensic audit and compliance. |
| **Personal Data Vault Security** | AES-256-GCM + OS Keychain | Encrypts identity data at rest; key is never written to DB. Tier 3 data requires explicit per-request user approval. |
| **Human-in-the-Loop Gate** | WebSocket Permission Requests | High-risk actions pause execution until human explicitly clicks "Approve" in UI. |
| **Sandbox Isolation** | Windows Sandbox / Independent Chrome Data Dir | Runs unverified scripts in disposable VMs and isolates web tasks in separate browser contexts. |

---

## 9. Event-Driven Communication Protocol (WebSocket Schema)

Communication between the React frontend and FastAPI backend occurs via WebSockets (`/ws/{session_id}`) using strictly typed JSON event objects:

```json
{
  "type": "task_step_started",
  "session_id": "3b29a1f2-...",
  "task_id": "task-8841",
  "step_index": 0,
  "total_steps": 2,
  "command_type": "open_url",
  "description": "Open https://www.google.com",
  "risk_level": "low"
}
```

### Event Payload Types:
1. **`chat_message`**: User sends natural language input to backend.
2. **`text_chunk`**: Assistant response fragment streamed to chat panel.
3. **`task_started` / `task_completed` / `task_failed`**: Saga execution lifecycle events.
4. **`task_step_started` / `task_step_completed`**: Granular progress per action step.
5. **`security_check_started` / `security_check_result`**: Real-time Malware Guard step visualization.
6. **`action_blocked`**: Sent when Malware Guard halts a step; contains redacted hash and block reason.
7. **`permission_requested` / `permission_granted` / `permission_denied`**: Human approval flow triggers.
8. **`audit_log_entry`**: Broadcasts new hash-chained audit entry to security panel.
9. **`viewport_screenshot`**: Transmits Base64 browser frame for live viewport playback.

---

## 10. Conclusion

**DirectAct-AI (Lakshya-AI Engine)** represents an enterprise-grade, security-first paradigm for autonomous AI action. By interposing a deterministic 8-stage Malware Guard, native Windows AMSI scanning, an AES-256 encrypted Vault, and a hash-chained audit ledger between the AI model and the operating system, DirectAct-AI achieves what raw LLM execution agents cannot: **provable safety, zero-trust control, and complete auditability** while retaining the power of natural language goal execution.
