"""LLM-driven daily briefing synthesis.

Opt-in. Users configure a model + API key in ``~/.twitter-memory/config.toml``:

    [briefing]
    provider = "anthropic"
    model    = "claude-sonnet-4-6"
    api_key  = "sk-ant-..."

Or with environment variable fallbacks (CI/testing):

    TWITTER_MEMORY_BRIEFING_PROVIDER=anthropic
    TWITTER_MEMORY_BRIEFING_MODEL=claude-sonnet-4-6
    TWITTER_MEMORY_BRIEFING_API_KEY=...

The tool reads the structured day JSON produced by
:func:`mcp_server.export.build_json`, hands a compact prompt to the LLM, and
returns a structured dict shaped for agent consumption (see
``briefing_prompt`` for the schema contract).

Providers are pluggable through a tiny interface (one ``call`` function per
provider). Today: Anthropic. Others easy to add. Failures return a dict with
``error`` populated so agents can react without the tool raising.
"""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any, Callable

from mcp_server import export, settings


_DEFAULT_PROVIDER = "anthropic"
_DEFAULT_MODEL = "claude-sonnet-4-6"
_CONFIG_PATH = settings.DATA_DIR / "config.toml"


def _load_config() -> dict[str, Any]:
    """Load briefing config from config.toml (if present) overlaid by env vars.
    Returns ``{}`` when neither source provides values — the caller treats
    that as "not configured"."""
    cfg: dict[str, Any] = {}
    if _CONFIG_PATH.exists():
        try:
            with open(_CONFIG_PATH, "rb") as f:
                parsed = tomllib.load(f)
            cfg.update((parsed.get("briefing") or {}) if isinstance(parsed, dict) else {})
        except Exception:
            # Malformed config — ignore and fall back to env.
            pass
    env_map = {
        "provider": os.environ.get("TWITTER_MEMORY_BRIEFING_PROVIDER"),
        "model": os.environ.get("TWITTER_MEMORY_BRIEFING_MODEL"),
        "api_key": os.environ.get("TWITTER_MEMORY_BRIEFING_API_KEY"),
    }
    for k, v in env_map.items():
        if v:
            cfg[k] = v
    return cfg


def _build_prompt(day_json: dict[str, Any]) -> str:
    """Compact prompt for the LLM. Strips big sections (impressions, full
    interactions list) and keeps: summary, top tweets, hesitations, topics,
    anomalies. Agent-ready JSON contract is described inline so the model
    knows exactly what shape to return."""
    # Trim inputs to what a synthesis actually needs. Keep top 15 ranked tweets
    # (enough to see patterns), all hesitations, topics+authors, anomalies.
    tweets_slim = [
        {
            "rank": t["rank"],
            "tweet_id": t["tweet_id"],
            "handle": t["handle"],
            "text": t["text"],
            "topics": t["topics"],
            "dwell_ms": t["total_dwell_ms"],
            "impressions": t["impressions_count"],
            "engagement": t["engagement"],
            "user_engaged": t["user_had_interaction"],
        }
        for t in (day_json.get("tweets_ranked") or [])[:15]
    ]
    payload = {
        "date": day_json.get("date"),
        "summary": day_json.get("summary"),
        "anomalies": day_json.get("anomalies"),
        "topics": day_json.get("topics"),
        "authors_top": (day_json.get("authors") or [])[:10],
        "tweets_ranked_top": tweets_slim,
        "interactions": day_json.get("interactions"),
    }
    contract = (
        "You are producing a concise daily briefing for the user about their "
        "Twitter activity. Return STRICT JSON matching this schema:\n"
        '{ "headline": "<2 sentences>", '
        '"hesitations": [{"tweet_id": "...", "handle": "...", '
        '"action_almost_taken": "like|retweet|reply|bookmark", "note": "..."}], '
        '"suggested_replies": [{"tweet_id": "...", "reasoning": "...", '
        '"draft_tone_hints": ["..."]}], '
        '"follow_ups": [{"handle": "...", "why": "..."}], '
        '"topic_gaps": ["..."] }\n'
        "Only include hesitations that appear in the day data; if none, "
        "return an empty list. Suggest replies only when the data reasonably "
        "supports one; better to return an empty array than fabricate."
    )
    return f"{contract}\n\n---\nDay data:\n{json.dumps(payload, ensure_ascii=False)}"


def _call_anthropic(model: str, api_key: str, prompt: str) -> str:
    """Minimal Anthropic Messages API call using the ``anthropic`` SDK.

    Imported lazily so the rest of the module (and tests) don't need
    anthropic installed. Returns raw text from the assistant message.
    """
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(
            "anthropic package not installed. pip install anthropic or "
            "switch providers in ~/.twitter-memory/config.toml"
        ) from e
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    # Join all text blocks in the response content.
    return "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    )


_PROVIDERS: dict[str, Callable[[str, str, str], str]] = {
    "anthropic": _call_anthropic,
}


def _parse_llm_json(raw: str) -> dict[str, Any]:
    """Extract the JSON object from the model's response. Tolerates
    leading/trailing prose and a code fence wrapper."""
    # Strip a ```json ... ``` fence if present.
    txt = raw.strip()
    if txt.startswith("```"):
        # drop opening fence line
        lines = txt.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        txt = "\n".join(lines)
    # Fallback: find the first { and last } — good enough for well-behaved models.
    start = txt.find("{")
    end = txt.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("briefing response contained no JSON object")
    return json.loads(txt[start : end + 1])


def generate(
    day_json: dict[str, Any],
    call_override: Callable[[str, str, str], str] | None = None,
) -> dict[str, Any]:
    """Synthesize a briefing for one day. Tests inject ``call_override`` to
    bypass the real LLM.

    Returns ``{"error": "..."}`` on config / provider / parse failure so
    agents can handle gracefully."""
    cfg = _load_config()
    provider = cfg.get("provider") or _DEFAULT_PROVIDER
    model = cfg.get("model") or _DEFAULT_MODEL
    api_key = cfg.get("api_key")
    if not api_key and call_override is None:
        return {
            "error": (
                "daily_briefing is not configured. Add a [briefing] section to "
                f"{_CONFIG_PATH} with api_key + model, or set "
                "TWITTER_MEMORY_BRIEFING_API_KEY."
            ),
        }
    caller = call_override or _PROVIDERS.get(provider)
    if caller is None:
        return {"error": f"Unknown briefing provider: {provider}"}
    prompt = _build_prompt(day_json)
    try:
        raw = caller(model, api_key or "unused-for-tests", prompt)
    except Exception as e:
        return {"error": f"LLM call failed: {type(e).__name__}: {e}"}
    try:
        parsed = _parse_llm_json(raw)
    except (ValueError, json.JSONDecodeError) as e:
        return {"error": f"LLM returned invalid JSON: {e}", "raw": raw[:500]}
    parsed.setdefault("headline", "")
    parsed.setdefault("hesitations", [])
    parsed.setdefault("suggested_replies", [])
    parsed.setdefault("follow_ups", [])
    parsed.setdefault("topic_gaps", [])
    parsed["_meta"] = {"provider": provider, "model": model}
    return parsed
