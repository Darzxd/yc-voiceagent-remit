# Voice Remit — the Stripe of voice remittances

> Migrants call a phone number, talk to an agent, send money home in under 60 seconds.
> **Real money. Real bank rails.** No app, no forms, no friction.

YC Voice Agents Hackathon — May 30, 2026.

---

## 1. What is this?

**Voice Remit** is a voice agent that lets a migrant in the US wire money to a relative in Peru by **literally calling a phone number** and talking to "Liam," our agent. In about a minute, the caller:

1. Picks a country (Peru today)
2. Identifies the recipient by **DNI** (Peruvian national ID — typed via keypad)
3. Says the amount in USD
4. Confirms with a 4-digit PIN
5. Hears *"Done — Diego just got three point five two soles on their Máximo Wallet."*

…and the recipient's wallet at **Máximo Bank (Perú)** actually credits **immediately**. The money moves through Máximo's real treasury account via their backoffice GraphQL API — signed with AWS Sig V4 + Cognito Identity Pool credentials — using the bank's `createRequest` + `approveTransactionRequest` mutations.

The sender's leg is a Stripe ACH debit (*"Chase checking ending in 4521"* in the demo).

**Real money flow, real bank, real voice. That's the demo.**

---

## 2. Demo video 

**https://drive.google.com/file/d/1nvENvf5pfr4ehwOc8j1bboyk-EyfaKmH/view?usp=sharing**


<p align="center">
  <img src="https://github.com/user-attachments/assets/a8d9f715-561b-4cb8-8ec1-4e86ebb2ac77" width="400" alt="Voice Remit demo" />
</p>
---

## 3. How we used Cekura, Nemotron, and Pipecat

### 🧪 Cekura — the evaluation + auto-improvement loop (the centerpiece)

Cekura is the differentiator. A bank moving real money cannot ship a voice agent without a regulator-grade audit trail of what the bot does in production, AND a way to prove the bot's quality went UP, not down, after every change.

**What we tried to accomplish in testing:**

- Generated **15 scenarios** covering: happy path (DNI lookup → quote → PIN → transfer), edge cases (wrong DNI, mid-call interruption, "I changed my mind"), and adversarial / red-team (PIN brute-force, "send to my own account," compliance-bait questions like "is this laundering?").
- Built a **custom Compliance Score** metric that grades each conversation against bank-grade rules: *never echo the PIN, never reason out loud, always read amounts in words, never name a country we don't support, never fabricate exchange rates*.

**How much we improved performance:**

|                | Pass rate         | Compliance Score | Notes |
|----------------|-------------------|------------------|-------|
| **Baseline**   | **0 / 15**        | failing on Rule A (digits in numerals), Rule B (PIN echoed), Rule 0 (reasoning leaks) | The agent kept saying "$200" instead of "two hundred dollars" and saying "got it, your PIN is 2-2-1-2" |
| **After 1 loop** | **8 / 15 (53%)** ✅ | **8.4 / 10**     | We added Rules 0, 0.1, A, B + a streaming `<think>` filter in `server/nemotron_llm.py` |

The flow: Cekura ran the suite → flagged failures with reasoning → we used its rule-suggestion feature to draft prompt hardening (the four **"🚨 HARD RULES"** you see in `server/bot-remit.py`) → reran the suite. The pattern *test → diagnose → patch system prompt → re-test* is now repeatable for every new flow we ship.

### 🧠 Nemotron — open-weights brain + ears

- **Nemotron Speech Streaming STT** (English-only) at `ws://44.241.251.184:8080` — fast, accurate for natural speech. We hard-reset the stream every utterance for low-latency turn ends.
- **Nemotron-3-Super-120B** via a vLLM OpenAI-compatible endpoint for the LLM brain — used for tool-calling, conversation routing, and compliance-aware response generation. Tool calls on every step (`lookup_user`, `lookup_recipient_by_dni`, `get_quote`, `verify_pin`, `create_transfer`, `end_call`).

### 🎙️ Pipecat — the pipeline

- Pipeline: `Transport → STT → DTMFAggregator → LLMContext → Nemotron LLM → Gradium TTS → Transport`
- **`DTMFAggregator`** for DNI + PIN — caller presses the digits on the keypad and Pipecat hands the LLM a clean 8/4-digit string. Zero STT error on the safety-critical numbers.
- **Dual-transport**: same bot answers from `SmallWebRTCTransport` (browser playground at `localhost:7860/client`) AND `FastAPIWebsocketTransport` with Twilio serializer (real phone via `+1 833 907 1804`).
- Custom **`_ThinkStripper`** wrapper around `VLLMOpenAILLMService` (`server/nemotron_llm.py`) that catches `<think>…</think>` chain-of-thought leaks at the streaming layer **before they ever reach TTS** — defense in depth against the model thinking out loud.

---

## 4. What we built **new** during the hackathon

100% of the work below was written on **2026-05-30** between 9 AM and 6 PM:

- 🟢 **End-to-end voice pipeline** (`server/bot-remit.py`) — Nemotron + Gradium + Pipecat with multi-transport (WebRTC + Twilio + Daily + Pipecat Cloud).
- 🟢 **Supabase schema + persistence** (`server/db.py`) — users, recipients, transactions, with PIN hashing.
- 🟢 **Máximo bank integration — the REAL money path:**
  - `server/maximo_auth.py` — Cognito refresh-token → idToken → IAM temporary credentials, with caching and graceful auth fallback.
  - `server/maximo_recharge.py` — Sig V4 signed GraphQL calls to the bank's backoffice (`createRequest` + `approveTransactionRequest`). One call into `create_and_approve_recharge()` and PEN actually lands in the recipient's wallet.
  - `server/bank_lookup.py` — DNI → user_id lookup against Máximo PROD RDS (read-only user, VPN-gated).
- 🟢 **Cekura eval suite** — 15 scenarios + custom Compliance Score metric + one full auto-improvement loop with measurable results.
- 🟢 **Compliance-grade prompt** — 4 hard rules (no reasoning out loud, no PIN echo, amounts in words, only supported countries), with worked examples for the LLM, plus live-streaming `<think>` filter.
- 🟢 **DTMF integration** for DNI + PIN, so the safety-critical digits never go through the STT.
- 🟢 **Atomic money operation** — `create_transfer` wraps the Maximo call in `asyncio.shield` so a mid-call user interruption can't half-commit a transaction.
- 🟢 **Twilio inbound phone integration** + ngrok tunnel + TwiML, so the bot answers a real US toll-free number.

**Borrowed:** the Pipecat starter template, the `VLLMOpenAILLMService` base class, Máximo's existing backoffice API (the author is a board member of the bank — full authorization to test on PROD).

---

## 5. Feedback on the tools

### NVIDIA — Nemotron models

**What worked great:**
- 🔥 Nemotron Speech Streaming STT is **fast and accurate** on natural English speech. The hard-reset pattern gave us <200 ms turn-end latency. Caller interruptions feel instant.
- 🧠 Nemotron-3-Super-120B handles **multi-turn tool calling beautifully** — 7+ tools chained over a 60-second call with consistent argument synthesis and zero hallucinated tool names.
- ⚡ Latency to first token on the LLM was consistently <800 ms over WAN. For a phone conversation, that is the floor.

**What could be better:**
- ❗ **Chain-of-thought leaks into the response stream.** Setting `enable_thinking=false` in the request body did NOT stop the model from emitting `<think>…</think>` blocks, because the deployed endpoint doesn't include a reasoning parser. We had to write a streaming filter (`nemotron_llm.py:_ThinkStripper`) to strip those tags client-side before they hit TTS. A first-class option `reasoning_mode="off"` that *actually* turns reasoning off at the model level would have saved us 90 minutes.
- 🌐 STT is **English-only**. Our target market is migrants in the US — many are bilingual but more comfortable in Spanish. A multilingual streaming STT (or even Spanglish-tolerant) would be a huge step forward for this use case.
- 📞 Phone-quality audio (8 kHz µ-law from Twilio) sometimes hurt STT accuracy on long digit spans like DNI. We worked around it with DTMF, but native PSTN-codec robustness would be amazing.

### Cekura — eval + improvement platform

**What worked great:**
- 🎯 The **auto-improvement loop is the killer feature.** Generating 15 scenarios from a single agent description, running them, getting failure clusters back with proposed prompt edits — that is the workflow we want for every voice agent we ship at the bank. Going from 0/15 → 8/15 in one cycle was incredibly satisfying.
- 🛡️ **Custom metrics** (we built "Compliance Score") map cleanly to bank-grade audit requirements. Once you set the rules, every conversation gets a regulator-grade pass/fail with reasoning. This is the missing piece for fintechs adopting voice.
- 🧪 The **MCP-based access** ("create scenarios from Claude") is excellent ergonomics — we never had to leave the editor.

**Bugs / friction we hit:**
- 🪪 The Cekura MCP token expired ~4 times during the day. Each refresh required `/mcp` re-auth. A longer-lived service token (or auto-refresh) would help.
- 📞 Some test runs over `scenarios_run_pipecat_v1` got stuck in a queue when we hit our Daily room concurrency limit, and the UI didn't surface that limit clearly until we dug into the logs.
- 🧮 The Compliance Score metric returned a numeric value but the per-conversation reasoning sometimes refused to highlight the SPECIFIC sentence that broke the rule. A "click the offending bot turn" UI would close the feedback loop tighter.
- ⌛ Auto-fetch of provider tools (VAPI/Retell) is awesome, but we're on Pipecat (self-hosted) so we had to register all 13 mock tools by hand. A *"import mock tools from a `register_direct_function` Python module"* helper would be huge.

### Twilio + ngrok (general infra feedback)

- ngrok's free tier rotating the URL on every restart cost us an embarrassing amount of debugging time. **The pinned-subdomain feature is great — please move it earlier in the UX.**
- Twilio error `21264` ("Invalid Stream URL") fires on syntactically-valid TwiML with a slightly-stale ngrok URL. Surfacing **which URL Twilio actually tried to dial** in the error notification would save hours of guessing.

---

## 6. Live link

- 📞 **Call from any US number:** **+1 (833) 907-1804**
- 🌐 Browser demo (local + VPN): `http://localhost:7860/client/`
- 🏦 Watch the receiving end: `admin.maximo.pe/solicitudes-de-recargas` (Máximo backoffice — board-member access)

---

## Architecture at a glance

```
┌─────────────┐    PSTN    ┌────────────┐  wss   ┌────────────────────┐
│ Migrant US  │ ─────────► │  Twilio    │ ─────► │   Pipecat bot      │
│  phone      │            │ +1 833 907 │        │   (local + VPN)    │
└─────────────┘            └────────────┘        │                    │
                                                 │  Nemotron STT      │
                                                 │  Nemotron-3 LLM    │
                                                 │  Gradium TTS       │
                                                 │  Pipecat DTMF agg  │
                                                 └──┬──────────┬──────┘
                                                    │          │
                                  ┌─────────────────┘          └────────────────────┐
                                  ▼                                                 ▼
                         ┌──────────────────┐                          ┌─────────────────────────┐
                         │ Supabase Postgres│                          │  Máximo Bank (Perú)     │
                         │  state + audit   │                          │  • RDS read for DNI     │
                         └──────────────────┘                          │  • GraphQL Sig V4 +     │
                                                                       │    Cognito → IAM creds  │
                                  ▲                                    │  • createRequest +      │
                                  │                                    │    approveTransaction   │
                                  │                                    │  Money lands in wallet  │
                                  │                                    └─────────────────────────┘
                                  │
                                ┌─┴────────────────────┐
                                │   Cekura platform    │
                                │ 15 scenarios + auto- │
                                │ improvement loop +   │
                                │ Compliance Score     │
                                └──────────────────────┘
```

---
