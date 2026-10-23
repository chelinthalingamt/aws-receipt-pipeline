#!/usr/bin/env bash
# Test the EC2 pipeline: upload one receipt to ec2/ and show what came out.
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
KEY="ec2/$(basename "$FILE")"
STARTED=$(date -u +%Y-%m-%dT%H:%M:%SZ)

INSTANCE_ID=$(aws cloudformation describe-stacks --stack-name receipt-ec2 \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)
STATE=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query "Reservations[0].Instances[0].State.Name" --output text)
if [ "$STATE" != "running" ]; then
  echo "The server is '$STATE', so nothing will process the file. Run: bash ec2-power.sh start" >&2
  exit 1
fi

echo "[1/3] Uploading $FILE to s3://$BUCKET/$KEY"
aws s3 cp "$FILE" "s3://$BUCKET/$KEY" --only-show-errors

echo "[2/3] Waiting for the server to notice and process it (it checks every 10 s)..."
VALUES="{\":k\": {\"S\": \"$KEY\"}, \":t\": {\"S\": \"$STARTED\"}, \":s\": {\"S\": \"ec2\"}}"
FILTER="s3_key = :k AND processed_at >= :t AND #src = :s"
FOUND=0
for _ in $(seq 1 30); do
  sleep 6
  FOUND=$(aws dynamodb scan --table-name "$TABLE" \
    --filter-expression "$FILTER" --expression-attribute-names '{"#src": "source"}' \
    --expression-attribute-values "$VALUES" --query "Count" --output text)
  [ "$FOUND" != "0" ] && break
done

if [ "$FOUND" = "0" ]; then
  echo "No result after 3 minutes. If the server was created just now it may still be"
  echo "installing; otherwise check its logs with:  bash ec2-power.sh logs"
  exit 1
fi

echo "[3/3] Item saved in DynamoDB by the EC2 version:"
aws dynamodb scan --table-name "$TABLE" \
  --filter-expression "$FILTER" --expression-attribute-names '{"#src": "source"}' \
  --expression-attribute-values "$VALUES" \
  --query "Items[0].{receipt_id:receipt_id.S, vendor:vendor.S, amount:amount.N, currency:currency.S, date:receipt_date.S, status:status.S, source:source.S, ms:processing_ms.N}" \
  --output table
echo
echo "Also check your inbox: the email says 'Processed by ec2'."
