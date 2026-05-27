#!/usr/bin/env bash
set -euo pipefail

# Post a review to the mock GitHub API's control endpoint.
# Called by the testrunner's post_review MCP tool.
#
# Usage: post_review.sh <pr_number> <state> <body>
#   pr_number: integer PR number
#   state:     CHANGES_REQUESTED | APPROVED | COMMENTED
#   body:      review body text
#
# Requires: FORGEJO_API_URL or MOCK_PORT env var

if [[ $# -lt 3 ]]; then
    echo "Usage: post_review.sh <pr_number> <state> <body>" >&2
    exit 1
fi

PR_NUMBER="$1"
STATE="$2"
BODY="$3"

# Resolve mock API URL
if [[ -n "${FORGEJO_API_URL:-}" ]]; then
    BASE_URL="$FORGEJO_API_URL"
elif [[ -n "${MOCK_PORT:-}" ]]; then
    BASE_URL="http://127.0.0.1:$MOCK_PORT"
else
    echo "ERROR: Neither FORGEJO_API_URL nor MOCK_PORT is set" >&2
    exit 1
fi

# Build JSON payload (escape body for JSON safety)
PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({
    'pr_number': int(sys.argv[1]),
    'state': sys.argv[2],
    'body': sys.argv[3]
}))
" "$PR_NUMBER" "$STATE" "$BODY")

# Post to control endpoint
RESPONSE=$(curl -sf -X POST "${BASE_URL}/_control/reviews" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" 2>&1) || {
    echo "ERROR: Failed to post review: $RESPONSE" >&2
    exit 1
}

echo "$RESPONSE"
