"""Per-field enrichment response validation tests (issue #511, FULL-ENH-05).

Covers the acceptance behavior of check C15:

- Each auxiliary field of the parsed enrichment JSON is validated/coerced
  INDEPENDENDENTLY: summary -> str; questions/entities/aliases -> list of
  str only (non-str entries dropped), capped at 5/10/10.
- A malformed field is dropped (with a warning naming the field) and does
  NOT abort the remaining fields.
- Well-formed responses produce byte-identical results to the previous
  behavior; non-JSON responses keep the existing error path.
"""

import json
import os
import sys
import types

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow  # noqa: F401
except ImportError:
    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

import pytest

from app.services.chunk_enrichment import ChunkEnrichment, ChunkEnrichmentService


class FakeLLMClient:
    """In-memory LLM client returning a fixed response string."""

    def __init__(self, response: str):
        self._response = response
        self.calls: list = []

    async def chat_completion(self, messages, max_tokens=None, temperature=None):
        self.calls.append(
            {"messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        )
        return self._response


def _chunk(text="Alpha beta gamma delta epsilon zeta eta theta content."):
    return {"chunk_uid": "c1", "text": text, "metadata": {}}


def _service(response: str, enrichment_fields=None) -> tuple:
    client = FakeLLMClient(response)
    return (
        ChunkEnrichmentService(
            llm_client=client, concurrency=1, enrichment_fields=enrichment_fields
        ),
        client,
    )


_ALL_FIELDS = ["summary", "questions", "entities", "aliases"]


@pytest.mark.asyncio
class TestPerFieldValidation:
    async def test_partially_malformed_response_fields_coerced_independently(self):
        """C15 core: valid fields retained, malformed fields dropped per-field."""
        response = json.dumps(
            {
                "summary": "ok",
                "questions": "not-a-list",
                "entities": [1, "x", "y"],
                "aliases": None,
            }
        )
        service, _ = _service(response, enrichment_fields=_ALL_FIELDS)

        results = await service.enrich_chunks([_chunk()], "Doc Title")

        assert len(results) == 1
        enrichment = results[0]
        assert enrichment is not None
        assert enrichment.summary == "ok"
        assert enrichment.questions == []
        assert enrichment.entities == ["x", "y"]
        assert enrichment.aliases == []

    async def test_all_fields_malformed_still_returns_enrichment(self):
        response = json.dumps(
            {
                "summary": 42,
                "questions": {"deep": "dict"},
                "entities": "nope",
                "aliases": 3.14,
            }
        )
        service, _ = _service(response, enrichment_fields=_ALL_FIELDS)

        results = await service.enrich_chunks([_chunk()], "Doc Title")

        assert len(results) == 1
        enrichment = results[0]
        assert enrichment.summary == ""
        assert enrichment.questions == []
        assert enrichment.entities == []
        assert enrichment.aliases == []

    async def test_non_str_summary_dropped_to_empty(self):
        response = json.dumps({"summary": ["not", "a", "string"]})
        service, _ = _service(response)

        results = await service.enrich_chunks([_chunk()], "Doc Title")

        assert results[0].summary == ""

    async def test_list_fields_drop_non_string_entries(self):
        response = json.dumps(
            {
                "questions": [None, 1, "q1", 2.5, True, "q2"],
                "entities": [["nested"], "e1", {"x": 1}, "e2"],
                "aliases": ["a1", "a2", 99],
            }
        )
        service, _ = _service(response, enrichment_fields=_ALL_FIELDS)

        results = await service.enrich_chunks([_chunk()], "Doc Title")

        enrichment = results[0]
        assert enrichment.questions == ["q1", "q2"]
        assert enrichment.entities == ["e1", "e2"]
        assert enrichment.aliases == ["a1", "a2"]

    async def test_caps_enforced_after_filtering(self):
        response = json.dumps(
            {
                "questions": [f"q{i}" for i in range(7)],
                "entities": [f"e{i}" for i in range(12)],
                "aliases": [f"a{i}" for i in range(12)],
            }
        )
        service, _ = _service(response, enrichment_fields=_ALL_FIELDS)

        results = await service.enrich_chunks([_chunk()], "Doc Title")

        enrichment = results[0]
        assert enrichment.questions == [f"q{i}" for i in range(5)]
        assert enrichment.entities == [f"e{i}" for i in range(10)]
        assert enrichment.aliases == [f"a{i}" for i in range(10)]

    async def test_cap_applies_to_surviving_string_entries_only(self):
        """A list whose non-str prefix is dropped still caps at the limit."""
        response = json.dumps(
            {
                "questions": [0, 1, 2, 3, 4, 5, 6, "q1", "q2", "q3", "q4", "q5", "q6"],
            }
        )
        service, _ = _service(response)

        results = await service.enrich_chunks([_chunk()], "Doc Title")

        assert results[0].questions == ["q1", "q2", "q3", "q4", "q5"]


@pytest.mark.asyncio
class TestWellFormedUnchanged:
    async def test_well_formed_response_byte_identical(self):
        payload = {
            "summary": "A concise summary.",
            "questions": ["q1?", "q2?", "q3?"],
            "entities": ["alpha", "beta"],
            "aliases": ["a", "b"],
        }
        service, _ = _service(json.dumps(payload), enrichment_fields=_ALL_FIELDS)

        results = await service.enrich_chunks([_chunk()], "Doc Title")

        expected = ChunkEnrichment(
            chunk_id="c1",
            summary="A concise summary.",
            questions=["q1?", "q2?", "q3?"],
            entities=["alpha", "beta"],
            aliases=["a", "b"],
            section_breadcrumb="Doc Title",
        )
        assert results[0] == expected
        assert results[0].to_dict() == expected.to_dict()

    async def test_markdown_fenced_json_still_parsed(self):
        payload = {"summary": "fenced", "questions": [], "entities": [], "aliases": []}
        service, _ = _service("```json\n" + json.dumps(payload) + "\n```")

        results = await service.enrich_chunks([_chunk()], "Doc Title")

        assert results[0].summary == "fenced"

    async def test_extra_unknown_fields_ignored(self):
        payload = {"summary": "s", "unexpected": {"x": 1}, "questions": ["q"]}
        service, _ = _service(json.dumps(payload))

        results = await service.enrich_chunks([_chunk()], "Doc Title")

        assert results[0].summary == "s"
        assert results[0].questions == ["q"]

    async def test_missing_fields_default_to_empty(self):
        service, _ = _service(json.dumps({"summary": "only summary"}))

        results = await service.enrich_chunks([_chunk()], "Doc Title")

        enrichment = results[0]
        assert enrichment.summary == "only summary"
        assert enrichment.questions == []
        assert enrichment.entities == []
        assert enrichment.aliases == []


@pytest.mark.asyncio
class TestErrorPathsPreserved:
    async def test_invalid_json_returns_empty_enrichment(self):
        service, _ = _service("this is not json at all")

        results = await service.enrich_chunks([_chunk()], "Doc Title")

        assert len(results) == 1
        assert results[0] == ChunkEnrichment(chunk_id="c1", section_breadcrumb="Doc Title")

    async def test_non_dict_json_returns_empty_enrichment(self):
        service, _ = _service(json.dumps(["a", "list"]))

        results = await service.enrich_chunks([_chunk()], "Doc Title")

        assert len(results) == 1
        assert results[0].summary == ""
        assert results[0].questions == []

    async def test_malformed_field_logs_warning_naming_field(self, caplog):
        response = json.dumps({"summary": "ok", "questions": "not-a-list"})
        service, _ = _service(response)

        with caplog.at_level("WARNING", logger="app.services.chunk_enrichment"):
            await service.enrich_chunks([_chunk()], "Doc Title")

        warning_text = " ".join(rec.getMessage() for rec in caplog.records)
        assert "questions" in warning_text, (
            "the warning must name the malformed field; got: %r" % warning_text
        )
        assert "c1" in warning_text or "chunk" in warning_text.lower()
