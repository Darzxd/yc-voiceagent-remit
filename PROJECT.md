# Voice Remit — Briefing del proyecto (YC Voice Agents Hackathon, 30 May 2026)

> Este archivo es el contexto del PROYECTO que vamos a construir sobre este starter kit.
> El **stack técnico** está en `README.md`. Esto es **la idea y el plan**.

## La idea en una frase

**"El Stripe de las remesas por voz."** Un migrante llama a un número, habla con un agente de voz, y la plata llega a la cuenta de su familia en el extranjero en ~60 segundos. Sin app, sin formularios, sin inglés. Los bancos se conectan vía API y nosotros somos infraestructura invisible (white-label).

- 130M migrantes envían $800B/año en remesas. Western Union cobra 5–8% y tarda días. Wise/Remitly piden app + inglés + alfabetización digital.
- El hueco: **barato Y accesible**. Solo una llamada.

## Por qué esta idea GANA este hackathon

Los jueces (textual del README) quieren ver:
1. **Cekura mejorando el agente de voz** ← este es nuestro centro.
2. **Modelos open-source de NVIDIA** ← Nemotron STT + LLM, ya en el stack.

Todo lo demás (Stripe real, banca real, KYC real) los jueces **NO lo califican**. Es decoración. Si el demo depende de mover plata real sobre el wifi del evento, se rompe y perdemos.

## El demo (lo que mostramos en vivo)

### Centro del demo = el loop de auto-mejora con Cekura (esto es lo que gana)
1. `/cekura-report` corre 10–20 llamadas simuladas contra nuestro agente Pipecat → detecta un patrón de fallo (ej: "se caen en la confirmación de monto", "el agente habla de más").
2. Aplicamos el fix al prompt → re-corremos → **el score sube**.
3. Cierre: *"Nadie tocó el script. El agente se mejoró solo. No hubo humano en el loop."*

> Con **un solo** ciclo de auto-mejora end-to-end funcionando basta para el demo.

### Envoltura = la historia real de remesa US→Perú (lo que emociona)
- Demo en vivo **seguro**: usuario *returning* (pre-cargado, ya verificado) llama → voz + PIN → *"manda $200 a María"* → el agente confirma monto/tipo de cambio/comisión → PIN → "listo, 740 soles enviados" en ~60s.
- Debe sonar **humano**, baja latencia (≤1.2s voz-a-voz), **sin pensar en voz alta**.
- **MOCKEAR** el charge de Stripe y la recarga al banco → mostrar dashboard/DB actualizándose, NO transacciones reales en vivo.
- KYC (Keynua): contarlo como la historia del "primer registro", pero el demo NO depende de un face-scan en vivo.

## Stack: lo que cambia respecto a nuestro repo viejo

Repo viejo: `/Users/ignacior/Developer/voice-remit-practice/`

| Componente | Repo viejo (TIRAR) | Starter (USAR) |
|---|---|---|
| STT | Deepgram | NVIDIA Parakeet WebSocket (`nvidia_stt.py`) |
| LLM | NvidiaLLMService 49B + monkeypatch JSON | `VLLMOpenAILLMService` → Nemotron-3-Super-120B |
| TTS | ElevenLabs | Gradium (`GradiumTTSService`) |
| Tools | `register_function` + ToolsSchema | `register_direct_function` (firmas tipadas) |
| Turnos | SileroVAD solo | + `FilterIncompleteUserTurnStrategies` + STT hard-reset |
| Deploy | EC2 propio + .pem | Pipecat Cloud (`pc cloud deploy`) |
| Eval | `evaluation.py` a mano | Cekura vía Claude Code (`/cekura-report`) |

### Clave anti-"pensar en voz alta"
`enable_thinking=False` (ya es default en `bot-nemotron.py` vía `chat_template_kwargs`). **Mantener OFF para voz siempre.** Si se activa sin reasoning-parser en el server, el chain-of-thought se cuela en `content` y se habla.

### El monkeypatch de JSON duplicado del repo viejo: NO portar
Era un workaround para el bug de tool-calls de Llama 3.1 8B. Con Nemotron sobre vLLM OpenAI-compatible + `register_direct_function` ya no hace falta.

## Endpoints (válidos solo durante el evento)

```bash
export NVIDIA_ASR_URL=ws://44.241.251.184:8080
export NEMOTRON_LLM_URL=http://nemotron-fleet-alb-1322439314.us-west-2.elb.amazonaws.com/v1
export NEMOTRON_LLM_MODEL=nvidia/nemotron-3-super
# GRADIUM_API_KEY=  <-- pedir en el evento (créditos via Gradium)
# TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN  <-- copiar del .env viejo
```

## Qué PORTAR del repo viejo (es nuestra ventaja sobre el demo florería)

Desde `/Users/ignacior/Developer/voice-remit-practice/app/`:
- `db.py` — Supabase: users, recipients, transactions, onboarding_step
- `prompts.py` — el system prompt del flujo de remesa (adaptar a estilo del starter)
- `keynua.py` — KYC (mockear para el demo)
- `payments.py` — Stripe (mockear el charge para el demo)
- `bank.py` — tipo de cambio + recarga (ya tiene modo mock en dev)
- `verify_page.py` — página /verify (opcional para el demo)

Los **tools** del agente (lookup_user, save_user_name, send_keynua_link, verify_pin, create_transfer, get_exchange_rate, etc.) se reescriben como `register_direct_function` siguiendo el patrón de `bot-nemotron.py`.

## Orden de construcción (9am → 6pm submissions)

1. **Primero:** starter corriendo con los 3 endpoints + `GRADIUM_API_KEY`. El flower bot habla por WebRTC en localhost:7860. Confirmar latencia y `enable_thinking=False`.
2. **Verificar 2 riesgos YA:**
   - (a) ¿Gradium tiene voz en **español**? (el sender habla español — si no, replanteamos idioma del demo)
   - (b) ¿Nemotron llama tools bien con `register_direct_function`?
3. Crear `bot-remit.py` (copia de `bot-nemotron.py`) → reemplazar tools de florería por tools de remesa. Portar `db.py`. Mockear rails.
4. **Mediodía:** Cekura vía plugin de Claude Code (`/plugin marketplace add cekura-ai/cekura-skills`, `/plugin install cekura@cekura-skills`, `/cekura-report`). Baseline report.
5. **Tarde:** un ciclo completo de auto-mejora end-to-end (detectar fallo → editar prompt → re-correr → score sube).
6. **Final:** pre-cargar data de demo (usuario returning verificado + recipient "María"), ensayar pitch de 60s.

## Pitch de 60s (referencia)

"130 millones de migrantes envían 150 mil millones a casa cada año. Pagan 8% y esperan 3 días, o bajan una app que no entienden. Nosotros construimos el rail de remesas por voz para bancos." → [llamada en vivo] *"manda $200 a mi mamá María en Lima"* → [agente confirma y envía] → [dashboard: 740 soles acreditados] → "60 segundos, una llamada, sin app." → [Cekura: fallo detectado → prompt auto-actualizado] → "Esta llamada ya fue mejor que la anterior. Ningún humano la tocó."
