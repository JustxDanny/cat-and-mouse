"""Cat & Mouse — twice-weekly (Tue+Sat 11:00) single-theme deep dive → Telegram.

Was a daily per-category link dashboard until 2026-08-04. Daniel read it for half a second and did
nothing with it, which is what a list of repo names deserves. Now every fire picks ONE theme out of
everything fetched and ships a short argument about it, with the links as evidence underneath.

Pipeline:  fetch → drop anything sent in the last 14d → normalise to flat candidates →
           heuristic score+theme → local-model judge (Ollama, optional) → pick the strongest theme
           → render one message.

The local model only ever answers small bounded questions (score / theme / one sentence). Structure,
ranking, theme choice and the workspace facts are deterministic code, so the digest stays sharp even
when Ollama is off — it just loses the hand-written one-liners.
"""

import json
import math
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

import local_llm

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "out"
OUT_DIR.mkdir(exist_ok=True)
MSK = timezone(timedelta(hours=3))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
REDDIT_UA = "DenchikAgent/1.0 by /u/daniel-rd"

# Tue→Sat is 4 days, Sat→Tue is 3. Fetch 4 so nothing falls into the gap between fires.
FETCH_DAYS = 4
# Nothing repeats inside a fortnight (~4 fires). Wider than the old 7d because there are fewer fires.
LEDGER_DAYS = 14
# Cap on local-model calls per fire: 24 x ~1.5s on the 5070 ≈ 40s. Beyond that the fire drags.
MAX_JUDGE = 24
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def github_token_from_gh() -> str:
    """Borrow the token the GitHub CLI already holds, instead of writing a second plaintext copy
    into .env. Nothing new lands on disk and `gh auth logout` revokes it here too.

    gh.exe is resolved by absolute path because PATH is not inherited in scheduled-task context —
    the same reason the VS Code launcher is hardcoded. Returns "" if gh is missing or not logged in;
    the token is never printed."""
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "GitHub CLI" / "gh.exe",
        Path.home() / "AppData" / "Local" / "GitHubCLI" / "gh.exe",
    ]
    exe = next((p for p in candidates if p.exists()), None)
    if not exe:
        return ""
    try:
        proc = subprocess.run([str(exe), "auth", "token"], capture_output=True, text=True, timeout=15)
    except Exception as e:
        print(f"[gh-auth] gh auth token failed: {str(e)[:100]}", file=sys.stderr)
        return ""
    if proc.returncode != 0:
        return ""
    tok = (proc.stdout or "").strip()
    return tok if tok.startswith(("gho_", "ghp_", "ghu_", "ghs_", "github_pat_")) else ""


def load_env() -> dict:
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


# ---------- GitHub ----------
def github_top(client: httpx.Client, queries: list[str], gh_token: str = "", since_days: int = FETCH_DAYS, limit: int = 5, exclude: set[str] = frozenset()) -> list[dict]:
    """Engagement-first ranking: forks (real users) + open_issues (discussion) + log(stars) tiebreaker.
    Hidden gems with low stars but high active engagement surface alongside megaprojects.
    `exclude` = URLs already reported this fortnight; skipped before ranking so top-N stays fresh.
    Unauthenticated search is capped at 10 req/min, so without a token we pace ourselves."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d")
    pool: dict[str, dict] = {}
    headers = {"Accept": "application/vnd.github+json"}
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"
    for i, q in enumerate(queries):
        # Unauthenticated search has a secondary limit far tighter than the documented 10/min —
        # measured 2026-08-04: 4 queries went through at 7s spacing, the next 4 all 403'd. So pace
        # hard and retry once after a real cooldown. With a token none of this runs.
        if not gh_token and i:
            time.sleep(20)
        params = {
            "q": f"{q} pushed:>{cutoff}",
            "sort": "updated",
            "order": "desc",
            "per_page": 30,
        }
        try:
            r = client.get(
                "https://api.github.com/search/repositories",
                params=params,
                headers=headers,
            )
            if r.status_code in (403, 429) and not gh_token:
                time.sleep(45)
                r = client.get("https://api.github.com/search/repositories", params=params, headers=headers)
            if r.status_code in (403, 429):
                print(f"[gh] '{q}' rate-limited ({r.status_code}); add GITHUB_TOKEN to .env (5000/h)", file=sys.stderr)
                continue
            r.raise_for_status()
            for it in r.json().get("items", []):
                key = it["full_name"]
                if key in pool:
                    continue
                if it.get("html_url") in exclude:
                    continue
                pool[key] = {
                    "name": key,
                    "url": it["html_url"],
                    "desc": (it.get("description") or "")[:200],
                    "stars": it["stargazers_count"],
                    "forks": it.get("forks_count", 0),
                    "issues": it.get("open_issues_count", 0),
                    "lang": it.get("language") or "",
                    "pushed": it["pushed_at"],
                    "matched_q": q,
                }
        except Exception as e:
            print(f"[gh] '{q}' failed: {e}", file=sys.stderr)

    def score(r: dict) -> float:
        if r["stars"] < 5:  # spam filter
            return 0
        return r["forks"] * 3 + r["issues"] * 2 + math.log1p(r["stars"])

    items = sorted(pool.values(), key=score, reverse=True)
    return items[:limit]


# ---------- GitHub repo activity (watch specific repos' issues/comments/releases) ----------
def github_repo_activity(client: httpx.Client, repos: list[str], gh_token: str = "", since_days: int = FETCH_DAYS, limit: int = 5, exclude: set[str] = frozenset(), per_repo_cap: int = 3) -> dict:
    """For a watch-list of owner/repo, surface the most-discussed recently-active issues+PRs
    (sort=comments) plus any fresh releases. This is how the digest tracks the tools Daniel actually
    lives in — claude-code releases and the arguments in its issue threads — rather than search stats."""
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"Accept": "application/vnd.github+json"}
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"
    issues: list[dict] = []
    releases: list[dict] = []
    for repo in repos:
        # Fresh releases = the canonical "new version dropped" signal.
        try:
            rr = client.get(f"https://api.github.com/repos/{repo}/releases", params={"per_page": 3}, headers=headers)
            if rr.status_code == 200:
                for rel in rr.json():
                    pub = rel.get("published_at") or ""
                    url = rel.get("html_url") or ""
                    if pub >= cutoff_iso and url and url not in exclude:
                        releases.append({
                            "repo": repo,
                            "tag": rel.get("tag_name") or rel.get("name") or "",
                            "url": url,
                            "published": pub,
                            "body": (rel.get("body") or "")[:400],
                        })
            elif rr.status_code in (403, 429):
                print(f"[gh-activity] {repo} releases rate-limited ({rr.status_code}); add GITHUB_TOKEN", file=sys.stderr)
        except Exception as e:
            print(f"[gh-activity] {repo} releases: {e}", file=sys.stderr)
        # Most-discussed issues/PRs active in the window — where the real disagreements live.
        try:
            ir = client.get(
                f"https://api.github.com/repos/{repo}/issues",
                params={"state": "all", "sort": "comments", "direction": "desc", "since": cutoff_iso, "per_page": 15},
                headers=headers,
            )
            if ir.status_code == 200:
                for it in ir.json():
                    url = it.get("html_url") or ""
                    if not url or url in exclude:
                        continue
                    issues.append({
                        "repo": repo,
                        "title": (it.get("title") or "")[:160],
                        "body": (it.get("body") or "")[:400],
                        "url": url,
                        "number": it.get("number"),
                        "comments": int(it.get("comments") or 0),
                        "is_pr": "pull_request" in it,
                    })
            elif ir.status_code in (403, 429):
                print(f"[gh-activity] {repo} issues rate-limited ({ir.status_code}); add GITHUB_TOKEN", file=sys.stderr)
        except Exception as e:
            print(f"[gh-activity] {repo} issues: {e}", file=sys.stderr)
    issues.sort(key=lambda x: x["comments"], reverse=True)
    releases.sort(key=lambda x: x["published"], reverse=True)
    # Cap per repo before the global cut. Without this a high-volume tracker swamps the list —
    # openai/codex alone carries ~11k open issues against modelcontextprotocol's ~150, so a plain
    # sort-by-comments would hand every slot to the same project and kill the theme diversity the
    # whole digest depends on.
    kept: list[dict] = []
    seen_per_repo: dict[str, int] = defaultdict(int)
    for it in issues:
        if seen_per_repo[it["repo"]] >= per_repo_cap:
            continue
        seen_per_repo[it["repo"]] += 1
        kept.append(it)
    return {"issues": kept[:limit], "releases": releases[:limit]}


# ---------- YouTube via yt-dlp ----------
def youtube_top(creators: list[str], since_days: int = FETCH_DAYS, limit: int = 5, exclude: set[str] = frozenset()) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    candidates: list[dict] = []
    for handle in creators:
        url = f"https://www.youtube.com/@{handle}/videos"
        try:
            proc = subprocess.run(
                [
                    sys.executable, "-m", "yt_dlp",
                    # No description field on purpose: descriptions contain newlines, which would
                    # split one video across several output lines and silently drop it. A weaker
                    # blurb beats losing the item — these creators write hook-heavy titles anyway.
                    "--print", "%(id)s|%(title)s|%(view_count)s|%(timestamp)s",
                    "--playlist-end", "5",
                    "--dateafter", f"now-{since_days}days",
                    "--skip-download",
                    "--no-warnings", "--quiet",
                    url,
                ],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0 and not proc.stdout.strip():
                err = (proc.stderr or "").strip().splitlines()[-1] if proc.stderr else f"rc={proc.returncode}"
                print(f"[yt] {handle}: {err[:160]}", file=sys.stderr)
                continue
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line or "|" not in line:
                    continue
                parts = line.split("|", 3)
                if len(parts) != 4:
                    continue
                vid_id, title, views_s, ts_s = parts
                if ts_s in ("NA", "None", ""):
                    continue
                try:
                    ts = int(ts_s)
                    views = int(views_s) if views_s not in ("NA", "None", "") else 0
                except ValueError:
                    continue
                up_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                if up_dt < cutoff:
                    continue
                vurl = f"https://youtu.be/{vid_id}"
                if vurl in exclude:
                    continue
                candidates.append({
                    "title": title,
                    "creator": handle,
                    "views": views,
                    "url": vurl,
                    "blurb": "",
                    "uploaded": up_dt.isoformat(),
                })
        except subprocess.TimeoutExpired:
            print(f"[yt] {handle}: timeout", file=sys.stderr)
        except Exception as e:
            print(f"[yt] {handle}: {e}", file=sys.stderr)
    candidates.sort(key=lambda x: x["views"], reverse=True)
    # Dedup creators in top to avoid one creator dominating.
    picked: list[dict] = []
    seen_creators: set[str] = set()
    for c in candidates:
        if c["creator"] in seen_creators and len(picked) < limit:
            continue
        picked.append(c)
        seen_creators.add(c["creator"])
        if len(picked) >= limit:
            break
    if len(picked) < limit:
        for c in candidates:
            if c not in picked:
                picked.append(c)
                if len(picked) >= limit:
                    break
    return picked[:limit]


# ---------- Reddit (official OAuth API) ----------
# As of 2026-07 Reddit's unauth endpoints are unusable: /top.json serves the HTML SPA to everyone
# (globally, not an IP block — confirmed from local + BR + RU nodes), and /top/.rss is throttled to
# ~1 request per IP per minute (dies after the first sub). The authenticated app-only API gives
# 100 req/min and works straight from Daniel's local IP — so the old SSH-relay is gone.
def reddit_token(client: httpx.Client, client_id: str, client_secret: str) -> str:
    """App-only OAuth bearer token (client_credentials grant). Register a free 'script' app at
    https://www.reddit.com/prefs/apps → put its id+secret in .env as REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET."""
    r = client.post(
        "https://www.reddit.com/api/v1/access_token",
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        headers={"User-Agent": REDDIT_UA},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _reddit_fetch_one(client: httpx.Client, sub: str, token: str) -> dict | None:
    """One subreddit's top-of-week via the authenticated API. Same Listing JSON shape as the old
    public endpoint, so the parse below is unchanged. Returns parsed JSON or None on failure."""
    try:
        r = client.get(
            f"https://oauth.reddit.com/r/{sub}/top",
            params={"t": "week", "limit": 50},
            headers={"Authorization": f"bearer {token}", "User-Agent": REDDIT_UA},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[reddit] r/{sub}: {e}", file=sys.stderr)
        return None


def reddit_top(client: httpx.Client, subreddits: list[str], token: str = "", since_days: int = FETCH_DAYS, limit: int = 3, exclude: set[str] = frozenset()) -> list[dict]:
    if not token:
        return []  # no creds → graceful empty section (add REDDIT_CLIENT_ID/SECRET to .env)
    cutoff = int(time.time()) - since_days * 86400
    pool: dict[str, dict] = {}
    for sub in subreddits:
        data = _reddit_fetch_one(client, sub, token)
        if not data:
            continue
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            obj_id = d.get("id")
            if not obj_id or obj_id in pool:
                continue
            if d.get("stickied") or d.get("over_18"):
                continue
            if int(d.get("created_utc") or 0) < cutoff:
                continue
            permalink = d.get("permalink") or ""
            full_permalink = "https://www.reddit.com" + permalink if permalink else ""
            if full_permalink and full_permalink in exclude:
                continue
            pool[obj_id] = {
                "title": (d.get("title") or "")[:200],
                "blurb": (d.get("selftext") or "")[:400],
                "subreddit": d.get("subreddit") or sub,
                "permalink": full_permalink,
                "external_url": d.get("url_overridden_by_dest") or "",
                "score": int(d.get("score") or 0),
                "comments": int(d.get("num_comments") or 0),
                "author": d.get("author", ""),
            }
    # Daniel's primary signal: comment volume = real discussion.
    items = sorted(pool.values(), key=lambda x: (x["comments"], x["score"]), reverse=True)
    return items[:limit]


# ---------- Telegram MarkdownV2 ----------
_MD2_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"


def md(s: str) -> str:
    return "".join(("\\" + c) if c in _MD2_SPECIAL else c for c in s)


def _link(text: str, url: str, maxlen: int = 80) -> str:
    """One tappable MarkdownV2 link, text trimmed to one glanceable line."""
    t = text.strip()
    if len(t) > maxlen:
        t = t[:maxlen - 1].rstrip() + "…"
    return f"[{md(t)}]({url})"


def _k(n: int) -> str:
    """Compact star/count with no '.' (dots are MarkdownV2-special)."""
    return f"{n // 1000}k" if n >= 1000 else str(n)


# ---------- Candidates: one flat shape for every source ----------
KIND_ICON = {"release": "🏷", "issue": "🔧", "pr": "🔀", "repo": "🐙", "video": "▶️", "post": "💬"}

# Editorial stance, not a popularity measure. A repo with 400 forks proves something is popular;
# a thread with 1400 comments proves people are actively stuck on a problem, and the transferable
# answer lives in the argument. Stars are a lagging vanity number, so repos sit at the bottom.
KIND_WEIGHT = {"post": 4.0, "issue": 4.0, "pr": 1.5, "release": 2.0, "video": 2.5, "repo": 0.5}
# Kinds where the raw number IS the story — 1400 comments and 14 comments are different events.
DISCUSSION_KINDS = ("post", "issue", "pr")

# Platforms Daniel cannot run. Keyword scoring has no idea he is on Windows with an NVIDIA card,
# so Apple-Silicon projects kept surfacing (Rapid-MLX led a fire on 2026-08-04 purely on fork
# count). A penalty rather than a hard filter: a genuinely huge Mac-flavoured thread can still
# earn its place, it just has to be much bigger than the alternatives.
OFF_PLATFORM = ("apple silicon", "macos", " mac ", "m1 max", "m2 ultra", "m3 max", "m4 max",
                "mlx", "iphone", "ios app", "xcode", "swiftui", "metal backend")
OFF_PLATFORM_PENALTY = 4.0


def to_candidates(repo_activity: dict | None, gh: list[dict], yt: list[dict], rd: list[dict]) -> list[dict]:
    """Flatten every fetcher's output into one comparable shape so themes can cut across sources —
    a Reddit argument, a claude-code issue and a YouTube video about the same idea land together."""
    out: list[dict] = []
    if repo_activity:
        for rel in repo_activity.get("releases", []):
            out.append({
                "kind": "release",
                "title": f"{rel['repo']} {rel['tag']}",
                "blurb": rel.get("body", ""),
                "url": rel["url"],
                "engagement": 1,
                "meta": "new release",
                "why": "fresh release — the changelog is the whole point",
            })
        for it in repo_activity.get("issues", []):
            out.append({
                "kind": "pr" if it["is_pr"] else "issue",
                "title": it["title"],
                "blurb": it.get("body", ""),
                "url": it["url"],
                "engagement": it["comments"],
                "meta": f"{it['repo'].split('/')[-1]} · 💬{it['comments']}",
                "why": f"{it['comments']} comments on the thread — the disagreement is the useful part",
            })
    for r in gh:
        out.append({
            "kind": "repo",
            "title": r["name"],
            "blurb": r.get("desc", ""),
            "url": r["url"],
            "engagement": r["forks"] * 3 + r["issues"] * 2,
            "meta": f"⭐{_k(r['stars'])}",
            "why": f"⭐{_k(r['stars'])} · {_k(r['forks'])} forks — people are running it, not just starring it",
        })
    for v in yt:
        out.append({
            "kind": "video",
            "title": v["title"],
            "blurb": v.get("blurb", ""),
            "url": v["url"],
            "engagement": v["views"],
            "meta": f"@{v['creator']}",
            "why": f"{_k(v['views'])} views since it dropped · @{v['creator']}",
        })
    for p in rd:
        if not p.get("permalink"):
            continue
        out.append({
            "kind": "post",
            "title": p["title"],
            "blurb": p.get("blurb", ""),
            "url": p["permalink"],
            "engagement": p["comments"],
            "meta": f"r/{p['subreddit']} · 💬{p['comments']}",
            "why": f"{p['comments']} comments in r/{p['subreddit']} — read the argument, not the post",
        })
    return out


# ---------- Themes ----------
def theme_hits(text: str, theme: dict) -> int:
    t = text.lower()
    return sum(1 for k in theme["keywords"] if k in t)


def heuristic_pass(cands: list[dict], themes: list[dict]) -> None:
    """Score and theme every candidate without touching the model. Four terms, deliberately:

      position within its own kind (0-5)  — is this the biggest repo / loudest thread of the batch
      topical fit (0-4)                   — how many of the theme's keywords it actually hits
      kind weight (0.5-4)                 — editorial preference for arguments over star counts
      discussion heat (0-1.5)             — absolute comment volume, log-scaled, threads only

    Position alone is not enough: with only two releases in a batch, the top one would otherwise
    score the same as a 1483-comment thread purely for being first in a near-empty bucket.
    This is the floor the digest never drops below, model or no model."""
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for c in cands:
        by_kind[c["kind"]].append(c)
    for kind_items in by_kind.values():
        ranked = sorted(kind_items, key=lambda c: c["engagement"], reverse=True)
        n = len(ranked)
        for i, c in enumerate(ranked):
            c["h_score"] = 5.0 * (1 - i / n) if n > 1 else 3.5
    for c in cands:
        if c["kind"] in DISCUSSION_KINDS and c["engagement"] > 0:
            c["h_score"] += min(1.5, math.log10(1 + c["engagement"]) / 2)
    for c in cands:
        text = f"{c['title']} {c['blurb']}"
        best_id, best_n = "", 0
        for th in themes:
            n = theme_hits(text, th)
            if n > best_n:
                best_id, best_n = th["id"], n
        c["theme"] = best_id
        c["theme_hits"] = best_n
        # A clear topical match is itself evidence of relevance to Daniel's focus.
        c["score"] = c["h_score"] + min(best_n, 4) + KIND_WEIGHT.get(c["kind"], 0.0)
        low = text.lower()
        if any(w in low for w in OFF_PLATFORM):
            c["score"] -= OFF_PLATFORM_PENALTY
            c["off_platform"] = True
        c["line"] = heuristic_line(c["blurb"])


def heuristic_line(blurb: str) -> str:
    """First real sentence of the item's own text, when it stands on its own. Keeps the pattern
    section alive without a model — the local LLM overwrites this with something better when it runs.
    Rejects fragments and walls of text rather than printing something half-formed."""
    s = " ".join((blurb or "").split())
    if not s:
        return ""
    for end in (". ", "! ", "? "):
        i = s.find(end)
        if i > 0:
            s = s[:i]
            break
    s = s.rstrip(".!? ")
    if len(s) < 25 or len(s) > 130:
        return ""
    return s


def llm_pass(cands: list[dict], themes: list[dict], model: str, host: str) -> int:
    """Have the local model compress the strongest candidates into one readable line each.

    **The model does not affect ranking.** An earlier version blended its 0-10 relevance score
    65/35 into the sort; a bake-off on 2026-08-04 showed qwen2.5:7b scoring an 831-comment thread
    about Claude Max session limits at 0/10 while giving a routine SDK patch release 8/10, which
    made the ordering worse than pure heuristics. Scores are gone. The model's theme label is
    accepted only where keyword matching found nothing at all, so it can add signal but never
    override it. Returns how many items it compressed."""
    theme_ids = [t["id"] for t in themes]
    ordered = sorted(cands, key=lambda c: c["score"], reverse=True)[:MAX_JUDGE]
    done = 0
    for c in ordered:
        got = local_llm.compress(model, host, c["title"], c["blurb"], theme_ids)
        if not got:
            continue
        done += 1
        if got["line"]:
            c["line"] = got["line"]
        if got["theme"] and not c["theme"]:
            c["theme"] = got["theme"]
            c["score"] += 1.0  # a theme found only by the model is weaker evidence than a keyword hit
    return done


def pick_theme(cands: list[dict], themes: list[dict], avoid: str = "") -> tuple[dict, list[dict]]:
    """Strongest theme = sum of its top 5 items' scores, needing at least 2 items to count as a
    theme at all. The previous fire's theme is discounted 30% so the digest does not talk about
    context hygiene four times running. Falls back to a mixed pick when nothing clusters."""
    by_theme: dict[str, list[dict]] = defaultdict(list)
    for c in cands:
        if c["theme"]:
            by_theme[c["theme"]].append(c)
    best_id, best_score = "", 0.0
    for tid, items in by_theme.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda c: c["score"], reverse=True)
        s = sum(c["score"] for c in items[:5])
        if tid == avoid:
            s *= 0.7
        if s > best_score:
            best_id, best_score = tid, s
    if not best_id:
        mixed = {"id": "mixed", "label": "MIXED BAG", "emoji": "🎲",
                 "lens": "nothing clustered this time — here is what actually moved"}
        return mixed, sorted(cands, key=lambda c: c["score"], reverse=True)[:6]
    theme = next(t for t in themes if t["id"] == best_id)
    return theme, by_theme[best_id]


# ---------- Workspace facts (real measurements, never filler) ----------
def workspace_fact(theme_id: str) -> str:
    """One measured fact about Daniel's own setup, or "" when there is nothing honest to measure.
    Deliberately narrow: a made-up 'you might be wasting tokens' line is exactly the generic mush
    that killed the old digest. Only themes with a real, cheap measurement get a line."""
    home = Path.home()
    if theme_id == "context":
        chain = [
            home / ".claude" / "CLAUDE.md",
            home / "projects" / "CLAUDE.md",
            ROOT / "CLAUDE.md",
        ]
        total = 0
        present = 0
        for p in chain:
            try:
                total += len(p.read_text(encoding="utf-8", errors="ignore"))
                present += 1
            except OSError:
                continue
        if not total:
            return ""
        return (f"your always-on CLAUDE.md chain is {present} files, ~{total // 1000}k chars "
                f"≈ {total // 4000}k tokens — prepended to every single message you send")
    if theme_id == "harness":
        skills = home / ".claude" / "skills"
        try:
            n = sum(1 for d in skills.iterdir() if d.is_dir())
        except OSError:
            return ""
        if not n:
            return ""
        return f"you have {n} skills installed — each one's description loads on every session"
    return ""


# ---------- Message ----------
def build_deep_dive(date_str: str, theme: dict, angle: str, items: list[dict], fact: str, blind: list[str]) -> str:
    """One message, one argument. Header → the claim → the pattern → your own numbers →
    the single thing to open → the rest as evidence."""
    L = [f"{theme['emoji']} *{md(theme['label'])}* · {md(date_str)}", ""]
    L.append(md(angle))

    pattern = [c for c in items[:4] if c.get("line")]
    if pattern:
        L.append("")
        L.append("*The pattern*")
        for c in pattern:
            L.append(f"· {md(c['line'])}")

    if fact:
        L.append("")
        L.append(f"*Your side* — {md(fact)}")

    if items:
        head = items[0]
        L.append("")
        L.append(f"*Start here* → {_link(head['title'], head['url'], 70)}")
        L.append(f"_{md(head['why'])}_")

    rest = items[1:6]
    if rest:
        L.append("")
        L.append("*The rest of it*")
        for c in rest:
            icon = KIND_ICON.get(c["kind"], "•")
            L.append(f"{icon} {_link(c['title'], c['url'], 62)} · {md(c['meta'])}")

    if blind:
        L.append("")
        L.append(f"⚠️ _{md('running blind: ' + ' · '.join(blind))}_")

    return "\n".join(L)


def send_telegram(token: str, chat_id: str, text: str, silent: bool = False) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": False,
        "link_preview_options": {"is_disabled": False, "show_above_text": False},
        "disable_notification": silent,
    }
    r = httpx.post(url, json=payload, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"telegram send failed: {r.status_code} {r.text}")


def normalize_categories(cfg: dict) -> dict:
    """Accept the categorized config. Legacy flat config is wrapped as a single 'default' category."""
    if "categories" in cfg and isinstance(cfg["categories"], dict):
        return cfg["categories"]
    return {
        "default": {
            "title": "Cat & Mouse",
            "emoji": "🐾",
            "github_queries": cfg.get("github_queries", []),
            "youtube_creators": cfg.get("youtube_creators", []),
            "reddit_subreddits": cfg.get("reddit_subreddits", []),
        }
    }


# ---------- De-dup ledger ----------
def recent_reported_urls() -> set[str]:
    """URLs already sent inside the last LEDGER_DAYS, so nothing repeats within a fortnight (~4 fires).
    Reuses the digest_<date>.json files as the ledger — each stores a flat `urls` list of everything it
    sent. Today's own file (age 0) is excluded so a re-run does not dedup against itself; files
    LEDGER_DAYS or older fall out of the window and may appear again.
    The digest files ARE the ledger — no separate state. If out/ is ever pruned faster than a
    fortnight, switch to a dedicated seen.json."""
    today = datetime.now(MSK).date()
    seen: set[str] = set()
    for p in OUT_DIR.glob("digest_*.json"):
        try:
            d = datetime.strptime(p.stem[len("digest_"):], "%Y-%m-%d").date()
        except ValueError:
            continue
        age = (today - d).days
        if age < 1 or age >= LEDGER_DAYS:  # skip today's file and anything past the window
            continue
        try:
            snap = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for u in snap.get("urls", []):
            if u:
                seen.add(u)
    return seen


def last_theme() -> str:
    """The theme of the most recent fire, so pick_theme() can avoid repeating it back to back."""
    files = sorted(OUT_DIR.glob("digest_*.json"))
    for p in reversed(files):
        try:
            snap = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        t = snap.get("theme", {})
        if isinstance(t, dict) and t.get("id"):
            return t["id"]
    return ""


# ---------- Main ----------
def main() -> int:
    dry_run = "--dry-run" in sys.argv
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not dry_run and (not token or not chat_id):
        print("[fatal] missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
        return 1

    cfg = json.loads((ROOT / "creators.json").read_text(encoding="utf-8"))
    categories = normalize_categories(cfg)
    themes = cfg.get("themes", [])
    if not themes:
        print("[fatal] creators.json has no `themes` — the digest cannot cluster", file=sys.stderr)
        return 1

    now = datetime.now(MSK)
    today = now.strftime("%Y-%m-%d")
    date_str = f"{DAY_NAMES[now.weekday()]} {now.strftime('%d.%m')}"

    # `blind` collects every channel that is switched off, and gets printed at the bottom of the
    # message. Daniel keeps forgetting these are empty; putting it in his face is the fix.
    blind: list[str] = []

    gh_token = env.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    if not gh_token:
        gh_token = github_token_from_gh()
        if gh_token:
            print("[gh-auth] using the GitHub CLI's token (no copy stored in .env)")
    if not gh_token:
        print("[warn] no GITHUB_TOKEN and gh is not logged in — throttling search hard.", file=sys.stderr)
        blind.append("GitHub unauthenticated")

    seen_urls = recent_reported_urls()
    if seen_urls:
        print(f"[dedup] {len(seen_urls)} URLs sent in the last {LEDGER_DAYS}d will be skipped")

    # Reddit is off by config, not by oversight — see creators.json `_reddit_note`. The `blind`
    # footer is for gaps Daniel can close; nagging him twice a week about an API Reddit will not
    # grant is just noise, so a deliberately-disabled channel stays silent.
    reddit_enabled = bool(cfg.get("reddit_enabled", True))
    reddit_client_id = env.get("REDDIT_CLIENT_ID", "") or os.environ.get("REDDIT_CLIENT_ID", "")
    reddit_client_secret = env.get("REDDIT_CLIENT_SECRET", "") or os.environ.get("REDDIT_CLIENT_SECRET", "")
    reddit_bearer = ""

    # normalize_host() matters here: Daniel's shell exports OLLAMA_HOST=0.0.0.0 (the server bind
    # value), which is not a dialable client URL.
    ollama_host = local_llm.normalize_host(env.get("OLLAMA_HOST") or os.environ.get("OLLAMA_HOST", ""))
    model = local_llm.probe(ollama_host, env.get("LOCAL_MODEL", ""))
    if not model:
        blind.append("no local model")

    snapshot: dict = {"date": today, "categories": {}, "urls": []}
    all_cands: list[dict] = []

    with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": UA}) as client:
        if not reddit_enabled:
            print("[reddit] disabled in creators.json (Reddit closed self-serve Data API apps) — skipping")
        elif reddit_client_id and reddit_client_secret:
            try:
                reddit_bearer = reddit_token(client, reddit_client_id, reddit_client_secret)
            except Exception as e:
                print(f"[warn] Reddit OAuth token failed ({e}) — Reddit sections empty. Check .env creds.", file=sys.stderr)
                blind.append("Reddit auth failed")
        else:
            print("[warn] reddit_enabled=true but no REDDIT_CLIENT_ID/SECRET in .env", file=sys.stderr)
            blind.append("Reddit creds missing")

        for cat_id, cat_cfg in categories.items():
            print(f"[category] {cat_id} — {cat_cfg.get('title', cat_id)}")

            repo_activity = None
            watch_repos = cat_cfg.get("github_repos", [])
            if watch_repos:
                print(f"  [fetch] github repo activity ({len(watch_repos)} repos)")
                repo_activity = github_repo_activity(client, watch_repos, gh_token=gh_token, limit=cat_cfg.get("repo_activity_limit", 5), exclude=seen_urls, per_repo_cap=cat_cfg.get("per_repo_cap", 3))
                print(f"    → {len(repo_activity['issues'])} issues, {len(repo_activity['releases'])} releases")

            print("  [fetch] github search")
            gh = github_top(client, cat_cfg.get("github_queries", []), gh_token=gh_token, limit=cat_cfg.get("gh_limit", 5), exclude=seen_urls)
            print(f"    → {len(gh)} repos")

            print("  [fetch] reddit")
            rd = reddit_top(client, cat_cfg.get("reddit_subreddits", []), token=reddit_bearer, limit=cat_cfg.get("rd_limit", 3), exclude=seen_urls)
            print(f"    → {len(rd)} posts")

            print("  [fetch] youtube (yt-dlp)")
            yt = youtube_top(cat_cfg.get("youtube_creators", []), limit=cat_cfg.get("yt_limit", 5), exclude=seen_urls)
            print(f"    → {len(yt)} videos")
            if not yt:
                blind.append("no YouTube hits")

            cat_cands = to_candidates(repo_activity, gh, yt, rd)
            all_cands.extend(cat_cands)
            # Snapshot keeps the old per-category shape so /digest-analyze and the ledger still read it.
            snapshot["categories"][cat_id] = {
                "title": cat_cfg.get("title", cat_id),
                "emoji": cat_cfg.get("emoji", "📦"),
                "repo_activity": repo_activity,
                "github": gh,
                "youtube": yt,
                "reddit": rd,
                "urls": [c["url"] for c in cat_cands],
            }
            snapshot["urls"].extend(c["url"] for c in cat_cands)

    if not all_cands:
        print("[fatal] every source came back empty — nothing to send", file=sys.stderr)
        return 1

    print(f"[rank] {len(all_cands)} candidates")
    heuristic_pass(all_cands, themes)
    if model:
        print(f"[llm] compressing top {min(MAX_JUDGE, len(all_cands))} with {model}")
        done = llm_pass(all_cands, themes, model, ollama_host)
        print(f"    → {done} compressed")
        if not done:
            blind.append("local model unreachable mid-run")

    theme, items = pick_theme(all_cands, themes, avoid=last_theme())
    items = sorted(items, key=lambda c: c["score"], reverse=True)
    print(f"[theme] {theme['label']} — {len(items)} items")

    # The angle is deliberately deterministic. Two attempts at having the local model write it
    # (2026-08-04) produced grammatical nothing — "These updates address model behavior and tooling,
    # saving time by improving session management" — because synthesising across items is judgement,
    # the one job small models fail at. A counted fact is short, true, and cannot degrade.
    angle = (f"{len(items)} of the {len(all_cands)} things that moved since the last fire landed "
             f"on the same subject: {theme['lens']}.")

    fact = workspace_fact(theme["id"])
    blind = list(dict.fromkeys(blind))  # one category today, but never print the same warning twice
    message = build_deep_dive(date_str, theme, angle, items, fact, blind)

    snapshot["theme"] = {"id": theme["id"], "label": theme["label"]}
    snapshot["chosen"] = [{k: c[k] for k in ("kind", "title", "url", "score", "theme", "line")} for c in items[:6]]
    (OUT_DIR / f"digest_{today}.json").write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "latest_urls.txt").write_text("\n".join(c["url"] for c in items[:6]), encoding="utf-8")
    (OUT_DIR / "latest_message.md").write_text(message, encoding="utf-8")

    if dry_run:
        print("\n" + "=" * 60 + "\n" + message + "\n" + "=" * 60)
        print("[dry-run] nothing sent")
        return 0

    print(f"[telegram] → chat {chat_id}")
    send_telegram(token, chat_id, message)
    print("[done]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
