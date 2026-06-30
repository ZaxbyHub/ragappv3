"""Admin-gated API endpoints for prompt versioning (FR-007).

All endpoints require admin:config scope and CSRF protection.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.models.database import get_pool
from app.security import csrf_protect, require_scope
from app.services.ab_testing import ABTestingService
from app.services.prompt_store import PromptVersionStore

router = APIRouter(prefix="/prompts", tags=["prompts"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class PromptVersionMetadata(BaseModel):
    """Prompt version metadata (no full content)."""

    id: int
    version: str
    created_at: str
    is_active: bool
    created_by: str | None


class PromptVersionContent(BaseModel):
    """Prompt version with full content (for recovery)."""

    id: int
    version: str
    content: str
    created_at: str
    is_active: bool
    created_by: str | None


class CreatePromptVersionRequest(BaseModel):
    version: str
    content: str
    activate: bool = False


class CreatePromptVersionResponse(BaseModel):
    id: int
    version: str
    created_at: str
    is_active: bool


class ActivatePromptVersionResponse(BaseModel):
    id: int
    version: str
    is_active: bool


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=list[PromptVersionMetadata])
async def list_prompt_versions(
    _auth: dict = Depends(require_scope("admin:config")),
    _csrf_token: str = Depends(csrf_protect),
) -> list[PromptVersionMetadata]:
    """List all prompt versions (metadata only, no full content)."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        store = PromptVersionStore(conn)
        versions = await asyncio.to_thread(store.list_versions)
        return [
            PromptVersionMetadata(
                id=v.id,
                version=v.version,
                created_at=v.created_at,
                is_active=v.is_active,
                created_by=v.created_by,
            )
            for v in versions
        ]
    finally:
        pool.release_connection(conn)


@router.get("/active", response_model=PromptVersionContent | None)
async def get_active_prompt_version(
    _auth: dict = Depends(require_scope("admin:config")),
    _csrf_token: str = Depends(csrf_protect),
) -> PromptVersionContent | None:
    """Get the currently-active prompt version (metadata + content)."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        store = PromptVersionStore(conn)
        version = await asyncio.to_thread(store.get_active)
        if version is None:
            return None
        return PromptVersionContent(
            id=version.id,
            version=version.version,
            content=version.content,
            created_at=version.created_at,
            is_active=version.is_active,
            created_by=version.created_by,
        )
    finally:
        pool.release_connection(conn)


@router.post(
    "",
    response_model=CreatePromptVersionResponse,
    status_code=201,
)
async def create_prompt_version(
    payload: CreatePromptVersionRequest,
    _auth: dict = Depends(require_scope("admin:config")),
    _csrf_token: str = Depends(csrf_protect),
) -> CreatePromptVersionResponse:
    """Create a new prompt version.

    If ``activate`` is True, the new version is set as active immediately.
    """
    if not payload.version.strip():
        raise HTTPException(status_code=400, detail="version cannot be empty")
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="content cannot be empty")

    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        store = PromptVersionStore(conn)
        created = await asyncio.to_thread(
            store.create_version,
            payload.version.strip(),
            payload.content,
            activate=payload.activate,
            created_by=_auth.get("user_id"),
        )
        return CreatePromptVersionResponse(
            id=created.id,
            version=created.version,
            created_at=created.created_at,
            is_active=created.is_active,
        )
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            raise HTTPException(
                status_code=409,
                detail=f"A prompt version with version={payload.version!r} already exists",
            )
        raise
    finally:
        pool.release_connection(conn)


@router.post(
    "/{version}/activate",
    response_model=ActivatePromptVersionResponse,
)
async def activate_prompt_version(
    version: str,
    _auth: dict = Depends(require_scope("admin:config")),
    _csrf_token: str = Depends(csrf_protect),
) -> ActivatePromptVersionResponse:
    """Activate a specific prompt version by name.

    This is transactional: the named version is set to is_active=1
    and all other versions are set to is_active=0.
    """
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        store = PromptVersionStore(conn)
        activated = await asyncio.to_thread(store.activate, version)
        return ActivatePromptVersionResponse(
            id=activated.id,
            version=activated.version,
            is_active=activated.is_active,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    finally:
        pool.release_connection(conn)


# ---------------------------------------------------------------------------
# A/B experiment models (FR-007 part 3)
# ---------------------------------------------------------------------------


class CreateExperimentRequest(BaseModel):
    """Request to create a new A/B experiment."""
    name: str
    control_version: str
    challenger_version: str
    split_pct: int = 50


class EndExperimentRequest(BaseModel):
    """Request to end an A/B experiment and declare the winner."""
    winner: str  # 'control' or 'challenger'


class ExperimentVariantCounts(BaseModel):
    """Per-variant exposure counts for an experiment listing."""
    control_exposures: int
    challenger_exposures: int


class ExperimentResponse(BaseModel):
    """An A/B experiment with per-variant exposure counts."""
    id: int
    name: str
    control_version: str
    challenger_version: str
    split_pct: int
    status: str
    winner: str | None
    created_at: str
    ended_at: str | None
    control_exposures: int
    challenger_exposures: int


# ---------------------------------------------------------------------------
# A/B experiment endpoints (FR-007 part 3)
# ---------------------------------------------------------------------------


@router.post("/ab-experiments", response_model=ExperimentResponse, status_code=201)
async def create_ab_experiment(
    payload: CreateExperimentRequest,
    _auth: dict = Depends(require_scope("admin:config")),
    _csrf_token: str = Depends(csrf_protect),
) -> ExperimentResponse:
    """Create a new A/B prompt experiment.

    The experiment becomes active immediately.  At most one experiment should be
    active at a time — end the current experiment before creating a new one.
    """
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="name cannot be empty")
    if payload.control_version == payload.challenger_version:
        raise HTTPException(
            status_code=400,
            detail="control_version and challenger_version must differ",
        )
    if not (0 <= payload.split_pct <= 100):
        raise HTTPException(
            status_code=400,
            detail="split_pct must be between 0 and 100",
        )

    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        ab_service = ABTestingService(conn)
        # Validate that both version strings exist in prompt_versions
        store = PromptVersionStore(conn)
        ctrl = await asyncio.to_thread(store.get_version, payload.control_version)
        chlg = await asyncio.to_thread(store.get_version, payload.challenger_version)
        if ctrl is None:
            raise HTTPException(
                status_code=400,
                detail=f"control_version={payload.control_version!r} not found",
            )
        if chlg is None:
            raise HTTPException(
                status_code=400,
                detail=f"challenger_version={payload.challenger_version!r} not found",
            )
        try:
            exp = await asyncio.to_thread(
                ab_service.create_experiment,
                payload.name.strip(),
                payload.control_version,
                payload.challenger_version,
                payload.split_pct,
            )
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail=f"An experiment with name={payload.name!r} already exists",
                )
            raise

        # Build response with zero exposure counts
        return ExperimentResponse(
            id=exp.id,
            name=exp.name,
            control_version=exp.control_version,
            challenger_version=exp.challenger_version,
            split_pct=exp.split_pct,
            status=exp.status,
            winner=exp.winner,
            created_at=exp.created_at,
            ended_at=exp.ended_at,
            control_exposures=0,
            challenger_exposures=0,
        )
    finally:
        pool.release_connection(conn)


@router.get("/ab-experiments", response_model=list[ExperimentResponse])
async def list_ab_experiments(
    _auth: dict = Depends(require_scope("admin:config")),
    _csrf_token: str = Depends(csrf_protect),
) -> list[ExperimentResponse]:
    """List all A/B experiments with per-variant exposure counts."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        ab_service = ABTestingService(conn)
        experiments = await asyncio.to_thread(ab_service.list_experiments)
        return [
            ExperimentResponse(
                id=ewc.experiment.id,
                name=ewc.experiment.name,
                control_version=ewc.experiment.control_version,
                challenger_version=ewc.experiment.challenger_version,
                split_pct=ewc.experiment.split_pct,
                status=ewc.experiment.status,
                winner=ewc.experiment.winner,
                created_at=ewc.experiment.created_at,
                ended_at=ewc.experiment.ended_at,
                control_exposures=ewc.control_exposures,
                challenger_exposures=ewc.challenger_exposures,
            )
            for ewc in experiments
        ]
    finally:
        pool.release_connection(conn)


@router.post("/ab-experiments/{experiment_id}/end", response_model=ExperimentResponse)
async def end_ab_experiment(
    experiment_id: int,
    payload: EndExperimentRequest,
    _auth: dict = Depends(require_scope("admin:config")),
    _csrf_token: str = Depends(csrf_protect),
) -> ExperimentResponse:
    """End an A/B experiment and declare the winner.

    After ending, the experiment's status becomes 'ended' and the winner is recorded.
    Ending an already-ended experiment returns a 400 error.
    """
    if payload.winner not in ("control", "challenger"):
        raise HTTPException(
            status_code=400,
            detail="winner must be 'control' or 'challenger'",
        )

    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        ab_service = ABTestingService(conn)
        try:
            await asyncio.to_thread(
                ab_service.end_experiment, experiment_id, payload.winner
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # Fetch updated listing to get exposure counts
        experiments = await asyncio.to_thread(ab_service.list_experiments)
        for ewc in experiments:
            if ewc.experiment.id == experiment_id:
                return ExperimentResponse(
                    id=ewc.experiment.id,
                    name=ewc.experiment.name,
                    control_version=ewc.experiment.control_version,
                    challenger_version=ewc.experiment.challenger_version,
                    split_pct=ewc.experiment.split_pct,
                    status=ewc.experiment.status,
                    winner=ewc.experiment.winner,
                    created_at=ewc.experiment.created_at,
                    ended_at=ewc.experiment.ended_at,
                    control_exposures=ewc.control_exposures,
                    challenger_exposures=ewc.challenger_exposures,
                )
        # Should not reach here — end_experiment succeeded
        raise HTTPException(status_code=404, detail="Experiment not found after end")
    finally:
        pool.release_connection(conn)


# ---------------------------------------------------------------------------
# Fallback: recover prior prompt version by name (registered LAST so that
# specific A/B experiment routes are matched first)
# ---------------------------------------------------------------------------


@router.get("/{version}", response_model=PromptVersionContent)
async def get_prompt_version(
    version: str,
    _auth: dict = Depends(require_scope("admin:config")),
    _csrf_token: str = Depends(csrf_protect),
) -> PromptVersionContent:
    """Recover a prior prompt version by name (returns full content)."""
    pool = get_pool(str(settings.sqlite_path))
    conn = pool.get_connection()
    try:
        store = PromptVersionStore(conn)
        pv = await asyncio.to_thread(store.get_version, version)
        if pv is None:
            raise HTTPException(
                status_code=404,
                detail=f"No prompt version with version={version!r}",
            )
        return PromptVersionContent(
            id=pv.id,
            version=pv.version,
            content=pv.content,
            created_at=pv.created_at,
            is_active=pv.is_active,
            created_by=pv.created_by,
        )
    finally:
        pool.release_connection(conn)
