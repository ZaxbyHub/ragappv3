"""Issue #1008 acceptance checks — NLTK offline resilience (frozen).

Two arm's-length checks over the offline-incident root causes:

* C1 (DISCRIMINATING): the Dockerfile backend stage bakes NLTK data (the
  ``punkt`` tokenizer family at minimum) next to the existing spaCy
  ``en_core_web_sm`` bake. At base the backend stage mentions spaCy but
  zero NLTK anything, so unstructured's NLTK-backed partitioners run in a
  container with no tokenizer data.
* C2 (DISCRIMINATING): a zero-element parse of a NON-EMPTY file must not
  return silently — the diagnostic (log warning or raised error) must name
  NLTK / tokenizer data so an operator can tell a missing-corpus container
  from an empty document. At base ``DocumentParser.parse`` returns
  unstructured's empty element list verbatim with no log line, so the
  assertion fails for exactly that reason.

The tests are frozen: they must pass UNMODIFIED once the fix lands (the
Dockerfile gains the nltk download step; the parse path gains the
diagnostic). They assert observables only — file text, log records, and
raised error text.
"""

import logging
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _unstructured_auto_module():
    """Return ``unstructured.partition.auto``, installing the sibling-suite
    stub tree when unstructured is absent (test_document_processor.py idiom).

    ``DocumentParser.parse`` lazy-imports
    ``from unstructured.partition.auto import partition`` inside the call,
    so a stub installed here is what production code resolves.
    """
    auto = sys.modules.get("unstructured.partition.auto")
    if auto is not None and hasattr(auto, "partition"):
        return auto
    unstructured = types.ModuleType("unstructured")
    unstructured.__path__ = []
    partition_pkg = types.ModuleType("unstructured.partition")
    partition_pkg.__path__ = []
    auto = types.ModuleType("unstructured.partition.auto")
    auto.partition = lambda *args, **kwargs: []
    unstructured.partition = partition_pkg
    sys.modules["unstructured"] = unstructured
    sys.modules["unstructured.partition"] = partition_pkg
    sys.modules["unstructured.partition.auto"] = auto
    return auto


def test_dockerfile_backend_stage_bakes_nltk_data():
    """C1: the backend build stage must bake NLTK data (punkt family)."""
    dockerfile = REPO_ROOT / "Dockerfile"
    assert dockerfile.is_file(), "harness: repo-root Dockerfile must exist"
    text = dockerfile.read_text(encoding="utf-8")

    # The backend stage begins at `FROM ... AS backend` and runs to EOF
    # (it is the final stage). Everything before it is the frontend builder.
    marker = "AS backend"
    assert marker in text, "harness: Dockerfile must declare a backend stage"
    backend_stage = text.split(marker, 1)[1].lower()

    assert "nltk" in backend_stage, (
        "the Dockerfile backend stage must bake NLTK data at build time "
        "(the spaCy en_core_web_sm bake alone leaves unstructured's "
        "NLTK-backed partitioners without tokenizer data in the image)"
    )
    assert "punkt" in backend_stage, (
        "the NLTK bake must name at least the punkt tokenizer corpus "
        "family (sentence tokenization), not just a bare nltk bootstrap"
    )


def test_zero_element_parse_names_nltk_data(tmp_path, monkeypatch, caplog):
    """C2: zero elements from a non-empty file must surface an NLTK
    diagnostic (log or raised error), never a silent empty list."""
    auto = _unstructured_auto_module()
    # The zero-element double: a NON-EMPTY file whose partition() returns
    # no elements — the offline container's exact observable.
    monkeypatch.setattr(auto, "partition", lambda *args, **kwargs: [], raising=False)

    from app.services.document_processor import DocumentParser

    non_empty = tmp_path / "f1-nonempty.pdf"
    non_empty.write_bytes(b"%PDF-1.4\n%offline-container stub document\n%%EOF\n")

    captured_logs = []
    class _Capture(logging.Handler):
        def emit(self, record):
            captured_logs.append(record.getMessage())

    module_logger = logging.getLogger("app.services.document_processor")
    handler = _Capture()
    module_logger.addHandler(handler)
    try:
        with caplog.at_level(logging.DEBUG, logger="app.services.document_processor"):
            raised_text = None
            try:
                result = DocumentParser().parse(str(non_empty))
            except Exception as exc:  # noqa: BLE001 - the fix may raise
                raised_text = str(exc)
            else:
                assert result == [], (
                    "harness: the zero-element partition double must flow "
                    f"through unchanged at base, got {result!r}"
                )
    finally:
        module_logger.removeHandler(handler)

    combined = " ".join(captured_logs + caplog.messages)
    if raised_text is not None:
        combined = combined + " " + raised_text
    lowered = combined.lower()
    assert "nltk" in lowered or "tokenizer" in lowered, (
        "a zero-element parse of a non-empty file must surface a diagnostic "
        "naming NLTK/tokenizer data (log warning or raised error text); at "
        "base the parse returns [] silently — captured logs "
        f"{captured_logs!r}, raised {raised_text!r}"
    )
