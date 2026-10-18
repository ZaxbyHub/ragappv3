"""Acceptance check for issue #715 finding TQ-instruction-batch-4-14 (C10).

Desired post-fix behavior (RED on current master):

RetrievalTool.description must document every keyword argument that
RetrievalTool.execute accepts, including ``top_k``.

Today the description ends with
``"Arguments: query (str), vault_id (int | None, optional)."`` while
``execute`` also accepts ``top_k`` (agentic_tools.py) — an LLM planner
reading the description cannot discover the top_k control.
"""

import inspect

from app.services.agentic_tools import RetrievalTool


def test_retrieval_description_names_top_k():
    tool = RetrievalTool(retrieval_top_k=5)
    found = "top_k" in tool.description
    # E712 suppressed: the acceptance-check red signature is
    # ``AssertionError: assert False == True``.
    assert found == True  # noqa: E712


def test_retrieval_description_names_every_execute_kwarg():
    """Stronger companion check: every NAMED parameter of execute() (self and
    the ``**_`` catch-all excluded) must appear verbatim in the description,
    so the planner LLM sees the full call surface of the tool."""
    tool = RetrievalTool(retrieval_top_k=5)
    sig = inspect.signature(RetrievalTool.execute)
    named_params = [
        p.name
        for p in sig.parameters.values()
        if p.name != "self" and p.kind is not inspect.Parameter.VAR_KEYWORD
    ]
    missing = [name for name in named_params if name not in tool.description]
    assert missing == []
