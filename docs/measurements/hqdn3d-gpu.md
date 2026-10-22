# hqdn3d on the GPU: speed and quality

Measurement of the GPU `hqdn3d` filters of patches 0024 to 0027 against
the CPU `hqdn3d`, `nlmeans_vulkan` and the AV1 encoders' own denoisers, as
[ADR-0004](../adr/0004-hqdn3d-on-the-gpu.md) plans it. The plan comes first,
the results of the main run on 2026-10-18 follow it, and then a second run
the same day on the RTX 4090, which the main run had to skip.

`hqdn3d_videotoolbox` (0027) is not measured: no Apple host is available.
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
  scope. `vmaf` runs with four threads; the encoders run with the thread
  count FFmpeg picks by default.

### Evidence

The run writes one JSON file of every measurement and the command lines
behind it, next to this file, and the tables below give its sha256.

## Results

Run of 2026-10-18, 06:05 to 06:26 UTC, with `scripts/measure_hqdn3d.py`
at commit `011c329` of this branch:

- FFmpeg `n9.0.2-26-g04622fa` (n9.0.2 with patches 0001 to 0026), binary
  sha256 `649795e673624dc2b7fc4f559ca03da24ee64108c062d6da84b23dfe99d315f0`.
- `vmaf` `v3.1.0-2519-g19c8a4be1`, CPU backend on every score.
- Clips: `bbb` sha256 `9fcbaa58…5c6ae9`, `netflix-bar` sha256
  `c1da10e7…6c9f75` (full values in the evidence file).
- Host gate readings before the four stages: load average 25.8 to 31.3,
  23.0 to 27.2 GiB available memory, 79 to 84 GiB free swap. The host ran
  other work at the same time, so the speed figures carry that load.

### What was not measured

- **RTX 4090** (Vulkan 0, CUDA 0, OpenCL 2.0) in this run: its
  device-local budget was 920 MiB, under the 1 GiB bound, for the whole run,
  because other work held the device. The run skipped it (48 parity cases,
  3 speed series) and evicted nothing. The [RTX 4090 run](#rtx-4090-run)
  below measures those cases.
- **`hqdn3d_videotoolbox`**: no Apple host, as stated above.

### Measure 1: parity bound

Every GPU filter that ran gave output identical to the CPU filter: 0
differing samples, largest difference 0, in all 48 cases (2 clips, 2 grain
levels, 2 settings, 2 formats on each backend).

| Backend | Device | yuv420p cases | p010le cases | Differing samples | Largest difference |
| :--- | :--- | ---: | ---: | ---: | ---: |
| `hqdn3d_vulkan`, Vulkan 1 | Arc A380 | 8 | 8 | 0 | 0 |
| `hqdn3d_opencl`, OpenCL 1.0 | Arc A380 | 8 | 8 | 0 | 0 |
| `hqdn3d_vulkan`, Vulkan 2 | Radeon iGPU | 8 | 8 | 0 | 0 |

A planted check confirmed the comparison sees a
difference: the same chain with `luma_spatial=4.5` against the CPU defaults
reported 4 404 756 differing samples.

### Measure 2: denoiser quality before encoding

VMAF and PSNR-Y of each filter output against the clean clip. The GPU
`hqdn3d` filters give the CPU output (measure 1), so the CPU rows stand for
them. `nlmeans_vulkan` ran on the Arc A380.

| Clip        | Noise | Arm                   | Device   | VMAF  | PSNR-Y |
| ----------- | ----- | --------------------- | -------- | ----- | ------ |
| bbb         | 10    | none                  | none     | 94.83 | 38.94  |
| bbb         | 10    | hqdn3d_defaults       | cpu      | 94.98 | 40.61  |
| bbb         | 10    | hqdn3d_luma_spatial=8 | cpu      | 90.95 | 38.67  |
| bbb         | 10    | nlmeans_vulkan_s1     | vulkan:1 | 94.83 | 38.95  |
| bbb         | 10    | nlmeans_vulkan_s3     | vulkan:1 | 91.93 | 38.39  |
| bbb         | 20    | none                  | none     | 89.36 | 32.98  |
| bbb         | 20    | hqdn3d_defaults       | cpu      | 90.12 | 33.98  |
| bbb         | 20    | hqdn3d_luma_spatial=8 | cpu      | 90.57 | 36.11  |
| bbb         | 20    | nlmeans_vulkan_s1     | vulkan:1 | 89.36 | 32.98  |
| bbb         | 20    | nlmeans_vulkan_s3     | vulkan:1 | 88.44 | 34.08  |
| netflix-bar | 10    | none                  | none     | 96.37 | 38.94  |
| netflix-bar | 10    | hqdn3d_defaults       | cpu      | 96.65 | 43.02  |
| netflix-bar | 10    | hqdn3d_luma_spatial=8 | cpu      | 94.45 | 42.91  |
| netflix-bar | 10    | nlmeans_vulkan_s1     | vulkan:1 | 96.37 | 39.50  |
| netflix-bar | 10    | nlmeans_vulkan_s3     | vulkan:1 | 94.62 | 41.52  |
| netflix-bar | 20    | none                  | none     | 93.15 | 32.98  |
| netflix-bar | 20    | hqdn3d_defaults       | cpu      | 93.77 | 34.33  |
| netflix-bar | 20    | hqdn3d_luma_spatial=8 | cpu      | 94.53 | 38.18  |
| netflix-bar | 20    | nlmeans_vulkan_s1     | vulkan:1 | 93.15 | 32.98  |
| netflix-bar | 20    | nlmeans_vulkan_s3     | vulkan:1 | 92.72 | 36.29  |

- `hqdn3d` with its defaults raises VMAF a little at both grain levels
  (+0.15 to +0.76).
- `luma_spatial=8` lowers VMAF at n = 10 (-3.9 on `bbb`, -1.9 on
  `netflix-bar`) and raises it at n = 20 (+1.2, +1.4).
- `nlmeans_vulkan` with `s=1` leaves VMAF unchanged; `s=3` lowers it.

### Measure 3: quality at equal bitrate

Bjøntegaard delta of each arm against the arm without a denoiser on the same
encoder, over the achieved bitrates. Negative BD-rate and positive BD-quality
favour the arm. The GPU `hqdn3d` arms are not encoded separately: measure 1
found their output identical to the CPU filter, so the `hqdn3d` rows are
theirs too. `filmgrain_on` and `_off`, and `denoise_on` and `_off`, are the
same stream decoded with film grain synthesis on and off.

| Clip        | Noise | Arm                 | Encoder | VMAF overlap | BD-Rate (%)                       | BD-Quality |
| ----------- | ----- | ------------------- | ------- | ------------ | --------------------------------- | ---------- |
| bbb         | 10    | svt_hqdn3d_defaults | svt     | 87.19-94.67  | +9.55                             | -0.423     |
| bbb         | 10    | svt_hqdn3d_luma8    | svt     | 85.32-90.59  | +62.75                            | -3.915     |
| bbb         | 10    | svt_filmgrain_on    | svt     | 84.13-94.60  | +12.21                            | -0.597     |
| bbb         | 10    | svt_filmgrain_off   | svt     | 84.09-94.66  | +13.58                            | -0.563     |
| bbb         | 10    | aom_hqdn3d_defaults | aom     | 89.38-94.83  | +24.19                            | -0.441     |
| bbb         | 10    | aom_hqdn3d_luma8    | aom     | 87.94-90.60  | +84.10                            | -4.334     |
| bbb         | 10    | aom_denoise_on      | aom     | 88.21-94.82  | +16.24                            | -0.393     |
| bbb         | 10    | aom_denoise_off     | aom     | 88.25-94.90  | +14.26                            | -0.329     |
| bbb         | 20    | svt_hqdn3d_defaults | svt     | 72.17-91.37  | n/a: score falls as bitrate rises | -0.370     |
| bbb         | 20    | svt_hqdn3d_luma8    | svt     | 82.39-91.25  | n/a: score falls as bitrate rises | -0.755     |
| bbb         | 20    | svt_filmgrain_on    | svt     | 71.33-90.88  | n/a: score falls as bitrate rises | -1.100     |
| bbb         | 20    | svt_filmgrain_off   | svt     | 71.33-90.88  | n/a: score falls as bitrate rises | -1.088     |
| bbb         | 20    | aom_hqdn3d_defaults | aom     | 81.07-92.25  | +35.51                            | +0.136     |
| bbb         | 20    | aom_hqdn3d_luma8    | aom     | 85.70-91.40  | +8388.30                          | -1.000     |
| bbb         | 20    | aom_denoise_on      | aom     | 78.22-92.10  | +7.91                             | -0.559     |
| bbb         | 20    | aom_denoise_off     | aom     | 78.23-92.14  | +7.30                             | -0.527     |
| netflix-bar | 10    | svt_hqdn3d_defaults | svt     | 85.74-95.25  | +0.57                             | -0.086     |
| netflix-bar | 10    | svt_hqdn3d_luma8    | svt     | 86.37-93.61  | +45.68                            | -2.147     |
| netflix-bar | 10    | svt_filmgrain_on    | svt     | 81.24-95.21  | +26.84                            | -1.374     |
| netflix-bar | 10    | svt_filmgrain_off   | svt     | 81.24-95.25  | +29.52                            | -1.273     |
| netflix-bar | 10    | aom_hqdn3d_defaults | aom     | 91.09-95.94  | +12.86                            | -0.480     |
| netflix-bar | 10    | aom_hqdn3d_luma8    | aom     | 90.03-93.76  | +75.50                            | -2.727     |
| netflix-bar | 10    | aom_denoise_on      | aom     | 89.34-95.81  | +18.38                            | -0.837     |
| netflix-bar | 10    | aom_denoise_off     | aom     | 89.42-95.94  | +16.02                            | -0.741     |
| netflix-bar | 20    | svt_hqdn3d_defaults | svt     | 78.59-92.72  | +1.03                             | +0.586     |
| netflix-bar | 20    | svt_hqdn3d_luma8    | svt     | 80.68-92.72  | +5.30                             | +0.856     |
| netflix-bar | 20    | svt_filmgrain_on    | svt     | 77.92-92.72  | +16.53                            | -1.070     |
| netflix-bar | 20    | svt_filmgrain_off   | svt     | 77.92-92.72  | +16.04                            | -1.051     |
| netflix-bar | 20    | aom_hqdn3d_defaults | aom     | 79.21-93.23  | +59.90                            | -0.072     |
| netflix-bar | 20    | aom_hqdn3d_luma8    | aom     | 88.35-93.23  | +25.76                            | -0.512     |
| netflix-bar | 20    | aom_denoise_on      | aom     | 79.21-93.23  | +28.81                            | -1.963     |
| netflix-bar | 20    | aom_denoise_off     | aom     | 79.21-93.23  | +28.16                            | -1.914     |

- BD-rate is `n/a` where a curve's VMAF falls as its bitrate rises (`bbb`
  at n = 20 on SVT-AV1: 91.33 at 600 kbit/s, 90.85 at 1200 kbit/s for the
  arm without a denoiser). The cubic fit of `bd_rate.py` has no meaning on
  such a curve; BD-quality, integrated over the bitrate, is still given.
- The "VMAF overlap" column is the score range both curves share, over
  which BD-rate integrates. `bbb` n = 20, libaom, `luma8` rests on a curve
  that saturates (91.34 at 1110 kbit/s, 91.40 at 3361 kbit/s); its +8388 %
  is an artefact of the fit, not a cost.
- With SVT-AV1, `hqdn3d` with its defaults is close to neutral on
  `netflix-bar` (+0.6 % and +1.0 %) and costs +9.6 % on `bbb` at n = 10.
  With libaom it costs +12.9 % to +59.9 %.
- The encoders' own denoisers cost bitrate at equal VMAF against the clean
  clip wherever BD-rate is defined (+7.3 % to +29.5 %), with grain
  synthesis on or off.
- Rate control misses its target by more than 10 % in 130 of the 160
  points (48-frame clips, VBR); the table below flags each one. The deltas
  use the achieved bitrates, so a miss moves a point along its curve
  instead of mislabelling it; where two curves end at different rates it
  narrows the range they share.

<details>
<summary>Every rate point (160 encodes)</summary>

| Clip        | Noise | Arm             | Encoder | Grain synthesis | Target (kbps) | Achieved (kbps) | VMAF  | Flagged |
| ----------- | ----- | --------------- | ------- | --------------- | ------------- | --------------- | ----- | ------- |
| bbb         | 10    | none            | svt     | off             | 150           | 160.8           | 83.06 | ok      |
| bbb         | 10    | none            | svt     | off             | 300           | 399.2           | 91.46 | FLAG    |
| bbb         | 10    | none            | svt     | off             | 600           | 767.2           | 93.77 | FLAG    |
| bbb         | 10    | none            | svt     | off             | 1200          | 1292.0          | 94.83 | ok      |
| bbb         | 10    | hqdn3d_defaults | svt     | off             | 150           | 238.6           | 87.19 | FLAG    |
| bbb         | 10    | hqdn3d_defaults | svt     | off             | 300           | 479.9           | 91.81 | FLAG    |
| bbb         | 10    | hqdn3d_defaults | svt     | off             | 600           | 824.1           | 93.67 | FLAG    |
| bbb         | 10    | hqdn3d_defaults | svt     | off             | 1200          | 1418.3          | 94.67 | FLAG    |
| bbb         | 10    | hqdn3d_luma8    | svt     | off             | 150           | 271.7           | 85.32 | FLAG    |
| bbb         | 10    | hqdn3d_luma8    | svt     | off             | 300           | 494.9           | 88.67 | FLAG    |
| bbb         | 10    | hqdn3d_luma8    | svt     | off             | 600           | 889.2           | 89.95 | FLAG    |
| bbb         | 10    | hqdn3d_luma8    | svt     | off             | 1200          | 1507.5          | 90.59 | FLAG    |
| bbb         | 10    | none            | aom     | off             | 150           | 332.9           | 87.94 | FLAG    |
| bbb         | 10    | none            | aom     | off             | 300           | 533.8           | 91.78 | FLAG    |
| bbb         | 10    | none            | aom     | off             | 600           | 1000.1          | 94.31 | FLAG    |
| bbb         | 10    | none            | aom     | off             | 1200          | 2418.6          | 95.19 | FLAG    |
| bbb         | 10    | hqdn3d_defaults | aom     | off             | 150           | 403.5           | 89.38 | FLAG    |
| bbb         | 10    | hqdn3d_defaults | aom     | off             | 300           | 663.2           | 92.54 | FLAG    |
| bbb         | 10    | hqdn3d_defaults | aom     | off             | 600           | 1213.1          | 94.19 | FLAG    |
| bbb         | 10    | hqdn3d_defaults | aom     | off             | 1200          | 2789.5          | 94.83 | FLAG    |
| bbb         | 10    | hqdn3d_luma8    | aom     | off             | 150           | 427.1           | 86.59 | FLAG    |
| bbb         | 10    | hqdn3d_luma8    | aom     | off             | 300           | 736.6           | 89.25 | FLAG    |
| bbb         | 10    | hqdn3d_luma8    | aom     | off             | 600           | 1328.3          | 90.19 | FLAG    |
| bbb         | 10    | hqdn3d_luma8    | aom     | off             | 1200          | 2893.2          | 90.60 | FLAG    |
| bbb         | 10    | svt_filmgrain   | svt     | on              | 150           | 205.5           | 84.13 | FLAG    |
| bbb         | 10    | svt_filmgrain   | svt     | on              | 300           | 419.2           | 91.22 | FLAG    |
| bbb         | 10    | svt_filmgrain   | svt     | on              | 600           | 753.0           | 93.51 | FLAG    |
| bbb         | 10    | svt_filmgrain   | svt     | on              | 1200          | 1305.6          | 94.60 | ok      |
| bbb         | 10    | svt_filmgrain   | svt     | off             | 150           | 205.5           | 84.09 | FLAG    |
| bbb         | 10    | svt_filmgrain   | svt     | off             | 300           | 419.2           | 91.25 | FLAG    |
| bbb         | 10    | svt_filmgrain   | svt     | off             | 600           | 753.6           | 93.57 | FLAG    |
| bbb         | 10    | svt_filmgrain   | svt     | off             | 1200          | 1320.6          | 94.66 | FLAG    |
| bbb         | 10    | aom_denoise     | aom     | on              | 150           | 371.8           | 88.21 | FLAG    |
| bbb         | 10    | aom_denoise     | aom     | on              | 300           | 580.6           | 91.85 | FLAG    |
| bbb         | 10    | aom_denoise     | aom     | on              | 600           | 1059.3          | 94.14 | FLAG    |
| bbb         | 10    | aom_denoise     | aom     | on              | 1200          | 2464.8          | 94.82 | FLAG    |
| bbb         | 10    | aom_denoise     | aom     | off             | 150           | 371.8           | 88.25 | FLAG    |
| bbb         | 10    | aom_denoise     | aom     | off             | 300           | 580.6           | 91.90 | FLAG    |
| bbb         | 10    | aom_denoise     | aom     | off             | 600           | 1059.3          | 94.21 | FLAG    |
| bbb         | 10    | aom_denoise     | aom     | off             | 1200          | 2464.8          | 94.90 | FLAG    |
| bbb         | 20    | none            | svt     | off             | 150           | 88.9            | 71.33 | FLAG    |
| bbb         | 20    | none            | svt     | off             | 300           | 331.1           | 87.70 | FLAG    |
| bbb         | 20    | none            | svt     | off             | 600           | 721.8           | 91.37 | FLAG    |
| bbb         | 20    | none            | svt     | off             | 1200          | 1115.2          | 90.87 | ok      |
| bbb         | 20    | hqdn3d_defaults | svt     | off             | 150           | 90.5            | 72.17 | FLAG    |
| bbb         | 20    | hqdn3d_defaults | svt     | off             | 300           | 396.8           | 88.34 | FLAG    |
| bbb         | 20    | hqdn3d_defaults | svt     | off             | 600           | 756.9           | 91.51 | FLAG    |
| bbb         | 20    | hqdn3d_defaults | svt     | off             | 1200          | 1169.7          | 91.38 | ok      |
| bbb         | 20    | hqdn3d_luma8    | svt     | off             | 150           | 184.9           | 82.39 | FLAG    |
| bbb         | 20    | hqdn3d_luma8    | svt     | off             | 300           | 460.5           | 88.53 | FLAG    |
| bbb         | 20    | hqdn3d_luma8    | svt     | off             | 600           | 835.0           | 90.76 | FLAG    |
| bbb         | 20    | hqdn3d_luma8    | svt     | off             | 1200          | 1443.1          | 91.25 | FLAG    |
| bbb         | 20    | none            | aom     | off             | 150           | 134.7           | 76.71 | FLAG    |
| bbb         | 20    | none            | aom     | off             | 300           | 290.1           | 85.82 | ok      |
| bbb         | 20    | none            | aom     | off             | 600           | 528.2           | 90.20 | FLAG    |
| bbb         | 20    | none            | aom     | off             | 1200          | 1878.2          | 92.25 | FLAG    |
| bbb         | 20    | hqdn3d_defaults | aom     | off             | 150           | 187.9           | 81.07 | FLAG    |
| bbb         | 20    | hqdn3d_defaults | aom     | off             | 300           | 347.7           | 87.67 | FLAG    |
| bbb         | 20    | hqdn3d_defaults | aom     | off             | 600           | 641.9           | 91.26 | ok      |
| bbb         | 20    | hqdn3d_defaults | aom     | off             | 1200          | 2333.3          | 92.27 | FLAG    |
| bbb         | 20    | hqdn3d_luma8    | aom     | off             | 150           | 306.0           | 85.70 | FLAG    |
| bbb         | 20    | hqdn3d_luma8    | aom     | off             | 300           | 514.3           | 89.06 | FLAG    |
| bbb         | 20    | hqdn3d_luma8    | aom     | off             | 600           | 1110.6          | 91.34 | FLAG    |
| bbb         | 20    | hqdn3d_luma8    | aom     | off             | 1200          | 3360.7          | 91.40 | FLAG    |
| bbb         | 20    | svt_filmgrain   | svt     | on              | 150           | 115.2           | 71.25 | FLAG    |
| bbb         | 20    | svt_filmgrain   | svt     | on              | 300           | 359.4           | 87.75 | FLAG    |
| bbb         | 20    | svt_filmgrain   | svt     | on              | 600           | 734.9           | 90.88 | FLAG    |
| bbb         | 20    | svt_filmgrain   | svt     | on              | 1200          | 1108.2          | 90.75 | ok      |
| bbb         | 20    | svt_filmgrain   | svt     | off             | 150           | 114.8           | 71.25 | FLAG    |
| bbb         | 20    | svt_filmgrain   | svt     | off             | 300           | 359.3           | 87.78 | FLAG    |
| bbb         | 20    | svt_filmgrain   | svt     | off             | 600           | 736.5           | 90.88 | FLAG    |
| bbb         | 20    | svt_filmgrain   | svt     | off             | 1200          | 1078.9          | 90.74 | FLAG    |
| bbb         | 20    | aom_denoise     | aom     | on              | 150           | 176.7           | 78.22 | FLAG    |
| bbb         | 20    | aom_denoise     | aom     | on              | 300           | 329.7           | 86.16 | ok      |
| bbb         | 20    | aom_denoise     | aom     | on              | 600           | 568.9           | 90.31 | ok      |
| bbb         | 20    | aom_denoise     | aom     | on              | 1200          | 1898.6          | 92.10 | FLAG    |
| bbb         | 20    | aom_denoise     | aom     | off             | 150           | 176.7           | 78.23 | FLAG    |
| bbb         | 20    | aom_denoise     | aom     | off             | 300           | 329.7           | 86.18 | ok      |
| bbb         | 20    | aom_denoise     | aom     | off             | 600           | 568.9           | 90.35 | ok      |
| bbb         | 20    | aom_denoise     | aom     | off             | 1200          | 1898.6          | 92.14 | FLAG    |
| netflix-bar | 10    | none            | svt     | off             | 150           | 145.4           | 81.24 | ok      |
| netflix-bar | 10    | none            | svt     | off             | 300           | 277.4           | 90.04 | ok      |
| netflix-bar | 10    | none            | svt     | off             | 600           | 541.2           | 93.78 | ok      |
| netflix-bar | 10    | none            | svt     | off             | 1200          | 1100.8          | 95.25 | ok      |
| netflix-bar | 10    | hqdn3d_defaults | svt     | off             | 150           | 191.5           | 85.74 | FLAG    |
| netflix-bar | 10    | hqdn3d_defaults | svt     | off             | 300           | 329.8           | 90.99 | ok      |
| netflix-bar | 10    | hqdn3d_defaults | svt     | off             | 600           | 614.4           | 94.25 | ok      |
| netflix-bar | 10    | hqdn3d_defaults | svt     | off             | 1200          | 1323.6          | 95.64 | FLAG    |
| netflix-bar | 10    | hqdn3d_luma8    | svt     | off             | 150           | 242.4           | 86.37 | FLAG    |
| netflix-bar | 10    | hqdn3d_luma8    | svt     | off             | 300           | 408.5           | 90.38 | FLAG    |
| netflix-bar | 10    | hqdn3d_luma8    | svt     | off             | 600           | 747.8           | 92.63 | FLAG    |
| netflix-bar | 10    | hqdn3d_luma8    | svt     | off             | 1200          | 1423.0          | 93.61 | FLAG    |
| netflix-bar | 10    | none            | aom     | off             | 150           | 220.6           | 88.34 | FLAG    |
| netflix-bar | 10    | none            | aom     | off             | 300           | 375.7           | 92.65 | FLAG    |
| netflix-bar | 10    | none            | aom     | off             | 600           | 584.5           | 94.74 | ok      |
| netflix-bar | 10    | none            | aom     | off             | 1200          | 1009.8          | 95.94 | FLAG    |
| netflix-bar | 10    | hqdn3d_defaults | aom     | off             | 150           | 332.6           | 91.09 | FLAG    |
| netflix-bar | 10    | hqdn3d_defaults | aom     | off             | 300           | 517.1           | 93.80 | FLAG    |
| netflix-bar | 10    | hqdn3d_defaults | aom     | off             | 600           | 824.6           | 95.26 | FLAG    |
| netflix-bar | 10    | hqdn3d_defaults | aom     | off             | 1200          | 1328.6          | 96.11 | FLAG    |
| netflix-bar | 10    | hqdn3d_luma8    | aom     | off             | 150           | 376.9           | 90.03 | FLAG    |
| netflix-bar | 10    | hqdn3d_luma8    | aom     | off             | 300           | 568.9           | 91.91 | FLAG    |
| netflix-bar | 10    | hqdn3d_luma8    | aom     | off             | 600           | 894.3           | 93.03 | FLAG    |
| netflix-bar | 10    | hqdn3d_luma8    | aom     | off             | 1200          | 1543.6          | 93.76 | FLAG    |
| netflix-bar | 10    | svt_filmgrain   | svt     | on              | 150           | 200.9           | 80.99 | FLAG    |
| netflix-bar | 10    | svt_filmgrain   | svt     | on              | 300           | 350.2           | 90.26 | FLAG    |
| netflix-bar | 10    | svt_filmgrain   | svt     | on              | 600           | 584.5           | 93.64 | ok      |
| netflix-bar | 10    | svt_filmgrain   | svt     | on              | 1200          | 1113.8          | 95.21 | ok      |
| netflix-bar | 10    | svt_filmgrain   | svt     | off             | 150           | 200.8           | 81.02 | FLAG    |
| netflix-bar | 10    | svt_filmgrain   | svt     | off             | 300           | 350.2           | 90.33 | FLAG    |
| netflix-bar | 10    | svt_filmgrain   | svt     | off             | 600           | 582.5           | 93.77 | ok      |
| netflix-bar | 10    | svt_filmgrain   | svt     | off             | 1200          | 1116.1          | 95.29 | ok      |
| netflix-bar | 10    | aom_denoise     | aom     | on              | 150           | 305.6           | 89.34 | FLAG    |
| netflix-bar | 10    | aom_denoise     | aom     | on              | 300           | 456.9           | 92.77 | FLAG    |
| netflix-bar | 10    | aom_denoise     | aom     | on              | 600           | 691.9           | 94.73 | FLAG    |
| netflix-bar | 10    | aom_denoise     | aom     | on              | 1200          | 1125.4          | 95.81 | ok      |
| netflix-bar | 10    | aom_denoise     | aom     | off             | 150           | 305.6           | 89.42 | FLAG    |
| netflix-bar | 10    | aom_denoise     | aom     | off             | 300           | 456.9           | 92.86 | FLAG    |
| netflix-bar | 10    | aom_denoise     | aom     | off             | 600           | 691.9           | 94.83 | FLAG    |
| netflix-bar | 10    | aom_denoise     | aom     | off             | 1200          | 1125.4          | 95.94 | ok      |
| netflix-bar | 20    | none            | svt     | off             | 150           | 126.1           | 77.92 | FLAG    |
| netflix-bar | 20    | none            | svt     | off             | 300           | 210.9           | 84.37 | FLAG    |
| netflix-bar | 20    | none            | svt     | off             | 600           | 437.1           | 90.65 | FLAG    |
| netflix-bar | 20    | none            | svt     | off             | 1200          | 1066.3          | 92.72 | FLAG    |
| netflix-bar | 20    | hqdn3d_defaults | svt     | off             | 150           | 126.7           | 78.59 | FLAG    |
| netflix-bar | 20    | hqdn3d_defaults | svt     | off             | 300           | 241.0           | 87.28 | FLAG    |
| netflix-bar | 20    | hqdn3d_defaults | svt     | off             | 600           | 506.9           | 91.41 | FLAG    |
| netflix-bar | 20    | hqdn3d_defaults | svt     | off             | 1200          | 1260.2          | 93.30 | ok      |
| netflix-bar | 20    | hqdn3d_luma8    | svt     | off             | 150           | 147.6           | 80.68 | ok      |
| netflix-bar | 20    | hqdn3d_luma8    | svt     | off             | 300           | 296.0           | 89.38 | ok      |
| netflix-bar | 20    | hqdn3d_luma8    | svt     | off             | 600           | 574.9           | 92.20 | ok      |
| netflix-bar | 20    | hqdn3d_luma8    | svt     | off             | 1200          | 1516.7          | 93.93 | FLAG    |
| netflix-bar | 20    | none            | aom     | off             | 150           | 131.3           | 79.21 | FLAG    |
| netflix-bar | 20    | none            | aom     | off             | 300           | 204.3           | 86.68 | FLAG    |
| netflix-bar | 20    | none            | aom     | off             | 600           | 360.9           | 91.73 | FLAG    |
| netflix-bar | 20    | none            | aom     | off             | 1200          | 626.0           | 93.23 | FLAG    |
| netflix-bar | 20    | hqdn3d_defaults | aom     | off             | 150           | 130.6           | 78.55 | FLAG    |
| netflix-bar | 20    | hqdn3d_defaults | aom     | off             | 300           | 259.8           | 89.29 | FLAG    |
| netflix-bar | 20    | hqdn3d_defaults | aom     | off             | 600           | 426.6           | 92.51 | FLAG    |
| netflix-bar | 20    | hqdn3d_defaults | aom     | off             | 1200          | 906.2           | 93.93 | FLAG    |
| netflix-bar | 20    | hqdn3d_luma8    | aom     | off             | 150           | 246.9           | 88.35 | FLAG    |
| netflix-bar | 20    | hqdn3d_luma8    | aom     | off             | 300           | 409.8           | 91.70 | FLAG    |
| netflix-bar | 20    | hqdn3d_luma8    | aom     | off             | 600           | 704.0           | 93.57 | FLAG    |
| netflix-bar | 20    | hqdn3d_luma8    | aom     | off             | 1200          | 1820.2          | 94.69 | FLAG    |
| netflix-bar | 20    | svt_filmgrain   | svt     | on              | 150           | 174.4           | 77.49 | FLAG    |
| netflix-bar | 20    | svt_filmgrain   | svt     | on              | 300           | 240.8           | 84.17 | FLAG    |
| netflix-bar | 20    | svt_filmgrain   | svt     | on              | 600           | 484.3           | 90.68 | FLAG    |
| netflix-bar | 20    | svt_filmgrain   | svt     | on              | 1200          | 1085.3          | 92.77 | ok      |
| netflix-bar | 20    | svt_filmgrain   | svt     | off             | 150           | 174.5           | 77.53 | FLAG    |
| netflix-bar | 20    | svt_filmgrain   | svt     | off             | 300           | 240.7           | 84.20 | FLAG    |
| netflix-bar | 20    | svt_filmgrain   | svt     | off             | 600           | 491.6           | 90.73 | FLAG    |
| netflix-bar | 20    | svt_filmgrain   | svt     | off             | 1200          | 1055.0          | 92.83 | FLAG    |
| netflix-bar | 20    | aom_denoise     | aom     | on              | 150           | 186.5           | 78.87 | FLAG    |
| netflix-bar | 20    | aom_denoise     | aom     | on              | 300           | 269.4           | 87.27 | FLAG    |
| netflix-bar | 20    | aom_denoise     | aom     | on              | 600           | 428.7           | 91.72 | FLAG    |
| netflix-bar | 20    | aom_denoise     | aom     | on              | 1200          | 690.1           | 93.37 | FLAG    |
| netflix-bar | 20    | aom_denoise     | aom     | off             | 150           | 186.5           | 78.91 | FLAG    |
| netflix-bar | 20    | aom_denoise     | aom     | off             | 300           | 269.4           | 87.31 | FLAG    |
| netflix-bar | 20    | aom_denoise     | aom     | off             | 600           | 428.7           | 91.77 | FLAG    |
| netflix-bar | 20    | aom_denoise     | aom     | off             | 1200          | 690.1           | 93.44 | FLAG    |

</details>

### Measure 4: speed

Frames per second on 240 frames of 1080p yuv420p, median of three runs. Each
run is one `ffmpeg` process, so the figure includes process start and device
setup. `transfer_baseline` is `hwupload,hwdownload` alone on the device. A
series stops at the first run over 120 s and the table prints the bound.

| Task                         | Device     | Median FPS          | Run 1 | Run 2 | Run 3 |
| ---------------------------- | ---------- | ------------------- | ----- | ----- | ----- |
| hqdn3d_defaults              | cpu        | 215.7               | 224.5 | 212.5 | 215.7 |
| hqdn3d_luma_spatial=8        | cpu        | 216.7               | 221.3 | 216.7 | 215.9 |
| transfer_baseline            | vulkan:1   | 32.7                | 31.8  | 32.7  | 34.0  |
| hqdn3d_vulkan_defaults       | vulkan:1   | 29.5                | 27.7  | 29.5  | 29.7  |
| hqdn3d_vulkan_luma_spatial=8 | vulkan:1   | 28.2                | 28.2  | 30.0  | 25.9  |
| nlmeans_vulkan_s1            | vulkan:1   | 5.4                 | 5.4   | 5.4   | 5.3   |
| nlmeans_vulkan_s3            | vulkan:1   | 5.3                 | 5.3   | 5.3   | 5.4   |
| transfer_baseline            | opencl:1.0 | 478.1               | 240.7 | 478.1 | 520.2 |
| hqdn3d_opencl_defaults       | opencl:1.0 | 180.2               | 183.2 | 180.2 | 174.6 |
| hqdn3d_opencl_luma_spatial=8 | opencl:1.0 | 159.4               | 169.0 | 149.0 | 159.4 |
| transfer_baseline            | vulkan:2   | 411.8               | 400.8 | 411.8 | 412.5 |
| hqdn3d_vulkan_defaults       | vulkan:2   | 73.5                | 73.5  | 74.3  | 68.5  |
| hqdn3d_vulkan_luma_spatial=8 | vulkan:2   | 72.5                | 68.8  | 74.7  | 72.5  |
| nlmeans_vulkan_s1            | vulkan:2   | < 2.0 (run > 120 s) | -     | -     | -     |
| nlmeans_vulkan_s3            | vulkan:2   | < 2.0 (run > 120 s) | -     | -     | -     |
| encoder_svt_plain_1200k      | cpu        | 110.7               | 101.3 | 114.1 | 110.7 |
| encoder_svt_denoise_1200k    | cpu        | 52.3                | 51.2  | 52.3  | 54.5  |
| encoder_aom_plain_1200k      | cpu        | 11.7                | 11.7  | 11.6  | 11.8  |
| encoder_aom_denoise_1200k    | cpu        | 2.7                 | 2.5   | 2.7   | 3.2   |

- The CPU `hqdn3d` runs at 216 fps. Every GPU path is slower in this setup,
  because the frames start and end in system memory: on the Arc A380 the
  Vulkan upload and download alone reach 32.7 fps, and the filter adds
  little to that (29.5 fps); OpenCL on the same card reaches 180 fps; the
  Radeon iGPU reaches 73.5 fps with Vulkan.
- The run does not measure a chain that keeps the frames on the device (a
  hardware decoder or encoder next to the filter), where the transfer cost
  falls away.
- `nlmeans_vulkan` runs at 5.4 fps on the Arc A380 and under 2 fps on the
  Radeon iGPU.
- SVT-AV1 at preset 8 drops from 110.7 to 52.3 fps with its film grain
  denoiser; libaom at `cpu-used=6` drops from 11.7 to 2.7 fps with
  `denoise-noise-level=10`.

### RTX 4090 run

Run of 2026-10-18, 13:33 to 13:38 UTC, with `scripts/measure_hqdn3d.py` at
commit `117822d` of this repository, the parity and speed stages only, on
the three RTX 4090 backends and the CPU. The FFmpeg binary is the one of the
main run (same sha256). The run started once the device-local budget of the
card had stayed at 1 GiB or more for four reads 30 s apart (9 000 MiB at
the start). Host gate readings: load average 5.8 and 5.1, 26.3 to 26.7 GiB
available memory, 94 GiB free swap.

Parity: every backend gave output identical to the CPU filter, 0 differing
samples and largest difference 0, in all 48 cases (2 clips, 2 grain levels,
2 settings, 2 formats on each backend).

| Backend | Device | yuv420p cases | p010le cases | Differing samples | Largest difference |
| :--- | :--- | ---: | ---: | ---: | ---: |
| `hqdn3d_vulkan`, Vulkan 0 | RTX 4090 | 8 | 8 | 0 | 0 |
| `hqdn3d_cuda`, CUDA 0 | RTX 4090 | 8 | 8 | 0 | 0 |
| `hqdn3d_opencl`, OpenCL 2.0 | RTX 4090 | 8 | 8 | 0 | 0 |

Speed, measured as in Measure 4:

| Task                         | Device     | Median FPS | Run 1 | Run 2 | Run 3 |
| ---------------------------- | ---------- | ---------- | ----- | ----- | ----- |
| hqdn3d_defaults              | cpu        | 257.5      | 263.4 | 257.5 | 256.7 |
| hqdn3d_luma_spatial=8        | cpu        | 261.3      | 260.3 | 264.4 | 261.3 |
| transfer_baseline            | vulkan:0   | 257.6      | 232.3 | 257.6 | 275.9 |
| hqdn3d_vulkan_defaults       | vulkan:0   | 153.5      | 150.7 | 157.2 | 153.5 |
| hqdn3d_vulkan_luma_spatial=8 | vulkan:0   | 298.5      | 288.8 | 301.0 | 298.5 |
| nlmeans_vulkan_s1            | vulkan:0   | 37.9       | 37.9  | 37.8  | 37.9  |
| nlmeans_vulkan_s3            | vulkan:0   | 37.6       | 37.6  | 37.8  | 37.0  |
| transfer_baseline            | cuda:0     | 640.5      | 624.5 | 640.5 | 689.6 |
| hqdn3d_cuda_defaults         | cuda:0     | 322.0      | 322.0 | 320.2 | 322.0 |
| hqdn3d_cuda_luma_spatial=8   | cuda:0     | 321.6      | 322.7 | 321.6 | 318.5 |
| transfer_baseline            | opencl:2.0 | 624.7      | 595.3 | 640.5 | 624.7 |
| hqdn3d_opencl_defaults       | opencl:2.0 | 289.3      | 289.3 | 299.7 | 277.6 |
| hqdn3d_opencl_luma_spatial=8 | opencl:2.0 | 298.9      | 291.9 | 300.5 | 298.9 |
| encoder_svt_plain_1200k      | cpu        | 248.2      | 255.3 | 141.9 | 248.2 |
| encoder_svt_denoise_1200k    | cpu        | 102.6      | 102.6 | 103.2 | 100.3 |
| encoder_aom_plain_1200k      | cpu        | 23.3       | 23.3  | 23.5  | 23.0  |
| encoder_aom_denoise_1200k    | cpu        | 5.4        | 5.4   | 5.6   | 5.4   |

- With the frames starting and ending in system memory, `hqdn3d_cuda`
  reaches 322 fps and `hqdn3d_opencl` 289 fps, above the CPU `hqdn3d` of
  this run (257.5 fps); the CUDA and OpenCL upload and download alone reach
  640 and 625 fps.
- `hqdn3d_vulkan` reaches 153.5 fps with the defaults but 298.5 fps with
  `luma_spatial=8`, against a Vulkan transfer baseline of 257.6 fps. The run
  does not explain the gap between the two settings; the three runs of each of
  the two series agree within 10 %.
- `nlmeans_vulkan` reaches 37.9 fps on the card.
- The host load was about a fifth of the main run's, so the CPU and encoder
  rows here are faster than in Measure 4 (CPU `hqdn3d` 257.5 against 215.7
  fps); compare GPU and CPU figures within one run only.

### Evidence

[`hqdn3d-gpu-results.json`](hqdn3d-gpu-results.json), sha256
`a493cee9b7a961b8c8f91628708b6f3428242aa380cd2843711289a116310087`, holds
every measurement with its command line, the skipped cases with their
reasons and the host gate readings. Host paths in it are replaced by
`<data>` (clips and work files), `<ffmpeg-src>` (the FFmpeg build),
`<vmafx>` (the `vmaf` tool and model) and `<pelorus>` (`bd_rate.py`).
`python3 scripts/measure_hqdn3d.py --json
docs/measurements/hqdn3d-gpu-results.json table` renders the tables above
from it.

[`hqdn3d-gpu-results-4090.json`](hqdn3d-gpu-results-4090.json), sha256
`4de8529c7ce6d2cfe08599f8870ecf8f54f9f7220469486ecaa04073db175452`, holds
the RTX 4090 run in the same form, and `python3 scripts/measure_hqdn3d.py
--json docs/measurements/hqdn3d-gpu-results-4090.json table` renders its
tables. `tests/test_measure_hqdn3d.py` checks both sha256 values and that
every parity and speed row the two files render appears in this document.
