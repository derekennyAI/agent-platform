#!/usr/bin/env bash
# Constrained shell script runner for Derek.
# Only executes scripts from the user-scripts/ directory inside this skill.
# Works in both the real workspace and sandbox environments.

set -euo pipefail

# Resolve relative to this script's location — works in sandbox or real workspace
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS_DIR="$SKILL_DIR/user-scripts"
AUDIT_LOG="$SKILL_DIR/audit.log"
TIMEOUT_SECS=60

if [ $# -lt 1 ]; then
  echo "Usage: run.sh <script-name> [args...]" >&2
  echo "Scripts must exist in $SCRIPTS_DIR" >&2
  exit 1
fi

SCRIPT_NAME="$1"
shift

# Resolve the target path — handle both bare names and relative paths
if [[ "$SCRIPT_NAME" = /* ]]; then
  TARGET="$SCRIPT_NAME"
else
  TARGET="$SCRIPTS_DIR/$SCRIPT_NAME"
fi

# Resolve symlinks and canonicalize
REAL_TARGET="$(realpath -m "$TARGET" 2>/dev/null)" || {
  echo "ERROR: cannot resolve path: $TARGET" >&2
  exit 1
}

# Verify the script lives inside the allowed directory
REAL_SCRIPTS_DIR="$(realpath "$SCRIPTS_DIR")"
if [[ "$REAL_TARGET" != "$REAL_SCRIPTS_DIR/"* ]]; then
  echo "ERROR: script must be inside $SCRIPTS_DIR" >&2
  echo "Resolved to: $REAL_TARGET" >&2
  exit 1
fi

# Verify the script exists
if [ ! -f "$REAL_TARGET" ]; then
  echo "ERROR: script not found: $REAL_TARGET" >&2
  exit 1
fi

# Verify executable bit
if [ ! -x "$REAL_TARGET" ]; then
  echo "ERROR: script is not executable. Run: chmod +x $REAL_TARGET" >&2
  exit 1
fi

# Log execution
mkdir -p "$(dirname "$AUDIT_LOG")"
echo "$(date -Iseconds) RUN $SCRIPT_NAME [args: $*]" >> "$AUDIT_LOG"

# Execute with timeout
EXIT_CODE=0
timeout "$TIMEOUT_SECS" "$REAL_TARGET" "$@" || EXIT_CODE=$?

# Log result
if [ "$EXIT_CODE" -eq 124 ]; then
  echo "$(date -Iseconds) TIMEOUT $SCRIPT_NAME (${TIMEOUT_SECS}s limit)" >> "$AUDIT_LOG"
  echo "ERROR: script timed out after ${TIMEOUT_SECS}s" >&2
elif [ "$EXIT_CODE" -ne 0 ]; then
  echo "$(date -Iseconds) FAIL $SCRIPT_NAME exit=$EXIT_CODE" >> "$AUDIT_LOG"
else
  echo "$(date -Iseconds) OK $SCRIPT_NAME" >> "$AUDIT_LOG"
fi

exit "$EXIT_CODE"
