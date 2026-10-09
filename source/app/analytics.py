from __future__ import annotations

import json
from typing import Any

import pandas as pd

from .db import Database
from .flow_summary import attach_five_day_flow


MODULE_TITLES = {
    # Keep the legacy API key for compatibility with existing callers.
    "historical_top100": "20日历史前300序列表",
    "daily_up": "单日强势前进 TOP 榜",
    "daily_down": "单日走弱 TOP 榜",
    "five_day_up": "最近5日强势前进 TOP 榜",
    "five_day_down": "最近5日走弱 TOP 榜",
    "five_day_top100_up": "百名内最近5日强势前进 TOP 榜",
    "ten_day_six_up": "最近10天至少有6天名次前进榜",
    # Keep the former module keys as compatibility aliases for existing clients.
    "five_day_consecutive_up": "最近10天至少有6天名次前进榜",
    "ten_day_consecutive_up": "最近10天至少有6天名次前进榜",
    "t_groups": "T0/T1/T2 主线最终格局",
    "trend": "核心趋势总结",
    "strategy": "操作策略",
}

HISTORY_DAYS = 20
HISTORY_TOP_N = 300
TOP100_RANK_LIMIT = 100
TEN_DAY_RANK_WINDOW_DAYS = 10
TEN_DAY_MIN_ADVANCE_DAYS = 6


def recalculate_metrics(
    database: Database,
    sector_type: str,
    window: int = 50,
    dataset_id: str | None = None,
) -> int:
    """Calculate local RPS50/rank values and persist them.

    If a row came from a structured snapshot and contains ``source_rank`` or
    ``source_rps50``, those values are retained as the displayed metrics.  A
    normal history provider has no source rank, so the local calculation is
    used instead.
    """
    rows = database.get_sector_rows(sector_type, dataset_id=dataset_id)
    if not rows:
        return 0
    frame = _prepare_frame(rows)
    date_values = _date_values(frame)
    frame = _calculate_rps_and_rank(frame, window)
    frame = _calculate_two_day_change(frame, date_values)
    metrics = []
    for row in frame[["dataset_id", "trade_date", "sector_type", "sector_code", "rps50", "rank"]].to_dict(
        orient="records"
    ):
        metrics.append(
            {
                **row,
                "trade_date": pd.Timestamp(row["trade_date"]).strftime("%Y-%m-%d"),
            }
        )
    return database.update_metrics(metrics)


def build_report(
    database: Database,
    sector_type: str,
    window: int = 50,
    report_date: str | None = None,
    dataset_id: str | None = None,
) -> dict[str, Any]:
    """Build the fixed review modules used by the web UI/LLM."""
    rows = database.get_sector_rows(sector_type, dataset_id=dataset_id)
    if not rows:
        raise ValueError(f"暂无 {sector_type} 板块数据，请先执行同步")

    frame = _calculate_rps_and_rank(_prepare_frame(rows), window)
    all_dates = _date_values(frame)
    available_dates = [item.strftime("%Y-%m-%d") for item in all_dates]
    if not available_dates:
        raise ValueError("数据中没有有效交易日")

    coverage = (
        frame.assign(report_date=frame["trade_date"].dt.strftime("%Y-%m-%d"))
        .groupby("report_date")["sector_code"]
        .nunique()
    )
    max_coverage = int(coverage.max()) if not coverage.empty else 0
    minimum_coverage = max(1, int(max_coverage * 0.8))

    if report_date and report_date in available_dates:
        selected_date = report_date
    else:
        # A provider can return a small number of sectors for the next trading
        # day before the remaining sectors are available.  Prefer the newest
        # date with at least 80% of normal coverage.
        eligible_dates = coverage[coverage >= minimum_coverage].index.tolist()
        selected_date = eligible_dates[-1] if eligible_dates else available_dates[-1]

    selected = pd.Timestamp(selected_date)
    latest = frame[frame["trade_date"] == selected].copy()
    if latest.empty:
        raise ValueError(f"没有找到交易日 {selected_date} 的数据")

    dates_until_selected = [item for item in all_dates if item <= selected]
    history_dates = dates_until_selected[-HISTORY_DAYS:]
    history_date_strings = [item.strftime("%Y-%m-%d") for item in history_dates]
    previous_date = dates_until_selected[-2] if len(dates_until_selected) >= 2 else None
    two_day_start = dates_until_selected[-3] if len(dates_until_selected) >= 3 else None

    frame = _calculate_two_day_change(frame, dates_until_selected)
    latest = frame[frame["trade_date"] == selected].copy()
    five_day = _five_day_strength(frame, selected, dates_until_selected)
    latest = _attach_five_day_metrics(latest, five_day)
    latest = latest.sort_values(["rank", "rps50", "sector_code"], ascending=[True, False, True])

    daily = _daily_rank_change(frame, selected, previous_date)
    daily_up = daily[daily["rank_change_1d"] > 0].sort_values(
        ["rank_change_1d", "rps_change_1d", "sector_code"],
        ascending=[False, False, True],
    )
    daily_down = daily[daily["rank_change_1d"] < 0].sort_values(
        ["rank_change_1d", "rps_change_1d", "sector_code"],
        ascending=[True, True, True],
    )

    five_up = five_day[five_day["rank_change_5d"] > 0].sort_values(
        ["rank_change_5d", "rps_change_5d", "sector_code"],
        ascending=[False, False, True],
    )
    five_down = five_day[five_day["rank_change_5d"] < 0].sort_values(
        ["rank_change_5d", "rps_change_5d", "sector_code"],
        ascending=[True, True, True],
    )
    five_day_top100_up = _top100_five_day_up(five_day)
    ten_day_six_up = _ten_day_at_least_six_rank_up(
        frame, selected, dates_until_selected
    )

    t_groups = _build_t_groups(latest, five_day)
    trend = _deterministic_trend(latest, five_day, daily_up, daily_down, t_groups)
    strategy = _build_strategy(latest, daily_up, daily_down, five_up, t_groups)
    historical_top100 = _historical_top100(frame, latest, history_dates)

    selected_coverage = int(coverage.get(selected_date, 0))
    partial_dates = [
        date_string
        for date_string in history_date_strings
        if int(coverage.get(date_string, 0)) < max_coverage
    ]
    source_rows = int(latest["source_rank"].notna().sum()) if "source_rank" in latest else 0
    warnings: list[str] = []
    if len(history_dates) < HISTORY_DAYS:
        warnings.append(
            f"当前仅有 {len(history_dates)} 个可用交易日，未达到 {HISTORY_DAYS} 日历史表要求"
        )
    if len(dates_until_selected) < TEN_DAY_RANK_WINDOW_DAYS:
        warnings.append(
            f"最近10天至少有6天名次前进榜需要至少 {TEN_DAY_RANK_WINDOW_DAYS} 个可用交易日"
        )
    if partial_dates:
        warnings.append("部分历史日期板块数量不完整，缺失单元格会显示为 —")
    if len(dates_until_selected) < window + 1:
        warnings.append(
            f"RPS{window} 需要至少 {window + 1} 个交易日；历史不足的位置显示为 —，不会用其他指标冒充"
        )

    rank_mode = (
        "source_snapshot"
        if source_rows == len(latest) and source_rows
        else "mixed"
        if source_rows
        else "local_calculated"
    )
    data_status = {
        "dataset_id": dataset_id or "legacy",
        "latest_available_date": available_dates[-1],
        "selected_date_coverage": selected_coverage,
        "normal_sector_coverage": max_coverage,
        "coverage_ratio": _safe_float(selected_coverage / max_coverage if max_coverage else 0),
        "history_dates_available": len(history_dates),
        "history_dates_required": HISTORY_DAYS,
        "partial_dates": partial_dates,
        "newer_dates_ignored": [date for date in available_dates if date > selected_date],
        "rank_mode": rank_mode,
        "source_rank_rows": source_rows,
        "warnings": warnings,
    }
    data_window = {
        "history_dates": history_date_strings,
        "rps_window": window,
        "sector_count": int(latest["sector_code"].nunique()),
        "normal_sector_count": max_coverage,
        "previous_date": previous_date.strftime("%Y-%m-%d") if previous_date is not None else None,
        "two_day_start": two_day_start.strftime("%Y-%m-%d") if two_day_start is not None else None,
        "five_day_start": five_day["five_day_start"].iloc[0] if not five_day.empty else None,
        "ten_day_rank_start": (
            dates_until_selected[-TEN_DAY_RANK_WINDOW_DAYS].strftime("%Y-%m-%d")
            if len(dates_until_selected) >= TEN_DAY_RANK_WINDOW_DAYS
            else None
        ),
        "ten_day_rank_required": TEN_DAY_RANK_WINDOW_DAYS,
        "ten_day_min_advance_days": TEN_DAY_MIN_ADVANCE_DAYS,
    }

    module1 = {
        "title": MODULE_TITLES["historical_top100"],
        "dates": history_date_strings,
        "row_count": len(historical_top100["rows"]),
        "rows": historical_top100["rows"],
        "cell_definition": "每个日期单元格包含排名、RPS50、收盘价、涨跌幅、成交额；最后一列为最近3个交易日第一天到最新一天的名次变动；— 表示数据不足或该日期没有该板块数据",
        "rank_change_3d_baseline_date": historical_top100["rank_change_3d_baseline_date"],
        "rank_change_3d_definition": historical_top100["rank_change_3d_definition"],
    }
    module2 = {
        "title": MODULE_TITLES["daily_up"],
        "compare_date": previous_date.strftime("%Y-%m-%d") if previous_date is not None else None,
        "report_date": selected_date,
        "rows": _records(daily_up.head(20)),
    }
    module3 = {
        "title": MODULE_TITLES["daily_down"],
        "compare_date": previous_date.strftime("%Y-%m-%d") if previous_date is not None else None,
        "report_date": selected_date,
        "rows": _records(daily_down.head(20)),
    }
    module4 = {
        "title": MODULE_TITLES["five_day_up"],
        "start_date": five_day["five_day_start"].iloc[0] if not five_day.empty else None,
        "report_date": selected_date,
        "rows": _records(five_up.head(20)),
    }
    module5 = {
        "title": MODULE_TITLES["five_day_down"],
        "start_date": five_day["five_day_start"].iloc[0] if not five_day.empty else None,
        "report_date": selected_date,
        "rows": _records(five_down.head(20)),
    }
    module_top100 = {
        "title": MODULE_TITLES["five_day_top100_up"],
        "scope": f"最新排名≤{TOP100_RANK_LIMIT}",
        "rank_limit": TOP100_RANK_LIMIT,
        "start_date": five_day_top100_up["five_day_start"].iloc[0]
        if not five_day_top100_up.empty
        else (five_day["five_day_start"].iloc[0] if not five_day.empty else None),
        "report_date": selected_date,
        "eligible_count": int(len(five_day_top100_up)),
        "rows": _records(five_day_top100_up.head(20)),
    }
    module_ten_day = {
        "title": MODULE_TITLES["ten_day_six_up"],
        "required_days": TEN_DAY_RANK_WINDOW_DAYS,
        "min_advance_days": TEN_DAY_MIN_ADVANCE_DAYS,
        "comparison_days": TEN_DAY_RANK_WINDOW_DAYS - 1,
        "available_days": min(len(dates_until_selected), TEN_DAY_RANK_WINDOW_DAYS),
        "start_date": (
            dates_until_selected[-TEN_DAY_RANK_WINDOW_DAYS].strftime("%Y-%m-%d")
            if len(dates_until_selected) >= TEN_DAY_RANK_WINDOW_DAYS
            else None
        ),
        "report_date": selected_date,
        "rule": "最近10个可用交易日的9个相邻区间中，排名严格前进至少6天（排名数字变小）；缺失数据的区间不计入前进天数，报告日和窗口首日缺少排名的板块不入榜。",
        "eligible_count": int(len(ten_day_six_up)),
        "rows": _records(ten_day_six_up.head(20)),
    }
    module6 = {
        "title": MODULE_TITLES["t_groups"],
        "rules": {
            "T0": "排名≤10 且 RPS50≥85",
            "T1": "排名11–30 且 RPS50≥75",
            "T2": "排名31–50 且 RPS50≥65，或最近5日排名前进≥15",
        },
        "groups": t_groups,
    }
    module7 = {"title": MODULE_TITLES["trend"], **trend}
    module8 = {"title": MODULE_TITLES["strategy"], **strategy}

    result: dict[str, Any] = {
        "dataset_id": dataset_id or "legacy",
        "sector_type": sector_type,
        "report_date": selected_date,
        "available_dates": list(reversed(available_dates[-30:])),
        "data_window": data_window,
        "data_status": data_status,
        # Compatibility fields retained for the earlier UI/API consumers.
        "latest_top100": _records(latest.head(100)),
        "strongest_today": _records(daily_up.head(10)),
        "weakest_today": _records(daily_down.head(10)),
        "strongest_5d": _records(five_up.head(10)),
        "weakest_5d": _records(five_down.head(10)),
        "t_groups": t_groups,
        "history": _records(
            frame[
                frame["trade_date"].dt.strftime("%Y-%m-%d").isin(history_date_strings)
                & frame["sector_code"].isin(latest.head(HISTORY_TOP_N)["sector_code"])
            ].sort_values(["trade_date", "rank"])
        ),
        "trend_summary": trend,
        "strategy": strategy,
        "modules": {
            "historical_top100": module1,
            "daily_up": module2,
            "daily_down": module3,
            "five_day_up": module4,
            "five_day_down": module5,
            "five_day_top100_up": module_top100,
            "ten_day_six_up": module_ten_day,
            "five_day_consecutive_up": module_ten_day,
            "ten_day_consecutive_up": module_ten_day,
            "t_groups": module6,
            "trend": module7,
            "strategy": module8,
        },
        "module1_historical_top100": module1,
        "module2_daily_up": module2,
        "module3_daily_down": module3,
        "module4_five_day_up": module4,
        "module5_five_day_down": module5,
        "module6_five_day_top100_up": module_top100,
        "module7_ten_day_six_up": module_ten_day,
        "module7_five_day_consecutive_up": module_ten_day,
        "module7_ten_day_consecutive_up": module_ten_day,
        "module6_t_groups": module6,
        "module7_trend": module7,
        "module8_strategy": module8,
    }
    attach_five_day_flow(database, sector_type, selected_date, module_ten_day)
    result["llm_input"] = _llm_input_compact(result)
    return result


def _prepare_frame(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    if "dataset_id" not in frame:
        frame["dataset_id"] = "legacy"
    frame["dataset_id"] = frame["dataset_id"].fillna("legacy").astype(str)
    for column in [
        "close",
        "pct_change",
        "amount",
        "volume",
        "rps50",
        "rank",
        "source_rank",
        "source_rps50",
    ]:
        if column not in frame:
            frame[column] = None
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame["sector_code"] = frame["sector_code"].astype(str).str.strip()
    frame["sector_name"] = frame["sector_name"].astype(str).str.strip()
    frame = frame.dropna(subset=["trade_date", "sector_code", "close"])
    return frame.sort_values(["sector_code", "trade_date"]).reset_index(drop=True)


def _date_values(frame: pd.DataFrame) -> list[pd.Timestamp]:
    if frame.empty:
        return []
    return sorted(pd.Timestamp(value) for value in frame["trade_date"].dropna().unique())


def _calculate_rps_and_rank(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    frame = frame.copy()
    if frame.empty:
        return frame
    safe_window = max(1, int(window))
    frame["return_window"] = frame.groupby("sector_code")["close"].transform(
        lambda series: series / series.shift(safe_window) - 1
    )

    scored: list[pd.DataFrame] = []
    for trade_date, group in frame.groupby("trade_date", sort=False):
        group = group.copy()
        calculated_rps = group["return_window"].rank(pct=True, method="average") * 100
        group["calculated_rps50"] = calculated_rps
        group["calculated_rank"] = group["calculated_rps50"].rank(
            ascending=False, method="min"
        )
        group["rps50"] = group["source_rps50"].fillna(group["calculated_rps50"])
        group["rank"] = group["source_rank"].fillna(group["calculated_rank"])
        group["rank_mode"] = group["source_rank"].map(
            lambda value: "source" if pd.notna(value) else "calculated"
        )
        group["trade_date"] = pd.Timestamp(trade_date)
        scored.append(group)
    return pd.concat(scored, ignore_index=True).sort_values(
        ["sector_code", "trade_date"]
    ).reset_index(drop=True)


def _calculate_two_day_change(
    frame: pd.DataFrame,
    date_values: list[pd.Timestamp] | None = None,
) -> pd.DataFrame:
    """Compare each row with the same sector two globally available dates ago."""
    if frame.empty:
        return frame.copy()
    frame = frame.copy()
    date_values = date_values or _date_values(frame)
    mapping = {
        value: date_values[index - 2] if index >= 2 else pd.NaT
        for index, value in enumerate(date_values)
    }
    baseline = frame[["sector_code", "trade_date", "rps50", "rank"]].rename(
        columns={
            "trade_date": "comparison_date",
            "rps50": "rps50_2d_ago",
            "rank": "rank_2d_ago",
        }
    )
    result = frame.copy()
    result["comparison_date"] = result["trade_date"].map(mapping)
    result = result.merge(baseline, on=["sector_code", "comparison_date"], how="left")
    result["rps_change_2d"] = result["rps50"] - result["rps50_2d_ago"]
    result["rank_change_2d"] = result["rank_2d_ago"] - result["rank"]
    result["rank_change_2d_label"] = result["rank_change_2d"].map(rank_change_label)
    result = result.drop(columns=["comparison_date"])
    return result


def _daily_rank_change(
    frame: pd.DataFrame,
    selected: pd.Timestamp,
    previous: pd.Timestamp | None,
) -> pd.DataFrame:
    current = frame[frame["trade_date"] == selected].copy()
    if previous is None:
        current["rank_1d_ago"] = None
        current["rps50_1d_ago"] = None
    else:
        baseline = frame[frame["trade_date"] == previous][
            ["sector_code", "rank", "rps50", "close"]
        ].rename(
            columns={
                "rank": "rank_1d_ago",
                "rps50": "rps50_1d_ago",
                "close": "close_1d_ago",
            }
        )
        current = current.merge(baseline, on="sector_code", how="left")
    current["rank_change_1d"] = current["rank_1d_ago"] - current["rank"]
    current["rps_change_1d"] = current["rps50"] - current["rps50_1d_ago"]
    current["rank_change_1d_label"] = current["rank_change_1d"].map(rank_change_label)
    return current


def _five_day_strength(
    frame: pd.DataFrame,
    selected: pd.Timestamp,
    date_values: list[pd.Timestamp] | None = None,
) -> pd.DataFrame:
    date_values = date_values or _date_values(frame)
    dates = [value for value in date_values if value <= selected]
    window_dates = dates[-5:]
    columns = [
        "sector_code",
        "sector_name",
        "start_rank",
        "rank",
        "start_rps50",
        "rps50",
        "start_close",
        "end_close",
        "return_5d",
        "rank_change_5d",
        "rps_change_5d",
        "five_day_start",
        "five_day_end",
        "rank_change_5d_label",
    ]
    if len(window_dates) < 2:
        return pd.DataFrame(columns=columns)
    start_date = window_dates[0]
    start = frame[frame["trade_date"] == start_date][
        ["sector_code", "rank", "rps50", "close"]
    ].rename(
        columns={
            "rank": "start_rank",
            "rps50": "start_rps50",
            "close": "start_close",
        }
    )
    end = frame[frame["trade_date"] == selected][
        [
            "sector_code",
            "sector_name",
            "rank",
            "rps50",
            "close",
            "pct_change",
            "amount",
            "volume",
        ]
    ].rename(columns={"close": "end_close"})
    result = start.merge(end, on="sector_code", how="inner")
    result["return_5d"] = (result["end_close"] / result["start_close"] - 1) * 100
    result["rank_change_5d"] = result["start_rank"] - result["rank"]
    result["rps_change_5d"] = result["rps50"] - result["start_rps50"]
    result["five_day_start"] = start_date.strftime("%Y-%m-%d")
    result["five_day_end"] = selected.strftime("%Y-%m-%d")
    result["rank_change_5d_label"] = result["rank_change_5d"].map(rank_change_label)
    return result.sort_values(["rank_change_5d", "rps_change_5d"], ascending=[False, False]).reset_index(
        drop=True
    )


def _top100_five_day_up(five_day: pd.DataFrame) -> pd.DataFrame:
    """Keep only current top-100 boards that advanced over the five-day window."""
    if five_day.empty:
        return five_day.copy()
    result = five_day[
        (pd.to_numeric(five_day["rank"], errors="coerce") <= TOP100_RANK_LIMIT)
        & (pd.to_numeric(five_day["rank_change_5d"], errors="coerce") > 0)
    ].copy()
    return result.sort_values(
        ["rank_change_5d", "rps_change_5d", "sector_code"],
        ascending=[False, False, True],
    ).reset_index(drop=True)


def _ten_day_at_least_six_rank_up(
    frame: pd.DataFrame,
    selected: pd.Timestamp,
    date_values: list[pd.Timestamp] | None = None,
) -> pd.DataFrame:
    """Find boards with at least six improving rank transitions in ten dates.

    The window contains the latest ten available trading dates, including the
    report date, and therefore has nine adjacent comparisons.  An improvement
    means that the numeric rank becomes smaller.  Missing internal comparisons
    are ignored; the first and last ranks must exist so the cumulative change
    and rank path can be displayed.
    """
    columns = [
        "sector_code",
        "sector_name",
        "start_rank",
        "rank",
        "start_rps50",
        "rps50",
        "start_close",
        "end_close",
        "pct_change",
        "amount",
        "volume",
        "return_10d",
        "rank_change_10d",
        "rps_change_10d",
        "rank_path_10d",
        "up_days_10d",
        "up_days_10d_label",
        "valid_comparisons_10d",
        "ten_day_start",
        "ten_day_end",
        "rank_change_10d_label",
    ]
    date_values = date_values or _date_values(frame)
    window_dates = [value for value in date_values if value <= selected][-TEN_DAY_RANK_WINDOW_DAYS:]
    if len(window_dates) < TEN_DAY_RANK_WINDOW_DAYS:
        return pd.DataFrame(columns=columns)

    window = frame[frame["trade_date"].isin(window_dates)][
        ["sector_code", "trade_date", "rank"]
    ].copy()
    if window.empty:
        return pd.DataFrame(columns=columns)

    rank_matrix = window.pivot_table(
        index="sector_code",
        columns="trade_date",
        values="rank",
        aggfunc="last",
    ).reindex(columns=window_dates)
    previous_ranks = rank_matrix.iloc[:, :-1]
    current_ranks = rank_matrix.iloc[:, 1:]
    previous_values = previous_ranks.to_numpy()
    current_values = current_ranks.to_numpy()
    comparable = pd.DataFrame(
        previous_ranks.notna().to_numpy() & current_ranks.notna().to_numpy(),
        index=rank_matrix.index,
    )
    daily_improvement = pd.DataFrame(
        comparable.to_numpy()
        & (current_values < previous_values),
        index=rank_matrix.index,
    )
    up_days = daily_improvement.sum(axis=1)
    valid_comparisons = comparable.sum(axis=1)
    has_endpoints = rank_matrix.iloc[:, 0].notna() & rank_matrix.iloc[:, -1].notna()
    eligible = (up_days >= TEN_DAY_MIN_ADVANCE_DAYS) & has_endpoints
    eligible_codes = eligible[eligible].index.tolist()
    if not eligible_codes:
        return pd.DataFrame(columns=columns)

    start_date = window_dates[0]
    start = frame[frame["trade_date"] == start_date][
        ["sector_code", "rank", "rps50", "close"]
    ].rename(
        columns={
            "rank": "start_rank",
            "rps50": "start_rps50",
            "close": "start_close",
        }
    )
    end = frame[frame["trade_date"] == selected][
        [
            "sector_code",
            "sector_name",
            "rank",
            "rps50",
            "close",
            "pct_change",
            "amount",
            "volume",
        ]
    ].rename(columns={"close": "end_close"})
    start = start[start["sector_code"].isin(eligible_codes)]
    end = end[end["sector_code"].isin(eligible_codes)]
    result = start.merge(end, on="sector_code", how="inner")
    if result.empty:
        return pd.DataFrame(columns=columns)

    result["return_10d"] = (result["end_close"] / result["start_close"] - 1) * 100
    result["rank_change_10d"] = result["start_rank"] - result["rank"]
    result["rps_change_10d"] = result["rps50"] - result["start_rps50"]
    result["rank_path_10d"] = result["sector_code"].map(
        lambda code: _format_rank_path(rank_matrix.loc[code].tolist())
    )
    result["up_days_10d"] = result["sector_code"].map(up_days)
    result["up_days_10d_label"] = result["up_days_10d"].map(lambda value: f"{int(value)}天")
    result["valid_comparisons_10d"] = result["sector_code"].map(valid_comparisons)
    result["ten_day_start"] = start_date.strftime("%Y-%m-%d")
    result["ten_day_end"] = selected.strftime("%Y-%m-%d")
    result["rank_change_10d_label"] = result["rank_change_10d"].map(rank_change_label)
    return result.sort_values(
        ["up_days_10d", "rank_change_10d", "rps_change_10d", "return_10d", "sector_code"],
        ascending=[False, False, False, False, True],
    ).reset_index(drop=True)


def _five_day_consecutive_rank_up(
    frame: pd.DataFrame,
    selected: pd.Timestamp,
    date_values: list[pd.Timestamp] | None = None,
) -> pd.DataFrame:
    """Compatibility wrapper for the former module-7 helper name."""
    return _ten_day_at_least_six_rank_up(frame, selected, date_values)


def _format_rank_path(values: list) -> str:
    return "→".join(
        "—" if value is None or pd.isna(value) else str(int(round(float(value))))
        for value in values
    )


def _attach_five_day_metrics(latest: pd.DataFrame, five_day: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "sector_code",
        "start_rank",
        "start_rps50",
        "return_5d",
        "rank_change_5d",
        "rps_change_5d",
        "five_day_start",
        "five_day_end",
        "rank_change_5d_label",
    ]
    if five_day.empty:
        result = latest.copy()
        for column in columns[1:]:
            result[column] = None
        return result
    return latest.merge(five_day[columns], on="sector_code", how="left")


def _build_t_groups(latest: pd.DataFrame, five_day: pd.DataFrame) -> dict[str, list[dict]]:
    enriched = latest.copy()
    if "rank_change_5d" not in enriched:
        enriched = _attach_five_day_metrics(enriched, five_day)

    def group_rows(mask: pd.Series, reason: str) -> list[dict]:
        group = enriched[mask].copy()
        group["classification_reason"] = reason
        return _records(group.sort_values(["rank", "rps50"], ascending=[True, False]))

    t0 = (enriched["rank"] <= 10) & (enriched["rps50"] >= 85)
    t1 = (
        (enriched["rank"] >= 11)
        & (enriched["rank"] <= 30)
        & (enriched["rps50"] >= 75)
    )
    t2 = (
        (
            (enriched["rank"] >= 31)
            & (enriched["rank"] <= 50)
            & (enriched["rps50"] >= 65)
        )
        | (enriched["rank_change_5d"] >= 15)
    )
    # The final map is easier to read when a board belongs to one tier only.
    # A T0/T1 board that also advanced 15 places remains in its stronger tier;
    # the movement rule is intended to discover emerging T2 candidates.
    t2 = t2 & ~t0 & ~t1
    return {
        "T0": group_rows(t0, "排名≤10 且 RPS50≥85"),
        "T1": group_rows(t1, "排名11–30 且 RPS50≥75"),
        "T2": group_rows(t2, "排名31–50 且 RPS50≥65，或最近5日排名前进≥15"),
    }


def _deterministic_trend(
    latest: pd.DataFrame,
    five_day: pd.DataFrame,
    daily_up: pd.DataFrame,
    daily_down: pd.DataFrame,
    t_groups: dict[str, list[dict]],
) -> dict[str, Any]:
    pct = latest["pct_change"].dropna()
    rising = int((pct > 0).sum())
    falling = int((pct < 0).sum())
    flat = int((pct == 0).sum())
    total = max(1, len(pct))
    top_rps = latest.sort_values(["rps50", "rank"], ascending=[False, True]).head(5)
    top_5d = five_day.sort_values(
        ["rank_change_5d", "rps_change_5d"], ascending=[False, False]
    ).head(5)
    return {
        "breadth": {
            "rising": rising,
            "falling": falling,
            "flat": flat,
            "rising_ratio": _safe_float(rising / total),
            "falling_ratio": _safe_float(falling / total),
        },
        "median_pct_change": _safe_float(pct.median()),
        "daily_up_count": int(len(daily_up)),
        "daily_down_count": int(len(daily_down)),
        "top_rps_sectors": top_rps["sector_name"].tolist(),
        "top_5d_advance_sectors": top_5d["sector_name"].tolist(),
        "t_counts": {name: len(rows) for name, rows in t_groups.items()},
        "headline": _trend_headline(rising, falling, len(pct), len(t_groups.get("T0", []))),
        "observations": [
            f"当日上涨 {rising} 个、下跌 {falling} 个、平盘 {flat} 个板块",
            f"单日排名前进榜 {len(daily_up)} 个，走弱榜 {len(daily_down)} 个",
            f"T0/T1/T2 数量为 {len(t_groups.get('T0', []))}/{len(t_groups.get('T1', []))}/{len(t_groups.get('T2', []))}",
        ],
        "risk_flags": _risk_flags(rising, falling, len(pct), daily_down, latest),
        "caution": "这是基于板块历史行情的量化复盘，不构成投资建议；因果关系需要额外新闻/基本面数据验证。",
    }


def _build_strategy(
    latest: pd.DataFrame,
    daily_up: pd.DataFrame,
    daily_down: pd.DataFrame,
    five_up: pd.DataFrame,
    t_groups: dict[str, list[dict]],
) -> dict[str, Any]:
    t0_codes = {row.get("sector_code") for row in t_groups.get("T0", [])}
    strong_codes = [
        row.get("sector_code")
        for row in (t_groups.get("T1", []) + t_groups.get("T2", []))[:10]
    ]
    strong_code_set = set(strong_codes)
    rotation_codes = [
        code
        for code in five_up.head(10)["sector_code"].tolist()
        if code not in t0_codes and code not in strong_code_set
    ]
    rotation_code_set = set(rotation_codes)
    short_codes = [
        code
        for code in daily_up.head(10)["sector_code"].tolist()
        if code not in t0_codes
        and code not in strong_code_set
        and code not in rotation_code_set
    ]
    occupied_codes = t0_codes | strong_code_set | rotation_code_set | set(short_codes)
    avoid_codes = [
        code
        for code in daily_down.head(10)["sector_code"].tolist()
        if code not in occupied_codes
    ]
    by_code = latest.set_index("sector_code")["sector_name"].to_dict()

    def names(codes) -> list[str]:
        return [by_code.get(code, code) for code in codes if code in by_code]

    total = max(1, len(latest))
    rising_ratio = float((latest["pct_change"] > 0).sum()) / total
    falling_ratio = float((latest["pct_change"] < 0).sum()) / total
    if falling_ratio >= 0.6:
        market_state = "防守"
        allocation = {"core": 30, "strong": 20, "rotation": 10, "short": 0, "cash": 40}
    elif rising_ratio >= 0.6:
        market_state = "扩散"
        allocation = {"core": 45, "strong": 25, "rotation": 15, "short": 5, "cash": 10}
    else:
        market_state = "震荡"
        allocation = {"core": 40, "strong": 25, "rotation": 15, "short": 5, "cash": 15}

    buckets = [
        {
            "name": "核心底仓",
            "reference_percent": allocation["core"],
            "sectors": [row.get("sector_name") for row in t_groups.get("T0", [])[:10]],
            "rule": "只从 T0 中观察；若 T0 为空，不强行补入其他板块。",
        },
        {
            "name": "强势分支",
            "reference_percent": allocation["strong"],
            "sectors": names(strong_codes),
            "rule": "观察 T1/T2 与最近5日排名前进是否同时成立，等待下一交易日验证。",
        },
        {
            "name": "轮动加仓",
            "reference_percent": allocation["rotation"],
            "sectors": names(rotation_codes[:10]),
            "rule": "只观察最近5日排名前进且尚未进入核心/强势分支的板块，等待确认。",
        },
        {
            "name": "短线博弈",
            "reference_percent": allocation["short"],
            "sectors": names(short_codes[:10]),
            "rule": "只作为短周期观察清单，必须设置退出条件，不把单日涨幅当作趋势确认。",
        },
        {
            "name": "坚决回避",
            "reference_percent": 0,
            "sectors": names(avoid_codes),
            "rule": "单日排名明显走弱的板块先进入回避观察，不据此推断具体个股。",
        },
        {
            "name": "现金/等待",
            "reference_percent": allocation["cash"],
            "sectors": [],
            "rule": f"当前宽度状态为{market_state}；当上涨宽度不足、数据不完整或主线无法确认时保留等待空间。",
        },
    ]
    return {
        "disclaimer": "以上百分比是复盘模板权重，不是个性化持仓建议；应按风险承受能力自行调整。",
        "reference_allocation_total_percent": sum(item["reference_percent"] for item in buckets),
        "market_state": market_state,
        "buckets": buckets,
        "validation_next_day": [
            "核心板块是否继续维持较高 RPS50 与排名",
            "最近5日强势板块是否出现单日排名反转",
            "走弱榜数量是否继续扩大，以及历史数据是否完整",
        ],
        "avoid_actions": [
            "不因单日涨幅直接追涨",
            "不把板块排名直接等同于个股买卖信号",
            "不在数据缺失时补写或猜测行情数值",
        ],
    }


def _historical_top100(
    frame: pd.DataFrame,
    latest: pd.DataFrame,
    history_dates: list[pd.Timestamp],
) -> dict[str, Any]:
    top = latest.head(HISTORY_TOP_N)
    codes = top["sector_code"].tolist()
    date_strings = [date.strftime("%Y-%m-%d") for date in history_dates]
    historical = frame[
        frame["sector_code"].isin(codes)
        & frame["trade_date"].isin(history_dates)
    ].copy()
    lookup = {
        (row["sector_code"], pd.Timestamp(row["trade_date"]).strftime("%Y-%m-%d")): row
        for _, row in historical.iterrows()
    }
    rows: list[dict[str, Any]] = []
    for _, current in top.iterrows():
        code = current["sector_code"]
        history: dict[str, Any] = {}
        for date_string in date_strings:
            row = lookup.get((code, date_string))
            history[date_string] = _history_cell(row)
        payload = _clean_record(current.to_dict())
        payload["history"] = history
        # “最近3天”按最近三个可用交易日理解：比较这三个日期的第一天和最新一天。
        baseline_date = date_strings[-3] if len(date_strings) >= 3 else None
        baseline_row = lookup.get((code, baseline_date)) if baseline_date else None
        rank_change_3d = None
        if baseline_row is not None:
            current_rank = current.get("rank")
            baseline_rank = baseline_row.get("rank")
            if pd.notna(current_rank) and pd.notna(baseline_rank):
                rank_change_3d = baseline_rank - current_rank
        payload["rank_change_3d"] = _clean_value(rank_change_3d)
        payload["rank_change_3d_label"] = rank_change_label(rank_change_3d)
        payload["rank_change_2d_label"] = rank_change_label(payload.get("rank_change_2d"))
        rows.append(payload)
    return {
        "dates": date_strings,
        "rows": rows,
        "rank_change_3d_baseline_date": date_strings[-3] if len(date_strings) >= 3 else None,
        "rank_change_3d_definition": "最近3个交易日第一天排名相对最新报告日排名的变化；↑表示排名前进，↓表示排名下降",
    }


def _history_cell(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "rank": _clean_value(row.get("rank")),
        "rps50": _clean_value(row.get("rps50")),
        "pct_change": _clean_value(row.get("pct_change")),
        "close": _clean_value(row.get("close")),
        "amount": _clean_value(row.get("amount")),
        "rank_change_2d": _clean_value(row.get("rank_change_2d")),
        "rank_change_2d_label": rank_change_label(row.get("rank_change_2d")),
    }


def _trend_headline(rising: int, falling: int, total: int, t0_count: int) -> str:
    if total <= 0:
        return "没有足够的当日涨跌幅数据"
    if rising > falling:
        return f"上涨宽度占优，当前有 {t0_count} 个 T0 核心观察板块"
    if falling > rising:
        return "下跌宽度占优，先观察主线是否继续退潮"
    return "涨跌宽度接近，等待排名与 RPS50 的进一步确认"


def _risk_flags(
    rising: int,
    falling: int,
    total: int,
    daily_down: pd.DataFrame,
    latest: pd.DataFrame,
) -> list[str]:
    flags = []
    if total and falling / total >= 0.6:
        flags.append("下跌板块占比达到 60% 以上")
    if len(daily_down) >= max(3, int(total * 0.3)):
        flags.append("单日排名走弱板块较多")
    if latest["source_rank"].notna().sum() == 0:
        flags.append("当前排名为本地计算值，不是上游截图/快照原始排名")
    return flags


def _llm_input(result: dict[str, Any]) -> str:
    source_modules = result["modules"]
    history_module = source_modules["historical_top100"]
    def pick_rows(module_name: str, limit: int = 5) -> list[dict]:
        rows = source_modules[module_name].get("rows", [])[:limit]
        selected = []
        for row in rows:
            selected.append(
                {
                    "sector_code": row.get("sector_code"),
                    "sector_name": row.get("sector_name"),
                    "rank": row.get("rank"),
                    "rps50": row.get("rps50"),
                    "pct_change": row.get("pct_change"),
                    "rank_change_label": row.get("rank_change_1d_label")
                    or row.get("rank_change_5d_label")
                    or row.get("rank_change_10d_label"),
                    "rank_change": row.get("rank_change_1d")
                    if row.get("rank_change_1d") is not None
                    else row.get("rank_change_5d")
                    if row.get("rank_change_5d") is not None
                    else row.get("rank_change_10d"),
                    "return_5d": row.get("return_5d"),
                    "return_10d": row.get("return_10d"),
                    "rank_path_5d": row.get("rank_path_5d"),
                    "rank_path_10d": row.get("rank_path_10d"),
                    "up_days_10d": row.get("up_days_10d"),
                    "valid_comparisons_10d": row.get("valid_comparisons_10d"),
                }
            )
        return selected

    group_rows = {
        name: [
            {
                "sector_name": row.get("sector_name"),
                "rank": row.get("rank"),
                "rps50": row.get("rps50"),
                "reason": row.get("classification_reason"),
            }
            for row in rows[:5]
        ]
        for name, rows in (source_modules["t_groups"].get("groups") or {}).items()
    }
    strategy = source_modules["strategy"]
    llm_modules = {
        "historical_top100": {
            "title": history_module.get("title"),
            "dates": history_module.get("dates", [])[-5:],
            "row_count": history_module.get("row_count", 0),
            "top_rows": [
                {
                    "sector_name": row.get("sector_name"),
                    "rank": row.get("rank"),
                    "rps50": row.get("rps50"),
                    "pct_change": row.get("pct_change"),
                    "rank_change_2d_label": row.get("rank_change_2d_label"),
                }
                for row in history_module.get("rows", [])[:5]
            ],
            "note": "完整历史前300数字已经在本地模块中计算；这里仅给 LLM 代表性摘要。",
        },
        "daily_up": {"title": source_modules["daily_up"].get("title"), "rows": pick_rows("daily_up")},
        "daily_down": {"title": source_modules["daily_down"].get("title"), "rows": pick_rows("daily_down")},
        "five_day_up": {"title": source_modules["five_day_up"].get("title"), "rows": pick_rows("five_day_up")},
        "five_day_down": {"title": source_modules["five_day_down"].get("title"), "rows": pick_rows("five_day_down")},
        "five_day_top100_up": {
            "title": source_modules["five_day_top100_up"].get("title"),
            "scope": source_modules["five_day_top100_up"].get("scope"),
            "eligible_count": source_modules["five_day_top100_up"].get("eligible_count", 0),
            "rows": pick_rows("five_day_top100_up"),
        },
        "ten_day_six_up": {
            "title": source_modules["ten_day_six_up"].get("title"),
            "required_days": source_modules["ten_day_six_up"].get("required_days"),
            "min_advance_days": source_modules["ten_day_six_up"].get("min_advance_days"),
            "comparison_days": source_modules["ten_day_six_up"].get("comparison_days"),
            "start_date": source_modules["ten_day_six_up"].get("start_date"),
            "eligible_count": source_modules["ten_day_six_up"].get("eligible_count", 0),
            "rows": pick_rows("ten_day_six_up"),
        },
        "t_groups": {"title": source_modules["t_groups"].get("title"), "groups": group_rows},
        "trend": {
            "title": source_modules["trend"].get("title"),
            "headline": source_modules["trend"].get("headline"),
            "breadth": source_modules["trend"].get("breadth"),
            "median_pct_change": source_modules["trend"].get("median_pct_change"),
            "top_rps_sectors": source_modules["trend"].get("top_rps_sectors", [])[:5],
            "top_5d_advance_sectors": source_modules["trend"].get("top_5d_advance_sectors", [])[:5],
            "risk_flags": source_modules["trend"].get("risk_flags", []),
        },
        "strategy": {
            "title": strategy.get("title"),
            "disclaimer": strategy.get("disclaimer"),
            "buckets": [
                {
                    "name": bucket.get("name"),
                    "reference_percent": bucket.get("reference_percent"),
                    "sectors": bucket.get("sectors", [])[:3],
                }
                for bucket in strategy.get("buckets", [])
            ],
            "validation_next_day": strategy.get("validation_next_day", []),
            "avoid_actions": strategy.get("avoid_actions", []),
        },
    }
    payload = {
        "protocol": "fixed_10_module_board_review_v1",
        "arrow_definition": "↑n=排名前进n名，↓n=排名下降n名，→0=无变化",
        "report_date": result["report_date"],
        "sector_type": result["sector_type"],
        "data_window": {
            "history_dates": result["data_window"].get("history_dates", []),
            "sector_count": result["data_window"].get("sector_count"),
            "previous_date": result["data_window"].get("previous_date"),
            "five_day_start": result["data_window"].get("five_day_start"),
            "ten_day_rank_start": result["data_window"].get("ten_day_rank_start"),
            "ten_day_rank_required": result["data_window"].get("ten_day_rank_required"),
            "ten_day_min_advance_days": result["data_window"].get("ten_day_min_advance_days"),
        },
        "data_status": result["data_status"],
        "modules": llm_modules,
    }
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def _llm_input_compact(result: dict[str, Any]) -> str:
    """Create a short, human-readable LLM context.

    The web/API response still contains the complete ten-module result.  A
    compact text context keeps the model focused on summarization instead of
    asking it to reprocess a large nested historical matrix.
    """
    modules = result["modules"]
    history = modules["historical_top100"]
    trend = modules["trend"]
    strategy = modules["strategy"]

    def value(item) -> str:
        return "-" if item is None else str(item)

    def rows_text(module_name: str) -> str:
        rows = modules[module_name].get("rows", [])[:5]
        if not rows:
            return "数据不足"
        items = []
        is_ten_day_six_up = module_name == "ten_day_six_up"
        return_key = "return_10d" if is_ten_day_six_up else "return_5d"
        return_label = "10日涨幅" if is_ten_day_six_up else "5日涨幅"
        for row in rows:
            change = row.get("rank_change_1d_label") or row.get("rank_change_5d_label") or "→0"
            if is_ten_day_six_up:
                change = row.get("rank_change_10d_label") or change
                path = f", 名次轨迹{value(row.get('rank_path_10d'))}, 前进{value(row.get('up_days_10d'))}天"
            else:
                path = ""
            items.append(
                f"{value(row.get('sector_name'))}(排名{value(row.get('rank'))}, "
                f"RPS50 {value(row.get('rps50'))}, 涨幅{value(row.get('pct_change'))}%, "
                f"名次{change}{path}, {return_label}{value(row.get(return_key))}%)"
            )
        return "；".join(items)

    group_text = "；".join(
        f"{name}: {', '.join(value(row.get('sector_name')) for row in rows[:5]) or '无'}"
        for name, rows in (modules["t_groups"].get("groups") or {}).items()
    ) or "数据不足"
    bucket_text = "；".join(
        f"{bucket.get('name')} {bucket.get('reference_percent')}%"
        for bucket in strategy.get("buckets", [])
    )
    history_text = "；".join(
        f"{value(row.get('sector_name'))}(排名{value(row.get('rank'))}, RPS50 {value(row.get('rps50'))})"
        for row in history.get("rows", [])[:5]
    ) or "数据不足"
    breadth = trend.get("breadth") or {}
    status = result["data_status"]
    window = result["data_window"]
    return "\n".join(
        [
            "协议：本地程序已计算固定十模块，LLM 只做文字归纳，不重新计算全部历史数字。",
            f"报告日期：{result['report_date']}；板块类型：{result['sector_type']}；板块数：{value(window.get('sector_count'))}。",
            f"历史日期：{','.join(window.get('history_dates', []))}；状态：{';'.join(status.get('warnings', [])) or '覆盖正常'}。",
            f"历史前300代表：{history_text}。",
            f"单日前进：{rows_text('daily_up')}。",
            f"单日走弱：{rows_text('daily_down')}。",
            f"五日前进：{rows_text('five_day_up')}。",
            f"五日走弱：{rows_text('five_day_down')}。",
            f"百名内五日前进（最新排名≤100）：{rows_text('five_day_top100_up')}。",
            f"最近10天至少有6天名次前进：{rows_text('ten_day_six_up')}。规则是最近10个可用交易日的9个相邻区间中，排名严格前进至少6天；缺失数据的区间不计入前进天数，报告日和窗口首日缺少排名的板块不入榜。",
            f"T0/T1/T2：{group_text}。规则：T0排名≤10且RPS50≥85；T1排名11–30且RPS50≥75；T2排名31–50且RPS50≥65或5日排名前进≥15。",
            f"趋势：{value(trend.get('headline'))}；上涨{value(breadth.get('rising'))}，下跌{value(breadth.get('falling'))}；RPS靠前：{','.join(trend.get('top_rps_sectors', [])[:5]) or '无'}；风险：{';'.join(trend.get('risk_flags', [])) or '暂无'}。",
            f"策略模板：{bucket_text}。验证：{'；'.join(strategy.get('validation_next_day', []))}。避免：{'；'.join(strategy.get('avoid_actions', []))}。",
            "完整历史表和全部数字以网页本地模块为准；不要补造数据或新闻。",
        ]
    )


def rank_change_label(value) -> str:
    """Return the fixed Chinese arrow notation used by the review protocol."""
    if value is None or pd.isna(value):
        return "—"
    number = int(round(float(value)))
    if number > 0:
        return f"↑{number}"
    if number < 0:
        return f"↓{abs(number)}"
    return "→0"


def _records(frame: pd.DataFrame) -> list[dict]:
    if frame is None or frame.empty:
        return []
    return [_clean_record(row) for row in frame.to_dict(orient="records")]


def _clean_record(record: dict) -> dict:
    clean = {}
    for key, value in record.items():
        clean[key] = _clean_value(value)
    return clean


def _clean_value(value):
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if value is pd.NaT or value is pd.NA:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _safe_float(value):
    if value is None or pd.isna(value):
        return None
    return round(float(value), 4)
