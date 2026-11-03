# SPDX-FileCopyrightText: 2026 Lusoris
# SPDX-License-Identifier: EUPL-1.2
"""Apply the CUDA libvmaf patches to pinned excerpts and inject driver errors."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH_NUMBERS = ("0040", "0041", "0042")
FORMATS = {
    "AV_PIX_FMT_YUV420P",
    "AV_PIX_FMT_YUV422P",
    "AV_PIX_FMT_YUV444P",
    "AV_PIX_FMT_YUV420P10LE",
    "AV_PIX_FMT_YUV422P10LE",
    "AV_PIX_FMT_YUV444P10LE",
    "AV_PIX_FMT_YUV420P12LE",
    "AV_PIX_FMT_YUV422P12LE",
    "AV_PIX_FMT_YUV444P12LE",
    "AV_PIX_FMT_YUV444P16",
}
HARNESS = r"""
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#define CONFIG_LIBVMAF_CUDA_FILTER 1
#define AVERROR(e) (-(e))
#define AVERROR_EXTERNAL (-1000)
#define CU_MEMORYTYPE_DEVICE 2
enum AVPixelFormat {
    AV_PIX_FMT_YUV420P, AV_PIX_FMT_YUV422P, AV_PIX_FMT_YUV444P,
    AV_PIX_FMT_YUV420P10LE, AV_PIX_FMT_YUV422P10LE, AV_PIX_FMT_YUV444P10LE,
    AV_PIX_FMT_YUV420P12LE, AV_PIX_FMT_YUV422P12LE, AV_PIX_FMT_YUV444P12LE,
    AV_PIX_FMT_YUV444P16
};
typedef uintptr_t CUdeviceptr;
typedef struct { unsigned nb_components; } AVPixFmtDescriptor;
typedef struct { int unused; } VmafContext;
typedef struct { uint8_t *data[3]; int linesize[3]; } AVFrame;
typedef struct {
    uint8_t *data[3]; int stride[3], w[3], h[3], bpc;
} VmafPicture;
typedef struct {
    int srcMemoryType, dstMemoryType;
    CUdeviceptr srcDevice, dstDevice;
    int srcPitch, dstPitch, WidthInBytes, Height;
} CUDA_MEMCPY2D;
typedef struct {
    int (*cuCtxPushCurrent)(void *);
    int (*cuCtxPopCurrent)(void **);
    int (*cuMemcpy2D)(const CUDA_MEMCPY2D *);
} CudaFunctions;
typedef struct { CudaFunctions *cuda_dl; } CUDAInternal;
typedef struct { CUDAInternal *internal; void *cuda_ctx; } AVCUDADeviceContext;
static int alloc_error, push_error, copy_error, pop_error, bpc;
static int pushes, copies, pops, active, geometry = 1;

static const AVPixFmtDescriptor *av_pix_fmt_desc_get(enum AVPixelFormat fmt)
{
    static const AVPixFmtDescriptor desc = { 3 };
    (void) fmt;
    return &desc;
}

static int vmaf_cuda_fetch_preallocated_picture(VmafContext *ctx, VmafPicture *pic)
{
    (void) ctx;
    (void) pic;
    return alloc_error;
}

static int push(void *ctx)
{
    (void) ctx;
    pushes++;
    active += !push_error;
    return push_error;
}

static int pop(void **ctx)
{
    (void) ctx;
    pops++;
    active -= !pop_error;
    return pop_error;
}

static int copy(const CUDA_MEMCPY2D *m)
{
    int i = copies++;
    int width = i ? 32 : 64;
    geometry &= m->srcMemoryType == CU_MEMORYTYPE_DEVICE;
    geometry &= m->dstMemoryType == CU_MEMORYTYPE_DEVICE;
    geometry &= m->srcDevice == (CUdeviceptr) (i + 1);
    geometry &= m->dstDevice == (CUdeviceptr) (i + 4);
    geometry &= m->srcPitch == (i ? 80 : 160);
    geometry &= m->dstPitch == (i ? 96 : 192);
    geometry &= m->WidthInBytes == width * ((bpc + 7) / 8);
    geometry &= m->Height == (i ? 16 : 32);
    return copies == copy_error ? 7 : 0;
}

#include "libavfilter/vf_libvmaf.c"

int main(int argc, char **argv)
{
    CudaFunctions functions = { push, pop, copy };
    CUDAInternal internal = { &functions };
    AVCUDADeviceContext device = { &internal, NULL };
    AVFrame src = { { (uint8_t *) 1, (uint8_t *) 2, (uint8_t *) 3 }, { 160, 80, 80 } };
    VmafPicture dst = {
        { (uint8_t *) 4, (uint8_t *) 5, (uint8_t *) 6 },
        { 192, 96, 96 }, { 64, 32, 32 }, { 32, 16, 16 }, 0
    };
    int ret;
    if (argc != 6)
        return 2;
    alloc_error = atoi(argv[1]);
    push_error = atoi(argv[2]);
    copy_error = atoi(argv[3]);
    pop_error = atoi(argv[4]);
    dst.bpc = bpc = atoi(argv[5]);
    (void) supported_formats;
    ret = copy_picture_data_cuda(NULL, &device, &src, &dst, AV_PIX_FMT_YUV420P);
    printf("%d %d %d %d %d %d\n", ret, pushes, copies, pops, active, geometry);
    return 0;
}
"""


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run local fixture Git commands, the C compiler, or the inert driver harness."""
    result = subprocess.run(  # nosec B603 - fixed local tools and fixture binary.
        args, cwd=cwd, capture_output=True, text=True, check=False, timeout=30
    )
    if result.returncode:
        raise RuntimeError(f"{args[0]} exited {result.returncode}: {result.stderr}")
    return result


class LibvmafPatchesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        """Apply the libvmaf CUDA patches to the fixture and build the mocked-driver harness."""
        if shutil.which("cc") is None:
            raise unittest.SkipTest("CUDA error injection requires a C compiler")
        scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(scratch.cleanup)
        cls.tree = Path(scratch.name)
        run("git", "init", "--quiet", "--template=", cwd=cls.tree)
        target = cls.tree / "libavfilter/vf_libvmaf.c"
        target.parent.mkdir()
        shutil.copyfile(ROOT / "tests/fixtures/libvmaf_cuda/vf_libvmaf.c", target)
        patches = [next((ROOT / "patches").glob(f"{number}-*.patch")) for number in PATCH_NUMBERS]
        run("git", "apply", "--whitespace=error", *(str(path) for path in patches), cwd=cls.tree)
        cls.source = target.read_text()
        target.write_text(cls.source.split("\nstatic int do_vmaf_cuda(", 1)[0] + "\n#endif\n")
        (cls.tree / "harness.c").write_text(HARNESS)
        cls.compile_harness()

    @classmethod
    def compile_harness(cls, output: str = "harness") -> None:
        """Compile harness.c in the scratch tree into the named executable."""
        run(
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "harness.c",
            "-o",
            output,
            cwd=cls.tree,
        )

    def outcome(
        self, *, alloc: int = 0, push: int = 0, copy: int = 0, pop: int = 0, bpc: int = 8
    ) -> list[int]:
        """Run the harness with injected failures and return its printed counters."""
        result = run(
            str(self.tree / "harness"),
            *(str(v) for v in (alloc, push, copy, pop, bpc)),
            cwd=self.tree,
        )
        return [int(value) for value in result.stdout.split()]

    def test_all_planes_and_byte_widths(self) -> None:
        """Successful copies cover three planes with the right geometry at every bit depth."""
        for bpc in (8, 10, 12, 16):
            with self.subTest(bpc=bpc):
                self.assertEqual(self.outcome(bpc=bpc), [0, 1, 3, 1, 0, 1])

    def test_every_copy_failure_pops_context(self) -> None:
        """A copy failure on any plane still pops the CUDA context once."""
        for plane in (1, 2, 3):
            with self.subTest(plane=plane):
                self.assertEqual(self.outcome(copy=plane), [-1000, 1, plane, 1, 0, 1])

    def test_allocation_and_push_failure_do_not_pop(self) -> None:
        """Allocation and push failures return before any pop."""
        self.assertEqual(self.outcome(alloc=1), [-12, 0, 0, 0, 0, 1])
        self.assertEqual(self.outcome(push=1), [-1000, 1, 0, 0, 0, 1])

    def test_pop_failure_is_reported(self) -> None:
        """A pop failure returns AVERROR_EXTERNAL, with or without a copy failure."""
        self.assertEqual(self.outcome(pop=1), [-1000, 1, 3, 1, 1, 1])
        self.assertEqual(self.outcome(copy=2, pop=1), [-1000, 1, 2, 1, 1, 1])

    def assert_cuda_formats(self, source: str) -> None:
        """Assert the CUDA supported_formats array holds exactly FORMATS."""
        array = source.split("supported_formats[] = {", 1)[1].split("};", 1)[0]
        self.assertEqual(set(re.findall(r"AV_PIX_FMT_\w+", array)), FORMATS)

    def test_formats_preserve_third_party_entries(self) -> None:
        """The patched format list keeps every third-party entry."""
        self.assert_cuda_formats(self.source)

    def test_planted_missing_12_bit_entries_are_detected(self) -> None:
        """Removing a 12-bit entry makes the format check fail."""
        for sampling in ("420", "422", "444"):
            with (
                self.subTest(sampling=sampling),
                self.assertRaisesRegex(AssertionError, f"AV_PIX_FMT_YUV{sampling}P12LE"),
            ):
                self.assert_cuda_formats(
                    self.source.replace(
                        f"    AV_PIX_FMT_YUV{sampling}P12LE,\n",
                        "",
                    )
                )

    def test_planted_early_return_is_detected(self) -> None:
        """Restoring the early return leaves the context pushed and is detected."""
        target = self.tree / "libavfilter/vf_libvmaf.c"
        original = target.read_text()
        self.assertEqual(original.count("goto fail;"), 1)
        try:
            mutant = original.replace("goto fail;", "return AVERROR_EXTERNAL;")
            target.write_text(mutant.replace("\nfail:\n", "\n"))
            self.compile_harness("mutant")
            result = run(str(self.tree / "mutant"), "0", "0", "2", "0", "8", cwd=self.tree)
            with self.assertRaisesRegex(AssertionError, "Lists differ"):
                self.assertEqual(
                    [int(value) for value in result.stdout.split()],
                    [-1000, 1, 2, 1, 0, 1],
                )
        finally:
            target.write_text(original)

    def test_planted_added_trailing_space_is_rejected(self) -> None:
        """git apply refuses a patch whose added line ends in a space."""
        patches = [next((ROOT / "patches").glob(f"{n}-*.patch")) for n in PATCH_NUMBERS]
        bad = (
            patches[2]
            .read_text()
            .replace("+            goto fail;\n", "+            goto fail; \n")
        )
        self.assertNotEqual(bad, patches[2].read_text())
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory)
            run("git", "init", "--quiet", "--template=", cwd=tree)
            target = tree / "libavfilter/vf_libvmaf.c"
            target.parent.mkdir()
            shutil.copyfile(ROOT / "tests/fixtures/libvmaf_cuda/vf_libvmaf.c", target)
            (tree / "bad.patch").write_text(bad)
            with self.assertRaisesRegex(RuntimeError, "trailing whitespace"):
                run(
                    "git",
                    "apply",
                    "--whitespace=error",
                    *(str(path) for path in patches[:2]),
                    "bad.patch",
                    cwd=tree,
                )


class PatchWhitespaceTest(unittest.TestCase):
    def assert_added_whitespace(self, patch: str) -> None:
        """Assert no added line in the patch ends in a space or tab."""
        for number, line in enumerate(patch.splitlines(), 1):
            if line.startswith("+") and not line.startswith("+++ "):
                self.assertEqual(line, line.rstrip(" \t"), f"added line {number}")

    def test_series_added_lines_have_no_trailing_whitespace(self) -> None:
        """No patch in the series adds a line with trailing whitespace."""
        for path in sorted((ROOT / "patches").glob("*.patch")):
            with self.subTest(patch=path.name):
                self.assert_added_whitespace(path.read_text())

    def test_planted_trailing_space_and_tab_are_rejected(self) -> None:
        """The whitespace check refuses a trailing space and a trailing tab."""
        self.assert_added_whitespace(" context\n \n+valid\n")
        for whitespace in (" ", "\t"):
            with (
                self.subTest(whitespace=repr(whitespace)),
                self.assertRaisesRegex(AssertionError, "added line"),
            ):
                self.assert_added_whitespace(f"+invalid{whitespace}\n")


if __name__ == "__main__":
    unittest.main()
