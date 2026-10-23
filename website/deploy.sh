#!/usr/bin/env bash
# Receipt pipeline website: deploy it, or take it offline.
# Run in AWS CloudShell, inside the website folder:
#
#   bash deploy.sh        # build and deploy; prints the website address
#   bash deploy.sh off    # take the website offline (deletes only the website)
#
# Needs the shared-infrastructure and serverless-lambda stacks in the same region.

set -euo pipefail

STACK="receipt-web"
FUNCTION="receipt-pipeline-web"

shared_output() {
  aws cloudformation describe-stacks --stack-name receipt-shared \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
BUCKET=$(shared_output BucketName)
TABLE=$(shared_output TableName)
[ -n "$BUCKET" ] && [ -n "$TABLE" ] || { echo "shared-infrastructure stack not found in this region." >&2; exit 1; }

if [ "${1:-}" = "off" ]; then
  echo "Taking the website offline..."
  aws cloudformation delete-stack --stack-name "$STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK"
  aws s3api delete-bucket-cors --bucket "$BUCKET" || true
  echo "Done. The pipeline itself is untouched; run 'bash deploy.sh' to bring the website back."
  exit 0
fi

echo "[1/5] Packaging the website code..."
rm -f web.zip
python3 - <<'PY'
import zipfile
with zipfile.ZipFile("web.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    for name in ("web_function.py", "index.html"):
        info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        with open(name, "rb") as fh:
            zf.writestr(info, fh.read())
PY
CODE_KEY="deploy/web-$(sha256sum web.zip | cut -c1-12).zip"
aws s3 cp web.zip "s3://$BUCKET/$CODE_KEY" --only-show-errors
echo

echo "[2/5] Creating the website function and its public address..."
aws cloudformation deploy \
  --stack-name "$STACK" \
  --template-file template.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides "BucketName=$BUCKET" "TableName=$TABLE" "CodeS3Key=$CODE_KEY" \
  --tags Project=receipt-pipeline \
  --no-fail-on-empty-changeset
URL=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='WebsiteUrl'].OutputValue" --output text)
echo

echo "[3/5] Allowing the public to open the address (both permissions AWS requires)..."
aws lambda remove-permission --function-name "$FUNCTION" --statement-id PublicUrlAccess >/dev/null 2>&1 || true
aws lambda remove-permission --function-name "$FUNCTION" --statement-id PublicUrlInvoke >/dev/null 2>&1 || true
aws lambda add-permission --function-name "$FUNCTION" --statement-id PublicUrlAccess \
  --action lambda:InvokeFunctionUrl --principal "*" --function-url-auth-type NONE >/dev/null
if ! aws lambda add-permission --function-name "$FUNCTION" --statement-id PublicUrlInvoke \
      --action lambda:InvokeFunction --principal "*" --invoked-via-function-url >/dev/null 2>/tmp/perm_err.txt; then
  echo "  This CLI does not know --invoked-via-function-url; adding the permission without that condition."
  aws lambda add-permission --function-name "$FUNCTION" --statement-id PublicUrlInvoke \
    --action lambda:InvokeFunction --principal "*" >/dev/null
fi
echo

echo "[4/5] Letting the page upload straight to the bucket (CORS)..."
ORIGIN="${URL%/}"
aws s3api put-bucket-cors --bucket "$BUCKET" --cors-configuration "{
  \"CORSRules\": [{
    \"AllowedOrigins\": [\"$ORIGIN\"],
    \"AllowedMethods\": [\"POST\"],
    \"AllowedHeaders\": [\"*\"],
    \"MaxAgeSeconds\": 3000
  }]
}"
echo

echo "[5/5] Checking the page answers..."
for _ in 1 2 3 4 5 6; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "$URL" || echo 000)
  [ "$CODE" = "200" ] && break
  sleep 5
done
if [ "$CODE" = "200" ]; then
  echo "Website is live:"
else
  echo "The page answered $CODE. Permissions can take a minute to apply; try the link shortly:"
fi
echo
echo "    $URL"
echo
echo "Anyone with this link can upload. Take it offline after your demo with:  bash deploy.sh off"
