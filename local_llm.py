"""Local model helper — Ollama on the RTX 5070 Laptop GPU (8GB VRAM).

Design rule: **the model compresses, code judges.** The single job here is turning one item into
one readable sentence. It does not score relevance, it does not choose the theme (except where
keyword matching found nothing), and it does not write the digest's opening line — all three were
tried on 2026-08-04 and all three came back worse than deterministic code:

  - scoring:  rated an 831-comment thread on Claude Max session limits 0/10, a routine SDK patch 8/10
  - the angle: "These updates address model behavior and tooling, saving time by improving session
    management" — grammatical, on-topic, empty

Compression is mechanical and a 7B model is good at it. Judgement is not mechanical and it is not.
Before handing this module a bigger job, re-run the bake-off rather than assuming.

Every function degrades to None when Ollama is not running, the model is not pulled, or the reply
does not parse. digest.py treats None as "use the heuristic" and the fire still ships.

Setup (one time):  winget install Ollama.Ollama  &&  ollama pull qwen2.5:7b-instruct-q4_K_M
Optional in .env:  LOCAL_MODEL=<tag>   OLLAMA_HOST=<url>   (bind values like 0.0.0.0 are handled)
"""

import json
import sys
from urllib.parse import urlsplit

import httpx

DEFAULT_HOST = "http://localhost:11434"
# Tried in order when LOCAL_MODEL is unset — first one actually pulled wins.
# All fit an 8GB laptop card. Measured on Daniel's box 2026-08-04:
#   qwen2.5:7b-instruct-q4_K_M  2.2s/item, clean JSON, usable one-liners  <- default
#   qwen3:4b                    fails outright unless think=false (thinking tokens break JSON mode)
MODEL_PREFERENCE = [
    "qwen2.5:7b-instruct-q4_K_M",
    "qwen2.5:7b",
    "qwen2.5:latest",
    "qwen3:4b",
    "gemma2:9b-instruct-q4_K_M",
]
KEEP_ALIVE = "5m"  # model stays resident across the ~24 calls of one fire, then frees the VRAM


def normalize_host(raw: str) -> str:
    """Turn whatever is in OLLAMA_HOST into a base URL that can actually be dialled.

    OLLAMA_HOST is the **server bind** variable, not a client endpoint. On Daniel's box it is set
    to `0.0.0.0`, which as a client URL means nothing — httpx got `0.0.0.0/api/tags`, failed, and
    the digest silently fell back to heuristics on every run. Bind-all addresses become localhost,
    a missing scheme becomes http://, a missing port becomes 11434.
    """
    h = (raw or "").strip()
    if not h:
        return DEFAULT_HOST
    if "://" not in h:
        h = "http://" + h
    try:
        parsed = urlsplit(h)
        hostname = parsed.hostname or "localhost"
        port = parsed.port or 11434
        scheme = parsed.scheme or "http"
    except ValueError:
        return DEFAULT_HOST
    if hostname in ("0.0.0.0", "::", "[::]"):
        hostname = "localhost"
    if ":" in hostname:  # bare IPv6 literal needs brackets in a URL
        hostname = f"[{hostname}]"
    return f"{scheme}://{hostname}:{port}"


def probe(host: str = DEFAULT_HOST, preferred: str = "") -> str:
    """Return the model name to use, or "" if Ollama is unreachable / has nothing suitable.
    Called once per fire; the result is threaded through the helpers below."""
    try:
        r = httpx.get(f"{host}/api/tags", timeout=4)
        r.raise_for_status()
        tags = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception:
        print("[llm] Ollama not reachable — digest falls back to heuristics. "
              "Install: winget install Ollama.Ollama", file=sys.stderr)
        return ""
    if not tags:
        print("[llm] Ollama is up but no model is pulled. Run: ollama pull qwen3.5:4b", file=sys.stderr)
        return ""
    if preferred:
        # Accept a bare name too ("qwen3.5:4b" matches "qwen3.5:4b" and "qwen3.5:4b-instruct-q4_K_M").
        for t in tags:
            if t == preferred or t.startswith(preferred):
                return t
        print(f"[llm] LOCAL_MODEL={preferred} not pulled (have: {', '.join(tags[:5])})", file=sys.stderr)
    for want in MODEL_PREFERENCE:
        for t in tags:
            if t == want or t.startswith(want):
                return t
    print(f"[llm] using {tags[0]} (none of the preferred small models are pulled)", file=sys.stderr)
    return tags[0]


def _gen(model: str, host: str, system: str, prompt: str, want_json: bool, num_predict: int, timeout: int = 90) -> str:
    """One raw generation. Empty string on any failure — callers decide the fallback."""
    payload = {
        "model": model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        # Thinking models (qwen3, deepseek-r1) emit a reasoning block before the answer, which
        # breaks JSON mode outright — qwen3:4b failed 5/5 judge calls until this was set.
        # Harmless on non-thinking models.
        "think": False,
        "options": {
            "temperature": 0.2,       # judgement, not creativity — keep it boring and repeatable
            "num_predict": num_predict,
            "top_p": 0.9,
        },
    }
    if want_json:
        payload["format"] = "json"
    try:
        r = httpx.post(f"{host}/api/generate", json=payload, timeout=timeout)
        r.raise_for_status()
        return (r.json().get("response") or "").strip()
    except Exception as e:
        print(f"[llm] generate failed: {str(e)[:120]}", file=sys.stderr)
        return ""


def _gen_json(model: str, host: str, system: str, prompt: str, num_predict: int = 120) -> dict | None:
    raw = _gen(model, host, system, prompt, want_json=True, num_predict=num_predict)
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        # Small models occasionally wrap JSON in prose despite format=json — salvage the braces.
        a, b = raw.find("{"), raw.rfind("}")
        if a >= 0 and b > a:
            try:
                obj = json.loads(raw[a:b + 1])
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                pass
        print(f"[llm] unparseable reply: {raw[:80]}", file=sys.stderr)
        return None


# ---------- bounded jobs ----------

_COMPRESS_SYS = (
    "You compress developer-tooling links into one plain sentence for a reader who already knows "
    "the field. Say what the thing IS or what problem it is about. Never editorialise, never use "
    "marketing words (streamline, leverage, seamless, powerful, robust, enhance), never restate "
    "the title. Answer with JSON only."
)


def compress(model: str, host: str, title: str, blurb: str, theme_ids: list[str]) -> dict | None:
    """One call per candidate: a compressed one-liner, plus a theme label used ONLY as a fallback
    when keyword matching found nothing.

    **This deliberately does not score.** A bake-off on 2026-08-04 had qwen2.5:7b rate an
    831-comment thread about Claude Max session limits 0/10 with no theme, while giving a routine
    SDK patch release 8/10 — blending that into the ranking made the digest measurably worse.
    Compression is mechanical and small models are good at it; relevance judgement is not, and it
    stays in deterministic code. Do not add a score field back without re-running the bake-off.

    Returns {"theme": <id or "">, "line": str} or None.
    """
    themes = ", ".join(theme_ids)
    prompt = (
        f"TITLE: {title}\n"
        f"BLURB: {blurb[:400]}\n\n"
        f"Return JSON with exactly these keys:\n"
        f'  "line": one plain sentence, max 16 words, saying what this actually is or what '
        f"problem it concerns.\n"
        f'  "theme": one of [{themes}] — whichever fits best, or "" if none fit.'
    )
    obj = _gen_json(model, host, _COMPRESS_SYS, prompt, num_predict=120)
    if not obj:
        return None
    theme = str(obj.get("theme") or "").strip().lower()
    if theme not in theme_ids:
        theme = ""
    line = str(obj.get("line") or "").strip()
    if len(line) > 160:
        line = line[:157].rstrip() + "…"
    if any(w in line.lower() for w in FILLER):
        line = ""  # marketing-speak is worse than the item's own first sentence
    return {"theme": theme, "line": line}


# Words that reliably mark a sentence as saying nothing. Cheap safety net on generated text.
FILLER = (
    "streamline", "leverage", "seamless", "robust", "powerful", "enhance", "empower",
    "cutting-edge", "game-chang", "revolution", "unlock", "supercharge", "elevate",
    "in today's", "landscape", "delve",
)
