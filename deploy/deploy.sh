#!/bin/sh
# Ship the committed code to the server and rebuild the API there (a minute or so of downtime).
# Accounts, conversations and the knowledge base live in Docker volumes and are not touched.
#
#   make deploy            # or: sh deploy/deploy.sh
#
# The target comes from deploy/target.env (see deploy/target.env.example).
set -eu
cd "$(dirname "$0")/.."

if [ ! -f deploy/target.env ]; then
  echo "Create deploy/target.env from deploy/target.env.example first." >&2
  exit 1
fi
. deploy/target.env

if ! git diff --quiet HEAD; then
  echo "There are uncommitted changes; only committed code is deployed. Commit or stash them first." >&2
  exit 1
fi

REV=$(git rev-parse --short HEAD)
ARCHIVE=$(mktemp -t fra-release).tar.gz
trap 'rm -f "$ARCHIVE"' EXIT
git archive --format=tar.gz -o "$ARCHIVE" HEAD

echo "Deploying $REV to $GCP_INSTANCE ($GCP_PROJECT, $GCP_ZONE)..."
gcloud compute scp "$ARCHIVE" "$GCP_INSTANCE:/tmp/fra-release.tar.gz" \
  --project "$GCP_PROJECT" --zone "$GCP_ZONE" --quiet
gcloud compute ssh "$GCP_INSTANCE" --project "$GCP_PROJECT" --zone "$GCP_ZONE" --quiet \
  --command "sudo sh -c 'tar xzf /tmp/fra-release.tar.gz -O deploy/server-update.sh > /opt/fra/server-update.sh && sh /opt/fra/server-update.sh /tmp/fra-release.tar.gz $REV'"
