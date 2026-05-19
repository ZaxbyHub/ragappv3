"""Reconcile indexed SQLite files with LanceDB chunk rows.

The script is safe by default: it reports corpus disagreements and only deletes
LanceDB rows when a deletion flag is paired with --confirm.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lancedb
from lancedb.index import IvfPq

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHUNK_SCALE = "default"
LANCEDB_RECONCILE_COLUMNS = ("id", "file_id", "vault_id", "chunk_scale")
LANCEDB_SCAN_BATCH_SIZE = 10_000
VECTOR_INDEX_MIN_ROWS = 256
DEFAULT_EMBEDDING_DIM = 1024
DEFAULT_VECTOR_METRIC = "cosine"


def _lance_escape(value: Any) -> str:
    return str(value).replace("'", "''")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _default_data_dir() -> Path:
    raw = os.getenv("DATA_DIR")
    return Path(raw) if raw else BACKEND_ROOT / "data"


@dataclass(frozen=True)
class IndexedFile:
    file_id: str
    vault_id: str
    file_name: str
    chunk_count: int


@dataclass(frozen=True)
class ChunkRow:
    id: str
    file_id: str
    vault_id: str
    chunk_scale: str


@dataclass(frozen=True)
class CleanupPlan:
    orphan_file_ids: tuple[str, ...]
    orphan_vault_id: str | None
    stale_multiscale: bool


def _as_count_dict(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items(), key=lambda item: item[0]))


def _stringify(value: Any) -> str:
    return "" if value is None else str(value)


def _load_indexed_files(sqlite_path: Path) -> dict[str, IndexedFile]:
    if not sqlite_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {sqlite_path}")

    with sqlite3.connect(sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, vault_id, file_name, COALESCE(chunk_count, 0) AS chunk_count
            FROM files
            WHERE status = 'indexed'
            ORDER BY vault_id, id
            """
        ).fetchall()

    return {
        str(row["id"]): IndexedFile(
            file_id=str(row["id"]),
            vault_id=str(row["vault_id"]),
            file_name=str(row["file_name"]),
            chunk_count=int(row["chunk_count"] or 0),
        )
        for row in rows
    }


async def _open_chunks_table(lancedb_path: Path) -> Any | None:
    db = await lancedb.connect_async(str(lancedb_path))
    table_names = await db.table_names()
    if "chunks" not in table_names:
        return None
    return await db.open_table("chunks")


async def _load_lancedb_chunks(table: Any | None) -> list[ChunkRow]:
    if table is None:
        return []

    chunks: list[ChunkRow] = []
    if hasattr(table, "query"):
        query = table.query().select(list(LANCEDB_RECONCILE_COLUMNS))
        if hasattr(query, "to_batches"):
            reader = await query.to_batches(max_batch_length=LANCEDB_SCAN_BATCH_SIZE)
            async for batch in reader:
                chunks.extend(_chunk_rows_from_raw_rows(_raw_rows_from_arrowish(batch)))
            return chunks
        raw_table = await query.to_arrow()
    else:
        raw_table = await table.to_arrow()

    chunks.extend(_chunk_rows_from_raw_rows(_raw_rows_from_arrowish(raw_table)))
    return chunks


def _raw_rows_from_arrowish(raw_table: Any) -> list[Any]:
    if hasattr(raw_table, "to_pylist"):
        return raw_table.to_pylist()
    if hasattr(raw_table, "to_pandas"):
        return raw_table.to_pandas().to_dict(orient="records")
    return list(raw_table)


def _chunk_rows_from_raw_rows(raw_rows: list[Any]) -> list[ChunkRow]:
    chunks: list[ChunkRow] = []
    for row in raw_rows:
        chunk_scale = _stringify(row.get("chunk_scale")) or DEFAULT_CHUNK_SCALE
        chunks.append(
            ChunkRow(
                id=_stringify(row.get("id")),
                file_id=_stringify(row.get("file_id")),
                vault_id=_stringify(row.get("vault_id")),
                chunk_scale=chunk_scale,
            )
        )
    return chunks


def build_report(
    indexed_files: dict[str, IndexedFile],
    chunks: list[ChunkRow],
    *,
    multi_scale_indexing_enabled: bool,
) -> dict[str, Any]:
    sqlite_by_vault: Counter[str] = Counter(
        indexed_file.vault_id for indexed_file in indexed_files.values()
    )
    lancedb_by_vault: Counter[str] = Counter(chunk.vault_id for chunk in chunks)
    lancedb_by_file: Counter[str] = Counter(chunk.file_id for chunk in chunks)
    lancedb_by_scale: Counter[str] = Counter(chunk.chunk_scale for chunk in chunks)

    orphan_file_ids = sorted(
        file_id for file_id in lancedb_by_file if file_id not in indexed_files
    )
    orphan_vault_ids = {
        vault_id: lancedb_by_vault[vault_id]
        for vault_id in sorted(lancedb_by_vault)
        if vault_id not in sqlite_by_vault
    }
    indexed_files_missing_lancedb_rows = {
        file_id: {
            "vault_id": indexed_file.vault_id,
            "file_name": indexed_file.file_name,
            "sqlite_chunk_count": indexed_file.chunk_count,
        }
        for file_id, indexed_file in sorted(indexed_files.items())
        if indexed_file.chunk_count > 0 and lancedb_by_file.get(file_id, 0) == 0
    }

    mismatches_by_file: dict[str, dict[str, Any]] = {}
    for file_id, indexed_file in indexed_files.items():
        observed_vaults = sorted(
            {chunk.vault_id for chunk in chunks if chunk.file_id == file_id}
        )
        mismatched_vaults = [
            vault_id
            for vault_id in observed_vaults
            if vault_id != indexed_file.vault_id
        ]
        if mismatched_vaults:
            mismatches_by_file[file_id] = {
                "sqlite_vault_id": indexed_file.vault_id,
                "lancedb_vault_ids": observed_vaults,
                "mismatched_lancedb_vault_ids": mismatched_vaults,
                "row_count": sum(
                    1
                    for chunk in chunks
                    if chunk.file_id == file_id
                    and chunk.vault_id in mismatched_vaults
                ),
            }

    stale_multiscale_rows = [
        chunk
        for chunk in chunks
        if not multi_scale_indexing_enabled and chunk.chunk_scale != DEFAULT_CHUNK_SCALE
    ]
    stale_multiscale_by_scale: Counter[str] = Counter(
        chunk.chunk_scale for chunk in stale_multiscale_rows
    )

    return {
        "sqlite_indexed_files_by_vault_id": _as_count_dict(sqlite_by_vault),
        "lancedb_chunks_by_vault_id": _as_count_dict(lancedb_by_vault),
        "lancedb_chunks_by_file_id": _as_count_dict(lancedb_by_file),
        "lancedb_chunks_by_chunk_scale": _as_count_dict(lancedb_by_scale),
        "file_ids_present_in_lancedb_but_not_sqlite_indexed": orphan_file_ids,
        "vault_ids_present_in_lancedb_but_not_sqlite_indexed": orphan_vault_ids,
        "indexed_sqlite_files_missing_lancedb_rows": indexed_files_missing_lancedb_rows,
        "vault_id_mismatches_between_sqlite_files_and_lancedb_chunks": dict(
            sorted(mismatches_by_file.items(), key=lambda item: item[0])
        ),
        "stale_multiscale_rows_when_disabled": {
            "multi_scale_indexing_enabled": multi_scale_indexing_enabled,
            "row_count": len(stale_multiscale_rows),
            "by_chunk_scale": _as_count_dict(stale_multiscale_by_scale),
        },
        "totals": {
            "sqlite_indexed_files": len(indexed_files),
            "lancedb_chunks": len(chunks),
            "orphan_file_ids": len(orphan_file_ids),
            "orphan_vault_ids": len(orphan_vault_ids),
            "indexed_files_missing_lancedb_rows": len(
                indexed_files_missing_lancedb_rows
            ),
            "vault_mismatched_files": len(mismatches_by_file),
            "stale_multiscale_rows": len(stale_multiscale_rows),
        },
    }


def _delete_filter_for_file_ids(file_ids: list[str]) -> str:
    quoted = ", ".join(f"'{_lance_escape(file_id)}'" for file_id in file_ids)
    return f"file_id IN ({quoted})"


async def _delete_with_count(table: Any, filter_expr: str) -> tuple[int, int | None]:
    count = await table.count_rows(filter_expr)
    if count > 0:
        await table.delete(filter_expr)
    try:
        remaining = await table.count_rows(filter_expr)
    except Exception:
        remaining = None
    return int(count), None if remaining is None else int(remaining)


async def apply_cleanup(table: Any, plan: CleanupPlan) -> dict[str, dict[str, int | None]]:
    deleted: dict[str, int] = {}
    remaining: dict[str, int | None] = {}

    if plan.orphan_file_ids:
        filter_expr = _delete_filter_for_file_ids(list(plan.orphan_file_ids))
        deleted["orphan_file_id_rows"], remaining["orphan_file_id_rows"] = (
            await _delete_with_count(table, filter_expr)
        )

    if plan.orphan_vault_id is not None:
        safe_vault_id = _lance_escape(plan.orphan_vault_id)
        filter_expr = f"vault_id = '{safe_vault_id}'"
        deleted["orphan_vault_rows"], remaining["orphan_vault_rows"] = (
            await _delete_with_count(table, filter_expr)
        )

    if plan.stale_multiscale:
        filter_expr = f"chunk_scale != '{DEFAULT_CHUNK_SCALE}'"
        deleted["stale_multiscale_rows"], remaining["stale_multiscale_rows"] = (
            await _delete_with_count(table, filter_expr)
        )

    return {
        "deleted_counts": deleted,
        "remaining_after_delete_counts": remaining,
    }


async def optimize_after_cleanup(table: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"optimized": False, "index_action": "none"}

    if hasattr(table, "optimize"):
        try:
            optimize_stats = await table.optimize()
            result["optimized"] = True
            result["optimize_stats"] = repr(optimize_stats)
        except Exception as exc:
            result["optimize_error"] = str(exc)

    try:
        row_count = await table.count_rows()
    except Exception as exc:
        result["index_action"] = "index_maintenance_skipped"
        result["index_error"] = f"count_rows failed: {exc}"
        return result

    try:
        indices = await table.list_indices() if hasattr(table, "list_indices") else []
    except Exception as exc:
        result["index_action"] = "index_maintenance_skipped"
        result["index_error"] = f"list_indices failed: {exc}"
        return result
    has_embedding_idx = any(
        getattr(index, "name", "") == "embedding_idx" for index in indices
    )

    if has_embedding_idx and row_count < VECTOR_INDEX_MIN_ROWS:
        if hasattr(table, "drop_index"):
            try:
                await table.drop_index("embedding_idx")
                result["index_action"] = "dropped_embedding_idx_below_threshold"
            except Exception as exc:
                result["index_action"] = "drop_embedding_idx_failed"
                result["index_error"] = str(exc)
        return result

    if row_count >= VECTOR_INDEX_MIN_ROWS:
        try:
            embedding_dim = int(os.getenv("EMBEDDING_DIM", str(DEFAULT_EMBEDDING_DIM)))
            vector_metric = os.getenv("VECTOR_METRIC", DEFAULT_VECTOR_METRIC)
            await table.create_index(
                column="embedding",
                config=IvfPq(
                    distance_type=vector_metric,
                    num_partitions=256,
                    num_sub_vectors=embedding_dim // 8,
                ),
                replace=True,
            )
            result["index_action"] = "rebuilt_embedding_idx"
            if hasattr(table, "wait_for_index"):
                await table.wait_for_index(["embedding_idx"])
                result["waited_for_index"] = True
        except Exception as exc:
            result["index_action"] = "rebuild_embedding_idx_failed"
            result["index_error"] = str(exc)

    return result


def _cleanup_plan_from_report(args: argparse.Namespace, report: dict[str, Any]) -> CleanupPlan:
    orphan_file_ids: tuple[str, ...] = ()
    if args.delete_orphan_file_ids:
        orphan_file_ids = tuple(
            report["file_ids_present_in_lancedb_but_not_sqlite_indexed"]
        )

    return CleanupPlan(
        orphan_file_ids=orphan_file_ids,
        orphan_vault_id=str(args.delete_orphan_vault)
        if args.delete_orphan_vault is not None
        else None,
        stale_multiscale=bool(
            args.delete_stale_multiscale
            and report["stale_multiscale_rows_when_disabled"]["row_count"] > 0
        ),
    )


def _planned_delete_counts(report: dict[str, Any], plan: CleanupPlan) -> dict[str, int]:
    counts: dict[str, int] = {}
    if plan.orphan_file_ids:
        rows_by_file = report["lancedb_chunks_by_file_id"]
        counts["orphan_file_id_rows"] = sum(
            int(rows_by_file.get(file_id, 0)) for file_id in plan.orphan_file_ids
        )
    if plan.orphan_vault_id is not None:
        rows_by_vault = report["lancedb_chunks_by_vault_id"]
        counts["orphan_vault_rows"] = int(rows_by_vault.get(plan.orphan_vault_id, 0))
    if plan.stale_multiscale:
        counts["stale_multiscale_rows"] = int(
            report["stale_multiscale_rows_when_disabled"]["row_count"]
        )
    return counts


def _resolve_multi_scale_indexing_enabled(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    return _env_bool("MULTI_SCALE_INDEXING_ENABLED", False)


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    sqlite_path = Path(args.sqlite_path)
    lancedb_path = Path(args.lancedb_path)

    indexed_files = _load_indexed_files(sqlite_path)
    table = await _open_chunks_table(lancedb_path)
    chunks = await _load_lancedb_chunks(table)

    multi_scale_enabled = _resolve_multi_scale_indexing_enabled(
        args.multi_scale_indexing_enabled
    )
    report = build_report(
        indexed_files,
        chunks,
        multi_scale_indexing_enabled=multi_scale_enabled,
    )

    plan = _cleanup_plan_from_report(args, report)
    planned_delete_counts = _planned_delete_counts(report, plan)
    mutating_flags_requested = bool(planned_delete_counts) or bool(
        args.optimize_after_cleanup
    )
    report["cleanup_plan"] = {
        "dry_run": args.dry_run or not args.confirm,
        "requires_confirm": mutating_flags_requested
        and (args.dry_run or not args.confirm),
        "planned_delete_counts": planned_delete_counts,
    }

    if mutating_flags_requested and (args.dry_run or not args.confirm):
        return report

    if planned_delete_counts:
        if table is None:
            report["cleanup_result"] = {
                "deleted_counts": {},
                "remaining_after_delete_counts": {},
            }
        else:
            report["cleanup_result"] = await apply_cleanup(table, plan)

    if args.optimize_after_cleanup:
        if table is None:
            report["optimize_after_cleanup"] = {"optimized": False, "reason": "no chunks table"}
        else:
            report["optimize_after_cleanup"] = await optimize_after_cleanup(table)

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report and optionally clean LanceDB rows that disagree with indexed SQLite files.",
        epilog=(
            "Exit codes: 0 report completed; 1 unrecoverable error; "
            "2 destructive or optimize action requested without --confirm."
        ),
    )
    parser.add_argument(
        "--sqlite-path",
        default=str(_default_data_dir() / "app.db"),
        help="Path to SQLite app.db (default: DATA_DIR/app.db or backend/data/app.db)",
    )
    parser.add_argument(
        "--lancedb-path",
        default=str(_default_data_dir() / "lancedb"),
        help="Path to LanceDB directory (default: DATA_DIR/lancedb or backend/data/lancedb)",
    )
    parser.add_argument(
        "--multi-scale-indexing-enabled",
        choices=("auto", "true", "false"),
        default="auto",
        help="Whether non-default chunk_scale rows are current. auto reads MULTI_SCALE_INDEXING_ENABLED, default false.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report only. This is the default unless --confirm is supplied.",
    )
    parser.add_argument(
        "--delete-orphan-file-ids",
        action="store_true",
        help="Delete LanceDB rows whose file_id is not an indexed SQLite file.",
    )
    parser.add_argument(
        "--delete-orphan-vault",
        help="Delete LanceDB rows for this vault_id.",
    )
    parser.add_argument(
        "--delete-stale-multiscale",
        action="store_true",
        help="Delete chunk_scale != default rows reported stale when multi-scale indexing is disabled.",
    )
    parser.add_argument(
        "--optimize-after-cleanup",
        action="store_true",
        help="Run LanceDB optimize and refresh/drop embedding_idx after cleanup.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required for any destructive deletion flag.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        report = asyncio.run(_run(args))
        print(json.dumps(report, indent=2, sort_keys=True))
        if report["cleanup_plan"]["requires_confirm"]:
            print(
                "Destructive cleanup was not run. Re-run with --confirm after reviewing row counts.",
                file=sys.stderr,
            )
            return 2
    except Exception as exc:
        print(f"reconciliation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
