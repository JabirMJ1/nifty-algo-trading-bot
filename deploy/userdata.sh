#!/bin/bash
# EC2 user-data: runs once on first boot.
# Installs Docker, pulls the bot, sets up systemd to start/stop with the instance.

set -euo pipefail

# ── 1. System packages ────────────────────────────────────────────────────────
dnf update -y
dnf install -y docker git cronie
systemctl enable --now docker
usermod -aG docker ec2-user

# ── 2. Docker Compose plugin ──────────────────────────────────────────────────
mkdir -p /usr/local/lib/docker/cli-plugins
curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
     -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# ── 3. Clone repo ─────────────────────────────────────────────────────────────
# Replace with your actual repo URL
REPO_URL="https://github.com/YOUR_USER/trading-bot.git"
git clone "$REPO_URL" /opt/trading-bot
chown -R ec2-user:ec2-user /opt/trading-bot

# ── 4. Pull .env from AWS Secrets Manager ────────────────────────────────────
# Store your .env content as a secret named "trading-bot/env" in Secrets Manager.
aws secretsmanager get-secret-value \
    --secret-id "trading-bot/env" \
    --query SecretString \
    --output text \
    --region ap-south-1 \
  > /opt/trading-bot/.env
chown ec2-user:ec2-user /opt/trading-bot/.env
chmod 600 /opt/trading-bot/.env

# ── 5. Systemd service ────────────────────────────────────────────────────────
cat > /etc/systemd/system/trading-bot.service << 'EOF'
[Unit]
Description=Trading Bot (docker-compose)
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/trading-bot
ExecStart=/usr/bin/docker compose up -d --build
ExecStop=/usr/bin/docker compose down
User=ec2-user

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable trading-bot.service
# Don't start now — EventBridge will start/stop the instance on schedule.
# The service auto-starts when the instance boots.
