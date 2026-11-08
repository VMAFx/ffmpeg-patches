# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Tests for scripts/filter_lib_deps.py."""

from __future__ import annotations

import tempfile
import unittest
import unittest.mock
from pathlib import Path

from scripts import filter_lib_deps
from tests.helpers import capture_main

AVFORMAT_HEADER = """int avio_open(void);
int avio_write(void);
int avio_closep(void);
"""


class FilterLibDepsTest(unittest.TestCase):
    """Cover configure declarations, link edges, guards and exceptions."""

    def setUp(self) -> None:
        """Create one temporary FFmpeg-like source tree."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tree = Path(self.directory.name)
        (self.tree / "libavfilter").mkdir()
        (self.tree / "libavformat").mkdir()
        (self.tree / "libavformat" / "avformat.h").write_text(AVFORMAT_HEADER, encoding="utf-8")
        self.exceptions = self.tree / "exceptions.toml"
        self.exceptions.write_text("", encoding="utf-8")

    def write_tree(
        self,
        configure: str,
        sources: dict[str, str],
        objects: dict[str, list[str]],
        *,
        registered: tuple[str, ...] | None = None,
    ) -> None:
        """Write the minimal configure, Makefiles, registry and sources."""
        defaults = 'avformat_deps="avcodec avutil"\navcodec_deps="avutil"\n'
        defaults += 'swscale_deps="avutil"\nswresample_deps="avutil"\n'
        (self.tree / "configure").write_text(defaults + configure, encoding="utf-8")
        make_lines = []
        for name, paths in objects.items():
            make_lines.append(f"OBJS-$(CONFIG_{name.upper()}_FILTER) += {' '.join(paths)}")
        (self.tree / "libavfilter" / "Makefile").write_text(
            "\n".join(make_lines) + "\n", encoding="utf-8"
        )
        names = tuple(objects) if registered is None else registered
        registry = "".join(f"extern const FFFilter ff_vf_{name};\n" for name in names)
        (self.tree / "libavfilter" / "allfilters.c").write_text(registry, encoding="utf-8")
        for name, source in sources.items():
            path = self.tree / "libavfilter" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")

    def run_check(self, *extra: str) -> tuple[int, str, str]:
        """Run the public entry point with an empty exception list."""
        return capture_main(
            filter_lib_deps.main,
            "--exceptions",
            str(self.exceptions),
            *extra,
            str(self.tree),
        )

    def test_declared_and_linked_avformat_call_passes(self) -> None:
        """A declared avformat dependency with a conditional edge has no gap."""
        self.write_tree(
            'reader_filter_deps="avformat"\n'
            'enabled reader_filter && prepend avfilter_deps "avformat"\n',
            {"vf_reader.c": ("int run(void) { avcodec_send_packet(); return avio_open(); }\n")},
            {"reader": ["vf_reader.o"]},
        )
        status, out, _ = self.run_check()
        self.assertEqual(status, 0)
        self.assertIn("gaps=0 excepted=0", out)

    def test_planted_whisper_gap_reports_first_call(self) -> None:
        """Whisper fails without both edges and passes after both are declared."""
        self.write_tree(
            'whisper_filter_deps="whisper"\n',
            {
                "af_whisper.c": (
                    "int run(void) {\n"
                    "    avio_open();\n"
                    "    avio_write();\n"
                    "    return avio_closep();\n"
                    "}\n"
                )
            },
            {"whisper": ["af_whisper.o"]},
        )
        status, out, _ = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("| whisper | libavfilter/af_whisper.c:2 (avio_open) | libavformat |", out)
        configure = self.tree / "configure"
        configure.write_text(
            configure.read_text(encoding="utf-8").replace(
                'whisper_filter_deps="whisper"\n',
                'whisper_filter_deps="avformat whisper"\n'
                'enabled whisper_filter && prepend avfilter_deps "avformat"\n',
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.run_check()[0], 0)

    def test_declared_without_conditional_edge_fails(self) -> None:
        """A declared library without its link edge remains a gap."""
        self.write_tree(
            'metadata_filter_deps="avformat"\n',
            {"f_metadata.c": "int run(void) { return avio_open(); }\n"},
            {"metadata": ["f_metadata.o"]},
        )
        status, out, _ = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("| metadata |", out)
        self.assertIn("| yes | no | gap |", out)

    def test_conditional_edge_without_declaration_fails(self) -> None:
        """A conditional swscale edge without a declaration remains a gap."""
        self.write_tree(
            'qrencode_filter_deps="libqrencode"\n'
            'enabled qrencode_filter && prepend avfilter_deps "swscale"\n',
            {"vf_qrencode.c": "int run(void) { return sws_scale(); }\n"},
            {"qrencode": ["vf_qrencode.o"]},
        )
        status, out, _ = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("| no | yes | gap |", out)

    def test_select_adds_subsystem_objects_and_dependencies(self) -> None:
        """A selected subsystem contributes its objects and library dependencies."""
        self.write_tree(
            'detect_filter_select="dnn"\n'
            'dnn_deps="swscale"\n'
            'enabled detect_filter && prepend avfilter_deps "swscale"\n',
            {
                "vf_detect.c": "int run(void) { return 0; }\n",
                "dnn/dnn_io_proc.c": "int run(void) { return sws_scale(); }\n",
            },
            {"detect": ["vf_detect.o"]},
        )
        (self.tree / "libavfilter" / "dnn" / "Makefile").write_text(
            "OBJS-$(CONFIG_DNN) += dnn/dnn_io_proc.o\n", encoding="utf-8"
        )
        status, out, _ = self.run_check()
        self.assertEqual(status, 0)
        self.assertIn("calls=1 gaps=0", out)

    def test_select_expands_conditional_object_group(self) -> None:
        """A selected subsystem includes objects from a referenced conditional group."""
        self.write_tree(
            'detect_filter_select="dnn"\ndnn_deps="avformat"\n',
            {
                "vf_detect.c": "int run(void) { return 0; }\n",
                "dnn/dnn_io_proc.c": "int run(void) { return 0; }\n",
                "dnn/dnn_backend_x.c": "int run(void) { return avio_open(); }\n",
            },
            {"detect": ["vf_detect.o"]},
        )
        (self.tree / "libavfilter" / "dnn" / "Makefile").write_text(
            "OBJS-$(CONFIG_DNN) += dnn/dnn_io_proc.o\n"
            "OBJS-$(CONFIG_DNN) += $(DNN-OBJS-yes)\n"
            "DNN-OBJS-$(CONFIG_LIBX) += dnn/dnn_backend_x.o\n",
            encoding="utf-8",
        )
        status, out, _ = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn(
            "| detect | libavfilter/dnn/dnn_backend_x.c:1 (avio_open) | libavformat |",
            out,
        )

    def test_missing_conditional_object_group_adds_nothing(self) -> None:
        """A referenced object group without definitions adds no objects or failures."""
        self.write_tree(
            'detect_filter_select="dnn"\n',
            {
                "vf_detect.c": "int run(void) { return 0; }\n",
                "dnn/dnn_backend_x.c": "int run(void) { return avio_open(); }\n",
            },
            {"detect": ["vf_detect.o"]},
        )
        (self.tree / "libavfilter" / "dnn" / "Makefile").write_text(
            "OBJS-$(CONFIG_DNN) += $(MISSING-yes)\n", encoding="utf-8"
        )
        status, out, _ = self.run_check()
        self.assertEqual(status, 0)
        self.assertIn("calls=0 gaps=0", out)

    def test_exact_library_guard_excuses_call(self) -> None:
        """An exact CONFIG_SWRESAMPLE branch makes its swr call optional."""
        self.write_tree(
            "",
            {
                "af_meter.c": (
                    "#if CONFIG_SWRESAMPLE\nint run(void) { return swr_convert(); }\n#endif\n"
                )
            },
            {"meter": ["af_meter.o"]},
        )
        status, out, _ = self.run_check()
        self.assertEqual(status, 0)
        self.assertIn("calls=0 gaps=0", out)

    def test_other_filter_guard_excuses_only_its_first_branch(self) -> None:
        """Another filter guard is excused while its else branch still counts."""
        self.write_tree(
            "",
            {
                "vf_shared.c": (
                    "#if CONFIG_OTHER_FILTER\n"
                    "int guarded(void) { return avio_open(); }\n"
                    "#else\n"
                    "int fallback(void) { return avio_write(); }\n"
                    "#endif\n"
                )
            },
            {"owner": ["vf_shared.o"]},
        )
        status, out, _ = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("libavfilter/vf_shared.c:4 (avio_write)", out)
        self.assertNotIn("libavfilter/vf_shared.c:2 (avio_open)", out)

    def test_dependency_cycle_terminates(self) -> None:
        """A _deps and _select cycle terminates through the visited set."""
        self.write_tree(
            'cycle_filter_select="first"\nfirst_deps="second"\nsecond_select="first"\n',
            {"vf_cycle.c": "int run(void) { return 0; }\n"},
            {"cycle": ["vf_cycle.o"]},
        )
        self.assertEqual(self.run_check()[0], 0)

    def test_matching_exception_allows_gap(self) -> None:
        """A live matching exception converts a gap into an excepted row."""
        self.write_tree(
            "",
            {"vf_detect.c": "int run(void) { return sws_scale(); }\n"},
            {"detect": ["vf_detect.o"]},
        )
        self.exceptions.write_text(
            "[[exceptions]]\n"
            'filter = "detect"\n'
            'library = "swscale"\n'
            'reason = "upstream tracker 25638"\n'
            'category = "D"\n'
            "entered = 2026-11-08\n",
            encoding="utf-8",
        )
        status, out, _ = self.run_check("--today", "2026-11-09")
        self.assertEqual(status, 0)
        self.assertIn("excepted (D, expires 2027-03-08)", out)
        self.assertIn("excepted=1", out)

    def test_expired_exception_fails(self) -> None:
        """An expired matching exception names itself and no longer excuses a gap."""
        self.write_tree(
            "",
            {"vf_detect.c": "int run(void) { return sws_scale(); }\n"},
            {"detect": ["vf_detect.o"]},
        )
        self.exceptions.write_text(
            "[[exceptions]]\n"
            'filter = "detect"\n'
            'library = "swscale"\n'
            'reason = "temporary configure edge"\n'
            'category = "B"\n'
            "entered = 2026-11-01\n",
            encoding="utf-8",
        )
        status, out, err = self.run_check("--today", "2026-11-08")
        self.assertEqual(status, 1)
        self.assertIn("| gap |", out)
        self.assertIn("expired exception: detect libswscale", err)

    def test_stale_exception_fails(self) -> None:
        """An exception with no corresponding gap is stale and fails."""
        self.write_tree(
            "",
            {"vf_clean.c": "int run(void) { return 0; }\n"},
            {"clean": ["vf_clean.o"]},
        )
        self.exceptions.write_text(
            "[[exceptions]]\n"
            'filter = "clean"\n'
            'library = "avformat"\n'
            'reason = "temporary configure edge"\n'
            'category = "C"\n'
            "entered = 2026-11-08\n",
            encoding="utf-8",
        )
        status, _, err = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("stale exception: clean libavformat", err)

    def test_malformed_exception_is_input_error(self) -> None:
        """An unknown exception field is rejected as malformed input."""
        self.write_tree(
            "",
            {"vf_clean.c": "int run(void) { return 0; }\n"},
            {"clean": ["vf_clean.o"]},
        )
        self.exceptions.write_text(
            "[[exceptions]]\n"
            'filter = "clean"\n'
            'library = "avformat"\n'
            'reason = "temporary configure edge"\n'
            'category = "C"\n'
            "entered = 2026-11-08\n"
            'unknown = "no"\n',
            encoding="utf-8",
        )
        status, _, err = self.run_check()
        self.assertEqual(status, 2)
        self.assertIn("unknown field", err)

    def test_missing_configure_is_input_error(self) -> None:
        """A missing configure file returns the input-error status."""
        self.write_tree(
            "",
            {"vf_clean.c": "int run(void) { return 0; }\n"},
            {"clean": ["vf_clean.o"]},
        )
        (self.tree / "configure").unlink()
        self.assertEqual(self.run_check()[0], 2)

    def test_source_over_byte_bound_is_input_error(self) -> None:
        """A source larger than the named per-file bound is rejected."""
        self.write_tree(
            "",
            {"vf_large.c": "x" * 129},
            {"large": ["vf_large.o"]},
        )
        with unittest.mock.patch.object(filter_lib_deps, "MAX_BYTES_PER_FILE", 128):
            self.assertEqual(self.run_check()[0], 2)

    def test_registered_filter_without_makefile_uses_definition(self) -> None:
        """A registered filter falls back to the source defining its symbol."""
        self.write_tree(
            "",
            {
                "vf_fallback.c": (
                    "const FFFilter ff_vf_fallback = {0};\nint run(void) { return avio_open(); }\n"
                )
            },
            {},
            registered=("fallback",),
        )
        status, out, _ = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("| fallback | libavfilter/vf_fallback.c:2 (avio_open) |", out)


if __name__ == "__main__":
    unittest.main()
