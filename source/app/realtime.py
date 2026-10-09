from __future__ import annotations

from statistics import median
from typing import Iterable


def _number(value):
    if value in (None, "", "-"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def build_realtime_board(
    quotes: Iterable[dict],
    catalog: Iterable[dict] = (),
    limit: int = 100,
) -> dict:
    """Build a transparent intraday board ranking from normalized quotes.

    ``realtime_strength`` is deliberately named differently from RPS50: it is
    an intraday percentile based on the current percentage change, not a
    replacement for the historical 50-day RPS calculation.  Historical RPS50
    remains available in the daily report and can be supplied by a snapshot
    source when an exact upstream value is available.
    """
    names = {
        str(row.get("sector_code") or row.get("symbol") or "").strip(): str(
            row.get("sector_name") or row.get("name") or ""
        ).strip()
        for row in catalog
        if str(row.get("sector_code") or row.get("symbol") or "").strip()
    }
    rows: list[dict] = []
    seen: set[str] = set()
    for quote in quotes:
        symbol = str(quote.get("symbol") or quote.get("code") or "").strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        pct_change = _number(quote.get("pct_change"))
        amount = _number(quote.get("amount"))
        row = {
            **quote,
            "symbol": symbol,
            "sector_code": symbol,
            "name": str(quote.get("name") or names.get(symbol) or symbol),
            "sector_name": str(quote.get("name") or names.get(symbol) or symbol),
            "pct_change": pct_change,
            "amount": amount,
        }
        rows.append(row)

    rows.sort(
        key=lambda row: (
            row.get("pct_change") is None,
            -(row.get("pct_change") or 0),
            -(row.get("amount") or 0),
            row["symbol"],
        )
    )
    total = len(rows)
    for index, row in enumerate(rows, start=1):
        row["realtime_rank"] = index
        row["realtime_strength"] = round(
            100 if total <= 1 else (total - index) / (total - 1) * 100,
            2,
        )

    valid_pct = [row["pct_change"] for row in rows if row["pct_change"] is not None]
    rising = sum(value > 0 for value in valid_pct)
    falling = sum(value < 0 for value in valid_pct)
    flat = sum(value == 0 for value in valid_pct)
    safe_limit = max(1, min(int(limit), 500))
    selected = rows[:safe_limit]
    return {
        "quotes": selected,
        "stats": {
            "total": total,
            "returned": len(selected),
            "rising": rising,
            "falling": falling,
            "flat": flat,
            "median_pct_change": round(float(median(valid_pct)), 4) if valid_pct else None,
            "top_gainers": [
                {"symbol": row["symbol"], "name": row["name"], "pct_change": row["pct_change"]}
                for row in rows[:5]
                if row.get("pct_change") is not None
            ],
            "top_losers": [
                {"symbol": row["symbol"], "name": row["name"], "pct_change": row["pct_change"]}
                for row in sorted(
                    (row for row in rows if row.get("pct_change") is not None),
                    key=lambda item: (item["pct_change"], -(item.get("amount") or 0)),
                )[:5]
            ],
        },
    }
