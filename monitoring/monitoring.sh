#!/usr/bin/env bash
# CloudWatch dashboard and error alarm for the receipt pipeline.
# Run in AWS CloudShell, in the folder where monitoring.zip was unzipped:
#
#   bash monitoring.sh             # create or update the dashboard and alarm
#   bash monitoring.sh demo        # upload 6 receipts so the graphs have data (sends 6 emails)
#   bash monitoring.sh test-alarm  # make the alarm fire once, to show the alert email
#
# Safe to run again.

set -euo pipefail

ACTION="${1:-deploy}"
STACK="receipt-monitoring"
REGION="${AWS_REGION:-$(aws configure get region || echo ap-southeast-2)}"

shared_output() {
  aws cloudformation describe-stacks --stack-name receipt-shared \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

case "$ACTION" in
  deploy)
    TOPIC=$(shared_output TopicArn)
    [ -n "$TOPIC" ] || { echo "shared-infrastructure stack not found in this region." >&2; exit 1; }
    INSTANCE_ID=$(aws cloudformation describe-stacks --stack-name receipt-ec2 \
      --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text 2>/dev/null || true)
    [ -n "$INSTANCE_ID" ] && [ "$INSTANCE_ID" != "None" ] || INSTANCE_ID=none

    echo "[1/2] Creating the alarm and the dashboard..."
    aws cloudformation deploy \
      --stack-name "$STACK" \
      --template-file monitoring.yaml \
      --parameter-overrides "TopicArn=$TOPIC" "InstanceId=$INSTANCE_ID" \
      --tags Project=receipt-pipeline \
      --no-fail-on-empty-changeset
    echo
    echo "[2/2] Done. Open the dashboard here:"
    echo "https://${REGION}.console.aws.amazon.com/cloudwatch/home?region=${REGION}#dashboards/dashboard/receipt-pipeline"
    echo
    echo "Alarm emails go to the same addresses as the receipt emails."
    echo "No graphs yet? Run:  bash monitoring.sh demo" ;;

  demo)
    BUCKET=$(shared_output BucketName)
    [ -f samples/sample-invoice.pdf ] || { echo "samples/sample-invoice.pdf not found (copy it from serverless-lambda/samples)." >&2; exit 1; }
    STAMP=$(date +%H%M%S)
    echo "Uploading 5 copies of the sample invoice and 1 broken file to serverless/ ..."
    for i in 1 2 3 4 5; do
      aws s3 cp samples/sample-invoice.pdf "s3://$BUCKET/serverless/demo-$STAMP-$i.pdf" --only-show-errors
    done
    echo "this is not a real pdf" > broken-demo.pdf
    aws s3 cp broken-demo.pdf "s3://$BUCKET/serverless/demo-$STAMP-broken.pdf" --only-show-errors
    rm -f broken-demo.pdf
    echo "Done: expect 5 PROCESSED and 1 FAILED receipt, and 6 emails."
    echo "Metric graphs update within about 5 minutes; refresh the dashboard." ;;

  test-alarm)
    aws cloudwatch set-alarm-state --alarm-name receipt-pipeline-lambda-errors \
      --state-value ALARM --state-reason "Manual test from monitoring.sh"
    echo "Alarm set to ALARM: an 'ALARM: receipt-pipeline-lambda-errors' email is on its way."
    echo "It returns to OK by itself at the next check (within about 5 minutes) and sends an OK email." ;;

  *)
    echo "Usage: bash monitoring.sh [deploy|demo|test-alarm]" >&2; exit 1 ;;
esac
