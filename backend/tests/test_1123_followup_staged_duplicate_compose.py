"""Executable entrypoint for issue #1131 AC2.

The frozen check C9 (``test_982_1131_c9_staged_abort_duplicate_ledger.py``)
is the canonical, anchor-bound implementation of this acceptance criterion.
This module exists so the issue's literal argv names a runnable test file:
it re-exports that discriminating check at a stable, issue-named path.
"""

from test_982_1131_c9_staged_abort_duplicate_ledger import (  # noqa: F401
    test_staged_abort_composes_duplicate_ledger as test_staged_duplicate_compose,
)
