#!/bin/sh
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
# Run the .semgrep.yml rules from the repository root; make semgrep and the
# scanners workflow (.github/workflows/scanners.yml) both call this script.
# A finding fails through --error. A rule configuration warning (for example
# an include pattern semgrep will reinterpret) goes to stderr with exit code 0,
# even under --strict, so this script fails on it. Extra arguments are passed
# to semgrep scan; SEMGREP names the semgrep executable (default semgrep).
set -eu

err=$(mktemp) || exit 1
trap 'rm -f "$err"' EXIT

rc=0
"${SEMGREP:-semgrep}" scan --config .semgrep.yml --error --metrics off \
    --disable-version-check "$@" 2>"$err" || rc=$?
cat "$err" >&2

if grep -E '^Rule [^ ]+ contains ' "$err" >/dev/null; then
    echo "semgrep_scan: a rule in .semgrep.yml draws the warning above; fix the rule" >&2
    if [ "$rc" -eq 0 ]; then
        rc=1
    fi
fi
exit "$rc"
