#!/usr/bin/env bash
# ==============================================================================
# Automated systemd deployment script for OLX Kyiv Real Estate Parser
# Supported OS: Ubuntu 20.04/22.04/24.04, Debian 11/12
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="olxparser"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
TARGET_PORT="${PORT:-8000}"
CURRENT_USER="${SUDO_USER:-$(id -un)}"
CURRENT_GROUP="${SUDO_GID:-$(id -gn)}"

echo "=========================================================="
echo " OLX Kyiv Real Estate Parser - Systemd Deployment Script"
echo "=========================================================="
echo " Project Directory: ${SCRIPT_DIR}"
echo " Service User:      ${CURRENT_USER}:${CURRENT_GROUP}"
echo " Web UI Port:       ${TARGET_PORT}"
echo "=========================================================="

# 1. Check Python 3 installation
if ! command -v python3 &>/dev/null; then
    echo "[-] Error: python3 is not installed. Please run: sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
    exit 1
fi

# 2. Setup Virtual Environment
VENV_DIR="${SCRIPT_DIR}/.venv"
if [ ! -d "${VENV_DIR}" ]; then
    echo "[+] Creating Python virtual environment in ${VENV_DIR}..."
    python3 -m venv "${VENV_DIR}"
else
    echo "[*] Virtual environment already exists at ${VENV_DIR}"
fi

# 3. Install or update dependencies
echo "[+] Installing and updating dependencies from requirements.txt..."
"${VENV_DIR}/bin/pip" install --upgrade pip --quiet
"${VENV_DIR}/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt" --quiet

# 4. Create persistent data and exports directories
mkdir -p "${SCRIPT_DIR}/data" "${SCRIPT_DIR}/exports"
chown -R "${CURRENT_USER}:${CURRENT_GROUP}" "${SCRIPT_DIR}/data" "${SCRIPT_DIR}/exports" 2>/dev/null || true

# 5. Generate Systemd service unit content
TMP_SERVICE=$(mktemp)
cat <<EOF > "${TMP_SERVICE}"
[Unit]
Description=OLX Kyiv Real Estate Parser & Web UI Service
After=network.target

[Service]
Type=simple
User=${CURRENT_USER}
Group=${CURRENT_GROUP}
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${VENV_DIR}/bin/uvicorn app:app --host 0.0.0.0 --port ${TARGET_PORT}

Environment="PYTHONUNBUFFERED=1"
Environment="TZ=Europe/Kyiv"
Environment="OLX_DB_PATH=${SCRIPT_DIR}/data/olx_storage.db"
Environment="OLX_EXPORTS_DIR=${SCRIPT_DIR}/exports"
Environment="PATH=${VENV_DIR}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"

Restart=always
RestartSec=5s
KillMode=mixed
TimeoutStopSec=30s

StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF

# 6. Install service unit file
if [ "$(id -u)" -eq 0 ]; then
    echo "[+] Installing systemd service unit to ${SERVICE_FILE}..."
    mv "${TMP_SERVICE}" "${SERVICE_FILE}"
    chmod 644 "${SERVICE_FILE}"

    echo "[+] Reloading systemd daemon..."
    systemctl daemon-reload

    echo "[+] Enabling and restarting ${SERVICE_NAME} service..."
    systemctl enable "${SERVICE_NAME}.service"
    systemctl restart "${SERVICE_NAME}.service"

    echo ""
    echo "[✓] Deployment completed successfully!"
    echo " -> Web Dashboard:  http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo '127.0.0.1'):${TARGET_PORT}"
    echo " -> Service Status: systemctl status ${SERVICE_NAME}"
    echo " -> Live Logs:      journalctl -u ${SERVICE_NAME} -f"
else
    echo "[!] Not running as root/sudo. Generated service unit at ${SCRIPT_DIR}/${SERVICE_NAME}.service"
    mv "${TMP_SERVICE}" "${SCRIPT_DIR}/${SERVICE_NAME}.service"
    echo ""
    echo "To finalize installation with systemd, run with sudo:"
    echo "  sudo cp ${SCRIPT_DIR}/${SERVICE_NAME}.service ${SERVICE_FILE}"
    echo "  sudo systemctl daemon-reload"
    echo "  sudo systemctl enable --now ${SERVICE_NAME}.service"
fi
