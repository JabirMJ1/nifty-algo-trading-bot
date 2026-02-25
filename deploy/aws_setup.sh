#!/bin/bash
# One-time AWS infrastructure setup.
# Run this from your local machine with AWS CLI configured.
# Prerequisites: aws cli v2, jq
#
# Usage:
#   chmod +x deploy/aws_setup.sh
#   ./deploy/aws_setup.sh
#
# Fill in the variables below before running.

set -euo pipefail

# ── Config — edit these ───────────────────────────────────────────────────────
REGION="ap-south-1"
INSTANCE_NAME="trading-bot"
KEY_PAIR_NAME="my-ec2-key"        # will be created; private key saved locally
BUDGET_LIMIT_USD="10"                  # hard monthly cap — instance stops if exceeded
ALERT_EMAIL="you@example.com"           # where to send cost alerts
GITHUB_REPO="JabirMJ1/trading-bot"    # for the deploy key instruction at the end
# ─────────────────────────────────────────────────────────────────────────────

echo "=== [1/7] Creating key pair ==="
aws ec2 create-key-pair \
  --region "$REGION" \
  --key-name "$KEY_PAIR_NAME" \
  --query "KeyMaterial" \
  --output text > "${KEY_PAIR_NAME}.pem"
chmod 400 "${KEY_PAIR_NAME}.pem"
echo "  Private key saved to ${KEY_PAIR_NAME}.pem — add this to GitHub secret EC2_SSH_KEY"

echo "=== [2/7] Creating security group ==="
SG_ID=$(aws ec2 create-security-group \
  --region "$REGION" \
  --group-name "${INSTANCE_NAME}-sg" \
  --description "Trading bot security group" \
  --query "GroupId" --output text)

# SSH only from your current IP
MY_IP=$(curl -s https://checkip.amazonaws.com)/32
aws ec2 authorize-security-group-ingress \
  --region "$REGION" \
  --group-id "$SG_ID" \
  --protocol tcp --port 22 --cidr "$MY_IP"
echo "  Security group: $SG_ID (SSH allowed from $MY_IP only)"

echo "=== [3/7] Launching EC2 t3a.micro (Amazon Linux 2023) ==="
# Latest Amazon Linux 2023 AMI in ap-south-1
AMI_ID=$(aws ec2 describe-images \
  --region "$REGION" \
  --owners amazon \
  --filters "Name=name,Values=al2023-ami-*-x86_64" \
            "Name=state,Values=available" \
  --query "sort_by(Images, &CreationDate)[-1].ImageId" \
  --output text)

INSTANCE_ID=$(aws ec2 run-instances \
  --region "$REGION" \
  --image-id "$AMI_ID" \
  --instance-type t3a.micro \
  --key-name "$KEY_PAIR_NAME" \
  --security-group-ids "$SG_ID" \
  --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":20,"VolumeType":"gp3"}}]' \
  --user-data "file://deploy/userdata.sh" \
  --iam-instance-profile Name="${INSTANCE_NAME}-profile" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${INSTANCE_NAME}}]" \
  --query "Instances[0].InstanceId" \
  --output text)

echo "  Instance: $INSTANCE_ID"
echo "  Waiting for running state..."
aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"

PUBLIC_IP=$(aws ec2 describe-instances \
  --region "$REGION" \
  --instance-ids "$INSTANCE_ID" \
  --query "Reservations[0].Instances[0].PublicIpAddress" \
  --output text)
echo "  Public IP: $PUBLIC_IP — add this to GitHub secret EC2_HOST"

echo "=== [4/7] Creating IAM role for EC2 (Secrets Manager access) ==="
aws iam create-role \
  --role-name "${INSTANCE_NAME}-role" \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]
  }' > /dev/null

aws iam attach-role-policy \
  --role-name "${INSTANCE_NAME}-role" \
  --policy-arn arn:aws:iam::aws:policy/SecretsManagerReadWrite

aws iam create-instance-profile \
  --instance-profile-name "${INSTANCE_NAME}-profile" > /dev/null
aws iam add-role-to-instance-profile \
  --instance-profile-name "${INSTANCE_NAME}-profile" \
  --role-name "${INSTANCE_NAME}-role"

echo "=== [5/7] EventBridge rules — start 8:45 AM IST, stop 4:00 PM IST (Mon-Fri) ==="
# IST = UTC+5:30, so:
#   8:45 AM IST = 03:15 UTC
#   4:00 PM IST = 10:30 UTC

# IAM role for EventBridge to start/stop EC2
aws iam create-role \
  --role-name "${INSTANCE_NAME}-scheduler-role" \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole"}]
  }' > /dev/null

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws iam put-role-policy \
  --role-name "${INSTANCE_NAME}-scheduler-role" \
  --policy-name "StartStopEC2" \
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",
      \"Action\":[\"ec2:StartInstances\",\"ec2:StopInstances\"],
      \"Resource\":\"arn:aws:ec2:${REGION}:${ACCOUNT_ID}:instance/${INSTANCE_ID}\"
    }]
  }"

SCHEDULER_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${INSTANCE_NAME}-scheduler-role"

# Start rule: 8:45 AM IST Mon-Fri
aws scheduler create-schedule \
  --region "$REGION" \
  --name "${INSTANCE_NAME}-start" \
  --schedule-expression "cron(15 3 ? * MON-FRI *)" \
  --schedule-expression-timezone "Asia/Kolkata" \
  --flexible-time-window Mode=OFF \
  --target "{
    \"Arn\": \"arn:aws:scheduler:::aws-sdk:ec2:startInstances\",
    \"RoleArn\": \"${SCHEDULER_ROLE_ARN}\",
    \"Input\": \"{\\\"InstanceIds\\\":[\\\"${INSTANCE_ID}\\\"]}\"
  }"

# Stop rule: 4:00 PM IST Mon-Fri
aws scheduler create-schedule \
  --region "$REGION" \
  --name "${INSTANCE_NAME}-stop" \
  --schedule-expression "cron(30 10 ? * MON-FRI *)" \
  --schedule-expression-timezone "Asia/Kolkata" \
  --flexible-time-window Mode=OFF \
  --target "{
    \"Arn\": \"arn:aws:scheduler:::aws-sdk:ec2:stopInstances\",
    \"RoleArn\": \"${SCHEDULER_ROLE_ARN}\",
    \"Input\": \"{\\\"InstanceIds\\\":[\\\"${INSTANCE_ID}\\\"]}\"
  }"

echo "  Start schedule: 08:45 IST Mon-Fri"
echo "  Stop  schedule: 16:00 IST Mon-Fri"

echo "=== [6/7] Budget: \$${BUDGET_LIMIT_USD}/month hard cap — stops instance if breached ==="
BUDGET_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/aws-budgets-sns-policy"  # AWS managed

# SNS topic for email alert
SNS_ARN=$(aws sns create-topic --name "${INSTANCE_NAME}-cost-alert" \
  --region "us-east-1" --query TopicArn --output text)  # Budgets always uses us-east-1
aws sns subscribe --topic-arn "$SNS_ARN" \
  --protocol email --notification-endpoint "$ALERT_EMAIL" \
  --region "us-east-1" > /dev/null
echo "  Check your email and confirm the SNS subscription."

# IAM policy so Budgets can stop the instance
aws iam create-policy \
  --policy-name "${INSTANCE_NAME}-budget-stop-policy" \
  --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[{
      \"Effect\":\"Allow\",
      \"Action\":[\"ec2:StopInstances\"],
      \"Resource\":\"arn:aws:ec2:${REGION}:${ACCOUNT_ID}:instance/${INSTANCE_ID}\"
    }]
  }" > /dev/null

aws budgets create-budget \
  --account-id "$ACCOUNT_ID" \
  --budget "{
    \"BudgetName\": \"${INSTANCE_NAME}-monthly-cap\",
    \"BudgetLimit\": {\"Amount\": \"${BUDGET_LIMIT_USD}\", \"Unit\": \"USD\"},
    \"TimeUnit\": \"MONTHLY\",
    \"BudgetType\": \"COST\",
    \"CostFilters\": {
      \"TagKeyValue\": [\"user:Name\$${INSTANCE_NAME}\"]
    }
  }" \
  --notifications-with-subscribers "[
    {
      \"Notification\": {
        \"NotificationType\": \"ACTUAL\",
        \"ComparisonOperator\": \"GREATER_THAN\",
        \"Threshold\": 80,
        \"ThresholdType\": \"PERCENTAGE\"
      },
      \"Subscribers\": [{\"SubscriptionType\": \"SNS\", \"Address\": \"${SNS_ARN}\"}]
    },
    {
      \"Notification\": {
        \"NotificationType\": \"ACTUAL\",
        \"ComparisonOperator\": \"GREATER_THAN\",
        \"Threshold\": 100,
        \"ThresholdType\": \"PERCENTAGE\"
      },
      \"Subscribers\": [{\"SubscriptionType\": \"SNS\", \"Address\": \"${SNS_ARN}\"}]
    }
  ]"

echo "  Budget: alert at 80% (\$8), hard stop at 100% (\$${BUDGET_LIMIT_USD})"
echo "  Note: AWS Budgets alerts are near-realtime but not instant (up to 24hr delay)."
echo "  For a truly instant kill-switch, manually stop the instance from the console."

echo "=== [7/7] Done ==="
echo ""
echo "Next steps:"
echo "  1. Add these GitHub repository secrets:"
echo "       EC2_HOST              = ${PUBLIC_IP}"
echo "       EC2_INSTANCE_ID       = ${INSTANCE_ID}"
echo "       EC2_SSH_KEY           = (contents of ${KEY_PAIR_NAME}.pem)"
echo "       AWS_ACCESS_KEY_ID     = (IAM user key — see step below)"
echo "       AWS_SECRET_ACCESS_KEY = (IAM user secret — see step below)"
echo ""
echo "     Create a least-privilege IAM user for GitHub Actions:"
echo "       aws iam create-user --user-name github-actions-deploy"
echo "       aws iam attach-user-policy --user-name github-actions-deploy \\"
echo "         --policy-arn arn:aws:iam::aws:policy/AmazonEC2FullAccess"
echo "       aws iam create-access-key --user-name github-actions-deploy"
echo ""
echo "  2. Store your .env in Secrets Manager:"
echo "       aws secretsmanager create-secret \\"
echo "         --name trading-bot/env \\"
echo "         --region ${REGION} \\"
echo "         --secret-string file://.env"
echo ""
echo "  3. Confirm the SNS email subscription (check ${ALERT_EMAIL})"
echo ""
echo "  4. On market mornings, SSH in and refresh the Kite token:"
echo "       ssh -i ${KEY_PAIR_NAME}.pem ec2-user@${PUBLIC_IP}"
echo "       cd /opt/trading-bot && python generate_token.py"
echo "       aws secretsmanager update-secret --name trading-bot/env --secret-string file://.env --region ${REGION}"
echo "       docker compose restart bot"
