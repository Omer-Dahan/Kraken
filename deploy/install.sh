#!/usr/bin/env bash
#
# Sets up Kraken as a self-contained systemd service.
#
# Run as root on the server. Invoked through bash so it works straight off a checkout
# made on Windows, where the executable bit does not survive:
#
#     bash /opt/Kraken/deploy/install.sh
#
# Idempotent - safe to re-run after every source sync. It never touches .env or the
# session/ directory once they exist, so re-running will not cost you an authorized
# Telethon session (re-authorizing needs an interactive terminal).

set -euo pipefail

SERVICE_USER="${SERVICE_USER:-vm}"
SERVICE_GROUP="${SERVICE_GROUP:-$SERVICE_USER}"
# The group shared with every other process that writes into the library (qBittorrent,
# Jellyfin). Defaults to the service's own group, which is correct on a single-user box;
# set MEDIA_GROUP=media and add the other services to it when they run as their own users.
MEDIA_GROUP="${MEDIA_GROUP:-$SERVICE_GROUP}"
SERVICE_NAME="kraken.service"
LEGACY_SERVICE="telegram-bot.service"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { echo "error: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

[[ $EUID -eq 0 ]] || die "run as root (systemd unit installation needs it)"
id "$SERVICE_USER" &>/dev/null || die "user '$SERVICE_USER' does not exist - set SERVICE_USER=... and re-run"
getent group "$MEDIA_GROUP" &>/dev/null || die "group '$MEDIA_GROUP' does not exist - create it (groupadd $MEDIA_GROUP) or set MEDIA_GROUP=... and re-run"
# Without membership the setgid bit below would lock the service out of the library it owns.
if ! id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qx "$MEDIA_GROUP"; then
    step "Adding $SERVICE_USER to the '$MEDIA_GROUP' group"
    usermod -aG "$MEDIA_GROUP" "$SERVICE_USER"
    echo "note: the new group membership takes effect when $SERVICE_NAME restarts below"
fi
for required in requirements.txt .env.example deploy/kraken.service src/telegram_bot.py; do
    [[ -e "$PROJECT_DIR/$required" ]] || die "missing $required in $PROJECT_DIR - is the source synced?"
done

step "Project directory: $PROJECT_DIR (service runs as $SERVICE_USER:$SERVICE_GROUP, media group '$MEDIA_GROUP')"

if ! dpkg -s python3-venv &>/dev/null; then
    step "Installing python3-venv"
    apt-get update -qq
    apt-get install -y --no-install-recommends python3-venv >/dev/null
fi

step "Building the virtual environment"
# Recreated from scratch on a Python major upgrade, when the venv's symlinked
# interpreter no longer matches the system one and every import breaks.
if [[ -x "$PROJECT_DIR/.venv/bin/python" ]] \
   && ! "$PROJECT_DIR/.venv/bin/python" -c '' 2>/dev/null; then
    echo "existing venv is broken (stale interpreter) - rebuilding"
    rm -rf "$PROJECT_DIR/.venv"
fi
[[ -d "$PROJECT_DIR/.venv" ]] || python3 -m venv "$PROJECT_DIR/.venv"

"$PROJECT_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"
echo "installed: $("$PROJECT_DIR/.venv/bin/python" --version)"

step "Preparing runtime directories"
mkdir -p "$PROJECT_DIR/session"

MEDIA_ROOT="/media"
STAGING_DIR="/media/.incoming"
if [[ -f "$PROJECT_DIR/.env" ]]; then
    ENV_MR="$(grep -E '^MEDIA_ROOT=' "$PROJECT_DIR/.env" | cut -d'=' -f2- | tr -d '"' | tr -d "'" || true)"
    ENV_SD="$(grep -E '^STAGING_DIR=' "$PROJECT_DIR/.env" | cut -d'=' -f2- | tr -d '"' | tr -d "'" || true)"
    [[ -n "$ENV_MR" ]] && MEDIA_ROOT="$ENV_MR"
    [[ -n "$ENV_SD" ]] && STAGING_DIR="$ENV_SD"
fi

mkdir -p "$MEDIA_ROOT" "$STAGING_DIR" "$MEDIA_ROOT/movies" "$MEDIA_ROOT/tv"

# Migrate anything left over from the old flat /opt layout. Moved, not copied, so the
# next run does not keep resurrecting a stale session file from a path nothing reads.
if [[ -d /opt/session && "$PROJECT_DIR" != "/opt" ]]; then
    shopt -s nullglob
    legacy=(/opt/session/*.session /opt/session/*.session-journal)
    if (( ${#legacy[@]} )); then
        step "Migrating ${#legacy[@]} session file(s) from the old /opt/session"
        mv -v "${legacy[@]}" "$PROJECT_DIR/session/"
    fi
    shopt -u nullglob
fi
if [[ -f /opt/.env && ! -f "$PROJECT_DIR/.env" ]]; then
    step "Migrating /opt/.env"
    mv -v /opt/.env "$PROJECT_DIR/.env"
fi

env_is_template=0
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    env_is_template=1
    echo "created .env from .env.example - it still holds placeholder values"
fi

step "Setting ownership and permissions"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$PROJECT_DIR"
chmod 600 "$PROJECT_DIR/.env"
chmod 700 "$PROJECT_DIR/session"

# The library dirs are shared with whatever writes into them - qBittorrent finishes a
# torrent as its own user, and the bot then has to be able to move and delete that file.
# Ownership alone cannot arrange that (only an owner or root may chmod), so the shared
# footing is the GROUP: everything below is group-owned by $MEDIA_GROUP and group-writable,
# with setgid so files created later inherit the group instead of the creator's primary one.
#
# $MEDIA_ROOT itself is deliberately only chmod'd, never chown -R'd: it defaults to /media,
# a standard mount point, and recursing ownership over it would rewrite any unrelated disk
# mounted underneath. Only the directories Kraken actually owns are recursed into.
chown -R "$SERVICE_USER:$MEDIA_GROUP" "$STAGING_DIR" 2>/dev/null || true
chmod 775 "$MEDIA_ROOT" 2>/dev/null || true
chmod -R 2775 "$STAGING_DIR" 2>/dev/null || true
for library in movies tv; do
    [[ -d "$MEDIA_ROOT/$library" ]] || continue
    chgrp -R "$MEDIA_GROUP" "$MEDIA_ROOT/$library" 2>/dev/null || true
    # 2775 on directories, 664 on files: media is read by Jellyfin, never executed, and
    # -R alone would hand the execute bit to every .mkv it touched.
    find "$MEDIA_ROOT/$library" -type d -exec chmod 2775 {} + 2>/dev/null || true
    find "$MEDIA_ROOT/$library" -type f -exec chmod 664 {} + 2>/dev/null || true
done

# Files already on disk from before this ran keep their old owner, and a foreign owner is
# exactly what the bot cannot chmod its way out of when deleting. Report them rather than
# forcing a chown: taking ownership of another service's files is not the installer's call.
foreign=0
for library in movies tv; do
    [[ -d "$MEDIA_ROOT/$library" ]] || continue
    count="$(find "$MEDIA_ROOT/$library" ! -group "$MEDIA_GROUP" -printf . 2>/dev/null | wc -c)" || count=0
    foreign=$(( foreign + count ))
done
if (( foreign > 0 )); then
    echo "warning: $foreign item(s) under $MEDIA_ROOT are not in the '$MEDIA_GROUP' group"
    echo "         and could not be regrouped - the bot may fail to delete them."
    echo "         fix with: sudo chgrp -R $MEDIA_GROUP $MEDIA_ROOT/movies $MEDIA_ROOT/tv"
fi

step "Installing $SERVICE_NAME"
sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__SERVICE_USER__|$SERVICE_USER|g" \
    -e "s|__SERVICE_GROUP__|$SERVICE_GROUP|g" \
    "$PROJECT_DIR/deploy/kraken.service" > "/etc/systemd/system/$SERVICE_NAME"

if systemctl list-unit-files --no-legend "$LEGACY_SERVICE" | grep -q .; then
    step "Retiring $LEGACY_SERVICE (replaced by $SERVICE_NAME)"
    systemctl disable --now "$LEGACY_SERVICE" || true
    rm -f "/etc/systemd/system/$LEGACY_SERVICE"
fi

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null

# Only the last step differs between a first install and a re-deploy: a fresh install
# has placeholder credentials and would just crash-loop, so it is left stopped.
if (( env_is_template )); then
    cat <<EOF

Installed, but NOT started - $PROJECT_DIR/.env still has placeholder values.

  1. nano $PROJECT_DIR/.env          # API_ID, API_HASH, BOT_TOKEN, ALLOWED_USER_IDS
  2. sudo -u $SERVICE_USER $PROJECT_DIR/.venv/bin/python $PROJECT_DIR/src/generate_session.py
     (optional - only for the Userbot's large-file downloads; needs a real terminal)
  3. systemctl start $SERVICE_NAME
EOF
else
    step "Restarting $SERVICE_NAME"
    systemctl restart "$SERVICE_NAME"
    sleep 2
    systemctl is-active --quiet "$SERVICE_NAME" \
        && echo "$SERVICE_NAME is running" \
        || die "$SERVICE_NAME failed to start - check: journalctl -u $SERVICE_NAME -n 50 --no-pager"
fi
