"""Aux-lane routing for the retrieval-quality services .

Contract: QueryTransformer, QueryPlanner, and RetrievalEvaluator are bound to
the engine's auxiliary client — the instant lane — because the thinking
lane's model pre-opens a think block on every generation and starves
these short machine-shaped calls into empty content (issue #712's failure).
The thinking lane itself stays intact for user-facing answers.
"""

from app.services import rag_engine as rag_engine_module
from app.services.query_transformer import QueryPlanner, QueryTransformer
from app.services.retrieval_evaluator import RetrievalEvaluator


class _StubClient:
    def __init__(self, name: str):
        self.model = name
        self.name = name

    def __repr__(self):
        return f"_StubClient({self.name})"


class _StubService:
    """Stand-in for a default-constructing service the engine would build.

    ``EmbeddingService()`` / ``MemoryStore()`` default construction parses the
    configured endpoint host and is environment-dependent; the aux-lane
    contract under test never touches these.
    """


def _engine():
    return rag_engine_module.RAGEngine(
        embedding_service=_StubService(),
        memory_store=_StubService(),
        llm_client=_StubClient("thinking-lane"),
        instant_client=_StubClient("instant-lane"),
    )


def test_aux_client_is_the_instant_lane():
    engine = _engine()
    assert engine.aux_llm_client is engine.instant_client
    assert engine.aux_llm_client.model == "instant-lane"
    # The thinking lane itself is untouched.
    assert engine.thinking_client.model == "thinking-lane"


def test_aux_services_bind_to_the_instant_client():
    engine = _engine()
    transformer = engine._get_query_transformer(engine.aux_llm_client)
    planner = engine._get_query_planner(engine.aux_llm_client)
    evaluator = RetrievalEvaluator(engine.aux_llm_client)
    assert isinstance(transformer, QueryTransformer)
    assert isinstance(planner, QueryPlanner)
    assert transformer._llm_client is engine.instant_client
    assert planner._llm_client is engine.instant_client
    assert evaluator._llm_client is engine.instant_client
