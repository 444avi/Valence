"""Runtime configuration and filesystem layout for the Valence web layer.

Everything is driven by environment variables so the same code runs on the box
(defaults target /var/lib/valence, matching the systemd unit and the plan's data
model) and locally for development (export VALENCE_HOME to a writable path).
"""

from __future__ import annotations

import os
from pathlib import Path

# Repo root = parent of this package. Job subprocesses run from here so `arb`
# imports resolve, and we invoke the venv python that launched the API.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Data home: SQLite DB + result blobs. /var/lib/valence on the box (see plan §5).
HOME = Path(os.environ.get("VALENCE_HOME", "/var/lib/valence"))
DB_PATH = HOME / "valence.db"
RUNS_DIR = HOME / "runs"

# Optional cross-run cache TTL (days). Belt-and-suspenders on top of the
# full-text question_hash; unset = no TTL. See arb/valcache.py and plan §8.
CACHE_TTL_DAYS = os.environ.get("VALENCE_CACHE_TTL_DAYS", "") or None

# Optional hard wall-clock ceiling for a single job (seconds). OFF by default
# (0 = no timeout) — nothing is imposed unless you opt in by setting
# VALENCE_JOB_TIMEOUT_SECONDS. When set, a run that exceeds it is killed and
# marked failed (see web/jobs.py:supervise): a safety valve for a wedged job, not
# a default limit.
JOB_TIMEOUT_SECONDS = int(os.environ.get("VALENCE_JOB_TIMEOUT_SECONDS") or "0")

# Header Cloudflare Access injects with the authenticated user's email. Trusted
# only because the origin is unreachable except through the tunnel (plan §10).
ACCESS_EMAIL_HEADER = "cf-access-authenticated-user-email"

# Fallback attribution when the header is absent (local dev, direct curl).
UNKNOWN_USER = "unknown"

STATIC_DIR = Path(__file__).resolve().parent / "static"


def ensure_dirs() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def blob_path(run_id: str) -> Path:
    return RUNS_DIR / f"{run_id}.json"


def stderr_path(run_id: str) -> Path:
    return RUNS_DIR / f"{run_id}.stderr.log"
