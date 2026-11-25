#!/usr/bin/env bash
# Runs inside GitHub Actions (the "deploy" workflow), never on the server.
#
# Sends compose.yml, Caddyfile and deploy.sh to the EC2 instance and runs
# "deploy.sh <tag>" there, through AWS Systems Manager: no SSH port, no stored keys.
# Then waits for the result and fails the workflow if the deploy failed.
#
# Needs AWS credentials in the environment (from the workflow's OIDC role), and
# INSTANCE_ID and TAG.
set -euo pipefail

: "${INSTANCE_ID:?INSTANCE_ID is not set (repository variable EC2_INSTANCE_ID)}"
: "${TAG:?TAG is not set}"
if [[ ! "$TAG" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "Refusing an image tag with unexpected characters: $TAG" >&2
  exit 2
fi

cd "$(dirname "$0")"
payload=$(tar -czf - compose.yml Caddyfile deploy.sh | base64 -w0)

params=$(jq -n --arg payload "$payload" --arg tag "$TAG" '{
  commands: [
    "set -eu",
    "mkdir -p /opt/klciie",
    ("echo " + $payload + " | base64 -d | tar -xzf - --no-same-owner -C /opt/klciie"),
    "chmod 700 /opt/klciie/deploy.sh",
    ("/opt/klciie/deploy.sh " + $tag)
  ],
  executionTimeout: ["900"]
}')

command_id=$(aws ssm send-command \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --comment "deploy ${TAG:0:40}" \
  --parameters "$params" \
  --query Command.CommandId --output text)
echo "Sent deploy of $TAG to $INSTANCE_ID (command $command_id). Waiting..."

status=Pending
for _ in $(seq 1 200); do
  sleep 5
  status=$(aws ssm get-command-invocation --command-id "$command_id" --instance-id "$INSTANCE_ID" \
    --query Status --output text 2> /dev/null || echo Pending)
  case "$status" in
    Pending | InProgress | Delayed) ;;
    *) break ;;
  esac
done

echo "----- output from the server -----"
aws ssm get-command-invocation --command-id "$command_id" --instance-id "$INSTANCE_ID" \
  --query StandardOutputContent --output text || true
echo "----- errors from the server -----"
aws ssm get-command-invocation --command-id "$command_id" --instance-id "$INSTANCE_ID" \
  --query StandardErrorContent --output text || true
echo "----------------------------------"

if [ "$status" != Success ]; then
  echo "Deploy finished with status: $status" >&2
  exit 1
fi
echo "Deploy succeeded."
