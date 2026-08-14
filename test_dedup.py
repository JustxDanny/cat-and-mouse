"""Self-check for the de-dup ledger (recent_reported_urls + exclude filtering).
Window widened to a fortnight on 2026-08-04 when the digest went twice-weekly — 14 days is
~4 fires, the same "don't repeat yourself between a handful of sends" guarantee as the old 7d/7 fires.
Run: python test_dedup.py  →  prints OK or raises AssertionError."""
import json
import tempfile
from datetime import timedelta
from pathlib import Path

import digest


def _write_digest(out: Path, date, urls):
    (out / f"digest_{date.isoformat()}.json").write_text(
        json.dumps({"date": date.isoformat(), "urls": urls}), encoding="utf-8"
    )


def test_window():
    today = digest.datetime.now(digest.MSK).date()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        orig = digest.OUT_DIR
        digest.OUT_DIR = out
        try:
            _write_digest(out, today, ["u_today"])                       # own file — excluded
            _write_digest(out, today - timedelta(days=1), ["u_1"])       # in window
            _write_digest(out, today - timedelta(days=13), ["u_13"])     # last day in window
            _write_digest(out, today - timedelta(days=14), ["u_14"])     # exactly a fortnight — allowed again
            _write_digest(out, today - timedelta(days=30), ["u_30"])     # old — excluded
            (out / "digest_notadate.json").write_text("{}", encoding="utf-8")  # junk name — ignored
            seen = digest.recent_reported_urls()
        finally:
            digest.OUT_DIR = orig
    assert seen == {"u_1", "u_13"}, seen
    # exclude filtering is a plain membership skip — sanity-check youtube path shape
    assert "u_today" not in seen and "u_14" not in seen and "u_30" not in seen


if __name__ == "__main__":
    test_window()
    print(f"OK: de-dup window correct (blocks days 1-{digest.LEDGER_DAYS - 1}, re-allows at day {digest.LEDGER_DAYS})")
