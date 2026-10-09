from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import requests

from app import capital_flow as flow
from app.db import Database
from app.providers.quote_fallback import _request_get


def trading_days(count=10):
    days = []
    day = date(2025, 1, 6)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def history(code, days=None, missing=False):
    return [
        {"sector_type": "concept", "sector_code": code, "sector_name": code,
         "trade_date": day, "main_net_inflow": None if missing else (100 if index < 6 else -20)}
        for index, day in enumerate(days or trading_days())
    ]


def network_failure(*args, **kwargs):
    try:
        raise requests.ConnectionError("upstream disconnected")
    except requests.ConnectionError as cause:
        raise RuntimeError("history unavailable") from cause


class FlowReportReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "flow.db"
        self.db = Database(self.path)
        self.cutoff = trading_days()[-1]

    def catalog(self, count):
        boards = [{"sector_code": f"BK{i:04}", "sector_name": f"板块{i}"} for i in range(count)]
        self.db.save_sector_capital_flow_catalog("concept", boards)
        return boards

    def query(self, **kwargs):
        return flow.fetch_sector_capital_flow_report("concept", 100, self.cutoff, self.db, **kwargs)

    def test_ten_stored_dates_with_a_gap_do_not_pass_the_ten_trading_day_screen(self):
        self.catalog(1)
        days = trading_days(11)
        days.pop(3)
        self.cutoff = days[-1]
        self.db.upsert_sector_capital_flow_daily(history('BK0000', days))
        result = self.query(local_only=True)
        self.assertEqual(result['total'], 1)
        self.assertFalse(result['window_complete'])
        self.assertEqual(result['history_coverage']['window_days'], 9)
        self.assertEqual(result['inflow_days_rank'], [])

    def test_complete_daily_cache_needs_no_network_and_survives_restart(self):
        for board in self.catalog(2):
            self.db.upsert_sector_capital_flow_daily(history(board["sector_code"]))
        with patch.object(flow, "fetch_sector_capital_flow") as catalog, patch.object(flow, "_fetch_board_flow_history") as request:
            result = self.query()
        catalog.assert_not_called()
        request.assert_not_called()
        self.assertTrue(result["cached"])
        self.assertFalse(result["partial"])
        self.assertEqual(len(result["inflow_days_rank"]), 2)
        reopened = Database(self.path)
        self.assertEqual(len(reopened.get_sector_capital_flow_report("concept", self.cutoff)["rows"]), 2)

    def test_first_board_failure_does_not_block_other_board(self):
        self.catalog(2)
        def fetch(kind, code, name, cutoff, **kwargs):
            return history(code) if code == "BK0001" else network_failure()
        with patch.object(flow, "_fetch_board_flow_history", side_effect=fetch):
            result = self.query()
        self.assertEqual(result["rows"][0]["sector_code"], "BK0001")
        self.assertEqual(len(result["inflow_days_rank"]), 1)
        self.assertEqual(result["history_coverage"]["failed_boards"], 1)
        self.assertTrue(result["partial"])
        self.assertEqual(len(Database(self.path).get_sector_capital_flow_history("concept", self.cutoff)), 10)

    def test_three_day_history_keeps_daily_table_but_never_claims_no_matches(self):
        self.catalog(2)
        self.db.upsert_sector_capital_flow_daily(history("BK0000", trading_days(3)))
        with patch.object(flow, "_fetch_board_flow_history", side_effect=network_failure):
            result = self.query()
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["history_coverage"]["window_days"], 3)
        self.assertEqual(result["history_coverage"]["complete_boards"], 0)
        self.assertEqual(result["inflow_days_rank"], [])
        self.assertTrue(result["partial"])
        self.assertFalse(result["date_confirmed"])
        self.assertTrue(any("暂不能完整计算" in note for note in result["warnings"]))

    def test_continuous_transport_failures_stop_early_and_retain_old_data(self):
        self.catalog(12)
        self.db.upsert_sector_capital_flow_daily(history("BK0000"))
        self.cutoff = "2025-01-20"
        with patch.object(flow, "_fetch_board_flow_history", side_effect=network_failure) as request:
            result = self.query()
        self.assertEqual(request.call_count, flow._FLOW_FAILURE_LIMIT)
        self.assertEqual(result["report_date"], "2025-01-17")
        self.assertFalse(result["date_confirmed"])
        self.assertGreater(result["history_coverage"]["unattempted_boards"], 0)
        self.assertTrue(any("暂停" in note for note in result["warnings"]))

    def test_only_missing_boards_are_downloaded_on_retry(self):
        self.catalog(2)
        self.db.upsert_sector_capital_flow_daily(history("BK0000"))
        with patch.object(flow, "_fetch_board_flow_history", side_effect=network_failure):
            self.query()
        with patch.object(flow, "_fetch_board_flow_history", side_effect=lambda kind, code, *a, **k: history(code)) as request:
            result = self.query(retry_missing=True)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[1], "BK0001")
        self.assertEqual(result["history_coverage"]["complete_boards"], 2)
        self.assertFalse(result["partial"])

    def test_window_advance_also_updates_previously_complete_boards(self):
        for board in self.catalog(3):
            self.db.upsert_sector_capital_flow_daily(history(board["sector_code"]))
        self.cutoff = "2025-01-20"
        newer_days = trading_days()[1:] + [self.cutoff]
        with patch.object(flow, "_fetch_board_flow_history", side_effect=lambda kind, code, *a, **k: history(code, newer_days)) as request:
            result = self.query()
        self.assertEqual(request.call_count, 3)
        self.assertEqual(result["window_dates"], newer_days)
        self.assertEqual(result["history_coverage"]["complete_boards"], 3)

    def test_new_catalog_failure_uses_saved_catalog(self):
        self.catalog(1)
        self.db.upsert_sector_capital_flow_daily(history("BK0000"))
        with patch.object(flow, "fetch_sector_capital_flow", side_effect=RuntimeError("offline")), patch.object(flow, "_fetch_board_flow_history", side_effect=network_failure):
            result = self.query(refresh=True)
        self.assertEqual(result["total"], 1)
        self.assertTrue(any("本地已保存目录" in note for note in result["warnings"]))

    def test_legacy_daily_rows_restore_without_network_or_saved_snapshot(self):
        self.db.upsert_sector_capital_flow_daily(history("BK0000"))
        with patch.object(flow, "fetch_sector_capital_flow") as catalog, patch.object(flow, "_fetch_board_flow_history") as request:
            result = self.query(local_only=True)
        catalog.assert_not_called()
        request.assert_not_called()
        self.assertEqual(result["total"], 1)

    def test_later_and_other_type_rows_do_not_leak_into_historical_window(self):
        self.catalog(1)
        self.db.upsert_sector_capital_flow_daily(history("BK0000", trading_days(12)))
        self.db.upsert_sector_capital_flow_daily([{**row, "sector_type": "industry"} for row in history("BK9999")])
        result = self.query(local_only=True)
        self.assertEqual(result["report_date"], self.cutoff)
        self.assertEqual(result["window_dates"], trading_days())
        self.assertEqual(result["total"], 1)

    def test_invalid_missing_values_do_not_overwrite_valid_saved_flow(self):
        self.db.upsert_sector_capital_flow_daily(history("BK0000"))
        self.db.upsert_sector_capital_flow_daily(history("BK0000", missing=True))
        rows = self.db.get_sector_capital_flow_history("concept", self.cutoff)
        self.assertEqual(rows[0]["main_net_inflow"], 100)
        self.db.upsert_sector_capital_flow_daily([{**history("BK0000")[0], "main_net_inflow": 0}])
        self.assertEqual(self.db.get_sector_capital_flow_history("concept", self.cutoff)[0]["main_net_inflow"], 0)

    def test_clear_flow_data_preserves_formula_records(self):
        self.catalog(1)
        self.db.upsert_sector_capital_flow_daily(history("BK0000"))
        self.query(local_only=True)
        with self.db.connection() as conn:
            conn.execute("INSERT INTO formula_definitions (name, formula, created_at, updated_at) VALUES ('保留策略','X:C>1;','now','now')")
        self.db.clear_sector_data("concept")
        self.assertEqual(self.db.get_sector_capital_flow_catalog("concept"), [])
        self.assertEqual(self.db.get_sector_capital_flow_history("concept", self.cutoff), [])
        self.assertIsNone(self.db.get_sector_capital_flow_report("concept"))
        with self.db.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM formula_definitions").fetchone()[0], 1)

    def test_report_fallback_never_selects_snapshot_after_requested_date(self):
        for day in ["2025-01-01", "2025-01-20"]:
            self.db.save_sector_capital_flow_report({"sector_type": "concept", "requested_date": day, "rows": []})
        saved = self.db.get_sector_capital_flow_report("concept", max_date="2025-01-17")
        self.assertEqual(saved["requested_date"], "2025-01-01")


class FlowTransportTests(unittest.TestCase):
    def setUp(self):
        flow.HISTORY_POOL.reset()

    def response(self, days=None, status=200):
        response = Mock()
        response.status_code = status
        response.headers = {}
        response.json.return_value = {"data": {"klines": [f"{day},1,2,3,4,5,6,7,8,9,10,11,12" for day in (days or trading_days())]}}
        if status >= 400:
            response.raise_for_status.side_effect = requests.HTTPError("HTTP error", response=response)
        return response

    def test_history_fetches_available_history_then_filters_historical_date(self):
        response = self.response(trading_days(12))
        with patch.object(flow, "_paced_flow_get", return_value=response) as request:
            rows = flow._fetch_board_flow_history("concept", "BK0000", "板块", trading_days()[-1])
        self.assertEqual(len(rows), 10)
        self.assertEqual(request.call_args.kwargs["params"]["lmt"], "0")
        self.assertNotIn("end", request.call_args.kwargs["params"])
        response.close.assert_called_once()

    def test_transient_disconnect_retries_and_closes_successful_response(self):
        response = self.response()
        with patch.object(flow, "_paced_flow_get", side_effect=[requests.ConnectionError("reset"), response]) as request, patch.object(flow.time, "sleep"):
            rows = flow._fetch_board_flow_history("concept", "BK0000", "板块", trading_days()[-1])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(len(rows), 10)
        response.close.assert_called_once()

    def test_rate_limit_respects_retry_after(self):
        limited = self.response(status=429)
        limited.headers = {"Retry-After": "4"}
        with patch.object(flow, "_paced_flow_get", side_effect=[limited, self.response()]) as request:
            with self.assertRaises(flow.FlowHistoryUnavailable) as error:
                flow._fetch_board_flow_history("concept", "BK0000", "板块", trading_days()[-1])
        self.assertEqual(request.call_count, 1)
        self.assertGreaterEqual(error.exception.retry_after, 4)
        self.assertEqual(flow.HISTORY_POOL.available(set()), [])
        limited.close.assert_called_once()

    def test_permanent_http_error_is_not_retried(self):
        with patch.object(flow, "_paced_flow_get", return_value=self.response(status=404)) as request:
            with self.assertRaises(RuntimeError):
                flow._fetch_board_flow_history("concept", "BK0000", "板块", trading_days()[-1])
        self.assertEqual(request.call_count, 1)

    def test_history_with_only_missing_flow_is_not_treated_as_success(self):
        response = self.response()
        response.json.return_value = {"data": {"klines": [
            f"{day},-,2,3,4,5,6,7,8,9,10,11,12" for day in trading_days()
        ]}}
        with patch.object(flow, "_paced_flow_get", return_value=response), patch.object(flow.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "有效主力净流入"):
                flow._fetch_board_flow_history("concept", "BK0000", "板块", trading_days()[-1])

    def test_expired_request_budget_prevents_network_call(self):
        with patch("app.providers.quote_fallback.requests.Session") as session:
            with self.assertRaises(requests.Timeout):
                _request_get("https://example.invalid", _deadline=time.monotonic() - 1)
        session.return_value.get.assert_not_called()

    def test_shared_transport_limits_concurrency_across_queries(self):
        lock = threading.Lock()
        active = 0
        peak = 0
        def get(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(active, peak)
            time.sleep(0.01)
            with lock:
                active -= 1
            return self.response()
        with patch.object(flow, "_request_get", side_effect=get), patch.object(flow, "_FLOW_REQUEST_INTERVAL", 0), patch.object(flow, "_FLOW_NEXT_REQUEST_AT", 0):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: flow._paced_flow_get("https://example.invalid", deadline=time.monotonic() + 3), range(8)))
        self.assertEqual(peak, 2)


if __name__ == "__main__":
    unittest.main()
