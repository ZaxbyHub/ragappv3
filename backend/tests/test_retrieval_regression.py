"""Retrieval regression test harness with synthetic fixtures.

Deterministic, portable, no dependency on real user data.
Committable to version control.
"""

import hashlib
import os
import sys
from typing import Dict, List, Optional

import numpy as np
import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub missing optional dependencies (same pattern as test_rag_engine.py)
try:
    import lancedb
except ImportError:
    import types

    sys.modules["lancedb"] = types.ModuleType("lancedb")

try:
    import pyarrow
except ImportError:
    import types

    sys.modules["pyarrow"] = types.ModuleType("pyarrow")

try:
    from unstructured.partition.auto import partition
except ImportError:
    import types

    _unstructured = types.ModuleType("unstructured")
    _unstructured.partition = types.ModuleType("unstructured.partition")
    _unstructured.partition.auto = types.ModuleType("unstructured.partition.auto")
    _unstructured.partition.auto.partition = lambda *args, **kwargs: []
    _unstructured.chunking = types.ModuleType("unstructured.chunking")
    _unstructured.chunking.title = types.ModuleType("unstructured.chunking.title")
    _unstructured.chunking.title.chunk_by_title = lambda *args, **kwargs: []
    _unstructured.documents = types.ModuleType("unstructured.documents")
    _unstructured.documents.elements = types.ModuleType(
        "unstructured.documents.elements"
    )
    _unstructured.documents.elements.Element = type("Element", (), {})
    sys.modules["unstructured"] = _unstructured
    sys.modules["unstructured.partition"] = _unstructured.partition
    sys.modules["unstructured.partition.auto"] = _unstructured.partition.auto
    sys.modules["unstructured.chunking"] = _unstructured.chunking
    sys.modules["unstructured.chunking.title"] = _unstructured.chunking.title
    sys.modules["unstructured.documents"] = _unstructured.documents
    sys.modules["unstructured.documents.elements"] = _unstructured.documents.elements

# ============================================================
# SYNTHETIC FIXTURES
# ============================================================

# 10 short documents with known, distinct content
SYNTHETIC_DOCUMENTS = [
    {
        "id": "doc-001",
        "content": "The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris, France. It was named after engineer Gustave Eiffel. The tower is 330 metres tall.",
    },
    {
        "id": "doc-002",
        "content": "The Great Wall of China is a series of fortifications in northern China. It was built across centuries starting from the 7th century BC. Its total length is over 21,000 kilometres.",
    },
    {
        "id": "doc-003",
        "content": "Machine learning is a branch of artificial intelligence that focuses on building systems that learn from data. Common algorithms include decision trees, neural networks, and support vector machines.",
    },
    {
        "id": "doc-004",
        "content": "Photosynthesis is the process used by plants to convert light energy into chemical energy. It takes place primarily in the leaves using chlorophyll. The equation is 6CO2 + 6H2O + light → C6H12O6 + 6O2.",
    },
    {
        "id": "doc-005",
        "content": "The Python programming language was created by Guido van Rossum and first released in 1991. It emphasizes code readability with significant whitespace. Python supports multiple paradigms including object-oriented and functional programming.",
    },
    {
        "id": "doc-006",
        "content": "DNA or deoxyribonucleic acid is the hereditary material in humans and almost all other organisms. The DNA molecule consists of two strands that wind around each other forming a double helix.",
    },
    {
        "id": "doc-007",
        "content": "The theory of relativity was developed by Albert Einstein. It consists of special relativity published in 1905 and general relativity published in 1915. The famous equation E=mc² comes from special relativity.",
    },
    {
        "id": "doc-008",
        "content": "Cloud computing is the on-demand availability of computer system resources including data storage and computing power. Major providers include Amazon Web Services, Microsoft Azure, and Google Cloud Platform.",
    },
    {
        "id": "doc-009",
        "content": "The Amazon River in South America is the largest river by discharge volume of water in the world. It flows through Brazil, Colombia, and Peru. The Amazon rainforest is the largest tropical rainforest.",
    },
    {
        "id": "doc-010",
        "content": "Quantum computing uses quantum mechanics phenomena such as superposition and entanglement. Unlike classical computers that use bits, quantum computers use qubits. Companies like IBM and Google are developing quantum processors.",
    },
]

# Queries with expected relevant document IDs
SYNTHETIC_QUERIES = [
    {
        "query": "What is the height of the Eiffel Tower?",
        "expected_doc_ids": ["doc-001"],
        "min_recall": 1.0,  # Must find the exact document
    },
    {
        "query": "How long is the Great Wall of China?",
        "expected_doc_ids": ["doc-002"],
        "min_recall": 1.0,
    },
    {
        "query": "Who created Python programming language?",
        "expected_doc_ids": ["doc-005"],
        "min_recall": 1.0,
    },
    {
        "query": "What is the chemical equation for photosynthesis?",
        "expected_doc_ids": ["doc-004"],
        "min_recall": 1.0,
    },
    {
        "query": "Explain DNA structure",
        "expected_doc_ids": ["doc-006"],
        "min_recall": 1.0,
    },
    {
        "query": "What equation did Einstein formulate?",
        "expected_doc_ids": ["doc-007"],
        "min_recall": 1.0,
    },
    {
        "query": "What are major cloud computing providers?",
        "expected_doc_ids": ["doc-008"],
        "min_recall": 1.0,
    },
    {
        "query": "Tell me about quantum computing and qubits",
        "expected_doc_ids": ["doc-010"],
        "min_recall": 1.0,
    },
    {
        "query": "What is the largest river in the world by water volume?",
        "expected_doc_ids": ["doc-009"],
        "min_recall": 1.0,
    },
    {
        "query": "What is machine learning?",
        "expected_doc_ids": ["doc-003"],
        "min_recall": 1.0,
    },
]


# ============================================================
# FAKE SERVICES
# ============================================================


class DeterministicEmbeddingService:
    """Generates deterministic embeddings based on text content for reproducibility.

    Uses a bag-of-words approach with keyword hashing to ensure semantically
    similar texts have similar embeddings, enabling meaningful retrieval tests.
    """

    EMBEDDING_DIM = 768

    # Keywords associated with each document for semantic clustering
    DOC_KEYWORDS = {
        "doc-001": [
            "eiffel",
            "tower",
            "paris",
            "france",
            "gustave",
            "metres",
            "tall",
            "height",
        ],
        "doc-002": [
            "great",
            "wall",
            "china",
            "fortifications",
            "kilometres",
            "length",
            "long",
        ],
        "doc-003": [
            "machine",
            "learning",
            "artificial",
            "intelligence",
            "algorithms",
            "neural",
        ],
        "doc-004": [
            "photosynthesis",
            "plants",
            "chlorophyll",
            "chemical",
            "equation",
            "energy",
        ],
        "doc-005": ["python", "programming", "guido", "rossum", "language", "code"],
        "doc-006": [
            "dna",
            "deoxyribonucleic",
            "hereditary",
            "organisms",
            "helix",
            "strands",
        ],
        "doc-007": [
            "einstein",
            "relativity",
            "theory",
            "special",
            "general",
            "equation",
        ],
        "doc-008": [
            "cloud",
            "computing",
            "amazon",
            "aws",
            "azure",
            "google",
            "providers",
        ],
        "doc-009": ["amazon", "river", "brazil", "water", "rainforest", "largest"],
        "doc-010": [
            "quantum",
            "computing",
            "qubits",
            "superposition",
            "entanglement",
            "ibm",
        ],
    }

    async def embed_single(self, text: str) -> List[float]:
        return self._embed(text)

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [self._embed(t) for t in texts]

    def _embed(self, text: str) -> List[float]:
        # Use bag-of-words approach with keyword-based embedding
        # This ensures semantically similar texts cluster together
        text_lower = text.lower()
        set(text_lower.split())

        # Create a base vector from the text hash for uniqueness.
        # hashlib (NOT hash()) — built-in hash() is randomized per process by
        # PYTHONHASHSEED, which made these "deterministic" embeddings and the
        # corpus rank assertions flaky across CI runs (observed on PR #600:
        # test_no_false_positives_high_recall failed then passed on a same-sha
        # rerun with no diff change).
        seed = int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16) % (2**31)
        rng = np.random.RandomState(seed)
        vec = (
            rng.randn(self.EMBEDDING_DIM).astype(np.float32) * 0.1
        )  # Small random component

        # Add keyword-based components to ensure semantic clustering
        for doc_id, keywords in self.DOC_KEYWORDS.items():
            # Check how many keywords from this doc appear in the text
            keyword_matches = sum(1 for kw in keywords if kw in text_lower)
            if keyword_matches > 0:
                # Use doc_id hash to create a consistent vector direction for this doc
                doc_seed = int(hashlib.md5(doc_id.encode("utf-8")).hexdigest(), 16) % (2**31)
                doc_rng = np.random.RandomState(doc_seed)
                doc_vec = doc_rng.randn(self.EMBEDDING_DIM).astype(np.float32)
                doc_vec /= np.linalg.norm(doc_vec)
                # Add weighted contribution based on keyword matches
                vec += doc_vec * (keyword_matches * 0.5)

        # Normalize to unit vector
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec.tolist()


class InMemoryVectorStore:
    """Simple in-memory vector store for regression testing."""

    def __init__(self):
        self._documents: Dict[str, List[float]] = {}
        self._metadata: Dict[str, dict] = {}

    async def add_document(
        self,
        doc_id: str,
        content: str,
        embedding: List[float],
        metadata: Optional[dict] = None,
    ):
        self._documents[doc_id] = embedding
        self._metadata[doc_id] = metadata or {}

    async def search(self, query_embedding: List[float], top_k: int = 5) -> List[dict]:
        """Search by cosine similarity, return sorted results."""
        if not self._documents:
            return []

        query_vec = np.array(query_embedding)
        results = []
        for doc_id, doc_vec in self._documents.items():
            doc_vec_arr = np.array(doc_vec)
            similarity = float(
                np.dot(query_vec, doc_vec_arr)
                / (np.linalg.norm(query_vec) * np.linalg.norm(doc_vec_arr) + 1e-10)
            )
            results.append(
                {
                    "id": doc_id,
                    "content": self._metadata.get(doc_id, {}).get("content", ""),
                    "score": similarity,
                    "distance": 1.0 - similarity,  # cosine distance
                }
            )

        results.sort(key=lambda x: x["distance"])
        return results[:top_k]


# ============================================================
# RECALL COMPUTATION
# ============================================================


def compute_recall(retrieved_doc_ids: List[str], expected_doc_ids: List[str]) -> float:
    """Compute recall: fraction of expected docs found in retrieved results."""
    if not expected_doc_ids:
        return 1.0
    found = sum(1 for eid in expected_doc_ids if eid in retrieved_doc_ids)
    return found / len(expected_doc_ids)


# ============================================================
# FIXTURES
# ============================================================


@pytest.fixture
def embedding_service():
    """Fixture providing a deterministic embedding service."""
    return DeterministicEmbeddingService()


@pytest.fixture
def vector_store():
    """Fixture providing an empty in-memory vector store."""
    return InMemoryVectorStore()


@pytest.fixture
async def populated_store(embedding_service, vector_store):
    """Fixture providing a vector store populated with synthetic documents."""
    for doc in SYNTHETIC_DOCUMENTS:
        embedding = await embedding_service.embed_single(doc["content"])
        await vector_store.add_document(
            doc_id=doc["id"],
            content=doc["content"],
            embedding=embedding,
            metadata={"content": doc["content"]},
        )
    return vector_store


# ============================================================
# TESTS
# ============================================================


@pytest.mark.asyncio
async def test_synthetic_fixtures_are_deterministic(embedding_service):
    """Test that embedding the same text twice produces identical vectors."""
    text = "The quick brown fox jumps over the lazy dog."

    vec1 = await embedding_service.embed_single(text)
    vec2 = await embedding_service.embed_single(text)

    assert vec1 == vec2, "Embeddings for the same text should be deterministic"


def _md5_of_dna_embedding() -> str:
    """Embed the DNA query and return an md5 digest of the vector (helper for
    the cross-process stability test below). Constructs the service directly
    (the embedding_service fixture cannot be called outside a test)."""
    import asyncio
    import json

    async def _embed() -> List[float]:
        return await DeterministicEmbeddingService().embed_single(
            "Explain DNA structure"
        )

    vec = asyncio.run(_embed())
    return hashlib.md5(json.dumps(vec).encode("utf-8")).hexdigest()


def test_embeddings_stable_across_python_hash_seeds():
    """Cross-process guard (PR #602 review): embeddings must NOT depend on
    PYTHONHASHSEED. The within-process determinism test above cannot catch a
    regression to built-in hash() seeding (it compares two vectors from the
    SAME process, where hash() is still self-consistent), which is exactly how
    the original flake survived — rank assertions only flipped across CI runs.
    Spawn subprocesses under two different forced hash seeds and require the
    digest to match the in-process one; a hash()-seed regression fails here."""
    import json
    import subprocess
    import sys

    child_code = (
        "import sys, json, hashlib\n"
        "sys.path.insert(0, '.')\n"
        "import types\n"
        "for _m in ('lancedb', 'pyarrow'):\n"
        "    try:\n"
        "        __import__(_m)\n"
        "    except ImportError:\n"
        "        sys.modules[_m] = types.ModuleType(_m)\n"
        "import asyncio\n"
        "from test_retrieval_regression import DeterministicEmbeddingService\n"
        "svc = DeterministicEmbeddingService()\n"
        "vec = asyncio.run(svc.embed_single('Explain DNA structure'))\n"
        "print(hashlib.md5(json.dumps(vec).encode('utf-8')).hexdigest())\n"
    )
    digests = set()
    for forced_seed in ("0", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=forced_seed)
        out = subprocess.run(
            [sys.executable, "-c", child_code],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        assert out.returncode == 0, (
            f"subprocess (PYTHONHASHSEED={forced_seed}) failed: {out.stderr[-500:]}"
        )
        digests.add(out.stdout.strip())
    digests.add(_md5_of_dna_embedding())
    assert len(digests) == 1, (
        "Embeddings differ across PYTHONHASHSEED values — seeding regressed to "
        f"process-randomized hash(): {digests}"
    )


@pytest.mark.asyncio
async def test_embedding_dimension_consistency(embedding_service):
    """Test that all embeddings have the expected dimension."""
    texts = [
        "Short text.",
        "This is a much longer text that contains more words and should still produce the same dimension embedding.",
        "a",
    ]

    for text in texts:
        embedding = await embedding_service.embed_single(text)
        assert len(embedding) == DeterministicEmbeddingService.EMBEDDING_DIM, (
            f"Embedding dimension should be {DeterministicEmbeddingService.EMBEDDING_DIM}"
        )


@pytest.mark.asyncio
async def test_recall_each_query_individually(embedding_service, vector_store):
    """Test that each query retrieves its expected document(s) with recall >= min_recall."""
    # Index all documents
    for doc in SYNTHETIC_DOCUMENTS:
        embedding = await embedding_service.embed_single(doc["content"])
        await vector_store.add_document(
            doc_id=doc["id"],
            content=doc["content"],
            embedding=embedding,
            metadata={"content": doc["content"]},
        )

    # Test each query
    failures = []
    for test_case in SYNTHETIC_QUERIES:
        query = test_case["query"]
        expected_ids = test_case["expected_doc_ids"]
        min_recall = test_case["min_recall"]

        # Embed query and search
        query_embedding = await embedding_service.embed_single(query)
        results = await vector_store.search(query_embedding, top_k=5)
        retrieved_ids = [r["id"] for r in results]

        recall = compute_recall(retrieved_ids, expected_ids)

        if recall < min_recall:
            failures.append(
                f"Query '{query[:50]}...': recall={recall:.2f} < min_recall={min_recall}, "
                f"expected={expected_ids}, retrieved={retrieved_ids}"
            )

    assert not failures, "Some queries failed recall threshold:\n" + "\n".join(failures)


@pytest.mark.asyncio
async def test_recall_aggregate(embedding_service, vector_store):
    """Test that average recall across all queries meets default threshold (0.80)."""
    DEFAULT_RECALL_THRESHOLD = 0.80

    # Index all documents
    for doc in SYNTHETIC_DOCUMENTS:
        embedding = await embedding_service.embed_single(doc["content"])
        await vector_store.add_document(
            doc_id=doc["id"],
            content=doc["content"],
            embedding=embedding,
            metadata={"content": doc["content"]},
        )

    # Compute recall for each query
    recalls = []
    for test_case in SYNTHETIC_QUERIES:
        query = test_case["query"]
        expected_ids = test_case["expected_doc_ids"]

        query_embedding = await embedding_service.embed_single(query)
        results = await vector_store.search(query_embedding, top_k=5)
        retrieved_ids = [r["id"] for r in results]

        recall = compute_recall(retrieved_ids, expected_ids)
        recalls.append(recall)

    avg_recall = sum(recalls) / len(recalls)

    assert avg_recall >= DEFAULT_RECALL_THRESHOLD, (
        f"Average recall {avg_recall:.2f} is below threshold {DEFAULT_RECALL_THRESHOLD}"
    )


@pytest.mark.asyncio
async def test_empty_store_returns_empty(embedding_service, vector_store):
    """Test that searching an empty store returns an empty list."""
    query_embedding = await embedding_service.embed_single("test query")
    results = await vector_store.search(query_embedding, top_k=5)

    assert results == [], "Empty store should return empty results"


@pytest.mark.asyncio
async def test_top_k_respected(embedding_service, vector_store):
    """Test that search with top_k=3 returns at most 3 results."""
    # Index all documents
    for doc in SYNTHETIC_DOCUMENTS:
        embedding = await embedding_service.embed_single(doc["content"])
        await vector_store.add_document(
            doc_id=doc["id"],
            content=doc["content"],
            embedding=embedding,
            metadata={"content": doc["content"]},
        )

    query_embedding = await embedding_service.embed_single("test query")
    results = await vector_store.search(query_embedding, top_k=3)

    assert len(results) <= 3, f"Expected at most 3 results, got {len(results)}"


@pytest.mark.asyncio
async def test_no_false_positives_high_recall(embedding_service, vector_store):
    """Test that expected documents appear in top-3 results."""
    # Index all documents
    for doc in SYNTHETIC_DOCUMENTS:
        embedding = await embedding_service.embed_single(doc["content"])
        await vector_store.add_document(
            doc_id=doc["id"],
            content=doc["content"],
            embedding=embedding,
            metadata={"content": doc["content"]},
        )

    failures = []
    for test_case in SYNTHETIC_QUERIES:
        query = test_case["query"]
        expected_ids = test_case["expected_doc_ids"]

        query_embedding = await embedding_service.embed_single(query)
        results = await vector_store.search(query_embedding, top_k=3)
        retrieved_ids = [r["id"] for r in results]

        # Check that all expected docs are in top-3
        missing = [eid for eid in expected_ids if eid not in retrieved_ids]
        if missing:
            failures.append(
                f"Query '{query[:50]}...': expected {expected_ids} in top-3, "
                f"but missing {missing}, got {retrieved_ids}"
            )

    assert not failures, "Some expected documents not in top-3:\n" + "\n".join(failures)
