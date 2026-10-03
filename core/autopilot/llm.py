"""
Text generation for the autopilot — Groq (free tier) first, then OpenAI, then
Claude, using whichever API keys are configured.
"""
import json
import os
import re

import requests

TIMEOUT = 90


def available() -> bool:
    return any(os.environ.get(k) for k in ("GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"))


def _openai_style(url: str, key: str, model: str, prompt: str, max_tokens: int, temperature: float) -> str:
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _anthropic(key: str, prompt: str, max_tokens: int, temperature: float) -> str:
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        json={
            "model": os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"]


def complete_json(prompt: str, max_tokens: int = 3000, temperature: float = 0.7) -> dict:
    """Return the first provider's response parsed as a JSON object.
    Raises RuntimeError if no provider is configured or all fail."""
    attempts = []
    if os.environ.get("GROQ_API_KEY"):
        attempts.append(("groq", lambda: _openai_style(
            "https://api.groq.com/openai/v1/chat/completions", os.environ["GROQ_API_KEY"],
            os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"), prompt, max_tokens, temperature)))
    if os.environ.get("OPENAI_API_KEY"):
        attempts.append(("openai", lambda: _openai_style(
            "https://api.openai.com/v1/chat/completions", os.environ["OPENAI_API_KEY"],
            os.environ.get("OPENAI_MODEL", "gpt-4o-mini"), prompt, max_tokens, temperature)))
    if os.environ.get("ANTHROPIC_API_KEY"):
        attempts.append(("anthropic", lambda: _anthropic(
            os.environ["ANTHROPIC_API_KEY"], prompt, max_tokens, temperature)))
    if not attempts:
        raise RuntimeError("No AI key set. Add GROQ_API_KEY (free at console.groq.com).")

    errors = []
    for name, call in attempts:
        try:
            return parse_json_object(call())
        except Exception as e:
            errors.append(f"{name}: {str(e)[:150]}")
    raise RuntimeError("All AI providers failed — " + " | ".join(errors))


def parse_json_object(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"```$", "", raw).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in response")
    data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    return data
