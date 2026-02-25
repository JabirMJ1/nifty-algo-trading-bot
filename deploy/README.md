# Deployment Guide

## Architecture

```
GitHub push to main
       │
       ▼
GitHub Actions (deploy.yml)
       │  SSH
       ▼
EC2 t3a.micro (ap-south-1 / Mumbai)
  ├── docker-compose
  │     ├── trading-bot
  │     ├── postgres
  │     └── redis
  └── .env pulled from AWS Secrets Manager

EventBridge Scheduler
  ├── Start instance: 08:45 IST Mon–Fri
  └── Stop  instance: 16:00 IST Mon–Fri

AWS Budget: $10/month cap → SNS alert → email
```

## One-Time Setup

### 1. Prerequisites
```bash
# Install AWS CLI v2 and configure with your IAM credentials
aws configure
# Set default region
aws configure set region ap-south-1
```

### 2. Run the setup script
```bash
# Edit the variables at the top of the script first
nano deploy/aws_setup.sh

chmod +x deploy/aws_setup.sh
./deploy/aws_setup.sh
```

This creates:
- EC2 t3a.micro with 20GB gp3 storage
- Security group (SSH from your IP only)
- IAM role (EC2 → Secrets Manager access)
- EventBridge schedules (start 08:45 / stop 16:00 IST, Mon–Fri)
- SNS email alert + AWS Budget ($10/month cap)

### 3. Add GitHub repository secrets

Go to **GitHub → repo → Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|--------|-------|
| `EC2_HOST` | Public IP printed by setup script |
| `EC2_INSTANCE_ID` | Instance ID printed by setup script (e.g. `i-0abc123`) |
| `EC2_SSH_KEY` | Contents of `my-ec2-key.pem` |
| `AWS_ACCESS_KEY_ID` | IAM user key (see setup script output) |
| `AWS_SECRET_ACCESS_KEY` | IAM user secret (see setup script output) |

### 4. Store .env in Secrets Manager
```bash
aws secretsmanager create-secret \
  --name trading-bot/env \
  --region ap-south-1 \
  --secret-string file://.env
```

To update it later:
```bash
aws secretsmanager update-secret \
  --name trading-bot/env \
  --region ap-south-1 \
  --secret-string file://.env
```

### 5. Confirm SNS email subscription
Check your inbox and click the confirmation link from AWS.

---

## CD Pipeline

Every push to `main` triggers [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml):

1. SSH into EC2
2. `git pull origin main`
3. Re-pull `.env` from Secrets Manager
4. `docker compose down && docker compose up -d --build`

**The bot is NOT restarted during market hours** unless you push during 08:45–16:00 IST — keep that in mind. Prefer pushing after 16:00.

---

## Daily Manual Step — Kite Token

Kite access tokens expire every day. Before market open each morning:

```bash
# SSH into instance
ssh -i my-ec2-key.pem ec2-user@<EC2_HOST>

# Refresh token (opens a browser URL — paste in your local browser, copy back the token)
cd /opt/trading-bot
python generate_token.py

# Push updated .env back to Secrets Manager
aws secretsmanager update-secret \
  --name trading-bot/env \
  --region ap-south-1 \
  --secret-string file://.env

# Restart only the bot container (keeps postgres/redis running)
docker compose restart bot
```

---

## Cost Limiter

| Trigger | Action |
|---------|--------|
| 80% of budget ($8) | Email alert via SNS |
| 100% of budget ($10) | Email alert via SNS |

> AWS Budget alerts have up to a 24-hour delay. For an instant kill-switch,
> stop the instance manually from the AWS console or via:
> ```bash
> aws ec2 stop-instances --instance-ids <INSTANCE_ID> --region ap-south-1
> ```

Expected monthly cost at ~157 hrs/month runtime: **~$3.20**

---

## Useful Commands

```bash
# Check bot status
ssh -i my-ec2-key.pem ec2-user@<EC2_HOST> "docker compose -f /opt/trading-bot/docker-compose.yml ps"

# Tail live logs
ssh -i my-ec2-key.pem ec2-user@<EC2_HOST> "tail -f /opt/trading-bot/logs/bot_$(date +%Y-%m-%d).log"

# Manual start/stop instance
aws ec2 start-instances --instance-ids <INSTANCE_ID> --region ap-south-1
aws ec2 stop-instances  --instance-ids <INSTANCE_ID> --region ap-south-1
```
