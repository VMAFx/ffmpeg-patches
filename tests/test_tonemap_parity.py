# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Unit tests for scripts/tonemap_parity.py (ADR-0002 parity gate).

These run without FFmpeg: they check the colour difference, the argument
parsing and the pass/fail decision. The gate itself was proven on the DV5
fixture both ways (vf_libplacebo against itself: 0.0; with apply_dolbyvision=0:
23.9, refused).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tonemap_parity as tp  # noqa: E402

# Sharma, Wu, Dalal 2005, table 1: a, b, expected CIEDE2000.
SHARMA = [
    ([50.0, 2.6772, -79.7751], [50.0, 0.0, -82.7485], 2.0425),
    ([50.0, 3.1571, -77.2803], [50.0, 0.0, -82.7485], 2.8615),
    ([50.0, -1.3802, -84.2814], [50.0, 0.0, -82.7485], 1.0000),
    ([50.0, 0.0, 0.0], [50.0, -1.0, 2.0], 2.3669),
    ([50.0, 2.49, -0.001], [50.0, -2.49, 0.0009], 7.1792),
    ([50.0, 2.5, 0.0], [73.0, 25.0, -18.0], 27.1492),
    ([60.2574, -34.0099, 36.2677], [60.4626, -34.1751, 39.4387], 1.2644),
    ([90.8027, -2.0831, 1.441], [91.1528, -1.6435, 0.0447], 1.4441),
]


class De2000(unittest.TestCase):
    def test_sharma_pairs(self):
        a = np.array([p for p, _, _ in SHARMA])
        b = np.array([q for _, q, _ in SHARMA])
        want = np.array([w for _, _, w in SHARMA])
        np.testing.assert_allclose(tp.de2000(a, b), want, atol=1e-4)

    def test_identity_is_zero(self):
        lab = tp.lab(np.array([[[0, 0, 0], [255, 255, 255], [12, 200, 77]]], np.uint8))
        self.assertEqual(float(tp.de2000(lab, lab).max()), 0.0)

    def test_symmetric(self):
        p, q, _ = SHARMA[5]
        self.assertAlmostEqual(
            tp.de2000(np.array([p]), np.array([q]))[0],
            tp.de2000(np.array([q]), np.array([p]))[0],
        )

    def test_self_check_refuses_broken_formula(self):
        with (
            mock.patch.object(tp, "de2000", lambda a, b: np.zeros(len(a))),
            self.assertRaises(SystemExit),
        ):
            tp.self_check()

    def test_self_check_passes(self):
        tp.self_check()


class Compare(unittest.TestCase):
    def frames(self, value):
        return np.full((2, 4, 4, 3), value, np.uint8)

    def test_equal_frames(self):
        row = tp.compare(self.frames(90), self.frames(90))
        self.assertEqual((row["frames"], row["mean"], row["share_over_2_3"]), (2, 0.0, 0.0))

    def test_planted_colour_error_is_large(self):
        wrong = self.frames(90)
        wrong[..., 1] = 200
        row = tp.compare(self.frames(90), wrong)
        self.assertGreater(row["mean"], 2.3)
        self.assertEqual(row["share_over_2_3"], 1.0)

    def test_shape_mismatch_refused(self):
        with self.assertRaises(SystemExit):
            tp.compare(self.frames(1), np.zeros((1, 4, 4, 3), np.uint8))


class Args(unittest.TestCase):
    def test_pairs(self):
        got = tp.pairs(["a=x=1,y", "ocl=format=p010le"], "cand")
        self.assertEqual(got, {"a": "x=1,y", "ocl": "format=p010le"})

    def test_pairs_refuses_missing_name_or_value(self):
        for bad in ("noequals", "=value"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                tp.pairs([bad], "cand")

    def test_parse_defaults(self):
        args = tp.parse(["--ffmpeg", "f", "--clip", "c", "--ref", "r"])
        self.assertEqual((args.bar, args.cand, args.no_timing), (2.3, [], False))


class Load(unittest.TestCase):
    def test_whole_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.rgb"
            np.zeros(2 * 4 * 2 * 3, np.uint8).tofile(path)
            self.assertEqual(tp.load(path, 4, 2).shape, (2, 2, 4, 3))

    def test_partial_frame_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.rgb"
            np.zeros(4 * 2 * 3 + 1, np.uint8).tofile(path)
            with self.assertRaises(SystemExit):
                tp.load(path, 4, 2)


class Candidate(unittest.TestCase):
    ref = np.full((1, 2, 2, 3), 100, np.uint8)
    session = tp.Session(SimpleNamespace(bar=2.3), Path("."), (2, 2))

    def run_with(self, got):
        with mock.patch.object(tp, "measure", return_value=({"vf": "v", "pre": []}, got)):
            return tp.candidate(self.session, "n", "v", [], self.ref)

    def test_pass_at_identity(self):
        self.assertTrue(self.run_with(self.ref.copy())["pass"])

    def test_fail_on_planted_wrong_colour(self):
        wrong = self.ref.copy()
        wrong[..., 2] = 250
        self.assertFalse(self.run_with(wrong)["pass"])

    def test_ffmpeg_refusal_is_a_failed_path(self):
        err = subprocess.CalledProcessError(187, ["ffmpeg"])
        with mock.patch.object(tp, "measure", side_effect=err):
            row = tp.candidate(self.session, "n", "v", [], self.ref)
        self.assertEqual((row["error"], row["pass"]), ("ffmpeg exit 187", False))


if __name__ == "__main__":
    unittest.main()
