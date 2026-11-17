#!/bin/sh
# Runs on the server (as root): unpack a release, rebuild the API and wait until it is healthy.
#   /opt/fra/server.env   production settings (kept between releases)
#   /opt/fra/app          the running release
set -eu
ARCHIVE=${1:?path to the release archive}
REV=${2:-unknown}
ROOT=/opt/fra
COMPOSE="docker compose -f compose.minimal.yml -f compose.production.yml"

[ -f "$ROOT/server.env" ] || { echo "$ROOT/server.env is missing." >&2; exit 1; }

rm -rf "$ROOT/app.new"
mkdir -p "$ROOT/app.new"
tar xzf "$ARCHIVE" -C "$ROOT/app.new"
cp "$ROOT/server.env" "$ROOT/app.new/.env"
# Backup status for the admin Health page (deploy/backup.sh writes it; the API reads it).
mkdir -p "$ROOT/status" && chmod 755 "$ROOT/status"
chmod 600 "$ROOT/app.new/.env"
echo "$REV" > "$ROOT/app.new/REVISION"
# /version reports which release is running.
echo "GIT_SHA=$REV" >> "$ROOT/app.new/.env"

# Build before switching, so a failed build leaves the running release alone.
cd "$ROOT/app.new"
$COMPOSE build api

rm -rf "$ROOT/app.old"
[ -d "$ROOT/app" ] && mv "$ROOT/app" "$ROOT/app.old"
mv "$ROOT/app.new" "$ROOT/app"
cd "$ROOT/app"
$COMPOSE up -d --remove-orphans

echo "Waiting for the API to report healthy..."
i=0
until [ "$($COMPOSE ps api --format '{{.Health}}' 2>/dev/null)" = "healthy" ]; do
  i=$((i + 1))
  if [ "$i" -gt 90 ]; then
    echo "The API did not become healthy in time. Recent log:" >&2
    $COMPOSE logs --tail 40 api >&2
    exit 1
  fi
  sleep 5
done
rm -rf "$ROOT/app.old"
docker image prune -f >/dev/null
docker builder prune -f --filter until=72h >/dev/null
echo "Release $REV is live."
