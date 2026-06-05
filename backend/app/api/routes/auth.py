"""Authentication routes."""

import asyncio
import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import assign_user_to_default_vault, get_current_active_user, get_db
from app.limiter import limiter
from app.security import csrf_protect, get_csrf_manager, issue_csrf_token
from app.services.auth_service import (
    async_hash_password,
    async_verify_password,
    create_access_token,
    create_refresh_token,
    password_strength_check,
)
from app.utils.paths import refresh_cookie_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_TOKEN_COOKIE_NAME = "refresh_token"
REFRESH_TOKEN_MAX_AGE_DAYS = 30


def _record_failed_attempt_db(
    db,
    user_id: int,
    failed_attempts: int,
) -> None:
    """Record a failed login attempt and lock out the account if threshold reached.

    Executes in a single transaction with rollback on failure.
    """
    try:
        db.execute(
            "UPDATE users SET failed_attempts = failed_attempts + 1 WHERE id = ?",
            (user_id,),
        )
        if failed_attempts + 1 >= 5:
            lockout_until = datetime.now(timezone.utc) + timedelta(minutes=15)
            db.execute(
                "UPDATE users SET locked_until = ? WHERE id = ?",
                (lockout_until.isoformat(), user_id),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise


def _rotate_refresh_token_block(
    db,
    session_id: int,
    token_hash: str,
    user_id: int,
    new_refresh_token_hash: str,
    new_expires_at: datetime,
) -> None:
    """Execute the exclusive-lock block for refresh token rotation.

    Raises HTTPException on auth failure, re-raises other exceptions.
    """
    exclusive_started = False
    try:
        db.execute("BEGIN EXCLUSIVE")
        exclusive_started = True
    except sqlite3.OperationalError as exc:
        # Already in a transaction (e.g., from connection pool wrapper) — proceed without exclusive lock
        logger.warning(
            "BEGIN EXCLUSIVE failed for session %s (user %s) — falling back to existing transaction: %s",
            session_id,
            user_id,
            exc,
        )

    try:
        # Re-verify the session still exists (prevents TOCTOU)
        cursor = db.execute(
            "SELECT id FROM user_sessions WHERE id = ? AND refresh_token_hash = ?",
            (session_id, token_hash),
        )
        if not cursor.fetchone():
            if exclusive_started:
                db.execute("ROLLBACK")
            raise HTTPException(status_code=401, detail="Refresh token already used", headers={"WWW-Authenticate": "Bearer"})

        # Insert new session BEFORE deleting old — if INSERT fails, old session remains valid
        db.execute(
            "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at, last_used_at) VALUES (?, ?, ?, ?)",
            (
                user_id,
                new_refresh_token_hash,
                new_expires_at.isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        # Delete old session
        db.execute("DELETE FROM user_sessions WHERE id = ?", (session_id,))
        db.execute("COMMIT")
    except sqlite3.IntegrityError:
        if exclusive_started:
            try:
                db.execute("ROLLBACK")
            except Exception:
                pass
        raise HTTPException(status_code=401, detail="Refresh token already used", headers={"WWW-Authenticate": "Bearer"})
    except HTTPException:
        raise
    except Exception:
        if exclusive_started:
            try:
                db.execute("ROLLBACK")
            except Exception:
                pass
        raise


def _delete_expired_session_db(db, session_id: int) -> None:
    """Delete an expired session from the database with rollback on failure."""
    try:
        db.execute("DELETE FROM user_sessions WHERE id = ?", (session_id,))
        db.commit()
    except Exception:
        db.rollback()
        raise


def _is_secure_request(request: Request) -> bool:
    """Return True if the request came in over HTTPS or a trusted proxy header."""
    forwarded = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or forwarded.lower() == "https"


class RegisterRequest(BaseModel):
    username: str = Field(max_length=255)
    password: str = Field(max_length=128)
    full_name: str = Field(default="", max_length=255)


class LoginRequest(BaseModel):
    username: str = Field(max_length=255)
    password: str = Field(max_length=128)


class UpdateProfileRequest(BaseModel):
    full_name: Optional[str] = Field(default=None, max_length=255)
    password: Optional[str] = Field(default=None, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(max_length=128)


@limiter.limit("5/hour")
@router.post("/register")
async def register(
    request: Request,
    response: Response,
    body: RegisterRequest,
    db=Depends(get_db),
    _csrf_token: str = Depends(csrf_protect),
):
    """Register a new user. First user becomes superadmin. Issues CSRF token on success."""
    if not body.username or len(body.username) < 3:
        raise HTTPException(
            status_code=400, detail="Username must be at least 3 characters"
        )
    try:
        password_strength_check(body.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Check username uniqueness (case-insensitive)
    existing = await asyncio.to_thread(
        lambda: db.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (body.username,)
        ).fetchone()
    )
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")

    hashed_pw = await async_hash_password(body.password)

    try:
        def _register_db():
            try:
                # Atomic: count read and insert are in the same transaction
                user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                role = "superadmin" if user_count == 0 else "member"
                user_id = db.execute(
                    "INSERT INTO users (username, hashed_password, full_name, role, is_active) VALUES (?, ?, ?, ?, 1)",
                    (body.username, hashed_pw, body.full_name, role),
                ).lastrowid
                assign_user_to_default_vault(db, user_id)
                db.commit()
                return user_id, role
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                raise

        user_id, role = await asyncio.to_thread(_register_db)
    except Exception:
        logger.error("Failed to create user with default assignments", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred. Please try again later.")

    # Create tokens for auto-login
    access_token = create_access_token(user_id, body.username, role)
    refresh_token_raw, refresh_token_hash = create_refresh_token()

    # Store refresh token session
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_MAX_AGE_DAYS)
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    try:
        def _register_session_db():
            try:
                db.execute(
                    "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at, ip_address, user_agent) VALUES (?, ?, ?, ?, ?)",
                    (user_id, refresh_token_hash, expires_at.isoformat(), ip_address, user_agent),
                )
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                raise

        await asyncio.to_thread(_register_session_db)
    except Exception:
        logger.error("Failed to create session", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred. Please try again later.")

    # Set refresh token cookie
    response.set_cookie(
        key=REFRESH_TOKEN_COOKIE_NAME,
        value=refresh_token_raw,
        httponly=True,
        secure=_is_secure_request(request),
        samesite="lax",
        max_age=REFRESH_TOKEN_MAX_AGE_DAYS * 24 * 60 * 60,
        path=refresh_cookie_path(),
    )

    # Issue CSRF token matching login/refresh pattern
    csrf_manager = get_csrf_manager(request)
    issue_csrf_token(response, csrf_manager)

    return {
        "id": user_id,
        "username": body.username,
        "full_name": body.full_name,
        "role": role,
        "is_active": True,
        "access_token": access_token,
        "token_type": "bearer",
        "message": "User registered successfully",
    }


@limiter.limit("10/minute")
@router.post("/login")
async def login(
    request: Request,
    response: Response,
    body: LoginRequest,
    db=Depends(get_db),
    _csrf_token: str = Depends(csrf_protect),
):
    """Login and receive access token + refresh cookie."""
    row = await asyncio.to_thread(
        lambda: db.execute(
            "SELECT id, username, hashed_password, full_name, role, is_active, failed_attempts, locked_until FROM users WHERE username = ? COLLATE NOCASE",
            (body.username,),
        ).fetchone()
    )

    if not row:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    (
        user_id,
        db_username,
        hashed_pw,
        full_name,
        role,
        is_active,
        failed_attempts,
        locked_until,
    ) = row

    if not is_active:
        raise HTTPException(status_code=403, detail="User account is inactive")

    # Lockout check (before password verify)
    if locked_until:
        locked_until_dt = datetime.fromisoformat(locked_until)
        if locked_until_dt > datetime.now(timezone.utc):
            retry_after = int(
                (locked_until_dt - datetime.now(timezone.utc)).total_seconds()
            )
            raise HTTPException(
                status_code=423,
                detail=f"Account locked. Try again in {retry_after // 60} minutes.",
                headers={"Retry-After": str(retry_after)},
            )

    if not await async_verify_password(body.password, hashed_pw):
        await asyncio.to_thread(_record_failed_attempt_db, db, user_id, failed_attempts)
        if failed_attempts + 1 >= 5:
            raise HTTPException(
                status_code=423,
                detail="Account locked due to too many failed attempts. Try again in 15 minutes.",
                headers={"Retry-After": "900"},
            )
        raise HTTPException(status_code=401, detail="Invalid username or password")

    # Create tokens
    access_token = create_access_token(user_id, db_username, role)
    refresh_token_raw, refresh_token_hash = create_refresh_token()

    # Store refresh token session
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_MAX_AGE_DAYS)
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    try:
        def _login_create_session():
            try:
                db.execute(
                    "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at, ip_address, user_agent) VALUES (?, ?, ?, ?, ?)",
                    (
                        user_id,
                        refresh_token_hash,
                        expires_at.isoformat(),
                        ip_address,
                        user_agent,
                    ),
                )
                db.execute(
                    "UPDATE users SET failed_attempts = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), user_id),
                )
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                raise

        await asyncio.to_thread(_login_create_session)
    except Exception:
        logger.error("Failed to create session", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred. Please try again later.")

    # Set httpOnly refresh cookie
    response.set_cookie(
        key=REFRESH_TOKEN_COOKIE_NAME,
        value=refresh_token_raw,
        httponly=True,
        secure=_is_secure_request(request),
        samesite="lax",
        max_age=REFRESH_TOKEN_MAX_AGE_DAYS * 24 * 60 * 60,
        path=refresh_cookie_path(),
    )

    csrf_manager = get_csrf_manager(request)
    issue_csrf_token(response, csrf_manager)

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 15 * 60,
        "user": {
            "id": user_id,
            "username": db_username,
            "full_name": full_name,
            "role": role,
        },
    }


@limiter.limit("30/minute")
@router.post("/refresh")
async def refresh(
    request: Request,
    response: Response,
    refresh_token: Optional[str] = Cookie(None, alias=REFRESH_TOKEN_COOKIE_NAME),
    db=Depends(get_db),
    _csrf_token: str = Depends(csrf_protect),
):
    """Refresh access token using httpOnly refresh cookie. Rotates refresh token."""
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Refresh token missing", headers={"WWW-Authenticate": "Bearer"})

    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()

    # Initial token lookup
    row = await asyncio.to_thread(
        lambda: db.execute(
            """SELECT s.id, s.user_id, s.expires_at, u.username, u.role, u.is_active
               FROM user_sessions s JOIN users u ON s.user_id = u.id
               WHERE s.refresh_token_hash = ?""",
            (token_hash,),
        ).fetchone()
    )

    if not row:
        raise HTTPException(status_code=401, detail="Invalid refresh token", headers={"WWW-Authenticate": "Bearer"})

    session_id, user_id, expires_at_str, username, role, is_active = row

    # Check expiry
    expires_at = datetime.fromisoformat(expires_at_str)
    if expires_at < datetime.now(timezone.utc):
        await asyncio.to_thread(_delete_expired_session_db, db, session_id)
        raise HTTPException(status_code=401, detail="Refresh token expired", headers={"WWW-Authenticate": "Bearer"})

    if not is_active:
        raise HTTPException(status_code=403, detail="User account is inactive")

    # Rotate: delete old session, create new with SQLite serialization
    new_refresh_token_raw, new_refresh_token_hash = create_refresh_token()
    new_expires_at = datetime.now(timezone.utc) + timedelta(
        days=REFRESH_TOKEN_MAX_AGE_DAYS
    )

    try:
        # Wrap entire exclusive-lock block in a single to_thread call
        await asyncio.to_thread(lambda: _rotate_refresh_token_block(
            db, session_id, token_hash, user_id, new_refresh_token_hash, new_expires_at
        ))
    except HTTPException:
        raise
    except Exception:
        logger.error("Session rotation failed", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred. Please try again later.")

    access_token = create_access_token(user_id, username, role)

    response.set_cookie(
        key=REFRESH_TOKEN_COOKIE_NAME,
        value=new_refresh_token_raw,
        httponly=True,
        secure=_is_secure_request(request),
        samesite="lax",
        max_age=REFRESH_TOKEN_MAX_AGE_DAYS * 24 * 60 * 60,
        path=refresh_cookie_path(),
    )

    csrf_manager = get_csrf_manager(request)
    issue_csrf_token(response, csrf_manager)

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 15 * 60,
    }


@router.post("/logout")
async def logout(
    response: Response,
    refresh_token: Optional[str] = Cookie(None, alias=REFRESH_TOKEN_COOKIE_NAME),
    db=Depends(get_db),
    _csrf_token: str = Depends(csrf_protect),
):
    """Logout and revoke refresh token."""
    if refresh_token:
        token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
        try:
            def _logout_db(db_ref, token):
                try:
                    db_ref.execute(
                        "DELETE FROM user_sessions WHERE refresh_token_hash = ?", (token,)
                    )
                    db_ref.commit()
                except Exception:
                    try:
                        db_ref.rollback()
                    except Exception:
                        pass
                    raise

            await asyncio.to_thread(_logout_db, db, token_hash)
        except Exception:
            logger.error("Failed to delete session during logout", exc_info=True)

    response.delete_cookie(key=REFRESH_TOKEN_COOKIE_NAME, path=refresh_cookie_path())
    return {"message": "Logged out successfully"}


@router.get("/setup-status")
async def setup_status(db=Depends(get_db)):
    """Check if initial setup is needed (no users exist). No auth required."""
    user_count = await asyncio.to_thread(
        lambda: db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    )
    return {"needs_setup": user_count == 0}


@router.get("/me")
async def get_me(user: dict = Depends(get_current_active_user)):
    """Get current user profile. Includes must_change_password flag."""
    return {
        "id": user["id"],
        "username": user["username"],
        "full_name": user.get("full_name", ""),
        "role": user["role"],
        "is_active": user["is_active"],
        "must_change_password": user.get("must_change_password", False),
    }


@router.patch("/me")
async def update_me(
    body: UpdateProfileRequest,
    user: dict = Depends(get_current_active_user),
    db=Depends(get_db),
    _csrf_token: str = Depends(csrf_protect),
):
    """Update current user profile (full_name and/or password)."""
    user_id = user["id"]
    updates = []
    params = []

    if body.full_name is not None:
        updates.append("full_name = ?")
        params.append(body.full_name)

    if body.password is not None:
        try:
            password_strength_check(body.password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        hashed_pw = await async_hash_password(body.password)
        updates.append("hashed_password = ?")
        params.append(hashed_pw)

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    params.append(user_id)

    try:
        def _update_me_db():
            try:
                db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", tuple(params))
                db.commit()
                return db.execute(
                    "SELECT id, username, full_name, role, is_active FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                raise

        row = await asyncio.to_thread(_update_me_db)
    except Exception:
        logger.error("Failed to update user", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred. Please try again later.")

    return {
        "id": row[0],
        "username": row[1],
        "full_name": row[2],
        "role": row[3],
        "is_active": bool(row[4]),
        "message": "Profile updated successfully",
    }


@router.post("/change-password")
async def change_password(
    request: Request,
    body: ChangePasswordRequest,
    response: Response,
    user: dict = Depends(get_current_active_user),
    db=Depends(get_db),
    _csrf_token: str = Depends(csrf_protect),
):
    """Change current user's password. Requires current password verification.
    Revokes all existing sessions, clears must_change_password flag, and returns new tokens.
    """
    user_id = user["id"]
    username = user["username"]
    role = user["role"]

    # Fetch current hashed password
    row = await asyncio.to_thread(
        lambda: db.execute(
            "SELECT hashed_password FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    )
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    current_hashed_pw = row[0]

    # Verify current password
    if not await async_verify_password(body.current_password, current_hashed_pw):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    # Validate new password strength
    try:
        password_strength_check(body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Hash new password
    new_hashed_pw = await async_hash_password(body.new_password)

    # Update password and revoke all sessions in a transaction
    try:
        def _change_password_db():
            try:
                db.execute(
                    "UPDATE users SET hashed_password = ? WHERE id = ?",
                    (new_hashed_pw, user_id),
                )
                db.execute(
                    "DELETE FROM user_sessions WHERE user_id = ?",
                    (user_id,),
                )
                db.execute(
                    "UPDATE users SET must_change_password = 0 WHERE id = ?",
                    (user_id,),
                )
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                raise

        await asyncio.to_thread(_change_password_db)
    except Exception:
        logger.error("Failed to change password", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred. Please try again later.")

    # Generate new tokens
    access_token = create_access_token(user_id, username, role)
    refresh_token_raw, refresh_token_hash = create_refresh_token()

    # Store new refresh token session
    expires_at = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_MAX_AGE_DAYS)

    try:
        def _create_session_db():
            try:
                db.execute(
                    "INSERT INTO user_sessions (user_id, refresh_token_hash, expires_at) VALUES (?, ?, ?)",
                    (user_id, refresh_token_hash, expires_at.isoformat()),
                )
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                raise

        await asyncio.to_thread(_create_session_db)
    except Exception:
        logger.error("Failed to create new session", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred. Please try again later.")

    # Set httpOnly refresh cookie
    response.set_cookie(
        key=REFRESH_TOKEN_COOKIE_NAME,
        value=refresh_token_raw,
        httponly=True,
        secure=_is_secure_request(request),
        samesite="lax",
        max_age=REFRESH_TOKEN_MAX_AGE_DAYS * 24 * 60 * 60,
        path=refresh_cookie_path(),
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
    }


@router.get("/sessions")
async def list_sessions(
    user: dict = Depends(get_current_active_user),
    db=Depends(get_db),
):
    """List all active sessions for the current user.

    Returns sessions with: id, ip_address, user_agent, created_at, expires_at
    Never returns token hashes.
    """
    rows = await asyncio.to_thread(
        lambda: db.execute(
            """SELECT id, ip_address, user_agent, created_at, expires_at
               FROM user_sessions
               WHERE user_id = ? AND expires_at > datetime('now')
               ORDER BY created_at DESC""",
            (user["id"],),
        ).fetchall()
    )

    sessions = []
    for row in rows:
        sessions.append(
            {
                "id": row[0],
                "ip_address": row[1],
                "user_agent": row[2],
                "created_at": row[3],
                "expires_at": row[4],
            }
        )

    return {"sessions": sessions}


@router.delete("/sessions/{session_id}")
async def revoke_session(
    session_id: int,
    user: dict = Depends(get_current_active_user),
    db=Depends(get_db),
):
    """Revoke a specific session.

    Users can only revoke their own sessions.
    Returns 204 on success.
    """
    # Verify session belongs to user
    row = await asyncio.to_thread(
        lambda: db.execute(
            "SELECT id FROM user_sessions WHERE id = ? AND user_id = ?",
            (session_id, user["id"]),
        ).fetchone()
    )

    if not row:
        raise HTTPException(status_code=404, detail="Session not found")

    # Delete the session
    try:
        def _revoke_session_db():
            try:
                db.execute(
                    "DELETE FROM user_sessions WHERE id = ?",
                    (session_id,),
                )
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                raise

        await asyncio.to_thread(_revoke_session_db)
    except Exception:
        logger.error("Failed to revoke session", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred. Please try again later.")

    return Response(status_code=204)


@router.delete("/sessions")
async def revoke_all_sessions(
    response: Response,
    request: Request,
    user: dict = Depends(get_current_active_user),
    db=Depends(get_db),
):
    """Revoke all sessions except the current one.

    Rotates the current refresh token and returns new tokens.
    """
    # Get current session from cookie
    refresh_token = request.cookies.get(REFRESH_TOKEN_COOKIE_NAME)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Refresh token missing", headers={"WWW-Authenticate": "Bearer"})

    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()

    # Find current session
    row = await asyncio.to_thread(
        lambda: db.execute(
            "SELECT id FROM user_sessions WHERE refresh_token_hash = ? AND user_id = ?",
            (token_hash, user["id"]),
        ).fetchone()
    )

    if not row:
        raise HTTPException(status_code=401, detail="Invalid refresh token", headers={"WWW-Authenticate": "Bearer"})

    current_session_id = row[0]

    # Generate new refresh token for current session
    new_refresh_token_raw, new_refresh_token_hash = create_refresh_token()
    new_expires_at = datetime.now(timezone.utc) + timedelta(
        days=REFRESH_TOKEN_MAX_AGE_DAYS
    )

    try:
        def _revoke_all_sessions_db():
            try:
                db.execute(
                    "DELETE FROM user_sessions WHERE user_id = ? AND id != ?",
                    (user["id"], current_session_id),
                )
                db.execute(
                    "UPDATE user_sessions SET refresh_token_hash = ?, expires_at = ? WHERE id = ?",
                    (new_refresh_token_hash, new_expires_at.isoformat(), current_session_id),
                )
                db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                raise

        await asyncio.to_thread(_revoke_all_sessions_db)
    except Exception:
        logger.error("Failed to revoke all sessions", exc_info=True)
        raise HTTPException(status_code=500, detail="An internal error occurred. Please try again later.")

    # Generate new access token
    access_token = create_access_token(user["id"], user["username"], user["role"])

    # Set new refresh cookie
    response.set_cookie(
        key=REFRESH_TOKEN_COOKIE_NAME,
        value=new_refresh_token_raw,
        httponly=True,
        secure=_is_secure_request(request),
        samesite="lax",
        max_age=REFRESH_TOKEN_MAX_AGE_DAYS * 24 * 60 * 60,
        path=refresh_cookie_path(),
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 15 * 60,
    }
