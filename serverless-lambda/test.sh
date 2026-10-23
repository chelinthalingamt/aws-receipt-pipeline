#!/usr/bin/env bash
# Test the serverless pipeline: upload one receipt and show what came out.
#
#   bash test.sh                      # uses the bundled sample invoice
#   bash test.sh my-receipt.pdf       # or any .pdf / .jpg / .jpeg / .png

set -euo pipefail

FILE="${1:-samples/sample-invoice.pdf}"
[ -f "$FILE" ] || { echo "File not found: $FILE" >&2; exit 1; }

shared_output() {
  aws cloudformation describe-stacks --stack-name receipt-shared \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
BUCKET=$(shared_output BucketName)
TABLE=$(shared_output TableName)
KEY="serverless/$(basename "$FILE")"
STARTED=$(date -u +%Y-%m-%dT%H:%M:%SZ)

echo "[1/3] Uploading $FILE to s3://$BUCKET/$KEY"
aws s3 cp "$FILE" "s3://$BUCKET/$KEY" --only-show-errors

echo "[2/3] Waiting for the function to process it..."
VALUES="{\":k\": {\"S\": \"$KEY\"}, \":t\": {\"S\": \"$STARTED\"}}"
FOUND=0
for _ in $(seq 1 10); do
  sleep 3
  FOUND=$(aws dynamodb scan --table-name "$TABLE" \
    --filter-expression "s3_key = :k AND processed_at >= :t" \
    --expression-attribute-values "$VALUES" --query "Count" --output text)
  [ "$FOUND" != "0" ] && break
done

if [ "$FOUND" = "0" ]; then
  echo "No result in DynamoDB after 30 seconds. Recent function logs:"
  aws logs tail /aws/lambda/receipt-pipeline-processor --since 5m --format short | tail -n 30
  exit 1
fi

echo "[3/3] Item saved in DynamoDB:"
aws dynamodb scan --table-name "$TABLE" \
  --filter-expression "s3_key = :k AND processed_at >= :t" \
  --expression-attribute-values "$VALUES" \
  --query "Items[0].{receipt_id:receipt_id.S, vendor:vendor.S, amount:amount.N, currency:currency.S, date:receipt_date.S, status:status.S, method:extraction_method.S, ms:processing_ms.N, note:note.S}" \
  --output table
echo
echo "Also check your inbox for the notification email (the SNS subscription must be confirmed)."
