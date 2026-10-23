#!/usr/bin/env bash
# Serverless version (Person A): build and deploy the serverless receipt processor.
# Run in AWS CloudShell, inside the serverless-lambda folder:
#
#   bash deploy.sh
#
# Needs the shared-infrastructure stack (receipt-shared) to exist in the same region.
# Safe to run again: after editing lambda_function.py, re-run to redeploy.

set -euo pipefail

SHARED_STACK="receipt-shared"
STACK="receipt-serverless"

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

echo "[1/4] Packaging the function code with its PDF library..."
rm -rf build function.zip
mkdir build
# pypdf is pure Python, so the copy installed here runs unchanged on Lambda.
python3 -m pip install --quiet --target build --python-version 3.13 --only-binary=:all: pypdf 2>/dev/null \
  || python3 -m pip install --quiet --target build pypdf
cp lambda_function.py build/
# Zip with fixed timestamps so unchanged code always produces the same file.
python3 - <<'PY'
import os, zipfile
with zipfile.ZipFile("function.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk("build"):
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", "bin"))
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            path = os.path.join(root, name)
            info = zipfile.ZipInfo(os.path.relpath(path, "build"), date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(path, "rb") as fh:
                zf.writestr(info, fh.read())
PY
CODE_KEY="deploy/receipt-lambda-$(sha256sum function.zip | cut -c1-12).zip"
echo "Package: function.zip ($(du -h function.zip | cut -f1))"
echo

echo "[2/4] Uploading the package to s3://$BUCKET/$CODE_KEY ..."
aws s3 cp function.zip "s3://$BUCKET/$CODE_KEY" --only-show-errors
echo

echo "[3/4] Creating the IAM role and Lambda function..."
aws cloudformation deploy \
  --stack-name "$STACK" \
  --template-file template.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      "BucketName=$BUCKET" "TableName=$TABLE" "TopicArn=$TOPIC" "CodeS3Key=$CODE_KEY" \
  --tags Project=receipt-pipeline \
  --no-fail-on-empty-changeset
echo

echo "[4/4] Attaching the S3 trigger (new files under serverless/ invoke the function)..."
FUNCTION_ARN=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='FunctionArn'].OutputValue" --output text)
aws s3api put-bucket-notification-configuration \
  --bucket "$BUCKET" \
  --notification-configuration "{
    \"LambdaFunctionConfigurations\": [{
      \"Id\": \"receipt-serverless-trigger\",
      \"LambdaFunctionArn\": \"$FUNCTION_ARN\",
      \"Events\": [\"s3:ObjectCreated:*\"],
      \"Filter\": {\"Key\": {\"FilterRules\": [{\"Name\": \"prefix\", \"Value\": \"serverless/\"}]}}
    }]
  }"
echo "Trigger attached."
echo

echo "Done. Serverless pipeline is live:"
aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
echo
echo "NEXT: test it with   bash test.sh"
