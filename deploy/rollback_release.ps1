# ============================================================================
#  ATIP - roll the production instance back to a previous release (W9)
#
#  Application rollback: master is reset to -ToRef (a release tag or a
#  backup/pre-<wave>-master branch). Commits after it stay reachable on their
#  feature branch / tag. Untracked files (the owner's spreadsheets, atip_data/)
#  are not touched by the reset.
#
#  Database rollback (optional, -RestoreBackup <backup_id>): ATIP schema changes are
#  additive, so older code runs on the newer schema and a code rollback is normally
#  enough. Restore the database only when the release damaged data: the verified
#  backup is copied to a NEW file, the live file is kept as atip.db.pre-rollback-<ts>
#  and the restored copy takes its place. Data written after that backup is lost.
#
#  Configuration rollback (optional, -RestoreConfigFrom <release id>): copies
#  atip_data\releases\<id>\config.json.backup back to atip_data\config.json
#  (the current file is kept as config.json.pre-rollback-<ts>).
#
#  DRY RUN BY DEFAULT; -Authorize performs it.
#    powershell -ExecutionPolicy Bypass -File deploy\rollback_release.ps1 -ToRef backup/pre-w9-master
#    powershell -ExecutionPolicy Bypass -File deploy\rollback_release.ps1 -ToRef backup/pre-w9-master -Authorize
# ============================================================================
param(
    [Parameter(Mandatory = $true)][string]$ToRef,
    [string]$RestoreBackup = "",
    [string]$RestoreConfigFrom = "",
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
function Fail($text) { Write-Host "ROLLBACK STOPPED: $text" -ForegroundColor Red; exit 1 }
function AtipProcesses {
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -match '(^|\s|")main\.py("|\s|$)' -and $_.CommandLine -notmatch '--' }
}

$target = (git rev-parse --verify "$ToRef^{commit}") 2>$null
if (-not $target) { Fail "$ToRef is not a commit / tag / branch" }
$current = git rev-parse HEAD
Write-Host "Rollback master $current -> $ToRef ($target)  (mode: $(if ($Authorize) {'AUTHORIZED'} else {'DRY RUN'}))" -ForegroundColor Yellow
Write-Host "  database restore : $(if ($RestoreBackup) {$RestoreBackup} else {'no (code-only rollback)'})"
Write-Host "  config restore   : $(if ($RestoreConfigFrom) {"from release $RestoreConfigFrom"} else {'no'})"
if (-not $Authorize) { Write-Host "`nDRY RUN: nothing changed. Re-run with -Authorize." -ForegroundColor Yellow; exit 0 }

Step 1 "Safety backup of the current database"
& $Python -m ops backup --kind pre-release
if ($LASTEXITCODE -ne 0) { Fail "could not take a VERIFIED safety backup" }

Step 2 "Stop ATIP"
foreach ($p in @(AtipProcesses)) { Write-Host "  stopping PID $($p.ProcessId)"; Stop-Process -Id $p.ProcessId -Force }
Start-Sleep -Seconds 3

Step 3 "Application rollback (git)"
git checkout master
git reset --hard $target
Write-Host "  master now at $(git rev-parse --short HEAD)"

if ($RestoreBackup) {
    Step 4 "Database rollback from $RestoreBackup"
    $ts = Get-Date -Format "yyyyMMdd-HHmmss"
    $restored = "atip_data\restore\rollback-$ts.db"
    & $Python -m ops restore $RestoreBackup --target $restored
    if ($LASTEXITCODE -ne 0) { Fail "restore verification failed; live database untouched" }
    Rename-Item "atip_data\atip.db" "atip.db.pre-rollback-$ts"
    foreach ($s in "-wal", "-shm") { if (Test-Path "atip_data\atip.db$s") { Rename-Item "atip_data\atip.db$s" "atip.db.pre-rollback-$ts$s" } }
    Copy-Item $restored "atip_data\atip.db"
    Write-Host "  restored; previous file kept as atip_data\atip.db.pre-rollback-$ts"
}
if ($RestoreConfigFrom) {
    Step 5 "Configuration rollback"
    $src = "atip_data\releases\$RestoreConfigFrom\config.json.backup"
    if (-not (Test-Path $src)) { Fail "$src not found" }
    $ts = Get-Date -Format "yyyyMMdd-HHmmss"
    Copy-Item "atip_data\config.json" "atip_data\config.json.pre-rollback-$ts"
    Copy-Item $src "atip_data\config.json" -Force
}

Step 6 "Start ATIP (python main.py)"
Start-Sleep -Seconds 45        # autostart may relaunch it by itself; adopt that instance
if (@(AtipProcesses).Count -eq 0) {
    Start-Process -FilePath $Python -ArgumentList "main.py" -WorkingDirectory $RepoDir -WindowStyle Minimized | Out-Null
}

Step 7 "Verify health and trading safety"
& $Python -m ops release postcheck --port $Port --wait 180
$ok = ($LASTEXITCODE -eq 0)
& $Python -m ops release record $ToRef $(if ($ok) {"ROLLED_BACK"} else {"FAILED"}) "rollback_release.ps1 from $current"
if (-not $ok) { Fail "post-rollback checks failed - see output above" }
Write-Host "`nROLLED BACK to $ToRef. Smoke checks passed (not functional testing)." -ForegroundColor Green
