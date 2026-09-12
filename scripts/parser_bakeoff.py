#!/usr/bin/env python3
"""Parser bake-off harness: benchmark available parser backends on real docs.

Issue #258 (ENH-007 / legacy-10): the nightly tier installs the full parser
stack, so the same committed fixtures under backend/tests/fixtures/real_docs/
can be run through every importable backend and compared on wall-clock time
and table-cell extraction. Backends:

* ``unstructured-fast``   — ``unstructured.partition.auto.partition`` with
  ``strategy="fast"`` (available wherever the full stack is installed).
* ``unstructured-hi_res`` — ``partition(strategy="hi_res")``; may download
  layout models on first use. Errors are recorded in the report, not fatal.
* ``docling``             — optional; skipped with a note when not installed.
* ``marker``              — optional; skipped with a note when not installed.

Output: ``docs/benchmarks/parser-bakeoff.md`` — a timestamped run section
(newest first, the last ``--keep-runs`` sections retained) recording which
backends ran / were skipped / errored and a per-fixture table of timings,
text-chunk counts, and table-cell counts. The nightly workflow uploads the
markdown as an artifact and commits nothing.

Stdlib-only at module level; heavy parser imports happen lazily inside each
backend runner. Exit codes: 0 = report written (per-backend errors live in
the report); 1 = nothing could be benchmarked (no fixtures or no backend
produced a single result).
"""

from __future__ import annotations

import argparse
import html.parser
import importlib.util
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = ROOT / "backend" / "tests" / "fixtures" / "real_docs"
DEFAULT_OUTPUT = ROOT / "docs" / "benchmarks" / "parser-bakeoff.md"
DEFAULT_KEEP_RUNS = 10
FIXTURE_SUFFIXES = {".pdf", ".docx", ".xlsx", ".pptx", ".md"}


@dataclass
class Row:
    """One (fixture, backend) measurement."""

    fixture: str
    backend: str
    status: str  # "ran" | "error"
    seconds: float
    text_chunks: int
    table_cells: int
    note: str = ""


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # broken parent package, etc.
        return False


class _TableCellCounter(html.parser.HTMLParser):
    """Counts <td>/<th> start tags in an HTML table fragment."""

    def __init__(self) -> None:
        super().__init__()
        self.cells = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("td", "th"):
            self.cells += 1


def _html_cell_count(fragment: str) -> int:
    counter = _TableCellCounter()
    try:
        counter.feed(fragment)
    except Exception:  # noqa: BLE001 - malformed html must not kill a run
        return 0
    return counter.cells


def _pipe_cell_count(text: str) -> int:
    """Cells in a pipe-delimited row, markdown style (``a | b | c`` -> 3)."""
    stripped = text.strip()
    if "|" not in stripped:
        return 0
    return len([cell for cell in stripped.split("|") if cell.strip()])


def _element_texts_and_cells(elements) -> tuple[int, int]:
    """Non-empty text-chunk count and table-cell count for unstructured output."""
    chunks = 0
    cells = 0
    for element in elements or []:
        text = str(element).strip()
        if text:
            chunks += 1
            if "|" in text:
                cells += _pipe_cell_count(text)
        metadata = getattr(element, "metadata", None)
        as_html = getattr(metadata, "text_as_html", None)
        if as_html:
            cells += _html_cell_count(as_html)
    return chunks, cells


def _run_unstructured(strategy: str, path: Path) -> Row:
    from unstructured.partition.auto import partition

    started = time.perf_counter()
    elements = partition(filename=str(path), strategy=strategy)
    elapsed = time.perf_counter() - started
    chunks, cells = _element_texts_and_cells(elements)
    return Row(
        fixture=path.name,
        backend=f"unstructured-{strategy}",
        status="ran",
        seconds=elapsed,
        text_chunks=chunks,
        table_cells=cells,
    )


def _run_docling(path: Path) -> Row:
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter()
    started = time.perf_counter()
    result = converter.convert(str(path))
    elapsed = time.perf_counter() - started
    document = getattr(result, "document", result)
    chunks = sum(
        1
        for item in getattr(document, "texts", [])
        if getattr(item, "text", "").strip()
    )
    cells = 0
    for table in getattr(document, "tables", []) or []:
        try:
            cells += int(table.export_to_dataframe().size)
        except Exception:  # noqa: BLE001 - API varies across docling versions
            cells += _pipe_cell_count(str(getattr(table, "export_to_markdown", str)()))
    return Row(
        fixture=path.name,
        backend="docling",
        status="ran",
        seconds=elapsed,
        text_chunks=chunks,
        table_cells=cells,
    )


def _run_marker(path: Path) -> Row:
    # marker-pdf's entrypoint moved across versions; try the current
    # converter API first, then the legacy single-PDF helper.
    try:
        from marker.converters.pdf import PdfConverter

        converter = PdfConverter()
        started = time.perf_counter()
        rendered = converter(str(path))
        text = rendered.text if hasattr(rendered, "text") else str(rendered)
    except ImportError:
        from marker.convert import convert_single_pdf

        started = time.perf_counter()
        _, text, _ = convert_single_pdf(str(path), [])
    elapsed = time.perf_counter() - started
    chunks = sum(1 for line in text.splitlines() if line.strip())
    cells = sum(_pipe_cell_count(line) for line in text.splitlines())
    return Row(
        fixture=path.name,
        backend="marker",
        status="ran",
        seconds=elapsed,
        text_chunks=chunks,
        table_cells=cells,
    )


# (backend name, availability module, runner). Availability is probed once
# per invocation; runners import their heavy deps lazily.
BACKENDS: list[tuple[str, str, object]] = [
    ("unstructured-fast", "unstructured", lambda p: _run_unstructured("fast", p)),
    ("unstructured-hi_res", "unstructured", lambda p: _run_unstructured("hi_res", p)),
    ("docling", "docling", _run_docling),
    ("marker", "marker", _run_marker),
]


def run_bakeoff(fixtures_dir: Path) -> tuple[list[Row], dict[str, str]]:
    """Benchmark every available backend over every fixture."""
    rows: list[Row] = []
    backend_status: dict[str, str] = {}
    fixtures = sorted(
        p
        for p in fixtures_dir.iterdir()
        if p.is_file() and p.suffix.lower() in FIXTURE_SUFFIXES
    )
    for backend, module, runner in BACKENDS:
        if not _module_available(module):
            backend_status[backend] = "skipped — not installed"
            continue
        ran_any = False
        for fixture in fixtures:
            try:
                row = runner(fixture)
            except Exception as exc:  # noqa: BLE001 - record, don't abort
                row = Row(
                    fixture=fixture.name,
                    backend=backend,
                    status="error",
                    seconds=0.0,
                    text_chunks=0,
                    table_cells=0,
                    note=f"{type(exc).__name__}: {exc}",
                )
            rows.append(row)
            ran_any = ran_any or row.status == "ran"
        backend_status[backend] = "ran" if ran_any else "skipped — errored on every fixture"
    return rows, backend_status


def _format_run_section(rows: list[Row], backend_status: dict[str, str]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"## Run {stamp}",
        "",
        f"Python {sys.version.split()[0]} on {sys.platform}.",
        "",
        "Backends:",
    ]
    for backend, status in backend_status.items():
        lines.append(f"- {backend}: {status}")
    lines += [
        "",
        "| fixture | backend | status | seconds | text chunks | table cells | note |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row.fixture} | {row.backend} | {row.status} | "
            f"{row.seconds:.3f} | {row.text_chunks} | {row.table_cells} | "
            f"{row.note} |"
        )
    lines += ["", "Errors are recorded inline; a skipped backend never ran here.",
              ""]
    return "\n".join(lines)


def write_report(
    output: Path, rows: list[Row], backend_status: dict[str, str], keep: int
) -> None:
    """Write/refresh the benchmark markdown, newest run first."""
    header = (
        "# Parser bake-off\n"
        "\n"
        "Generated by `scripts/parser_bakeoff.py` in the nightly tier\n"
        "(`.github/workflows/nightly.yml`); the markdown is uploaded as a\n"
        "workflow artifact, never committed. Each section is one timestamped\n"
        f"run; the newest {keep} runs are kept.\n"
    )
    prior: list[str] = []
    if output.is_file():
        chunks = output.read_text(encoding="utf-8").split("\n## Run ")[1:]
        prior = ["\n## Run " + chunk for chunk in chunks if chunk.strip()]
    prior = prior[: max(keep - 1, 0)]
    new_section = _format_run_section(rows, backend_status).rstrip("\n")
    output.parent.mkdir(parents=True, exist_ok=True)
    body = "\n\n".join([new_section, *prior])
    output.write_text(header + "\n" + body + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixtures-dir", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--keep-runs", type=int, default=DEFAULT_KEEP_RUNS)
    args = parser.parse_args(argv)

    if not args.fixtures_dir.is_dir():
        print(f"parser-bakeoff: fixtures dir not found: {args.fixtures_dir}", file=sys.stderr)
        return 1
    rows, backend_status = run_bakeoff(args.fixtures_dir)
    write_report(args.output, rows, backend_status, args.keep_runs)
    ran = [row for row in rows if row.status == "ran"]
    print(
        f"parser-bakeoff: {len(ran)} measurement(s) across "
        f"{sum(1 for s in backend_status.values() if s.startswith('ran'))} backend(s); "
        f"report: {args.output}"
    )
    if not ran:
        print("parser-bakeoff: no backend produced a result", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
