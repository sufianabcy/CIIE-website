#!/bin/sh
# Deploy one image of the portal on this server, then check that it is healthy.
#
#   sudo /opt/klciie/deploy.sh <tag>      tag = a full git commit sha, or "main"
#
# The GitHub "deploy" workflow runs this through AWS Systems Manager after copying
# compose.yml, Caddyfile and this script into /opt/klciie. If the new version does not
# become healthy, it goes back to the previous tag and exits with an error.
set -eu
cd /opt/klciie

TAG="${1:-}"
case "$TAG" in
  "" | *[!A-Za-z0-9._-]*) echo "usage: $0 <image tag>" >&2; exit 2 ;;
esac
if [ ! -f .env ]; then
  echo "/opt/klciie/.env is missing." >&2
  exit 1
fi

set_tag() {
  if grep -q '^IMAGE_TAG=' .env; then
    sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=$1/" .env
  else
    printf '\nIMAGE_TAG=%s\n' "$1" >> .env
  fi
}

PREVIOUS=$(sed -n 's/^IMAGE_TAG=//p' .env | tail -n 1)
echo "Deploying $TAG (running before: ${PREVIOUS:-nothing})"
set_tag "$TAG"

# --wait returns once every service is running and web passes its health check.
if docker compose pull --quiet web worker \
  && docker compose up -d --remove-orphans --wait --wait-timeout 300; then
  # Keep a week of old images on disk so a rollback starts fast.
  docker image prune --all --force --filter "until=168h" > /dev/null 2>&1 || true
  echo "Deployed $TAG. The site is healthy."
  exit 0
fi

echo "Deploy of $TAG failed. Last lines from web:" >&2
docker compose logs --no-color --tail 80 web >&2 || true
if [ -n "$PREVIOUS" ] && [ "$PREVIOUS" != "$TAG" ]; then
  echo "Going back to $PREVIOUS." >&2
  set_tag "$PREVIOUS"
  docker compose up -d --remove-orphans --wait --wait-timeout 300 || true
fi
exit 1
