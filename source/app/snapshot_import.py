from __future__ import annotations

from datetime import date
import math
from typing import Iterable


class SnapshotValidationError(ValueError):
    """Raised when an imported snapshot is incomplete or ambiguous."""


def normalize_snapshot_rows(rows: Iterable[object]) -> list[dict]:
    """Validate source rank/RPS snapshot rows without deriving any values."""
    normalized: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for index, value in enumerate(rows, start=1):
        item = value.model_dump() if hasattr(value, "model_dump") else dict(value)  # type: ignore[arg-type]
        try:
            trade_date = date.fromisoformat(str(item["trade_date"]).strip())
        except (KeyError, TypeError, ValueError) as exc:
            raise SnapshotValidationError(
                f"第 {index} 行 trade_date 必须是 YYYY-MM-DD"
            ) from exc
        if trade_date > date.today():
            raise SnapshotValidationError(f"第 {index} 行 trade_date 不能晚于今天")

        sector_type = str(item.get("sector_type", "")).strip().lower()
        if sector_type not in {"concept", "industry"}:
            raise SnapshotValidationError(
                f"第 {index} 行 sector_type 必须是 concept 或 industry"
            )
        sector_code = str(item.get("sector_code", "")).strip()
        sector_name = str(item.get("sector_name", "")).strip()
        if not sector_code or len(sector_code) > 32:
            raise SnapshotValidationError(f"第 {index} 行 sector_code 不能为空且不能超过 32 个字符")
        if not sector_name or len(sector_name) > 100:
            raise SnapshotValidationError(f"第 {index} 行 sector_name 不能为空且不能超过 100 个字符")

        rank = _finite_number(item.get("rank"), index, "rank")
        if int(rank) != rank or rank < 1 or rank > 100000:
            raise SnapshotValidationError(f"第 {index} 行 rank 必须是 1 到 100000 的整数")
        rps50 = _finite_number(item.get("rps50"), index, "rps50")
        if not 0 <= rps50 <= 100:
            raise SnapshotValidationError(f"第 {index} 行 rps50 必须在 0 到 100 之间")
        pct_change = _finite_number(item.get("pct_change"), index, "pct_change")
        close = _finite_number(item.get("close"), index, "close")
        if close <= 0:
            raise SnapshotValidationError(f"第 {index} 行 close 必须大于 0")
        amount = _finite_number(item.get("amount"), index, "amount")
        if amount < 0:
            raise SnapshotValidationError(f"第 {index} 行 amount 不能小于 0")

        volume = item.get("volume")
        if volume is not None:
            volume = _finite_number(volume, index, "volume")
            if volume < 0:
                raise SnapshotValidationError(f"第 {index} 行 volume 不能小于 0")

        key = (trade_date.isoformat(), sector_type, sector_code.lower())
        if key in seen:
            raise SnapshotValidationError(
                f"第 {index} 行与同一日期/板块重复；请先合并冲突数据，不会自动覆盖"
            )
        seen.add(key)
        normalized.append(
            {
                "trade_date": trade_date.isoformat(),
                "sector_type": sector_type,
                "sector_code": sector_code,
                "sector_name": sector_name,
                "close": close,
                "pct_change": pct_change,
                "amount": amount,
                "volume": volume,
                "rank": int(rank),
                "rps50": rps50,
                "source_rank": int(rank),
                "source_rps50": rps50,
                "data_source": "snapshot_import",
            }
        )
    if not normalized:
        raise SnapshotValidationError("快照不能为空")
    return normalized


def _finite_number(value: object, index: int, field: str) -> float:
    if value is None or isinstance(value, bool):
        raise SnapshotValidationError(f"第 {index} 行 {field} 缺失或不是数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError(f"第 {index} 行 {field} 不是数字") from exc
    if not math.isfinite(number):
        raise SnapshotValidationError(f"第 {index} 行 {field} 不能是 NaN 或无穷大")
    return number
