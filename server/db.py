"""Supabase data layer for Voice Remit.

Module M1 of BUILD_PLAN. Synchronous client wrapped in async functions —
single-record reads/writes are fast enough; we do not block the event loop
in any meaningful way for a hackathon demo. Migrate to async (postgrest async
client) if profiling shows it matters.

PIN handling: hashes with SHA-256 (deterministic, no salt) so the user can
re-enter the same PIN and we get the same hash. Hackathon-grade; production
would want bcrypt + per-user salt.
"""

import hashlib
import os

from loguru import logger
from supabase import Client, create_client

_client: Client | None = None


def get_db() -> Client:
    """Lazy singleton Supabase client. Uses service-role key (server-side only)."""
    global _client
    if _client is None:
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_ROLE_KEY"],
        )
    return _client


def _hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()


def check_pin(pin: str, pin_hash: str | None) -> bool:
    if not pin_hash:
        return False
    return _hash_pin(pin) == pin_hash


# ─── Users ────────────────────────────────────────────────────────────────

async def lookup_user(phone_number: str) -> dict | None:
    res = get_db().table("users").select("*").eq("phone_number", phone_number).execute()
    return res.data[0] if res.data else None


async def create_user(phone_number: str) -> dict:
    res = get_db().table("users").insert({"phone_number": phone_number}).execute()
    logger.info(f"db.create_user phone={phone_number}")
    return res.data[0]


async def update_user(user_id: str, **fields) -> dict:
    if "pin" in fields:
        fields["pin_hash"] = _hash_pin(fields.pop("pin"))
    res = get_db().table("users").update(fields).eq("id", user_id).execute()
    return res.data[0]


async def get_or_create_user(phone_number: str) -> dict:
    user = await lookup_user(phone_number)
    if user:
        return user
    return await create_user(phone_number)


async def save_name(phone_number: str, full_name: str) -> dict:
    user = await get_or_create_user(phone_number)
    return await update_user(user["id"], full_name=full_name, onboarding_step="awaiting_id")


async def save_identity(phone_number: str, identity_number: str) -> dict:
    user = await lookup_user(phone_number)
    if not user:
        raise ValueError(f"User not found: {phone_number}")
    return await update_user(
        user["id"],
        identity_number=identity_number,
        onboarding_step="awaiting_keynua",
    )


async def mark_keynua_link_sent(phone_number: str, verification_id: str) -> dict:
    user = await lookup_user(phone_number)
    if not user:
        raise ValueError(f"User not found: {phone_number}")
    return await update_user(
        user["id"],
        keynua_verification_id=verification_id,
        keynua_status="link_sent",
        onboarding_step="awaiting_keynua",
    )


async def mark_keynua_verified(phone_number: str) -> dict:
    user = await lookup_user(phone_number)
    if not user:
        raise ValueError(f"User not found: {phone_number}")
    return await update_user(
        user["id"],
        keynua_status="verified",
        is_verified=True,
        onboarding_step="keynua_verified",
    )


async def save_voiceprint(phone_number: str, voiceprint_id: str) -> dict:
    user = await lookup_user(phone_number)
    if not user:
        raise ValueError(f"User not found: {phone_number}")
    return await update_user(
        user["id"],
        voiceprint_id=voiceprint_id,
        onboarding_step="awaiting_pin",
    )


async def save_pin(phone_number: str, pin: str) -> dict:
    user = await lookup_user(phone_number)
    if not user:
        raise ValueError(f"User not found: {phone_number}")
    return await update_user(
        user["id"],
        pin=pin,
        onboarding_step="awaiting_pin_confirm",
    )


async def confirm_pin(phone_number: str, pin: str) -> dict:
    """Verify the second PIN entry matches the stored hash.

    Returns:
        {"status": "ok", "user": dict}  on match (step → completed)
        {"status": "mismatch"}          on mismatch (step → awaiting_pin, PIN cleared)
    """
    user = await lookup_user(phone_number)
    if not user:
        raise ValueError(f"User not found: {phone_number}")
    if not check_pin(pin, user.get("pin_hash")):
        await update_user(user["id"], pin_hash=None, onboarding_step="awaiting_pin")
        return {"status": "mismatch"}
    updated = await update_user(
        user["id"],
        is_onboarded=True,
        onboarding_step="completed",
    )
    return {"status": "ok", "user": updated}


# ─── Recipients ───────────────────────────────────────────────────────────

async def get_recipients(user_id: str) -> list[dict]:
    res = get_db().table("recipients").select("*").eq("user_id", user_id).execute()
    return res.data or []


async def save_recipient(
    user_id: str,
    full_name: str,
    bank_name: str | None = None,
    account_number: str | None = None,
    phone_number: str | None = None,
    country: str = "PE",
) -> dict:
    row = {"user_id": user_id, "full_name": full_name, "country": country}
    if bank_name:
        row["bank_name"] = bank_name
    if account_number:
        row["account_number"] = account_number
    if phone_number:
        row["phone_number"] = phone_number
    res = get_db().table("recipients").insert(row).execute()
    logger.info(f"db.save_recipient user_id={user_id} name={full_name}")
    return res.data[0]


# ─── Transactions ─────────────────────────────────────────────────────────

async def create_transaction(
    user_id: str,
    recipient_id: str,
    amount_usd: float,
    exchange_rate: float,
    fee_usd: float = 2.99,
) -> dict:
    amount_pen = round(amount_usd * exchange_rate, 2)
    total_usd = round(amount_usd + fee_usd, 2)
    res = get_db().table("transactions").insert({
        "user_id": user_id,
        "recipient_id": recipient_id,
        "amount_usd": amount_usd,
        "amount_pen": amount_pen,
        "exchange_rate": exchange_rate,
        "fee_usd": fee_usd,
        "total_usd": total_usd,
        "status": "pending",
    }).execute()
    return res.data[0]


async def update_transaction(txn_id: str, **fields) -> dict:
    res = get_db().table("transactions").update(fields).eq("id", txn_id).execute()
    return res.data[0]


async def get_last_transaction(user_id: str) -> dict | None:
    """Most recent COMPLETED transaction for this user, with the recipient's
    name joined. Used to greet returning callers with context: "last time you
    sent X to Y — same again?". Returns None if no completed transfer yet.
    """
    res = (
        get_db()
        .table("transactions")
        .select("id, amount_usd, amount_pen, completed_at, recipient_id, recipients(full_name)")
        .eq("user_id", user_id)
        .eq("status", "completed")
        .order("completed_at", desc=True)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    row = res.data[0]
    recipient = row.get("recipients") or {}
    return {
        "transaction_id": row["id"],
        "recipient_name": recipient.get("full_name"),
        "amount_usd": float(row["amount_usd"]) if row.get("amount_usd") is not None else None,
        "amount_pen": float(row["amount_pen"]) if row.get("amount_pen") is not None else None,
        "completed_at": row.get("completed_at"),
    }
