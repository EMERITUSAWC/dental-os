#!/bin/sh
# Puts Dental OS on a clinic computer running Ubuntu Server 24.04 LTS. Run once, as root, from the unpacked folder:
#   sudo sh deploy/install-ubuntu.sh
# It installs Python and Caddy from Ubuntu's own packages, copies the code to /opt/dental-os, keeps the data in
# /var/lib/dental-os, and starts the service. Running it again updates the code and keeps the data and settings.
set -eu
SRC=$(cd "$(dirname "$0")/.." && pwd)

apt-get update
apt-get install -y python3 python3-venv caddy

id dentalos >/dev/null 2>&1 || useradd --system --home /var/lib/dental-os --shell /usr/sbin/nologin dentalos
install -d -o dentalos -g dentalos -m 750 /var/lib/dental-os /var/lib/dental-os/files /var/lib/dental-os/backups /var/log/dental-os

install -d /opt/dental-os
cp -r "$SRC/dental_os" "$SRC/wsgi.py" "$SRC/requirements.txt" "$SRC/deploy" /opt/dental-os/
[ -d /opt/dental-os/venv ] || python3 -m venv /opt/dental-os/venv
/opt/dental-os/venv/bin/pip install --quiet -r /opt/dental-os/requirements.txt
chmod 755 /opt/dental-os/deploy/dos

if [ ! -f /etc/dental-os.env ]; then
  cp /opt/dental-os/deploy/dental-os.env.example /etc/dental-os.env
  KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  sed -i "s/^SECRET_KEY=$/SECRET_KEY=$KEY/" /etc/dental-os.env
fi
chown root:dentalos /etc/dental-os.env
chmod 640 /etc/dental-os.env

cp /opt/dental-os/deploy/dental-os.service /etc/systemd/system/dental-os.service
cp /opt/dental-os/deploy/dental-os.cron /etc/cron.d/dental-os
chmod 644 /etc/cron.d/dental-os
[ -f /etc/caddy/Caddyfile.dental-os-done ] || { cp /opt/dental-os/deploy/Caddyfile.example /etc/caddy/Caddyfile; touch /etc/caddy/Caddyfile.dental-os-done; }

systemctl daemon-reload
systemctl enable --now dental-os
systemctl reload caddy || systemctl restart caddy

echo
echo "Dental OS is running. Next:"
echo "  1. Edit /etc/dental-os.env: PUBLIC_URL, OFFSITE_TARGET and OFFSITE_PASSPHRASE. Then: systemctl restart dental-os"
echo "  2. Edit /etc/caddy/Caddyfile if the name is not dental.local. Then: systemctl reload caddy"
echo "  3. sudo -u dentalos /opt/dental-os/deploy/dos create-clinic    (then create-user for the first manager)"
echo "  4. sudo -u dentalos /opt/dental-os/deploy/dos doctor           (lists what is still to do)"
