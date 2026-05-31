"""Call Maximo's backoffice GraphQL API: create + auto-approve a recharge.

This is the REAL money path used by the bank's backoffice today:
1. ``createRequest`` mutation registers a recharge request in PENDING state.
2. ``approveTransactionRequest`` flips it to APPROVED, which triggers the
   internal accounting + balance credit on the recipient's Maximo wallet.

The bot fires both back-to-back so the caller hears "done — Diego will get
X soles" while the receiver's wallet actually moves. No human in the loop
because the caller is an authorized board-member identity.

Auth: every request is signed with AWS Sig V4 using IAM temporary
credentials from ``maximo_auth.get_iam_creds()`` (Cognito Identity Pool
exchange). If creds are unavailable (network / VPN / revoked refresh
token) we return a soft-fail dict; the bot then surfaces "we couldn't
reach the bank — try again".
"""

import json
import os
from typing import Any

import aiohttp
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from loguru import logger

import maximo_auth


_CREATE_REQUEST_MUTATION = """\
mutation createRequest(
  $clientId: String!
  $operationType: OperationType!
  $concept: String!
  $amount: Float!
  $observations: String
) {
  createRequest(
    request: {
      client_id: $clientId
      type: RECHARGE
      operation_type: $operationType
      concept: $concept
      amount: $amount
      observations: $observations
    }
  ) {
    id
    creator_id
    moderator_id
    client_id
    type
    operation_type
    concept
    amount
    status
    observations
    rejected_reason
  }
}
"""

_APPROVE_MUTATION = """\
mutation approveTransactionRequest($transactionId: Float!) {
  approveTransactionRequest(id: $transactionId) {
    id
    creator_id
    moderator_id
    client_id
    type
    operation_type
    concept
    amount
    status
    observations
    rejected_reason
  }
}
"""


def _sign_and_build_headers(body: bytes, creds: maximo_auth.IamCreds) -> dict[str, str]:
    url = os.environ["MAXIMO_GQL_URL"]
    region = os.environ.get("MAXIMO_AWS_REGION", "us-east-1")
    req = AWSRequest(
        method="POST",
        url=url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "*/*"},
    )
    SigV4Auth(
        Credentials(creds.access_key, creds.secret_key, creds.session_token),
        "execute-api",
        region,
    ).add_auth(req)
    return dict(req.headers)


async def _gql(operation_name: str, query: str, variables: dict[str, Any]) -> dict:
    creds = await maximo_auth.get_iam_creds()
    if creds is None:
        return {"ok": False, "reason": "auth_unavailable"}

    body_obj = {"operationName": operation_name, "query": query, "variables": variables}
    body = json.dumps(body_obj).encode("utf-8")
    headers = _sign_and_build_headers(body, creds)
    url = os.environ["MAXIMO_GQL_URL"]

    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                url, data=body, headers=headers, timeout=aiohttp.ClientTimeout(total=20)
            ) as r:
                text = await r.text()
                try:
                    data = json.loads(text)
                except Exception:
                    return {"ok": False, "reason": "bad_response", "raw": text[:500]}
                if r.status >= 300:
                    logger.error(
                        f"maximo_recharge {operation_name} http={r.status}: {text[:500]}"
                    )
                    return {"ok": False, "reason": f"http_{r.status}", "body": data}
                if "errors" in data:
                    logger.error(
                        f"maximo_recharge {operation_name} gql errors: {data['errors']}"
                    )
                    return {"ok": False, "reason": "gql_error", "errors": data["errors"]}
                return {"ok": True, "data": data.get("data") or {}}
    except Exception as e:
        logger.error(f"maximo_recharge {operation_name} exception: {e}")
        return {"ok": False, "reason": "exception", "error": str(e)}


async def create_and_approve_recharge(
    *,
    client_id: str,
    amount_pen: float,
    sender_full_name: str,
) -> dict:
    """Fire both mutations back-to-back. Returns a dict the bot can hand to TTS.

    On full success: {ok: True, transaction_id, amount, status: "APPROVED"}
    On partial success (created but not approved): {ok: False, reason: "approve_failed",
        transaction_id, errors}
    On total failure: {ok: False, reason: ...}
    """
    observations = f"Enviado por {sender_full_name} desde Voice Remit"

    # 1) Create the request
    created = await _gql(
        "createRequest",
        _CREATE_REQUEST_MUTATION,
        {
            "clientId": client_id,
            "operationType": "MONEY_SEND",  # "Envío de dinero"
            "concept": "Remesa",
            "amount": float(amount_pen),
            "observations": observations,
        },
    )
    if not created.get("ok"):
        return created

    transaction = (created.get("data") or {}).get("createRequest") or {}
    transaction_id = transaction.get("id")
    if transaction_id is None:
        return {"ok": False, "reason": "no_transaction_id", "raw": created}

    # 2) Approve it
    approved = await _gql(
        "approveTransactionRequest",
        _APPROVE_MUTATION,
        {"transactionId": float(transaction_id)},
    )
    if not approved.get("ok"):
        return {
            "ok": False,
            "reason": "approve_failed",
            "transaction_id": transaction_id,
            "approve_result": approved,
        }

    approved_txn = (approved.get("data") or {}).get("approveTransactionRequest") or {}
    return {
        "ok": True,
        "transaction_id": transaction_id,
        "amount": approved_txn.get("amount") or amount_pen,
        "status": approved_txn.get("status", "APPROVED"),
        "observations": observations,
    }
