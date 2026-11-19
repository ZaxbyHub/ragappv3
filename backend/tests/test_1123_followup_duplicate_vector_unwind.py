"""Executable entrypoint for issue #1131 AC1.

The frozen check C8 (``test_982_1131_c8_unwind_duplicate_loser.py``) is the
canonical, anchor-bound implementation of this acceptance criterion. This
module exists so the issue's literal argv names a runnable test file: it
re-exports that discriminating check at a stable, issue-named path.
"""

from test_982_1131_c8_unwind_duplicate_loser import (  # noqa: F401
    test_finalization_race_loser_generation_unwound as test_duplicate_vector_unwind,
)
