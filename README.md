# ffmpeg-patches

One shared FFmpeg patch series for VMAFx projects: fixes tracked against FFmpeg master and sent upstream; consumers carry only their own feature patches.

<!-- praetor:readme-governance:start -->
[![HISS Adopted][praetor-hiss-badge]][praetor-hiss-agents]

Praetor manages this repository's declared governance policy. This managed
block records adoption state; it is not a verification certificate.

**Verification**: `make verify-all` runs the repository's configured
verification cascade.

**HISS Audit**: `praetorctl audit` enforces policy, generated-surface
integrity, and the debt ratchet.

**Context Sync**: `praetorctl compile-context --verify` verifies every
generated agent context against `AGENTS.md`.

**Debt Baseline**: `.standards-baseline.json` anchors the debt ratchet at
0 recorded infractions; audit forbids growth.

[praetor-hiss-badge]: https://img.shields.io/badge/Standards-HISS%20Adopted-blue
[praetor-hiss-agents]: https://github.com/VMAFx/ffmpeg-patches/blob/HEAD/AGENTS.md
<!-- praetor:readme-governance:end -->

## How it works

The series is a list of FFmpeg fixes in `git format-patch` form, applied in
order onto one pinned FFmpeg release. Every fix is also sent upstream. When
upstream merges it and a release containing it is pinned, the patch is
dropped.

Consumers apply this series first and their own feature patches on top:

- [VMAFx/vmafx](https://github.com/VMAFx/vmafx)
- [VMAFx/pelorus](https://github.com/VMAFx/pelorus)

A fix lives here once. A consumer does not carry its own copy.

## Upstream contributions

Pull requests on the FFmpeg forge that came out of this series and its
consumers, and those of other authors that the series backports. A row
without a series patch is a change the series does not carry.

<!-- upstream-contributions:start -->
| Pull request | Target | State | Change | Series patch |
| --- | --- | --- | --- | --- |
| [24045](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24045) | `master` | merged 2026-08-07 (`63d0237033cc`), in no release yet | avutil/hwcontext_vulkan: Query export support with the real image flags | 0037 |
| [24835](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24835) | `master` | merged 2026-10-19 (`fd9a8f25a07b`), in no release yet | avfilter/vf_tonemap_opencl: zero the peak detection buffer | 0017 |
| [24970](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24970) | `master` | open | avutil/vulkan: don't release frames to VK_QUEUE_FAMILY_IGNORED | 0001 |
| [24973](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24973) | `master` | open | avfilter/vf_libvmaf: fix error paths (frame leak, frame count, uninitialized score, CUDA context) | none |
| [24974](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24974) | `master` | merged 2026-10-20 (`2a8c6d30b934`), in no release yet | avcodec/av1dec: remove the coding bias from exported chroma grain params | none |
| [24975](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24975) | `release/9.0` | merged 2026-10-12 (`7e93014c5bbb`), in no release yet | [release/9.0] hwcontext_vulkan: only originate host image layout transitions from copyable layouts | none |
| [24977](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24977) | `master` | open | avcodec/vaapi_encode_h265: enable cu_qp_delta for ROI in CQP mode | none |
| [24978](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24978) | `master` | open | avfilter/vf_libvmaf: add max pool method, reject unknown ones | none |
| [24993](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24993) | `master` | open | avcodec/nvenc: keep hevc_nvenc user data SEI within the picture limit | 0003 |
| [24994](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24994) | `master` | open | avcodec/nvenc: drop user data SEI that NVENC would write truncated | 0004 |
| [25053](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25053) | `master` | open | hwcontext_vulkan: import a frame again when the CUDA context changes | 0009 |
| [25060](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25060) | `release/9.0` | merged 2026-10-14 (`669f5ab083c7`), in no release yet | [release/9.0] vulkan: de-phase queue rotation and pin video pools to a single queue | 0008 |
| [25109](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25109) | `master` | open | avutil/hwcontext_vaapi: accept GR1616 for P010/P012 DRM import | 0005 |
| [25110](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25110) | `master` | open | avutil/hwcontext_vulkan: wait on the CPU before RADV DRM mapping | 0007 |
| [25111](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25111) | `master` | open | Fix GCC 14 and 16 warnings in libavcodec, libavfilter, libavformat, libswscale and tests | 0002 |
| [25112](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25112) | `master` | merged 2026-10-16 (`30b16e57ff3d`), in no release yet | ffbuild/library.mak: add the devprogs target | none |
| [25189](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25189) | `master` | merged 2026-10-18 (`ffb15c94c286`), in no release yet | avfilter/Makefile: link cuda/load_helper.o into three CUDA filters | none |

State read from the FFmpeg forge on 2026-10-21.
<!-- upstream-contributions:end -->

A pull request against a release branch is a backport. It reaches a pinned
base with the first release cut from that branch after the merge.

The same facts are in `upstream-contributions.json`, one object per pull
request, for other pages to read. The table is generated from that file:
edit the file, then run `python3 scripts/series.py contributions --write`.

## Layout

| Path | Role |
| :-- | :-- |
| `base.env` | Pinned FFmpeg release: remote, tag and the commit the tag must peel to |
| `series.txt` | Patch order; the series is cumulative |
| `patches/NNNN-slug.patch` | One fix, generated by `git format-patch` |
| `patches/NNNN-slug.toml` | Its metadata: status, upstream link, consumers, origin |
| `upstream-contributions.json` | Every upstream pull request with its state; source of the table above |
| `scripts/series.py` | `check`, `apply`, `export`, the `coverage` and `warnings` gates, the `contributions` table and release `notes` |
| `scripts/upstream_drift.py` | Compares `upstream-contributions.json` with the FFmpeg forge |
| `scripts/consumer_stacks.py` | Applies each consumer's patch stack on top of the applied series |
| `scripts/graph_check.py` | Reports conversion filters FFmpeg inserted on its own into a filtergraph |
| `scripts/tonemap_parity.py` | Colour difference, time and VRAM of tone-map filter chains against `vf_libplacebo` (ADR-0002) |

Metadata keys:

- `status`: `unsubmitted`, `submitted`, `merged` or `rejected`
- `upstream`: link to the FFmpeg pull request or patch; required unless
  `unsubmitted`
- `consumers`: projects that need the fix; every consumer still applies the
  whole series
- `origin`, `notes`: where the patch came from, and anything a maintainer
  must know

The series carries one form of each patch: the one for the pinned release.
The pull request linked in `upstream` is the FFmpeg master form. For a
`submitted` patch, optional `master_form = { base = "<40-hex>", head = "<40-hex>" }`
records the PR's merge base with master at submission and its head commit.
Update these ids whenever the PR is pushed again. For a `merged` patch,
optional `upstream_commit = "<40-hex>"` records the commit carrying the fix
on FFmpeg master. All ids must be full lowercase commit hashes.
The daily upstream drift check fails when a recorded `master_form` no longer
matches the PR's head or merge base on the forge. An `unsubmitted` patch whose
fix travels in another author's PR records it as
`covered_by = "https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/<number>"`; the
daily check fails once that PR merges or closes.

## Apply the series

```bash
. ./base.env
git clone --branch "$FFMPEG_TAG" --depth 1 "$FFMPEG_REMOTE" ffmpeg
test "$(git -C ffmpeg rev-parse HEAD)" = "$FFMPEG_COMMIT"
python3 scripts/series.py apply --expect-base ffmpeg
```

`apply` runs `git am --3way` per patch without your git configuration and
prints one result per patch:

- `applied`: the patch applied
- `conflict`: it did not; the run stops unless `--keep-going` is given
- `upstream`: its added lines are already in the tree, so it is a drop
  candidate; on the pinned release this is an error

## Change a patch

1. Apply the series onto the pinned release, as above.
2. Commit, amend or reorder in that checkout. Keep FFmpeg's commit message
   style and add a `Signed-off-by` trailer.
3. Regenerate: `python3 scripts/series.py export ffmpeg`. Metadata follows
   its patch when the number changes.
4. Write or update `patches/NNNN-slug.toml` and run `make test`.
5. When a patch file is renamed, or its `status` or `upstream` link
   changes, update its record in `upstream-contributions.json` and
   regenerate the table; `check` names a record that no longer fits.

## Checks

Every pull request runs:

- `make test`: series check, ruff, REUSE and the unit tests of
  `scripts/series.py`
- inside the series check: every patch with an upstream link has a record
  in `upstream-contributions.json` with a matching state, and the table in
  this file equals the one generated from the records. The check does not
  ask the forge; the `checked` date says when the states were read
- apply on the pinned release, byte-identical regeneration, an FFmpeg build
  with the GPL components, Vulkan, NVENC, VAAPI and QSV (libvpl) enabled and the FATE tests
  that need no samples
- a coverage gate: every C file the series changes must appear as compiled
  (`CC` line) in the build log, so a file in a component the configuration
  leaves out fails the run instead of passing the warning gate unseen;
  included `*_template.c` files are not checked, and zero checked files fail
- a warning gate: any compiler warning in a file the series changes fails
  the run; warnings elsewhere in FFmpeg are counted in the summary. On a
  tree that applies the series to another upstream commit, such as FFmpeg
  master, `series.py warnings TREE LOG --base <commit>` names that commit,
  so upstream changes are not counted as the series' own

A nightly run applies the series onto FFmpeg master with
`series.py apply TREE --master-forms https://code.ffmpeg.org/FFmpeg/FFmpeg.git`.
It fetches each recorded `master_form` and applies that PR diff instead of
the pinned-release patch. A recorded `upstream_commit` that is an ancestor
of the tested master reports the patch as upstream without applying it;
a missing commit fails the run because its history cannot be checked.
Other patches use the series form. The run reports drop candidates and
fails on any conflict or tool error. Applying without `--master-forms`,
including consumer and CI runs with `--expect-base`, uses only the
pinned-release patches and ignores both metadata keys.

A daily run (`upstream.yml`) reads every pull request in
`upstream-contributions.json` from code.ffmpeg.org and fails when its state,
title, base branch, merge instant or merge commit no longer matches the
record. Update the record, regenerate the table and bump `checked`.

`consumers.yml` applies the series on the pinned base, then the patch stacks
of VMAFx/vmafx and VMAFx/pelorus (`ffmpeg-patches/series.txt` on their
default branches) on top, with `git am --3way` as their builds do. It runs
on every pull request, on `main` and nightly, and fails when a stack no
longer applies, so a series change that breaks a consumer shows before a
release carries it. It applies only; the consumers build in their own
repositories.

## Graph check

When two linked filters share no pixel or sample format, libavfilter inserts
a converter named `auto_<filter>_<n>` without asking. On a hardware path
(for example the Vulkan and CUDA import of 0009) that converter is a silent
software round trip. FFmpeg 8.0 and later dump the negotiated graph, and
`scripts/graph_check.py` exits 1 when the dump contains such a converter:

```sh
ffmpeg <inputs> <filters> <outputs> \
    -print_graphs_file graphs.json -print_graphs_format json
python3 scripts/graph_check.py graphs.json            # 1 on auto_scale_0 ...
python3 scripts/graph_check.py graphs.json --allow aresample
```

`tests/test_graph_check.py` runs it on two real dumps, one with an
`auto_scale_0` and one without.

## Tone-map parity

[ADR-0002](docs/adr/0002-dovi-reshape-and-tone-mapping.md) lets a tone-map path
land only when its mean CIEDE2000 against `vf_libplacebo` stays at or below
2.3. `scripts/tonemap_parity.py` measures that. It runs one reference chain
and any number of candidate chains on the same clip, compares every 30th frame
in BT.709 / gamma 2.4 / D65, and reports the mean, 95th percentile and maximum
difference, the wall time and the peak VRAM above idle (from `nvidia-smi`;
"not measured" without it). It exits 1 when a candidate misses the bar or
FFmpeg refuses to run it:

```sh
python3 scripts/tonemap_parity.py --ffmpeg ffmpeg --clip tests/fixtures/dv5/dv5.mp4 \
    --ref 'libplacebo=tonemapping=hable:colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv:format=yuv420p' \
    --cand 'ocl=format=p010le,hwupload,tonemap_opencl=tonemap=hable:format=nv12,hwdownload,format=nv12' \
    --pre 'ocl=-init_hw_device opencl=ocl:2.0 -filter_hw_device ocl' --json parity.json
```

`ocl:2.0` is OpenCL platform 2, device 0;
`ffmpeg -v debug -init_hw_device opencl -f lavfi -i nullsrc=d=0.01 -f null -`
lists them. On FFmpeg n9.0.2 without the series this example exits 1:
`tonemap_opencl` refuses the IPT-C2 frames of the clip, and the script records
that as a failed path with FFmpeg's exit status.

`tests/fixtures/dv5/dv5.mp4` is a Dolby Vision profile 5 clip with reshape
curves whose values are known; `tests/fixtures/dv5/dv5gen.c` wrote it and says
how. The base layer holds arbitrary IPT content rather than a picture converted
with the inverse matrices: the reference is whatever `vf_libplacebo` makes of
the same RPUs, so no inverse is needed. `tests/test_tonemap_parity.py` checks
the colour difference against the published CIEDE2000 test pairs and the
gate's pass and fail decisions without FFmpeg.

## GPU parity in CI

The hosted runners have no GPU. The pinned-base job of `series.yml` runs the
GPU filters on Mesa software devices instead: lavapipe for Vulkan and rusticl
on llvmpipe for OpenCL. The step fails when FFmpeg opens another device, when
a device case is skipped, or when a GPU filter leaves the CPU filter's output:

| Tests | Filters | Bound |
| --- | --- | --- |
| `tests/test_hqdn3d_vulkan.py`, `tests/test_hqdn3d_opencl.py` | `hqdn3d_vulkan`, `hqdn3d_opencl` | the samples of `hqdn3d` ([ADR-0004](docs/adr/0004-hqdn3d-on-the-gpu.md)) |
| `tests/test_tonemap_vulkan.py`, `tests/test_tonemap_opencl.py` | `tonemap_vulkan`, `tonemap_opencl` (0016, 0018, 0020, 0022, 0036) | one 8-bit code value of `tonemap=bt2408_hable`, with and without peak detection; a software conversion after `tonemap_opencl` keeps its output matrix (0016) |

Each test file also holds a planted wrong case that the comparison must refuse.

Two gaps remain:

- `hqdn3d_cuda` (`tests/test_hqdn3d_cuda.py`) needs an NVIDIA device, and
  CUDA has no software device, so CI does not run it. It runs where an NVIDIA
  GPU is present, with `CUDA_FFMPEG` and `CUDA_DEVICE` set.
- The Metal (VideoToolbox) filters are built and checked for warnings on the
  macOS job, but their output is not compared with the CPU filters until an
  Apple host can run the parity tests.

## Releases

A tag `vX.Y.Z` publishes a release; `vX.Y.Z-rc.N` publishes a prerelease.
`release.yml` first runs the full gate on the tagged commit, then
`release-build.yml` builds and signs these files:

- `ffmpeg-patches-<tag>.tar.gz`: `make dist` output, the series as committed,
  with `scripts/series.py`; the same commit always gives the same bytes
- `ffmpeg-patches-<tag>.spdx.json`: SPDX SBOM of the tarball
- `ffmpeg-patches-<tag>.provenance.sigstore.json`: SLSA build provenance,
  signed by the reusable workflow's identity
- `SHA256SUMS` and `SHA256SUMS.sigstore.json`: checksums, signed keyless with
  cosign

The release notes list the base and every patch with its upstream state, and
show the verify commands. A consumer pins the tag and the tarball's sha256.

Versions follow SemVer and start below 1.0.0:

- `v0.MINOR.PATCH` while vmafx and Pelorus adopt the series: MINOR moves when
  the FFmpeg base moves or a patch is added or dropped, PATCH for metadata or
  tooling
- `v1.0.0` once both consume it; from then on MAJOR moves with the FFmpeg base
  (consumers rebase their own patches), MINOR when a patch is added or
  dropped, PATCH for metadata or tooling

## Licence

The patches change FFmpeg's sources and keep FFmpeg's LGPL-2.1-or-later. The
tooling and documentation are EUPL-1.2. `REUSE.toml` records both.
