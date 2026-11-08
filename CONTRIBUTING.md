# Contributing to ffmpeg-patches

Thank you for contributing! This repository adheres strictly to the
**High-Integrity Systems Standard (HISS)** and modernized **NASA JPL
Power-of-10** rules.

## Core Directives & Verification

All changes must pass local verification before submitting:

```bash
make verify-all
```

Use Python 3.12 or newer and install the same pinned tools as CI before
running local verification. CI uses the version in `.python-version`.

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --require-hashes -r requirements-dev.txt
```

Pins live in `requirements-dev.in`; after editing it, recompile from the repository root with `uv pip compile --universal --generate-hashes --python-version=3.12 requirements-dev.in --output-file=requirements-dev.txt`.

Run the mutable-default check directly with `python3 scripts/lint_mutable_defaults.py`
or through `make lint-mutable-defaults`. Its Pylint pin in `requirements-dev.in`
must match the script's `PYLINT_VERSION`. The script
uses that exact version on PATH, falling back to `uvx` to fetch the pinned version.

`make semgrep` (part of `make verify-all`) runs the repository rules in `.semgrep.yml` with the scanners workflow's command; semgrep is not in requirements-dev.txt because CI runs it from the pinned container image, so install semgrep 1.178.0 yourself (for example with pipx).

### Modernized NASA JPL Power-of-10 Rules

1. **Simple Control Flow (HISS-01)**: Recursion is strictly banned; call graph
   must be an acyclic DAG; zero `goto`.
2. **Bounded Loops (HISS-02)**: All loops must have a statically verifiable
   scalar upper bound. Network and disk I/O require `context.Context` timeout.
3. **Deterministic Memory (HISS-03)**: Zero dynamic heap allocations
   (`malloc` / `free`) in hot simulation or rendering loops.
4. **Function Length Cap (HISS-04)**: No function may exceed
   **60 lines of code** ($\le 60$ LOC). When a patch makes a C function exceed
   60 lines, declare an exception in its sidecar TOML (e.g. `[[exceptions]]`
   with `rule = "HISS-04"`, `path`, `function`, `lines`, `category`, `entered`,
   `expires`, and `reason`). The `function` field accepts an ASCII C identifier
   such as `render_charset` or one macro call such as `fn(filter_plane)`,
   `FUNC(put)`, or `DEF(a, 8)`, with identifiers or integer arguments. Copy the
   parser's name: whitespace runs collapse to one space, but punctuation spacing
   stays as written, so `DEF(a,8)` and `DEF(a, 8)` are distinct valid names.
   Optional single spaces around parentheses and commas are accepted; empty
   argument lists, nested calls, newlines, and non-ASCII names are rejected.
   `scripts/series.py lengths` enforces this in CI.
5. **Assertion Density (HISS-15)**: Functions must assert preconditions, state
   invariants, and postconditions.
6. **Data Scope**: Variables must be declared at the smallest possible scope.
7. **Checked Errors (HISS-07)**: Check return values of all non-void functions;
   zero `.unwrap()` or unchecked errors.
8. **Static Execution (HISS-08)**: Dynamic code evaluation (`eval` / `exec`)
   and banned unsafe libc calls (`gets` / `strcpy` / `sprintf`) are prohibited.
9. **Pointer Safety (HISS-09)**: Pointer arithmetic must be bounded; all
   `unsafe` blocks require `// SAFETY:` justifications.
10. **Zero-Warning Hygiene (HISS-10)**: Zero compiler, linter, or formatting
    warnings tolerated across all builds.

### 3D Testing Discipline (HISS-15)

Every public function requires:

- **Positive tests**: Expected valid operational inputs.
- **Negative tests**: Invalid inputs, expected error returns.
- **Boundary tests**: Zero, one, max limits, off-by-one bounds.

### Pull Request CI

A pull request always runs the series lint and unit tests. The heavier jobs
run only when the changed files need them; the `series (scope · changed
files)` job prints the file count and the reason for each choice
(`scripts/series.py scope`):

- `ffmpeg`: pinned FFmpeg apply, regeneration, build, FATE and warning gates,
  for `patches/`, `series.txt`, `base.env`, `.python-version`, the series
  workflow and script, clang-tidy configuration and the GPU filter tests.
- `parity`: tone-mapping and hqdn3d parity, for the workflow, the series
  script, `scripts/tonemap_parity.py`, those tests, and patches that touch
  `hqdn3d` or `tonemap` files.
- `macos`: the macOS Metal build, for `patches/` and the series inputs.

Pushes to `main` and release runs always run the full set.

### Commit Messages

We enforce Conventional Commits:

- `feat:` New features
- `fix:` Bug fixes
- `chore:` Maintenance and governance
- `feat!:` / `fix!:` Breaking API changes (must include `Migration:` footer)
