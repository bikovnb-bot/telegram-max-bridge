#!/usr/bin/env bash
# Собирает зашифрованный архив важных файлов моста.
# Запускается на СЕРВЕРЕ МОСТА от root (таймер bridge-backup-make.timer).
#
# В архив попадает: .env, базы max_session.db и archive.db (консистентные копии
# через sqlite3 .backup), очередь вложений outbox/, юниты systemd, правило sudoers,
# authorized_keys туннеля, правила ufw. Код (репозиторий) и логи не сохраняются.
#
# Шифрование — публичным ключом GPG: на сервере моста есть только публичная
# часть, поэтому украденный архив (или доступ к хранилищу) прочитать нельзя.
# Закрытый ключ хранится на сервере, который забирает архивы, и у владельца.
#
# Настройка: /etc/default/bridge-backup (необязательно)
#   GPG_RECIPIENT=<отпечаток публичного ключа>   # обязательно
#   APP_DIR, OUT_DIR, GPG_HOME, READ_GROUP, KEEP_LOCAL

set -euo pipefail

[ -f /etc/default/bridge-backup ] && . /etc/default/bridge-backup

APP_DIR="${APP_DIR:-/opt/telegram-max-bridge}"
OUT_DIR="${OUT_DIR:-/var/backups/telegram-max-bridge}"
GPG_HOME="${GPG_HOME:-/root/.bridge-backup-gnupg}"
READ_GROUP="${READ_GROUP:-bridgebackup}"
KEEP_LOCAL="${KEEP_LOCAL:-3}"
: "${GPG_RECIPIENT:?GPG_RECIPIENT не задан (/etc/default/bridge-backup)}"

TS="$(date +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/bridge-backup"
mkdir -p "$STAGE/app" "$STAGE/system"

install -m 600 "$APP_DIR/.env" "$STAGE/app/.env"

for db in max_session.db archive.db; do
  if [ -f "$APP_DIR/max_session/$db" ]; then
    sqlite3 "$APP_DIR/max_session/$db" ".backup '$STAGE/app/$db'"
  fi
done

if [ -d "$APP_DIR/max_session/outbox" ]; then
  cp -a "$APP_DIR/max_session/outbox" "$STAGE/app/outbox"
fi

for f in \
  /etc/systemd/system/telegram-max-bridge.service \
  /etc/systemd/system/telegram-max-bridge-web.service \
  /etc/sudoers.d/bridge \
  /var/lib/tunnel/.ssh/authorized_keys \
  /etc/ufw/user.rules \
  /etc/ufw/user6.rules; do
  [ -f "$f" ] && cp --parents -p "$f" "$STAGE/system/"
done
ufw status verbose > "$STAGE/system/ufw-status.txt" 2>&1 || true

{
  echo "created: $TS"
  echo "host: $(hostname)"
  echo "app_dir: $APP_DIR"
  (cd "$APP_DIR" && git log --oneline -1 2>/dev/null) || true
} > "$STAGE/MANIFEST.txt"

tar -C "$WORK" -czf "$WORK/bridge.tar.gz" bridge-backup

install -d -m 750 -o root -g "$READ_GROUP" "$OUT_DIR"
OUT="$OUT_DIR/bridge-$TS.tar.gz.gpg"
gpg --homedir "$GPG_HOME" --batch --yes --trust-model always \
    --encrypt --recipient "$GPG_RECIPIENT" --output "$OUT" "$WORK/bridge.tar.gz"
chown root:"$READ_GROUP" "$OUT"
chmod 640 "$OUT"
ln -sfn "bridge-$TS.tar.gz.gpg" "$OUT_DIR/latest.tar.gz.gpg"

# локально держим только последние KEEP_LOCAL копий
ls -1t "$OUT_DIR"/bridge-*.tar.gz.gpg | tail -n +"$((KEEP_LOCAL + 1))" | xargs -r rm -f

logger -t bridge-backup "создан $OUT ($(stat -c %s "$OUT") байт)"
echo "ok $OUT"
