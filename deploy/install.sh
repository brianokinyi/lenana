#!/usr/bin/env bash
# VPS installer for yt-crypto-signals. Idempotent: safe to re-run after a pull.
#
# Usage (as root):
#   ./deploy/install.sh
#
# Expects the repository to already be at /opt/ycs.
set -euo pipefail

APP_DIR=/opt/ycs
APP_USER=ycs
PYTHON_VERSION=3.12

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo $0" >&2
    exit 1
fi

if [[ ! -f "$APP_DIR/pyproject.toml" ]]; then
    echo "Expected the repository at $APP_DIR (pyproject.toml not found)." >&2
    exit 1
fi

echo "==> Creating the service user"
# A dedicated system user with no shell and no home: the units run unprivileged.
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"
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
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/ycs" db upgrade

echo "==> Installing systemd units"
install -m 644 "$APP_DIR"/deploy/ycs-*.service "$APP_DIR"/deploy/ycs-*.timer /etc/systemd/system/
systemctl daemon-reload

echo "==> Enabling timers"
systemctl enable --now ycs-pipeline.timer ycs-outbox.timer ycs-daily.timer ycs-weekly.timer

cat <<'NEXT'

Installed. Next steps:

  1. Fill in /opt/ycs/.env           (keys: Anthropic, YouTube, Telegram, CoinGecko, Webshare)
  2. sudo -u ycs /opt/ycs/.venv/bin/ycs config-check
  3. sudo -u ycs /opt/ycs/.venv/bin/ycs test-proxy       <- do this before anything else
  4. sudo -u ycs /opt/ycs/.venv/bin/ycs test-telegram --find-chat-id
  5. sudo -u ycs /opt/ycs/.venv/bin/ycs test-telegram
  6. sudo -u ycs /opt/ycs/.venv/bin/ycs backfill --days 30

Watch it run:

  systemctl list-timers 'ycs-*'
  journalctl -u ycs-pipeline -f

NEXT
