"""Provider-agnostic LLM completion for Layer 2.

The semantic layer needs exactly one operation: given a system prompt and a user
prompt, return the model's text. Which vendor produces that text is irrelevant
to the thesis -- the grounding check verifies the output either way. Isolating
the vendor here keeps extractor.py about the experiment, and makes "does it stay
grounded on a *different* model?" a one-argument change.

Keys are read from the environment (OPENAI_API_KEY / ANTHROPIC_API_KEY) by the
respective SDKs. Never pass a key in code.
"""

from __future__ import annotations

import os
from typing import Optional

# Sensible defaults as of June 2026. Availability varies by account/tier;
# override `model` if your key doesn't have these. To list what your key can
# see: OpenAI -> client.models.list(); Anthropic -> client.models.list().
OPENAI_DEFAULT_MODEL = "gpt-5.5"
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"


def _maybe_load_dotenv() -> None:
    """Load a .env file if present, so keys defined there reach the SDKs.

    Best-effort: if python-dotenv isn't installed, real environment variables
    (or `uv run --env-file .env`) still work. Existing env vars take precedence
    over .env, which is the correct order.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def resolve_provider(provider: str = "auto") -> str:
    _maybe_load_dotenv()
    if provider != "auto":
        return provider
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    raise RuntimeError(
        "No API key found. Put OPENAI_API_KEY in your .env (or export it) "
        "and re-run."
    )


def default_model(provider: str) -> str:
    return {
        "openai": OPENAI_DEFAULT_MODEL,
        "anthropic": ANTHROPIC_DEFAULT_MODEL,
    }[provider]


def complete(
    system: str,
    user: str,
    *,
    provider: str = "auto",
    model: Optional[str] = None,
    max_tokens: int = 8192,
    force_json: bool = True,
    reasoning_effort: Optional[str] = "minimal",
) -> str:
    """Return the model's text for (system, user). Provider chosen by key if 'auto'.

    reasoning_effort applies to OpenAI GPT-5 reasoning models only ("minimal" |
    "low" | "medium" | "high", or None to omit). "minimal" keeps a structured
    extraction task fast and stops reasoning from eating the token budget.
    """
    provider = resolve_provider(provider)
    model = model or default_model(provider)
    if provider == "openai":
        return _complete_openai(system, user, model, max_tokens, force_json, reasoning_effort)
    if provider == "anthropic":
        return _complete_anthropic(system, user, model, max_tokens)
    raise ValueError(f"unknown provider: {provider!r}")


def _complete_openai(system: str, user: str, model: str, max_tokens: int,
                     force_json: bool, reasoning_effort: Optional[str]) -> str:
    from openai import OpenAI

    client = OpenAI()
    # Reasoning tokens count against max_completion_tokens; give generous room
    # so reasoning can't starve the visible answer (the empty-content failure).
    base_kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_completion_tokens": max(max_tokens, 4096),
    }
    if force_json:
        # json_object mode requires the word "json" to appear in the prompt;
        # our prompts already instruct "Output ONLY a single JSON object".
        base_kwargs["response_format"] = {"type": "json_object"}

    kwargs = dict(base_kwargs)
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort

    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:  # if this model rejects reasoning_effort, retry without it
        if reasoning_effort is not None and "reasoning_effort" in str(exc):
            resp = client.chat.completions.create(**base_kwargs)
        else:
            raise

    choice = resp.choices[0]
    content = choice.message.content
    if not content:
        raise RuntimeError(
            "OpenAI returned empty content "
            f"(finish_reason={choice.finish_reason!r}, usage={resp.usage}). "
            "For GPT-5 reasoning models this usually means the token budget was "
            "spent on reasoning -- raise max_tokens or set reasoning_effort='minimal'."
        )
    return content


def _complete_anthropic(system: str, user: str, model: str, max_tokens: int) -> str:
    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")
