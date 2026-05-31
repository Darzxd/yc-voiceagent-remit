#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Voice Remit — voice-powered remittance agent.

Onboarding flow (this file, current scope):
    new → awaiting_id → awaiting_keynua → keynua_verified
        → awaiting_pin → awaiting_pin_confirm → completed

Send-money flow comes in the next module (BUILD_PLAN §3 M5 / task #11).

Pipeline: Nemotron Speech Streaming STT → Nemotron-3-Super-120B LLM →
Gradium TTS, with onboarding tools registered on the LLM context.

Run::
    uv run bot-remit.py
"""

import asyncio
import datetime
import os
import uuid

import aiohttp
from dotenv import load_dotenv
from loguru import logger

import bank
import bank_lookup
import maximo_recharge
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndTaskFrame, FunctionCallResultProperties, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.dtmf_aggregator import DTMFAggregator
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.runner.types import (
    RunnerArguments,
    SmallWebRTCRunnerArguments,
    WebSocketRunnerArguments,
)
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.gradium.tts import GradiumTTSService
from pipecat.transports.daily.transport import DailyParams, DailyTransport
from pipecatcloud.agent import DailySessionArguments
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.turns.user_turn_strategies import FilterIncompleteUserTurnStrategies
from pipecat.workers.runner import WorkerRunner

import db
from nemotron_llm import VLLMOpenAILLMService
from nvidia_stt import NVidiaWebSocketSTTService

load_dotenv(override=True)


# Default caller identity used over WebRTC (local dev) where the transport
# does not carry a phone number. Over Twilio the real from_number is used.
DEMO_PHONE = os.getenv("DEMO_PHONE", "+15555550100")


async def get_call_info(call_sid: str) -> dict:
    """Fetch call info from Twilio REST API (used only over telephony)."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        logger.warning("Missing Twilio credentials, cannot fetch call info")
        return {}
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
    try:
        auth = aiohttp.BasicAuth(account_sid, auth_token)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=auth) as response:
                if response.status != 200:
                    logger.error(f"Twilio API error ({response.status}): {await response.text()}")
                    return {}
                data = await response.json()
                return {"from_number": data.get("from"), "to_number": data.get("to")}
    except Exception as e:
        logger.error(f"Error fetching call info: {e}")
        return {}


async def run_bot(
    transport: BaseTransport,
    from_number: str | None = None,
    audio_in_sample_rate: int = 16000,
    audio_out_sample_rate: int = 24000,
):
    """Main bot logic."""
    logger.info("Starting Voice Remit bot")

    caller_phone = from_number or DEMO_PHONE
    logger.info(f"Caller phone: {caller_phone}")

    # ── Tools the LLM can call ────────────────────────────────────────────
    #
    # All tools close over `caller_phone` so the LLM never has to pass it.
    # Onboarding only in this file. Send-money lands in the next module.

    async def lookup_user(params: FunctionCallParams) -> None:
        """Look up the caller in our system. Call this FIRST on every call to
        route the conversation based on their onboarding_step. Also returns
        their most recent completed transfer so you can greet them with
        context (e.g. "last time you sent $X to Y — same again?")."""
        user = await db.lookup_user(caller_phone)
        if not user:
            user = await db.create_user(caller_phone)
        last_transfer = None
        if user.get("is_onboarded"):
            last_transfer = await db.get_last_transaction(user["id"])
        await params.result_callback({
            "found": True,
            "full_name": user.get("full_name"),
            "first_name": (user.get("full_name") or "").split(" ")[0] or None,
            "onboarding_step": user.get("onboarding_step"),
            "is_verified": bool(user.get("is_verified")),
            "is_onboarded": bool(user.get("is_onboarded")),
            "keynua_status": user.get("keynua_status"),
            "has_voiceprint": bool(user.get("voiceprint_id")),
            "last_transfer": last_transfer,  # null if no completed transfers yet
        })

    async def save_user_name(params: FunctionCallParams, full_name: str) -> None:
        """Save the caller's full legal name. Call this only when they give a
        real name (first + last). Greetings like 'hi' are NOT names — ask again.

        Args:
            full_name: The caller's full legal name, e.g. 'Maria Garcia'.
        """
        await db.save_name(caller_phone, full_name)
        await params.result_callback({"status": "ok", "next_step": "awaiting_id"})

    async def save_user_id(params: FunctionCallParams, identity_number: str) -> None:
        """Save the caller's identity document number (DNI / passport / SSN).

        Args:
            identity_number: The ID number as the caller said it, digits only.
        """
        await db.save_identity(caller_phone, identity_number)
        await params.result_callback({"status": "ok", "next_step": "awaiting_keynua"})

    async def send_verification_link(params: FunctionCallParams) -> None:
        """Send the KYC verification + payment-method link to the caller's
        WhatsApp. Call this once you have their name and ID."""
        # TODO(task #5): wire to real Keynua + Twilio WhatsApp. For now we
        # generate a fake verification id and persist the state transition so
        # the rest of the flow is testable end-to-end via text/voice.
        verification_id = f"kn_{uuid.uuid4().hex[:12]}"
        await db.mark_keynua_link_sent(caller_phone, verification_id)
        logger.info(f"send_verification_link MOCK phone={caller_phone} id={verification_id}")
        await params.result_callback({
            "status": "ok",
            "message": "Verification link sent to WhatsApp (mocked)",
        })

    async def check_verification_status(params: FunctionCallParams) -> None:
        """Check whether the caller has completed KYC + card setup on the link
        we sent. Call this when they say they finished, or to retry after
        'still pending'."""
        # TODO(task #5): poll real Keynua. For now we auto-advance so the flow
        # is testable without leaving the Playground.
        await db.mark_keynua_verified(caller_phone)
        logger.info(f"check_verification_status MOCK phone={caller_phone} → verified")
        await params.result_callback({"status": "verified"})

    async def register_voiceprint(params: FunctionCallParams) -> None:
        """Register the caller's voiceprint after they speak the authorization
        phrase. Only call AFTER they have actually said the phrase."""
        # Voiceprint is simulated end-to-end (BUILD_PLAN §3 M8). We store a
        # mock id so the onboarding_step advances correctly.
        voiceprint_id = f"vp_{uuid.uuid4().hex[:12]}"
        await db.save_voiceprint(caller_phone, voiceprint_id)
        logger.info(f"register_voiceprint MOCK phone={caller_phone} id={voiceprint_id}")
        await params.result_callback({"status": "ok", "voiceprint_id": voiceprint_id})

    async def save_pin(params: FunctionCallParams, pin: str) -> None:
        """Save the caller's 4-digit security PIN (first entry). They will
        re-enter it once for confirmation.

        Args:
            pin: Exactly 4 digits as a string (e.g. '1234'). Accept both
                spoken digits and DTMF input — pass them through as-is.
        """
        if len(pin) != 4 or not pin.isdigit():
            await params.result_callback({
                "status": "error",
                "message": "PIN must be exactly 4 digits",
            })
            return
        await db.save_pin(caller_phone, pin)
        await params.result_callback({"status": "ok", "next_step": "awaiting_pin_confirm"})

    async def confirm_pin(params: FunctionCallParams, pin: str) -> None:
        """Confirm the PIN by entering it a second time. If it matches the
        first entry, onboarding completes.

        Args:
            pin: Exactly 4 digits as a string. Pass through as-is.
        """
        if len(pin) != 4 or not pin.isdigit():
            await params.result_callback({
                "status": "error",
                "message": "PIN must be exactly 4 digits",
            })
            return
        result = await db.confirm_pin(caller_phone, pin)
        if result["status"] == "mismatch":
            await params.result_callback({
                "status": "mismatch",
                "message": "PINs don't match. Ask them for the PIN again from scratch.",
            })
        else:
            await params.result_callback({
                "status": "ok",
                "message": "Onboarding complete.",
            })

    # ── Send-money tools (only used once onboarding_step == 'completed') ──

    async def lookup_recipient_by_dni(params: FunctionCallParams, dni: str) -> None:
        """Look up a Peruvian recipient on Máximo by their DNI (national ID).

        Use this once the caller has chosen Peru + Máximo and given you a DNI.
        Returns the recipient's full name from the Máximo customer DB so the
        agent can read it back for confirmation ("I found [name], confirm?").

        Args:
            dni: 7–9 digit Peruvian DNI as a string. Pass it as digits only,
                no spaces or dashes. If the caller said the DNI in words,
                convert to digits before calling.
        """
        result = await bank_lookup.lookup_by_dni(dni)
        await params.result_callback(result)

    async def get_recipients(params: FunctionCallParams) -> None:
        """List the caller's saved recipients (people they can send money to).
        Call this when the caller mentions sending money."""
        user = await db.lookup_user(caller_phone)
        if not user:
            await params.result_callback({"recipients": []})
            return
        recipients = await db.get_recipients(user["id"])
        await params.result_callback({
            "recipients": [
                {"name": r["full_name"], "country": r.get("country"), "bank": r.get("bank_name")}
                for r in recipients
            ]
        })

    async def get_quote(params: FunctionCallParams, amount_usd: float) -> None:
        """Quote a money transfer: today's USD→PEN rate, equivalent in soles,
        flat fee, and total to charge.

        Args:
            amount_usd: Amount the caller wants to send, in US dollars
                (e.g. 200 for two hundred dollars).
        """
        rate = await bank.get_exchange_rate("USD", "PEN")
        fee = round(amount_usd * bank.DEFAULT_FEE_PCT, 2)  # 3.2% commission
        amount_pen = round(amount_usd * rate, 2)
        total = round(amount_usd + fee, 2)
        await params.result_callback({
            "amount_usd": amount_usd,
            "rate": rate,
            "amount_pen": amount_pen,
            "fee_usd": fee,
            "fee_pct": "3.2%",
            "total_usd": total,
            "currency": "PEN",
        })

    async def verify_pin(params: FunctionCallParams, pin: str) -> None:
        """Verify the caller's PIN BEFORE running a transfer.

        Args:
            pin: 4-digit PIN as a string. Pass through whatever the caller
                says or types (spoken digits, DTMF). No need to clean it.
        """
        if len(pin) != 4 or not pin.isdigit():
            await params.result_callback({
                "status": "error",
                "message": "PIN must be exactly 4 digits",
            })
            return
        user = await db.lookup_user(caller_phone)
        if not user or not db.check_pin(pin, user.get("pin_hash")):
            await params.result_callback({"status": "error", "message": "Wrong PIN"})
            return
        await params.result_callback({"status": "ok"})

    async def create_transfer(
        params: FunctionCallParams,
        recipient_full_name: str,
        recipient_user_id: str,
        amount_usd: float,
    ) -> None:
        """Move REAL money: create + auto-approve a recharge on the recipient's
        Maximo Wallet. ONLY call AFTER verify_pin returned status=ok AND after
        lookup_recipient_by_dni confirmed the recipient.

        Args:
            recipient_full_name: The recipient's full name exactly as
                returned by lookup_recipient_by_dni (used in the audit trail).
            recipient_user_id: The recipient's Maximo user_id (UUID) returned
                by lookup_recipient_by_dni. This is the `clientId` for the
                bank's mutation.
            amount_usd: Amount the sender is sending, in US dollars.
        """
        rate = await bank.get_exchange_rate("USD", "PEN")
        amount_pen = round(amount_usd * rate, 2)
        fee_usd = round(amount_usd * bank.DEFAULT_FEE_PCT, 2)
        total_usd = round(amount_usd + fee_usd, 2)

        sender = await db.lookup_user(caller_phone)
        sender_full_name = (sender or {}).get("full_name") or "Voice Remit caller"

        # Record locally first (so we have an internal id if the bank call dies)
        local_txn = None
        if sender:
            try:
                local_txn = await db.create_transaction(
                    user_id=sender["id"],
                    recipient_id=None,
                    amount_usd=amount_usd,
                    exchange_rate=rate,
                    fee_usd=fee_usd,
                )
            except Exception as e:
                logger.warning(f"create_transfer local txn write failed: {e}")

        # Hit Maximo's GraphQL: createRequest + approveTransactionRequest.
        # Wrap in asyncio.shield so a mid-call user interruption (which causes
        # Pipecat to cancel this tool function) cannot kill the API request
        # half-way through. Money operations must be atomic — the underlying
        # task will run to completion even if our awaiting frame is cancelled.
        maximo_task = asyncio.create_task(
            maximo_recharge.create_and_approve_recharge(
                client_id=recipient_user_id,
                amount_pen=amount_pen,
                sender_full_name=sender_full_name,
            )
        )
        try:
            result = await asyncio.shield(maximo_task)
        except asyncio.CancelledError:
            # The LLM cancelled us, but the recharge task keeps running. Let it
            # finish so the money still moves; we just can't tell the caller.
            logger.warning(
                "create_transfer cancelled by LLM — letting Maximo task finish in "
                "background so the recharge still completes."
            )
            try:
                result = await maximo_task
                logger.info(
                    f"create_transfer background result: maximo={result.get('ok')} "
                    f"txn={result.get('transaction_id')}"
                )
            except Exception as e:
                logger.error(f"create_transfer background task error: {e}")
            raise
        logger.info(
            f"create_transfer phone={caller_phone} to={recipient_full_name} "
            f"({recipient_user_id}) usd={amount_usd} pen={amount_pen} "
            f"fee={fee_usd} maximo={result.get('ok')}"
        )

        if not result.get("ok"):
            if local_txn:
                try:
                    await db.update_transaction(local_txn["id"], status="failed")
                except Exception:
                    pass
            await params.result_callback({
                "status": "failed",
                "reason": result.get("reason"),
                "message": "Could not complete the transfer with Máximo.",
            })
            return

        # Success: update local record with the Maximo transaction id
        if local_txn:
            try:
                await db.update_transaction(
                    local_txn["id"],
                    status="completed",
                    bank_reference=str(result.get("transaction_id")),
                    pin_confirmed=True,
                    completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                )
            except Exception as e:
                logger.warning(f"create_transfer local txn update failed: {e}")

        await params.result_callback({
            "status": "completed",
            "recipient_name": recipient_full_name,
            "amount_usd": amount_usd,
            "amount_pen": amount_pen,
            "rate": rate,
            "fee_usd": fee_usd,
            "fee_pct": "3.2%",
            "total_usd": total_usd,
            "maximo_transaction_id": result.get("transaction_id"),
        })

    async def end_call(params: FunctionCallParams) -> None:
        """End the call. Only call AFTER you have said goodbye in the same
        turn. The pipeline flushes any queued speech and then hangs up."""
        logger.info("end_call invoked — pushing EndTaskFrame upstream")
        await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
        await params.result_callback(
            {"ok": True}, properties=FunctionCallResultProperties(run_llm=False)
        )

    tool_functions = [
        lookup_user,
        save_user_name,
        save_user_id,
        send_verification_link,
        check_verification_status,
        register_voiceprint,
        save_pin,
        confirm_pin,
        get_recipients,
        lookup_recipient_by_dni,
        get_quote,
        verify_pin,
        create_transfer,
        end_call,
    ]
    tools = ToolsSchema(standard_tools=tool_functions)

    # ── System instruction ────────────────────────────────────────────────

    system_instruction = (
        "You are Liam, a warm, casual voice agent for Voice Remit — a service that "
        "helps migrants send money home to family abroad. Talk like a friendly human, "
        "not a robot. The call is in English.\n\n"
        "IDENTITY\n"
        "- Your name is Liam. Your introduction is short and natural: \"Hi [first name], "
        "this is Liam from Voice Remit.\" Only introduce yourself once, on the greeting.\n"
        "- Voice Remit lets people send US dollars from the US to their family abroad "
        "in under a minute. Today's corridor is USD → PEN (Peruvian soles) at three "
        "point five two, with a commission of three point two percent on the amount "
        "(charged to the sender, never separate from the get_quote result). ALWAYS "
        "use the EXACT fee_usd and total_usd returned by get_quote — never invent.\n\n"
        "STYLE\n"
        "- 1–2 short sentences per turn. This is a phone call, not a chat.\n"
        "- Ask ONE thing at a time.\n"
        '- Skip filler openers ("Absolutely!", "Perfect!", "I\'d be happy to").\n'
        "- Use contractions. Fragments are fine.\n"
        "- NEVER output internal thoughts, stage directions, or narration. Only the "
        "words you want spoken aloud.\n"
        "- NEVER mention fraud, money laundering, or compliance reasons.\n"
        "- ALWAYS use the tools. Never guess user data.\n\n"
        "🚨 HARD RULES (these are compliance-grade — breaking them is a FAIL)\n\n"
        "Rule 0 — NEVER REASON OUT LOUD. If you need to think, do it silently. "
        "Your response MUST be only the final answer the caller hears. NEVER say "
        "things like \"Let me think...\", \"Wait, are you saying...\", \"Let me put "
        "those digits together...\", \"After checking...\", \"I'll look up...\", "
        "\"Actually...\", \"Hmm...\". NEVER emit `<think>` tags or any meta-commentary. "
        "If you wrote any analysis or chain of thought, DELETE IT before responding.\n"
        "  ✅ Correct: \"I found Ignacio Rojas on Máximo. Is that right?\"\n"
        "  ❌ Wrong:  \"Let me check that DNI... wait, that's only 7 digits... try "
        "again?\"\n\n"
        "Rule 0.1 — DNI is collected ACROSS MULTIPLE TURNS. The caller will often "
        "say 8 digits in chunks (\"ten thirty\" then \"eight eight nine six\"). "
        "ACCUMULATE digits across turns SILENTLY. Do not call lookup_recipient_by_dni "
        "until you have 8 continuous digits. While accumulating, say ONLY a tiny "
        "encouragement like \"Go ahead\" or \"Keep going\" — NOT a recap of what you "
        "heard so far. Once you have 8 digits, IMMEDIATELY call the tool. If the "
        "caller eventually gives more than 8 digits, take the LAST 8.\n\n"
        "Rule A — ALWAYS speak amounts in WORDS, never digits or currency symbols.\n"
        "  ✅ Correct:  \"two hundred dollars\"     \"seven hundred forty soles\"\n"
        "              \"two dollars and ninety-nine cents\"\n"
        "              \"thirty-seven soles\"      \"a hundred eighty-five soles\"\n"
        "  ❌ Wrong:    \"$200\"   \"740 sols\"   \"$2.99\"   \"37 soles\"\n"
        "  This applies to the dollar amount, the soles amount, AND the fee, EVERY "
        "time you mention them. Read decimals as words too (\"two point nine nine\" or "
        "\"two ninety-nine\"). If you slip and say a digit, do NOT correct yourself "
        "out loud — just continue.\n\n"
        "Rule B — NEVER repeat the caller's PIN aloud. Not even partially.\n"
        "  When the caller gives a PIN, immediately call verify_pin. Do NOT say "
        "\"got it, your PIN is two-two-one-two\" or any variant. Just verify "
        "silently and react to the result.\n"
        "  ✅ After verify_pin ok:   \"PIN confirmed — sending now.\"\n"
        "  ❌ Wrong:                 \"Got it, two two one two. Verifying...\"\n\n"
        "Rule C — Only send money to recipients in the caller's saved list. If they "
        "name someone not in get_recipients, refuse and ask who from their list. "
        "Never invent or create new recipients on the fly.\n\n"
        "FLOW — CALL lookup_user FIRST\n"
        "Every call starts by calling lookup_user. The response includes "
        "`first_name`, `onboarding_step`, and `last_transfer` (the caller's most "
        "recent completed transfer, or null). Route based on `onboarding_step`:\n\n"
        "- **new** (or no full_name yet): \"Hey! This is Liam from Voice Remit — we "
        "help people send money home. What's your full name?\" Wait for a real name "
        "(first + last). Then call save_user_name.\n"
        "- **awaiting_id**: \"Thanks, [first name]. What's your ID or document number?\" "
        "Call save_user_id with the digits.\n"
        "- **awaiting_keynua** with keynua_status='pending' or null: Call "
        "send_verification_link, then say: \"I just texted you a link on WhatsApp. "
        "Verify your face and add a card, then call me back.\"\n"
        "- **awaiting_keynua** when the user says they're done: Call "
        "check_verification_status. If verified, continue. If still pending, say "
        "\"Looks like it's not done yet — check your WhatsApp.\"\n"
        "- **keynua_verified** or **awaiting_voiceprint**: \"You're verified. One last "
        "thing — say: 'I authorize Voice Remit to process my transactions.'\" After "
        "they say it, call register_voiceprint.\n"
        "- **awaiting_pin**: \"Set a four-digit PIN. Say it or enter it.\" Take the "
        "digits and call save_pin.\n"
        "- **awaiting_pin_confirm**: \"One more time — same four digits.\" Call "
        "confirm_pin. On mismatch: \"Those didn't match — let's try again from "
        "scratch.\" On ok: \"You're all set. Want to send some money right now?\"\n"
        "- **completed**: Greet by first name with CONTEXT from `last_transfer`:\n"
        "    • If `last_transfer` is null (first time sending): \"Hi [first name], "
        "this is Liam from Voice Remit. Who do you want to send money to today?\"\n"
        "    • If `last_transfer` exists, ACKNOWLEDGE it briefly but DO NOT offer "
        "a one-tap repeat — we still go through country/bank/DNI for safety. "
        "Example: \"Hi Ignacio, welcome back. Last time you sent ten dollars to "
        "Estefano. Want to send to someone else today, or the same person?\" "
        "Either way, go to step 1 of SEND-MONEY below to confirm country.\n\n"
        "SEND-MONEY FLOW (dynamic country → bank → DNI lookup)\n"
        "1. **Country**: Ask \"What country do you want to send money to today?\" "
        "Only Peru is supported right now. If they say anything else: \"Right now "
        "we only support Peru — does Peru work?\"\n"
        "2. **Bank confirmation**: Once they say Peru, say \"Perfect — we have "
        "Máximo Wallet available in Peru. Want to send through Máximo?\" Wait for "
        "yes/ok.\n"
        "3. **DNI (via keypad)**: \"Please type the recipient's eight-digit DNI on "
        "your phone keypad.\" Wait silently for the caller to press digits. The "
        "DTMF aggregator will deliver the whole 8-digit string as ONE user message "
        "(e.g. user message becomes \"72584789\"). When that arrives, IMMEDIATELY "
        "call lookup_recipient_by_dni(dni) with those digits. If the caller speaks "
        "the digits instead of pressing them, accept it too — convert to a continuous "
        "digit string and call the tool. \n"
        "    • If `found: true`: Read back the name for confirmation: \"I found "
        "[full_name] on Máximo. Is that right?\" Wait for yes.\n"
        "    • If `found: false` and reason is `no_match`: \"I don't see anyone "
        "with that DNI on Máximo — can you repeat it?\" Stay in this step.\n"
        "    • If `found: false` and reason is `invalid_dni`: \"That doesn't look "
        "like a valid DNI — DNI is eight digits. Try again?\"\n"
        "    • If `found: false` and reason is `db_unavailable` or `db_error`: "
        "\"I can't reach Máximo right now — let's try again in a moment.\"\n"
        "4. **Amount**: After they confirm the name, ask \"How much in dollars do "
        "you want to send?\" When they say the amount, call get_quote(amount_usd).\n"
        "5. **Quote (Rule A — words! use the EXACT tool values)**: After get_quote "
        "returns, read its numbers IN WORDS — never make up amounts. ALWAYS mention "
        "that the total will be debited via Stripe ACH from the caller's linked "
        "Chase checking account ending in four five two one. Template: "
        "\"<amount_usd in words> dollars to <recipient first name> — at today's rate "
        "that's <amount_pen in words> soles. Fee <fee_usd in words> "
        "(three point two percent commission), total <total_usd in words>. We'll "
        "debit that from your Chase checking ending in four five two one. Your PIN "
        "to confirm?\" Example with amount_usd=200, amount_pen=704, fee_usd=6.40, "
        "total_usd=206.40 → \"Two hundred dollars to Diego — at today's rate that's "
        "seven hundred four soles. Fee six dollars and forty cents (three point two "
        "percent commission), total two hundred six dollars and forty cents. We'll "
        "debit that from your Chase checking ending in four five two one. Your PIN "
        "to confirm?\". Example with amount_usd=1, amount_pen=3.52, fee_usd=0.03, "
        "total_usd=1.03 → \"One dollar to Isabel — at today's rate that's three "
        "point five two soles. Fee three cents (three point two percent commission), "
        "total one dollar and three cents. We'll debit that from your Chase checking "
        "ending in four five two one. Your PIN to confirm?\".\n"
        "6. **PIN (via keypad — Rule B never echo!)**: \"Please type your "
        "four-digit PIN on the keypad to confirm.\" Wait silently. When the "
        "4-digit string arrives, IMMEDIATELY call verify_pin(pin). DO NOT "
        "repeat the digits aloud — not even partially. On error: \"That's not "
        "it — try again.\" Stay in this step. On ok: continue.\n"
        "7. **Transfer**: FIRST emit a short status sentence like \"PIN confirmed "
        "— sending now, give me just a moment.\" so the caller knows we're working "
        "and won't talk over the API call. In the SAME turn, call create_transfer "
        "with THREE args from the lookup_recipient_by_dni result and the quote: "
        "`recipient_full_name` = `full_name` from the lookup, "
        "`recipient_user_id` = `user_id` from the lookup, and "
        "`amount_usd` = the dollar amount the caller is sending. "
        "On status=completed: confirm IN WORDS: \"Done — [first name] just got "
        "[amount_pen in words] soles on their Máximo Wallet.\" "
        "On status=failed: \"Something went wrong on our end — let's try again in "
        "a moment.\"\n"
        "8. \"Anything else?\" If no, short goodbye and call end_call.\n\n"
        "When the caller says goodbye OR there's nothing else to do: say a short "
        "closing line (\"Talk soon — bye!\") AND call end_call in the SAME turn. "
        "Never call end_call without saying goodbye first."
    )

    stt = NVidiaWebSocketSTTService(
        url=os.getenv("NVIDIA_ASR_URL", "ws://192.168.7.228:8081"),
        strip_interim_prefix=True,
    )

    enable_thinking = os.getenv("NEMOTRON_ENABLE_THINKING", "false").lower() == "true"
    llm = VLLMOpenAILLMService(
        api_key=os.getenv("NEMOTRON_LLM_API_KEY", "EMPTY"),
        base_url=os.getenv("NEMOTRON_LLM_URL", "http://192.168.7.228:8000/v1"),
        settings=VLLMOpenAILLMService.Settings(
            model=os.getenv("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super"),
            system_instruction=system_instruction,
            extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": enable_thinking}}},
        ),
    )

    tts = GradiumTTSService(
        api_key=os.environ["GRADIUM_API_KEY"],
        settings=GradiumTTSService.Settings(
            voice=os.getenv("GRADIUM_VOICE_ID", "_6Aslh2DxfmnRLmP"),
        ),
    )

    for fn in tool_functions:
        llm.register_direct_function(fn)

    context = LLMContext(tools=tools)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=FilterIncompleteUserTurnStrategies(),
        ),
    )

    # DTMFAggregator converts keypad presses (Twilio telephony only — no-op
    # over WebRTC) into a text TranscriptionFrame after a short idle timeout.
    # That way the LLM sees "12345678" as if the caller had said it, with zero
    # STT error.  We use it for DNI (8 digits) and PIN (4 digits).
    dtmf = DTMFAggregator(timeout=2.0)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            dtmf,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=audio_in_sample_rate,
            audio_out_sample_rate=audio_out_sample_rate,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"Client connected, caller_phone={caller_phone}")
        # Tell the LLM to call lookup_user first and route from there.
        context.add_message({
            "role": "user",
            "content": (
                f"A caller just connected from {caller_phone}. Call lookup_user "
                "now to fetch their state, then greet them and route the "
                "conversation based on their onboarding_step (see system rules)."
            ),
        })
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""
    from_number: str | None = None
    transport_overrides: dict = {}

    if os.environ.get("ENV") != "local":
        from pipecat.audio.filters.krisp_viva_filter import KrispVivaFilter
        krisp_filter = KrispVivaFilter()
    else:
        krisp_filter = None

    match runner_args:
        case SmallWebRTCRunnerArguments():
            webrtc_connection: SmallWebRTCConnection = runner_args.webrtc_connection
            transport = SmallWebRTCTransport(
                webrtc_connection=webrtc_connection,
                params=TransportParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                ),
            )
        case DailySessionArguments():
            # Pipecat Cloud uses Daily.co rooms. Both the bot and the testing
            # agent (Cekura's caller) join the same room over WebRTC.
            transport = DailyTransport(
                room_url=runner_args.room_url,
                token=runner_args.token,
                bot_name="Voice Remit",
                params=DailyParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                    vad_analyzer=SileroVADAnalyzer(),
                ),
            )
        case WebSocketRunnerArguments():
            transport_overrides["audio_in_sample_rate"] = 8000
            transport_overrides["audio_out_sample_rate"] = 8000

            try:
                _, call_data = await parse_telephony_websocket(runner_args.websocket)
            except Exception as e:
                logger.error(
                    f"Twilio handshake failed before we got any frames: {e}. "
                    "Probably the caller hung up immediately, or someone "
                    "connected to /ws without sending Twilio frames (e.g. a "
                    "browser test). Closing the socket cleanly."
                )
                try:
                    await runner_args.websocket.close()
                except Exception:
                    pass
                return
            call_id = call_data.get("call_id") if call_data else None
            stream_id = call_data.get("stream_id") if call_data else None
            if not call_id or not stream_id:
                logger.error(
                    f"Twilio handshake incomplete — got call_data={call_data}. "
                    "Most likely the TwiML <Stream> URL is wrong or the TwiML "
                    "itself is malformed (e.g. stray text outside tags). The "
                    "WebSocket will be closed without setting up the pipeline."
                )
                try:
                    await runner_args.websocket.close()
                except Exception:
                    pass
                return

            call_info = await get_call_info(call_id)
            if call_info:
                from_number = call_info.get("from_number")
                logger.info(f"Call from: {from_number} to: {call_info.get('to_number')}")

            serializer = TwilioFrameSerializer(
                stream_sid=stream_id,
                call_sid=call_id,
                account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
                auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
            )
            transport = FastAPIWebsocketTransport(
                websocket=runner_args.websocket,
                params=FastAPIWebsocketParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                    add_wav_header=False,
                    serializer=serializer,
                ),
            )
        case _:
            logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
            return

    await run_bot(transport, from_number=from_number, **transport_overrides)


if __name__ == "__main__":
    from pipecat.runner.run import main
    main()
