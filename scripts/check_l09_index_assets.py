#!/usr/bin/env python3
"""Issue #780 L09 / AC4 — %BASE_URL% links in index.html must exist under frontend/public/.

frontend/index.html references build-served assets with Vite's %BASE_URL%
prefix. Issue #780 defect 4: the favicon links %BASE_URL%vite.svg, but
frontend/public/ has never contained it — production nginx try_files serves
/index.html (HTML) for the icon, so the browser falls back to its default.
This check scans index.html for every href= or src= attribute whose value is
%BASE_URL%-prefixed (both quote styles, optional whitespace around =, case
insensitive) and requires frontend/public/<target> to exist. A leading `/`
or a `..` segment in the target escapes public/ and is reported MISSING.
Query strings are stripped before the existence check (they must not mask a
missing file, and a missing file must not be reported present). Any missing
target prints "MISSING frontend/public/<target>" and the script exits 1; all
present prints "MISSING 0" and exits 0. Fail-closed (review feedback, PR
#880): when the text contains %BASE_URL% but the attribute scan finds zero
occurrences, the parser has drifted — exit 2. A missing index.html or
public/ dir also exits 2.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "frontend" / "index.html"
PUBLIC_DIR = ROOT / "frontend" / "public"

BASE_URL_ATTR_RE = re.compile(r"""(?:href|src)\s*=\s*["']%BASE_URL%([^"']+)["']""", re.IGNORECASE)


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

    targets = BASE_URL_ATTR_RE.findall(text)
    if not targets and "%BASE_URL%" in text:
        # Parser drift: the file references %BASE_URL% but the attribute
        # regex matched none of the occurrences (quote/attr-form change).
        print("index-assets: %BASE_URL% present but zero attributes parsed (parser drift)", file=sys.stderr)
        return 2

    missing_targets = []
    for target in targets:
        path = target.lstrip("/")
        path = path.split("?", 1)[0]  # a query string must not mask a missing file
        if not path or ".." in path.split("/"):
            # Escapes frontend/public/ — nothing the build serves can back it.
            missing_targets.append(target)
            continue
        if not (PUBLIC_DIR / path).is_file():
            missing_targets.append(target)
    if missing_targets:
        for target in missing_targets:
            print(f"MISSING frontend/public/{target}")
        return 1
    print("MISSING 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
