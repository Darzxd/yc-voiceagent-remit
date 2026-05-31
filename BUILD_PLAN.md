# Voice Remit — Plan de construcción modular (build desde cero)

> Construimos sobre el starter (`server/bot-nemotron.py`). El repo viejo es **solo referencia**
> de qué servicios y pasos; no portamos su código. La **idea** está en `PROJECT.md`, el **stack** en `README.md`.

---

## 0. Decisiones base (ya tomadas)

| # | Decisión | Detalle |
|---|---|---|
| 1 | **Idioma: inglés** | STT del evento es solo-inglés. Stack 100% NVIDIA, cero riesgo STT. |
| 2 | **Stack = README** | Nemotron STT + Nemotron-3-Super LLM + Gradium TTS + Pipecat. |
| 3 | **State store: Supabase** | Hay credenciales en `.env`. Creamos las tablas (no existen aún). |
| 4 | **Voiceprint: SIMULADO** | Auth real = PIN (DTMF). El voiceprint se cuenta en el pitch. |
| 5 | **KYC: Keynua REAL** | Tenemos credenciales. Verificación facial de verdad. |
| 6 | **Stripe: REAL (test mode)** | Conectar tarjeta vía página web (SetupIntent), cobrar en la transferencia. |
| 7 | **Banco: recarga REAL** | Tenemos API + endpoint (detalles pendientes de cablear). Mock como fallback. |
| 8 | **Prioridad: onboarding completo primero**, luego Cekura. ⚠️ Ver checkpoint de tiempo en §5. |

### ✅ Ya validado en el starter
- Pipeline STT→LLM→TTS funciona. Latencia < 1s. `enable_thinking=false`. Tool-calls de Nemotron OK con `register_direct_function`. Voz = Russell.
- ⚠️ Ruido del salón degrada STT → usar headset; probar lógica por el campo de texto del Playground.
- **PIN — mismo código, dos fuentes.** El tool recibe el PIN como string; el LLM lo extrae de lo que llegue:
  - *Dev (Playground):* el usuario lo **dice o escribe** ("1234") → al tool.
  - *Demo final (celular/Twilio):* `DTMFAggregator` captura el **teclado** → llega como texto → mismo tool.
  - DTMF real solo existe sobre Twilio; en WebRTC local se usa voz/texto. No requiere ramas de código separadas.

---

## 1. Flujo completo del usuario

### Fase A — Primera llamada: ONBOARDING (sender, inglés)
```
1.  Llama al número → lookup_user(phone) → no existe → crea user (step: new)
2.  "What's your full name?"            → save_user_name        (step: awaiting_id)
3.  "Your ID/document number?"          → save_user_id          (step: awaiting_kyc)
4.  "I texted you a link on WhatsApp —  → send_verification_link
     verify your face and add your        (Keynua crea verificación + Twilio manda link)
     card, then call me back."
        ↓  (el usuario abre el link → PÁGINA WEB /verify)
        ├─ Keynua: verificación facial (KYC real)
        └─ Stripe: conecta tarjeta (SetupIntent → guarda payment method)
        ↓  webhooks: Keynua → kyc_verified ; Stripe → payment_method guardado
5.  [vuelve a llamar] lookup_user → kyc_status: verified
6.  "Say: 'I authorize Voice Remit...'" → register_voiceprint   (SIMULADO) (step: awaiting_pin)
7.  "Enter a 4-digit PIN" [DTMF]        → save_pin              (step: awaiting_pin_confirm)
8.  "Enter it again" [DTMF]            → confirm_pin           (step: completed)
9.  "You're all set! Send money now?"
```

### Fase B — Llamada returning: ENVIAR DINERO (el demo en vivo)
```
1.  lookup_user(phone) → step: completed, is_verified: true
2.  (voiceprint match SIMULADO) → "Hey [name]! What can I do for you?"
3.  "Send $200 to María"
4.  get_recipients(phone)  → ¿María guardada? si no: save_recipient(nombre, banco, cuenta)
5.  get_quote(200)         → rate 3.70, 740 soles, fee $2.99, total $202.99
6.  "200 dollars to María — that's 740 soles. Fee two ninety-nine. PIN to confirm."
7.  [DTMF PIN] → verify_pin(phone, pin)
8.  create_transfer(phone, "María", 200):
        a) Stripe: cobra la tarjeta guardada        (REAL, test mode)
        b) Banco:  recarga la cuenta de María       (REAL)
        c) registra transaction → status: completed
9.  "Done — María will get 740 soles." + dashboard se actualiza
```

---

## 2. Arquitectura

Dos procesos + Supabase compartido:

```
┌─────────────────────┐         ┌──────────────────────────┐
│  VOICE BOT (:7860)   │         │  WEB/API APP (FastAPI)    │
│  server/bot-remit.py │         │  server/web.py            │
│  Pipecat:            │         │  GET  /verify  (KYC+card) │
│   Nemotron STT       │         │  POST /webhooks/keynua    │
│   Nemotron-3 LLM     │         │  POST /webhooks/stripe    │
│   Gradium TTS        │         │  GET  /dashboard (demo)   │
│   tools (direct fn)  │         └────────────┬─────────────┘
└──────────┬──────────┘                       │
           │            ┌──────────────────────┴─────────┐
           └────────────┤        Supabase (state)         ├──────────┐
                        │  users · recipients · txns      │          │
                        └─────────────────────────────────┘          │
   Servicios externos: Keynua (KYC) · Stripe (pagos) · Banco (recarga) · Twilio (voz+WhatsApp+DTMF)
```

> **Webhooks necesitan URL pública** (Keynua/Stripe/WhatsApp llaman desde fuera). En dev: `ngrok`. En prod: deploy.

---

## 3. Módulos

### M1 · Data layer — `server/db.py` + schema Supabase
Cliente Supabase (service role) + CRUD. **Schema a crear:**
- **users**: `id, phone_number (unique), full_name, identity_number, onboarding_step, kyc_status, keynua_verification_id, stripe_customer_id, stripe_payment_method_id, voiceprint_id, pin_hash, is_verified, is_onboarded, created_at`
- **recipients**: `id, user_id→users, full_name, relationship, country, city, bank_name, account_number, currency, created_at`
- **transactions**: `id, user_id, recipient_id, amount_usd, amount_pen, exchange_rate, fee_usd, total_usd, status, stripe_charge_id, bank_reference, created_at`

`onboarding_step` ∈ `{new, awaiting_id, awaiting_kyc, verified, awaiting_pin, awaiting_pin_confirm, completed}`.

### M2 · Voice agent — `server/bot-remit.py`
Copia estructural de `bot-nemotron.py`. Pipeline + `DTMFAggregator` + tools `register_direct_function`. Tools:

| Categoría | Tool | Hace |
|---|---|---|
| Onboarding | `save_user_name(name)` | guarda nombre, step→awaiting_id |
| | `save_user_id(identity_number)` | guarda ID, step→awaiting_kyc |
| | `send_verification_link()` | Keynua + WhatsApp (M3, M7) |
| | `check_verification_status()` | poll Keynua (M3) |
| | `register_voiceprint()` | **mock** (M8) |
| | `save_pin(pin)` / `confirm_pin(pin)` | PIN DTMF |
| Transaccional | `lookup_user()` | perfil + step (enruta el flujo) |
| | `get_recipients()` / `save_recipient(...)` | destinatarios |
| | `get_quote(amount_usd)` | rate + soles + fee + total (M5) |
| | `verify_pin(pin)` | valida PIN |
| | `create_transfer(recipient_name, amount_usd)` | Stripe charge (M4) + recarga banco (M5) + registra txn |
| | `end_call()` | despedida + colgar |

El system prompt enruta según `onboarding_step` (igual lógica que el prompt viejo, reescrito al estilo starter: frases cortas, montos en palabras, sin pensar en voz alta).

### M3 · KYC — `server/keynua.py` (Keynua REAL)
- `create_verification(doc_number, phone, full_name)` → `{verification_id, user_token, link}`
- `get_status(verification_id)` → `{state}` (poll para "finished")
- Webhook handler (en M6) marca `kyc_status=verified`.

### M4 · Pagos — `server/payments.py` (Stripe REAL, test mode)
- `get_or_create_customer(user)` → `stripe_customer_id`
- `create_setup_intent(customer_id)` → client_secret (la página web guarda la tarjeta)
- `charge(customer_id, payment_method_id, amount_cents, description)` → en `create_transfer`

### M5 · Banco — `server/bank.py` (recarga REAL)
- `get_exchange_rate(from="USD", to="PEN")` → float
- `trigger_recarga(account, amount, currency, reference)` → acredita la cuenta. **Mock fallback** si la API no responde (para no romper el demo).
- ⏳ Pendiente: URL + key + shape del endpoint (los tienes, hay que pegarlos).

### M6 · Web app — `server/web.py` (FastAPI) + URL pública (ngrok)
- `GET /verify?token=...` → página con widget Keynua + Stripe Elements (conectar tarjeta).
- `POST /webhooks/keynua` → marca KYC verificado.
- `POST /webhooks/stripe` → guarda payment_method_id.
- `GET /dashboard` → tabla de transactions en vivo (el momento "740 soles acreditados").

### M7 · Messaging — `server/messaging.py` (Twilio WhatsApp REAL)
- `send_verification_link(phone, url)` → WhatsApp con el link de KYC/tarjeta.
- (opcional) notificación al recipient cuando llega la plata.

### M8 · Voiceprint — `server/voiceprint.py` (SIMULADO)
- `register(phone)` → devuelve `vp_<uuid>` mock. `verify(phone)` → True.
- En el pitch: "NVIDIA NeMo Speaker Verification".

### M9 · Cekura — vía plugin Claude Code
- `/plugin install cekura@cekura-skills` → `/cekura-report` contra bot-remit (provider=Pipecat).
- Baseline → detectar fallo → editar prompt → re-correr → score sube. **El centro del pitch.**

### M10 · Dashboard demo — parte de M6
- `/dashboard` muestra users/transactions actualizándose. Para el momento visual del demo.

---

## 4. Orden de construcción

1. **Supabase schema** (M1) — crear tablas vía MCP.
2. **`db.py`** — cliente + CRUD + hash PIN.
3. **`bot-remit.py` esqueleto** — pipeline + `lookup_user` + greeting + enrutado por `onboarding_step`.
4. **Onboarding por voz** — `save_user_name`, `save_user_id`, `save_pin`/`confirm_pin` (probar local por texto).
5. **Keynua + WhatsApp** (M3, M7) — `send_verification_link`, `check_verification_status`.
6. **Web verify page + Stripe SetupIntent** (M6, M4) — conectar tarjeta.
7. **Webhooks** (Keynua, Stripe) → actualizar user. (ngrok para URL pública.)
8. **Send-money** — `get_quote`, `verify_pin`, `create_transfer` (Stripe charge + recarga banco real).
9. **Dashboard** (M10).
10. **Cekura** (M9): baseline + un ciclo de auto-mejora.
11. **Ensayo**: pitch 60s + dry run + grabación de respaldo.

---

## 5. Decisiones pendientes / inputs que necesito de ti

- **Banco**: URL + key + shape del endpoint de recarga + cuenta destino de "María". (Dijiste que los tienes.)
- **URL pública para webhooks**: ¿uso `ngrok` o ya tienes un dominio/deploy? (Keynua/Stripe/WhatsApp lo necesitan.)
- **Stripe**: ¿test mode está OK para el demo? (las keys del `.env` son `sk_test_...`).
- **Datos del demo**: nombre del usuario, número de teléfono que vas a usar para llamar, datos de María (banco/cuenta).

> ⚠️ **Checkpoint de tiempo (~6h):** los jueces premian el loop de Cekura + NVIDIA. El onboarding real (Keynua web + Stripe + webhooks + ngrok) es mucho y arriesga no llegar a Cekura. **Propuesta:** poner un corte a las **~2:30pm** — si el onboarding real no está estable, congelarlo en el estado que tenga y saltar a Cekura (paso 10) sí o sí, para tener el diferenciador que gana. El onboarding restante se *cuenta* en el pitch.

---

## 6. Riesgos vivos
- **Ruido del salón** → STT sucio. Headset + texto para pruebas.
- **Endpoints del evento** (Nemotron) válidos solo hoy → grabar demo funcionando temprano.
- **Webhooks + ngrok** → punto de falla nuevo; probar el ida-y-vuelta KYC temprano.
- **DTMF solo por teléfono** (no en WebRTC local) → onboarding con PIN se valida llamando al número Twilio.
- **No llegar a Cekura** → ver checkpoint §5.
