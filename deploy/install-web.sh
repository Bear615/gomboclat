#!/usr/bin/env bash
# One-shot secure deployment of the web control hub behind nginx + Let's Encrypt.
#
#   sudo ./deploy/install-web.sh                          # deploy dcgsl.duckdns.org
#   sudo ./deploy/install-web.sh --email you@example.com  # cert expiry notices
#   sudo ./deploy/install-web.sh --domain other.duckdns.org --port 9000
#   sudo ./deploy/install-web.sh --duckdns-token TOKEN    # keep the DNS record fresh
#   sudo ./deploy/install-web.sh --reset-password         # change the admin password
#
# What it does (idempotent — safe to re-run any time):
#   1. installs nginx + certbot (apt or dnf)
#   2. installs the app venv + deps, ensures .env, prompts for the admin password
#   3. writes an nginx vhost: HTTP serves only the ACME challenge + redirects to HTTPS
#   4. obtains a Let's Encrypt certificate (webroot) + a renew-time nginx reload hook
#   5. hardens the vhost: TLS 1.2/1.3 only, HSTS, security headers, /login rate limit
#   6. installs + starts a hardened systemd service running `run.py --web` on localhost
#   7. opens ports 80/443 in ufw / firewalld when one is active
#   8. optionally installs a DuckDNS IP-updater timer (--duckdns-token)
#
# Requirements: the domain's DNS must point at this machine's public IP, and
# ports 80 + 443 must be reachable from the internet (router port-forwarding).

set -euo pipefail

DOMAIN="dcgsl.duckdns.org"
PORT="8134"
EMAIL=""
DUCKDNS_TOKEN=""
RESET_PASSWORD=0
SERVICE="gomboclat-web"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn:\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m error:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --domain)        DOMAIN="$2"; shift 2 ;;
    --port)          PORT="$2"; shift 2 ;;
    --email)         EMAIL="$2"; shift 2 ;;
    --duckdns-token) DUCKDNS_TOKEN="$2"; shift 2 ;;
    --reset-password) RESET_PASSWORD=1; shift ;;
    -h|--help)       grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

[ "$(id -u)" = "0" ] || die "run me with sudo: sudo ./deploy/install-web.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_USER="${SUDO_USER:-root}"
VENV_PY="$APP_DIR/.venv/bin/python"

info "Deploying web hub for https://$DOMAIN (app: $APP_DIR, user: $RUN_USER)"

# --- 1. System packages -----------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
  info "Installing nginx + certbot (apt)"
  DEBIAN_FRONTEND=noninteractive apt-get update -y -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx certbot curl
elif command -v dnf >/dev/null 2>&1; then
  info "Installing nginx + certbot (dnf)"
  dnf install -y -q nginx certbot curl
else
  die "no apt-get or dnf found — install nginx and certbot manually, then re-run."
fi
systemctl enable --now nginx >/dev/null 2>&1 || true

# --- 2. App install + admin password ---------------------------------------
info "Installing the app (venv + dependencies)"
sudo -u "$RUN_USER" bash "$APP_DIR/setup.sh" --install

info "Pinning web settings in .env (localhost:$PORT behind nginx)"
sudo -u "$RUN_USER" bash -c "cd '$APP_DIR' && '$VENV_PY' -c \"
from bot.config import update_env_file
update_env_file({'WEB_HOST': '127.0.0.1', 'WEB_PORT': '$PORT', 'WEB_DOMAIN': '$DOMAIN'})
\""

if [ "$RESET_PASSWORD" = "1" ] || ! grep -qE '^WEB_PASSWORD_HASH=.+' "$APP_DIR/.env"; then
  info "Choose the web UI admin password (stored only as an scrypt hash)"
  sudo -u "$RUN_USER" bash -c "cd '$APP_DIR' && '$VENV_PY' run.py --set-web-password"
else
  info "Admin password already set (use --reset-password to change it)"
fi

# --- 3. Optional DuckDNS updater -------------------------------------------
DUCKDNS_SUB="${DOMAIN%%.duckdns.org}"
if [ -n "$DUCKDNS_TOKEN" ]; then
  info "Installing DuckDNS IP updater for '$DUCKDNS_SUB' (every 5 minutes)"
  ENV_FILE="/etc/default/${SERVICE}-duckdns"
  printf 'DUCKDNS_SUB=%s\nDUCKDNS_TOKEN=%s\n' "$DUCKDNS_SUB" "$DUCKDNS_TOKEN" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  cat > "/etc/systemd/system/${SERVICE}-duckdns.service" <<EOF
[Unit]
Description=DuckDNS IP update for $DOMAIN
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/curl -fsS "https://www.duckdns.org/update?domains=\${DUCKDNS_SUB}&token=\${DUCKDNS_TOKEN}&ip="
EOF
  cat > "/etc/systemd/system/${SERVICE}-duckdns.timer" <<EOF
[Unit]
Description=Periodic DuckDNS IP update for $DOMAIN

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now "${SERVICE}-duckdns.timer"
  systemctl start "${SERVICE}-duckdns.service" || warn "DuckDNS update failed — check the token."
fi

if ! getent hosts "$DOMAIN" >/dev/null 2>&1; then
  warn "$DOMAIN does not resolve yet — certbot will fail until DNS points here."
fi

# --- 4. nginx phase 1: ACME challenge + redirect ----------------------------
if [ -d /etc/nginx/sites-available ]; then
  SITE_FILE="/etc/nginx/sites-available/${SERVICE}.conf"
  ln -sf "$SITE_FILE" "/etc/nginx/sites-enabled/${SERVICE}.conf"
else
  SITE_FILE="/etc/nginx/conf.d/${SERVICE}.conf"
fi
RATE_FILE="/etc/nginx/conf.d/${SERVICE}-ratelimit.conf"
ACME_ROOT="/var/www/letsencrypt"
mkdir -p "$ACME_ROOT"

# The login rate-limit zone must live in the http{} context.
cat > "$RATE_FILE" <<'EOF'
# Brute-force protection for the gomboclat web hub login (10 attempts/minute).
limit_req_zone $binary_remote_addr zone=gomboclat_login:10m rate=10r/m;
EOF

write_http_block() {
  cat <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    # Let's Encrypt HTTP-01 challenges.
    location ^~ /.well-known/acme-challenge/ {
        root $ACME_ROOT;
        default_type "text/plain";
    }

    # Everything else goes to HTTPS.
    location / {
        return 301 https://\$host\$request_uri;
    }
}
EOF
}

CERT_DIR="/etc/letsencrypt/live/$DOMAIN"
if [ ! -e "$CERT_DIR/fullchain.pem" ]; then
  info "Writing bootstrap nginx config (HTTP only, for the ACME challenge)"
  write_http_block > "$SITE_FILE"
  nginx -t
  systemctl reload nginx

  info "Requesting a Let's Encrypt certificate for $DOMAIN"
  CERTBOT_ARGS=(certonly --webroot -w "$ACME_ROOT" -d "$DOMAIN" --non-interactive --agree-tos)
  if [ -n "$EMAIL" ]; then
    CERTBOT_ARGS+=(-m "$EMAIL")
  else
    CERTBOT_ARGS+=(--register-unsafely-without-email)
    warn "No --email given — you won't get certificate expiry notices."
  fi
  if ! certbot "${CERTBOT_ARGS[@]}"; then
    die "certbot failed. Check that $DOMAIN resolves to this machine's public IP and
       that ports 80/443 are forwarded here, then re-run this script."
  fi
else
  info "Certificate for $DOMAIN already exists — skipping issuance"
fi

# Reload nginx whenever certbot renews (webroot renewals don't do it themselves).
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh <<'EOF'
#!/bin/sh
systemctl reload nginx
EOF
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

# --- 5. nginx phase 2: full hardened vhost ----------------------------------
info "Writing hardened HTTPS vhost"
{
  write_http_block
  cat <<EOF

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name $DOMAIN;

    ssl_certificate     $CERT_DIR/fullchain.pem;
    ssl_certificate_key $CERT_DIR/privkey.pem;

    # Mozilla "intermediate" TLS: 1.2/1.3 only, strong AEAD ciphers.
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:gomboclat_ssl:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;

    # The hub only ever receives small JSON/form bodies.
    client_max_body_size 64k;

    # Extra brute-force protection in front of the app's own login throttle.
    location = /login {
        limit_req zone=gomboclat_login burst=5 nodelay;
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
    }
}
EOF
} > "$SITE_FILE"

nginx -t
systemctl reload nginx

# --- 6. systemd service for the hub -----------------------------------------
info "Installing systemd service: $SERVICE"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=AI-Moderator web control hub (gomboclat)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV_PY $APP_DIR/run.py --web
Restart=on-failure
RestartSec=5

# Hardening (the app only needs to read/write its own directory).
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

info "Waiting for the hub to come up on 127.0.0.1:$PORT"
ok=0
for _ in $(seq 1 15); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
if [ "$ok" = "1" ]; then
  info "Hub is up."
else
  warn "Hub did not answer yet — check: journalctl -u $SERVICE -n 50"
fi

# --- 7. Firewall -------------------------------------------------------------
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  info "Opening ports 80/443 in ufw"
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  info "Opening http/https in firewalld"
  firewall-cmd --permanent --add-service=http --add-service=https >/dev/null
  firewall-cmd --reload >/dev/null
fi

# --- 8. Summary --------------------------------------------------------------
cat <<EOF

$(printf '\033[1;32m')✔ Done.$(printf '\033[0m')

  Web UI      : https://$DOMAIN
  App service : systemctl status $SERVICE   ·   journalctl -u $SERVICE -f
  nginx vhost : $SITE_FILE
  Certificate : $CERT_DIR (auto-renews via certbot's timer + nginx reload hook)
  Password    : sudo ./deploy/install-web.sh --reset-password
$( [ -n "$DUCKDNS_TOKEN" ] && echo "  DuckDNS     : systemctl list-timers ${SERVICE}-duckdns.timer" )

The hub itself listens only on 127.0.0.1:$PORT — the internet reaches it
exclusively through nginx over TLS.
EOF
