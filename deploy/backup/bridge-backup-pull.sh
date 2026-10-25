#!/usr/bin/env bash
# Забирает зашифрованный бэкап моста с сервера моста и заливает его в S3.
# Запускается на СЕРВЕРЕ, который хранит ключи S3 (cron, раз в сутки), — так ключи
# хранилища не лежат на сервере моста и его взлом не даёт стереть бэкапы.
#
# Забор идёт по SSH ограниченным ключом: на сервере моста для него разрешена
# единственная команда — отдать последний архив (см. README, «Резервные копии»).
# При любой ошибке в MAX уходит уведомление через /forward моста.
#
# Настройка: /etc/default/bridge-backup-pull
#   BRIDGE_SSH_TARGET=bridgebackup@<ip-сервера-моста>        # обязательно
#   GPG_RECIPIENT=<отпечаток публичного ключа>               # обязательно
#   S3_ENV_FILE=/путь/к/.env     # файл с BEGET_S3_* (и BRIDGE_FORWARD_URL/TOKEN)
#   UPLOAD_SCRIPT=/opt/beget-s3-upload.py   ROTATE_SCRIPT=/opt/beget-s3-rotate.py
#   SSH_KEY, KNOWN_HOSTS, GPG_HOME, S3_PREFIX, KEEP_DAYS, WORK_DIR

set -euo pipefail

[ -f /etc/default/bridge-backup-pull ] && . /etc/default/bridge-backup-pull

: "${BRIDGE_SSH_TARGET:?BRIDGE_SSH_TARGET не задан}"
: "${GPG_RECIPIENT:?GPG_RECIPIENT не задан}"
: "${S3_ENV_FILE:?S3_ENV_FILE не задан}"
SSH_KEY="${SSH_KEY:-/root/.ssh/bridge_backup_pull}"
KNOWN_HOSTS="${KNOWN_HOSTS:-/root/.ssh/bridge_backup_known_hosts}"
GPG_HOME="${GPG_HOME:-/root/.bridge-backup-gnupg}"
S3_PREFIX="${S3_PREFIX:-telegram-max-bridge}"
KEEP_DAYS="${KEEP_DAYS:-14}"
WORK_DIR="${WORK_DIR:-/var/backups/bridge-pull}"
UPLOAD_SCRIPT="${UPLOAD_SCRIPT:-/opt/beget-s3-upload.py}"
ROTATE_SCRIPT="${ROTATE_SCRIPT:-/opt/beget-s3-rotate.py}"
CONFIG_FILES="${CONFIG_FILES:-/etc/systemd/system/bridge-tunnel.service /etc/nginx/sites-available/bridge-panel}"

set -a
# shellcheck disable=SC1090
. "$S3_ENV_FILE"
set +a

notify_failure() {
  local text="⚠️ Бэкап моста не удался: $1"
  if [ -n "${BRIDGE_FORWARD_URL:-}" ] && [ -n "${BRIDGE_FORWARD_TOKEN:-}" ]; then
    curl -s -m 15 -X POST "$BRIDGE_FORWARD_URL" \
      -H "Authorization: Bearer $BRIDGE_FORWARD_TOKEN" -H "Content-Type: application/json" \
      -d "{\"text\": \"$text\"}" >/dev/null || true
  fi
  logger -t bridge-backup-pull "$text"
}
OUT=""
trap 'rc=$?; [ -n "$OUT" ] && rm -f "$OUT.part"; [ "$rc" -ne 0 ] && notify_failure "сбой на строке $LINENO (код $rc), см. /var/log/bridge-backup.log"; exit "$rc"' EXIT

: "${BEGET_S3_ACCESS_KEY:?нет BEGET_S3_ACCESS_KEY в $S3_ENV_FILE}"
: "${BEGET_S3_SECRET_KEY:?нет BEGET_S3_SECRET_KEY в $S3_ENV_FILE}"
: "${BEGET_S3_BUCKET:?нет BEGET_S3_BUCKET в $S3_ENV_FILE}"

TS="$(date +%Y%m%d-%H%M%S)"
install -d -m 700 "$WORK_DIR"

OUT="$WORK_DIR/bridge-$TS.tar.gz.gpg"

# 1. Забираем архив с сервера моста и проверяем, что это именно зашифрованный файл
ssh -i "$SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$KNOWN_HOSTS" -o ConnectTimeout=20 \
    "$BRIDGE_SSH_TARGET" > "$OUT.part"
[ "$(stat -c %s "$OUT.part")" -ge 1024 ] || { echo "архив слишком мал" >&2; exit 1; }
PACKETS="$(gpg --list-packets "$OUT.part" 2>&1 || true)"
grep -q "pubkey enc packet" <<<"$PACKETS" \
  || { echo "полученный файл не похож на зашифрованный архив" >&2; exit 1; }
mv "$OUT.part" "$OUT"
chmod 600 "$OUT"

# 2. Конфиги этого сервера (туннель, прокси панели), тоже зашифрованно
CFG_TAR="$WORK_DIR/server-config-$TS.tar.gz"
# shellcheck disable=SC2086
tar -czf "$CFG_TAR" $CONFIG_FILES 2>/dev/null || true
CFG_OUT="$CFG_TAR.gpg"
gpg --homedir "$GPG_HOME" --batch --yes --trust-model always \
    --encrypt --recipient "$GPG_RECIPIENT" --output "$CFG_OUT" "$CFG_TAR"
rm -f "$CFG_TAR"
chmod 600 "$CFG_OUT"

# 3. Заливаем в S3 и чистим старые копии
python3 "$UPLOAD_SCRIPT" "$OUT" "$S3_PREFIX/bridge-server/$(basename "$OUT")"
python3 "$UPLOAD_SCRIPT" "$CFG_OUT" "$S3_PREFIX/server-config/$(basename "$CFG_OUT")"
python3 "$ROTATE_SCRIPT" "$S3_PREFIX/bridge-server/" "$KEEP_DAYS"
python3 "$ROTATE_SCRIPT" "$S3_PREFIX/server-config/" "$KEEP_DAYS"

# 4. Локально оставляем 3 последние копии
for pattern in 'bridge-*.tar.gz.gpg' 'server-config-*.tar.gz.gpg'; do
  ls -1t "$WORK_DIR"/$pattern 2>/dev/null | tail -n +4 | xargs -r rm -f
done

logger -t bridge-backup-pull "готово: $(basename "$OUT") -> s3://$BEGET_S3_BUCKET/$S3_PREFIX/"
echo "ok $(basename "$OUT")"
