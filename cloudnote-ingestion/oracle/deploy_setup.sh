#!/bin/bash

# ==============================================================================
# CloudNote Ingestion Worker - Oracle VM Deployment Setup Script
# ==============================================================================
# This script automates the host provisioning, folder setup, permissions,
# systemd service/timer configurations, and automatic process management.
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
echo "          CLOUDNOTE INGESTION - ORACLE VM DEPLOYMENT SETUP            "
echo "======================================================================"
echo -e "${NC}"

# 1. Establish Installation Directory
INSTALL_DIR="/opt/cloudnote-ingestion"
log_info "Creating target directory: ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"

# 2. Check and Install Docker and Docker Compose
log_info "Verifying Docker installation..."
if ! command -v docker &> /dev/null; then
    log_warning "Docker not found. Attempting installation..."
    apt-get update && apt-get install -y docker.io
    systemctl enable --now docker
else
    log_success "Docker is installed: $(docker --version)"
fi

log_info "Verifying Docker Compose installation..."
if ! docker compose version &> /dev/null; then
    log_warning "Docker Compose not found. Installing docker-compose-plugin..."
    apt-get update && apt-get install -y docker-compose-plugin
else
    log_success "Docker Compose is installed: $(docker compose version)"
fi

# 3. Create persistent directories and set permissions
log_info "Creating persistent storage folders for logs and screenshots..."
mkdir -p "${INSTALL_DIR}/logs"
mkdir -p "${INSTALL_DIR}/screenshots"

# Set permissions so the docker daemon and local user can access them
chmod 777 "${INSTALL_DIR}/logs"
chmod 777 "${INSTALL_DIR}/screenshots"
log_success "Persistent storage directories configured successfully."

# 4. Copy systemd configuration files
log_info "Installing Systemd units..."
if [ -f "cloudnote.service" ]; then
    cp cloudnote.service /etc/systemd/system/
    cp cloudnote.timer /etc/systemd/system/
elif [ -f "oracle/cloudnote.service" ]; then
    cp oracle/cloudnote.service /etc/systemd/system/
    cp oracle/cloudnote.timer /etc/systemd/system/
else
    log_error "Systemd unit files (cloudnote.service/timer) not found in current directory or oracle/."
    exit 1
fi

# Ensure Asia/Kolkata timezone is configured on the host OS
log_info "Ensuring system timezone is configured..."
if ! timedatectl show --property=Timezone | grep -q "Asia/Kolkata"; then
    log_warning "System timezone is not Asia/Kolkata. Setting timezone to Asia/Kolkata for systemd timer accuracy..."
    timedatectl set-timezone Asia/Kolkata || log_warning "Failed to set timezone. Please ensure tzdata is installed."
fi
log_success "Timezone configuration verified: $(date)"

# 5. Reload systemd and register units
log_info "Reloading Systemd daemon..."
systemctl daemon-reload

log_info "Enabling and starting cloudnote.timer..."
systemctl enable cloudnote.timer
systemctl start cloudnote.timer
log_success "Systemd timer has been registered and started."

# 6. Verify Systemd status
log_info "Verifying timer registration..."
if systemctl is-active --quiet cloudnote.timer; then
    log_success "Timer is active."
    systemctl status cloudnote.timer --no-pager | head -n 15
else
    log_error "Failed to activate cloudnote.timer."
fi

# 7. Check for environment file
if [ ! -f "${INSTALL_DIR}/.env" ]; then
    log_warning "No .env file found at ${INSTALL_DIR}."
    log_info "Creating a template .env file. Please update it with your credentials."
    cat <<EOT > "${INSTALL_DIR}/.env"
# CloudNote Ingestion - Environment Configuration
LPU_USERNAME=your_username
LPU_PASSWORD=your_password
HEADLESS=True
LOG_LEVEL=INFO
EOT
    chmod 600 "${INSTALL_DIR}/.env"
fi

echo ""
echo -e "${GREEN}======================================================================"
echo "          SETUP SUCCESSFUL - ORACLE VM DEPLOYMENT COMPLETED           "
echo "======================================================================${NC}"
echo "Instructions:"
echo "1. Change directory to: cd ${INSTALL_DIR}"
echo "2. Edit the .env file with your university portal credentials."
echo "3. Copy your project code (excluding systemd configs) to: ${INSTALL_DIR}"
echo "4. Build the container manually once: sudo docker compose build"
echo "5. Validate manually by running: sudo systemctl start cloudnote.service"
echo "6. Monitor logs using: tail -f logs/ingestion.log"
echo "======================================================================"
