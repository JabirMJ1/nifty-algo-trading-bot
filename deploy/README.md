# Deployment Reference

## Live Infrastructure

| Resource | Value |
|----------|-------|
| **EC2 Instance ID** | `i-0123456789abcdef0` |
| **Public IP** | `203.0.113.12` |
| **Instance type** | `t2.micro` (1 vCPU, 1 GB RAM) — forced by new-account vCPU limit |
| **Region** | `ap-south-1` (Mumbai) |
| **AMI** | Amazon Linux 2023 |
| **Storage** | 30 GB gp3 |
| **SSH key** | `~/my-ec2-key.pem` (local) |
| **SSH user** | `ec2-user` |
| **App path on EC2** | `/opt/trading-bot` |
| **Security group** | `sg-0123456789abcdef0` |

## Architecture

```
GitHub push to main
       │
       ▼
GitHub Actions (.github/workflows/deploy.yml)
       │  wakes EC2 if stopped → SSH
       ▼
EC2 t2.micro (ap-south-1 / Mumbai)  ← 203.0.113.12
  ├── docker-compose
  │     ├── trading-bot (python main.py)
  │     ├── postgres:16-alpine
  │     └── redis:7-alpine
  └── .env pulled from AWS Secrets Manager (trading-bot/env)

EventBridge Scheduler
  ├── Start instance: 08:45 IST Mon–Fri
  └── Stop  instance: 16:00 IST Mon–Fri

AWS Budget: $10/month → SNS alert → you@example.com
```

## AWS Resources Created

| Resource | Name / ID |
|----------|-----------|
| EC2 instance | `i-0123456789abcdef0` |
| Key pair | `my-ec2-key` |
| Security group | `trading-bot-sg` / `sg-0123456789abcdef0` |
| IAM role (EC2) | `trading-bot-role` (SecretsManagerReadWrite) |
| IAM instance profile | `trading-bot-profile` |
| IAM role (EventBridge) | `trading-bot-scheduler-role` |
| EventBridge schedule | `trading-bot-start` (08:45 IST Mon–Fri) |
| EventBridge schedule | `trading-bot-stop` (16:00 IST Mon–Fri) |
| Secrets Manager secret | `trading-bot/env` (ap-south-1) |
| IAM user (CI/CD) | `github-actions-deploy` |
| SNS topic | `trading-bot-cost-alert` (us-east-1) |
| AWS Budget | `trading-bot-monthly-cap` ($10/month) |

## GitHub Repository Secrets

Go to **GitHub → repo → Settings → Secrets and variables → Actions**

| Secret | Value |
|--------|-------|
| `EC2_HOST` | `203.0.113.12` |
| `EC2_INSTANCE_ID` | `i-0123456789abcdef0` |
| `EC2_SSH_KEY` | Contents of `~/my-ec2-key.pem` |
| `AWS_ACCESS_KEY_ID` | IAM user `github-actions-deploy` key |
| `AWS_SECRET_ACCESS_KEY` | IAM user `github-actions-deploy` secret |

## Kite Token — Fully Automated

Token refresh runs automatically on every bot startup via `modules/sensory/auto_token.py`:
1. Logs into Zerodha with `KITE_USER_ID` + `KITE_PASSWORD` + TOTP (generated from `KITE_TOTP_SECRET`)
2. Gets fresh `access_token`, writes it to `.env`
3. WebSocket connects with the new token

**No manual steps required.** The 3 credentials are stored in Secrets Manager under `trading-bot/env`.

If auto-refresh ever fails (Zerodha API change, wrong credentials), the bot falls back to
`KITE_ACCESS_TOKEN` in `.env` and logs a warning. You can also refresh manually:
```bash
ssh -i ~/my-ec2-key.pem ec2-user@203.0.113.12
cd /opt/trading-bot
python generate_token.py
aws secretsmanager update-secret --secret-id trading-bot/env --region ap-south-1 --secret-string file://.env
docker compose restart bot
```

---

## CD Pipeline

Every push to `main` triggers `.github/workflows/deploy.yml`:
1. Wakes EC2 if stopped/stopping (waits for SSH)
2. `git pull origin main`
3. Re-pulls `.env` from Secrets Manager
4. `docker compose down && docker compose up -d --build`

Prefer pushing **after 16:00 IST** to avoid restarting during market hours.

---

## Daily Operations

The bot is fully automatic on weekdays:

| Time (IST) | Event |
|------------|-------|
| 08:45 | EventBridge starts EC2 |
| ~08:46 | Docker starts, `auto_token.py` logs in to Zerodha |
| 08:55 | Scheduler pre-market check (validates token) |
| 09:15 | Market open — circuit breaker resets, bot ready |
| 09:30 | Signal gate opens (first 15 min excluded) |
| 15:30 | Market close — all positions force-closed |
| 15:35 | EOD PnL report |
| 16:00 | EventBridge stops EC2 |

---

## Useful Commands

```bash
# SSH in
ssh -i ~/my-ec2-key.pem ec2-user@203.0.113.12

# Check container status
ssh -i ~/my-ec2-key.pem ec2-user@203.0.113.12 "cd /opt/trading-bot && docker compose ps"

# Tail live bot logs
ssh -i ~/my-ec2-key.pem ec2-user@203.0.113.12 "docker compose -f /opt/trading-bot/docker-compose.yml logs bot -f --tail=50"

# Manual start/stop EC2
aws ec2 start-instances --instance-ids i-0123456789abcdef0 --region ap-south-1
aws ec2 stop-instances  --instance-ids i-0123456789abcdef0 --region ap-south-1

# Update .env in Secrets Manager (from local machine)
aws secretsmanager update-secret --secret-id trading-bot/env --region ap-south-1 --secret-string file://.env

# Rebuild and restart bot only (keep postgres/redis running)
ssh -i ~/my-ec2-key.pem ec2-user@203.0.113.12 "cd /opt/trading-bot && docker compose up -d --build bot"
```

---

## Cost

| Item | Rate | Monthly (~157 hrs) |
|------|------|-------------------|
| t2.micro | $0.0116/hr | ~$1.82 |
| 30 GB gp3 storage | $0.08/GB/mo | ~$2.40 |
| Data transfer | negligible | ~$0.10 |
| **Total** | | **~$4.32** |

Budget alerts: email at **$8** (80%) and **$10** (100%) to `you@example.com`.

> To upgrade to cheaper `t3a.micro` ($0.0094/hr): request a vCPU limit increase from AWS
> (free, ~24 hr turnaround) then change instance type in the console.

---

## Switching to Live Trading

When ready to go live, update `.env`:
```
PAPER_TRADE=false
```
Then push to Secrets Manager and redeploy:
```bash
aws secretsmanager update-secret --secret-id trading-bot/env --region ap-south-1 --secret-string file://.env
git commit --allow-empty -m "Switch to live trading" && git push
```
