# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for scripts/graph_check.py.

The fixtures are real graph dumps of FFmpeg master N-127573-gabc4e49ccf:

    ffmpeg -f lavfi -i testsrc2=s=64x64:d=0.04 -vf format=yuv444p,scale=32:32 \
        -pix_fmt yuv420p -f null - -print_graphs_file auto-scale.json \
        -print_graphs_format json
    ffmpeg -f lavfi -i testsrc2=s=64x64:d=0.04 -vf format=yuv420p \
        -c:v wrapped_avframe -f null - -print_graphs_file no-conversion.json \
        -print_graphs_format json

testsrc2 emits yuv420p, so the first graph needs a converter in front of
format=yuv444p and FFmpeg inserts auto_scale_0; the second needs none.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "graphs"
sys.path.insert(0, str(ROOT / "scripts"))

import graph_check  # noqa: E402


def check(*argv: str) -> tuple[int, str, str]:
    """Run main() and return the exit status, stdout and stderr."""
    with (
        contextlib.redirect_stdout(io.StringIO()) as out,
        contextlib.redirect_stderr(io.StringIO()) as err,
    ):
        status = graph_check.main(list(argv))
    return status, out.getvalue(), err.getvalue()


class GraphCheckTest(unittest.TestCase):
    def test_auto_inserted_scale_is_refused(self) -> None:
        status, out, _ = check(str(FIXTURES / "auto-scale.json"))
        self.assertEqual(status, 1)
        self.assertEqual(out, "auto-inserted G0_auto_scale_0: yuv420p 64x64 -> yuv444p 64x64\n")

    def test_graph_without_conversion_passes(self) -> None:
        self.assertEqual(check(str(FIXTURES / "no-conversion.json")), (0, "", ""))

    def test_allowed_filter_passes(self) -> None:
        self.assertEqual(check(str(FIXTURES / "auto-scale.json"), "--allow", "scale"), (0, "", ""))

    def test_other_allowed_filter_still_refuses(self) -> None:
        self.assertEqual(check(str(FIXTURES / "auto-scale.json"), "--allow", "aresample")[0], 1)

    def test_name_of_another_filter_is_not_auto(self) -> None:
        # An instance id that looks like auto_scale_0 on a filter that is not
        # scale did not come from the format negotiation.
        dump = json.loads((FIXTURES / "auto-scale.json").read_text())
        renamed = copy.deepcopy(dump)
        for flt in renamed["graphs"][0]["filters"]:
            if flt["filter_name"] == "scale":
                flt["filter_name"] = "null"
        self.assertEqual(graph_check.auto_filters(renamed), [])
        self.assertEqual(len(graph_check.auto_filters(dump)), 1)

    def test_unreadable_dump_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"graphs": [{"name": "no filters key"}]}')
            self.assertEqual(check(str(bad))[0], 2)
            bad.write_text("not json")
            self.assertEqual(check(str(bad))[0], 2)
            self.assertEqual(check(str(Path(tmp) / "missing.json"))[0], 2)


if __name__ == "__main__":
    unittest.main()
