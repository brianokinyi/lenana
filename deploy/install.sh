#!/usr/bin/env bash
# VPS installer for lenana. Idempotent: safe to re-run after a pull.
#
# Usage (as root):
#   ./deploy/install.sh
#
# Expects the repository to already be at /opt/lenana.
set -euo pipefail

APP_DIR=/opt/lenana
APP_USER=lenana
PYTHON_VERSION=3.12

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo $0" >&2
    exit 1
fi

if [[ ! -f "$APP_DIR/pyproject.toml" ]]; then
    echo "Expected the repository at $APP_DIR (pyproject.toml not found)." >&2
    exit 1
fi

echo "==> Stopping Lenana during installation"
for unit in lenana-pipeline.timer lenana-pipeline.service lenana-dashboard.service \
    lenana-outbox.timer lenana-outbox.service lenana-daily.timer lenana-daily.service \
    lenana-weekly.timer lenana-weekly.service; do
    if systemctl is-active --quiet "$unit"; then
        systemctl stop "$unit"
    fi
done

echo "==> Creating the service user"
# A dedicated system user; its writable application home stays under data/.
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir "$APP_DIR/data" --shell /usr/sbin/nologin "$APP_USER"
else
    usermod --home "$APP_DIR/data" "$APP_USER"
fi

echo "==> Installing uv"
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi

echo "==> Building the virtualenv"
cd "$APP_DIR"
uv venv --python "$PYTHON_VERSION" .venv
# --frozen installs exactly what uv.lock pins, so a VPS build matches the
# versions the code was tested against.
uv sync --frozen --no-dev 2>/dev/null || uv pip install -e .

echo "==> Preparing the data directory"
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$APP_DIR/data" "$APP_DIR/data/backups"

if [[ ! -f "$APP_DIR/.env" ]]; then
    install -o "$APP_USER" -g "$APP_USER" -m 600 "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo "    Created .env from the example — fill in your keys before starting."
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR/data"
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

echo "==> Applying database migrations"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/lenana" db upgrade

echo "==> Installing systemd units"
# Retire the separate scheduled jobs when upgrading an existing installation.
for job in outbox daily weekly; do
    for kind in timer service; do
        unit="lenana-$job.$kind"
        if [[ -f "/etc/systemd/system/$unit" ]]; then
            systemctl disable --now "$unit"
            rm -f "/etc/systemd/system/$unit"
        fi
    done
done
install -m 644 "$APP_DIR"/deploy/lenana-*.service "$APP_DIR"/deploy/lenana-*.timer /etc/systemd/system/
systemctl daemon-reload

echo "==> Enabling pipeline timer and dashboard"
systemctl enable --now lenana-pipeline.timer lenana-dashboard.service

cat <<'NEXT'

Installed. Next steps:

  1. Fill in /opt/lenana/.env           (keys: Anthropic, YouTube, Telegram, CoinGecko, Webshare)
  2. sudo -u lenana /opt/lenana/.venv/bin/lenana config-check
  3. sudo -u lenana /opt/lenana/.venv/bin/lenana test-proxy       <- do this before anything else
  4. sudo -u lenana /opt/lenana/.venv/bin/lenana test-telegram --find-chat-id
  5. sudo -u lenana /opt/lenana/.venv/bin/lenana test-telegram
  6. sudo -u lenana /opt/lenana/.venv/bin/lenana backfill --days 30

Watch it run:

  systemctl list-timers 'lenana-*'
  journalctl -u lenana-pipeline -f

NEXT
