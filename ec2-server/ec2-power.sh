#!/usr/bin/env bash
# Control the EC2 server so it only costs money while you are testing.
#
#   bash ec2-power.sh status    # is it running?
#   bash ec2-power.sh stop      # stop it (no compute charges while stopped)
#   bash ec2-power.sh start     # start it again (the poller starts by itself)
#   bash ec2-power.sh restart   # restart the poller with the latest code
#   bash ec2-power.sh logs      # last 40 lines of the poller's log

set -euo pipefail

ACTION="${1:-status}"
INSTANCE_ID=$(aws cloudformation describe-stacks --stack-name receipt-ec2 \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)

state() {
  aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query "Reservations[0].Instances[0].State.Name" --output text
}

run_on_server() {
  # Runs a shell command on the server through Systems Manager and prints its output.
  local cmd_id status
  cmd_id=$(aws ssm send-command --instance-ids "$INSTANCE_ID" \
    --document-name AWS-RunShellScript \
    --parameters "commands=[\"$1\"]" \
    --query "Command.CommandId" --output text)
  for _ in $(seq 1 15); do
    sleep 2
    status=$(aws ssm get-command-invocation --command-id "$cmd_id" --instance-id "$INSTANCE_ID" \
      --query "Status" --output text 2>/dev/null || echo Pending)
    case "$status" in Pending|InProgress|Delayed) continue ;; *) break ;; esac
  done
  aws ssm get-command-invocation --command-id "$cmd_id" --instance-id "$INSTANCE_ID" \
    --query "StandardOutputContent" --output text
}

case "$ACTION" in
  status)
    echo "Server $INSTANCE_ID is: $(state)" ;;
  stop)
    aws ec2 stop-instances --instance-ids "$INSTANCE_ID" --output text >/dev/null
    echo "Stopping $INSTANCE_ID ..."
    aws ec2 wait instance-stopped --instance-ids "$INSTANCE_ID"
    echo "Stopped. No compute charges until you start it again." ;;
  start)
    aws ec2 start-instances --instance-ids "$INSTANCE_ID" --output text >/dev/null
    echo "Starting $INSTANCE_ID ..."
    aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
    echo "Running. The poller starts automatically within about a minute." ;;
  restart)
    [ "$(state)" = "running" ] || { echo "Server is not running. Use: bash ec2-power.sh start" >&2; exit 1; }
    run_on_server "systemctl restart receipt-poller && systemctl is-active receipt-poller" ;;
  logs)
    [ "$(state)" = "running" ] || { echo "Server is not running. Use: bash ec2-power.sh start" >&2; exit 1; }
    run_on_server "journalctl -u receipt-poller -n 40 --no-pager" ;;
  *)
    echo "Usage: bash ec2-power.sh status|stop|start|restart|logs" >&2; exit 1 ;;
esac
