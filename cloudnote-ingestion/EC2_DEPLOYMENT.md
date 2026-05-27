# CloudNote Ingestion - EC2 Deployment Guide

This guide covers deploying the production-ready CloudNote system on an AWS EC2 instance running Ubuntu.

## Prerequisites
- AWS EC2 Instance (Ubuntu 22.04 LTS or newer)
- Minimum 2GB RAM (4GB recommended for headless Chromium)
- Security Group allowing inbound traffic on:
  - Port `80` (HTTP / Frontend)
  - Port `8000` (Backend API)
  - Port `3001` (Grafana - Optional but recommended)
  - Port `9090` (Prometheus - Optional)

## 1. SSH into your EC2 Instance
```bash
ssh -i /path/to/your-key.pem ubuntu@<your-ec2-ip-address>
```

## 2. Install Docker & Docker Compose
Run the following commands to install the latest Docker engine:
```bash
# Add Docker's official GPG key
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# Add the repository to Apt sources
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update

# Install Docker packages
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Enable Docker without sudo (optional, requires re-login)
sudo usermod -aG docker $USER
```

## 3. Clone the Repository
```bash
git clone <your-repo-url>
cd cloudnote-ingestion
```

## 4. Configure Production Environment
Copy the production template into `.env`:
```bash
cp .env.production .env
```
Edit the `.env` file to set your actual LPU credentials:
```bash
nano .env
```
Ensure it contains:
```env
LPU_USERNAME=your_actual_username
LPU_PASSWORD=your_actual_password
HEADLESS=true
DEMO_MODE=false
DEBUG_MODE=false
BACKEND_INTERNAL_URL=http://backend:8000
LOG_LEVEL=INFO
```

## 5. Deploy the Stack
Deploy the full stack in detached mode:
```bash
sudo docker compose up -d --build
```
> **Note:** Rebuilding is only necessary if you've pulled new code or modified the Dockerfiles. Use `docker compose up -d` for standard restarts.

## 6. Verify Deployment
Check that all containers are `Up` and `healthy`:
```bash
sudo docker ps
```

Monitor logs for the ingestion worker or backend:
```bash
sudo docker logs -f cloudnote-ingestion-worker
sudo docker logs -f cloudnote-backend
```

## 7. Access Points
Once deployed, access the services using your EC2 public IP:
- **Frontend Dashboard:** `http://<ec2-ip>:3000`
- **Backend API Docs:** `http://<ec2-ip>:8000/docs`
- **Grafana Dashboards:** `http://<ec2-ip>:3001` (Default login: admin / admin123)
- **Prometheus UI:** `http://<ec2-ip>:9090`

## Production Safety Notes
- The worker is configured to automatically recover from unhandled exceptions.
- `docker-compose.yml` ensures all services have `restart: unless-stopped`, meaning they will automatically boot up if the EC2 instance restarts.
- Persistent volumes are configured for Prometheus and Grafana metrics, and local mounts for `logs/` and `screenshots/` to survive container restarts.
- All dependencies for headless Playwright (including Chromium and fonts) are handled natively within the Microsoft Playwright container image on Ubuntu Jammy. No extra host installations are required.
