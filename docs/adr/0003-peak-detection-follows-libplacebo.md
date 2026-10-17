# ADR-0003: Peak detection follows libplacebo and the Dolby Vision level 1 peak

- **Status**: Accepted
- **Date**: 2026-10-17
- **Authors**: Lusoris
- **Supersedes**: the "Peak detection" column of the CPU row in ADR-0002,
  section "Filters"

## Context

ADR-0002 adds dynamic peak detection to `vf_tonemap` "after the
`tonemap_opencl` algorithm" and holds every path to `vf_libplacebo` with
`peak_detect=1` at a mean CIEDE2000 of at most 2.3. Two measurements
showed that this cannot be met as written:

- A port of the `tonemap_opencl` detector (`opencl/tonemap.cl`,
  `detect_peak_avg`) reached a mean of 2.43 on the DV5 clip, above the bar.
- libplacebo v7.360.1 does not measure a frame that carries Dolby Vision
  level 1 metadata. `hdr_update_peak` in `src/renderer.c` returns early when
  `avg_pq_y` is set, and the tone map then takes its peak from the level 1
  `max_pq`. On the DV5 clip, `vf_libplacebo` gives the same output with
  `peak_detect=1` and `peak_detect=0` (mean CIEDE2000 0.0 between the two).

`vf_dovi_reshape` removes the Dolby Vision side data, so `vf_tonemap` cannot
see the level 1 block, and a measured peak can never match a reference that
does not measure.

## Decision

### The detector

`vf_tonemap` ports the peak detector of libplacebo v7.360.1
(`src/shaders/colorspace.c`: `pl_shader_detect_peak`, `measure_peak`,
`update_peak_buf`), with
libplacebo's LGPL-2.1-or-later notice kept next to the code:

- per-pixel luminance with the frame's luma coefficients, PQ-encoded at
  14 bits, with libplacebo's black cutoff;
- 16x16 tiles that each contribute an average, a maximum and a histogram;
- the 99.995th percentile of the frame histogram as the frame peak;
- an infinite-impulse-response filter over about 20 frames, reset by scene
  changes.

Two options, the same names and defaults in `vf_tonemap` and in
`tonemap_vulkan`:

| Option | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `peak_detect` | bool | 0 | measure the peak per frame instead of using `peak` or the frame metadata |
| `threshold` | double | 0.2 | scene change window: a change of the average PQ level between `5 * threshold` and `15 * threshold` percent blends from smoothing to a reset; 0 disables scene detection |

With the default 0.2 the window is libplacebo's default of 1 to 3 percent.
With `peak_detect=0` the output is byte-identical to the filter before this
change.

### The Dolby Vision level 1 peak

`vf_dovi_reshape` exports the level 1 block as frame metadata:

| Key | Value |
| :--- | :--- |
| `lavfi.dovi.l1_max_pq` | level 1 `max_pq`, a 12-bit PQ code value (0 to 4095) |
| `lavfi.dovi.l1_avg_pq` | level 1 `avg_pq`, a 12-bit PQ code value (0 to 4095) |

A frame without a level 1 block loses both keys. With `peak_detect=1`,
`vf_tonemap` uses the PQ-decoded `lavfi.dovi.l1_max_pq` as the frame peak
and skips measurement when the key is present, which is libplacebo's rule.
The key names are permanent.

## Consequences

- **Positive**: The CPU path matches `vf_libplacebo` with `peak_detect=1`
  on both test clips. A Dolby Vision frame keeps its mastered per-shot peak
  through `vf_dovi_reshape`, and any later filter can read it.
- **Negative**: Two more frame metadata keys to keep stable. The detector is
  a second port of libplacebo code, pinned to v7.360.1 like the rest of the
  series and re-checked at every FFmpeg bump.

## References

- ADR-0002, which this record amends.
- Operator decision `ffmpeg-patches-dv5-peak-detect-option`, 2026-10-17
  (paraphrased): add `peak_detect` and `threshold` to `vf_tonemap` and
  `tonemap_vulkan`, default output unchanged.
- Operator decision `ffmpeg-patches-dv5-peak-dv-metadata`, 2026-10-17
  (paraphrased): `vf_dovi_reshape` exports the level 1 peak under the two
  keys above, `vf_tonemap` uses it when present, and `threshold` keeps
  libplacebo's window meaning.
