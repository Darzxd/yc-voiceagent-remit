#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""vLLM OpenAI-compatible LLM service that times TTFB to the first NON-THINKING token.

Stock pipecat (``BaseOpenAILLMService._process_context``) stops the TTFB clock on
the first streamed chunk that carries any ``choices`` (base_llm.py:467) — i.e. the
first role / reasoning delta. For a reasoning model served with thinking enabled
(Nemotron-3-Super over vLLM), the answer (``content``) tokens do not begin until
the model finishes thinking, so the stock metric badly understates TTFB (in
aiewf-eval, ~270 ms reported vs. ~2.2 s to the first real answer token).

This subclass defers the TTFB stop until a delta actually carries user-visible
output (text ``content`` or a ``tool_call``), WITHOUT duplicating the large
``_process_context`` method:

  * ``get_chat_completions`` wraps the chunk stream and "arms" a flag on the first
    content/tool delta (resetting it per invocation);
  * ``stop_ttfb_metrics`` is gated on that flag.

Pipecat already calls ``stop_ttfb_metrics()`` on every chunk with ``choices``, so
once armed the existing call records TTFB at the correct moment; before that it is
a no-op. ``reasoning_content``-only, role-only, and empty deltas never arm it. When
thinking is disabled the first delta is already ``content``, so this is a no-op
correction (TTFB == stock).

Mirrors aiewf-eval's ``multi_turn_eval.services.vllm_openai.VLLMOpenAILLMService``,
adapted to this pipecat's ``get_chat_completions(self, context)`` signature.
"""

from pipecat.services.openai.llm import OpenAILLMService


class _ThinkStripper:
    """Streaming filter that drops ``<think>...</think>`` blocks from text.

    Nemotron-3-Super, even with ``enable_thinking=False`` requested, sometimes
    emits chain-of-thought wrapped in ``<think>`` tags inline with the spoken
    ``content``. Without a server-side reasoning parser this leaks into TTS.
    This filter is a defensive client-side guard: feed each delta's content,
    receive only the portions outside any ``<think>`` block. Handles tag splits
    across chunks via a small lookback equal to ``len(tag) - 1``.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self):
        self._buf = ""
        self._in_think = False

    def feed(self, text: str) -> str:
        if not text:
            return ""
        self._buf += text
        out: list[str] = []
        while True:
            if not self._in_think:
                i = self._buf.find(self._OPEN)
                if i == -1:
                    # only keep a lookback if the tail could be the START of an
                    # opening tag ("<" or "<t" or ... or "<think"). Otherwise emit
                    # everything — keeping unrelated trailing text in the buffer
                    # silently drops it.
                    lt = self._buf.rfind("<")
                    if lt == -1:
                        out.append(self._buf)
                        self._buf = ""
                        break
                    suffix = self._buf[lt:]
                    if self._OPEN.startswith(suffix):
                        out.append(self._buf[:lt])
                        self._buf = suffix
                        break
                    out.append(self._buf)
                    self._buf = ""
                    break
                out.append(self._buf[:i])
                self._buf = self._buf[i + len(self._OPEN):]
                self._in_think = True
            else:
                j = self._buf.find(self._CLOSE)
                if j == -1:
                    # inside think block — drop everything, but keep a tail that
                    # could be the start of </think> (so we can match across chunks).
                    lt = self._buf.rfind("<")
                    if lt == -1:
                        self._buf = ""
                        break
                    suffix = self._buf[lt:]
                    self._buf = suffix if self._CLOSE.startswith(suffix) else ""
                    break
                self._buf = self._buf[j + len(self._CLOSE):]
                self._in_think = False
        return "".join(out)


class VLLMOpenAILLMService(OpenAILLMService):
    """OpenAI-compatible vLLM service whose TTFB metric is the first answer token."""

    def __init__(self, *args, **kwargs):
        """Initialize the service; see OpenAILLMService for accepted args."""
        super().__init__(*args, **kwargs)
        self._ttft_armed = False

    async def get_chat_completions(self, context):
        """Wrap the chunk stream to arm TTFB on the first content/tool delta.

        ``_process_context`` calls this once per turn, right after
        ``start_ttfb_metrics()`` and before iterating — so reset the per-turn
        arming flag here.
        """
        self._ttft_armed = False
        stream = await super().get_chat_completions(context)
        stripper = _ThinkStripper()

        async def _armed_stream():
            try:
                async for chunk in stream:
                    choices = getattr(chunk, "choices", None)
                    delta = getattr(choices[0], "delta", None) if choices else None

                    # Strip <think>...</think> blocks from any text content
                    # before downstream processors see it.
                    if delta is not None:
                        content = getattr(delta, "content", None)
                        if content:
                            cleaned = stripper.feed(content)
                            try:
                                delta.content = cleaned
                            except Exception:
                                pass

                    if not self._ttft_armed:
                        # First non-thought token = first text content or tool call.
                        if delta is not None and (
                            getattr(delta, "content", None) or getattr(delta, "tool_calls", None)
                        ):
                            self._ttft_armed = True
                    yield chunk
            finally:
                # pipecat's _closing() only closes this wrapper generator; close the
                # underlying OpenAI stream too (HTTP resource + uvloop asyncgen safety).
                if hasattr(stream, "close"):
                    await stream.close()
                elif hasattr(stream, "aclose"):
                    await stream.aclose()

        return _armed_stream()

    async def stop_ttfb_metrics(self, *, end_time: float | None = None):
        """Defer the per-chunk TTFB stop until a non-thought token has streamed."""
        if self._ttft_armed:
            await super().stop_ttfb_metrics(end_time=end_time)
