"""Canvas API routes for versioned code/document artifacts (issue #509).

All canvas endpoints intentionally live in this single module, including the
chat-scoped create/list routes under ``/chat/sessions/{session_id}/artifacts``.
Colocation keeps one feature owner for the whole canvas surface (single module
to review/test); the chat-scoped paths create/list rows that require the chat
session row anyway, so transactional locality is preserved here. There is no
path/method collision with chat.py — chat.py owns no
``/chat/sessions/{id}/artifacts`` route, and FastAPI matches the full
path + method, so the two routers compose without ambiguity.

Authorization matrix (pinned in the #509 plan): every route resolves the
artifact/session row FIRST and raises 404 when absent (before any policy
evaluation — no vault-existence oracle), then authorizes through
``evaluate(user, "vault", vault_id, "read"|"write")`` exactly like chat.py's
get_session. There is no separate admin bypass: admin access flows through the
same evaluate() policy. Mutating routes add ``csrf_protect``; create and
edit-range are rate-limited. Every route 503s with ``canvas_disabled`` when
``settings.canvas_enabled`` is false.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple
from urllib.parse import quote as _url_quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import get_current_active_user, get_db, get_evaluate_policy
from app.config import settings
from app.limiter import limiter
from app.security import csrf_protect
from app.services.canvas_store import CANVAS_VERSION_CONFLICT, CanvasStore
from app.services.llm_client import LLMError
from app.services.upload_validation import secure_filename

logger = logging.getLogger(__name__)

router = APIRouter(tags=["canvas"])

# Rate limit shared by the two model/storage-heavy entry points (create and
# targeted model edit). Plain literal per the #509 plan.
_CANVAS_RATE_LIMIT = "30/minute"

# Bounded generation parameters for targeted range edits.
_EDIT_RANGE_TEMPERATURE = 0.2
_EDIT_RANGE_MAX_TOKENS = 4096

# language -> (file extension, text media type). Download/exposure surface for
# the pinned extension map in the plan; anything unmapped falls back to .txt.
_LANGUAGE_FILES: Dict[str, Tuple[str, str]] = {
    "py": (".py", "text/x-python"),
    "js": (".js", "text/javascript"),
    "ts": (".ts", "text/typescript"),
    "tsx": (".tsx", "text/javascript"),
    "jsx": (".jsx", "text/javascript"),
    "java": (".java", "text/x-java-source"),
    "go": (".go", "text/x-go"),
    "rs": (".rs", "text/rust"),
    "c": (".c", "text/x-c"),
    "cpp": (".cpp", "text/x-c++"),
    "h": (".h", "text/x-c"),
    "cs": (".cs", "text/x-csharp"),
    "rb": (".rb", "text/x-ruby"),
    "php": (".php", "text/x-php"),
    "sh": (".sh", "text/x-shellscript"),
    "sql": (".sql", "text/x-sql"),
    "html": (".html", "text/html"),
    "css": (".css", "text/css"),
    "json": (".json", "application/json"),
    "yaml": (".yaml", "text/yaml"),
    "yml": (".yml", "text/yaml"),
    "xml": (".xml", "application/xml"),
    "toml": (".toml", "text/x-toml"),
    "md": (".md", "text/markdown"),
    "txt": (".txt", "text/plain"),
}

# Module-level lazy singleton so tests (and conftest pool resets) share one
# store instance that resolves its pool per call. See canvas_store docstring.
_canvas_store: Optional[CanvasStore] = None


def get_canvas_store() -> CanvasStore:
    """Return the process-wide CanvasStore, creating it on first use."""
    global _canvas_store
    if _canvas_store is None:
        _canvas_store = CanvasStore()
    return _canvas_store


# ── request models ──────────────────────────────────────────────────────────


class CreateCanvasArtifactRequest(BaseModel):
    """Create a canvas artifact from chat answer output."""

    kind: str
    name: str
    content: str
    language: Optional[str] = None
    message_id: Optional[int] = None
    turn_id: Optional[str] = None
    source_refs: Optional[List[Dict[str, Any]]] = None


class SaveCanvasVersionRequest(BaseModel):
    """Append a user-edited version."""

    content: str
    name: Optional[str] = None
    base_version_no: int = Field(ge=1)
    force: bool = False


class RestoreCanvasVersionRequest(BaseModel):
    """Append a restore of a historical version."""

    version_no: int = Field(ge=1)
    base_version_no: int = Field(ge=1)


class EditRangeRequest(BaseModel):
    """Ask the model to replace an inclusive 1-based line range."""

    start_line: int
    end_line: int
    instruction: str
    base_version_no: int = Field(ge=1)


# ── helpers ─────────────────────────────────────────────────────────────────


def _require_canvas_enabled() -> None:
    """503 with ``canvas_disabled`` on every canvas route when the flag is off."""
    if not settings.canvas_enabled:
        raise HTTPException(status_code=503, detail="canvas_disabled")


def _parse_refs(raw: Optional[str]) -> List[Dict[str, Any]]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _parse_model_edit(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, json.JSONDecodeError):
        return None


def _serialize_artifact(artifact: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "artifact_uid": artifact["artifact_uid"],
        "session_id": artifact["session_id"],
        "message_id": artifact["message_id"],
        "turn_id": artifact["turn_id"],
        "kind": artifact["kind"],
        "name": artifact["name"],
        "language": artifact["language"],
        "vault_id": artifact["vault_id"],
        "current_version_no": artifact["current_version_no"],
        "source_refs": _parse_refs(artifact["source_refs_json"]),
        "created_by": artifact["created_by"],
        "created_at": artifact["created_at"],
        "updated_at": artifact["updated_at"],
    }


def _serialize_version_summary(version: Dict[str, Any]) -> Dict[str, Any]:
    """Version list shape: metadata only, never content bodies."""
    return {
        "id": version["id"],
        "version_no": version["version_no"],
        "name": version["name"],
        "origin": version["origin"],
        "content_sha256": version["content_sha256"],
        "model_edit": _parse_model_edit(version["model_edit_json"]),
        "created_by": version["created_by"],
        "created_at": version["created_at"],
    }


def _serialize_version(version: Dict[str, Any]) -> Dict[str, Any]:
    summary = _serialize_version_summary(version)
    summary["content"] = version["content"]
    return summary


async def _load_artifact_or_404(artifact_uid: str) -> Dict[str, Any]:
    """Load the artifact row by uid; 404 (before policy) when absent."""
    store = get_canvas_store()
    artifact = await asyncio.to_thread(store.get_by_uid, artifact_uid)
    if artifact is None:
        raise HTTPException(status_code=404, detail="canvas_artifact_not_found")
    return artifact


async def _authorize_artifact(
    artifact: Dict[str, Any],
    user: dict,
    evaluate: Callable,
    action: Literal["read", "write"],
) -> None:
    if not await evaluate(user, "vault", artifact["vault_id"], action):
        raise HTTPException(
            status_code=403,
            detail=f"No {action} access to this vault",
        )


def _check_content(content: str) -> None:
    """Shared create/save content guards: non-empty and within the size cap."""
    if not content or not content.strip():
        raise HTTPException(status_code=422, detail="canvas_content_required")
    if len(content) > settings.canvas_max_artifact_kb * 1024:
        raise HTTPException(status_code=413, detail="canvas_artifact_too_large")


def _strip_markdown_fences(text: str) -> str:
    """Strip a wrapping ``` fence pair if the model added one anyway.

    Only whole first/last fence lines are removed; any other whitespace of the
    replacement is preserved byte-for-byte (no normalization pass).
    """
    lines = text.split("\n")
    if len(lines) >= 2 and lines[0].startswith("```"):
        last = len(lines) - 1
        while last > 0 and lines[last].strip() == "":
            last -= 1
        if lines[last].strip() == "```":
            return "\n".join(lines[1:last])
    return text


def _download_filename(name: str, language: Optional[str]) -> str:
    safe_name = secure_filename(name.replace(" ", "_")) or "canvas"
    # secure_filename(".")/".." return "."/".." — normalize those to the
    # fallback so a degenerate artifact name cannot yield "..txt".
    if safe_name in {".", ".."}:
        safe_name = "canvas"
    ext, _media = _LANGUAGE_FILES.get((language or "").lower(), (".txt", "text/plain"))
    if not safe_name.lower().endswith(ext):
        safe_name = f"{safe_name}{ext}"
    return safe_name


def _download_media_type(language: Optional[str]) -> str:
    _ext, media = _LANGUAGE_FILES.get((language or "").lower(), (".txt", "text/plain"))
    return media


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── chat-scoped create/list (intentionally colocated — see module docstring) ─


@router.post("/chat/sessions/{session_id}/artifacts")
@limiter.limit(_CANVAS_RATE_LIMIT)
async def create_session_artifact(
    request: Request,
    session_id: int,
    body: CreateCanvasArtifactRequest,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
):
    """Create a canvas artifact from chat answer output.

    Version 1 (origin="created") is inserted atomically with the artifact row.
    ``turn_id`` is resolved server-side from ``message_id`` when provided (the
    server value wins); the client-supplied ``turn_id`` is honored only when
    ``message_id`` is absent. ``source_refs`` is persisted as a snapshot.
    """
    _require_canvas_enabled()

    if body.kind not in ("code", "document"):
        raise HTTPException(status_code=422, detail="canvas_invalid_kind")
    _check_content(body.content)

    session_result = await asyncio.to_thread(
        conn.execute,
        "SELECT id, vault_id FROM chat_sessions WHERE id = ?",
        (session_id,),
    )
    session_row = await asyncio.to_thread(session_result.fetchone)
    if session_row is None:
        raise HTTPException(status_code=404, detail="canvas_session_not_found")
    vault_id = session_row[1]

    # Creating session-scoped data is a mutation: it requires vault WRITE,
    # matching every chat mutation route (add_message / batch / truncate /
    # feedback all evaluate "write" on the session's vault).
    if not await evaluate(user, "vault", vault_id, "write"):
        raise HTTPException(status_code=403, detail="No write access to this vault")

    turn_id: Optional[str] = None
    if body.message_id is not None:
        # Ownership validation first (mirrors chat.py message-feedback shape):
        # the message must belong to THIS session, else 404 without leaking
        # that the message exists elsewhere.
        check_result = await asyncio.to_thread(
            conn.execute,
            "SELECT 1 FROM chat_messages WHERE id = ? AND session_id = ?",
            (body.message_id, session_id),
        )
        if await asyncio.to_thread(check_result.fetchone) is None:
            raise HTTPException(status_code=404, detail="canvas_message_not_found")
        msg_result = await asyncio.to_thread(
            conn.execute,
            "SELECT turn_id FROM chat_messages WHERE id = ?",
            (body.message_id,),
        )
        msg_row = await asyncio.to_thread(msg_result.fetchone)
        turn_id = msg_row[0] if msg_row is not None else None
    else:
        turn_id = body.turn_id

    store = get_canvas_store()
    artifact = await asyncio.to_thread(
        store.create_artifact,
        session_id=session_id,
        kind=body.kind,
        name=body.name,
        content=body.content,
        language=body.language,
        message_id=body.message_id,
        turn_id=turn_id,
        vault_id=vault_id,
        created_by=user.get("id"),
        source_refs=body.source_refs,
    )

    version = await asyncio.to_thread(
        store.get_version, artifact["id"], artifact["current_version_no"]
    )
    detail = _serialize_artifact(artifact)
    detail["current_version"] = _serialize_version(version) if version else None
    logger.info(
        "canvas: created artifact uid=%s kind=%s session=%s message=%s",
        artifact["artifact_uid"],
        body.kind,
        session_id,
        body.message_id,
    )
    return detail


@router.get("/chat/sessions/{session_id}/artifacts")
async def list_session_artifacts(
    session_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """List a session's canvas artifacts (independent identities; duplicate
    names are allowed — identity is the artifact_uid, not the name)."""
    _require_canvas_enabled()

    session_result = await asyncio.to_thread(
        conn.execute,
        "SELECT id, vault_id FROM chat_sessions WHERE id = ?",
        (session_id,),
    )
    session_row = await asyncio.to_thread(session_result.fetchone)
    if session_row is None:
        raise HTTPException(status_code=404, detail="canvas_session_not_found")

    if not await evaluate(user, "vault", session_row[1], "read"):
        raise HTTPException(status_code=403, detail="No read access to this vault")

    store = get_canvas_store()
    artifacts = await asyncio.to_thread(store.list_for_session, session_id)
    return {"artifacts": [_serialize_artifact(a) for a in artifacts]}


# ── artifact routes ─────────────────────────────────────────────────────────


@router.get("/canvas/capabilities")
async def canvas_capabilities(
    user: dict = Depends(get_current_active_user),
):
    """Feature gating for the frontend. Fails closed: when canvas is disabled
    this 503s (and the frontend treats any non-success as disabled)."""
    _require_canvas_enabled()
    return {"enabled": settings.canvas_enabled}


@router.get("/canvas/artifacts/{artifact_uid}")
async def get_canvas_artifact(
    artifact_uid: str,
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """Artifact detail including the current version's full content."""
    _require_canvas_enabled()
    artifact = await _load_artifact_or_404(artifact_uid)
    await _authorize_artifact(artifact, user, evaluate, "read")

    store = get_canvas_store()
    version = await asyncio.to_thread(
        store.get_version, artifact["id"], artifact["current_version_no"]
    )
    detail = _serialize_artifact(artifact)
    detail["current_version"] = _serialize_version(version) if version else None
    return detail


@router.get("/canvas/artifacts/{artifact_uid}/versions")
async def list_canvas_versions(
    artifact_uid: str,
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """Version history WITHOUT content bodies (metadata + origin + model_edit)."""
    _require_canvas_enabled()
    artifact = await _load_artifact_or_404(artifact_uid)
    await _authorize_artifact(artifact, user, evaluate, "read")

    store = get_canvas_store()
    versions = await asyncio.to_thread(store.list_versions, artifact["id"])
    return {"versions": [_serialize_version_summary(v) for v in versions]}


@router.get("/canvas/artifacts/{artifact_uid}/versions/{version_no}")
async def get_canvas_version(
    artifact_uid: str,
    version_no: int,
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """One exact version, content included."""
    _require_canvas_enabled()
    artifact = await _load_artifact_or_404(artifact_uid)
    await _authorize_artifact(artifact, user, evaluate, "read")

    store = get_canvas_store()
    version = await asyncio.to_thread(store.get_version, artifact["id"], version_no)
    if version is None:
        raise HTTPException(status_code=404, detail="canvas_version_not_found")
    return _serialize_version(version)


@router.post("/canvas/artifacts/{artifact_uid}/versions")
async def save_canvas_version(
    artifact_uid: str,
    body: SaveCanvasVersionRequest,
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
):
    """Append a user-edited version.

    Optimistic concurrency: a stale ``base_version_no`` yields 409
    ``canvas_version_conflict`` unless ``force=true`` (append at current+1,
    history preserved).
    """
    _require_canvas_enabled()
    _check_content(body.content)
    artifact = await _load_artifact_or_404(artifact_uid)
    await _authorize_artifact(artifact, user, evaluate, "write")

    store = get_canvas_store()
    version = await asyncio.to_thread(
        store.append_version,
        artifact["id"],
        content=body.content,
        origin="user_edit",
        name=body.name,
        created_by=user.get("id"),
        base_version_no=body.base_version_no,
        force=body.force,
    )
    if version is CANVAS_VERSION_CONFLICT:
        raise HTTPException(status_code=409, detail="canvas_version_conflict")
    return _serialize_version(version)


@router.post("/canvas/artifacts/{artifact_uid}/restore")
async def restore_canvas_version(
    artifact_uid: str,
    body: RestoreCanvasVersionRequest,
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
):
    """Restore a historical version by APPENDING a copy of its content.

    History is never destroyed; the new version carries origin="restore" and
    model_edit=NULL (restores copy content only — no fabricated provenance).
    """
    _require_canvas_enabled()
    artifact = await _load_artifact_or_404(artifact_uid)
    await _authorize_artifact(artifact, user, evaluate, "write")

    store = get_canvas_store()
    historical = await asyncio.to_thread(
        store.get_version, artifact["id"], body.version_no
    )
    if historical is None:
        raise HTTPException(status_code=404, detail="canvas_version_not_found")

    version = await asyncio.to_thread(
        store.append_version,
        artifact["id"],
        content=historical["content"],
        origin="restore",
        name=historical["name"],
        created_by=user.get("id"),
        base_version_no=body.base_version_no,
        force=False,
    )
    if version is CANVAS_VERSION_CONFLICT:
        raise HTTPException(status_code=409, detail="canvas_version_conflict")
    return _serialize_version(version)


@router.post("/canvas/artifacts/{artifact_uid}/edit-range")
@limiter.limit(_CANVAS_RATE_LIMIT)
async def edit_canvas_range(
    request: Request,
    artifact_uid: str,
    body: EditRangeRequest,
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
    _csrf_token: str = Depends(csrf_protect),
):
    """Targeted model edit of an inclusive 1-based line range.

    The model call happens BEFORE any write transaction (no DB lock held
    during LLM latency). The prompt contains ONLY the selected lines + the
    instruction. After the model returns, the splice appends via
    ``append_version`` with the ORIGINAL ``base_version_no``; if a concurrent
    save superseded it, the append 409s and the model output is DISCARDED.
    """
    _require_canvas_enabled()
    if not body.instruction or not body.instruction.strip():
        raise HTTPException(status_code=422, detail="canvas_instruction_required")
    artifact = await _load_artifact_or_404(artifact_uid)
    await _authorize_artifact(artifact, user, evaluate, "write")

    store = get_canvas_store()
    base = await asyncio.to_thread(
        store.get_version, artifact["id"], body.base_version_no
    )
    if base is None:
        raise HTTPException(status_code=404, detail="canvas_version_not_found")

    lines = base["content"].split("\n")
    line_count = len(lines)
    if not (1 <= body.start_line <= body.end_line <= line_count):
        raise HTTPException(
            status_code=422,
            detail=(
                "canvas_invalid_range: 1 <= start_line <= end_line <= "
                f"{line_count}"
            ),
        )

    selected = "\n".join(lines[body.start_line - 1 : body.end_line])
    messages = [
        {
            "role": "system",
            "content": (
                "You are a precise editing assistant. You return ONLY the "
                "replacement text for the selected lines. Never add markdown "
                "code fences, explanations, or any surrounding context."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Selected lines {body.start_line}-{body.end_line} "
                f"of {line_count}:\n\n{selected}\n\n"
                f"Instruction: {body.instruction}\n\n"
                "Return the replacement for the selected lines only, "
                "no markdown fences."
            ),
        },
    ]

    client = getattr(request.app.state, "llm_client", None)
    if client is None:
        raise HTTPException(status_code=502, detail="canvas_model_unavailable")
    try:
        replacement = await client.chat_completion(
            messages,
            temperature=_EDIT_RANGE_TEMPERATURE,
            max_tokens=_EDIT_RANGE_MAX_TOKENS,
        )
    except LLMError as exc:
        logger.warning("canvas: edit-range model call failed uid=%s: %s", artifact_uid, exc)
        raise HTTPException(status_code=502, detail="canvas_model_unavailable") from exc

    replacement = _strip_markdown_fences(replacement)

    # Pinned splice: split/join on LF only, so untouched lines (including CR
    # characters from CRLF endings and a trailing final newline) stay
    # byte-identical by construction.
    new_content = "\n".join(
        lines[: body.start_line - 1]
        + replacement.split("\n")
        + lines[body.end_line :]
    )
    _check_content(new_content)

    version = await asyncio.to_thread(
        store.append_version,
        artifact["id"],
        content=new_content,
        origin="model_edit",
        created_by=user.get("id"),
        base_version_no=body.base_version_no,
        force=False,
        model_edit={
            "start_line": body.start_line,
            "end_line": body.end_line,
            "instruction": body.instruction,
            "base_version_no": body.base_version_no,
        },
    )
    if version is CANVAS_VERSION_CONFLICT:
        # Concurrent save superseded the base: the model output is discarded.
        raise HTTPException(status_code=409, detail="canvas_version_conflict")
    return _serialize_version(version)


@router.get("/canvas/artifacts/{artifact_uid}/versions/{version_no}/download")
async def download_canvas_version(
    artifact_uid: str,
    version_no: int,
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """Download exactly this version's bytes (UTF-8, no normalization)."""
    _require_canvas_enabled()
    artifact = await _load_artifact_or_404(artifact_uid)
    await _authorize_artifact(artifact, user, evaluate, "read")

    store = get_canvas_store()
    version = await asyncio.to_thread(store.get_version, artifact["id"], version_no)
    if version is None:
        raise HTTPException(status_code=404, detail="canvas_version_not_found")

    headers: Dict[str, str] = {
        "Content-Disposition": (
            f'attachment; filename="{_download_filename(artifact["name"], artifact["language"])}"'
        ),
        "X-Canvas-Session-Id": str(artifact["session_id"]),
    }
    if artifact["turn_id"] is not None:
        headers["X-Canvas-Turn-Id"] = str(artifact["turn_id"])
    if artifact["message_id"] is not None:
        headers["X-Canvas-Origin-Message-Id"] = str(artifact["message_id"])
    source_refs = _parse_refs(artifact["source_refs_json"])
    if source_refs:
        headers["X-Canvas-Source-Refs"] = _url_quote(
            json.dumps(source_refs, separators=(",", ":"))
        )

    return Response(
        content=version["content"].encode("utf-8"),
        media_type=_download_media_type(artifact["language"]),
        headers=headers,
    )


@router.get("/canvas/artifacts/{artifact_uid}/export")
async def export_canvas_manifest(
    artifact_uid: str,
    version_no: Optional[int] = None,
    user: dict = Depends(get_current_active_user),
    evaluate: Callable = Depends(get_evaluate_policy),
):
    """Export a JSON manifest for one version (defaults to the current one).

    The manifest carries the create-time source-refs snapshot (not live
    references) plus lineage ids so an exported artifact remains provable.
    """
    _require_canvas_enabled()
    artifact = await _load_artifact_or_404(artifact_uid)
    await _authorize_artifact(artifact, user, evaluate, "read")

    target_no = version_no if version_no is not None else artifact["current_version_no"]
    store = get_canvas_store()
    version = await asyncio.to_thread(store.get_version, artifact["id"], target_no)
    if version is None:
        raise HTTPException(status_code=404, detail="canvas_version_not_found")

    return {
        "artifact_uid": artifact["artifact_uid"],
        "kind": artifact["kind"],
        "name": artifact["name"],
        "language": artifact["language"],
        "version_no": version["version_no"],
        "version_name": version["name"],
        "content": version["content"],
        "content_sha256": version["content_sha256"],
        "source_refs": _parse_refs(artifact["source_refs_json"]),
        "session_id": artifact["session_id"],
        "turn_id": artifact["turn_id"],
        "message_id": artifact["message_id"],
        "exported_at": _utc_now_iso(),
    }
