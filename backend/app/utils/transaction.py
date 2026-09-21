"""Transaction context manager for SQLite connections via the connection pool."""

import asyncio
import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def db_transaction(pool):
    """Async context manager for SQLite transactions with auto-commit/rollback.

    Usage:
        async with db_transaction(pool) as conn:
            conn.execute("INSERT INTO ...")
            conn.execute("UPDATE ...")
        # Auto-committed on clean exit, auto-rolled-back on exception.
    """
    conn = await pool.get_connection_async()
    try:
        yield conn
        await asyncio.to_thread(conn.commit)
    except Exception:
        try:
            await asyncio.to_thread(conn.rollback)
        except Exception as rollback_err:
            logger.warning("Rollback failed: %s", rollback_err)
        raise
    finally:
        pool.release_connection(conn)
