# ADR-0002: Dolby Vision reshape and peak detection in FFmpeg's own tone-map filters

- **Status**: Proposed
- **Date**: 2026-10-17
- **Authors**: Lusoris

## Context

Issue #63 tracks the work. FFmpeg n9.0.2 parses the Dolby Vision RPU into
`AV_FRAME_DATA_DOVI_METADATA` (`libavcodec/dovi_rpudec.c`,
`libavutil/dovi_meta.h`), but only `vf_libplacebo` applies it
(`apply_dolbyvision`, `libavfilter/vf_libplacebo.c:1662`). A profile 5 base
layer is IPTPQc2, so every other tone-map path decodes it to the wrong
colours:

| Filter | DV reshape | Dynamic peak detection | Device |
| :--- | :--- | :--- | :--- |
| `vf_libplacebo` | yes | yes (`peak_detect`) | Vulkan |
| `vf_tonemap_opencl` | no | yes (`opencl/tonemap.cl`, `detect_peak_avg`) | OpenCL |
| `vf_tonemap_vaapi` | no | no; the driver uses static metadata | VAAPI |
| `vf_zscale` + `vf_tonemap` | no | no; static `peak` | CPU |
| Vulkan tone-map filter | none exists | | Vulkan |

Linking libplacebo for this one feature brings its tone-map path (about
7,300 lines) and the shader, dispatch and GPU layers it runs on (about
12,700 more). A 4K HDR10 measurement against `vf_libplacebo` found
`tonemap_opencl` faster (3.5 s against 5.5 s for 300 frames) and lighter
(about 440 against 610 MiB of VRAM), but with a mean CIEDE2000 of 8.5 to
libplacebo's output.

VMAFx/vmafx (HDR and Dolby Vision scoring inputs) and VMAFx/pelorus (a
tone-map stage before encoding) both need this. Both apply this series
first, as ADR-0001 sets out.

## Decision

### Scope of this repository

ADR-0001 limits this repository to FFmpeg fixes. A feature that both
consumers need is shared the same way: it lives here once, as patches on
the pinned release, and neither consumer keeps a copy. This series is the
first such feature. Each patch's metadata lists `vmafx` and `pelorus` as
consumers.

### One shared helper

`libavfilter/dovi_reshape.c` and `dovi_reshape.h` hold the only code that
reads `AVDOVIMetadata`. They are internal to libavfilter (`ff_` names, no
public API), so they add nothing to FFmpeg's ABI. The helper:

- converts the fixed-point reshape curves (pivots, polynomial and MMR
  pieces) and the colour matrices (`ycc_to_rgb`, `rgb_to_lms`) into one
  packed float block that GPU kernels take as a constant buffer;
- reshapes one pixel in C, which is the reference every other path is
  tested against;
- bakes a 3D LUT from the same block for paths that cannot run a kernel.

The math follows libplacebo v7.360.1 (`cee9b076f2c6`):
`pl_map_dovi_metadata` (`src/include/libplacebo/utils/libav_internal.h`),
`pl_shader_dovi_reshape` and the IPT decode in `pl_shader_decode_color`
(`src/shaders/colorspace.c`). Ported code keeps libplacebo's
LGPL-2.1-or-later notice next to FFmpeg's, which uses the same licence. Each
kernel language (OpenCL C, GLSL) has one copy of the per-pixel loop, and a
test holds it to the C reference.

### Filters

| Path | DV reshape | Peak detection |
| :--- | :--- | :--- |
| CPU: new `vf_dovi_reshape` before `zscale` | exact, C | added to `vf_tonemap`, after the `tonemap_opencl` algorithm |
| `vf_tonemap_opencl` | exact, kernel | exists |
| `vf_tonemap_vaapi` | 3D LUT through `VAProcFilter3DLUT` | DV level 1 metadata when present; static otherwise |
| new `vf_tonemap_vulkan` | exact, GLSL compute | compute pass with a frame histogram and temporal smoothing |

`vf_dovi_reshape` turns a Dolby Vision frame into BT.2020 PQ and drops the
RPU side data, so `zscale`, `vf_tonemap` and any later filter treat the
frame as plain HDR10.

`vf_tonemap_vaapi` hands the work to the driver and cannot run a kernel.
Where `vaQueryVideoProcFilters` reports `VAProcFilter3DLUT`, the filter feeds
it a LUT that the helper bakes from each new RPU. Where the driver lacks it,
the filter fails on a Dolby Vision frame and names the
`hwmap=derive_device=opencl,tonemap_opencl` route. It never passes the base
layer through unreshaped.

### Parity target

The reference is `vf_libplacebo` of the same FFmpeg build with
`tonemapping=hable:gamut_mode=clip:dithering=none:deband=0`, peak detection
matched to the path under test, and an output of BT.709, limited range,
yuv420p. Every other path uses `hable` as well.

| Measure | Target per path |
| :--- | :--- |
| Mean CIEDE2000 on the DV5 clip | at most 2.3 (one just-noticeable difference) |
| Mean CIEDE2000 on the HDR10 clip | at most 2.3, or the measured value before the series if that is lower |
| 95th percentile CIEDE2000 | reported |
| Wall time, software frames in and out | reported next to `vf_libplacebo` |
| Peak VRAM above idle | reported next to `vf_libplacebo` |

CIEDE2000 is computed in BT.709, gamma 2.4, D65, on every 30th frame. The
script checks itself against Sharma 2005, pair 1. A path that misses 2.3
does not land. Its numbers and the cause go to issue #63 instead.

### Test material

No third-party clip is committed. Fixtures are generated:

- a C test under `libavfilter/tests/` builds `AVDOVIMetadata` with known
  polynomial and MMR curves and checks the helper against values computed
  by hand;
- a DV5 clip comes from an RGB source converted to IPTPQc2 with the inverse
  matrices and encoded by libx265 with `dolbyvision`, which writes profile 5
  RPUs (`libavcodec/libx265.c`, `libavcodec/dovi_rpuenc.c`);
- the HDR10 clip is the synthetic BT.2020 PQ clip of the earlier
  measurement.

A real-content DV5 clip may be used for the parity numbers if one is
available locally. It is named by hash, never committed.

### Patches and tests

One patch per filter, in this order: the helper with its unit test,
`vf_dovi_reshape`, peak detection in `vf_tonemap`, `tonemap_opencl`,
`tonemap_vaapi`, then `tonemap_vulkan`. Each patch comes with a test that
compares its output with the C reference within a stated tolerance, plus a
planted wrong-colour case that the test must reject: a transposed IPT matrix
and a dropped MMR term. Tests of the GPU filters run where the device
exists, as `tests/test_qsv_editlist.py` does, and are skipped elsewhere.

## Consequences

- **Positive**: Dolby Vision profile 5 tone-maps correctly on CPU, OpenCL,
  VAAPI and Vulkan without libplacebo. One helper serves all four paths and
  both consumers. `vf_dovi_reshape` also fixes every HDR10 filter chain
  placed after it.
- **Negative**: The series carries ported libplacebo math that upstream
  keeps fixing (31 changes to these files since v7.360.1). Each port is
  pinned to a libplacebo commit in its patch metadata and is re-checked at
  every FFmpeg bump. The VAAPI path depends on an Intel-only VPP feature and
  is coarser than the exact paths. The series is larger than any fix it
  sits next to, and its upstream route to FFmpeg is still open.
