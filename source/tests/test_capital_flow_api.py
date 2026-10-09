from __future__ import annotations

import importlib
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

from app import capital_flow as flow
from app.db import Database
from test_capital_flow_reliability import history, network_failure, trading_days


class LocalAsgiClient:
    """Exercise the actual ASGI routes without extra HTTP test dependencies."""

    def __init__(self, app):
        self.app = app

    def get(self, path, params=None, method="GET", body=None):
        async def request():
            messages = []
            async def receive():
                return {"type": "http.request", "body": json.dumps(body).encode() if body is not None else b"", "more_body": False}
            async def send(message):
                messages.append(message)
            await self.app({
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                "root_path": "", "query_string": urlencode(params or {}).encode(), "headers": [(b"content-type",b"application/json")] if body is not None else [],
                "client": ("127.0.0.1", 12345), "server": ("localhost", 80),
            }, receive, send)
            status = next(m["status"] for m in messages if m["type"] == "http.response.start")
            response_body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            class Response:
                status_code = status
                def json(self):
                    return json.loads(response_body)
            return Response()
        return asyncio.run(request())


class CapitalFlowApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # main normally opens the user's database at import time. Redirect that
        # initialization before importing it; never touch the real database.
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "bootstrap.db")
            with patch("app.db.Database", return_value=database), patch("app.config.ensure_directories"):
                cls.main = importlib.import_module("main")

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "api.db"
        self.db = Database(self.path)
        patcher = patch.object(self.main, "database", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        # No lifespan context: startup automations must not run in these tests.
        self.client = LocalAsgiClient(self.main.app)
        self.cutoff = trading_days()[-1]

    def current_fixture(self, time='2026-09-30T07:39:30+00:00'):
        return {'ok': True, 'sector_type': 'concept', 'total': 2, 'source': 'current',
                'updated_at': time, 'rows': [
                    {'sector_code': f'BK000{i}', 'sector_name': f'板块{i}', 'rank': i + 1,
                     'main_net_inflow': 1000 - i, 'source_time': time} for i in range(2)]}

    def test_current_refresh_immediately_shares_single_day_without_history(self):
        self.db.save_sector_capital_flow_report({'sector_type': 'concept', 'requested_date': '2026-10-06',
                                                'report_date': '2026-09-24', 'rows': [], 'total': 0})
        with patch.object(self.main, 'fetch_sector_capital_flow', return_value=self.current_fixture()), patch.object(flow, '_fetch_board_flow_history', side_effect=network_failure) as history_request:
            current = self.client.get('/api/sector-capital-flow/current', {'limit': 1})
            daily = self.client.get('/api/sector-capital-flow', {'date': '2026-10-06', 'limit': 1000})
            self.assertEqual(current.json()['closed_rows_saved'], 2)
            self.assertEqual(current.json()['confirmed_trade_date'], '2026-09-30')
            self.assertEqual(daily.status_code, 200)
            data = daily.json()
            self.assertEqual(data['total'], 2)
            self.assertEqual(data['requested_date'], '2026-10-06')
            self.assertEqual(data['resolved_date'], '2026-09-30')
            self.assertEqual(data['report_date'], '2026-09-30')
            self.assertTrue(data['daily_complete'])
            self.assertTrue(data['date_confirmed'])
            self.assertFalse(data['window_complete'])
            self.assertEqual(data['inflow_days_rank'], [])
            self.assertEqual(data['history_coverage']['window_days'], 1)
            history_request.assert_not_called()
        self.main.database = Database(self.path)
        restored = self.client.get('/api/sector-capital-flow/cache', {'date': '2026-10-06'})
        self.assertEqual(restored.json()['total'], 2)

    def test_lower_online_refresh_reuses_current_close_and_does_not_wait_for_history(self):
        with patch.object(flow, 'fetch_sector_capital_flow', return_value=self.current_fixture()), patch.object(flow, '_fetch_board_flow_history', side_effect=network_failure) as history_request:
            result = self.client.get('/api/sector-capital-flow', {'date': '2026-10-06', 'refresh': 'true'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['total'], 2)
        self.assertFalse(result.json()['cached'])
        self.assertEqual(result.json()['refresh_error'], '')
        history_request.assert_not_called()

    def test_pre_upgrade_current_cache_bridges_without_network_and_is_idempotent(self):
        self.db.save_current_capital_flow(self.current_fixture())
        with patch.object(flow, 'fetch_sector_capital_flow') as network, patch.object(flow, '_fetch_board_flow_history') as history_request:
            result = self.client.get('/api/sector-capital-flow/cache', {'date': '2026-10-06'})
            self.assertEqual(result.json()['total'], 2)
            before = self.db.get_sector_capital_flow_history('concept', '2026-09-30')
            self.client.get('/api/sector-capital-flow/current/cache')
            after = self.db.get_sector_capital_flow_history('concept', '2026-09-30')
            self.assertEqual(before, after)
            self.assertEqual(self.client.get('/api/sector-capital-flow/cache', {'date': '2026-09-29'}).status_code, 404)
            network.assert_not_called()
            history_request.assert_not_called()

    def test_intraday_or_missing_source_time_does_not_overwrite_closing_history(self):
        self.db.save_current_capital_flow(self.current_fixture())
        self.client.get('/api/sector-capital-flow/current/cache')
        with patch.object(self.main, 'fetch_sector_capital_flow', return_value=self.current_fixture('2026-09-30T04:00:00+00:00')):
            intraday = self.client.get('/api/sector-capital-flow/current')
        self.assertEqual(intraday.json()['closed_rows_saved'], 0)
        self.assertEqual(self.db.get_sector_capital_flow_history('concept', '2026-09-30')[0]['main_net_inflow'], 1000)
        self.db.clear_capital_flow_data()
        self.db.save_current_capital_flow(self.current_fixture(None))
        self.assertEqual(self.client.get('/api/sector-capital-flow/cache', {'date': '2026-10-06'}).status_code, 404)

    def test_clear_only_flow_with_backup_and_no_network(self):
        self.db.upsert_sector_capital_flow_daily(history("BK0000"))
        for kind in ("concept", "industry"):
            self.db.save_current_capital_flow({"sector_type": kind, "rows": [{"sector_code": "BK0000"}]})
            self.db.save_sector_capital_flow_catalog(kind, [{"sector_code": "BK0000", "sector_name": "缓存"}])
            self.db.save_sector_capital_flow_report({"sector_type": kind, "requested_date": self.cutoff})
        with self.db.connection() as conn:
            conn.execute("INSERT INTO sector_daily (trade_date, sector_type, sector_code, sector_name, fetched_at, dataset_id) VALUES ('2025-01-01','concept','BK0000','保留','old','tdx_standard')")
            conn.execute("INSERT INTO formula_definitions (name, formula, created_at, updated_at) VALUES ('保留公式','X:C>1;','old','old')")
            conn.execute("INSERT INTO formula_runs (formula_name,formula,timeframe,provider,data_source,warnings_json,result_json,created_at) VALUES ('保留公式','X:C>1;','weekly','tdx','tdx','[]','{\"rows\":[{\"code\":\"000001\"}]}','old')")
        env = Path(self.folder.name) / '.env'
        env.write_text('test-key-placeholder', encoding='utf-8')
        with patch.object(self.main, "fetch_sector_capital_flow") as live, patch.object(flow, "fetch_sector_capital_flow") as historical, patch.object(flow, "_fetch_board_flow_history") as history_download:
            response = self.client.get('/api/sector-capital-flow/clear', method='POST')
            self.assertEqual(response.status_code, 200)
            backup = json.loads(Path(response.json()['backup_path']).read_text(encoding='utf-8'))
            self.assertEqual(len(backup['sector_capital_flow_current']), 2)
            for table in backup:
                with self.db.connection() as conn:
                    self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
            self.main.database = Database(self.path)
            self.assertEqual(self.client.get('/api/sector-capital-flow/current/cache').status_code, 404)
            self.assertEqual(self.client.get('/api/sector-capital-flow/cache').status_code, 404)
            live.assert_not_called()
            historical.assert_not_called()
            history_download.assert_not_called()
        with self.db.connection() as conn:
            for table in ('sector_daily', 'formula_definitions', 'formula_runs'):
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 1)
        self.assertEqual(env.read_text(encoding='utf-8'), 'test-key-placeholder')
        empty = self.client.get('/api/sector-capital-flow/clear', method='POST')
        self.assertEqual(empty.json()['deleted_rows'], 0)

    def test_clear_refuses_inflight_query_and_backup_failure_preserves_cache(self):
        self.db.save_current_capital_flow({'sector_type': 'concept', 'rows': []})
        with patch.object(self.main, '_flow_active_operations', 1):
            self.assertEqual(self.client.get('/api/sector-capital-flow/clear', method='POST').status_code, 409)
        with patch('app.db.json.dump', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.db.clear_capital_flow_data()
        self.assertIsNotNone(self.db.get_current_capital_flow('concept'))

    def test_current_is_independent_and_persists_full_snapshot(self):
        current = {"ok": True, "sector_type": "concept", "total": 2, "rows": [
            {"sector_code": "BK0000", "sector_name": "甲"},
            {"sector_code": "BK0001", "sector_name": "乙"},
        ]}
        with patch.object(self.main, "fetch_sector_capital_flow", return_value=current) as live, patch.object(self.main, "fetch_sector_capital_flow_report") as historical:
            response = self.client.get("/api/sector-capital-flow/current", {"limit": 1})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()["rows"]), 1)
            live.assert_called_once_with("concept", 1000)
            historical.assert_not_called()
        self.main.database = Database(self.path)
        with patch.object(self.main, "fetch_sector_capital_flow") as live:
            restored = self.client.get("/api/sector-capital-flow/current/cache", {"limit": 1000})
            self.assertEqual(len(restored.json()["rows"]), 2)
            live.assert_not_called()
        self.assertIsNone(self.db.get_sector_capital_flow_report("concept"))
        with patch.object(self.main, "fetch_sector_capital_flow", side_effect=RuntimeError("offline")):
            fallback = self.client.get("/api/sector-capital-flow/current")
            self.assertTrue(fallback.json()["cached"])
            self.assertEqual(fallback.json()["refresh_error"], "offline")
        self.db.clear_sector_data("concept")
        self.assertIsNone(self.db.get_current_capital_flow("concept"))

    def test_restart_restore_can_build_report_from_legacy_daily_rows_without_network(self):
        self.db.upsert_sector_capital_flow_daily(history("BK0000"))
        with patch.object(flow, "fetch_sector_capital_flow") as catalog, patch.object(flow, "_fetch_board_flow_history") as download:
            response = self.client.get("/api/sector-capital-flow/cache", params={"sector_type": "concept", "date": self.cutoff})
            self.main.database = Database(self.path)
            restored = self.client.get("/api/sector-capital-flow/cache", params={"sector_type": "concept", "date": self.cutoff})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(restored.json()["total"], 1)
        self.assertTrue(restored.json()["cached"])
        catalog.assert_not_called()
        download.assert_not_called()

    def test_retry_endpoint_downloads_only_missing_board_and_normal_query_reuses_snapshot(self):
        self.db.save_sector_capital_flow_catalog("concept", [
            {"sector_code": "BK0000", "sector_name": "已有板块"},
            {"sector_code": "BK0001", "sector_name": "缺失板块"},
        ])
        self.db.upsert_sector_capital_flow_daily(history("BK0000"))
        with patch.object(flow, "_fetch_board_flow_history", side_effect=lambda kind, code, *a, **k: history(code)) as download:
            response = self.client.get("/api/sector-capital-flow", params={"sector_type": "concept", "date": self.cutoff, "retry_missing": "true"})
            restored = self.client.get("/api/sector-capital-flow", params={"sector_type": "concept", "date": self.cutoff})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["history_coverage"]["complete_boards"], 2)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(download.call_args.args[1], "BK0001")
        self.assertTrue(restored.json()["cached"])

    def test_offline_snapshot_fallback_shows_both_selected_and_actual_dates(self):
        self.db.save_sector_capital_flow_report({
            "sector_type": "concept", "requested_date": "2025-01-16", "report_date": "2025-01-16",
            "rows": [{"sector_code": "BK0000"}], "total": 1,
        })
        with patch.object(flow, "fetch_sector_capital_flow", side_effect=network_failure):
            response = self.client.get("/api/sector-capital-flow", params={"sector_type": "concept", "date": self.cutoff})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["requested_date"], self.cutoff)
        self.assertEqual(result["report_date"], "2025-01-16")
        self.assertFalse(result["date_confirmed"])
        self.assertTrue(result["cached"])
        self.assertTrue(result["partial"])
        self.assertTrue(result["refresh_error"])

    def test_invalid_date_is_a_validation_error_not_a_fake_cached_success(self):
        response = self.client.get("/api/sector-capital-flow", params={"date": "invalid"})
        self.assertEqual(response.status_code, 400)

    def test_local_cache_miss_never_contacts_provider(self):
        with patch.object(flow, "fetch_sector_capital_flow") as catalog, patch.object(flow, "_fetch_board_flow_history") as download:
            response = self.client.get("/api/sector-capital-flow/cache", params={"sector_type": "concept", "date": self.cutoff})
        self.assertEqual(response.status_code, 404)
        catalog.assert_not_called()
        download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
