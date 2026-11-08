// SPDX-FileCopyrightText: 2026 Lusoris
// SPDX-License-Identifier: EUPL-1.2
import { spawnSync } from "node:child_process"
import { join } from "node:path"

export const RawConfigureGuard = async ({ directory, worktree }) => ({
  "tool.execute.before": async (input, output) => {
    if (input.tool !== "bash") return
    const result = spawnSync(
      "python3",
      ["-I", join(worktree, "scripts/raw_configure_guard.py"), "--client", "opencode"],
      {
        cwd: worktree,
        input: JSON.stringify({ tool: input.tool, args: output.args, cwd: output.args.workdir ?? directory }),
        encoding: "utf8",
        timeout: 15000,
      },
    )
    if (result.status !== 0) {
      throw new Error(result.stderr?.trim() || "Raw configure guard failed")
    }
  },
})
