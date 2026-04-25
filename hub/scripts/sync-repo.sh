#!/bin/sh
set -eu

CONFIG_FILE="${1:-/etc/node-control/hub.conf}"

[ -f "$CONFIG_FILE" ] || { echo "config not found: $CONFIG_FILE" >&2; exit 1; }
. "$CONFIG_FILE"

: "${REPO_ROOT:?missing REPO_ROOT}"

GIT_REMOTE="${GIT_REMOTE:-origin}"
GIT_BRANCH="${GIT_BRANCH:-main}"
HUB_DIR="${HUB_DIR:-$REPO_ROOT/hub}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.host.yml}"
HUB_ENV_FILE="${HUB_ENV_FILE:-/etc/node-control/hub.env}"

[ -d "$REPO_ROOT/.git" ] || { echo "repo is not a git checkout: $REPO_ROOT" >&2; exit 1; }
[ -d "$HUB_DIR" ] || { echo "hub dir not found: $HUB_DIR" >&2; exit 1; }
[ -f "$HUB_DIR/$COMPOSE_FILE" ] || { echo "compose file not found: $HUB_DIR/$COMPOSE_FILE" >&2; exit 1; }

COMPOSE_ENV_ARG=""
if [ -f "$HUB_ENV_FILE" ]; then
  COMPOSE_ENV_ARG="--env-file $HUB_ENV_FILE"
fi

LOCAL_REV="$(git -C "$REPO_ROOT" rev-parse HEAD)"
git -C "$REPO_ROOT" fetch "$GIT_REMOTE" "$GIT_BRANCH" --quiet
REMOTE_REV="$(git -C "$REPO_ROOT" rev-parse FETCH_HEAD)"

if [ "$LOCAL_REV" = "$REMOTE_REV" ]; then
  echo "hub sync: no repo changes ($LOCAL_REV)"
else
  git -C "$REPO_ROOT" pull --ff-only "$GIT_REMOTE" "$GIT_BRANCH" --quiet
  echo "hub sync: updated repo from $LOCAL_REV to $REMOTE_REV"
fi

cd "$HUB_DIR"
if docker compose $COMPOSE_ENV_ARG -f "$COMPOSE_FILE" run --rm hub /bin/sh /opt/node-control/repo/hub/scripts/process-observed.sh /etc/node-control/hub.conf; then
  echo "hub sync: observed data processed"
else
  echo "hub sync: observed data processing failed, continuing with last approved delta" >&2
fi
docker compose $COMPOSE_ENV_ARG -f "$COMPOSE_FILE" run --rm hub /bin/sh /opt/node-control/repo/hub/scripts/build-runtime.sh /etc/node-control/hub.conf
docker compose $COMPOSE_ENV_ARG -f "$COMPOSE_FILE" run --rm hub /bin/sh /opt/node-control/repo/hub/scripts/publish-runtime.sh /etc/node-control/hub.conf

echo "hub sync: runtime refreshed"
