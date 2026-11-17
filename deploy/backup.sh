#!/bin/sh
# Runs on the server each night (see docs/deployment.md): dumps PostgreSQL (accounts, sessions,
# conversations, approvals, monitoring) and keeps the last 14 days. The knowledge base is not included; it
# changes only when rebuilt, and is restored from its snapshot.
set -eu
umask 077
ROOT=/opt/fra
mkdir -p "$ROOT/backups"
chmod 700 "$ROOT/backups"
cd "$ROOT/app"
OUT="$ROOT/backups/rag_agent-$(date +%F).dump"
docker compose -f compose.minimal.yml -f compose.production.yml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
find "$ROOT/backups" -name 'rag_agent-*.dump' -mtime +14 -delete

# When and how big, for the admin Health page (the API reads this folder, never the backups).
mkdir -p "$ROOT/status"
chmod 755 "$ROOT/status"
printf '{"at": "%s", "bytes": %s}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(stat -c %s "$OUT")" > "$ROOT/status/backup.json.tmp"
chmod 644 "$ROOT/status/backup.json.tmp"
mv "$ROOT/status/backup.json.tmp" "$ROOT/status/backup.json"
