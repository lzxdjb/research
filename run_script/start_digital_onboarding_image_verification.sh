#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

HOST="${IMAGE_VERIFICATION_HOST:-127.0.0.1}"
PORT="${IMAGE_VERIFICATION_PORT:-7871}"

echo "Starting digital-onboarding image verification service at http://$HOST:$PORT"
python3 -m uvicorn recipe.digital_onboarding.image_verification_service:app --host "$HOST" --port "$PORT"
