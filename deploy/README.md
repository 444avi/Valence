# Valence web deploy

Wraps the `arb` CLI in a job-runner API + web UI, reachable only by the Arboretum
team through a Cloudflare Tunnel. Follows the build sequence in
`valence-web-implementation-plan.md`. Each step is verifiable before the next.

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
VALENCE_HOME=./var .venv/bin/uvicorn web.app:app --port 8080
# open http://127.0.0.1:8080
```

The cache and per-run LLM counter switch on automatically for web-launched jobs
(the API exports `VALENCE_DB` / `VALENCE_RUN_ID`); the plain CLI stays cache-free.

---

## Step 1 - Access path (do this before any app code)

Provision the host and put Cloudflare Access in front of it.

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
  `cloudflared-config.yml.example`), enable `deploy/systemd/cloudflared.service`.
- In the Cloudflare Zero Trust dashboard, add an **Access application** for that
  hostname with a **Google Workspace domain policy** (allow
  `@arboretuminvestments.net`).

**Acceptance:** a hello-world page on `:8080` loads for a team Google account and
is blocked in an incognito window.

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

## Step 6 - Launch form + run list with attribution

Already implemented. The run list polls `GET /runs`; a running job shows
`running` until it finishes (no streaming). Attribution comes from the
`Cf-Access-Authenticated-User-Email` header.

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
- **Zero inbound security group.** The outbound-only tunnel is what makes the
  `Cf-Access-Authenticated-User-Email` header trustworthy. Never add an ingress
  rule or a direct public path to the API.
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
