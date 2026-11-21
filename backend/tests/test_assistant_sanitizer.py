"""Tests for the centralized assistant content sanitizer.

Covers all four supported thinking-content patterns plus idempotency and
edge cases (unterminated blocks, empty input, mixed content).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.utils.assistant_sanitizer import (
    cleanup_existing_chat_messages_rows,
    sanitize_assistant_content,
    sanitize_chat_messages_content,
)


class TestSanitizeAssistantContent(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(sanitize_assistant_content(""), "")
        self.assertEqual(sanitize_assistant_content(None), "")  # type: ignore[arg-type]

    def test_no_thinking(self):
        self.assertEqual(
            sanitize_assistant_content("Just a normal answer."),
            "Just a normal answer.",
        )

    def test_strip_think_block(self):
        self.assertEqual(
            sanitize_assistant_content("<think>secret</think>visible"),
            "visible",
        )

    def test_strip_multiple_think_blocks(self):
        self.assertEqual(
            sanitize_assistant_content(
                "Hello <think>plan</think> world <think>more</think> end"
            ),
            "Hello  world  end",
        )

    def test_strip_lhs_rhs(self):
        self.assertEqual(
            sanitize_assistant_content("before _lhsthinking_rhs after"),
            "before  after",
        )

    def test_strip_thinking_process_block(self):
        text = "Thinking Process: I will check facts.</think>Final answer."
        self.assertEqual(sanitize_assistant_content(text), "Final answer.")

    def test_unterminated_think_block_is_stripped(self):
        # A leaked unterminated <think>... must not be persisted.
        text = "Visible answer\n<think>still thinking but stream ended"
        self.assertEqual(sanitize_assistant_content(text), "Visible answer")

    def test_idempotent(self):
        text = "<think>plan</think>Hello"
        once = sanitize_assistant_content(text)
        twice = sanitize_assistant_content(once)
        self.assertEqual(once, twice)
        self.assertEqual(once, "Hello")

    def test_case_insensitive_think(self):
        self.assertEqual(
            sanitize_assistant_content("<THINK>SECRET</THINK>visible"),
            "visible",
        )

    def test_chat_messages_alias(self):
        # The persistence-boundary alias must behave identically.
        text = "<think>x</think>kept"
        self.assertEqual(
            sanitize_chat_messages_content(text),
            sanitize_assistant_content(text),
        )

    def test_mixed_patterns(self):
        text = (
            "<think>a</think>start "
            "_lhsB_rhs middle "
            "Thinking Process: c</think>end"
        )
        self.assertEqual(
            sanitize_assistant_content(text),
            "start  middle end",
        )


class TestCleanupRows(unittest.TestCase):
    def test_returns_only_changed_rows(self):
        rows = [
            (1, "<think>x</think>visible"),
            (2, "no thinking here"),
            (3, "_lhsdrop_rhsalso visible"),
        ]
        out = cleanup_existing_chat_messages_rows(rows)
        ids = [r[0] for r in out]
        self.assertEqual(ids, [1, 3])
        self.assertEqual(out[0][1], "visible")
        self.assertEqual(out[1][1], "also visible")

    def test_idempotent(self):
        rows = [(1, "<think>x</think>v")]
        first = cleanup_existing_chat_messages_rows(rows)
        # Apply the cleaned rows again — no further changes.
        second = cleanup_existing_chat_messages_rows(first)
        self.assertEqual(second, [])


class AnswerWrapperFenceAwarenessTest(unittest.TestCase):
    """Review F-07: <answer> wrapper tags are stripped outside code fences but
    a literal <answer> inside a fenced block is preserved (runtime AND the
    startup re-sanitize migration, which would otherwise rewrite history)."""

    def test_real_wrapper_tags_outside_fence_are_stripped(self):
        self.assertEqual(
            sanitize_assistant_content("<answer>the capital is Paris</answer>"),
            "the capital is Paris",
        )

    def test_literal_answer_inside_backtick_fence_survives(self):
        text = (
            "Here is an example:\n"
            "```xml\n"
            "<answer>\n"
            "  <value>42</value>\n"
            "</answer>\n"
            "```\n"
            "That was the example."
        )
        self.assertEqual(sanitize_assistant_content(text), text)

    def test_literal_answer_inside_tilde_fence_survives(self):
        text = "~~~\n<answer>keep me</answer>\n~~~"
        self.assertEqual(sanitize_assistant_content(text), text)

    def test_wrapper_outside_fence_still_stripped_when_fence_present(self):
        text = (
            "<answer>real answer</answer>\n"
            "```\n<answer>not a wrapper</answer>\n```\n"
        )
        cleaned = sanitize_assistant_content(text)
        self.assertIn("real answer", cleaned)
        self.assertIn("<answer>not a wrapper</answer>", cleaned)

    def test_fence_aware_strip_is_idempotent(self):
        text = "```\n<answer>x</answer>\n```\n<answer>y</answer>"
        once = sanitize_assistant_content(text)
        self.assertEqual(once, sanitize_assistant_content(once))

    def test_longer_closing_fence_keeps_literal_answer(self):
        # CommonMark: a closing fence may be longer than the opening one.
        text = "```\n<answer>keep</answer>\n````"
        self.assertEqual(sanitize_assistant_content(text), text)

    def test_unclosed_fence_runs_to_eof_keeps_literal_answer(self):
        text = "```\n<answer>keep</answer>"
        self.assertEqual(sanitize_assistant_content(text), text)

    def test_wrapper_outside_unclosed_fence_is_still_stripped(self):
        # A real wrapper BEFORE an unterminated fence has its tags removed (inner
        # text kept, per AC1); the fence then consumes the rest to EOF
        # (CommonMark), protecting the later literal markup.
        text = "<answer>strip me</answer>\n```\n<answer>keep me</answer>"
        cleaned = sanitize_assistant_content(text)
        self.assertIn("strip me", cleaned)
        self.assertNotIn("<answer>strip me", cleaned)
        self.assertIn("<answer>keep me</answer>", cleaned)


if __name__ == "__main__":
    unittest.main()
