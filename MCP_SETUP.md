# MCP Browser Automation Engine — Setup Guide

## Overview

DirectAct-AI now includes an MCP (Model Context Protocol) browser automation engine
that replaces the legacy direct-Playwright execution path for web tasks. The MCP engine
provides persistent session reuse, faster execution, and streaming screenshots.

---

## 1. One-Time Login Setup

The MCP engine uses a **dedicated browser profile** at `~/.directact/browser-profile/`.
This is separate from your personal Chrome — no lock conflicts.

### First-time setup:

1. Start the DirectAct-AI backend:
   ```bash
   cd backend
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

2. In the frontend chat, send the WebSocket event `setup_login` or use the API:
   ```bash
   # Or via WebSocket message:
   # {"type": "setup_login"}
   ```

3. A **headed** Chrome window will open with the DirectAct automation profile.

4. **Log into the sites you need** (Gmail, GitHub, Jira, etc.) in this browser window.

5. Close the browser window when done. Your cookies are now saved in the dedicated profile.

6. From now on, the MCP engine will reuse these cookies automatically — even in headless mode.

### Re-login if cookies expire:

Repeat steps 2-5 whenever your session cookies expire for a site.

---

## 2. Switching Engine Modes

### UI Toggle (Recommended)

In the ChatPanel, you'll see two control rows:

```
Mode:  ⚡ Auto Hybrid  |  🌐 Web Browser  |  💻 Desktop App
Engine:  🔧 Legacy  ←→  🧪 MCP (Beta)
```

- **🔧 Legacy**: Uses the existing `web_engine.py` + direct Playwright calls.
- **🧪 MCP (Beta)**: Uses the new MCP engine with persistent context, batch planning,
  and optimized screenshots.

Click the Engine toggle to switch between them. Both engines share the same security
pipeline (OPA + AMSI + HITL approval).

### API / WebSocket Override

Include `engine_mode` in your `chat_message` WebSocket payload:

```json
{
  "type": "chat_message",
  "content": "open google.com and search for AI news",
  "target_engine": "web",
  "engine_mode": "mcp"
}
```

Valid values: `"legacy_playwright"` (default) or `"mcp"`.

### Environment Variable

Set in `.env`:
```
MCP_DEFAULT_HEADLESS=true
MCP_BROWSER_PROFILE_DIR=~/.directact/browser-profile
MCP_SCREENSHOT_QUALITY=55
MCP_DOM_WAIT_STRATEGY=domcontentloaded
```

---

## 3. Reading Timing Logs

The MCP engine logs timing data at every stage. Look for `⏱️` markers in the backend console:

| Log Pattern | What It Means |
|---|---|
| `⏱️  SessionManager: context launched in Xms` | Time to launch/reuse the persistent browser context |
| `⏱️  SessionManager: page created for session=... in Xms` | Time to create a new tab |
| `⏱️  LLM planning completed in Xms` | Time for batch ActionPlan generation (one LLM call) |
| `⏱️  Security check for step X completed in Xms` | Per-step MalwareGuard pipeline duration |
| `⏱️  MCP tool 'navigate' completed in Xms` | Per-tool execution time |
| `⏱️  Screenshot captured in Xms` | Screenshot capture + compression time |
| `⏱️  WebSocket screenshot broadcast in Xms` | Time to send screenshot to frontend |
| `⏱️  MCP task COMPLETE: X/Y steps, total=Xms` | End-to-end task duration |
| `⏱️  Orchestrator MCP pipeline total: Xms (planning=Xms)` | Full pipeline including routing |

### Diagnosing slowness:

1. **Context launch > 2000ms**: Profile may be large. Try deleting cache files in `~/.directact/browser-profile/`.
2. **LLM planning > 5000ms**: LLM is slow. Check your API key quotas and network.
3. **Navigation > 10000ms**: Page is heavy. `domcontentloaded` should help vs `networkidle`.
4. **Screenshot > 500ms**: Reduce quality: `MCP_SCREENSHOT_QUALITY=40` in `.env`.

### Audit Trail

All MCP tool calls are logged to the existing audit trail with check_name `mcp_tool:<tool_name>`.
View in the Security Status panel or query the SQLite database directly.

---

## 4. Speed Benchmark

To compare MCP vs Legacy engine performance on the same task:

1. Select **🔧 Legacy** engine mode.
2. Send: "open google.com and search for Python tutorials"
3. Note the `total_duration_ms` in the backend console log.
4. Switch to **🧪 MCP (Beta)** engine mode.
5. Send the same task in a new session.
6. Compare `total_duration_ms` values.

Expected improvement targets:
- **Context launch**: < 500ms (reuse) vs 3-5s (fresh launch in legacy)
- **First action**: < 3s from submit vs 5-8s in legacy
- **Screenshot streaming**: JPEG at quality 55 vs full-res PNG

---

## 5. Architecture Overview

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│  ChatPanel   │────▶│  WebSocket Route  │────▶│   Orchestrator      │
│  (React UI)  │     │  (websocket.py)   │     │  (orchestrator.py)  │
└──────────────┘     └──────────────────┘     └──────┬──────────────┘
                                                       │
                                           engine_mode="mcp"?
                                              ┌────────┴────────┐
                                              ▼                 ▼
                                    ┌─────────────┐   ┌──────────────┐
                                    │  MCP Client  │   │ Legacy Engine │
                                    │ (mcp_client) │   │ (web_engine)  │
                                    └──────┬──────┘   └──────────────┘
                                           │
                                    ┌──────▼──────┐
                                    │  MCP Server  │  (in-process dispatch)
                                    │ (7 tools)    │
                                    └──────┬──────┘
                                           │
                              ┌────────────┼────────────┐
                              ▼            ▼            ▼
                      ┌──────────┐  ┌───────────┐  ┌──────────┐
                      │ Security │  │  Session   │  │  Audit   │
                      │  Guard   │  │  Manager   │  │   Log    │
                      │(OPA/AMSI)│  │(persistent)│  │(hash-chained)│
                      └──────────┘  └───────────┘  └──────────┘
```

All MCP tool calls go through the same MalwareGuard pipeline as legacy execution.
The MCP engine is an **execution strategy swap**, not a security bypass.

---

## 6. Known Limitations

> **⚠️ In-Process Direct Dispatch**
>
> The current MCP implementation uses **in-process direct function dispatch** —
> the MCP client calls server tool handlers directly without stdio/SSE transport.
>
> This means:
> - **No network exposure**: The MCP server is not accessible from outside the process.
> - **No external MCP server compatibility**: You cannot plug in third-party MCP servers
>   (e.g., Gmail MCP, Notion MCP, Slack MCP) directly. Those require full MCP stdio/SSE
>   transport which would need a transport adapter layer.
> - **Migration path**: If external MCP servers are needed later, add a transport adapter
>   in `browser_mcp_client.py` that can route to either in-process handlers OR external
>   MCP servers via stdio/SSE. The tool schemas are already MCP-compatible.

> **⚠️ Browser Profile Lock**
>
> The dedicated automation profile at `~/.directact/browser-profile/` can only be used
> by one process at a time. If the backend crashes without cleanup, the profile may be
> locked. Solution: restart the backend or delete the `SingletonLock` file in the profile
> directory.

> **⚠️ Cookie Expiration**
>
> Session cookies eventually expire. The MCP engine does NOT auto-refresh login sessions.
> Use `check_session_status` tool or re-run the login setup when sessions expire.
