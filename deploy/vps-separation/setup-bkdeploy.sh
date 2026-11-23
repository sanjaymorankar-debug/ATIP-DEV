#!/bin/bash
# setup-bkdeploy.sh -- give bkesari its own deploy login on gk-db-1 (VPS separation plan, 23 Nov 2026, step 2).
#
# Run as root on the server, from a folder holding this script, bk-ctl, sudoers-bkdeploy and units.bkesari, ONLY
# after the owner's yes in the server session. It changes no running service, no GoKesari or agtci file, no Caddy
# and no firewall. Safe to re-run (it keeps an existing allow-list and refreshes the rest).
#
#   sudo bash setup-bkdeploy.sh <bkdeploy.pub> [<wireguard subnet, default 10.66.0.0/24>]
#   sudo bash setup-bkdeploy.sh --check         # what is installed, read-only
#   sudo bash setup-bkdeploy.sh --remove        # take it all away again (the rollback)
#
# What it creates:
#   user bkdeploy (system user, bash, password locked), its authorized_keys: the one public key, usable only from
#     the WireGuard subnet, with `restrict` (no port / agent / X11 forwarding, no pty);
#   /usr/local/sbin/bk-ctl (root 0755), /etc/bk-ctl/units (root 0644), /etc/sudoers.d/bkesari-deploy (root 0440,
#     checked with visudo first), /var/log/bk-ctl.log (root 0640).
# sshd: if sshd_config has AllowUsers / AllowGroups, bkdeploy is NOT added by this script; it says so and stops
# before the sudo rule, so the owner decides that change separately.
set -euo pipefail
export TZ=Asia/Kolkata
HERE=$(cd "$(dirname "$0")" && pwd)
U=bkdeploy
SUDOERS=/etc/sudoers.d/bkesari-deploy
say() { printf '%s  %s\n' "$(date '+%H:%M:%S IST')" "$*"; }
die() { say "STOP: $*"; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root (sudo)"

check() {
  say "user:      $(id $U 2>/dev/null || echo 'none')"
  say "keys:      $( [ -f /home/$U/.ssh/authorized_keys ] && grep -cvE '^[[:space:]]*(#|$)' /home/$U/.ssh/authorized_keys || echo 0)"
  say "bk-ctl:    $(ls -l /usr/local/sbin/bk-ctl 2>/dev/null || echo 'not installed')"
  say "units:     $(tr '\n' ' ' < /etc/bk-ctl/units 2>/dev/null | sed 's/#[^ ]*//g' || echo 'none')"
  say "sudoers:   $(ls -l $SUDOERS 2>/dev/null || echo 'not installed')"
  if id $U >/dev/null 2>&1; then sudo -l -U $U 2>/dev/null | sed -n '/may run/,$p'; fi
}

case "${1:-}" in
  --check) check; exit 0 ;;
  --remove)
    say "removing the bkdeploy login (bkesari services keep running; nothing else changes)"
    rm -f "$SUDOERS"
    if id $U >/dev/null 2>&1; then pkill -u $U || true; userdel -r $U 2>/dev/null || userdel $U; fi
    rm -f /usr/local/sbin/bk-ctl
    say "kept for the record: /etc/bk-ctl/units, /var/log/bk-ctl.log"
    check; exit 0 ;;
  ""|-*) die "usage: setup-bkdeploy.sh <bkdeploy.pub> [<subnet>] | --check | --remove" ;;
esac

PUB=$1
NET=${2:-10.66.0.0/24}
for f in bk-ctl sudoers-bkdeploy units.bkesari; do [ -f "$HERE/$f" ] || die "missing $HERE/$f"; done
[ -f "$PUB" ] || die "no public key file $PUB"
[ "$(grep -cvE '^[[:space:]]*(#|$)' "$PUB")" = 1 ] || die "$PUB must hold exactly one key"
KEY=$(grep -vE '^[[:space:]]*(#|$)' "$PUB")
case "$KEY" in ssh-ed25519\ *|ecdsa-sha2-*\ *|ssh-rsa\ *) ;; *) die "$PUB is not an ssh public key" ;; esac
case "$KEY" in *PRIVATE*) die "that is a private key: give the .pub file" ;; esac
case "$NET" in *[!0-9./]*) die "subnet looks wrong: $NET" ;; esac
id bkesari >/dev/null 2>&1 || die "no app user bkesari on this server"

if sshd -T 2>/dev/null | grep -qiE '^(allowusers|allowgroups) '; then
  sshd -T 2>/dev/null | grep -iE '^(allowusers|allowgroups) ' | sed 's/^/  sshd: /'
  die "sshd limits who may log in; add $U there first (owner's decision), then re-run"
fi

say "1/5 user $U"
id $U >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/$U --shell /bin/bash $U
passwd -l $U >/dev/null
install -d -o $U -g "$(id -gn $U)" -m 700 /home/$U/.ssh

say "2/5 its key (from $NET only, restricted)"
tmp=$(mktemp)
printf 'from="%s",restrict %s\n' "$NET" "$KEY" > "$tmp"
install -o $U -g "$(id -gn $U)" -m 600 "$tmp" /home/$U/.ssh/authorized_keys
rm -f "$tmp"

say "3/5 /usr/local/sbin/bk-ctl and its allow-list"
install -o root -g root -m 755 "$HERE/bk-ctl" /usr/local/sbin/bk-ctl
install -d -o root -g root -m 755 /etc/bk-ctl
[ -f /etc/bk-ctl/units ] || install -o root -g root -m 644 "$HERE/units.bkesari" /etc/bk-ctl/units
touch /var/log/bk-ctl.log && chown root:root /var/log/bk-ctl.log && chmod 640 /var/log/bk-ctl.log
bad=$(grep -vE '^[[:space:]]*(#|$)' /etc/bk-ctl/units | grep -vE '^bkesari-[A-Za-z0-9@._-]+\.(service|timer)$' || true)
[ -z "$bad" ] || die "/etc/bk-ctl/units lists non-bkesari units: $bad"

say "4/5 sudo rule (checked with visudo before it is installed)"
visudo -cf "$HERE/sudoers-bkdeploy" >/dev/null || die "visudo rejects sudoers-bkdeploy"
install -o root -g root -m 440 "$HERE/sudoers-bkdeploy" "$SUDOERS"
visudo -c >/dev/null || { rm -f "$SUDOERS"; die "the full sudo config failed its check: rule removed again"; }

say "5/5 done"
check
