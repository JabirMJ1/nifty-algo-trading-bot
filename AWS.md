# AWS Reference — Trading Bot

Quick reference for managing the trading bot on AWS.

---

## Infrastructure Overview

| Resource | Value |
|----------|-------|
| Region | `ap-south-1` (Mumbai) |
| EC2 Instance | `i-0123456789abcdef0` (t2.micro) |
| EC2 IP | `203.0.113.11` (changes on restart — always re-fetch) |
| EC2 User | `ec2-user` |
| Bot directory | `/opt/trading-bot` |
| CloudWatch Log Group | `/trading-bot/bot` |
| S3 Bucket (log archives) | `trading-bot-logs-123456789012` |
| Secrets Manager | `trading-bot/env` |

---

## SSH into the EC2 Instance

The local SSH key is `~/.ssh/my-ec2-key`. The IP changes every time the instance stops/starts — always fetch the current one first.

```bash
# 1. Get current IP
IP=$(aws ec2 describe-instances \
  --instance-ids i-0123456789abcdef0 \
  --region ap-south-1 \
  --query "Reservations[0].Instances[0].PublicIpAddress" \
  --output text)
echo $IP

# 2. Push your public key (valid for 60 seconds)
aws ec2-instance-connect send-ssh-public-key \
  --region ap-south-1 \
  --instance-id i-0123456789abcdef0 \
  --instance-os-user ec2-user \
  --ssh-public-key file://$HOME/.ssh/my-ec2-key.pub

# 3. SSH in
ssh -i ~/.ssh/my-ec2-key ec2-user@$IP
```

> **One-liner** (does all 3 steps):
> ```bash
> IP=$(aws ec2 describe-instances --instance-ids i-0123456789abcdef0 --region ap-south-1 --query "Reservations[0].Instances[0].PublicIpAddress" --output text) && aws ec2-instance-connect send-ssh-public-key --region ap-south-1 --instance-id i-0123456789abcdef0 --instance-os-user ec2-user --ssh-public-key file://$HOME/.ssh/my-ec2-key.pub && ssh -i ~/.ssh/my-ec2-key ec2-user@$IP
> ```

---

## EC2 — Start / Stop / Status

```bash
# Check instance state
aws ec2 describe-instances \
  --instance-ids i-0123456789abcdef0 \
  --region ap-south-1 \
  --query "Reservations[0].Instances[0].{State:State.Name,IP:PublicIpAddress}" \
  --output table

# Start instance (also triggered automatically at 8:45 AM IST via GitHub Actions)
aws ec2 start-instances --instance-ids i-0123456789abcdef0 --region ap-south-1
aws ec2 wait instance-running --instance-ids i-0123456789abcdef0 --region ap-south-1

# Stop instance (also triggered automatically at 11:35 PM IST via GitHub Actions)
aws ec2 stop-instances --instance-ids i-0123456789abcdef0 --region ap-south-1
```

---

## Docker — Bot Management (run on EC2 via SSH)

```bash
cd /opt/trading-bot

# View live bot logs
docker compose logs -f bot

# View last 100 lines
docker compose logs --tail=100 bot

# Check running containers
docker compose ps

# Restart bot (e.g. after config change)
docker compose restart bot

# Full redeploy (pull + rebuild)
git pull origin main && docker compose down && docker compose up -d --build

# Check bot environment variables loaded from Secrets Manager
docker compose exec bot env | grep -E "CAPITAL|PAPER|INSTRUMENTS"
```

---

## CloudWatch — View Logs from Local Machine

```bash
# Live tail (follows new log lines)
aws logs tail /trading-bot/bot --region ap-south-1 --follow

# Last 1 hour
aws logs tail /trading-bot/bot --region ap-south-1 --since 1h

# Last 3 hours
aws logs tail /trading-bot/bot --region ap-south-1 --since 3h

# Search for errors in last 24 hours
aws logs filter-log-events \
  --log-group-name "/trading-bot/bot" \
  --region ap-south-1 \
  --start-time $(date -d '24 hours ago' +%s000) \
  --filter-pattern "ERROR" \
  --query "events[*].message" \
  --output text

# Search for a specific keyword (e.g. "margin", "SIGNAL", "ORDER PLACED")
aws logs filter-log-events \
  --log-group-name "/trading-bot/bot" \
  --region ap-south-1 \
  --start-time $(date -d '24 hours ago' +%s000) \
  --filter-pattern "ORDER PLACED" \
  --query "events[*].message" \
  --output text

# Search today's logs only
aws logs filter-log-events \
  --log-group-name "/trading-bot/bot" \
  --region ap-south-1 \
  --start-time $(date -d 'today 00:00' +%s000) \
  --query "events[*].message" \
  --output text | less
```

---

## Secrets Manager — Bot Config (`.env`)

All bot environment variables live in Secrets Manager, not on the server.
The deploy workflow pulls them fresh on every deploy.

```bash
# View current .env values
aws secretsmanager get-secret-value \
  --secret-id "trading-bot/env" \
  --query SecretString \
  --output text \
  --region ap-south-1

# Update a single value (e.g. change CAPITAL)
CURRENT=$(aws secretsmanager get-secret-value \
  --secret-id "trading-bot/env" \
  --query SecretString \
  --output text \
  --region ap-south-1)

# Edit and push back (opens in vim — use i to insert, :wq to save)
echo "$CURRENT" > /tmp/bot-env.txt
vim /tmp/bot-env.txt
aws secretsmanager put-secret-value \
  --secret-id "trading-bot/env" \
  --secret-string file:///tmp/bot-env.txt \
  --region ap-south-1
rm /tmp/bot-env.txt

# View specific keys
aws secretsmanager get-secret-value \
  --secret-id "trading-bot/env" \
  --query SecretString \
  --output text \
  --region ap-south-1 | grep -E "CAPITAL|PAPER_TRADE|INSTRUMENTS|MARGIN"
```

> **After updating secrets:** redeploy the bot so it picks up the new values:
> ```bash
> # Trigger deploy from local machine (no SSH needed)
> gh workflow run deploy.yml --repo JabirMJ1/trading-bot
> ```

---

## S3 — Log Archives

Weekly logs are exported every Friday to S3.

```bash
# List archived weeks
aws s3 ls s3://trading-bot-logs-123456789012/weekly/ --region ap-south-1

# List files in a specific week
aws s3 ls s3://trading-bot-logs-123456789012/weekly/2026-W10/ --region ap-south-1 --recursive

# Download a week's logs
aws s3 sync \
  s3://trading-bot-logs-123456789012/weekly/2026-W10/ \
  ./logs/archive/2026-W10/ \
  --region ap-south-1

# Trigger manual archive (without waiting for Friday)
gh workflow run archive-logs.yml --repo JabirMJ1/trading-bot
```

---

## GitHub Actions — Manual Triggers

```bash
# Deploy latest code to EC2
gh workflow run deploy.yml --repo JabirMJ1/trading-bot

# Start EC2 manually (outside market hours)
gh workflow run start-ec2.yml --repo JabirMJ1/trading-bot

# Stop EC2 manually
gh workflow run stop-ec2.yml --repo JabirMJ1/trading-bot

# View recent workflow runs
gh run list --repo JabirMJ1/trading-bot --limit 10

# Watch a running workflow
gh run watch --repo JabirMJ1/trading-bot
```

---

## Automatic Schedule (GitHub Actions)

| Time (IST) | Action |
|------------|--------|
| 8:45 AM Mon–Fri | EC2 starts, deploy triggered |
| 11:35 PM Mon–Fri | EC2 stops |
| Friday 5:30 PM | Logs archived to S3 |
| Every push to `main` | Auto-deploy to EC2 |

---

## Troubleshooting

**Bot not trading / orders failing:**
```bash
aws logs filter-log-events \
  --log-group-name "/trading-bot/bot" \
  --region ap-south-1 \
  --start-time $(date -d '2 hours ago' +%s000) \
  --filter-pattern "ERROR" \
  --query "events[*].message" --output text
```

**Check if bot container is running:**
```bash
IP=$(aws ec2 describe-instances --instance-ids i-0123456789abcdef0 --region ap-south-1 --query "Reservations[0].Instances[0].PublicIpAddress" --output text)
aws ec2-instance-connect send-ssh-public-key --region ap-south-1 --instance-id i-0123456789abcdef0 --instance-os-user ec2-user --ssh-public-key file://$HOME/.ssh/my-ec2-key.pub
ssh -i ~/.ssh/my-ec2-key ec2-user@$IP "docker compose -f /opt/trading-bot/docker-compose.yml ps"
```

**EC2 IP changed after restart:**
Always use `aws ec2 describe-instances` to get the current IP — it changes on every stop/start.

**Kite token expired:**
```bash
# Run generate_token.py on the server
IP=$(aws ec2 describe-instances --instance-ids i-0123456789abcdef0 --region ap-south-1 --query "Reservations[0].Instances[0].PublicIpAddress" --output text)
aws ec2-instance-connect send-ssh-public-key --region ap-south-1 --instance-id i-0123456789abcdef0 --instance-os-user ec2-user --ssh-public-key file://$HOME/.ssh/my-ec2-key.pub
ssh -i ~/.ssh/my-ec2-key ec2-user@$IP "cd /opt/trading-bot && python generate_token.py"
```
