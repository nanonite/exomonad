#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

: "${EXOMONAD_FORGEJO_E2E_URL:?set the dedicated Forgejo URL}"
: "${EXOMONAD_FORGEJO_E2E_TOKEN:?set the dedicated Forgejo token}"
: "${EXOMONAD_FORGEJO_E2E_OWNER:?set the dedicated Forgejo owner}"
: "${EXOMONAD_FORGEJO_E2E_REPO:?set the dedicated Forgejo repository}"
: "${EXOMONAD_FORGEJO_E2E_GIT_REMOTE:?set the disposable Forgejo Git remote}"
: "${EXOMONAD_BEAST_WORKSPACE:?set the captured Beast workspace}"
: "${EXOMONAD_BEAST_CONTINUE_COMMAND:?set the Beast continuation command}"

if [[ "${EXOMONAD_FORGEJO_E2E_MOCK:-0}" == "1" ]]; then
    echo "#1057 requires real Forgejo; EXOMONAD_FORGEJO_E2E_MOCK=1 is not accepted" >&2
    exit 2
fi

cd "$PROJECT_ROOT"
exec python3 "$SCRIPT_DIR/run.py" --mode all
