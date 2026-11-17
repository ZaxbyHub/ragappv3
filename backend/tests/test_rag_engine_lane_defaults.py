"""2a: a properly constructed engine never relies on class-level defaults."""
from unittest.mock import MagicMock

from app.services.rag_engine import RAGEngine


def _engine(**kwargs):
    kwargs.setdefault("embedding_service", MagicMock())
    kwargs.setdefault("vector_store", MagicMock())
    kwargs.setdefault("memory_store", MagicMock())
    kwargs.setdefault("llm_client", MagicMock(name="llm_client"))
    kwargs.setdefault("reranking_service", None)
    return RAGEngine(**kwargs)


def test_constructed_engine_has_no_class_level_lane_defaults():
    for attr in ("llm_client", "_thinking_client_override",
                 "_instant_client_override"):
        assert attr not in vars(RAGEngine), (
            f"RAGEngine.{attr} is a class-level default; it hides "
            "initialisation faults behind None"
        )


def test_constructed_engine_resolves_every_lane():
    engine = _engine()
    assert "llm_client" in vars(engine)
    assert "_thinking_client_override" in vars(engine)
    assert "_instant_client_override" in vars(engine)
    assert engine.thinking_client is engine.llm_client
    assert engine.instant_client is engine.llm_client
    assert engine.aux_llm_client is engine.llm_client


def test_constructed_engine_honours_overrides():
    thinking = MagicMock(name="thinking")
    instant = MagicMock(name="instant")
    engine = _engine(thinking_client=thinking, instant_client=instant)
    assert engine.thinking_client is thinking
    assert engine.instant_client is instant
    # the aux lane deliberately rides the instant lane (issue #712)
    assert engine.aux_llm_client is instant
    assert engine.aux_llm_client is not engine.llm_client
