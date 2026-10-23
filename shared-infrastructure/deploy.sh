#!/usr/bin/env bash
# Shared infrastructure setup for the receipt digitization mini project.
# Run in AWS CloudShell, inside the shared-infrastructure folder:
#
#   bash deploy.sh your-email@example.com
#
# Safe to run again: the stack is updated in place and an existing budget is left alone.

set -euo pipefail

EMAIL="${1:?Usage: bash deploy.sh your-email@example.com}"
STACK="receipt-shared"
BUDGET_NAME="receipt-pipeline-budget"
BUDGET_USD="5"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION="${AWS_REGION:-$(aws configure get region || echo unknown)}"

echo "Account: $ACCOUNT_ID   Region: $REGION"
echo

echo "[1/3] Creating S3 bucket, DynamoDB table and SNS topic..."
aws cloudformation deploy \
  --stack-name "$STACK" \
  --template-file template.yaml \
  --parameter-overrides "NotificationEmail=$EMAIL" \
  --tags Project=receipt-pipeline \
  --no-fail-on-empty-changeset
echo

echo "[2/3] Creating a \$$BUDGET_USD monthly budget with email alerts..."
# IncludeCredit=false makes the budget track usage BEFORE free credits are applied,
# so you get warned about real consumption instead of always seeing \$0.
if aws budgets create-budget \
    --account-id "$ACCOUNT_ID" \
    --budget "{
      \"BudgetName\": \"$BUDGET_NAME\",
      \"BudgetLimit\": {\"Amount\": \"$BUDGET_USD\", \"Unit\": \"USD\"},
      \"TimeUnit\": \"MONTHLY\",
      \"BudgetType\": \"COST\",
      \"CostTypes\": {\"IncludeCredit\": false}
    }" \
    --notifications-with-subscribers "[
      {
        \"Notification\": {
          \"NotificationType\": \"ACTUAL\",
          \"ComparisonOperator\": \"GREATER_THAN\",
          \"Threshold\": 50,
          \"ThresholdType\": \"PERCENTAGE\"
        },
        \"Subscribers\": [{\"SubscriptionType\": \"EMAIL\", \"Address\": \"$EMAIL\"}]
      },
      {
        \"Notification\": {
          \"NotificationType\": \"ACTUAL\",
          \"ComparisonOperator\": \"GREATER_THAN\",
          \"Threshold\": 100,
          \"ThresholdType\": \"PERCENTAGE\"
        },
        \"Subscribers\": [{\"SubscriptionType\": \"EMAIL\", \"Address\": \"$EMAIL\"}]
      }
    ]" 2>/tmp/budget_error.txt; then
  echo "Budget created: alerts at 50% and 100% of \$$BUDGET_USD."
else
  if grep -q "DuplicateRecordException" /tmp/budget_error.txt; then
    echo "Budget already exists, leaving it as is."
  else
    echo "Budget step did not complete (the shared resources above are unaffected):"
    cat /tmp/budget_error.txt
  fi
fi
echo

echo "[3/3] Done. Note these values, the next steps need them:"
aws cloudformation describe-stacks \
  --stack-name "$STACK" \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" \
  --output table
echo
echo "NEXT: open the email from AWS Notifications sent to $EMAIL and click 'Confirm subscription'."
