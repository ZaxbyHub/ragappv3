#!/usr/bin/env python3
"""Issue #780 L09 / AC4 — %BASE_URL% links in index.html must exist under frontend/public/.

frontend/index.html references build-served assets with Vite's %BASE_URL%
prefix. Issue #780 defect 4: the favicon links %BASE_URL%vite.svg, but
frontend/public/ has never contained it — production nginx try_files serves
/index.html (HTML) for the icon, so the browser falls back to its default.
This check scans index.html for every href="%BASE_URL%<target>" occurrence
and requires frontend/public/<target> to exist. Any missing target prints
"MISSING frontend/public/<target>" (one line per missing target) and the
script exits 1; all present prints "MISSING 0" and exits 0. A missing
index.html or public/ dir fails closed with exit 2.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "frontend" / "index.html"
PUBLIC_DIR = ROOT / "frontend" / "public"

BASE_URL_HREF_RE = re.compile(r"href=\"%BASE_URL%([^\"]+)\"")


def main() -> int:
    missing_paths = [p for p in (INDEX_HTML, PUBLIC_DIR) if not p.exists()]
    if missing_paths or not INDEX_HTML.is_file() or not PUBLIC_DIR.is_dir():
        print(f"index-assets: required paths not found: {missing_paths}", file=sys.stderr)
        return 2
    try:
        text = INDEX_HTML.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"index-assets: {exc}", file=sys.stderr)
        return 2

    missing_targets = [
        target
        for target in BASE_URL_HREF_RE.findall(text)
        if not (PUBLIC_DIR / target).is_file()
    ]
    if missing_targets:
        for target in missing_targets:
            print(f"MISSING frontend/public/{target}")
        return 1
    print("MISSING 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
