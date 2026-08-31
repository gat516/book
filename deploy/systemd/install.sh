#!/usr/bin/env bash
# Install the novel-engine user units so the workers survive crashes and reboots.
#
# The pipeline worker's crash reaper (§6.3) recovers a claim stranded by a worker that
# died -- but it runs *inside* the worker, so it cannot cover "no worker is running".
# That gap is a supervision gap, and this is the layer that closes it.
#
#   ./deploy/systemd/install.sh          # install + enable + start
#   systemctl --user status novel-engine.target
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SRC="$REPO/deploy/systemd"

if [[ ! -f "$REPO/.env" ]]; then
  echo "error: $REPO/.env not found -- every unit reads it via EnvironmentFile." >&2
  echo "       cp .env.example .env and fill it in first." >&2
  exit 1
fi

mkdir -p "$UNIT_DIR"
# Collect the real unit names as we go: `systemctl enable` takes no globs.
units=()
for unit in "$SRC"/novel-*.service "$SRC"/novel-engine.target; do
  name="$(basename "$unit")"
  sed "s|@REPO@|$REPO|g" "$unit" > "$UNIT_DIR/$name"
  units+=("$name")
  echo "installed $name"
done

systemctl --user daemon-reload
systemctl --user enable "${units[@]}"
systemctl --user restart novel-engine.target

# Without lingering, user units are killed at logout and never start at boot -- the
# reboot half of this fix simply would not work. This is the one step needing polkit,
# so it may prompt for a password.
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo no)" != "yes" ]]; then
  echo
  echo "Enabling lingering so the services start at boot and survive logout..."
  loginctl enable-linger "$USER" || {
    echo "warning: could not enable lingering. Services will still restart on crash," >&2
    echo "         but will NOT start at boot. Run: sudo loginctl enable-linger $USER" >&2
  }
fi

echo
systemctl --user --no-pager --plain list-units 'novel-*' || true
