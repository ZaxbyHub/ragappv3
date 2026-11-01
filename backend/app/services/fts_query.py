"""Shared FTS5 MATCH-query tokenizer for store-level search (issue #515).

User-typed search strings must never reach the FTS5 query parser as raw
syntax: ordinary input like ``Model-X`` (hyphen = FTS5 column-filter syntax),
``O"Brien`` (unbalanced quote) or ``title:secret`` (column qualifier) raises
``sqlite3.OperationalError`` when passed straight to ``MATCH`` — raw through
WikiStore, silently swallowed to ``[]`` by KMSStore. This module turns any
string into a safe prefix-match query instead.

The recipe mirrors ``kms_retrieval.build_kms_fts_query`` (proven in production
and pinned by ``test_kms_retrieval.py``):

1. lowercase the input;
2. hyphens become spaces (FTS5 reads ``-`` as an operator, and the default
   unicode61 tokenizer already splits indexed text on hyphens, so ``my-doc``
   still matches via the ``my*`` / ``doc*`` prefix tokens);
3. tokenize with a Unicode ``\\w+`` regex so CJK and accented letters survive
   (an ASCII-only tokenizer silently discarded them);
4. emit one ``token*`` prefix term per token, joined by spaces (implicit AND);
5. cap the token count to bound the MATCH expression.

Punctuation-only ASCII input tokenizes to ``""`` — callers treat that as "no
searchable tokens" and skip the FTS leg entirely.

This module is importable standalone (stdlib only): route-level helpers such
as ``documents._build_files_fts_query`` and ``kms_retrieval.build_kms_fts_query``
re-point here for tokenizer convergence without importing each other's modules.
"""

import re

# Unicode word characters: letters/digits/underscore in any script, so CJK
# queries tokenize (and prefix-match) instead of being discarded as
# "punctuation". ``re.UNICODE`` is the default for str patterns; spelled out
# here because the property is load-bearing for this helper.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# Mirror build_kms_fts_query's proven cap: 8 prefix tokens is plenty for a
# search-box query and keeps the MATCH expression cheap.
MAX_TOKENS = 8

# Generous ceiling on the FTS candidate id list handed to a store search
# (SEARCH-003, issue #515). It exists ONLY to bound the SQL ``IN (...)``
# parameter list fed to the post-FTS query; visible filtering, ordering, and
# the result cap are always decided by that SQL over the candidate set —
# never by this number.
FTS_CANDIDATE_CAP = 500

# Ceiling on the column size an FTS5 ``snippet()``/``highlight()`` auxiliary
# call may be evaluated against (issue #1002). Measured pathology on densely
# matching bodies (repro probe, sqlite 3.45.1): snippet() cost is super-linear
# in BOTH column size and match density — 0.4 s @ 200K chars (probe density),
# 9.8 s @ 1M, >30 s @ 4M, >90 s (killed) @ 43M; at the ceiling's own size a
# single all-matching row measured 1.4–3.3 s on dev boxes, so a worst-case
# 8-row chat query is ~11–26 s at adversarial density while realistic
# spreadsheet density (~1/6 of tokens matching) stays sub-second. MATCH + rank
# alone stays fast at every size (0.09 s @ 43M).
#
# The guard measures BYTES via ``length(CAST(col AS BLOB))`` — byte length is
# the NUL-proof measure (plain ``length()`` stops at the first NUL byte, which
# let a crafted NUL-prefixed body bypass the guard entirely — review finding
# F-001, probe: 504 s on a 2M-char NUL body under the old shape), and the same
# numeric ceiling is *stricter* for multibyte text than the ASCII calibration.
# SQLite's lazily-evaluated CASE skips the auxiliary call for over-ceiling
# columns, and the caller falls back to a bounded excerpt. 131072 stays inside
# the measured budget envelope at realistic densities (pin: 0 < value <= 200000;
# 8 rows just under 200K ≈ 3.2 s < the 5 s check budget); anything ≥ ~1M
# recreates the production wedge where a single matching chat query pinned a
# worker thread indefinitely.
FTS_SNIPPET_MAX_COLUMN_BYTES = 131_072


def build_fts_match_query(raw_search: str, max_tokens: int = MAX_TOKENS) -> str:
    """Sanitize ``raw_search`` into a safe FTS5 prefix-match MATCH string.

    Returns ``""`` when no usable token remains (punctuation-only ASCII
    input); callers then skip the FTS search leg rather than matching on
    raw syntax.
    """
    normalized = (raw_search or "").lower().replace("-", " ")
    tokens = _TOKEN_RE.findall(normalized)
    return " ".join(f"{token}*" for token in tokens[:max_tokens])
