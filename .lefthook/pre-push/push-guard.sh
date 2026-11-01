#!/bin/sh
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2

set -eu
exec python3 scripts/push_guard.py "$@"
