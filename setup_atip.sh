#!/usr/bin/env bash
# ATIP — macOS setup, migration and deployment. The Mac counterpart of
# setup_atip.ps1; the plan it follows is docs/MAC_DEPLOYMENT.md.
#
# Like start_atip.sh it resolves the project from its OWN location, never a
# hardcoded path, so it works wherever the clone lives (now
# /Users/agtci/Documents/GIT_ATIP/ATIP) and from any subfolder: calling
# ../setup_atip.sh from dashboard/ or pipeline/ still acts on the project root.
#
# Usage: ./setup_atip.sh <command>
#   check                  read-only preflight: what is ready and what is missing
#   install                .venv on Python 3.11+, packages, atip_data/config.json, .env, database
#   migrate --from DIR     copy atip_data/ and .env from an older ATIP folder (stop that copy first)
#   data                   first market data: Dhan security list + 600-day history (needs the Dhan keys)
#   agents [TARGET]        LaunchAgents for THIS folder: all (default), platform, token, snapshot, --uninstall
#   start                  run ATIP in this Terminal, on IST
#   stop                   stop the LaunchAgent copy of ATIP
#   verify                 is ATIP running from this folder, and healthy?
#   all [--from DIR]       migrate (with --from), install, data, agents, verify
#
# Add --yes to answer yes to its questions: stop an old copy's LaunchAgent, replace
# this folder's own atip_data/ (kept aside, never deleted), move the LaunchAgents here.
# ATIP_PYTHON=/path/to/python3.x picks the interpreter; ATIP_PORT=8080 tells it about
# a dashboard started on another --port.
# Written for macOS's own /bin/bash 3.2: no associative arrays, no ${x,,}.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CALLED_FROM="$PWD"
cd "$ROOT" || exit 1

VENV_PY="$ROOT/.venv/bin/python"
LA_DIR="$HOME/Library/LaunchAgents"
PLATFORM_LABEL="com.atip.platform"
TOKEN_LABEL="com.atip.dhan-token-refresh"
SNAPSHOT_LABEL="com.atip.publish-snapshot"
ALL_LABELS="$PLATFORM_LABEL $TOKEN_LABEL $SNAPSHOT_LABEL"
IS_MAC=0
[[ "$(uname -s)" == "Darwin" ]] && IS_MAC=1
# main.py serves the dashboard on --port, 8000 unless given; config.json's
# dashboard_port is not read by it. ATIP_PORT tells this script about another one.
PORT="${ATIP_PORT:-8000}"
PROBLEMS=0
YES=0
FROM=""

# ── Output ────────────────────────────────────────────────────────────────
ok()   { printf '  ✅  %s\n' "$*"; }
warn() { printf '  ⚠️   %s\n' "$*"; }
bad()  { printf '  ❌  %s\n' "$*"; PROBLEMS=$((PROBLEMS + 1)); }
info() { printf '      %s\n' "$*"; }
step() { printf '\n── %s ──\n' "$*"; }
die()  { printf '\n  ❌  %s\n\n' "$*" >&2; exit 1; }

confirm() {   # $1 = question; yes only with --yes or a "y" typed at a terminal
    [[ $YES == 1 ]] && return 0
    [[ -t 0 ]] || return 1
    local ans
    read -r -p "      $1 [y/N] " ans
    [[ "$ans" =~ ^[Yy] ]]
}

# ── Python ────────────────────────────────────────────────────────────────
py_version_ok() {   # $1 = interpreter; true when it is Python 3.11 or newer
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

find_python() {     # prints a Python 3.11+; Homebrew's 3.13 first (the README's), ATIP_PYTHON overrides
    if [[ -n "${ATIP_PYTHON:-}" ]]; then
        py_version_ok "$ATIP_PYTHON" && { echo "$ATIP_PYTHON"; return 0; }
        return 1
    fi
    local prefix="" cand p
    command -v brew >/dev/null 2>&1 && prefix="$(brew --prefix 2>/dev/null)"
    for cand in "$prefix/bin/python3.13" /opt/homebrew/bin/python3.13 /usr/local/bin/python3.13 \
                python3.13 python3.14 python3.12 python3.11 python3; do
        p="$(command -v "$cand" 2>/dev/null)" || continue
        py_version_ok "$p" && { echo "$p"; return 0; }
    done
    return 1
}

helper_py() {       # any Python for the stdlib-only helpers below (sqlite3, fcntl, json)
    if [[ -x "$VENV_PY" ]]; then echo "$VENV_PY"; return; fi
    find_python || command -v python3 || echo python3
}

require_venv() {
    [[ -x "$VENV_PY" ]] && py_version_ok "$VENV_PY" && return 0
    die ".venv with Python 3.11+ is missing — run ./setup_atip.sh install first"
}

# ── ATIP state ────────────────────────────────────────────────────────────
instance_running() {   # $1 = ATIP folder; true while a scheduler/dashboard holds its lock (main.py)
    local lock="$1/atip_data/atip_single_instance.lock"
    [[ -f "$lock" ]] || return 1
    "$(helper_py)" - "$lock" <<'PY'
import fcntl, sys
fd = open(sys.argv[1], "a")
try:
    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(0)                     # held: ATIP is running from that folder
fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
sys.exit(1)
PY
}

wait_stopped() {   # $1 = folder; up to 30 s for its ATIP to let go of the lock
    local i
    for i in $(seq 1 30); do
        instance_running "$1" || return 0
        sleep 1
    done
    return 1
}

agent_dir() {      # $1 = label; the folder an installed LaunchAgent runs from
    local f="$LA_DIR/$1.plist"
    [[ -f "$f" ]] || return 1
    grep -A1 '<key>WorkingDirectory</key>' "$f" | sed -n 's:.*<string>\(.*\)</string>.*:\1:p' | head -n 1
}

agent_loaded() { [[ $IS_MAC == 1 ]] && launchctl print "gui/$UID/$1" >/dev/null 2>&1; }

other_atip_pids() {   # "pid<TAB>folder" for each python main.py running outside this folder
    local pid cwd cmdline prog
    for pid in $(pgrep -f 'main\.py' 2>/dev/null); do
        [[ "$pid" == "$$" ]] && continue
        cmdline="$(ps -o command= -p "$pid" 2>/dev/null)" || continue
        prog="${cmdline%% *}"
        prog="${prog##*/}"                  # python3.13, python, or Python (Homebrew's framework build)
        [[ "$prog" =~ ^[Pp]ython ]] || continue   # not a wrapper such as nohup or timeout
        [[ "$cmdline" =~ main\.py ]] || continue
        cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
        [[ -z "$cwd" && -e "/proc/$pid/cwd" ]] && cwd="$(readlink "/proc/$pid/cwd")"
        [[ "$cwd" == "$ROOT" ]] && continue
        printf '%s\t%s\n' "$pid" "${cwd:-an unknown folder}"
    done
}

db_summary() {     # $1 = sqlite file; "rows symbols last_date" of prices_daily, read-only
    "$(helper_py)" - "$1" <<'PY' 2>/dev/null || echo "0 0 -"
import pathlib, sqlite3, sys
c = sqlite3.connect(pathlib.Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True)
try:
    n, s, d = c.execute("SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(date) FROM prices_daily").fetchone()
except sqlite3.Error:
    n, s, d = 0, 0, None
print(n or 0, s or 0, d or "-")
PY
}

secret_states() {  # "NAME state source" for the keys ATIP uses; never prints a value
    "$VENV_PY" - <<'PY' 2>/dev/null
from ops.secrets import get, status
for name in ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "DHAN_PIN", "DHAN_TOTP_SECRET", "TELEGRAM_TOKEN",
             "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY", "ALPHA_VANTAGE_KEY", "ATIP_ENCRYPTION_KEY"):
    v = get(name, log_access=False) or ""
    state = "missing" if not v else "placeholder" if v.startswith("YOUR_") else "set"
    src = {"env": "shell", "dotenv": ".env", "file": "atip_data/secrets", "vault": "vault",
           "legacy_config": "config.json"}.get(status(name)["source"], "-")
    print(name, state, src)
PY
}

missing_packages() {   # the requirements.txt modules .venv cannot import
    "$VENV_PY" - <<'PY' 2>/dev/null
import importlib
mods = ["pandas", "numpy", "requests", "dhanhq", "yfinance", "ta", "schedule", "fastapi", "uvicorn",
        "anthropic", "feedparser", "bs4", "dotenv", "cryptography", "openpyxl", "tqdm"]
miss = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception:
        miss.append(m)
print(" ".join(miss))
PY
}

db_quick_check() {   # $1 = sqlite file; prints "ok" when PRAGMA quick_check passes
    "$(helper_py)" - "$1" <<'PY' 2>&1
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute("PRAGMA quick_check").fetchone()[0])
PY
}

dhan_configured() {
    [[ -x "$VENV_PY" ]] || return 1
    local s
    s="$(secret_states)"
    grep -q '^DHAN_CLIENT_ID set ' <<<"$s" && grep -q '^DHAN_ACCESS_TOKEN set ' <<<"$s"
}

health() {   # $1 = path; prints the HTTP status of the local dashboard, 000 when nothing answers
    curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:$PORT$1" 2>/dev/null || true
}

# ══ check ═════════════════════════════════════════════════════════════════
cmd_check() {
    PROBLEMS=0
    step "Folder"
    ok "ATIP folder: $ROOT"
    if [[ "$CALLED_FROM" != "$ROOT" ]]; then
        info "(you ran this from $CALLED_FROM — ATIP always runs from the ATIP folder shown above,"
        info " the one holding main.py; dashboard/ and pipeline/ are packages inside it)"
    fi
    if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
        local dirty=""
        [[ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no 2>/dev/null)" ]] && dirty=", local edits"
        ok "git: $(git -C "$ROOT" rev-parse --abbrev-ref HEAD) at $(git -C "$ROOT" log -1 --format='%h %cd' --date=short)$dirty"
    else
        warn "not a git checkout — updates then mean copying files by hand"
    fi
    if [[ $IS_MAC == 1 ]]; then
        case "$ROOT/" in
            "$HOME/Documents/"*)
                warn "inside ~/Documents: the LaunchAgent needs Full Disk Access for /bin/bash"
                info "(System Settings → Privacy & Security → Full Disk Access), else it fails"
                info "with 'Operation not permitted' — see docs/MAC_DEPLOYMENT.md, step 7" ;;
        esac
        if [[ -d "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Documents" ]]; then
            case "$ROOT/" in
                "$HOME/Documents/"*)
                    bad "iCloud 'Desktop & Documents Folders' looks ON: it syncs atip.db while ATIP writes it"
                    info "turn it off (System Settings → your name → iCloud → iCloud Drive), or move ATIP out of ~/Documents" ;;
            esac
        fi
    fi

    step "Python"
    local py
    if py="$(find_python)"; then
        ok "Python 3.11+ available: $("$py" --version 2>&1) ($py)"
    else
        bad "no Python 3.11+ (macOS's own python3 is 3.9) — brew install python@3.13"
    fi
    if [[ -x "$VENV_PY" ]]; then
        if py_version_ok "$VENV_PY"; then
            ok ".venv: $("$VENV_PY" --version 2>&1)"
            local missing
            missing="$(missing_packages)"
            if [[ -z "$missing" ]]; then ok "packages: all of requirements.txt import"
            else bad "packages missing in .venv: $missing — ./setup_atip.sh install"; fi
        else
            bad ".venv is built on $("$VENV_PY" --version 2>&1) (too old) — ./setup_atip.sh install rebuilds it"
        fi
    else
        bad "no .venv yet — ./setup_atip.sh install"
    fi

    step "Configuration and keys"
    if [[ -f atip_data/config.json ]]; then ok "atip_data/config.json"
    else bad "atip_data/config.json missing — ./setup_atip.sh install creates it from config_template.json"; fi
    [[ -f .env ]] && ok ".env" || warn "no .env (ANTHROPIC_API_KEY goes there; ./setup_atip.sh install creates it)"
    if [[ -x "$VENV_PY" && -f atip_data/config.json ]]; then
        local name state src
        while read -r name state src; do
            [[ -n "$name" ]] || continue
            case "$name:$state" in
                DHAN_CLIENT_ID:set|DHAN_ACCESS_TOKEN:set) ok "$name (from $src)" ;;
                DHAN_CLIENT_ID:*|DHAN_ACCESS_TOKEN:*)     bad "$name not set — required (atip_data/config.json)" ;;
                DHAN_PIN:set|DHAN_TOTP_SECRET:set)        ok "$name (token auto-renewal, from $src)" ;;
                DHAN_PIN:*|DHAN_TOTP_SECRET:*)            warn "$name not set — an EXPIRED Dhan token then needs renewing by hand" ;;
                ANTHROPIC_API_KEY:set)                    ok "$name (from $src)" ;;
                ANTHROPIC_API_KEY:*)                      warn "$name not set — news uses the rule-based fallback" ;;
                ATIP_ENCRYPTION_KEY:set)                  ok "$name (from $src)" ;;
                ATIP_ENCRYPTION_KEY:*)
                    if [[ -f atip_data/secrets/vault.json ]]; then bad "vault.json exists but ATIP_ENCRYPTION_KEY is missing — the vault cannot be read"
                    else info "ATIP_ENCRYPTION_KEY not set (only needed for the vault: python -m ops keygen)"; fi ;;
                *:set)                                    ok "$name (from $src)" ;;
                *)                                        info "$name not set (optional)" ;;
            esac
            if [[ "$state" == set && "$src" == shell ]]; then
                warn "$name comes from your shell only — the LaunchAgent will not see it; put it in .env"
            fi
        done <<<"$(secret_states)"
    elif [[ ! -x "$VENV_PY" ]]; then
        info "keys are checked once .venv exists"
    fi

    step "Data"
    if [[ -f atip_data/atip.db ]]; then
        local rows syms last
        read -r rows syms last <<<"$(db_summary atip_data/atip.db)"
        ok "atip_data/atip.db: $(du -h atip_data/atip.db | cut -f1), $rows daily prices for $syms symbols, last $last"
        [[ "$rows" == 0 ]] && warn "no price history yet — ./setup_atip.sh data (or migrate your old atip_data/)"
    else
        bad "no database yet — ./setup_atip.sh install (or migrate --from your old folder)"
    fi
    [[ -f atip_data/raw/dhan/security_id_list.csv ]] && ok "Dhan security list" \
        || warn "no Dhan security list — ./setup_atip.sh data"
    [[ -f atip_data/publish.json ]] && ok "atip_data/publish.json (snapshot upload)" \
        || info "no atip_data/publish.json — the 5-minute snapshot agent is skipped"

    step "This Mac"
    if [[ "$(date +%z)" == "+0530" ]]; then ok "clock is on IST"
    else warn "clock is on $(date +%Z) — the agents and ./setup_atip.sh start set IST themselves; the 06:30 token agent uses this clock"; fi
    if [[ $IS_MAC == 1 ]]; then
        local sl
        sl="$(pmset -g 2>/dev/null | awk '$1 == "sleep" {print $2; exit}')"
        if [[ -n "$sl" && "$sl" != 0 ]]; then
            warn "the Mac sleeps after $sl min idle — nothing runs while it sleeps (docs/MAC_DEPLOYMENT.md, step 7)"
        fi
    fi
    local code
    code="$(health /health/live)"
    if instance_running "$ROOT"; then
        ok "ATIP is running from this folder (dashboard http://localhost:$PORT answers $code)"
    elif [[ "$code" != 000 ]]; then
        bad "port $PORT is already taken — see what holds it: lsof -nP -iTCP:$PORT -sTCP:LISTEN"
    else
        ok "port $PORT is free"
    fi
    local others
    others="$(other_atip_pids)"
    if [[ -n "$others" ]]; then
        while IFS=$'\t' read -r pid dir; do
            warn "another ATIP (pid $pid) runs from $dir — two copies send every alert twice"
        done <<<"$others"
    fi

    if [[ $IS_MAC == 1 ]]; then
        step "LaunchAgents"
        local label dir
        for label in $ALL_LABELS; do
            if dir="$(agent_dir "$label")"; then
                if [[ "$dir" == "$ROOT" ]]; then ok "$label → this folder$(agent_loaded "$label" || echo ' (not loaded)')"
                else warn "$label still points at $dir — ./setup_atip.sh agents moves it here"; fi
            else
                info "$label not installed"
            fi
        done
    fi

    echo
    if [[ $PROBLEMS == 0 ]]; then echo "  Ready. Nothing blocking."; return 0; fi
    echo "  $PROBLEMS blocking item(s) above (❌)."
    return 1
}

# ══ install ═══════════════════════════════════════════════════════════════
cmd_install() {
    step "Python 3.11+"
    local py
    if ! py="$(find_python)"; then
        if [[ $IS_MAC == 1 ]] && command -v brew >/dev/null 2>&1; then
            info "no Python 3.11+ found; installing Homebrew's python@3.13"
            brew install python@3.13 || die "brew install python@3.13 failed"
            py="$(find_python)" || die "python@3.13 installed but not found; rerun with ATIP_PYTHON=/path/to/python3.13"
        else
            die "Python 3.11+ not found. Install Homebrew (https://brew.sh), then: brew install python@3.13"
        fi
    fi
    ok "$("$py" --version 2>&1) ($py)"

    if instance_running "$ROOT"; then
        warn "ATIP is running from this folder; restart it after this install picks up new packages:"
        info "launchctl kickstart -k gui/$UID/$PLATFORM_LABEL   (or Ctrl+C and ./setup_atip.sh start)"
    fi

    step "Virtualenv (.venv)"
    if [[ -x "$VENV_PY" ]] && py_version_ok "$VENV_PY"; then
        ok "reusing .venv ($("$VENV_PY" --version 2>&1))"
    else
        if [[ -e .venv ]]; then
            info ".venv has no Python 3.11+ (built from Apple's 3.9, or moved from another folder) — rebuilding it"
            rm -rf .venv
        fi
        "$py" -m venv .venv || die "could not create .venv with $py"
        ok "created .venv ($("$VENV_PY" --version 2>&1))"
    fi

    step "Packages (requirements.txt)"
    "$VENV_PY" -m pip install --quiet --upgrade pip || die "pip could not upgrade itself"
    "$VENV_PY" -m pip install --quiet -r requirements.txt || die "pip install -r requirements.txt failed — see above"
    ok "requirements.txt installed"

    step "Configuration"
    mkdir -p atip_data
    if [[ -f atip_data/config.json ]]; then
        ok "atip_data/config.json exists — left as it is"
    else
        cp config_template.json atip_data/config.json && chmod 600 atip_data/config.json
        ok "created atip_data/config.json from config_template.json"
        info "add dhan_client_id and dhan_access_token:  open -e atip_data/config.json"
    fi
    if [[ -f .env ]]; then
        ok ".env exists — left as it is"
    else
        printf '%s\n' "# ATIP secrets, read by ops/secrets.py. Git-ignored. One NAME=value per line." \
                      "# ANTHROPIC_API_KEY=sk-ant-..." > .env && chmod 600 .env
        ok "created .env (add ANTHROPIC_API_KEY=... for AI news)"
    fi
    chmod +x start_atip.sh publish_snapshot.sh tools/dhan_token_refresh.sh deploy/launchd/install.sh \
             setup_atip.sh 2>/dev/null || true

    step "Database"
    if "$VENV_PY" main.py --init > atip_data/setup_init.log 2>&1; then
        ok "atip_data/atip.db ready — schema created or upgraded, nothing dropped (log: atip_data/setup_init.log)"
    else
        tail -n 20 atip_data/setup_init.log >&2
        die "python main.py --init failed (full log: atip_data/setup_init.log)"
    fi
}

# ══ migrate ═══════════════════════════════════════════════════════════════
cmd_migrate() {
    [[ -n "$FROM" ]] || die "usage: ./setup_atip.sh migrate --from /path/to/the/old/ATIP/folder"
    local from="$FROM"
    [[ "$from" == /* ]] || from="$CALLED_FROM/$from"     # relative to where you typed it
    from="$(cd "$from" 2>/dev/null && pwd)" || die "no such folder: $FROM"
    # Pointed at a package inside the old project (…/ATIP-dev/pipeline)? Use the project.
    if [[ ! -f "$from/main.py" && -f "$from/../main.py" ]]; then
        from="$(cd "$from/.." && pwd)"
        info "using $from (the folder holding main.py)"
    fi
    [[ "$from" != "$ROOT" ]] || die "--from is this folder; give the OLD folder"
    [[ -d "$from/atip_data" ]] || die "$from has no atip_data/ — nothing to migrate"

    step "The old copy must be stopped"
    if instance_running "$from"; then
        if agent_loaded "$PLATFORM_LABEL" && [[ "$(agent_dir "$PLATFORM_LABEL")" == "$from" ]] &&
           confirm "ATIP's LaunchAgent is running from $from. Stop it now?"; then
            launchctl bootout "gui/$UID/$PLATFORM_LABEL" 2>/dev/null || true
            wait_stopped "$from" || true
        fi
        instance_running "$from" && die "ATIP is still running from $from. Stop it (Ctrl+C in its Terminal, or
      launchctl bootout gui/$UID/$PLATFORM_LABEL), then run this again. A copy taken while it
      writes can be inconsistent."
    fi
    ok "no ATIP running from $from"
    if instance_running "$ROOT"; then
        die "ATIP is running from this folder; stop it before migrating into it (./setup_atip.sh stop)"
    fi

    step "Space"
    local need avail
    need="$(du -sk "$from/atip_data" | cut -f1)"
    avail="$(df -Pk "$ROOT" | awk 'NR == 2 {print $4}')"
    [[ "$need" -lt "$avail" ]] || die "atip_data/ needs $((need / 1024)) MB, only $((avail / 1024)) MB free here"
    ok "$((need / 1024)) MB to copy, $((avail / 1024)) MB free"

    step "Copy atip_data/ and .env"
    if [[ -e atip_data ]]; then
        if [[ -n "$(ls -A atip_data 2>/dev/null)" ]]; then
            if [[ -f atip_data/atip.db || -f atip_data/config.json ]]; then
                confirm "This folder already has its own atip_data/. Replace it with the copy from $from? (the current one is kept aside)" ||
                    die "nothing copied — this folder's atip_data/ is in use. Answer y, or pass --yes, to replace it"
            fi
            local aside
            aside="atip_data.before-migrate-$(date +%Y%m%d-%H%M%S)"
            mv atip_data "$aside" || die "could not move the existing atip_data/ aside"
            ok "kept this folder's existing atip_data/ as $aside (git-ignored; delete it once happy)"
        else
            rmdir atip_data
        fi
    fi
    cp -Rp "$from/atip_data" "$ROOT/atip_data" || die "copy failed — $from is untouched"
    local what="" x
    for x in atip.db config.json publish.json secrets raw backups; do
        [[ -e "atip_data/$x" ]] && what="$what, $x"
    done
    ok "copied $from/atip_data → $ROOT/atip_data (${what#, })"
    printf '%s\t%s\n' "$from" "$(date '+%Y-%m-%d %H:%M:%S %z')" > atip_data/.migrated_from
    if [[ -f "$from/.env" ]]; then
        if [[ ! -e .env ]]; then
            cp -p "$from/.env" .env && ok "copied .env"
        elif cmp -s "$from/.env" .env; then
            ok ".env already identical"
        else
            warn "this folder already has a different .env — kept it; compare with $from/.env"
        fi
    else
        info "the old folder has no .env"
    fi

    step "Check the copy"
    if [[ -f atip_data/atip.db ]]; then
        local f qc
        for f in atip.db atip.db-wal; do
            [[ -f "$from/atip_data/$f" ]] || continue
            cmp -s "$from/atip_data/$f" "atip_data/$f" || die "$f differs from the original after copying"
        done
        qc="$(db_quick_check atip_data/atip.db)"
        [[ "$qc" == ok ]] || die "atip.db failed PRAGMA quick_check: $qc"
        local rows syms last
        read -r rows syms last <<<"$(db_summary atip_data/atip.db)"
        ok "atip.db identical to the original and passes quick_check ($rows daily prices, $syms symbols, last $last)"
    else
        warn "the old atip_data/ had no atip.db — install will create an empty one"
    fi
    info "not copied: .venv (a virtualenv cannot move; install builds a new one)"
    info "the old folder is untouched — keep it until this one has run a full trading day (it is the rollback)"
    local adir
    if adir="$(agent_dir "$PLATFORM_LABEL")" && [[ "$adir" != "$ROOT" ]]; then
        warn "the LaunchAgents still point at $adir: next run ./setup_atip.sh agents,"
        info "or at your next login the old copy starts again"
    fi
}

# ══ data ══════════════════════════════════════════════════════════════════
cmd_data() {
    require_venv
    dhan_configured || die "Dhan keys missing: add dhan_client_id and dhan_access_token to
      atip_data/config.json (open -e atip_data/config.json), then run this again"
    step "Dhan security list"
    "$VENV_PY" main.py --dhan-securities || die "security list download failed (atip_data/atip.log)"
    step "Dhan history: 600 days, about 5–10 minutes"
    "$VENV_PY" main.py --dhan-history || die "history download failed (atip_data/atip.log)"
    ok "first data loaded"
}

data_if_needed() {   # all: fetch only what is not there yet
    if [[ ! -f atip_data/raw/dhan/security_id_list.csv ]]; then
        step "Dhan security list"
        "$VENV_PY" main.py --dhan-securities || die "security list download failed (atip_data/atip.log)"
    fi
    local rows syms last
    read -r rows syms last <<<"$(db_summary atip_data/atip.db)"
    if [[ "$rows" == 0 ]]; then
        step "Dhan history: 600 days, about 5–10 minutes"
        "$VENV_PY" main.py --dhan-history || die "history download failed (atip_data/atip.log)"
    else
        ok "price history present ($rows rows, last $last) — skipping the 600-day download"
    fi
}

# ══ agents ════════════════════════════════════════════════════════════════
cmd_agents() {
    local target="${1:-all}"
    [[ $IS_MAC == 1 ]] || die "LaunchAgents are macOS-only; elsewhere use ./setup_atip.sh start"
    case "$target" in
        --uninstall|uninstall) deploy/launchd/install.sh --uninstall; return ;;
        snapshot) [[ -f atip_data/publish.json ]] || die "the snapshot agent needs atip_data/publish.json (publish_snapshot.py's docstring)" ;;
        all|platform|token) ;;
        *) die "usage: ./setup_atip.sh agents [all|platform|token|snapshot|--uninstall]" ;;
    esac
    require_venv   # without .venv launchd's bare PATH falls back to Apple's Python 3.9

    step "Other copies of ATIP"
    local pid dir others moved=0
    others="$(other_atip_pids)"
    if [[ -n "$others" ]]; then
        while IFS=$'\t' read -r pid dir; do
            if [[ "$dir" == "$(agent_dir "$PLATFORM_LABEL" 2>/dev/null)" ]]; then
                warn "pid $pid is the LaunchAgent copy in $dir — installing here replaces it"
                moved=1
            else
                die "ATIP (pid $pid) is running from $dir outside launchd. Stop it (Ctrl+C in its
      Terminal) first: two schedulers would both trade signals and send every alert."
            fi
        done <<<"$others"
        if [[ $moved == 1 && $YES != 1 ]]; then
            confirm "Move ATIP's LaunchAgents to $ROOT?" || die "nothing changed"
        fi
    fi
    if instance_running "$ROOT" && [[ "$(agent_dir "$PLATFORM_LABEL" 2>/dev/null)" != "$ROOT" ]]; then
        die "ATIP is running from this folder in a Terminal; stop it (Ctrl+C) first — the agent starts its own"
    fi
    ok "no conflicting copy on this Mac"
    info "if the old Windows machine still runs ATIP, switch it off first: .\\setup_atip.ps1 -UnregisterStartup"
    dhan_configured || warn "the Dhan keys are not set yet — ATIP will start but fetch no Indian market data"

    step "Install"
    case "$target" in
        all)
            if [[ -f atip_data/publish.json ]]; then
                deploy/launchd/install.sh all || die "deploy/launchd/install.sh failed"
            else
                deploy/launchd/install.sh platform || die "deploy/launchd/install.sh failed"
                deploy/launchd/install.sh token || die "deploy/launchd/install.sh failed"
                info "snapshot agent skipped: atip_data/publish.json does not exist"
                local sdir
                if sdir="$(agent_dir "$SNAPSHOT_LABEL")" && [[ "$sdir" != "$ROOT" ]]; then
                    warn "the snapshot agent still runs from $sdir — once publish.json is here:"
                    info "./setup_atip.sh agents snapshot   (or stop it: launchctl bootout gui/$UID/$SNAPSHOT_LABEL)"
                fi
            fi ;;
        *)  deploy/launchd/install.sh "$target" || die "deploy/launchd/install.sh failed" ;;
    esac

    if [[ "$target" == all || "$target" == platform ]]; then
        step "Waiting for the dashboard (up to 90 s)"
        local i code=000
        for i in $(seq 1 45); do
            # the lock too: an old copy still shutting down can answer on the port for a moment
            if instance_running "$ROOT"; then
                code="$(health /health/live)"
                [[ "$code" == 200 ]] && break
            fi
            sleep 2
        done
        if [[ "$code" == 200 ]]; then ok "ATIP is up from this folder: http://localhost:$PORT"
        else bad "the dashboard did not answer — ./setup_atip.sh verify shows why"; fi
    fi
}

# ══ start / stop ══════════════════════════════════════════════════════════
cmd_start() {
    require_venv
    instance_running "$ROOT" && die "ATIP is already running from this folder (the LaunchAgent?) — ./setup_atip.sh verify"
    local others
    others="$(other_atip_pids)"
    if [[ -n "$others" && $YES != 1 ]]; then
        die "ATIP is already running from $(cut -f2 <<<"$others" | head -n 1) (pid $(cut -f1 <<<"$others" | head -n 1)).
      Stop it first: two schedulers would both send every alert. (--yes starts anyway.)"
    fi
    TZ=Asia/Kolkata exec ./start_atip.sh "$@"
}

cmd_stop() {
    local adir
    adir="$(agent_dir "$PLATFORM_LABEL" 2>/dev/null)" || adir=""
    if agent_loaded "$PLATFORM_LABEL" && [[ -n "$adir" && "$adir" != "$ROOT" ]]; then
        warn "the LaunchAgent runs ATIP from $adir, not this folder — left running"
        info "stop it with: launchctl bootout gui/$UID/$PLATFORM_LABEL"
    elif agent_loaded "$PLATFORM_LABEL"; then
        launchctl bootout "gui/$UID/$PLATFORM_LABEL" 2>/dev/null || true
        if wait_stopped "$ROOT"; then ok "ATIP's LaunchAgent stopped"; else warn "still shutting down"; fi
        info "it starts again at the next login; ./setup_atip.sh agents --uninstall removes it for good"
        info "the token-refresh and snapshot agents keep running (./setup_atip.sh agents --uninstall stops all)"
    elif instance_running "$ROOT"; then
        warn "ATIP runs from a Terminal here — press Ctrl+C in that window"
    else
        ok "ATIP is not running from this folder"
    fi
}

# ══ verify ════════════════════════════════════════════════════════════════
cmd_verify() {
    PROBLEMS=0
    local code
    step "Process"
    if instance_running "$ROOT"; then ok "ATIP is running from $ROOT"
    else bad "ATIP is not running from this folder — ./setup_atip.sh start, or ./setup_atip.sh agents"; fi
    if [[ $IS_MAC == 1 ]]; then
        local label dir
        for label in $ALL_LABELS; do
            dir="$(agent_dir "$label" 2>/dev/null)" || { info "$label not installed"; continue; }
            if [[ "$dir" != "$ROOT" ]]; then bad "$label runs from $dir, not this folder"
            elif agent_loaded "$label"; then ok "$label loaded"
            else warn "$label installed but not loaded (log out and in, or ./setup_atip.sh agents)"; fi
        done
    fi
    local others
    others="$(other_atip_pids)"
    if [[ -n "$others" ]]; then
        while IFS=$'\t' read -r pid dir; do bad "a second ATIP (pid $pid) runs from $dir"; done <<<"$others"
    fi

    step "Dashboard"
    code="$(health /health/live)"
    [[ "$code" == 200 ]] && ok "http://localhost:$PORT/health/live → 200" || bad "http://localhost:$PORT/health/live → $code"
    code="$(health /health/ready)"
    case "$code" in
        200) ok "/health/ready → 200" ;;
        000) ;;
        *)   warn "/health/ready → $code (a part is degraded: curl -s http://localhost:$PORT/health | python3 -m json.tool)" ;;
    esac

    step "Logs"
    if [[ -f atip_data/launchd.err ]] && grep -q "Operation not permitted" atip_data/launchd.err; then
        bad "launchd.err: 'Operation not permitted' — macOS blocks the agent in ~/Documents"
        info "give /bin/bash Full Disk Access (docs/MAC_DEPLOYMENT.md, step 7), then ./setup_atip.sh agents platform"
    fi
    if [[ -f atip_data/atip.log ]]; then
        local today n
        today="$(TZ=Asia/Kolkata date +%Y-%m-%d)"
        n="$(grep -c "^$today .* ERROR " atip_data/atip.log 2>/dev/null || true)"
        info "atip.log: ${n:-0} ERROR line(s) today; last ones:"
        grep "^$today .* ERROR " atip_data/atip.log 2>/dev/null | tail -n 3 | cut -c1-160 | sed 's/^/        /'
    fi
    [[ -f atip_data/dhan_token_refresh.log ]] && info "token refresh: $(tail -n 1 atip_data/dhan_token_refresh.log | cut -c1-140)"
    [[ -f atip_data/publish.log ]] && info "snapshot:      $(tail -n 1 atip_data/publish.log | cut -c1-140)"
    info "today's jobs:  .venv/bin/python main.py --status"

    echo
    if [[ $PROBLEMS == 0 ]]; then echo "  Verified: ATIP runs from $ROOT."; return 0; fi
    echo "  $PROBLEMS problem(s) above (❌)."
    return 1
}

# ══ all ═══════════════════════════════════════════════════════════════════
cmd_all() {
    if [[ -n "$FROM" ]]; then
        local done_from=""
        [[ -f atip_data/.migrated_from ]] && done_from="$(cut -f1 atip_data/.migrated_from)"
        local want="$FROM"
        [[ "$want" == /* ]] || want="$CALLED_FROM/$want"
        want="$(cd "$want" 2>/dev/null && pwd)" || want="$FROM"
        [[ ! -f "$want/main.py" && -f "$want/../main.py" ]] && want="$(cd "$want/.." && pwd)"
        if [[ -n "$done_from" && "$done_from" == "$want" ]]; then
            step "Migration"
            ok "already migrated from $want ($(cut -f2 atip_data/.migrated_from)) — not copying again"
        else
            cmd_migrate
        fi
    fi
    cmd_install
    if ! dhan_configured; then
        step "Next"
        info "1. add dhan_client_id and dhan_access_token:  open -e atip_data/config.json"
        info "2. run ./setup_atip.sh all again — it skips everything already done"
        return 0
    fi
    data_if_needed
    if [[ $IS_MAC == 1 ]]; then
        cmd_agents all
        cmd_verify
    else
        step "Next"
        info "no launchd here — start ATIP with ./setup_atip.sh start"
    fi
}

usage() {
    sed -n '/^# Usage:/,/^# Written for/p' "$ROOT/setup_atip.sh" | sed '$d' | sed 's/^# \{0,1\}//'
}

# ══ main ══════════════════════════════════════════════════════════════════
CMD="${1:-help}"
[[ $# -gt 0 ]] && shift
ARGS=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y) YES=1 ;;
        --from)   [[ $# -ge 2 ]] || die "--from needs a folder"; FROM="$2"; shift ;;
        --from=*) FROM="${1#--from=}" ;;
        *)        ARGS="$ARGS $1" ;;
    esac
    shift
done

echo
echo "  ATIP — macOS setup  ($CMD)"

case "$CMD" in
    check)   cmd_check ;;
    install) cmd_install ;;
    migrate) cmd_migrate ;;
    data)    cmd_data ;;
    agents)  cmd_agents $ARGS ;;
    start)   cmd_start $ARGS ;;
    stop)    cmd_stop ;;
    verify)  cmd_verify ;;
    all)     cmd_all ;;
    help|-h|--help) usage ;;
    *)       usage >&2; exit 2 ;;
esac
