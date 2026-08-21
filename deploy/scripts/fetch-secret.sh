#!/usr/bin/env bash
# Fetch ANTHROPIC_API_KEY from SSM Parameter Store into a private EnvironmentFile.
# Runs as ExecStartPre for valence-api.service so the key never sits in a
# plaintext .env on disk. The file lives in a tmpfs RuntimeDirectory (/run) and
# is mode 0600, owned by the service user.
#
# The SecureString parameter is created once, out of band (see deploy/README.md):
#   aws ssm put-parameter --name /valence/anthropic-api-key --type SecureString \
#       --value "sk-ant-..."   # value never enters this repo or the model's context
set -euo pipefail

PARAM_NAME="${VALENCE_SSM_PARAM:-/valence/anthropic-api-key}"
ENV_DIR="/run/valence"
ENV_FILE="${ENV_DIR}/env"

mkdir -p "$ENV_DIR"

# --with-decryption resolves the SecureString via KMS; the instance role grants
# ssm:GetParameter on /valence/* and kms:Decrypt on the parameter's key only.
KEY="$(aws ssm get-parameter --name "$PARAM_NAME" --with-decryption \
        --query 'Parameter.Value' --output text)"

umask 077
printf 'ANTHROPIC_API_KEY=%s\n' "$KEY" > "$ENV_FILE"
chmod 0600 "$ENV_FILE"
# Best-effort owner set; ignore if the service user does not exist yet at build time.
chown valence:valence "$ENV_FILE" 2>/dev/null || true
