"""Compare normal LanceDB vector search with flat-scan bypass results.

Usage:
    python scripts/vector_search_bypass_index_diagnostic.py \
        --embedding-json "[0.1, 0.2, 0.3]" --limit 10

The script does not generate embeddings. Pass the exact query embedding you
want to diagnose as JSON or via a file containing a JSON list of floats.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# ``app.*`` only resolves with backend/ on the path; the documented
# repo-root invocation puts only backend/scripts on sys.path (issue
# #727 / T1-28-K-01).
sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.services.vector_store import VectorStore


def _load_embedding(args: argparse.Namespace) -> list[float]:
    if args.embedding_json:
        raw: Any = json.loads(args.embedding_json)
    else:
        raw = json.loads(Path(args.embedding_file).read_text(encoding="utf-8"))

    if not isinstance(raw, list) or not raw:
        raise ValueError("embedding must be a non-empty JSON list")

    try:
        return [float(value) for value in raw]
    except (TypeError, ValueError) as exc:
        raise ValueError("embedding values must be numeric") from exc


async def _run(args: argparse.Namespace) -> None:
    db_path = Path(args.db_path) if args.db_path else None
    store = VectorStore(db_path=db_path)
    embedding = _load_embedding(args)
    diagnostic = await store.vector_search_bypass_index_diagnostic(
        embedding=embedding,
        limit=args.limit,
        filter_expr=args.filter,
        vault_id=args.vault_id,
    )
    print(json.dumps(diagnostic, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare normal ANN search with LanceDB bypass_vector_index search."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--embedding-json", help="Query embedding as a JSON list")
    source.add_argument("--embedding-file", help="Path to a JSON list of floats")
    parser.add_argument("--db-path", help="Optional LanceDB path override")
    parser.add_argument("--vault-id", help="Optional vault_id filter")
    parser.add_argument("--filter", help="Optional LanceDB filter expression")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be >= 1")

    try:
        asyncio.run(_run(args))
    except Exception as exc:
        print(f"diagnostic failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
