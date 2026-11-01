"""C01 acceptance check (issue #706, vector-store migration safety).

The README's ``## Upgrading`` section must name the admin reindex
endpoint (``/api/documents/reindex``) as the remedy for migration /
index-shape changes. An operator whose index needs re-embedding after an
upgrade has exactly one supported lever — the reindex job — and the
upgrade notes must point at it. The reindex example must also be
copy-paste runnable (full URL, a named completion path) and quote the
503 detail string as the backend words it.
"""

from pathlib import Path


def test_readme_upgrading_names_reindex_remedy() -> None:
    # backend/tests/<this file> -> repo root is parents[2].
    readme_path = Path(__file__).resolve().parents[2] / "README.md"
    text = readme_path.read_text(encoding="utf-8")

    # Slice the `## Upgrading` section: from the heading line up to (not
    # including) the next `## ` heading.
    section_lines: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            if in_section:
                break
            if line.strip() == "## Upgrading":
                in_section = True
                continue
        if in_section:
            section_lines.append(line)

    assert in_section, "README.md must have an '## Upgrading' section"
    section = "\n".join(section_lines)
    count = int(section.count("/api/documents/reindex"))
    assert count >= 1

    # The remedy must be copy-paste runnable: curl rejects a path-only URL
    # outright, so no invocation in the section may be host-less.
    assert "curl -X POST /" not in section, (
        "Upgrading curl examples must use a full URL (e.g. http://localhost:9090/...)"
    )
    # The reindex POST returns immediately with a job_id; the section must
    # also name the completion path (the job status route).
    assert "/api/documents/reindex/jobs/" in section
    # The 503 detail must be quoted as the backend words it (deps.py
    # require_model_ready), not paraphrased.
    assert "Embedding model mismatch — admin reindex required" in section
