#!/usr/bin/env python3
"""CI gate for skill-tree drift; delegates to scripts.sync_skills.

Runs sync_skills in --check mode and propagates its exit code so the Quality
contracts CI job fails when repo-specific skills have drifted across the three
runner trees. See scripts/sync_skills.py for the drift definition and
docs/engineering/skill-conventions.md for the canonical spec.
"""

from __future__ import annotations

import sys
from pathlib import Path

# scripts/ is a package (scripts/__init__.py exists). When invoked as
# `python scripts/check_skill_sync.py` the repo root is not on sys.path by
# default; mirror scripts/migrate_memories.py:11 by appending ROOT.
ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))

from scripts.sync_skills import main as sync_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(sync_main(["--check"]))
