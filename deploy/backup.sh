#!/bin/sh
# Runs on the server each night (see docs/deployment.md): dumps PostgreSQL (accounts, sessions,
# conversations, approvals) and keeps the last 14 days. The knowledge base is not included; it
# changes only when rebuilt, and is restored from its snapshot.
set -eu
ROOT=/opt/fra
mkdir -p "$ROOT/backups"
chmod 700 "$ROOT/backups"
cd "$ROOT/app"
OUT="$ROOT/backups/rag_agent-$(date +%F).dump"
docker compose -f compose.minimal.yml -f compose.production.yml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
find "$ROOT/backups" -name 'rag_agent-*.dump' -mtime +14 -delete
