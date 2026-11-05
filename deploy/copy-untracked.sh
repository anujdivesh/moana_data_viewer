#!/usr/bin/env bash
#
# Copy to the server what git doesn't carry (it's git-ignored on purpose):
#   deploy/.env   secrets and server settings, if you prepared them here
#   certs/        CA bundle, only needed if the server's network re-signs HTTPS
#   data          optional: a local data folder, to the server's DATA_PATH
#
#   ./deploy/copy-untracked.sh user@server:/opt/geodata-viewer
#   ./deploy/copy-untracked.sh user@server:/opt/geodata-viewer --dry-run
#   ./deploy/copy-untracked.sh user@server:/opt/geodata-viewer --data ~/surveys/tonga /srv/data/tonga
#
# Uses rsync over ssh: only changed files are sent, so re-running is cheap.
# The local dev .env is not copied; Docker deployments use deploy/.env.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() { sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

[ $# -ge 1 ] || usage 1
case "$1" in -h|--help) usage ;; esac
DEST="${1%/}"; shift
case "$DEST" in *:/*) ;; *) echo "destination must look like user@server:/path/to/project" >&2; exit 1 ;; esac
HOST="${DEST%%:*}"
REMOTE_DIR="${DEST#*:}"

DRY_RUN=false
DATA_SRC=
DATA_DEST=
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|-n) DRY_RUN=true; shift ;;
    --data) [ $# -ge 3 ] || usage 1; DATA_SRC="${2%/}"; DATA_DEST="${3%/}"; shift 3 ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
done

command -v rsync >/dev/null || { echo "rsync is not installed" >&2; exit 1; }
# Options every rsync understands, including macOS's openrsync.
RSYNC=(rsync -az --progress)
if $DRY_RUN; then
  RSYNC+=(--dry-run)
  echo "Dry run: nothing is copied."
fi
remote() { $DRY_RUN || ssh "$HOST" "$1"; }  # skipped in a dry run

cd "$PROJECT_DIR"
remote "mkdir -p '$REMOTE_DIR/deploy'"

# Settings and secrets: owner-only on the server too.
if [ -f deploy/.env ]; then
  echo "==> deploy/.env"
  "${RSYNC[@]}" deploy/.env "$DEST/deploy/.env"
  remote "chmod 600 '$REMOTE_DIR/deploy/.env'"
else
  echo "==> deploy/.env: none here (deploy.sh creates it on the server on the first run)"
fi

if [ -d certs ]; then
  echo "==> certs/"
  "${RSYNC[@]}" certs/ "$DEST/certs/"
fi

if [ -n "$DATA_SRC" ]; then
  [ -d "$DATA_SRC" ] || { echo "data folder not found: $DATA_SRC" >&2; exit 1; }
  echo "==> data: $DATA_SRC -> $HOST:$DATA_DEST"
  remote "mkdir -p '$DATA_DEST'"
  "${RSYNC[@]}" "$DATA_SRC/" "$HOST:$DATA_DEST/"
  # World-readable, so the container's unprivileged user can read it.
  remote "chmod -R o+rX '$DATA_DEST'"
  echo "   set DATA_PATH=$DATA_DEST in deploy/.env on the server"
fi

echo "Done. On the server:  cd '$REMOTE_DIR' && ./deploy/deploy.sh"
