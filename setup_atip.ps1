# ============================================================
#  ATIP — Windows PowerShell Setup + Startup Automation
#  Run as Administrator: Right-click → Run with PowerShell
# ============================================================

param(
    [switch]$Install,
    [switch]$RegisterStartup,
    [switch]$UnregisterStartup,
    [switch]$Status,
    [switch]$All
)

$ATIP_DIR = "D:\Projects\atip"
$TASK_NAME = "ATIP Platform"

function Write-Header($text) {
    Write-Host "`n$('='*55)" -ForegroundColor Cyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host "$('='*55)`n" -ForegroundColor Cyan
}

function Install-Packages {
    Write-Header "Installing Python Packages"
    Set-Location $ATIP_DIR

    $packages = @(
        @("Core",       "pandas numpy requests python-dotenv"),
        @("Dhan API",   "dhanhq"),
        @("yfinance",   "yfinance"),
        @("TA Lib",     "pandas-ta --no-deps"),
        @("TA fallback","ta"),
        @("Scheduler",  "schedule"),
        @("Dashboard",  "fastapi uvicorn[standard]"),
        @("AI + News",  "anthropic feedparser beautifulsoup4"),
        @("Utils",      "tqdm openpyxl")
    )

    foreach ($pkg in $packages) {
        Write-Host "  Installing $($pkg[0])..." -ForegroundColor Yellow
        pip install $pkg[1].Split(" ") 2>&1 | Select-String -Pattern "Successfully|already|ERROR" | Write-Host
    }

    Write-Host "`n✅  All packages installed!" -ForegroundColor Green
}

function Setup-Config {
    Write-Header "Setting Up Configuration"
    $data_dir = Join-Path $ATIP_DIR "atip_data"
    $config   = Join-Path $data_dir "config.json"
    $template = Join-Path $ATIP_DIR "config_template.json"

    if (-not (Test-Path $data_dir)) {
        New-Item -ItemType Directory -Path $data_dir | Out-Null
        Write-Host "  ✅  Created atip_data\" -ForegroundColor Green
    }

    if (-not (Test-Path $config)) {
        Copy-Item $template $config
        Write-Host "  ✅  Created atip_data\config.json" -ForegroundColor Green
        Write-Host "  ⚠️   IMPORTANT: Edit atip_data\config.json and add your:" -ForegroundColor Yellow
        Write-Host "       - dhan_client_id       (from https://web.dhanhq.com → My Profile → Apps)" -ForegroundColor Yellow
        Write-Host "       - dhan_access_token" -ForegroundColor Yellow
        Write-Host "       - telegram_token + telegram_chat_id" -ForegroundColor Yellow
    } else {
        Write-Host "  ℹ️   config.json already exists — not overwriting" -ForegroundColor Blue
    }
}

function Initialize-Database {
    Write-Header "Initialising Database"
    Set-Location $ATIP_DIR
    python main.py --init
    Write-Host "`n✅  Database initialised!" -ForegroundColor Green
}

function Register-StartupTask {
    Write-Header "Registering Windows Startup Task"

    $xml_path = Join-Path $ATIP_DIR "ATIP_TaskScheduler.xml"

    if (-not (Test-Path $xml_path)) {
        Write-Host "  ❌  ATIP_TaskScheduler.xml not found at $xml_path" -ForegroundColor Red
        return
    }

    # Check if task already exists
    $existing = schtasks /query /tn $TASK_NAME 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  ℹ️   Task '$TASK_NAME' already registered — updating..." -ForegroundColor Blue
        schtasks /delete /tn $TASK_NAME /f | Out-Null
    }

    # Register the task
    $result = schtasks /create /xml $xml_path /tn $TASK_NAME 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  ✅  Task '$TASK_NAME' registered successfully!" -ForegroundColor Green
        Write-Host "  ✅  ATIP will now auto-start on Windows login" -ForegroundColor Green
        Write-Host "  ✅  And run daily at 6:45 AM on weekdays" -ForegroundColor Green
    } else {
        Write-Host "  ❌  Failed to register task:" -ForegroundColor Red
        Write-Host $result -ForegroundColor Red
        Write-Host "`n  Try running this PowerShell as Administrator" -ForegroundColor Yellow
    }
}

function Unregister-StartupTask {
    Write-Header "Removing Startup Task"
    $result = schtasks /delete /tn $TASK_NAME /f 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  ✅  Task '$TASK_NAME' removed — ATIP will no longer auto-start" -ForegroundColor Green
    } else {
        Write-Host "  ⚠️   Task not found or already removed" -ForegroundColor Yellow
    }
}

function Show-Status {
    Write-Header "ATIP Status"

    # Task Scheduler status
    Write-Host "  Windows Startup Task:" -ForegroundColor Cyan
    $task = schtasks /query /tn $TASK_NAME /fo LIST 2>&1
    if ($LASTEXITCODE -eq 0) {
        $task | Select-String "Status|Last Run|Next Run" | ForEach-Object {
            Write-Host "    $_" -ForegroundColor White
        }
    } else {
        Write-Host "    ⚠️  Not registered (run .\setup_atip.ps1 -RegisterStartup)" -ForegroundColor Yellow
    }

    # Python and packages
    Write-Host "`n  Python Environment:" -ForegroundColor Cyan
    Write-Host "    $(python --version 2>&1)" -ForegroundColor White

    $pkgs = @("pandas","yfinance","dhanhq","fastapi","schedule","anthropic")
    foreach ($pkg in $pkgs) {
        $ver = pip show $pkg 2>&1 | Select-String "Version" | ForEach-Object { $_.ToString().Split(":")[1].Trim() }
        if ($ver) {
            Write-Host "    ✅  $pkg $ver" -ForegroundColor Green
        } else {
            Write-Host "    ❌  $pkg (not installed)" -ForegroundColor Red
        }
    }

    # Config file
    Write-Host "`n  Configuration:" -ForegroundColor Cyan
    $config = Join-Path $ATIP_DIR "atip_data\config.json"
    if (Test-Path $config) {
        $cfg = Get-Content $config | ConvertFrom-Json
        $dhan_ok  = $cfg.dhan_client_id  -and $cfg.dhan_client_id  -ne "YOUR_DHAN_CLIENT_ID"
        $tg_ok    = $cfg.telegram_token  -and $cfg.telegram_token  -ne "YOUR_TELEGRAM_BOT_TOKEN"
        Write-Host "    $(if($dhan_ok){'✅'}else{'⚠️ '}) Dhan API     $(if($dhan_ok){'configured'}else{'NOT configured — edit atip_data\config.json'})" -ForegroundColor $(if($dhan_ok){'Green'}else{'Yellow'})
        Write-Host "    $(if($tg_ok){'✅'}else{'⚠️ '}) Telegram     $(if($tg_ok){'configured'}else{'NOT configured'})" -ForegroundColor $(if($tg_ok){'Green'}else{'Yellow'})
    } else {
        Write-Host "    ❌  config.json not found — run .\setup_atip.ps1 -All" -ForegroundColor Red
    }

    # Database
    $db = Join-Path $ATIP_DIR "atip_data\db"
    if (Test-Path $db) {
        $size = [math]::Round((Get-Item $db).Length / 1MB, 2)
        Write-Host "`n  Database: ✅  db ($size MB)" -ForegroundColor Green
    } else {
        Write-Host "`n  Database: ❌  Not created yet — run: python main.py --init" -ForegroundColor Red
    }
}

# ── Main ─────────────────────────────────────────────────────────────────

Write-Host @"

  ╔══════════════════════════════════════════════════════╗
  ║   ATIP — Windows Setup & Startup Automation          ║
  ╚══════════════════════════════════════════════════════╝

"@ -ForegroundColor Cyan

Set-Location $ATIP_DIR

if ($All) {
    Install-Packages
    Setup-Config
    Initialize-Database
    Register-StartupTask
    Show-Status
    Write-Host "`n🚀  ATIP is ready! Run: python main.py`n" -ForegroundColor Green
}
elseif ($Install)           { Install-Packages;        Setup-Config }
elseif ($RegisterStartup)   { Register-StartupTask }
elseif ($UnregisterStartup) { Unregister-StartupTask }
elseif ($Status)            { Show-Status }
else {
    Write-Host "  Usage:" -ForegroundColor Yellow
    Write-Host "    .\setup_atip.ps1 -All                # Full setup (first time)" -ForegroundColor White
    Write-Host "    .\setup_atip.ps1 -Install            # Install packages only" -ForegroundColor White
    Write-Host "    .\setup_atip.ps1 -RegisterStartup    # Auto-start on Windows login" -ForegroundColor White
    Write-Host "    .\setup_atip.ps1 -UnregisterStartup  # Remove auto-start" -ForegroundColor White
    Write-Host "    .\setup_atip.ps1 -Status             # Check everything" -ForegroundColor White
    Write-Host ""
    Write-Host "  Quick start (run as Administrator):" -ForegroundColor Yellow
    Write-Host "    .\setup_atip.ps1 -All" -ForegroundColor Cyan
    Write-Host ""
}
