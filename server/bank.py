"""Bank rails: exchange rate + recarga (credit recipient's account).

Module M5 of BUILD_PLAN. Real API call when BANK_API_URL is set, otherwise
returns a mock success so the rest of the flow stays testable.

When you have the bank's docs, fill in:
- BANK_API_URL, BANK_API_KEY in .env
- the exact endpoint path (DEFAULT: '/recarga')
- the body field names (DEFAULT: account/amount/currency/reference)
- the auth header (DEFAULT: 'Authorization: Bearer <key>')
"""

import os
import uuid

import aiohttp
from loguru import logger


# Hardcoded demo rate. Replace with a live source when wiring the real bank.
DEMO_RATES = {("USD", "PEN"): 3.52}
DEFAULT_FEE_USD = 2.99  # legacy flat fee (kept for backwards-compat)
DEFAULT_FEE_PCT = 0.032  # 3.2% commission on send, charged to the sender


async def get_exchange_rate(from_currency: str = "USD", to_currency: str = "PEN") -> float:
    """Return the current FX rate. Hardcoded for the demo; in production the
    bank publishes this via its own API and we just pass it through."""
    rate = DEMO_RATES.get((from_currency.upper(), to_currency.upper()))
    if rate is None:
        raise ValueError(f"No rate for {from_currency}/{to_currency}")
    return rate


async def trigger_recarga(
    account: str,
    amount: float,
    currency: str = "PEN",
    reference: str | None = None,
) -> dict:
    """Credit the recipient's account at the partner bank.

    Real call when BANK_API_URL is configured; mock success otherwise so the
    end-to-end flow keeps working even before the bank API is wired.
    """
    ref = reference or f"VR-{uuid.uuid4().hex[:10]}"
    bank_url = os.getenv("BANK_API_URL", "").strip()
    bank_key = os.getenv("BANK_API_KEY", "").strip()

    if not bank_url:
        logger.info(f"bank.trigger_recarga MOCK account={account} amount={amount} {currency} ref={ref}")
        return {"status": "success", "mock": True, "reference": ref, "amount": amount, "currency": currency}

    payload = {
        "account": account,
        "amount": amount,
        "currency": currency,
        "reference": ref,
    }
    headers = {"Authorization": f"Bearer {bank_key}"} if bank_key else {}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{bank_url.rstrip('/')}/recarga",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                body = await response.json()
                logger.info(f"bank.trigger_recarga REAL status={response.status} body={body}")
                if response.status >= 300:
                    return {"status": "failed", "reference": ref, "http_status": response.status, "body": body}
                return {"status": "success", "reference": body.get("reference", ref), "raw": body}
    except Exception as e:
        logger.error(f"bank.trigger_recarga ERROR: {e}")
        return {"status": "error", "reference": ref, "error": str(e)}
