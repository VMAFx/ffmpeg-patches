# ADR-0001: One shared FFmpeg fix series for every consumer

- **Status**: Proposed
- **Date**: 2026-10-09
- **Authors**: Lusoris

## Context

VMAFx/vmafx and VMAFx/pelorus each carry an FFmpeg patch stack against the
same release, n9.0.2 (`946fcce07b6d`). Most of their patches are features of
the project itself. A few are plain FFmpeg fixes that any FFmpeg user needs:

- Pelorus 0021 corrects the queue family of the first Vulkan frame barrier
  (FFmpeg pull request #24970).
- vmafx 0019 removes the GCC 14 and GCC 16 diagnostics of an FFmpeg build.

A fix carried by one consumer is missing from the other, and a fix carried by
both is maintained twice, with two rebases at every FFmpeg bump.

## Decision

This repository owns every FFmpeg fix patch of the VMAFx projects. A consumer
applies this series first and its own feature patches on top. It keeps no
copy of a fix that lives here.

Layout:

- `base.env` pins the FFmpeg release by tag and by the commit the tag must
  peel to.
- `series.txt` orders `patches/NNNN-slug.patch`, which `git format-patch`
  writes with `--zero-commit --full-index --no-signature --no-numbered`, so a
  regeneration is byte-identical.
- `patches/NNNN-slug.toml` records each patch's status, upstream link,
  consumers and origin.

The series holds one form of each patch: the form for the pinned release.
The FFmpeg pull request is the master form and is linked from the metadata;
the series does not carry it a second time.

Checks:

- Every pull request lints the series and applies it on the pinned release.
  It then checks byte-identical regeneration, builds FFmpeg and runs the
  FATE tests that need no samples. A compiler warning in a file the series
  changes fails the run. Warnings elsewhere depend on the runner's
  libraries and on FFmpeg's own sources, so they are counted, not gated.
- A nightly run applies the series on FFmpeg master. It reports conflicts and
  drop candidates without failing. A drop candidate is a patch whose added
  lines already stand in master, which also catches fixes that upstream
  merged with different surrounding context.

Releases are tagged; a consumer pins the tag and the sha256 of the series
tarball. A reusable workflow builds the tarball, its SBOM and its SLSA
provenance in one job and signs the checksums with cosign, which meets the
declared supply chain (SLSA Build Level 3) without an exception.

## Consequences

- **Positive**: A fix is written, rebased and sent upstream once. Both
  consumers receive it at the same time, and its upstream state is recorded
  next to it.
- **Positive**: The pinned-release build doubles as a test of each fix
  against both consumers' toolchains.
- **Negative**: Consumers depend on this repository's release cadence for an
  urgent fix, and an FFmpeg bump needs this series rebased before either
  consumer can move.
- **Negative**: A mixed patch, part fix and part feature, has to be split
  before its fix part can move here (Pelorus 0022, and later Pelorus 0009).

## References

- FFmpeg pull request: <https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24970>
- Praetor archetype for patch-series repositories:
  <https://github.com/cordanaLLM/praetor/issues/1110>
