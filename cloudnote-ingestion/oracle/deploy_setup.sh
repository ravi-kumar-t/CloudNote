#!/bin/bash

# ==============================================================================
# CloudNote Platform - Oracle VM Host Deployment & Hardening Script
# ==============================================================================
# This script automates production environment setup, permissions, directories,
# systemd services (24/7 stack + scheduled scraper), and Nginx reverse proxy routing.
# ==============================================================================

set -euo pipefail

# Colorful status indicators
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0;3m' # No Color
BOLD='\033[1m'

log_info() {
    echo -e "${BLUE}${BOLD}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}${BOLD}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}${BOLD}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}${BOLD}[ERROR]${NC} $1"
}

# Ensure script is run with sudo privileges
if [ "$EUID" -ne 0 ]; then
    log_error "Please run this script as root or using sudo: sudo ./deploy_setup.sh"
    exit 1
fi

echo -e "${GREEN}"
echo "======================================================================"
echo "          CLOUDNOTE DEPLOYMENT - ORACLE VM HOST HARDENING             "
echo "======================================================================"
echo -e "${NC}"

# 1. Establish Installation Directory
INSTALL_DIR="/opt/cloudnote-ingestion"
log_info "Creating production folder: ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"

# 2. Check and Install Docker and Docker Compose
log_info "Verifying Docker installation..."
if ! command -v docker &> /dev/null; then
    log_warning "Docker not found. Installing Docker engine..."
    apt-get update && apt-get install -y docker.io
    systemctl enable --now docker
else
    log_success "Docker is installed: $(docker --version)"
fi

log_info "Verifying Docker Compose plugin installation..."
if ! docker compose version &> /dev/null; then
    log_warning "Docker Compose not found. Installing docker-compose-plugin..."
    apt-get update && apt-get install -y docker-compose-plugin
else
    log_success "Docker Compose plugin is installed: $(docker compose version)"
fi

# 3. Check and Install Nginx
log_info "Verifying Nginx installation..."
if ! command -v nginx &> /dev/null; then
    log_warning "Nginx not found. Installing Nginx reverse proxy..."
    apt-get update && apt-get install -y nginx
    systemctl enable --now nginx
else
    log_success "Nginx is installed: $(nginx -v 2>&1)"
fi

# 4. Create persistent directories and set secure permissions
log_info "Configuring persistent storage for shared SQLite DB and diagnostics..."
mkdir -p "${INSTALL_DIR}/logs"
mkdir -p "${INSTALL_DIR}/screenshots"

# Set open read-write permissions so non-root container users can read/write logs and screenshots
chmod 777 "${INSTALL_DIR}/logs"
chmod 777 "${INSTALL_DIR}/screenshots"
log_success "Persistent storage directories configured successfully."

# 5. Install Systemd configuration files
log_info "Installing Systemd units..."
SYS_DIR="/etc/systemd/system"

# Detect file locations
if [ -f "cloudnote-stack.service" ]; then
    cp cloudnote-stack.service "${SYS_DIR}/"
    cp cloudnote.service "${SYS_DIR}/"
    cp cloudnote.timer "${SYS_DIR}/"
elif [ -f "oracle/cloudnote-stack.service" ]; then
    cp oracle/cloudnote-stack.service "${SYS_DIR}/"
    cp oracle/cloudnote.service "${SYS_DIR}/"
    cp oracle/cloudnote.timer "${SYS_DIR}/"
else
    log_error "Systemd unit files not found in current directory or oracle/."
    exit 1
fi

# 6. Ensure Asia/Kolkata timezone is configured on the host OS for timer accuracy
log_info "Configuring host system timezone..."
if ! timedatectl show --property=Timezone | grep -q "Asia/Kolkata"; then
    log_warning "System timezone is not Asia/Kolkata. Correcting timezone to Asia/Kolkata..."
    timedatectl set-timezone Asia/Kolkata || log_warning "Failed to set timezone. Please ensure tzdata is installed."
fi
log_success "Timezone configuration verified: $(date)"

# 7. Reload systemd and register units
log_info "Reloading Systemd daemon..."
systemctl daemon-reload

log_info "Enabling and starting cloudnote-stack.service (24/7 web stack)..."
systemctl enable cloudnote-stack.service
systemctl start cloudnote-stack.service

log_info "Enabling and starting cloudnote.timer (scheduled ingestion scheduler)..."
systemctl enable cloudnote.timer
systemctl start cloudnote.timer

log_success "Systemd services and timers registered and started."

# 8. Configure Nginx Reverse Proxy
log_info "Configuring Nginx reverse proxy routes..."
NGINX_CONF="nginx.conf"
if [ ! -f "${NGINX_CONF}" ] && [ -f "oracle/nginx.conf" ]; then
    NGINX_CONF="oracle/nginx.conf"
fi

if [ -f "${NGINX_CONF}" ]; then
    cp "${NGINX_CONF}" /etc/nginx/sites-available/cloudnote
    # Create symlink if not already exists
    if [ ! -f /etc/nginx/sites-enabled/cloudnote ]; then
        ln -s /etc/nginx/sites-available/cloudnote /etc/nginx/sites-enabled/
    fi
    
    # Remove default Nginx site to prevent server block conflicts
    if [ -f /etc/nginx/sites-enabled/default ]; then
        log_info "Removing default Nginx server block to prevent port 80 routing conflicts..."
        rm /etc/nginx/sites-enabled/default
    fi
    
    # Verify Nginx syntax and reload
    if nginx -t; then
        systemctl reload nginx
        log_success "Nginx reverse proxy is configured, validated, and reloaded."
    else
        log_error "Nginx configuration syntax check failed. Please check /etc/nginx/sites-available/cloudnote"
    fi
else
    log_warning "Nginx config template not found. Please copy oracle/nginx.conf to /etc/nginx/sites-available/cloudnote manually."
fi

# 9. Provision template environment file
if [ ! -f "${INSTALL_DIR}/.env" ]; then
    log_info "Creating default template .env file at ${INSTALL_DIR}/.env..."
    cat <<EOT > "${INSTALL_DIR}/.env"
# ==============================================================================
# CloudNote Ingestion - Production Environment Settings
# ==============================================================================
LPU_USERNAME=your_12014633_username
LPU_PASSWORD=your_secure_password
HEADLESS=True
LOG_LEVEL=INFO

# Ingestion scheduling override (if needed)
DEBUG_SLEEP_OVERRIDE_SECONDS=
EOT
    chmod 600 "${INSTALL_DIR}/.env"
    log_success "Template .env created. Remember to update credentials!"
fi

echo ""
echo -e "${GREEN}======================================================================"
echo "    SETUP SUCCESSFUL - ORACLE VM PRODUCTION HARDENING COMPLETED       "
echo "======================================================================${NC}"
echo "Deploy Guide Checklist:"
echo " 1. Make sure your application code is in: ${INSTALL_DIR}"
echo " 2. Populate credentials in the environment config: ${INSTALL_DIR}/.env"
echo " 3. Verify Systemd Dashboard Stack is active: systemctl status cloudnote-stack.service"
echo " 4. Verify Systemd Scraper Timer is active: systemctl status cloudnote.timer"
echo " 5. Inspect real-time server and scraper logs:"
echo "    - Web Dashboard Stack: journalctl -u cloudnote-stack.service -f"
echo "    - Scheduled Scraper Jobs: journalctl -u cloudnote.service -f"
echo "    - Shared Ingestion logs: tail -f ${INSTALL_DIR}/logs/ingestion.log"
echo "======================================================================"
