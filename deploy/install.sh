#!/usr/bin/env bash
# Nova one-line installer for a Debian/Ubuntu VPS. Safe to run again (that is how you update).
#
#   curl -fsSL https://raw.githubusercontent.com/ErlanggaDevMode/Nova/<branch>/deploy/install.sh | bash
#
# Installs Nova to /opt/nova, creates a random admin password and JWT secret in
# /etc/nova/nova.env (root-only), runs Nova as the systemd service "nova" and adds
# the `nova-ctl` helper. Prints the URL and password at the end.
#
# Optional env vars: NOVA_BRANCH, NOVA_REPO, NOVA_DIR, NOVA_PORT
set -euo pipefail

REPO="${NOVA_REPO:-https://github.com/ErlanggaDevMode/Nova.git}"
BRANCH="${NOVA_BRANCH:-claude/tender-volta-vnq8pd}"
DIR="${NOVA_DIR:-/opt/nova}"
PORT="${NOVA_PORT:-8765}"
ENV_FILE=/etc/nova/nova.env

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (e.g. prefix with sudo)"
command -v systemctl >/dev/null || die "this installer needs systemd"

say "Installing system packages"
if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq git python3 python3-venv python3-pip openssl curl >/dev/null
else
    for c in git python3 openssl; do command -v "$c" >/dev/null || die "please install $c first"; done
fi

say "Fetching Nova ($BRANCH)"
if [ -d "$DIR/.git" ]; then
    git -C "$DIR" fetch -q origin "$BRANCH"
    git -C "$DIR" checkout -q -B "$BRANCH" FETCH_HEAD
else
    git clone -q -b "$BRANCH" "$REPO" "$DIR"
fi

say "Installing Python dependencies (takes a minute the first time)"
[ -x "$DIR/.venv/bin/python" ] || python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -e "$DIR"

NEW_PASSWORD=""
if [ ! -f "$ENV_FILE" ]; then
    say "Creating admin password and JWT secret"
    install -d -m 700 /etc/nova
    NEW_PASSWORD="$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 16)"
    umask 077
    cat > "$ENV_FILE" <<EOF
ADMIN_PASSWORD=$NEW_PASSWORD
JWT_SECRET_KEY=$(openssl rand -hex 32)
NOVA_PORT=$PORT
NOVA_LOG_LEVEL=WARNING
EOF
    chmod 600 "$ENV_FILE"
fi
PORT="$(grep -E '^NOVA_PORT=' "$ENV_FILE" | cut -d= -f2)"

say "Setting up the nova service"
cat > /etc/systemd/system/nova.service <<EOF
[Unit]
Description=Nova Core Server
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
EnvironmentFile=$ENV_FILE
ExecStart=$DIR/.venv/bin/uvicorn nova_core.main:app --host 0.0.0.0 --port \${NOVA_PORT} --log-level warning
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

install -m 755 "$DIR/deploy/nova-ctl" /usr/local/bin/nova-ctl

if ss -ltnp 2>/dev/null | grep -q ":$PORT " && ! systemctl is-active -q nova; then
    die "port $PORT is already in use (is Nova still running in another terminal? stop it with Ctrl+C) and run this again"
fi

systemctl daemon-reload
systemctl enable -q nova
systemctl restart nova

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    say "Opening port $PORT in ufw"
    ufw allow "$PORT/tcp" >/dev/null
fi

say "Waiting for Nova to start"
ok=""
for _ in $(seq 1 30); do
    if curl -fs -o /dev/null "http://127.0.0.1:$PORT/"; then ok=1; break; fi
    sleep 1
done
[ -n "$ok" ] || { journalctl -u nova -n 30 --no-pager; die "Nova did not start, log above"; }

IP="$(hostname -I | awk '{print $1}')"
echo
printf '\033[1;32mNova is running.\033[0m\n'
case "$IP" in
    10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*)
        echo "  Open:      http://<your VPS public IP>:$PORT"
        echo "             (this server only sees a private IP, $IP: on a NAT VPS use a port your"
        echo "              provider forwards, e.g. re-run with NOVA_PORT=<forwarded port>)"
        ;;
    *) echo "  Open:      http://$IP:$PORT" ;;
esac
if [ -n "$NEW_PASSWORD" ]; then
    echo "  Password:  $NEW_PASSWORD   (show it again with: nova-ctl password)"
else
    echo "  Password:  unchanged (show it with: nova-ctl password)"
fi
echo "  Manage:    nova-ctl status | logs | restart | update | password"
