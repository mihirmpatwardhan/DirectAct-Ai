# DirectAct-AI — one-click local startup (Windows)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot

Write-Host ''
Write-Host '========================================' -ForegroundColor Cyan
Write-Host '  DirectAct-AI — Starting local servers' -ForegroundColor Cyan
Write-Host '========================================' -ForegroundColor Cyan
Write-Host ''

# --- Check prerequisites ---
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host 'ERROR: Python not found. Install Python 3.11+ from python.org' -ForegroundColor Red
    exit 1
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host 'ERROR: Node.js not found. Install from nodejs.org' -ForegroundColor Red
    exit 1
}

# --- Backend .env ---
$envFile = Join-Path $Root 'backend\.env'
if (-not (Test-Path $envFile)) {
    Write-Host 'Creating backend/.env from .env.example ...' -ForegroundColor Yellow
    Copy-Item (Join-Path $Root 'backend\.env.example') $envFile
    Write-Host 'IMPORTANT: Edit backend/.env and add your GEMINI_API_KEY' -ForegroundColor Yellow
}

# --- Install deps if missing ---
Push-Location (Join-Path $Root 'backend')
python -c 'import fastapi, uvicorn, playwright, pywinauto, psutil, PIL' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing Python backend dependencies ...' -ForegroundColor Yellow
    python -m pip install -r requirements.txt
}

# Playwright's Python package and its Chromium binary are separate installs.
# Install the browser only when it is not already present.
$playwrightRoot = Join-Path $env:LOCALAPPDATA 'ms-playwright'
$chromiumInstalled = Test-Path (Join-Path $playwrightRoot 'chromium-*')
if (-not $chromiumInstalled) {
    Write-Host 'Installing Playwright Chromium browser ...' -ForegroundColor Yellow
    python -m playwright install chromium
}
Pop-Location

Push-Location (Join-Path $Root 'frontend')
if (-not (Test-Path 'node_modules')) {
    Write-Host 'Installing frontend dependencies (npm install) ...' -ForegroundColor Yellow
    npm install
}
Pop-Location

function Test-ListeningPort([int]$Port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Get-PortPids([int]$Port) {
    # Match the local port followed by whitespace; -SimpleMatch would treat
    # the regex token literally and leave stale app processes running.
    $rows = netstat -ano -p tcp 2>$null | Select-String ":$Port\s"
    $pids = @()
    foreach ($row in $rows) {
        $parts = ($row.Line -split '\s+') | Where-Object { $_ }
        if ($parts.Count -ge 5) {
            $processId = $parts[-1]
            if ($processId -match '^\d+$') { $pids += [int]$processId }
        }
    }
    return $pids | Sort-Object -Unique
}

function Stop-AppProcessOnPort([int]$Port, [string]$ProcessPattern) {
    $pids = Get-PortPids -Port $Port
    foreach ($processId in $pids) {
        try {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction Stop
            $cmdLine = $proc.CommandLine
            if ($cmdLine -and $cmdLine -match $ProcessPattern) {
                Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
                Write-Host "Stopped stale process on port $Port (PID $processId)." -ForegroundColor Yellow
            }
        }
        catch {
            # Ignore unrelated listeners; only kill the app processes we started.
        }
    }
}

function Wait-Backend([int]$Attempts = 90) {
    for ($i = 0; $i -lt $Attempts; $i++) {
        try {
            $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/v1/health' -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Seconds 1
    }
    return $false
}

# --- Clear stale app processes left behind by prior runs ---
Stop-AppProcessOnPort -Port 8000 -ProcessPattern 'uvicorn|app\.main:app'
Stop-AppProcessOnPort -Port 5173 -ProcessPattern 'vite|npm run dev'

# --- Start backend (idempotent) ---
if (Test-ListeningPort 8000) {
    if (Wait-Backend 3) {
        Write-Host 'BACKEND already running on http://127.0.0.1:8000 — reusing it.' -ForegroundColor Yellow
    } else {
        Write-Host 'ERROR: Port 8000 is occupied by another process.' -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host 'Starting BACKEND on http://127.0.0.1:8000 ...' -ForegroundColor Green
    $logDir = Join-Path $Root 'logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    # WatchFiles/Uvicorn reload workers are unreliable on Windows' selector
    # event loop and can leave a healthy port serving stale code.  Run the
    # backend normally by default; developers can explicitly opt into reload.
    $backendArgs = @('-X', 'utf8', '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000', '--timeout-graceful-shutdown', '5')
    if ($env:DIRECTACT_ENABLE_RELOAD -eq '1') {
        $backendArgs += '--reload'
    }
    Start-Process -FilePath 'python.exe' `
        -ArgumentList $backendArgs `
        -WorkingDirectory (Join-Path $Root 'backend') `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir 'backend.out.log') `
        -RedirectStandardError (Join-Path $logDir 'backend.err.log') | Out-Null
    if (-not (Wait-Backend)) {
        Write-Host 'ERROR: Backend did not become ready on port 8000.' -ForegroundColor Red
        exit 1
    }
}

# --- Start frontend (idempotent) ---
if (Test-ListeningPort 5173) {
        Write-Host 'FRONTEND already running on http://127.0.0.1:5173 — reusing it.' -ForegroundColor Yellow
} else {
    Write-Host 'Starting FRONTEND on http://127.0.0.1:5173 ...' -ForegroundColor Green
    $logDir = Join-Path $Root 'logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    Start-Process -FilePath 'cmd.exe' `
        -ArgumentList @('/c', 'npm run dev -- --host 127.0.0.1') `
        -WorkingDirectory (Join-Path $Root 'frontend') `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir 'frontend.out.log') `
        -RedirectStandardError (Join-Path $logDir 'frontend.err.log') | Out-Null
    for ($i = 0; $i -lt 20 -and -not (Test-ListeningPort 5173); $i++) {
        Start-Sleep -Seconds 1
    }
    if (-not (Test-ListeningPort 5173)) {
        Write-Host 'ERROR: Frontend did not become ready on port 5173.' -ForegroundColor Red
        exit 1
    }
}

Write-Host ''
Write-Host 'Done! Open these URLs:' -ForegroundColor Green
Write-Host '  1. Sign up / Login : http://127.0.0.1:5173/signup' -ForegroundColor White
Write-Host '  2. Dashboard       : http://localhost:5173/dashboard' -ForegroundColor White
Write-Host '  3. API docs        : http://127.0.0.1:8000/docs' -ForegroundColor White
Write-Host ''
Write-Host 'Top bar mein Live dikhna chahiye (WebSocket connected).' -ForegroundColor Yellow
Write-Host 'Agar Offline dikhe to backend terminal check karo.' -ForegroundColor Yellow
Write-Host ''

Start-Process 'http://127.0.0.1:5173/signup'
