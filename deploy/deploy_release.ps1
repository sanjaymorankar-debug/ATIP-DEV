# ============================================================================
#  ATIP - deploy a release to the production instance (W9)
#
#  Production = this repository's `master` checkout (D:\Projects\ATIP) running
#  `python main.py` (scheduler + dashboard + index feed) on 127.0.0.1:8000.
#  A release is a tagged commit on master (e.g. ATIP-W9-RC2). Deploying = stopping
#  the running process, taking a verified backup, applying migrations and starting
#  the tagged code, then verifying health and trading safety.
#
#  DRY RUN BY DEFAULT: without -Authorize this only prints the plan and runs the
#  read-only preflight. Nothing is stopped, backed up, migrated or started.
#
#    powershell -ExecutionPolicy Bypass -File deploy\deploy_release.ps1 -ReleaseId ATIP-W9-RC2
#    powershell -ExecutionPolicy Bypass -File deploy\deploy_release.ps1 -ReleaseId ATIP-W9-RC2 -Authorize
#
#  It never enables live trading, never pushes, never edits configuration.
# ============================================================================
param(
    [Parameter(Mandatory = $true)][string]$ReleaseId,
    [switch]$Authorize,
    [int]$Port = 8000,
    [string]$RepoDir = "",
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
# Windows PowerShell 5.1 leaves $PSScriptRoot empty inside param() defaults: resolve here
if (-not $RepoDir) { $RepoDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path) }
Set-Location $RepoDir

function Step($n, $text) { Write-Host "`n[$n] $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "DEPLOYMENT STOPPED: $text" -ForegroundColor Red; exit 1 }
function AtipProcesses {
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -match '(^|\s|")main\.py("|\s|$)' -and $_.CommandLine -notmatch '--' }
}

Write-Host "ATIP deployment of $ReleaseId from $RepoDir  (mode: $(if ($Authorize) {'AUTHORIZED'} else {'DRY RUN'}))" -ForegroundColor Yellow

Step 1 "Verify release commit"
$tagCommit = (git rev-list -n 1 $ReleaseId) 2>$null
if (-not $tagCommit) { Fail "tag $ReleaseId not found" }
$head = git rev-parse HEAD
$branch = git rev-parse --abbrev-ref HEAD
Write-Host "  tag $ReleaseId -> $tagCommit ; HEAD $head on $branch"
if ($branch -ne "master") { Fail "production must be on master (is $branch)" }
if ($tagCommit -ne $head) { Fail "master HEAD is not the release commit; merge/fast-forward the release first" }

Step "2-5" "Preflight (clean tree, configuration, secrets, database, migrations, trading safety, dependencies)"
& $Python -m ops release preflight $ReleaseId
$preflightOk = ($LASTEXITCODE -eq 0)
if (-not $Authorize) {
    Write-Host "`nDRY RUN complete. The preflight 'pre-deployment backup' check fails until step 3 runs;" -ForegroundColor Yellow
    Write-Host "with -Authorize the script: 3 backs up, 6 stops ATIP, 7 records the manifest + config backup," -ForegroundColor Yellow
    Write-Host "8 applies migrations, 9 starts ATIP, 10-14 verifies, 15 records the deployment." -ForegroundColor Yellow
    exit 0
}

Step 3 "Pre-deployment database backup (verified)"
& $Python -m ops backup --kind pre-release
if ($LASTEXITCODE -ne 0) { Fail "backup was not VERIFIED" }
& $Python -m ops release preflight $ReleaseId
if ($LASTEXITCODE -ne 0) { Fail "preflight has FAIL items (see above)" }

Step 6 "Stop the running ATIP"
$procs = @(AtipProcesses)
foreach ($p in $procs) { Write-Host "  stopping PID $($p.ProcessId)"; Stop-Process -Id $p.ProcessId -Force }
# Autostart / Task Scheduler may relaunch ATIP within ~40 s; the relaunch runs the code on disk
# (= the release). Wait and adopt it instead of starting a second instance (the mutex would refuse).
Start-Sleep -Seconds 45
$relaunched = @(AtipProcesses)

Step 7 "Record the release manifest and back up the configuration"
& $Python -m ops release manifest $ReleaseId | Out-Null
Write-Host "  atip_data\releases\$ReleaseId\manifest.json (+ config.json / .env copies, local only)"

Step 8 "Apply database migrations"
if ($relaunched.Count -gt 0) {
    Write-Host "  ATIP was relaunched automatically (PID $($relaunched[0].ProcessId)); it applies migrations at start"
} else {
    & $Python -m ops migrate
    if ($LASTEXITCODE -ne 0) { Fail "a migration FAILED - restore with deploy\rollback_release.ps1" }
}

Step 9 "Start ATIP (python main.py - never --dashboard)"
if ($relaunched.Count -eq 0) {
    $p = Start-Process -FilePath $Python -ArgumentList "main.py" -WorkingDirectory $RepoDir -WindowStyle Minimized -PassThru
    Write-Host "  started PID $($p.Id)"
}

Step "10-14" "Verify health, logs, scheduler, broker mode and LIVE_TRADING_ENABLED = FALSE"
& $Python -m ops release postcheck --port $Port --wait 180
$ok = ($LASTEXITCODE -eq 0)

Step 15 "Record the deployment"
& $Python -m ops release record $ReleaseId $(if ($ok) {"DEPLOYED"} else {"FAILED"}) "deploy_release.ps1"
if (-not $ok) { Fail "post-deployment checks failed - investigate, or roll back with deploy\rollback_release.ps1" }
Write-Host "`nDEPLOYED $ReleaseId ($head). Post-deployment smoke checks passed; this is NOT functional testing." -ForegroundColor Green
