"""Error-detail lookup for clients behind a body-replacing proxy (T3).

The portal in front of this deployment replaces the body AND the headers of
every non-2xx response, so the browser cannot classify a failure from what
it receives. ``ErrorCodeHeaderMiddleware`` records each non-2xx outcome
(code + detail + status) against the caller's ``X-Meridian-Request-Id``
header for a short TTL; this endpoint returns it.

Scoping: a record is readable only with the exact bearer token that made
the failing request (the record stores a SHA-256 of that token, never the
token itself), so nothing leaks across users. The endpoint is deliberately
independent of the JWT/session machinery: the token hash IS the
authorization, and a caller whose token rotated between the failed request
and the lookup gets a 404 and falls back to a status-based message.
"""

from fastapi import APIRouter, HTTPException, Query, Request

from app.middleware.portal_resilience import _caller_token_hash, lookup_error

router = APIRouter(prefix="/errors", tags=["errors"])


@router.get("/lookup")
async def lookup_error_detail(
    request: Request,
    request_id: str = Query(..., min_length=8, max_length=128),
):
    token_hash = _caller_token_hash(request.scope)
    if not token_hash:
        raise HTTPException(status_code=401, detail="Authentication required")
    record = lookup_error(request_id, token_hash)
    if record is None:
        raise HTTPException(status_code=404, detail="No recorded error for this request id")
    return record
