"""Arm's-length checks for the chat answer-integrity trace (issue #1153).

Round-3 shape (plan v2 wave 2, F1b/F2b). Legs run under subTest so a base
run reports every leg's outcome individually.

C1 pins FOUR surfaces against answer wrapper tags: (1) persisted bytes via
_upsert_assistant_turn, (2) the non-stream response final-content
construction, (3) the production done-content helper
`_canonical_done_content` the streaming finalize must route through (the
import failure at base IS the RED — the producer surface does not exist
yet), with a wrapper open tag split across two stream chunks, and (4) an
AST wiring pin that stream_chat_response actually references that helper.
All content legs assert wrapper-tags-absent AND body-present.

C2 pins claim segmentation in the unverifiable-claims detector with BOTH
negative assertions (no bare numeral fragments, no dangling "\n<marker>"
tails) AND positive assertions (every list sentence and the colon-header
sentence must survive as whole claims), so deleting list items can never
satisfy the check.
"""

import ast
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import lancedb  # noqa: F401
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")
try:
    import pyarrow  # noqa: F401
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

from app.models.database import init_db, run_migrations
from app.services.citation_validator import (
    _find_unverifiable_claims,
    repair_against_sources_and_memories,
)
from app.utils.assistant_sanitizer import sanitize_chat_messages_content

_BODY = (
    "The deadline is October 15. The milestone review follows one week"
    " later."
)

# The wrapper open tag split across two stream chunks: a naive per-chunk
# strip can never match either piece.
_SPLIT_TAG_CHUNKS = [
    "Here is the answer you requested.\n",
    "<ans",
    "wer>\n",
    _BODY,
    "\n</answer>\n",
]


class AnswerWrapperTagStrippingTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        db_path = str(Path(self._tmpdir) / "app.db")
        init_db(db_path)
        run_migrations(db_path)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.addCleanup(self.conn.close)

        vault_id = self.conn.execute(
            "INSERT INTO vaults (name, description) VALUES ('audit1115', 'v')"
        ).lastrowid
        self.session_id = self.conn.execute(
            "INSERT INTO chat_sessions (vault_id, user_id, title, created_at,"
            " updated_at) VALUES (?, 1, 'AUDIT1115', CURRENT_TIMESTAMP,"
            " CURRENT_TIMESTAMP)",
            (vault_id,),
        ).lastrowid
        self.conn.commit()

    def _assert_wrapper_free(self, content):
        lowered = content.lower()
        self.assertNotIn("<answer>", lowered)
        self.assertNotIn("</answer>", lowered)
        self.assertIn(_BODY, content)

    def test_answer_wrapper_tags_are_stripped(self):
        from app.api.routes.chat import _upsert_assistant_turn

        # Raw model output shape: a preamble line before the opener, the
        # body wrapped in <answer>...</answer> tags.
        raw_model_output = (
            "Here is the answer you requested.\n"
            "<answer>\n"
            + _BODY
            + "\n"
            "</answer>\n"
        )

        # Leg 1 — persisted bytes: _upsert_assistant_turn is the chat.py
        # persistence helper; its content line runs
        # sanitize_chat_messages_content on every durable assistant row, so
        # the assertion is on the bytes actually written to
        # chat_messages.content.
        with self.subTest(leg="persisted-bytes"):
            _upsert_assistant_turn(
                self.conn,
                {
                    "session_id": self.session_id,
                    "turn_id": "turn-a1115-1",
                    "status": "done",
                    "content": raw_model_output,
                },
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT content FROM chat_messages"
                " WHERE session_id = ? AND turn_id = ? AND role = 'assistant'",
                (self.session_id, "turn-a1115-1"),
            ).fetchone()
            self.assertIsNotNone(row)
            self._assert_wrapper_free(row[0])

        # Leg 2 — non-stream response return path. The route function needs
        # the full RAG engine stack, so this drives the exact final-content
        # construction seam non_stream_chat_response uses (chat.py
        # :1840-1861): join(collected_content) -> citation repair ->
        # sanitize_chat_messages_content, whose output is what the response
        # body carries.
        with self.subTest(leg="non-stream-response-content"):
            collected = [
                raw_model_output[:37],
                raw_model_output[37:],
            ]
            full_content = "".join(collected)
            cv = repair_against_sources_and_memories(full_content, [], [])
            full_content = cv.repaired_content
            full_content = sanitize_chat_messages_content(full_content)
            self._assert_wrapper_free(full_content)

        # Leg 3 — stream done-payload canonical content, driven through the
        # PRODUCTION helper. Plan F1b extracts the done-payload
        # canonical-content construction (chat.py streaming finalize,
        # :1213-1248 region) into chat.py as
        # `_canonical_done_content(full_content, repaired_content)` —
        # sanitizing the canonical content through the persistence boundary
        # and returning (canonical_content, differs_from_raw) — and the
        # streaming route obtains turn_state payload content through it.
        # At base the helper does not exist: the import failure below IS
        # the RED (producer-surface absence).
        with self.subTest(leg="done-content-helper"):
            from app.api.routes.chat import _canonical_done_content

            full_content = "".join(_SPLIT_TAG_CHUNKS)
            canonical, differs_from_raw = _canonical_done_content(
                full_content, None
            )
            self._assert_wrapper_free(canonical)
            self.assertIs(differs_from_raw, True)

        # Leg 4 — wiring pin: the streaming route must actually route its
        # canonical-content construction through the helper (grep-able
        # Load-context reference inside stream_chat_response, whose nested
        # _event_generator_inner carries the done-payload finalize). At
        # base there is no reference, so the pin is RED.
        with self.subTest(leg="streaming-wiring-pin"):
            chat_path = (
                Path(__file__).resolve().parents[1]
                / "app"
                / "api"
                / "routes"
                / "chat.py"
            )
            tree = ast.parse(chat_path.read_text(encoding="utf-8"))
            stream_fn = next(
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "stream_chat_response"
            )
            references = [
                node
                for node in ast.walk(stream_fn)
                if isinstance(node, ast.Name)
                and node.id == "_canonical_done_content"
                and isinstance(node.ctx, ast.Load)
            ]
            self.assertGreaterEqual(
                len(references),
                1,
                "stream_chat_response never references"
                " _canonical_done_content (%d Load references)"
                % len(references),
            )


class UnverifiableClaimsSentenceSegmentationTest(unittest.TestCase):
    # Completely unrelated source text: every sentence scores below the
    # 0.15 Jaccard threshold, so the detector evaluates every segment.
    SOURCES = [
        "Zebra husbandry records span several continents and include"
        " detailed notes about grazing schedules and veterinary care."
    ]

    def _assert_claims_segment_on_sentences(
        self, content, required_sentences, header=None
    ):
        claims = _find_unverifiable_claims(content, [], self.SOURCES)
        # The detector must fire at all, otherwise the segmentation
        # assertions below would be vacuous.
        self.assertTrue(
            claims, "detector must fire for unrelated sources"
        )
        # Positive assertions — every required sentence must survive as a
        # WHOLE claim. Acceptable forms per the post-fix contract: the
        # sentence alone, or the sentence with its "N. " list marker
        # attached as a prefix (the marker may stay glued to the following
        # sentence or be dropped, but the sentence text itself must never
        # be deleted — a fix that removes list items to eliminate numeral
        # fragments cannot pass these).
        for marker, sentence in required_sentences:
            accepted = {sentence}
            if marker:
                accepted.add("%s %s" % (marker, sentence))
            self.assertTrue(
                any(c.strip() in accepted for c in claims),
                "whole sentence not preserved in claims: %r (accepted"
                " forms: %r; all claims: %r)"
                % (sentence, sorted(accepted), claims),
            )
        # The colon-header sentence must survive as its own whole claim:
        # its trailing "\n1." marker may move to the following sentence or
        # be dropped, but the header text must remain intact (never left
        # holding a dangling marker).
        if header is not None:
            self.assertTrue(
                any(c.strip() == header for c in claims),
                "header sentence not preserved intact: %r (all claims: %r)"
                % (header, claims),
            )
        # Negative assertions — no claim may be a bare numeral fragment
        # split off a list item.
        bare = [c for c in claims if re.match(r"^\d+\.$", c.strip())]
        self.assertEqual(
            bare,
            [],
            "bare numeral fragments emitted as claims: %r (all: %r)"
            % (bare, claims),
        )
        # And no claim may end with a dangling "\n<marker>" numeral marker.
        dangling = [c for c in claims if re.search(r"\n\s*\d+\.\s*\Z", c)]
        self.assertEqual(
            dangling,
            [],
            "claims ending with a dangling numeral marker: %r (all: %r)"
            % (dangling, claims),
        )

    def test_unverifiable_claims_segment_on_sentences(self):
        # Shape 1 — newline-separated ordered list. 80+ chars, 2+ sentence
        # terminators, no refusal phrases, so the _looks_factual gate
        # admits it; list items carry periods.
        with self.subTest(shape="newline-ordered-list"):
            intro_1 = (
                "The platform migration proceeds in two phases with strict"
                " entry criteria."
            )
            intro_2 = (
                "Each phase requires sign-off from the platform owner"
                " before any cutover work begins."
            )
            item_1 = (
                "The first phase migrates the legacy database cluster to"
                " the new regional host pair."
            )
            item_2 = (
                "The second phase reconfigures the application tier to"
                " use the new connection brokers."
            )
            content = "\n".join(
                [intro_1 + " " + intro_2, "1. " + item_1, "2. " + item_2]
            )
            self._assert_claims_segment_on_sentences(
                content,
                [
                    (None, intro_1),
                    (None, intro_2),
                    ("1.", item_1),
                    ("2.", item_2),
                ],
            )

        # Shape 2 — R2 colon-header shape: the lead-in fragment ends with
        # ":\n1." so the header and the first list marker share one
        # tokenizer segment. Post-fix the header claim is a whole sentence
        # with no dangling "\n1." tail, and no bare numeral is emitted.
        with self.subTest(shape="colon-header-list"):
            header = (
                "Consider these findings about the migration corridor and"
                " its visitors:"
            )
            item_1 = (
                "Zebras migrate north during the dry season across the"
                " plateau region."
            )
            item_2 = (
                "Their herds cross the river at two established fording"
                " points."
            )
            content = "\n".join(
                [header, "1. " + item_1, "2. " + item_2]
            )
            self._assert_claims_segment_on_sentences(
                content,
                [("1.", item_1), ("2.", item_2)],
                header=header,
            )


if __name__ == "__main__":
    unittest.main()
