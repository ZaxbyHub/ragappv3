#!/usr/bin/env python3
"""Fail when an addChatMessagesBatch mock result drops the server-issued seq.

The batch-save endpoint returns the durable order for every saved row
(backend/app/api/routes/chat.py: ``"seq": row[9]``), and the live-send path
must propagate that seq into the store rows (issue #683 — a dropped seq
collapses the retry/edit truncate anchor to 0 and deletes every persisted
turn). A send-path spec whose batch mock answers with rows lacking ``seq``
cannot observe that contract at all: it bakes in the buggy mock shape the
issue describes, so a fix would pass while the spec stays blind.

Contracts enforced:

  C-BATCHSEQ-1: in each target spec file, every row object literal appearing
      in an ``addChatMessagesBatch`` mock result (mockResolvedValue /
      mockResolvedValueOnce / mockReturnValue / mockReturnValueOnce /
      mockImplementation / mockImplementationOnce argument) carries a
      ``seq`` key whose value is numeric at runtime — a plain literal or a
      numeric expression such as the mapped-fixture form ``seq: index + 1``.
      ``null``, ``undefined``, booleans, and quoted (stringly) values
      violate the contract, which promises a number. A row literal is a
      balanced ``{...}`` block whose top-level keys include ``id`` (the
      saved-row shape); keys may be bare identifiers or quoted strings, and
      shorthand properties (``{ id }``) count as keys when at key position. A result setter
      invoked with no argument — or with an explicit ``undefined``/``null``
      result — installs nothing contract-accurate and is a violation.
  C-BATCHSEQ-2: a target file with NO ``addChatMessagesBatch`` mock result
      at all is itself a violation — the send-path specs must exercise the
      contract-accurate mock.

Occurrences inside comments or string/template literals are never treated as
mock chains, so documentation of the old mock shape cannot fail the gate.

Targets (fixed, in deterministic report order):

  frontend/src/hooks/useSendMessage.test.ts
  frontend/src/hooks/useSendMessage.issue553.test.ts
  frontend/src/hooks/useSendMessage.liveSeq.test.ts
  frontend/src/components/chat/TranscriptPane.liveSeqRevision.test.tsx
  frontend/src/hooks/useSendMessage.evidence.test.ts

Known limitation (documented in docs/releases/pending/683-retry-edit-durable-seq.md):
rows with no inline object literal at the result-setter call site — built by
helpers or referenced through variables
(``mockResolvedValue(savedRows(messages))``, ``mockResolvedValue(rows)``) or
resolved through a promise captured elsewhere (``resolveBatch([...])``) —
are not inspected.

Regex-over-source is acceptable here (test files, not production code), but
the row-literal extraction is a small brace-matching scan — not a flat regex
— so multiline arrays, chained mock calls (``.mockReset().mockResolvedValue(
[...])``), nested literals, and template strings are handled. stdlib only.

Exit codes: 0 = every batch-mock row carries a numeric seq (prints one OK
line), 1 = at least one offending file (prints one ``MISSING seq: <path>``
line per offender, in target order). Paths anchor to the repository root via
this file's location, so the gate behaves identically from any cwd (matching
the sibling scripts/check_*.py gates).
"""

import re
import sys
from pathlib import Path

TARGETS = (
    "frontend/src/hooks/useSendMessage.test.ts",
    "frontend/src/hooks/useSendMessage.issue553.test.ts",
    "frontend/src/hooks/useSendMessage.liveSeq.test.ts",
    "frontend/src/components/chat/TranscriptPane.liveSeqRevision.test.tsx",
    "frontend/src/hooks/useSendMessage.evidence.test.ts",
)

MOCK_TARGET = "addChatMessagesBatch"
# Mock methods that INSTALL a result (calls like toHaveBeenCalledWith are
# assertions, not results, and are ignored).
RESULT_SETTERS = frozenset(
    {
        "mockResolvedValue",
        "mockResolvedValueOnce",
        "mockReturnValue",
        "mockReturnValueOnce",
        "mockImplementation",
        "mockImplementationOnce",
    }
)

_CHAIN_STEP = re.compile(r"\s*\.\s*(\w+)")
# A contract-accurate seq value is numeric at runtime: a plain numeric
# literal or a numeric expression (e.g. the mapped-fixture form
# ``seq: index + 1``). Null, undefined, booleans, and quoted (stringly)
# values violate the contract, which promises a number.
_SEQ_NON_NUMERIC = frozenset({"null", "undefined", "true", "false"})


def _skip_string(text: str, i: int) -> int:
    """Index past the string literal whose opening quote is at ``text[i]``."""
    quote = text[i]
    i += 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == quote:
            return i + 1
        i += 1
    return n  # unterminated: caller bounds-checks anyway


def _skip_comment(text: str, i: int) -> int:
    """Index past the ``//`` or ``/* */`` comment starting at ``text[i]``."""
    n = len(text)
    if text[i : i + 2] == "//":
        j = text.find("\n", i)
        return n if j < 0 else j + 1
    j = text.find("*/", i + 2)
    return n if j < 0 else j + 2


def _at_string_or_comment(text: str, i: int) -> bool:
    c = text[i]
    if c in "\"'`":
        return True
    return c == "/" and text[i + 1 : i + 2] in ("/", "*")


def _find_code_occurrence(text: str, needle: str, start: int) -> int:
    """Index of the next ``needle`` outside strings and comments, or -1."""
    n = len(text)
    i = start
    while i < n:
        if _at_string_or_comment(text, i):
            i = _skip_string(text, i) if text[i] in "\"'`" else _skip_comment(text, i)
            continue
        if text.startswith(needle, i):
            return i
        i += 1
    return -1


def _read_balanced(text: str, i: int, open_ch: str, close_ch: str) -> int | None:
    """Index just past the balanced closer for the opener at ``text[i]``.

    Strings and comments inside are skipped so delimiters within them cannot
    unbalance the scan. None means unbalanced (end of input first).
    """
    n = len(text)
    depth = 0
    while i < n:
        if _at_string_or_comment(text, i):
            i = _skip_string(text, i) if text[i] in "\"'`" else _skip_comment(text, i)
            continue
        c = text[i]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _object_literals(text: str) -> list[str]:
    """Every balanced ``{...}`` block in ``text``, at any nesting depth."""
    n = len(text)
    blocks: list[str] = []
    starts: list[int] = []
    i = 0
    while i < n:
        if _at_string_or_comment(text, i):
            i = _skip_string(text, i) if text[i] in "\"'`" else _skip_comment(text, i)
            continue
        c = text[i]
        if c == "{":
            starts.append(i)
        elif c == "}" and starts:
            start = starts.pop()
            blocks.append(text[start + 1 : i])
        i += 1
    return blocks


def _skip_ws_comments(text: str, i: int) -> int:
    """Index past whitespace and comments starting at ``text[i]``."""
    n = len(text)
    while i < n:
        if text[i] in " \t\r\n":
            i += 1
        elif text[i] == "/" and text[i + 1 : i + 2] in ("/", "*"):
            i = _skip_comment(text, i)
        else:
            break
    return i


def _blank_comments(text: str) -> str:
    """``text`` with every comment replaced by spaces (same length).

    Newlines are preserved so line-oriented structure survives; string
    literals are kept verbatim. Downstream index-based scans can then treat
    comment text as plain whitespace in both directions.
    """
    out = list(text)
    n = len(text)
    i = 0
    while i < n:
        if _at_string_or_comment(text, i):
            if text[i] == "/":
                start = i
                i = _skip_comment(text, i)
                for k in range(start, i):
                    if out[k] != "\n":
                        out[k] = " "
                continue
            i = _skip_string(text, i)
            continue
        i += 1
    return "".join(out)


def _value_text(block: str, i: int) -> str:
    """Value text after a ``key:`` colon, up to the row delimiter.

    Comments are dropped from the span entirely (not merely skipped during
    the scan), so a trailing ``// legacy`` cannot mask a ``null`` value;
    string values keep their quotes so a quoted value cannot pass a numeric
    check.
    """
    n = len(block)
    j = i
    while j < n and block[j] in " \t\r\n":
        j += 1
    parts: list[str] = []
    while j < n:
        if _at_string_or_comment(block, j):
            if block[j] == "/":
                j = _skip_comment(block, j)
                continue
            start_str = j
            j = _skip_string(block, j)
            parts.append(block[start_str:j])
            continue
        if block[j] in ",}":
            break
        parts.append(block[j])
        j += 1
    return "".join(parts).strip()


def _top_level_key_value(block: str, key: str) -> str | None:
    """Raw value text for ``key:`` at brace-depth 0 of ``block``, or None.

    Keys may be bare identifiers or quoted strings (``"seq": 1``). Whole-
    identifier match only (``created_at`` does not contain a hit for
    ``id``); comments are blanked to spaces up front so they cannot displace
    a key or its colon in either direction, strings and comments are skipped
    during the walk, nested objects are entered so their keys do not leak
    into the outer level, and shorthand properties (``{ id }``) count as
    keys whose value is the identifier itself.
    """
    block = _blank_comments(block)
    n = len(block)
    depth = 0
    i = 0
    while i < n:
        c = block[i]
        if depth == 0 and c in "\"'":
            # A quoted string is only a KEY when the nearest preceding
            # non-whitespace character opens the block or follows a comma;
            # otherwise it is a value-position string (e.g. a ternary arm).
            prior = i - 1
            while prior >= 0 and block[prior] in " \t\r\n":
                prior -= 1
            is_key_position = prior < 0 or block[prior] in "{,"
            end = _skip_string(block, i)
            word = block[i + 1 : end - 1] if end > i + 1 else ""
            k = _skip_ws_comments(block, end)
            if is_key_position and word == key and k < n and block[k] == ":":
                return _value_text(block, k + 1)
            i = end
            continue
        if _at_string_or_comment(block, i):
            i = _skip_string(block, i) if block[i] in "\"'`" else _skip_comment(block, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif depth == 0 and (c.isalpha() or c in "_$"):
            j = i
            while j < n and (block[j].isalnum() or block[j] in "_$"):
                j += 1
            word = block[i:j]
            k = _skip_ws_comments(block, j)
            # Bare identifiers follow the same key-position rule as quoted
            # strings: an identifier is a KEY only when the nearest preceding
            # non-whitespace character opens the block or follows a comma.
            # Otherwise it is value text (``note: seq``, ``row.id``, a
            # ternary arm) and must never satisfy the key lookup.
            prior = i - 1
            while prior >= 0 and block[prior] in " \t\r\n":
                prior -= 1
            is_key_position = prior < 0 or block[prior] in "{,"
            if word == key and is_key_position and k < n and block[k] == ":":
                return _value_text(block, k + 1)
            if (
                word == key
                and is_key_position
                and (k >= n or block[k] in ",}")
            ):
                # Shorthand property ({ id }) at key position — the key
                # exists and its value is the identifier itself. k >= n is a
                # shorthand that ends the block (the closing brace is not
                # part of the span).
                return word
            i = j
            continue
            i = j
            continue
        i += 1
    return None


def _has_contract_seq(block: str) -> bool:
    """True when ``seq:`` exists at top level with a numeric-at-runtime value."""
    value = _top_level_key_value(block, "seq")
    if not value:
        return False
    if value[0] in "\"'`":
        return False  # stringly-typed seq ("1" is not the promised number)
    return value.lower() not in _SEQ_NON_NUMERIC


def scan_source(source: str) -> tuple[bool, bool]:
    """Scan one spec file's source.

    Returns ``(found_any_result, any_row_missing_seq)``. Follows each
    ``addChatMessagesBatch`` occurrence through its ``.method(...)`` call
    chain so chained installs (``.mockReset().mockResolvedValue([...])``)
    are seen; property chains (``.mock.calls[0]``) end the walk. Occurrences
    inside comments or string/template literals are ignored.
    """
    found_any = False
    missing = False
    n = len(source)
    pos = 0
    while True:
        occ = _find_code_occurrence(source, MOCK_TARGET, pos)
        if occ < 0:
            break
        j = occ + len(MOCK_TARGET)
        while True:
            m = _CHAIN_STEP.match(source, j)
            if not m:
                break
            method = m.group(1)
            k = m.end()
            while k < n and source[k] in " \t\r\n":
                k += 1
            if k >= n or source[k] != "(":
                break  # property access (e.g. .mock.calls) — chain ends
            end = _read_balanced(source, k, "(", ")")
            if end is None:
                j = n
                break
            if method in RESULT_SETTERS:
                found_any = True
                args = source[k + 1 : end - 1]
                literals = _object_literals(args)
                if literals:
                    for block in literals:
                        if _top_level_key_value(
                            block, "id"
                        ) is not None and not _has_contract_seq(block):
                            missing = True
                else:
                    bare = _blank_comments(args).strip()
                    if not bare or bare in ("undefined", "null"):
                        # Installs nothing contract-accurate (no argument, or
                        # an explicit undefined/null result).
                        missing = True
            j = end
        pos = occ + len(MOCK_TARGET)
    return found_any, missing


def find_offenders(repo_root: Path) -> list[str]:
    """Target files whose batch mocks drop seq (or have no batch mock)."""
    offenders: list[str] = []
    for rel in TARGETS:
        path = repo_root / rel
        if not path.is_file():
            # A renamed/deleted target is itself drift the gate must catch.
            offenders.append(rel)
            continue
        source = path.read_text(encoding="utf-8")
        found_any, missing = scan_source(source)
        if not found_any or missing:
            offenders.append(rel)
    return offenders


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    offenders = find_offenders(repo_root)
    if offenders:
        for rel in offenders:
            print(f"MISSING seq: {rel}")
        return 1
    print("OK: batch mocks carry seq")
    return 0


if __name__ == "__main__":
    sys.exit(main())
