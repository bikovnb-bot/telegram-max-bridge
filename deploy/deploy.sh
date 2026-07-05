#!/usr/bin/env bash
# Деплой обновлённого кода моста на сервер одной командой.
#
# Usage:
#   ./deploy/deploy.sh user@server-ip [remote-dir]
#
# Пакует только код (bridge/, requirements.txt, deploy/, README.md) — .env и
# max_session/ на сервере не трогает. Ставит зависимости (если requirements.txt
# изменился) и перезапускает systemd-сервисы.

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 user@server-ip [remote-dir]" >&2
  exit 1
fi

TARGET="$1"
REMOTE_DIR="${2:-/opt/telegram-max-bridge}"
ARCHIVE="/tmp/bridge-deploy-$(date +%s).tar.gz"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

echo "==> Собираю архив с кодом (без .env и max_session)..."
tar --exclude='__pycache__' -czf "$ARCHIVE" bridge requirements.txt deploy README.md

echo "==> Копирую на сервер $TARGET..."
scp "$ARCHIVE" "$TARGET:/tmp/bridge-deploy.tar.gz"
rm -f "$ARCHIVE"

echo "==> Разворачиваю и перезапускаю на сервере..."
ssh "$TARGET" REMOTE_DIR="$REMOTE_DIR" bash -s << 'REMOTE_SCRIPT'
set -euo pipefail

sudo mkdir -p "$REMOTE_DIR"
sudo tar -xzf /tmp/bridge-deploy.tar.gz -C "$REMOTE_DIR"
sudo chown -R bridge:bridge "$REMOTE_DIR/bridge" "$REMOTE_DIR/requirements.txt" "$REMOTE_DIR/deploy" "$REMOTE_DIR/README.md"
rm -f /tmp/bridge-deploy.tar.gz

echo "--> Обновляю зависимости..."
sudo -u bridge "$REMOTE_DIR/.venv/bin/pip" install -q -r "$REMOTE_DIR/requirements.txt"

echo "--> Перезапускаю сервисы..."
sudo systemctl restart telegram-max-bridge
sudo systemctl restart telegram-max-bridge-web

sleep 2
sudo systemctl --no-pager status telegram-max-bridge | head -n 5
sudo systemctl --no-pager status telegram-max-bridge-web | head -n 5
REMOTE_SCRIPT

echo "==> Готово. Проверьте лог: ssh $TARGET 'sudo journalctl -u telegram-max-bridge -n 30 --no-pager'"
