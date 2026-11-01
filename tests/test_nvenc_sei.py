# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Unit test for nvenc_hevc_sei_size_check() of patch 0003.

The patch, the backport of FFmpeg master 96b7f8281a5f, adds the function to
libavcodec/nvenc.c: hevc_nvenc keeps the SEI of a picture in order while
their payload bytes stay within NVENC_HEVC_SEI_MAX_PAYLOAD (512) and drops
the others, with a warning for the first drop and verbose messages after
that. The test takes the function out of the patch, compiles it with stub
types and a small C driver, and checks it against an independent Python
model on fixed boundary cases and seeded random pictures. A function whose
limit, comparison or codec guard is changed must be refused.

Needs a C compiler (CC, default cc); without one the compiled cases are
skipped.

    python3 -m unittest tests.test_nvenc_sei
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATCH = next((ROOT / "patches").glob("0003-*.patch"))
CC = os.environ.get("CC", "cc")
LIMIT = 512
HEVC, H264 = 173, 27
FIRST = "/* hevc_nvenc fails nvEncLockBitstream()"
SIGNATURE = "static int nvenc_hevc_sei_size_check("

PRELUDE = r"""
#include <inttypes.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

enum { AV_CODEC_ID_H264 = 27, AV_CODEC_ID_HEVC = 173 };
#define AV_LOG_WARNING 24
#define AV_LOG_VERBOSE 40

typedef struct { int id; } AVCodec;
typedef struct { const AVCodec *codec; void *priv_data; } AVCodecContext;
typedef struct {
    uint32_t payloadSize;
    uint32_t payloadType;
    uint8_t *payload;
} NV_ENC_SEI_PAYLOAD;
typedef struct { NV_ENC_SEI_PAYLOAD *sei_data; int sei_size_warned; } NvencContext;

static int warnings, verbose;

static void av_log(void *avcl, int level, const char *fmt, ...)
{
    (void)avcl;
    (void)fmt;
    if (level == AV_LOG_WARNING)
        warnings++;
    else if (level == AV_LOG_VERBOSE)
        verbose++;
}

static void av_freep(void *arg)
{
    void **p = arg;
    free(*p);
    *p = NULL;
}
"""

DRIVER = r"""
int main(void)
{
    static NV_ENC_SEI_PAYLOAD sei[64];
    for (int line = 0; line < 100000; line++) {
        AVCodec codec;
        NvencContext ctx = { sei, 0 };
        AVCodecContext avctx = { &codec, &ctx };
        int n, kept;
        if (scanf("%d %d", &codec.id, &n) != 2)
            return 0;
        if (n < 0 || n > 64)
            return 2;
        for (int i = 0; i < n; i++) {
            unsigned size;
            if (scanf("%u", &size) != 1)
                return 3;
            sei[i].payloadSize = size;
            sei[i].payloadType = 5;
            sei[i].payload = malloc(size + 1);
            if (!sei[i].payload)
                return 4;
        }
        warnings = verbose = 0;
        kept = nvenc_hevc_sei_size_check(&avctx, n);
        printf("%d", kept);
        for (int i = 0; i < kept; i++) {
            printf(" %" PRIu32, sei[i].payloadSize);
            free(sei[i].payload);
        }
        printf(" | %d %d\n", warnings, verbose);
    }
    return 5;
}
"""

Picture = tuple[int, tuple[int, ...]]


def function_from_patch(text: str) -> str:
    """Return the limit and nvenc_hevc_sei_size_check() as patch 0003 adds them."""
    lines = [line[1:] for line in text.splitlines() if line.startswith("+")]
    starts = [i for i, line in enumerate(lines) if line.startswith(FIRST)]
    if len(starts) != 1:
        raise ValueError(f"{PATCH.name} does not add the nvenc SEI size check once")
    body = lines[starts[0] :]
    if not any(line.startswith(SIGNATURE) for line in body):
        raise ValueError(f"{PATCH.name} does not add {SIGNATURE}")
    sig = next(i for i, line in enumerate(body) if line.startswith(SIGNATURE))
    end = next(i for i in range(sig, len(body)) if body[i] == "}")
    return "\n".join(body[: end + 1]) + "\n"


def expected(picture: Picture) -> str:
    """Kept payload sizes and the warning and verbose counts the function must give."""
    codec, sizes = picture
    if codec != HEVC:
        return " ".join(str(v) for v in (len(sizes), *sizes)) + " | 0 0"
    kept: list[int] = []
    dropped = 0
    for size in sizes:
        if sum(kept) + size <= LIMIT:
            kept.append(size)
        else:
            dropped += 1
    head = " ".join(str(v) for v in (len(kept), *kept))
    return f"{head} | {min(dropped, 1)} {max(dropped - 1, 0)}"


def cases() -> list[Picture]:
    """Boundary pictures, then seeded random ones."""
    fixed: list[Picture] = [
        (HEVC, ()),
        (HEVC, (512,)),
        (HEVC, (513,)),
        (HEVC, (256, 256)),
        (HEVC, (256, 257, 255)),
        (HEVC, (600, 100)),
        (HEVC, (512, 0)),
        (HEVC, (513, 600, 700)),
        (H264, (1000, 1000)),
    ]
    rng = random.Random(27)
    drawn: list[Picture] = [
        (
            rng.choice([HEVC, HEVC, H264]),
            tuple(rng.randrange(0, 700) for _ in range(rng.randrange(0, 7))),
        )
        for _ in range(3000)
    ]
    return fixed + drawn


def run_function(function: str, scratch: Path, rows: list[Picture]) -> list[str]:
    """Compile function with the stubs and the driver; return one output line per row."""
    source = scratch / "check.c"
    source.write_text(PRELUDE + function + DRIVER)
    exe = scratch / "check"
    cmd = [CC, "-std=c99", "-Wall", "-Wextra", "-pedantic", "-Werror"]
    build = subprocess.run(  # nosec B603 - compiler argv uses fixture-controlled paths and flags.
        [*cmd, str(source), "-o", str(exe)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if build.returncode != 0:
        raise AssertionError(f"{cmd} exited {build.returncode}: {build.stderr}")
    stdin = "".join(f"{codec} {len(sizes)} {' '.join(map(str, sizes))}\n" for codec, sizes in rows)
    done = subprocess.run(  # nosec B603 - executable is built in the temporary test directory.
        [str(exe)], input=stdin, capture_output=True, text=True, timeout=120, check=False
    )
    if done.returncode != 0:
        raise AssertionError(f"{exe} exited {done.returncode}: {done.stderr}")
    return done.stdout.splitlines()


def mismatches(got: list[str], rows: list[Picture]) -> list[int]:
    """Indexes of the rows whose output differs from the model."""
    want = [expected(row) for row in rows]
    if len(got) != len(want):
        return list(range(max(len(got), len(want))))
    return [i for i, (g, w) in enumerate(zip(got, want, strict=True)) if g != w]


class ModelTest(unittest.TestCase):
    def test_limit_is_inclusive(self) -> None:
        self.assertEqual(expected((HEVC, (512,))), "1 512 | 0 0")
        self.assertEqual(expected((HEVC, (513,))), "0 | 1 0")

    def test_a_later_smaller_sei_still_fits(self) -> None:
        self.assertEqual(expected((HEVC, (600, 100))), "1 100 | 1 0")
        self.assertEqual(expected((HEVC, (256, 257, 255))), "2 256 255 | 1 0")

    def test_one_warning_then_verbose(self) -> None:
        self.assertEqual(expected((HEVC, (513, 600, 700))), "0 | 1 2")

    def test_other_codecs_keep_everything(self) -> None:
        self.assertEqual(expected((H264, (1000, 1000))), "2 1000 1000 | 0 0")


@unittest.skipUnless(shutil.which(CC), f"no C compiler {CC}")
class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.function = function_from_patch(PATCH.read_text())
        self.rows = cases()
        self.scratch = Path(tempfile.mkdtemp(prefix="nvenc_sei_"))
        self.addCleanup(shutil.rmtree, self.scratch)

    def assert_refused(self, old: str, new: str) -> None:
        planted = self.function.replace(old, new)
        self.assertNotEqual(planted, self.function)
        got = run_function(planted, self.scratch, self.rows)
        self.assertNotEqual(mismatches(got, self.rows), [])

    def test_function_matches_the_model(self) -> None:
        got = run_function(self.function, self.scratch, self.rows)
        self.assertEqual(mismatches(got, self.rows), [])

    def test_planted_strict_comparison_is_refused(self) -> None:
        self.assert_refused("<= NVENC_HEVC_SEI_MAX_PAYLOAD", "< NVENC_HEVC_SEI_MAX_PAYLOAD")

    def test_planted_limit_change_is_refused(self) -> None:
        self.assert_refused(
            "#define NVENC_HEVC_SEI_MAX_PAYLOAD 512", "#define NVENC_HEVC_SEI_MAX_PAYLOAD 511"
        )

    def test_planted_codec_guard_change_is_refused(self) -> None:
        self.assert_refused("!= AV_CODEC_ID_HEVC", "== AV_CODEC_ID_HEVC")


if __name__ == "__main__":
    unittest.main()
