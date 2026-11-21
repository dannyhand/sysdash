#!/usr/bin/env bash
# Installs sysdash on a Debian/Ubuntu server running apache2.
# Usage:  sudo ./install.sh            (asks for a dashboard username/password the first time)
# Re-run any time to update the files; it keeps your password and history.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "Run with sudo: sudo ./install.sh"; exit 1; fi
cd "$(dirname "$0")"

command -v python3 >/dev/null || { echo "python3 is required (apt install python3)"; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { echo "Python 3.8+ is required (found $(python3 -V))"; exit 1; }
if ! command -v a2enmod >/dev/null || [[ ! -d /etc/apache2/conf-available ]]; then
    echo "This expects Debian/Ubuntu's apache2 package (a2enmod, /etc/apache2)."
    [[ -d /opt/bitnami ]] && echo "This looks like a Bitnami image - its Apache lives in /opt/bitnami/apache and needs different steps."
    exit 1
fi
command -v systemctl >/dev/null || { echo "systemd is required"; exit 1; }
if ! command -v htpasswd >/dev/null; then
    echo "Installing apache2-utils (for htpasswd)..."
    apt-get update -q && apt-get install -y apache2-utils
fi

echo "==> Copying files to /opt/sysdash"
install -d /opt/sysdash/www
install -m 755 sysdash.py /opt/sysdash/sysdash.py
install -m 644 www/index.html /opt/sysdash/www/index.html

echo "==> Creating service user 'sysdash'"
id sysdash >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin sysdash

echo "==> Installing systemd service"
install -m 644 sysdash.service /etc/systemd/system/sysdash.service
systemctl daemon-reload
systemctl enable sysdash >/dev/null
systemctl restart sysdash

echo "==> Configuring Apache"
a2enmod -q proxy proxy_http auth_basic authn_file headers
install -m 644 apache-sysdash.conf /etc/apache2/conf-available/sysdash.conf
a2enconf -q sysdash

# (re)create the login if the file is missing or has no user in it
if ! grep -q ':' /etc/apache2/sysdash.htpasswd 2>/dev/null; then
    read -rp "Dashboard username: " DASH_USER
    until htpasswd -c -B /etc/apache2/sysdash.htpasswd "$DASH_USER"; do
        echo "Passwords didn't match - try again."
    done
fi
chown root:www-data /etc/apache2/sysdash.htpasswd
chmod 640 /etc/apache2/sysdash.htpasswd

apache2ctl configtest
systemctl reload apache2

sleep 2
if curl -fs http://127.0.0.1:8765/api/live >/dev/null; then
    echo
    echo "Done. Open https://<your-domain>/dashboard/"
    echo "The first disk scan runs in the background; the space map appears when it finishes."
else
    echo "Backend didn't answer - check: journalctl -u sysdash -n 50"
    exit 1
fi
