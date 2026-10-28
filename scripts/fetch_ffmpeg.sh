#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
# Fetch the pinned tag and refuse it when its commit differs from base.env.
set -euo pipefail

# Match the workflow shell's empty expansion when a pin variable is unset.
: "${FFMPEG_REMOTE:=}" "${FFMPEG_TAG:=}" "${FFMPEG_COMMIT:=}"

git init -q ffmpeg
git -C ffmpeg remote add origin "$FFMPEG_REMOTE"
git -C ffmpeg fetch -q --depth=1 origin \
  "refs/tags/${FFMPEG_TAG}:refs/tags/${FFMPEG_TAG}"
actual=$(git -C ffmpeg rev-parse --verify "refs/tags/${FFMPEG_TAG}^{commit}")
if [ "$actual" != "$FFMPEG_COMMIT" ]; then
  echo "::error::${FFMPEG_TAG} peels to ${actual}, base.env pins ${FFMPEG_COMMIT}"
  exit 1
fi
git -C ffmpeg checkout -q --detach "$FFMPEG_COMMIT"
