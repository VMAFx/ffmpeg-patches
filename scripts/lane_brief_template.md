task: <task>
repo: VMAFx/ffmpeg-patches, worktree = cwd, branch <branch>. issue #<n>, round <r>.
finding / problem: <finding>
fix: <fix>; HISS: no recursion, bounded loops, functions <= 60 LOC, typed, timeout on every I/O call.
docstrings: every public function you add OR touch (even a one-line change inside it) carries a one-line docstring; add one when the function has none.
tests: <tests>; positive, negative, boundary; one-line docstring on every new test function, class and helper.
planted red: run the new tests against the unfixed code first; record rc + failure line.
run ONLY (with .lane/venv python), after `git add` of your changed files:
  `python -m unittest <modules>`
  `ruff check scripts tests`; `ruff format --check scripts tests`
  `python scripts/lint_mutable_defaults.py`; `python -c 'import scripts.series'`
  `python -I scripts/docstring_diff.py --cached`  (docstring check on staged changes; rc must be 0)
constraints: no commit, no push. touch only <paths>. `git add` them at end; never stage .lane/.
return (caveman): changed paths, tests added, planted-red output + rc, rc of each command. write .lane/evidence/<branch>-report.md.
