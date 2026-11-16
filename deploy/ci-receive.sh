#!/bin/sh
# Runs on the server as the only command the GitHub Actions deploy key may run (see
# docs/deployment.md): it reads a release archive from standard input and installs it.
set -eu
REV=$(printf '%s' "${SSH_ORIGINAL_COMMAND:-}" | tr -cd 'a-f0-9' | cut -c1-12)
ARCHIVE=$(mktemp /tmp/fra-release.XXXXXX)
trap 'rm -f "$ARCHIVE"' EXIT
cat > "$ARCHIVE"
sudo /opt/fra/server-update.sh "$ARCHIVE" "${REV:-unknown}"
