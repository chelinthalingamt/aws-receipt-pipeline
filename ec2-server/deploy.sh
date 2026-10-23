#!/usr/bin/env bash
# EC2 version (Person B): build and deploy the EC2 (server-based) receipt processor.
# Run in AWS CloudShell, inside the ec2-server folder:
#
#   bash deploy.sh
#
# Needs the shared-infrastructure stack (receipt-shared) in the same region.
# Safe to run again: after editing poller.py or lambda_function.py, re-run it
# and the running server restarts with the new code.

set -euo pipefail

SHARED_STACK="receipt-shared"
STACK="receipt-ec2"

shared_output() {
  aws cloudformation describe-stacks --stack-name "$SHARED_STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

BUCKET=$(shared_output BucketName)
TABLE=$(shared_output TableName)
TOPIC=$(shared_output TopicArn)
if [ -z "$BUCKET" ] || [ -z "$TABLE" ] || [ -z "$TOPIC" ]; then
  echo "Could not read the shared-infrastructure outputs. Run shared-infrastructure/deploy.sh first, in this same region." >&2
  exit 1
fi
echo "Using bucket: $BUCKET"
echo

echo "[1/4] Packaging the server code (poller.py + the shared lambda_function.py)..."
# One source of truth: in the project folder, the shared code lives with the Lambda version.
if [ -f ../serverless-lambda/lambda_function.py ]; then
  cp ../serverless-lambda/lambda_function.py lambda_function.py
  echo "Using the shared code from ../serverless-lambda/lambda_function.py"
fi
rm -f ec2-code.zip
python3 - <<'PY'
import zipfile
with zipfile.ZipFile("ec2-code.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    for name in ("poller.py", "lambda_function.py"):
        info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        with open(name, "rb") as fh:
            zf.writestr(info, fh.read())
PY
echo

echo "[2/4] Uploading it to s3://$BUCKET/deploy/ec2-code.zip ..."
aws s3 cp ec2-code.zip "s3://$BUCKET/deploy/ec2-code.zip" --only-show-errors
echo

STACK_EXISTED=yes
aws cloudformation describe-stacks --stack-name "$STACK" >/dev/null 2>&1 || STACK_EXISTED=no

echo "[3/4] Creating the IAM role, security group and EC2 instance (takes 2-4 minutes)..."
aws cloudformation deploy \
  --stack-name "$STACK" \
  --template-file template.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides "BucketName=$BUCKET" "TableName=$TABLE" "TopicArn=$TOPIC" \
  --tags Project=receipt-pipeline \
  --no-fail-on-empty-changeset
INSTANCE_ID=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)
echo

echo "[4/4] Starting the poller with the latest code..."
if [ "$STACK_EXISTED" = "no" ]; then
  echo "New server: it installs Python libraries and starts the poller on first boot."
  echo "Give it about 3 minutes before testing."
else
  STATE=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query "Reservations[0].Instances[0].State.Name" --output text)
  if [ "$STATE" != "running" ]; then
    echo "The server is '$STATE'. Start it with: bash ec2-power.sh start"
  else
    # Restarting the service makes it download the code zip again.
    aws ssm send-command --instance-ids "$INSTANCE_ID" \
      --document-name AWS-RunShellScript \
      --parameters 'commands=["systemctl restart receipt-poller"]' \
      --comment "Restart receipt poller with new code" \
      --query "Command.CommandId" --output text >/dev/null \
      && echo "Poller restarted with the new code." \
      || echo "Could not reach the server yet; it picks up the new code on its next restart (bash ec2-power.sh restart)."
  fi
fi
echo

echo "Done. EC2 pipeline:"
aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
echo
echo "NEXT: test it with   bash test.sh"
echo "IMPORTANT: the server is billed every hour it runs. Stop it when not testing:  bash ec2-power.sh stop"
