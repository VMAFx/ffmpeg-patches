# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Unit test for libavcodec/nvenc_sei.h of patch 0028.

The header holds the hevc_nvenc SEI budget of 0003 and the emulation
prevention and truncation rules of 0004 as plain C, so that Pelorus can
include it. The test takes the header out of the patch, compiles it alone
with a small C driver, and checks every function against an independent
Python model on fixed boundary cases and seeded random payloads. A header
whose escape rule is changed by one character must be refused.

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
PATCH = next((ROOT / "patches").glob("0028-*.patch"))
HEADER = "libavcodec/nvenc_sei.h"
CC = os.environ.get("CC", "cc")
# hevc_nvenc non-VCL limit, reserve and SEI budget of 0003, as the driver prints them.
BUDGET_LINE = "1024 256 768"

DRIVER = r"""
#include <stdio.h>
#include <stdlib.h>
#include "libavcodec/nvenc_sei.h"

int main(void)
{
    static uint8_t p[8192];
    unsigned type, size, pmax, zeros;
    unsigned long limit;

    printf("%d %d %d\n", NVENC_HEVC_NON_VCL_LIMIT, NVENC_HEVC_SEI_RESERVE, NVENC_HEVC_SEI_BUDGET);
    for (int line = 0; line < 100000; line++) {
        if (scanf("%u %lu %u %u %u", &type, &limit, &pmax, &zeros, &size) != 5)
            return 0;
        if (size > sizeof(p))
            return 2;
        for (unsigned i = 0; i < size; i++) {
            unsigned v;
            if (scanf("%2x", &v) != 1)
                return 3;
            p[i] = (uint8_t)v;
        }
        printf("%zu %zu %d\n", nvenc_sei_epb_count(p, size, (int)zeros),
               nvenc_sei_nal_size(p, type, size, (size_t)limit),
               nvenc_udu_sei_truncated(p, size, pmax));
    }
    return 4;
}
"""


def header_from_patch(text: str) -> str:
    """Return the new file HEADER as patch 0028 adds it."""
    start = text.index(f"diff --git a/{HEADER} b/{HEADER}")
    end = text.find("\ndiff --git ", start + 1)
    section = text[start : end if end >= 0 else len(text)]
    if "new file mode" not in section:
        raise ValueError(f"{PATCH.name} does not add {HEADER} as a new file")
    body = section[section.index("\n@@") + 1 :].splitlines()[1:]
    return "".join(line[1:] + "\n" for line in body if line.startswith("+"))


def epb(payload: bytes, zeros: int) -> int:
    """Emulation prevention bytes of payload after zeros zero bytes."""
    count = 0
    for byte in payload:
        if zeros >= 2 and byte <= 3:
            count += 1
            zeros = 0
        zeros = zeros + 1 if byte == 0 else 0
    return count


def nal_size(payload: bytes, kind: int, limit: int) -> int:
    """Annex B bytes of the SEI NAL unit NVENC writes for payload."""
    n = len(payload)
    fixed = 4 + 2 + kind // 255 + 1 + n // 255 + 1 + n + 2
    return fixed if fixed > limit else fixed + epb(payload, 0)


def truncated(payload: bytes, pmax: int) -> int:
    """1 when NVENC writes payload truncated next to a pmax-byte payload."""
    n = len(payload)
    return int(n + 3 + epb(payload, int(n % 255 == 0)) > 4 * (pmax + 5) // 3)


def cases() -> list[tuple[int, int, int, int, bytes]]:
    """Boundary cases, then seeded random zero-heavy payloads."""
    fixed = [
        (5, 10**9, 0, 0, b""),
        (5, 10**9, 3, 0, b"\x00\x00\x03"),
        (5, 10**9, 4, 2, b"\x01\x00\x00\x00"),
        (5, 10**9, 255, 0, bytes(255)),
        (5, 10**9, 254, 0, bytes(254)),
        (255, 10**9, 64, 0, bytes(64)),
        (5, 20, 300, 0, bytes(300)),
        (5, 10**9, 60, 0, bytes(60)),
        (5, 10**9, 100, 0, bytes(60)),
    ]
    rng = random.Random(27)
    drawn = []
    for _ in range(3000):
        share = rng.random()
        size = rng.randrange(0, 1300)
        payload = bytes(0 if rng.random() < share else rng.randrange(1, 5) for _ in range(size))
        limit = rng.choice([10**9, rng.randrange(0, 1500)])
        drawn.append(
            (
                rng.choice([5, 4, 600]),
                limit,
                size + rng.randrange(0, 400),
                rng.randrange(0, 3),
                payload,
            )
        )
    return fixed + drawn


def expected(case: tuple[int, int, int, int, bytes]) -> str:
    kind, limit, pmax, zeros, payload = case
    return f"{epb(payload, zeros)} {nal_size(payload, kind, limit)} {truncated(payload, pmax)}"


def run_header(
    header: str, scratch: Path, rows: list[tuple[int, int, int, int, bytes]]
) -> list[str]:
    """Compile header with the driver alone and return one output line per row."""
    (scratch / "libavcodec").mkdir(exist_ok=True)
    (scratch / HEADER).write_text(header)
    (scratch / "driver.c").write_text(DRIVER)
    exe = scratch / "driver"
    cmd = [CC, "-std=c99", "-Wall", "-Wextra", "-pedantic", "-Werror", "-I", str(scratch)]
    build = subprocess.run(
        [*cmd, str(scratch / "driver.c"), "-o", str(exe)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if build.returncode != 0:
        raise AssertionError(f"{cmd} exited {build.returncode}: {build.stderr}")
    stdin = "".join(f"{k} {lim} {pm} {z} {len(p)} {p.hex()}\n" for k, lim, pm, z, p in rows)
    done = subprocess.run(
        [str(exe)], input=stdin, capture_output=True, text=True, timeout=120, check=False
    )
    if done.returncode != 0:
        raise AssertionError(f"{exe} exited {done.returncode}: {done.stderr}")
    return done.stdout.splitlines()


def mismatches(got: list[str], rows: list[tuple[int, int, int, int, bytes]]) -> list[int]:
    """Indexes of the rows whose output differs from the model; -1 for the budget line."""
    if not got or got[0] != BUDGET_LINE:
        return [-1]
    got = got[1:]
    want = [expected(row) for row in rows]
    if len(got) != len(want):
        return list(range(max(len(got), len(want))))
    return [i for i, (g, w) in enumerate(zip(got, want, strict=True)) if g != w]


class ModelTest(unittest.TestCase):
    def test_three_zeros_need_one_escape(self) -> None:
        self.assertEqual(epb(b"\x00\x00\x00", 0), 1)
        self.assertEqual(epb(b"\x00\x00\x04", 0), 0)

    def test_carried_zeros_escape_the_first_byte(self) -> None:
        self.assertEqual(epb(b"\x01", 2), 1)
        self.assertEqual(epb(b"\x01", 1), 0)

    def test_nal_size_of_a_user_data_payload(self) -> None:
        # 4 start code, 2 header, 1 type, 1 size, 16 payload, 1 trailing, 1 boundary.
        self.assertEqual(nal_size(bytes(range(1, 17)), 5, 10**9), 26)
        self.assertEqual(nal_size(bytes(255), 5, 10**9), 4 + 2 + 1 + 2 + 255 + 2 + 127)

    def test_over_limit_size_is_not_scanned(self) -> None:
        self.assertEqual(nal_size(bytes(300), 5, 20), 4 + 2 + 1 + 2 + 300 + 2)

    def test_single_payload_truncation_edge(self) -> None:
        # One payload: truncated exactly when E > ceil(P / 3) + 3.
        self.assertEqual(truncated(bytes(60), 60), 1)
        self.assertEqual(truncated(b"\x00\x00\x01" * 20, 60), 0)


@unittest.skipUnless(shutil.which(CC), f"no C compiler {CC}")
class HeaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.header = header_from_patch(PATCH.read_text())
        self.rows = cases()
        self.scratch = Path(tempfile.mkdtemp(prefix="nvenc_sei_"))
        self.addCleanup(shutil.rmtree, self.scratch)

    def test_header_has_no_libav_include(self) -> None:
        includes = [line for line in self.header.splitlines() if line.startswith("#include")]
        self.assertEqual(includes, ["#include <stddef.h>", "#include <stdint.h>"])

    def test_header_matches_the_model(self) -> None:
        got = run_header(self.header, self.scratch, self.rows)
        self.assertEqual(mismatches(got, self.rows), [])

    def test_planted_escape_rule_change_is_refused(self) -> None:
        planted = self.header.replace("p[i] <= 3", "p[i] < 3")
        self.assertNotEqual(planted, self.header)
        got = run_header(planted, self.scratch, self.rows)
        self.assertNotEqual(mismatches(got, self.rows), [])

    def test_planted_budget_change_is_refused(self) -> None:
        planted = self.header.replace(
            "NVENC_HEVC_SEI_RESERVE   256", "NVENC_HEVC_SEI_RESERVE   255"
        )
        self.assertNotEqual(planted, self.header)
        got = run_header(planted, self.scratch, self.rows)
        self.assertEqual(mismatches(got, self.rows), [-1])


if __name__ == "__main__":
    unittest.main()
