#!/bin/bash
# Установка «Дикой поездки» на чистый Ubuntu 22.04/24.04.
# Запуск: curl -fsSL https://raw.githubusercontent.com/frrorsk1-coder/wild-trip/main/server/install.sh | bash
set -euo pipefail
REPO_TAR="https://codeload.github.com/frrorsk1-coder/wild-trip/tar.gz/refs/heads/main"
DIR=/opt/wildtrip
ENV=/etc/wildtrip.env

echo "== 1/6 Пакеты системы"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -q
apt-get install -y -q python3 python3-venv curl ca-certificates caddy

echo "== 2/6 Код игры"
mkdir -p "$DIR"
curl -fsSL "$REPO_TAR" | tar xz -C "$DIR" --strip-components=1
id wildtrip >/dev/null 2>&1 || useradd --system --home "$DIR" --shell /usr/sbin/nologin wildtrip

echo "== 3/6 Python-окружение"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q "aiohttp>=3.9,<4" "aiogram>=3.13,<4"
chown -R wildtrip:wildtrip "$DIR"

echo "== 4/6 Адрес сервера и HTTPS"
IP=$(curl -4 -fsS https://api.ipify.org || hostname -I | awk '{print $1}')
DOMAIN="${IP//./-}.sslip.io"
if [ ! -f "$ENV" ]; then
  printf 'BOT_TOKEN=\nPUBLIC_URL=https://%s\nAPP_SHORT=Game\nPORT=8080\n' "$DOMAIN" > "$ENV"
else
  sed -i "s#^PUBLIC_URL=.*#PUBLIC_URL=https://$DOMAIN#" "$ENV"
fi
chmod 600 "$ENV"
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:8080
}
EOF
systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy

echo "== 5/6 Служба игры"
cat > /etc/systemd/system/wildtrip.service <<EOF
[Unit]
Description=Wild Trip game server and Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=wildtrip
WorkingDirectory=$DIR/server
EnvironmentFile=$ENV
ExecStart=$DIR/venv/bin/python app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable wildtrip >/dev/null 2>&1
systemctl restart wildtrip

echo "== 6/6 Команды обслуживания"
cat > /usr/local/bin/wildtrip-token <<'EOF'
#!/bin/bash
# Сохраняет токен бота и перезапускает игру. Токен не показывается на экране.
read -r -s -p "Вставьте токен бота и нажмите Enter: " T; echo
T="$(echo "$T" | tr -d '[:space:]')"
[ -z "$T" ] && { echo "Пусто, ничего не изменено."; exit 1; }
sed -i "s#^BOT_TOKEN=.*#BOT_TOKEN=$T#" /etc/wildtrip.env
systemctl restart wildtrip; sleep 6
journalctl -u wildtrip -n 30 --no-pager | grep -E "Бот @|ERROR|Unauthorized|Conflict|Traceback" || echo "Смотрите журнал: wildtrip-log"
EOF
cat > /usr/local/bin/wildtrip-update <<EOF
#!/bin/bash
# Скачивает свежую версию игры с GitHub и перезапускает её.
set -e
curl -fsSL "$REPO_TAR" | tar xz -C "$DIR" --strip-components=1
chown -R wildtrip:wildtrip "$DIR"
systemctl restart wildtrip && echo "Обновлено и перезапущено."
EOF
cat > /usr/local/bin/wildtrip-log <<'EOF'
#!/bin/bash
journalctl -u wildtrip -n 60 --no-pager
EOF
chmod +x /usr/local/bin/wildtrip-token /usr/local/bin/wildtrip-update /usr/local/bin/wildtrip-log

sleep 4
echo
if curl -fsS -m 20 "https://$DOMAIN/healthz" >/dev/null 2>&1; then
  echo "ГОТОВО. Игра работает: https://$DOMAIN/"
else
  echo "Сервер запущен. HTTPS-сертификат получается, через минуту проверьте: https://$DOMAIN/"
fi
echo "Следующий шаг: выполните команду  wildtrip-token  и вставьте токен бота."
