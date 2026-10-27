#!/usr/bin/env bash
# Deploy the upgraded code and run every experiment, in one go (about 30 minutes).
# Run from the project folder in Ubuntu (WSL), after signing in:
#
#   aws login --profile signin --remote      # if it has been more than 12 hours
#   bash run_all.sh                          # deploy + all experiments
#   bash run_all.sh --no-website             # same, but leave the public website off
#
# Costs: about $3 of credits (Lambda, roughly 2,500 Rekognition reads, ~30 min of EC2).
# Results land in comparison/results/<run>/ ; update RUNS in comparison/analyze.py with the run name, then run it.

set -euo pipefail
cd "$(dirname "$0")"
WEBSITE=yes
[ "${1:-}" = "--no-website" ] && WEBSITE=no

step() { echo; echo "==================== $* ===================="; }

step "0/5 Checking the AWS sign-in"
aws sts get-caller-identity --query Account --output text >/dev/null \
  || { echo "Not signed in. Run: aws login --profile signin --remote"; exit 1; }
python3 -c "import boto3" 2>/dev/null || { echo "boto3 missing. Run: sudo apt install -y python3-boto3"; exit 1; }
echo "Signed in; region $(aws configure get region)."

step "1/5 Code checks (no AWS; the reported results all come from step 5, on AWS)"
python3 tests/test_batch.py
[ -f comparison/real-receipts/answers.json ] && [ -f comparison/real-receipts/sroie-000.jpg ] || python3 tests/fetch_datasets.py

step "2/5 Deploying the serverless version (Lambda)"
(cd serverless-lambda && bash deploy.sh)

step "3/5 Deploying the EC2 version's code"
(cd ec2-server && bash deploy.sh)

if [ "$WEBSITE" = "yes" ]; then
  step "4/5 Deploying the website with the spending dashboard"
  (cd website && bash deploy.sh)
else
  step "4/5 Website skipped (--no-website)"
fi

step "5/5 Running the experiments (about 25 minutes)"
(cd comparison && python3 experiments.py all)

echo
echo "Finished. Remember: the website is public while it is on - take it down after demos with"
echo "    cd website && bash deploy.sh off"
