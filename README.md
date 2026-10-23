# Receipt Pipeline on AWS

A serverless receipt/invoice digitization pipeline (S3 → Lambda → DynamoDB + SNS), the same
pipeline on an always-on EC2 server, a measured comparison of the two, monitoring, and an
upload website. Region: Asia Pacific (Sydney), `ap-southeast-2`.

## Folders

| Folder | What it deploys | Command |
| --- | --- | --- |
| `shared-infrastructure/` | Shared S3 bucket, DynamoDB table, SNS topic, $5 budget | `bash deploy.sh you@example.com` |
| `serverless-lambda/` | Lambda function, IAM role, S3 trigger | `bash deploy.sh` then `bash test.sh` |
| `ec2-server/` | EC2 server running the same code | `bash deploy.sh` then `bash test.sh` |
| `monitoring/` | CloudWatch dashboard and error alarm | `bash monitoring.sh` |
| `comparison/` | Runs the same 12 receipts through both versions | `python3 compare.py` |
| `website/` | Public upload website | `bash deploy.sh` (and `bash deploy.sh off`) |

Deploy them in the order listed. Run every command from inside its own folder
(for example `cd website && bash deploy.sh`).

**The receipt-reading code lives in one place:** `serverless-lambda/lambda_function.py`.
`ec2-server/deploy.sh` copies it into the EC2 folder automatically, so edit only that file.

## One-time setup on Windows (about 30 minutes)

### 1. Install WSL (Ubuntu inside Windows)
Open **PowerShell as Administrator** and run:
```powershell
wsl --install
```
Restart when asked, then open **Ubuntu** from the Start menu and choose a Linux username and password.

### 2. Install the tools (in the Ubuntu window)
```bash
sudo apt update && sudo apt install -y python3-pip python3-boto3 unzip zip curl
curl -s "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip -q awscliv2.zip && sudo ./aws/install && rm -rf aws awscliv2.zip
aws --version        # must be 2.32.0 or newer for 'aws login'
```

### 3. Sign in to AWS (no access keys needed)
```bash
aws login --profile signin --remote
```
It prints a link: open it in your Windows browser, sign in with your normal AWS console
email and password, and paste the code it shows back into Ubuntu. When it asks for a
region, type `ap-southeast-2`.

Then make that sign-in the default for every tool, including Python (boto3):
```bash
cat >> ~/.aws/config <<'EOF'

[default]
credential_process = aws configure export-credentials --profile signin --format process
region = ap-southeast-2
EOF
aws sts get-caller-identity     # should print your account number
```

### 4. Open the project in VS Code
1. Install **VS Code** on Windows, and the **WSL** extension inside it.
2. Copy this project into Ubuntu and open it:
   ```bash
   mkdir -p ~/projects && cd ~/projects
   git clone https://github.com/<your-username>/receipt-pipeline.git
   cd receipt-pipeline && code .
   ```
   VS Code opens connected to Ubuntu ("WSL: Ubuntu" in the bottom-left corner).

## Everyday workflow

1. Open Ubuntu, `cd ~/projects/receipt-pipeline && code .`
2. Sign in if it has been more than 12 hours: `aws login --profile signin --remote`
3. Edit a file in VS Code and save.
4. In VS Code's terminal (Ctrl + `), redeploy the part you changed:

| You changed | Run |
| --- | --- |
| `serverless-lambda/lambda_function.py` | `cd serverless-lambda && bash deploy.sh`, then `cd ../ec2-server && bash deploy.sh` |
| `website/index.html` or `web_function.py` | `cd website && bash deploy.sh` |
| `ec2-server/poller.py` | `cd ec2-server && bash deploy.sh` |
| `monitoring/monitoring.yaml` | `cd monitoring && bash monitoring.sh` |

Every script is safe to run again: unchanged parts are left alone.

## Costs and safety
- The EC2 server is billed per hour while running: `cd ec2-server && bash ec2-power.sh stop`.
- Anyone with the website link can upload: take it offline after demos with `cd website && bash deploy.sh off`.
- A $5 monthly budget alert emails the subscribed address at 50% and 100%.
