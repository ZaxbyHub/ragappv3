// Vitest launcher for repository-root invocations: `repro-check.sh` execs a
// flat argv with the repository root as cwd and no shell, so frontend tests
// are started by this script, which chdirs into frontend/ before handing off
// to the locally installed vitest.
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import process from "node:process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = resolve(dirname(fileURLToPath(import.meta.url)));

// Throwaway git worktrees (acceptance-check runs) contain no frontend/node_modules;
// without a junction, Node module resolution walks up past the worktree and can
// bind to an unrelated checkout's vitest. Linked worktrees share the main
// checkout's .git directory, which locates the real node_modules.
if (!fs.existsSync(join(frontendDir, "node_modules"))) {
  const gitCommonDir = spawnSync(
    "git",
    ["rev-parse", "--path-format=absolute", "--git-common-dir"],
    { cwd: frontendDir, encoding: "utf8" },
  );
  const mainRoot = dirname((gitCommonDir.stdout ?? "").trim());
  if (mainRoot && mainRoot !== "." && fs.existsSync(join(mainRoot, "frontend", "node_modules"))) {
    const link = resolve(frontendDir, "..", "node_modules");
    if (!fs.existsSync(link)) {
      fs.symlinkSync(join(mainRoot, "frontend", "node_modules"), link, "junction");
    }
  }
}

process.chdir(frontendDir);

const result = spawnSync(
  "npx",
  ["vitest", "run", ...process.argv.slice(2)],
  { stdio: "inherit", shell: process.platform === "win32" },
);
process.exit(result.status ?? 1);
