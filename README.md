# Cat & Mouse

**A twice-weekly briefing that reads the AI firehose for me — and ships one thing worth acting on to Telegram.**

*[Читать на русском →](README.ru.md)*

---

## The problem

Following AI tooling in 2026 means GitHub trending, YouTube channels, release
notes, endless "top 10 agents" threads. Skimming it daily costs an hour and
produces nothing — a list of repo names and star counts earns about half a
second of attention before it's dismissed.

## What it does

`digest.py` pulls fresh signal from GitHub (search queries + a watched-repo
activity list) and YouTube (via `yt-dlp`, no API key), drops anything already
sent in the last 14 days, scores every candidate deterministically, clusters
the survivors into **themes**, picks the single strongest theme, and sends one
short argument about it to Telegram — Tuesday and Saturday at 11:00, via
Windows Task Scheduler.

The goal: twice a week, **one thing worth acting on**. Not a menu.

## The design rule this project taught me

> **The model compresses; code judges.**

A local 7B model (Ollama) has exactly one job here: turning an item into one
readable sentence. It does not score relevance, it does not pick the theme, it
does not write the opening line. All three were tried — the bake-off results
live in the header comment of [`local_llm.py`](local_llm.py). Short version: a
7B model rated an 831-comment thread 0/10 and a routine patch 8/10. Compression
is mechanical and a small model is good at it; judgement is not, and it is not.

## How it got here

This started as a daily link-list dashboard and shrank, on purpose, into
something smaller and sharper:

| Iteration | What changed | Why |
|---|---|---|
| v1 — daily dashboard | One link-list message per category, every morning at 08:55 | Read for half a second, never acted on |
| v2 — auto-analyze | A verdict (do / try / skip) pushed after each digest; format tightened ~40% | Lists don't drive action; arguments do |
| v3 — wider net | Categories, Reddit OAuth, 14-day de-dup, single-ping notifications | More sources, less repetition |
| v4 — one-line feed | Digest reshaped into a terse catchy feed | Still a menu, just a shorter one |
| v5 — the reshape | Daily → Tue+Sat 11:00 · dashboard → single-theme deep dive · local-model compression · Reddit dropped, watch-list expanded | One theme per fire, links as evidence under a claim |

Reddit was removed when their API policy gated personal read-only apps out of
existence — the reasoning is documented inline in
[`creators.json`](creators.json), next to the config it explains.

## Files

| File | Role |
|---|---|
| `digest.py` | Fetch → de-dup → score → cluster → send. The whole pipeline. |
| `local_llm.py` | Ollama helper. Compression only — see the design rule. |
| `send_verdict.py` | Pushes the post-analysis verdict to Telegram. |
| `creators.json` | The watch-list, with the reasoning for every choice documented inline. |
| `verdict_prompt.txt` | The analysis prompt and its strict output format. |
| `run_morning.ps1` / `analyze_and_notify.ps1` | Windows Task Scheduler entry points. |
| `test_dedup.py` | Tests for the 14-day de-dup window. |

## Setup

```
pip install -r requirements.txt
cp .env.example .env      # fill in your values
python digest.py --dry-run
```

Env vars (see [`.env.example`](.env.example)): `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, `GITHUB_TOKEN`; optionally `LOCAL_MODEL` and `OLLAMA_HOST`
for the compression step — everything degrades gracefully when Ollama is off.

---

*Published as a snapshot of a personal tool. It runs my mornings, not a
product roadmap — but every design decision is documented where it lives, and
forking it into your own interests means editing one JSON file.*
