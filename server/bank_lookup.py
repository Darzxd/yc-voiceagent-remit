"""Look up Maximo bank users by DNI/document code.

Connects to the bank's RDS (read-only) and returns enough info to confirm a
remittance recipient by name: first name, last name, masked DNI, account
status, balance.

Read-only by design (the DB user has SELECT-only grants). Requires VPN to
reach the RDS endpoint, so this module is meant for the LOCAL bot during
the live demo — Pipecat Cloud cannot reach the private RDS.

If the connection fails (no VPN, DB down) we return a soft error dict so
the agent can degrade gracefully ("can't verify right now — try again").
"""

import asyncio
import os

import aiomysql
from loguru import logger


_POOL: aiomysql.Pool | None = None
_POOL_LOCK = asyncio.Lock()


async def _get_pool() -> aiomysql.Pool | None:
    """Lazy-init a small connection pool. Returns None if Maximo creds are
    not configured (so callers can fall back to mock/error)."""
    global _POOL
    if _POOL is not None:
        return _POOL
    async with _POOL_LOCK:
        if _POOL is not None:
            return _POOL
        host = os.getenv("MAXIMO_DB_HOST", "").strip()
        if not host:
            return None
        try:
            _POOL = await aiomysql.create_pool(
                host=host,
                port=int(os.getenv("MAXIMO_DB_PORT", "3306")),
                user=os.environ["MAXIMO_DB_USER"],
                password=os.environ["MAXIMO_DB_PASSWORD"],
                db=os.environ["MAXIMO_DB_NAME"],
                autocommit=True,
                minsize=1,
                maxsize=3,
                connect_timeout=5,
            )
            logger.info(f"bank_lookup: connected to Maximo DB ({host})")
        except Exception as e:
            logger.error(f"bank_lookup: failed to connect to Maximo DB: {e}")
            return None
    return _POOL


def _mask_dni(dni: str) -> str:
    """8-digit DNI -> '7258****' (first 4, mask the rest)."""
    if not dni:
        return ""
    return dni[:4] + "*" * max(0, len(dni) - 4)


async def lookup_by_dni(dni: str) -> dict:
    """Find a Maximo customer by document_code (DNI).

    Returns one of:
        {"found": True, "full_name": "First Last", "user_id": "<uuid>"}
        {"found": False, "reason": "no_match" | "invalid_dni" |
                                   "db_unavailable" | "db_error"}
    """
    dni = (dni or "").strip()
    if not dni.isdigit() or not (7 <= len(dni) <= 9):
        return {"found": False, "reason": "invalid_dni"}

    pool = await _get_pool()
    if pool is None:
        return {"found": False, "reason": "db_unavailable"}

    sql = (
        "SELECT user_id, name, last_name FROM User "
        "WHERE document_code = %s AND user_state = 'A' "
        "ORDER BY created_at DESC LIMIT 1"
    )
    try:
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, (dni,))
                row = await cur.fetchone()
    except Exception as e:
        logger.error(f"bank_lookup.lookup_by_dni({dni}) query error: {e}")
        return {"found": False, "reason": "db_error"}

    if not row:
        return {"found": False, "reason": "no_match"}

    first = (row.get("name") or "").strip()
    last = (row.get("last_name") or "").strip()
    full_name = f"{first} {last}".strip()
    return {
        "found": True,
        "full_name": full_name,
        "user_id": row.get("user_id"),
    }
