# Valence web deploy

Wraps the `arb` CLI in a job-runner API + web UI. Cloudflare Tunnel provides the
transport path; signed `arb_session` cookies from Arboretum Accounts provide
authentication. Valence requires the Max plan.

The application code (the `web/` package, the validation cache in
`arb/valcache.py`, and the tests) is done and runs locally today. What remains is
infrastructure, which needs your AWS account, Cloudflare zone, and the Anthropic
key — none of which live in this repo.

## What is in this repo already

| Piece | Where | Verified |
|---|---|---|
| Job-runner API (`/runs`, `/health`, `/usage`) | `web/app.py`, `web/jobs.py`, `web/db.py` | curl + `tests/test_web.py` |
| Cross-run validation cache + real-call counter | `arb/valcache.py`, hook in `arb/validator.py` | `tests/test_validator.py` |
| Three-screen UI (launch / run list / result) | `web/static/` | in-browser |
| systemd units, tunnel config, secret + backup scripts | `deploy/` | n/a (needs the box) |
| EC2 + IAM + S3 + alarm + budget | `deploy/cloudformation.yaml` | parse-checked |

Run it locally:

```bash
python -m pip install -r requirements.txt
VALENCE_HOME=./var \
ARBORETUM_ACCOUNTS_URL=https://accounts.arboretuminvestments.net \
ARBORETUM_ISSUER=https://accounts.arboretuminvestments.net \
ARBORETUM_AUDIENCE=arboretum-tools \
ARBORETUM_JWKS_URL=https://accounts.arboretuminvestments.net/.well-known/jwks.json \
ARBORETUM_COOKIE_NAME=arb_session \
ARBORETUM_ALLOWED_RETURN_HOSTS=valence.arboretuminvestments.net \
ARBORETUM_RETURN_ORIGIN=https://valence.arboretuminvestments.net \
.venv/bin/uvicorn web.app:app --port 8082
```

The cache and per-run LLM counter switch on automatically for web-launched jobs
(the API exports `VALENCE_DB` / `VALENCE_RUN_ID`); the plain CLI stays cache-free.

---

## Access path

Provision the host and connect the public hostname through Cloudflare Tunnel.

```bash
aws cloudformation deploy \
  --template-file deploy/cloudformation.yaml \
  --stack-name valence \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      VpcId=vpc-xxxx SubnetId=subnet-xxxx \
      AlertEmail=team-alerts@arboretuminvestments.net
```

Then, on the box (via `aws ssm start-session --target <InstanceId>` - no SSH):

- Install `cloudflared`, create the named tunnel to
  `valence.arboretuminvestments.net`, drop `/etc/cloudflared/config.yml` (see
  `cloudflared-config.yml.example`), and enable `deploy/systemd/cloudflared.service`.
- Do not use Cloudflare Access headers as identity. The origin verifies only the
  signed Accounts session cookie.

**Acceptance:** `/health` works through the tunnel, unauthenticated navigation is
sent to Accounts, and a forged Cloudflare Access email header remains a 401.

## Step 2 - Deploy the repo

```bash
sudo -u valence git clone <repo> /opt/valence
cd /opt/valence
sudo -u valence python3.12 -m venv .venv
# requirements-dev.txt pulls in requirements.txt plus pytest, which the
# systemd unit's ExecStartPre regression gate needs on the box.
sudo -u valence .venv/bin/pip install -r requirements-dev.txt
# Create the secret once; the value never enters this repo or a model context:
aws ssm put-parameter --name /valence/anthropic-api-key --type SecureString --value 'sk-ant-...'
sudo cp deploy/systemd/valence-api.service /etc/systemd/system/
sudo systemctl enable --now valence-api
```

`fetch-secret.sh` (ExecStartPre) pulls the key from SSM into
`/run/valence/env` at mode 0600; no plaintext `.env` on disk.

**Acceptance:**
```bash
.venv/bin/python -m arb --sections crypto --json   # runs clean
.venv/bin/python -m pytest tests/ -q               # passes (gates the unit too)
```
Also run one real scan **with** the LLM (not `--no-llm`) so a live validation
call completes end to end - the whole cost story rides on
`arb/validator.py`'s model id and `output_config` resolving against the installed
`anthropic` SDK.

## Step 3 - Core job API

Already implemented. Smoke-test the full cycle through the tunnel:

```bash
curl -X POST https://valence.arboretuminvestments.net/runs \
  -H 'Content-Type: application/json' \
  -d '{"type":"scan","args":{"sections":"crypto","no_llm":true}}'
curl https://valence.arboretuminvestments.net/runs        # list, newest first
curl https://valence.arboretuminvestments.net/runs/<id>   # metadata + result blob
```

**Acceptance:** launch-to-result works entirely through curl.

## Step 4 - Result view UI

Already implemented (`web/static/run.html`). **Acceptance:** a real scan's
results are readable in a browser by a second team member. Confirm the
**UNCONFIRMED** badge is unmissable (it is loud orange, not grey).

## Step 5 - Validation cache

Already implemented and on by default for web-launched jobs. **Acceptance:**
run the same scan twice; the second run's `llm_calls` drops sharply. Optionally
tune `VALENCE_CACHE_TTL_DAYS` in the systemd unit (default 60).

## Authentication, entitlement, and ownership

All pages and data APIs except `/health` and static assets require a valid signed
Accounts session with plan `max`. Free and Premium users receive a Max-required
response. Regular users can list, open, cancel, and count usage only for runs
owned by their stable Arboretum account ID. `VALENCE_ADMIN_ACCOUNT_ID` grants the
existing cross-account operational view; plan or email alone never grants it.

`db.init_db()` performs an additive migration by adding nullable
`runs.account_id` and its index. On an authenticated request, legacy rows with a
null account ID and the same verified email are claimed for that account. The
update includes `account_id IS NULL`, so ownership is never reassigned. Keep the
pre-deployment SQLite backup until this compatibility period is complete.

Set `VALENCE_ADMIN_ACCOUNT_ID` in a host-specific systemd drop-in. Resolve the
owner's exact stable ID from Accounts during deployment; do not commit a guessed
ID or fall back to trusting a request email.

Before deployment, make a SQLite-safe backup and retain the current unit and
application tree. A typical production sequence is:

```bash
STAMP=$(date -u +%Y%m%d-%H%M%S)
sudo install -d -m 0750 /var/backups/valence/$STAMP
sudo sqlite3 /data/valence/valence.db ".backup '/var/backups/valence/$STAMP/valence.db'"
sudo sqlite3 /var/backups/valence/$STAMP/valence.db "PRAGMA integrity_check"
sudo cp -a /opt/valence /var/backups/valence/$STAMP/app
sudo cp -a /etc/systemd/system/valence-api.service /var/backups/valence/$STAMP/
sudo -u valence /opt/valence/.venv/bin/pip install \
  '/opt/valence/vendor/arboretum_auth-0.1.2-py3-none-any.whl[fastapi]'
sudo -u valence /opt/valence/.venv/bin/python -m pytest tests/ -q
sudo systemctl restart valence-api.service
```

Rollback restores the backed-up application and unit, reinstalls the dependency
set recorded before deployment, and restarts the service. The database change is
additive and old code ignores it; restore the SQLite snapshot only if the data
itself is unexpectedly affected, while the service is stopped.

## Step 7 - Operations

- **Backups:** `crontab -e` for the `valence` user:
  `15 7 * * * VALENCE_BACKUP_BUCKET=<bucket> /opt/valence/deploy/scripts/backup.sh`
- **Status alarm:** created by the template (`valence-status-check-failed` -> SNS).
  Confirm the SNS email subscription.
- **Run-failure email (SES):** a fresh SES account is in the sandbox and can only
  send to verified addresses. Verify the team's recipients or request production
  access before relying on failure emails.

---

## Guardrails (do not regress)

- **No trade execution, ever.** No wallet keys, no exchange write credentials.
  Blast radius on compromise is a screener and nothing more.
- **Zero inbound security group.** Keep the outbound-only tunnel and never add
  an ingress rule or direct public path. Authentication still comes only from
  the signed Accounts session.
- **Per-type argv whitelist.** `web/jobs.py` validates every flag against its
  type/choice set; user strings never reach argv raw. `scan` and `max` differ.
- **Tests gate the boot.** `pytest tests/ -q` runs in `ExecStartPre`; a silent
  fee-model regression costs money rather than throwing.
- **Fee models are pinned to the Aug 2026 schedule.** Re-verify `arb/fees.py`
  against both platforms quarterly.

## Open items (from the plan)

- Check whether the existing `arbmon` VPS has headroom before paying for new EC2.
- `t4g.small` vs `t4g.micro` after memory-testing `arb.max --section sports`
  (micro needs 2 GB swap).
- Final validation-cache TTL length.
