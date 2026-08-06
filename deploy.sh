#!/bin/bash
# ============================================================
# Deploy Telegram Blaster ke VPS (Ubuntu/Debian)
# Jalankan sebagai root: bash deploy.sh
# ============================================================

set -e

APP_DIR="/var/www/telegram_blaster"
DOMAIN="miawjugabisa.com"

echo "=== 1. Update & install dependencies ==="
apt update -y
apt install -y python3 python3-pip python3-venv nginx certbot python3-certbot-nginx

echo "=== 2. Buat folder app ==="
mkdir -p $APP_DIR
cp -r ./* $APP_DIR/
cd $APP_DIR

echo "=== 3. Setup virtual environment ==="
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "=== 4. Setup Nginx ==="
cp nginx_miawjugabisa.conf /etc/nginx/sites-available/$DOMAIN
ln -sf /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/$DOMAIN
nginx -t && systemctl reload nginx

echo "=== 5. Setup systemd service ==="
cp telegram_blaster.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable telegram_blaster
systemctl start telegram_blaster

echo "=== 6. SSL dengan Let's Encrypt ==="
certbot --nginx -d $DOMAIN -d www.$DOMAIN --non-interactive --agree-tos -m admin@$DOMAIN

echo ""
echo "✅ Deploy selesai!"
echo "   App berjalan di: https://$DOMAIN"
echo "   Status service : systemctl status telegram_blaster"
echo "   Logs           : journalctl -u telegram_blaster -f"
