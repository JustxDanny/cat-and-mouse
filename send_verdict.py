"""Send a verdict file to Telegram as a plain-text message.

Usage: python send_verdict.py <path-to-verdict-file>

The path is passed explicitly by analyze_and_notify.ps1 (the exact
verdict_<today>.txt). No globbing here: glob-newest would silently
broadcast a stale or attacker-future-dated verdict if today's analyze
failed. PowerShell owns the contract; this script only delivers.

Sent LAST (after digest.py's per-category messages) so it is the most
recent message in the chat - the headline Daniel sees first on his phone.

Plain text on purpose: no MarkdownV2 escaping, nothing to break.
"""

import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).parent


def load_env() -> dict:
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            # Strip surrounding quotes: KEY="val" must not send literal quotes.
            env[k.strip()] = v.strip().strip("'\"")
    return env


def main() -> int:
    if len(sys.argv) < 2:
        print("[fatal] usage: send_verdict.py <verdict-file>", file=sys.stderr)
        return 1

    vf = Path(sys.argv[1])
    if not vf.is_file():
        print(f"[fatal] verdict file not found: {vf}", file=sys.stderr)
        return 1

    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[fatal] missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
        return 1

    text = vf.read_text(encoding="utf-8").strip()
    if not text:
        print(f"[fatal] {vf.name} is empty", file=sys.stderr)
        return 1
    # Telegram hard limit is 4096; verdict prompt caps ~1900, guard anyway.
    text = text[:4000]

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = httpx.post(url, json=payload, timeout=30)
    if r.status_code != 200:
        print(f"[fatal] telegram send failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1

    print(f"[done] sent {vf.name} ({len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
