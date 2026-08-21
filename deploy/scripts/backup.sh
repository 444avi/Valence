#!/usr/bin/env bash
# Nightly backup: a consistent SQLite snapshot plus the run result blobs, to S3.
# Wire via cron:  15 7 * * *  /opt/valence/deploy/scripts/backup.sh
# (07:15 UTC). Uses `sqlite3 .backup` for a consistent copy under WAL rather than
# copying the live file. The instance role grants s3:PutObject on this bucket only.
set -euo pipefail

HOME_DIR="${VALENCE_HOME:-/var/lib/valence}"
DB="${HOME_DIR}/valence.db"
RUNS_DIR="${HOME_DIR}/runs"
BUCKET="${VALENCE_BACKUP_BUCKET:?set VALENCE_BACKUP_BUCKET}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Consistent DB snapshot (safe while the API is writing under WAL).
sqlite3 "$DB" ".backup '${TMP}/valence.db'"

aws s3 cp "${TMP}/valence.db" "s3://${BUCKET}/db/valence-${STAMP}.db" --only-show-errors
aws s3 sync "$RUNS_DIR" "s3://${BUCKET}/runs/" --only-show-errors

echo "backup ${STAMP} uploaded to s3://${BUCKET}"
