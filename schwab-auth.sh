#!/usr/bin/env bash
#
# Temporarily open the admin service for Schwab OAuth re-authentication.
#
# This script:
#   1. Grants public access to the admin service
#   2. Opens the admin dashboard in your browser
#   3. Polls /status until a valid token appears (or 5-minute timeout)
#   4. Revokes public access
#
# Usage:
#   ./schwab-auth.sh
#
# Environment variables (or edit the defaults below):
#   PROJECT_ID  - GCP project ID
#   REGION      - Cloud Run region (default: us-west1)
#
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-west1}"
ADMIN_SERVICE="schwab-mcp-admin"
TIMEOUT=300  # 5 minutes

if [[ -z "$PROJECT_ID" ]]; then
    echo "ERROR: PROJECT_ID is required. Set it or configure gcloud."
    exit 1
fi

# Get the admin service URL
ADMIN_URL=$(gcloud run services describe "$ADMIN_SERVICE" \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --format 'value(status.url)' 2>/dev/null || true)

if [[ -z "$ADMIN_URL" ]]; then
    echo "ERROR: Could not find admin service '$ADMIN_SERVICE'."
    echo "Run ./deploy.sh --admin-only first."
    exit 1
fi

# Ensure we always revoke access on exit
revoke_access() {
    echo ""
    echo "--- Revoking public access ---"
    gcloud run services remove-iam-policy-binding "$ADMIN_SERVICE" \
        --region="$REGION" \
        --member="allUsers" \
        --role="roles/run.invoker" \
        --project="$PROJECT_ID" \
        --quiet 2>/dev/null || true
    echo "Admin service locked down."
}
trap revoke_access EXIT

# Grant temporary public access
echo "--- Granting temporary public access to $ADMIN_SERVICE ---"
gcloud run services add-iam-policy-binding "$ADMIN_SERVICE" \
    --region="$REGION" \
    --member="allUsers" \
    --role="roles/run.invoker" \
    --project="$PROJECT_ID" \
    --quiet >/dev/null

echo "Access granted. Opening admin dashboard..."
echo "  $ADMIN_URL"
echo ""

# Open browser
if command -v open &>/dev/null; then
    open "$ADMIN_URL"
elif command -v xdg-open &>/dev/null; then
    xdg-open "$ADMIN_URL"
else
    echo "Open this URL in your browser: $ADMIN_URL"
fi

# Poll /status until token exists or timeout
echo "Waiting for Schwab auth to complete (${TIMEOUT}s timeout)..."
echo "Complete the login in your browser, then this script will auto-close."
echo ""

elapsed=0
interval=5
while [[ $elapsed -lt $TIMEOUT ]]; do
    status=$(curl -s "${ADMIN_URL}/status" 2>/dev/null || echo "{}")
    exists=$(echo "$status" | python3 -c "import sys,json; print(json.load(sys.stdin).get('exists', False))" 2>/dev/null || echo "False")

    if [[ "$exists" == "True" ]]; then
        echo "✓ Schwab token detected! Auth complete."
        exit 0
    fi

    sleep "$interval"
    elapsed=$((elapsed + interval))
    remaining=$((TIMEOUT - elapsed))
    printf "\r  Waiting... %ds remaining" "$remaining"
done

echo ""
echo "⚠ Timeout reached. Token not detected."
echo "  You can re-run this script to try again."
exit 1
