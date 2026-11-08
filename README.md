<!-- markdownlint-disable MD013 MD023 MD033 MD036 MD041 -->
<!-- A centred header needs inline HTML, a banner image, indented headings and
     badge lines past 80 columns, so those five rules are off for this file.
     Same directive style as docs/adr/0000-template.md. The prose below still
     wraps at 80. -->
<div align="center">

  <img src="docs/assets/ffmpeg-patches-readme-banner.svg" alt="ffmpeg-patches — shared FFmpeg patch series for VMAFx and Pelorus" height="120" />

  <!-- README-STANDARD badge block: one group, fixed order (docs/README-STANDARD.md) -->
  <!-- 1 release/version -->
  [![Release](https://img.shields.io/badge/Release-Releases-3b82f6?style=for-the-badge&labelColor=111a35)](https://github.com/VMAFx/ffmpeg-patches/releases)
  <!-- 2 CI/build -->
  [![Series](https://github.com/VMAFx/ffmpeg-patches/actions/workflows/series.yml/badge.svg?branch=main&event=push)](https://github.com/VMAFx/ffmpeg-patches/actions/workflows/series.yml)
  [![Consumers](https://github.com/VMAFx/ffmpeg-patches/actions/workflows/consumers.yml/badge.svg?branch=main&event=push)](https://github.com/VMAFx/ffmpeg-patches/actions/workflows/consumers.yml)
  [![FFmpeg Master](https://github.com/VMAFx/ffmpeg-patches/actions/workflows/master.yml/badge.svg?branch=main&event=schedule)](https://github.com/VMAFx/ffmpeg-patches/actions/workflows/master.yml)
  [![Upstream Drift](https://github.com/VMAFx/ffmpeg-patches/actions/workflows/upstream.yml/badge.svg?branch=main&event=schedule)](https://github.com/VMAFx/ffmpeg-patches/actions/workflows/upstream.yml)
  [![Release](https://github.com/VMAFx/ffmpeg-patches/actions/workflows/release.yml/badge.svg)](https://github.com/VMAFx/ffmpeg-patches/actions/workflows/release.yml)
  <!-- 3 security/scorecard -->
  [![Scanners](https://github.com/VMAFx/ffmpeg-patches/actions/workflows/scanners.yml/badge.svg?branch=main&event=push)](https://github.com/VMAFx/ffmpeg-patches/actions/workflows/scanners.yml)
  [![OpenSSF Scorecard](https://img.shields.io/badge/OpenSSF-Scorecard-06b6d4?style=for-the-badge&labelColor=111a35)](https://scorecard.dev/viewer/?uri=github.com/VMAFx/ffmpeg-patches)
  [![Codacy](https://app.codacy.com/project/badge/Grade/59284a64ef65425eb779b400aa2b624c)](https://app.codacy.com/gh/VMAFx/ffmpeg-patches/dashboard)
  [![CodeRabbit](https://img.shields.io/badge/Reviews-CodeRabbit-8b5cf6?style=for-the-badge&logo=coderabbit&logoColor=white&labelColor=111a35)](https://coderabbit.ai)
  <!-- 4 standards -->
  [![HISS-21](https://img.shields.io/badge/Standards-HISS--21-06b6d4?style=for-the-badge&logo=nasa&labelColor=111a35)](AGENTS.md)
  [![Power of 10](https://img.shields.io/badge/NASA_JPL-Power_of_10-3b82f6?style=for-the-badge&logo=nasa&labelColor=111a35)](CONTRIBUTING.md#modernized-nasa-jpl-power-of-10-rules)
  [![Conventional Commits](https://img.shields.io/badge/Commits-Conventional-8b5cf6?style=for-the-badge&logo=conventionalcommits&logoColor=white&labelColor=111a35)](CONTRIBUTING.md)
  <!-- 5 licence -->
  [![License](https://img.shields.io/badge/License-EUPL--1.2_·_LGPL--2.1--or--later-3b82f6?style=for-the-badge&labelColor=111a35)](#licence)
  <!-- 6 support -->
  [![GitHub Sponsors](https://img.shields.io/badge/Sponsor-GitHub-8b5cf6?style=for-the-badge&logo=githubsponsors&logoColor=white&labelColor=111a35)](https://github.com/sponsors/lusoris)

  **Shared FFmpeg fix patch series for VMAFx projects, tracked against FFmpeg master ([`series.txt`](series.txt)) and [sent upstream](upstream-contributions.json)**

  *Consumers [`apply`](scripts/series.py) this series first and carry only their own feature patches*

</div>

ffmpeg-patches is one shared FFmpeg patch series for VMAFx projects: fixes
are tracked against FFmpeg master ([`series.txt`](series.txt)) and
[sent upstream](upstream-contributions.json); consumers
[`apply`](scripts/series.py) this series first and carry only their own
feature patches.

**[📖 Architectural Decision Records](docs/adr/README.md)** · [ADR-0001: Shared Fix Series](docs/adr/0001-shared-fix-series.md)

---

## How it works

The series is a list of FFmpeg fixes in `git format-patch` form, applied in
order onto one pinned FFmpeg release. Every fix is also sent upstream. When
upstream merges it and a release containing it is pinned, the patch is
dropped.

Consumers apply this series first and their own feature patches on top:

- [VMAFx/vmafx](https://github.com/VMAFx/vmafx)
- [VMAFx/pelorus](https://github.com/VMAFx/pelorus)

A fix lives here once. A consumer does not carry its own copy.

## Apply the series

```bash
. ./base.env
git clone --branch "$FFMPEG_TAG" --depth 1 "$FFMPEG_REMOTE" ffmpeg
test "$(git -C ffmpeg rev-parse HEAD)" = "$FFMPEG_COMMIT"
python3 scripts/series.py apply --expect-base ffmpeg
```

`apply` checks each patch and prints one result per patch. It runs
`git am --3way` without your git configuration only when the patch does not
already apply and its added lines are not in the tree:

- `applied`: the patch applied
- `conflict`: it did not; the run stops unless `--keep-going` is given
- `upstream`: its added lines are already in the tree, so it is a drop
  candidate; on the pinned release this is an error
- `not-tried`: the patch was not attempted because a preceding patch
  conflicted and `--keep-going` was not given

## Layout

| Path | Role |
| :-- | :-- |
| `base.env` | Pinned FFmpeg release: remote, tag and the commit the tag must peel to |
| `series.txt` | Patch order; the series is cumulative |
| `patches/NNNN-slug.patch` | One fix, generated by `git format-patch` |
| `patches/NNNN-slug.toml` | Its metadata: status, upstream link, consumers, origin |
| `upstream-contributions.json` | Every upstream pull request with its state; source of the contributions table |
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
A patch whose release form has no single master form (for example, one split
into several upstream pull requests) records
`release_only = "<reason>"`; it excludes `master_form` and `upstream_commit`.
The daily upstream drift check fails when a recorded `master_form` no longer
matches the PR's head or merge base on the forge. An `unsubmitted` patch whose
fix travels in another author's PR records it as
`covered_by = "https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/<number>"`; the
daily check fails once that PR merges or closes.

## Guides and reference

### Pre-PR variant sweep

Before pushing or opening a pull request, scan the committed fix and callers of its targets:

```bash
# Fetch the upstream base so both master and topic resolve as local refs:
git -C /path/to/FFmpeg fetch origin master:master
# A shallow clone needs enough history for the merge base; deepen it if merge-base fails:
git -C /path/to/FFmpeg fetch --unshallow origin  # or: fetch --deepen=<commits> origin
# Sweep from the merge base so upstream-only changes are excluded:
python3 scripts/variant_sweep.py -C /path/to/FFmpeg --class unchecked-return \
  "$(git -C /path/to/FFmpeg merge-base master topic)" topic -o sweep.md
```

The report records full base and head commit IDs. Targets combine callees whose return values appear in recognized error checks on added lines with functions whose bodies contain added lines. The scan covers touched C/header files and C files in the head revision mentioning those targets. It reports discarded calls and checks that normalize an error to `-1` or an `AVERROR_` value. Explicit `(void)` calls are intentional and excluded. This lexical scan supports review; inspect the surrounding control flow when deciding each row.

Replace every `TODO` disposition with one of:

- `fold`: same bug class and same file as the fix; fold the correction into this pull request.
- `follow-up: <reproducer path>`: otherwise, record the ancestor reproducer path in the evidence and track the correction separately.

Keep the PR body unchanged unless a variant is folded into the fix. After folding, regenerate the report for the new head and review its rows again. A report with no findings ends with `no hits`.

Pass the reviewed report before positional arguments to both commands (run these inside the FFmpeg clone):

```bash
/path/to/ffmpeg-patches/scripts/ffmpeg-forgejo push --sweep /path/to/sweep.md topic
/path/to/ffmpeg-patches/scripts/ffmpeg-forgejo pr --sweep /path/to/sweep.md topic "Fix title" /path/to/body.md
```

Both commands refuse a missing report, a head that differs from the local branch, or an unresolved or unknown disposition before contacting the forge or pushing. `--sweep` may be interleaved with `--lease` on `push` or `--overlap-ok` on `pr`.

#### Forge settings

The forge helper scripts share configuration through environment variables, implemented in [scripts/forge_settings.py](scripts/forge_settings.py) and verified by [tests/test_forge_settings.py](tests/test_forge_settings.py):

- `FFMPEG_FORGEJO_HOST`: forge URL (default: `https://code.ffmpeg.org`).
- `FFMPEG_FORGEJO_REPO`: repository path (default: `FFmpeg/FFmpeg`).
- `FFMPEG_FORGEJO_BOT_REVIEWER`: bot reviewer login (default: `Forgejo_Fairy`).
- `FFMPEG_FORGEJO_LOGIN`: user login on the forge; when unset, discovered from `/user` with the keyring token.

### Change a patch

1. Apply the series onto the pinned release, as above.
2. Commit, amend or reorder in that checkout. Keep FFmpeg's commit message
   style and add a `Signed-off-by` trailer.
3. Regenerate: `python3 scripts/series.py export ffmpeg`. Metadata follows
   its patch when the number changes.
4. Write or update `patches/NNNN-slug.toml` and run `make test`, then
   `make lengths FFMPEG_TREE=ffmpeg` to catch a function grown past the
   60-line cap (HISS-04) before it reaches CI.
5. When a patch file is renamed, or its `status` or `upstream` link
   changes, update its record in `upstream-contributions.json` and
   regenerate the table; `check` names a record that no longer fits.

### Checks

`series.yml` always runs the series check, lint and unit tests on pull requests. Its `scope` job compares the pull request head with its merge base: `ffmpeg` selects the pinned-base apply, regeneration, clang-tidy, build, FATE, coverage and warning gates; `parity` selects the GPU parity step within that job; `macos` selects the Metal build and warning gate and runs for every change to the patches, `series.txt`, `base.env` or the gate itself, because that job refuses clang warnings in every file the series changes. Both heavy jobs always start, so they stay required checks; outside the scope of a change they skip every step, so a docs-only change finishes them in seconds. When the `scope` job itself fails, both heavy jobs run in full, and the `scope` job is a required check of its own. Pushes to `main` and release calls run the full set (`scope --full`); draft pull requests still fail the check job until marked ready for review.

The checks cover:

- `make test`: series check, ruff, mutable defaults and the unit tests of
  `scripts/series.py`. The unit-test run fails when it leaves files in its
  temporary directory (`scripts/tmp_leak_guard.py`).
- REUSE compliance runs in the [REUSE workflow](.github/workflows/reuse.yml); the local `reuse-lint` hook in `lefthook.yml` runs `reuse lint` when REUSE 6.x is installed.
- `python3 scripts/lint_mutable_defaults.py` (also `make lint-mutable-defaults`): Pylint checks Python sources for `dangerous-default-value` (W0102), including defaults bound to identifiers that ruff B006 misses. The script's `PYLINT_VERSION` pins Pylint; it uses that exact version on PATH or fetches it with `uvx`.
- `make filter-deps FILTER_DEPS_TREE=<FFmpeg tree>`: scans filter source calls against configure declarations and conditional library link edges.
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
- a function-length gate (HISS-04): no patch makes a C function longer than
  60 lines without a live exception in that patch's `.toml` sidecar. CI runs
  it on every pull request. It needs the series applied to an FFmpeg checkout,
  so `make verify-all` runs it only when `FFMPEG_TREE` names one and otherwise
  skips it with a notice. Run it before pushing a patch change, with
  `make verify-all FFMPEG_TREE=<applied tree>` or the gate on its own,
  `make lengths FFMPEG_TREE=<applied tree>`

A nightly run applies the series onto FFmpeg master with
`series.py apply TREE --master-forms https://code.ffmpeg.org/FFmpeg/FFmpeg.git`.
It fetches each recorded `master_form` and applies that PR diff instead of
the pinned-release patch. A pull request that carries several patches records
its master_form on the last of them; the earlier ones report in-master-form
and are included only if that form applies. A recorded `upstream_commit`
that is an ancestor of the tested master reports the patch as upstream without
applying it; a missing commit fails the run because its history cannot be
checked. A `release_only` patch is reported as `release-only` and not applied.
Other patches use the series form. The run reports drop candidates and
fails on any conflict or tool error. Applying without `--master-forms`,
including consumer and CI runs with `--expect-base`, uses only the
pinned-release patches and ignores these metadata keys.

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

A consumer patch reports `handed-over` only when a series patch's `.toml`
declares that consumer and its added lines contain every consumer added-line
run at the same file path, while the consumer's known `--pin NAME=TAG` does
not yet contain that series patch (`--series-dir` defaults to this repository).
The gate warns that the consumer must drop the patch on its next pin bump
and continues its stack; a missing pin, an expired handover, or incomplete
coverage still fails, and an unknown tag is a tool error.

### Graph check

When two linked filters share no pixel or sample format, libavfilter inserts
a converter named `auto_<filter>_<n>` without asking. On a hardware path
(for example the Vulkan and CUDA import of 0008) that converter is a silent
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

### Tone-map parity

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

### GPU parity in CI

The hosted runners have no GPU. The pinned-base job of `series.yml` runs the
GPU filters on Mesa software devices instead: lavapipe for Vulkan and rusticl
on llvmpipe for OpenCL. The step fails when FFmpeg opens another device, when
a device case is skipped, or when a GPU filter leaves the CPU filter's output:

| Tests | Filters | Bound |
| --- | --- | --- |
| `tests/test_hqdn3d_vulkan.py`, `tests/test_hqdn3d_opencl.py` | `hqdn3d_vulkan`, `hqdn3d_opencl` | the samples of `hqdn3d` ([ADR-0004](docs/adr/0004-hqdn3d-on-the-gpu.md)) |
| `tests/test_tonemap_vulkan.py`, `tests/test_tonemap_opencl.py` | `tonemap_vulkan`, `tonemap_opencl` (0015, 0017, 0019, 0021, 0034) | one 8-bit code value of `tonemap=bt2408_hable`, with and without peak detection; a software conversion after `tonemap_opencl` keeps its output matrix (0015) |

Each test file also holds a planted wrong case that the comparison must refuse.

Two gaps remain:

- `hqdn3d_cuda` (`tests/test_hqdn3d_cuda.py`) needs an NVIDIA device, and
  CUDA has no software device, so CI does not run it. It runs where an NVIDIA
  GPU is present, with `CUDA_FFMPEG` and `CUDA_DEVICE` set.
- The Metal (VideoToolbox) filters are built and checked for warnings on the
  macOS job, but their output is not compared with the CPU filters until an
  Apple host can run the parity tests.

## Contribute

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow, coding
standards (HISS-21, NASA JPL Power of 10), and local verification instructions.

## Support ffmpeg-patches

**Why it matters.** ffmpeg-patches keeps one shared series of 39 FFmpeg fixes for VMAFx and pelorus and sends every fix upstream; a patch is dropped once a pinned FFmpeg release carries it. It tracks [36 FFmpeg pull requests](upstream-contributions.json), its own and backported ones: 10 merged, 23 open.

**What support funds.**
- Cloud test time on an NVIDIA device for the hqdn3d_cuda parity test, which hosted runners cannot run.
- Cloud test time on an Apple host to compare the Metal (VideoToolbox) filters with the CPU filters.

Sponsor on [GitHub Sponsors](https://github.com/sponsors/lusoris), any amount; no support or priority is sold. Open Collective: pending.

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

## Upstream and licence

### Upstream contributions

Pull requests on the FFmpeg forge that came out of this series and its
consumers, and those of other authors that the series backports. A row
without a series patch is a change the series does not carry.

<!-- upstream-contributions:start -->
| Pull request | Target | State | Change | Series patch |
| --- | --- | --- | --- | --- |
| [24045](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24045) | `master` | merged 2026-08-07 (`63d0237033cc`), in no release yet | avutil/hwcontext_vulkan: Query export support with the real image flags | 0035 |
| [24835](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24835) | `master` | merged 2026-10-19 (`fd9a8f25a07b`), in no release yet | avfilter/vf_tonemap_opencl: zero the peak detection buffer | 0016 |
| [24970](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24970) | `master` | open | avutil/vulkan: don't release frames to VK_QUEUE_FAMILY_IGNORED | 0001 |
| [24973](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24973) | `master` | open | avfilter/vf_libvmaf: fix error paths (frame leak, frame count, uninitialized score, CUDA context) | 0042 |
| [24974](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24974) | `master` | merged 2026-10-20 (`2a8c6d30b934`), in no release yet | avcodec/av1dec: remove the coding bias from exported chroma grain params | none |
| [24975](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24975) | `release/9.0` | merged 2026-10-12 (`7e93014c5bbb`), in no release yet | [release/9.0] hwcontext_vulkan: only originate host image layout transitions from copyable layouts | none |
| [24977](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24977) | `master` | open | avcodec/vaapi_encode_h265: enable cu_qp_delta for ROI in CQP mode | none |
| [24978](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24978) | `master` | open | avfilter/vf_libvmaf: add max pool method, reject unknown ones | none |
| [24993](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24993) | `master` | closed | avcodec/nvenc: keep hevc_nvenc user data SEI within the picture limit | none |
| [24994](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24994) | `master` | merged 2026-10-24 (`96b7f8281a5f`), in no release yet | avcodec/nvenc: drop hevc_nvenc SEI above a fixed size | 0003 |
| [25053](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25053) | `master` | open | hwcontext_vulkan: import a frame again when the CUDA context changes | 0008 |
| [25060](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25060) | `release/9.0` | merged 2026-10-14 (`669f5ab083c7`), in no release yet | [release/9.0] vulkan: de-phase queue rotation and pin video pools to a single queue | 0007 |
| [25109](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25109) | `master` | open | avutil/hwcontext_vaapi: accept GR1616 for P010/P012 DRM import | 0004 |
| [25110](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25110) | `master` | closed | avutil/hwcontext_vulkan: wait on the CPU before RADV DRM mapping | 0006 |
| [25111](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25111) | `master` | closed | Fix GCC 14 and 16 warnings in libavcodec, libavfilter, libavformat and tests | 0002 |
| [25112](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25112) | `master` | merged 2026-10-16 (`30b16e57ff3d`), in no release yet | ffbuild/library.mak: add the devprogs target | none |
| [25189](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25189) | `master` | merged 2026-10-18 (`ffb15c94c286`), in no release yet | avfilter/Makefile: link cuda/load_helper.o into three CUDA filters | none |
| [25468](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25468) | `master` | merged 2026-10-27 (`fa4daf630eb5`), in no release yet | avcodec/rv60dec: validate angular prediction buffer bounds | none |
| [25469](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25469) | `master` | open | swresample/tests/swresample: allocate sample buffers and test indices | none |
| [25470](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25470) | `master` | open | avcodec/tests: give large test buffers static storage | none |
| [25471](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25471) | `master` | open | tools/ismindex: allocate output paths and report manifest errors | none |
| [25473](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25473) | `master` | open | avfilter/vf_find_rect: assert the mipmap search pass bounds | none |
| [25474](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25474) | `master` | open | avfilter: fix GCC 14 and 16 warnings in vf_median, af_firequalizer and af_afftfilt | none |
| [25475](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25475) | `master` | open | avformat: fix GCC 14 and 16 -Wformat-truncation warnings in dashenc, matroskaenc and smoothstreamingenc | none |
| [25476](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25476) | `master` | open | avformat: fix GCC 14 and 16 warnings in mpegts, nspdec and rtsp | none |
| [25477](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25477) | `master` | open | avcodec/ffv1enc: move float remap scratch off the stack | none |
| [25478](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25478) | `master` | open | avcodec/aac: fix GCC 14 and 16 -Wstringop-overflow warnings in aacenc and aacsbr | none |
| [25479](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25479) | `master` | open | avcodec: fix GCC 14 and 16 warnings in a64multienc, h264dec, vlc and wmadec | none |
| [25480](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25480) | `master` | open | avcodec/vvc: fix GCC 14 and 16 -Wstack-usage warnings in scaled prediction and checkasm | none |
| [25481](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25481) | `master` | open | tests/checkasm: move large scratch buffers to the heap | none |
| [25483](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25483) | `master` | merged 2026-11-02 (`f675c507e20a`), in no release yet | avcodec/adpcm: fix GCC 14 and 16 unused warnings in single-decoder builds | none |
| [25533](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25533) | `master` | open | checkasm/snowdsp: Include intmath before public headers | 0036 |
| [25534](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25534) | `master` | open | avcodec/aom_film_grain: Assume the predicted luma point bound | 0037 |
| [25535](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25535) | `master` | open | avcodec/vvc/intra_template: bound reference corner extension | 0038 |
| [25536](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25536) | `master` | open | avcodec/wmalosslessdec: Clear subframe flags as an array | 0039 |
| [25708](https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/25708) | `master` | open | avfilter/vf_libvmaf: accept more planar CUDA formats and copy all planes | 0040, 0041 |

State read from the FFmpeg forge on 2026-11-04.
<!-- upstream-contributions:end -->

A pull request against a release branch is a backport. It reaches a pinned
base with the first release cut from that branch after the merge.

The same facts are in `upstream-contributions.json`, one object per pull
request, for other pages to read. Its `patch` field is a string of patch
filenames separated by a comma and a space, or an empty string when the series carries
none. Every named patch must link to that pull request and match its state.
The table lists all their series numbers and is generated from that file:
edit the file, then run `python3 scripts/series.py contributions --write`.
The pre-commit hook also regenerates and stages `README.md` whenever
`upstream-contributions.json` is staged.

Run `python3 scripts/upstream_drift.py` to compare recorded fields and
`master_form` commits with the FFmpeg forge. Add `--fix` to update changed
fields, set the checked date to today in UTC and regenerate the table.
Unchanged files keep their bytes and checked date. The README table is checked
even when the forge records match; `--fix` repairs a stale table without changing
the checked date. Concurrent edits detected before writing abort the repair
without changing any files. A merged or closed
`covered_by` pull request still needs human review before dropping its patch;
`--fix` reports it and exits 1. Tool errors exit 2.

The daily upstream workflow runs `--fix` with read-only repository permissions.
Changes appear in its job summary and the `upstream-drift` artifact as
`upstream-drift.patch`; the job fails whenever changes need review or drift
remains. Apply the downloaded patch with `git apply upstream-drift.patch`,
review it and submit the update through the usual pull request process.

### Licence

The patches change FFmpeg's sources and keep FFmpeg's LGPL-2.1-or-later. The
tooling and documentation are EUPL-1.2. `REUSE.toml` records both.

## Standards & Governance

<!-- praetor:readme-governance:start -->
Praetor manages this repository's declared governance policy. This managed
block records adoption state; it is not a verification certificate.

**Verification**: `make verify-all` runs the repository's configured
verification cascade.

**HISS Audit**: `praetorctl audit` enforces policy, generated-surface
integrity, and the debt ratchet.

**Context Sync**: `praetorctl compile-context --verify` verifies every
generated agent context against `AGENTS.md`.

**Debt Baseline**: `.standards-baseline.json` anchors the debt ratchet at
1 recorded infraction; audit forbids growth.
<!-- praetor:readme-governance:end -->
