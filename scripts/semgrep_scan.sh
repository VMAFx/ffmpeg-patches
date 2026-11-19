#!/bin/sh
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
# Run the .semgrep.yml rules from the repository root; make semgrep and the
# scanners workflow (.github/workflows/scanners.yml) both call this script.
# An optional first operand --rules=PATH replaces .semgrep.yml as the config
# and is not passed on to semgrep. A finding fails through --error. A rule
# configuration warning (for example an include pattern semgrep will
# reinterpret) goes to stderr with exit code 0, even under --strict, so this
# script fails on it. Remaining arguments are passed to semgrep scan; SEMGREP
# names the semgrep executable (default semgrep).
set -eu

rules=.semgrep.yml
case "${1-}" in
    --rules=*)
        rules=${1#--rules=}
        shift
        ;;
esac

err=$(mktemp) || exit 1
trap 'rm -f "$err"' EXIT

rc=0
"${SEMGREP:-semgrep}" scan --config "$rules" --error --metrics off \
    --disable-version-check "$@" 2>"$err" || rc=$?
cat "$err" >&2

if grep -E '^Rule [^ ]+ contains ' "$err" >/dev/null; then
    echo "semgrep_scan: a rule in $rules draws the warning above; fix the rule" >&2
    if [ "$rc" -eq 0 ]; then
        rc=1
    fi
fi
exit "$rc"
