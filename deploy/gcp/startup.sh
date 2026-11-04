#!/bin/bash
# Gary's VM boot script (ARCHITECTURE.md §11), run as root by Container-Optimized OS on
# every boot. Each boot pulls the newest image, so rebooting the VM is how it updates.
#
# Secrets never touch the repo or the VM's metadata: the env file is read from Secret Manager
# with the VM's own service account. The image is public, so pulling it needs no login.
set -euo pipefail

IMAGE=ghcr.io/magnentius/gary:latest
HOME_DIR=/var/lib/gary              # /var survives reboots on Container-Optimized OS
export DOCKER_CONFIG=$HOME_DIR/docker  # /root is read-only there

meta() { curl -sf -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/$1"; }

secret() {
  local token project
  token=$(meta instance/service-accounts/default/token | sed -E 's/.*"access_token":"([^"]+)".*/\1/')
  project=$(meta project/project-id)
  curl -sf -H "Authorization: Bearer $token" \
    "https://secretmanager.googleapis.com/v1/projects/$project/secrets/$1/versions/latest:access" \
    | grep -o '"data": *"[^"]*"' | cut -d'"' -f4 | base64 -d
}

mkdir -p "$HOME_DIR/campaign" "$DOCKER_CONFIG"
chown 10001:10001 "$HOME_DIR/campaign"   # the image runs as uid 10001
chmod 700 "$HOME_DIR" "$DOCKER_CONFIG"

umask 077
secret gary-env > "$HOME_DIR/env"
# Forget any old ghcr.io login: a stored token that has expired or been revoked would make
# the pull fail, where no login at all succeeds.
docker logout ghcr.io >/dev/null 2>&1 || true

docker pull "$IMAGE"
docker rm -f gary 2>/dev/null || true
docker run -d --name gary \
  --restart unless-stopped --init --stop-timeout 30 \
  --env-file "$HOME_DIR/env" \
  -v "$HOME_DIR/campaign:/campaign" \
  --log-driver gcplogs \
  "$IMAGE"
docker image prune -f >/dev/null
echo "gary started from $(docker inspect --format '{{.Image}}' gary)"
