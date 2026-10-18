# hqdn3d on the GPU: speed and quality

Measurement of the GPU `hqdn3d` filters of patches 0023 to 0026 against
the CPU `hqdn3d`, `nlmeans_vulkan` and the AV1 encoders' own denoisers, as
[ADR-0004](../adr/0004-hqdn3d-on-the-gpu.md) plans it. This file holds the
plan first; the results section follows the run.

`hqdn3d_videotoolbox` (0026) is not measured: no Apple host is available.
It compiles on the macOS job of `series.yml`; its speed and its parity with
the CPU filter are unknown.

## Plan

### Builds

- FFmpeg n9.0.2 (`base.env`) with the series of this repository applied by
  `scripts/series.py apply --expect-base`, configured with the Vulkan,
  CUDA, OpenCL, SVT-AV1 and libaom options. The run records the series
  commit, `ffmpeg -version` and the sha256 of the binary.
- Decoding of the AV1 streams uses an FFmpeg with libdav1d, so that film
  grain synthesis can be switched off (`-export_side_data film_grain`).
- VMAF scores come from the VMAFx `vmaf` tool with the `vmaf_v0.6.1` model,
  CPU only (`--backend cpu`), so that scoring takes no GPU memory. The run
  records its version and refuses a score whose `backend_used` is not the
  CPU.

### Content

- The `bbb` and `netflix-bar` clips of the Pelorus benchmark corpus
  (`scripts/bench/corpus.lock` in Pelorus): pinned downloads, verified by
  sha256, extracted to 48 frames of 640x360 yuv420p by its
  `fetch-corpus.sh`. The run records the sha256 of each extracted clip.
- Grain is added to the clean clip with `noise=alls=<n>:allf=t+u:all_seed=1`
  at two levels, n = 10 and n = 20. The clean clip stays the VMAF
  reference, so every arm is scored against the content without the
  impairment it should remove.
- Speed runs use 240 frames of 1920x1080 yuv420p from
  `testsrc2` with the same grain at n = 20, generated once and read from
  the page cache.

### Arms

| Arm | Where | Settings |
| :--- | :--- | :--- |
| none | - | no denoiser |
| hqdn3d | CPU | defaults (4:3:6:4.5), and `luma_spatial=8` |
| hqdn3d_vulkan, hqdn3d_cuda, hqdn3d_opencl | GPU | the same two settings |
| nlmeans_vulkan | GPU | `s=1` (default) and `s=3` |
| SVT-AV1 film grain | encoder | `film-grain=10:film-grain-denoise=1` |
| libaom | encoder | `denoise-noise-level=10` |

### Measures

1. **Parity bound.** Each GPU `hqdn3d` on every device that is free, for
   yuv420p and p010 input of every grained clip and both settings,
   against the CPU filter on the same input: the count of differing samples
   and the largest difference. The comparison rule is the one of
   `tests/test_hqdn3d_vulkan.py` (p010 may write the format maximum where
   the CPU filter writes a value above it). Expected: 0 differences.
2. **Denoiser quality before encoding.** VMAF and PSNR-Y of each filter
   output against the clean clip.
3. **Quality at equal bitrate.** Each filter output encoded with SVT-AV1
   (preset 8, VBR) and with libaom (`cpu-used=6`, VBR) at 150, 300, 600 and
   1200 kbit/s; the encoder arms with their own encoder at the same rates.
   VMAF of the decoded stream against the clean clip, with the achieved
   bitrate. An achieved bitrate more than 10 % off its target is flagged in
   the table, not dropped. The encoder arms are scored with film grain
   synthesis on and off. Across the four rates the run reports the
   Bjøntegaard delta against the arm without a denoiser, with the
   calculation of `scripts/bench/bd_rate.py` in Pelorus. The GPU `hqdn3d`
   arms reuse the encode of the CPU arm when measure 1 found their output
   identical; the table says so.
4. **Speed.** Frames per second of each filter on the 1080p clip, median of
   three runs, including upload to and download from the device; a chain of
   upload and download alone gives the transfer cost per device. Encoder
   speed with and without its denoiser at 1200 kbit/s.

### Devices and host

- Devices: RTX 4090 (Vulkan 0, CUDA 0, OpenCL 2.0), Arc A380 (Vulkan 1,
  OpenCL 1.0), the Radeon iGPU of the Ryzen 9 9950X3D (Vulkan 2).
- A device is used only when it reports enough free memory (1 GiB) before
  the run, read as the device-local heap budget of `VK_EXT_memory_budget`
  (`vulkaninfo`) for every API on that device; a device that does not is skipped and the table names it. The
  run never stops or evicts a model that holds a device.
- Before each run the host must have a load average below 40, 12 GiB
  available memory and 15 GiB free swap; every run is in a
  `systemd-run --user --scope --expand-environment=no -p MemoryHigh=8G`
  scope. Encoders and `vmaf` run with four threads.

### Evidence

The run writes one JSON file of every measurement and the command lines
behind it, next to this file, and the tables below give its sha256.
