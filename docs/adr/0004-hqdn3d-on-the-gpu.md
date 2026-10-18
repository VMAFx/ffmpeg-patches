# ADR-0004: hqdn3d on the GPU, bit-exact to the CPU filter

- **Status**: Accepted
- **Date**: 2026-10-18
- **Authors**: Lusoris

## Context

Users of hardware decode and encode pipelines asked for a light denoiser
that stays on the GPU. The GPU denoisers in FFmpeg n9.0.2 are
`nlmeans_opencl`, `nlmeans_vulkan` and `bilateral_cuda`, plus the fixed
vendor blocks behind `denoise_vaapi` and `vpp_qsv`. Non-local means is too
slow for real-time use at 2160p, and the denoisers built into the AV1
encoders are weak. `hqdn3d` is the light denoiser most guides recommend,
and it exists only as the CPU filter `vf_hqdn3d.c`.

`hqdn3d` is a recursive low-pass filter on integers:

- `precalc_coefs` turns each of the four strengths into a table of 16-bit
  corrections, indexed by the difference between two samples (`LUT_BITS`
  is 4 below 16 bits per sample and 8 at 16 bits).
- The spatial pass runs a horizontal recursion along each row
  (`pixel_ant`) and a vertical recursion down each column (`line_ant`).
- The temporal pass blends every pixel with the filtered previous frame
  (`frame_prev`), which the filter keeps until it is uninitialised or the
  frame size changes.

The x86 assembly and the C code give the same output: on n9.0.2 the md5 of
`hqdn3d` over a noisy 720p clip is identical with and without
`-cpuflags 0` for `yuv420p`, `yuv420p10le`, `yuv444p12le` and
`yuv444p16le`.

## Decision

### Algorithm and parity

Each GPU filter implements the integer algorithm of `vf_hqdn3d.c`
unchanged, so its output is bit-exact to the CPU filter. The recursions
fix the order of work inside a row and a column, but rows and columns are
independent of each other:

1. one invocation per row runs the horizontal recursion and stores the
   16-bit intermediate row;
2. one invocation per column runs the vertical recursion over the stored
   rows and then the temporal blend, and writes the output.

The correction tables are computed on the host by the same code as the
CPU filter and uploaded when a strength changes. Parity is checked as an
md5 of the output after `hwdownload` against `hqdn3d` on the same frames
in the equivalent planar format; any difference fails. Semi-planar inputs
(`nv12`, `p010`, `p012`, `p016`) count as the planar format with the same
depth.

### Names

The filters are `hqdn3d_vulkan`, `hqdn3d_cuda` and `hqdn3d_opencl`. Their
options are the options of `hqdn3d` with the same names, defaults and
derivation of unset strengths: `luma_spatial`, `chroma_spatial`,
`luma_tmp`, `chroma_tmp`. Like the CPU filter they accept runtime commands.
No new option names are added.

### Temporal state

The GPU filters drop the temporal state in the same cases as the CPU
filter: when the filter is uninitialised, when the link is configured
again and when the frame size changes. libavfilter has no seek event; a
seek reaches a filter as a rebuilt filter graph, which starts with fresh
state.

### Backends and order

| Framework | Decision | Reason |
| :--- | :--- | :--- |
| Vulkan | first, `hqdn3d_vulkan` | one shader runs on the RTX 4090, the Arc A380 and the Radeon iGPU of the test host |
| CUDA | second, `hqdn3d_cuda` | same two passes as CUDA kernels |
| OpenCL | third, `hqdn3d_opencl` | same two passes as OpenCL kernels |
| Metal (VideoToolbox) | later, `hqdn3d_videotoolbox` | built on the macOS CI leg; GPU parity stays marked unproven until an Apple host can run the parity test |
| VAAPI, QSV, D3D11, D3D12, AMF | out of scope | these filters drive fixed vendor processing blocks and run no custom kernels, so the algorithm cannot be expressed there |

Each backend lands as its own patch and pull request. Upstream submission
follows later and only through the posting process of this repository.

### Tests

Each backend ships a test that checks:

- bit-exact parity with `hqdn3d` for 8-bit and 10-bit 4:2:0 input and for
  the temporal-only path (a spatial strength set to 0 by a runtime command);
- that a rebuilt filter graph starts from fresh temporal state: the first
  output frame after the rebuild equals the first frame of a fresh run;
- two planted defects that the same checks must catch: a changed
  correction table (parity breach) and temporal state kept across the
  rebuild.

### Measurement

Speed and quality are measured with VMAFx against `hqdn3d`,
`nlmeans_vulkan` and the encoder denoisers (SVT-AV1 film-grain denoise,
libaom `denoise-noise-level`) at equal bitrate. The results go into the
pull request of each backend.

## Consequences

- **Positive**: Hardware pipelines get the common light denoiser without
  a round trip to system memory. Bit-exact parity makes every backend
  testable against one reference, and a user can swap `hqdn3d` for a GPU
  variant without a visible change.
- **Negative**: The recursions limit parallelism to one invocation per row
  and per column, so a frame keeps only a few thousand invocations busy.
  Each backend needs a persistent 16-bit buffer of the frame size per
  plane for the temporal state.

## References

- `libavfilter/vf_hqdn3d.c` and `libavfilter/x86/vf_hqdn3d.asm`, FFmpeg n9.0.2.
- Controller order ffmpeg-patches-hqdn3d-hw-20261017 (paraphrased): a
  hardware `hqdn3d` for every GPU filter framework FFmpeg has, Vulkan
  first, option names mirrored from the CPU filter.
