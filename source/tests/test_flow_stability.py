from datetime import datetime,timezone,timedelta
from email.utils import format_datetime
from pathlib import Path
from threading import Event
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

import requests
from app import capital_flow as flow
from app import flow_tasks as tasks
from app.db import Database
from app.flow_transport import HistoryRoutePool, FlowHistoryUnavailable, retry_after_seconds
from app.providers import quote_fallback as quotes


COMPLETE = {"history_coverage":{"complete_boards":1,"requested_boards":1},"date_confirmed":True,"catalog_complete":True}
PARTIAL = {"history_coverage":{"complete_boards":0,"requested_boards":1},"date_confirmed":False,"catalog_complete":True,
           "retry_recommended":True,"retry_after_seconds":0,"stopped_reason":"network unavailable"}


class StabilityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = Database(Path(self.folder.name)/"flow.db")
        flow.HISTORY_POOL.reset()
        self.manager = tasks.FlowHistoryJobs()
        self.addCleanup(self.manager.shutdown)

    def await_finished(self,job):
        for _ in range(400):
            state = self.manager.snapshot(job["id"])
            if state["status"] != "running":
                return state
            time.sleep(.005)
        self.fail("task did not finish")

    def seed(self,**changes):
        state = {"id":"resume-test","status":"running","sector_type":"concept","date":"2026-09-30",
                 "round":1,"max_rounds":3,"attempted":0,"downloaded":0,"failed":0,"total":1,
                 "created_at":time.time()-10,"expires_at":time.time()+1800,"next_retry_at":0,**changes}
        self.db.claim_flow_job(state,"old-process")
        with self.db.connection() as conn:
            conn.execute("UPDATE capital_flow_jobs SET lease_until=0")
        return state

    def test_failover_validates_code_and_remembers_successful_channel(self):
        bad = Mock(status_code=200,headers={})
        bad.json.return_value = {"data":{"code":"BKwrong","klines":["2026-09-30,1,2,3,4,5,6,7,8,9,10,11,12"]}}
        good = Mock(status_code=200,headers={})
        good.text = 'gupiaoFlow({"data":{"code":"BK1","klines":["2026-09-30,1,2,3,4,5,6,7,8,9,10,11,12"]}});'
        with patch.object(flow,"_paced_flow_get",side_effect=[bad,good,good]) as get,patch.object(flow.time,"sleep"):
            rows = flow._fetch_board_flow_history("concept","BK1","board","2026-09-30")
            flow._fetch_board_flow_history("concept","BK1","board","2026-09-30")
        self.assertEqual(rows[0]["sector_code"],"BK1")
        self.assertEqual(get.call_args_list[1].kwargs["params"]["cb"],"gupiaoFlow")
        self.assertEqual(get.call_args_list[2].kwargs["params"]["cb"],"gupiaoFlow")

    def test_retry_after_is_not_capped_and_no_other_channel_evades_it(self):
        response = Mock(status_code=429,headers={"Retry-After":"300"})
        response.raise_for_status.side_effect = requests.HTTPError("restricted",response=response)
        with patch.object(flow,"_paced_flow_get",return_value=response) as get:
            with self.assertRaises(FlowHistoryUnavailable) as error:
                flow._fetch_board_flow_history("concept","BK1","board","2026-09-30")
            with self.assertRaises(FlowHistoryUnavailable):
                flow._fetch_board_flow_history("concept","BK2","board","2026-09-30")
        self.assertEqual(get.call_count,1)
        self.assertGreater(error.exception.retry_after,290)

    def test_retry_after_http_date_and_nonfinite_values(self):
        stamp = format_datetime(datetime.now(timezone.utc)+timedelta(seconds=120))
        self.assertGreater(retry_after_seconds(stamp),118)
        for value in (None,"invalid","nan","inf","-10"):
            self.assertEqual(retry_after_seconds(value),0)

    def test_bad_channel_cools_and_recovers_without_bypassing_global_limit(self):
        pool = HistoryRoutePool()
        with patch("app.flow_transport.time.monotonic",return_value=100):
            pool.failure("eastmoney_https_json",ConnectionError())
            pool.failure("eastmoney_https_json",ConnectionError())
            self.assertNotIn("eastmoney_https_json",[r[0] for r in pool.available(set())])
        with patch("app.flow_transport.time.monotonic",return_value=161):
            self.assertIn("eastmoney_https_json",[r[0] for r in pool.available(set())])

    def test_requests_reuse_only_same_thread_sessions_and_reset_after_disconnect(self):
        quotes.close_reused_sessions()
        self.addCleanup(quotes.close_reused_sessions)
        first,second = Mock(),Mock()
        first.get.side_effect = [Mock(status_code=200),requests.ConnectionError("reset")]
        second.get.return_value = Mock(status_code=200)
        with patch.object(quotes.requests,"Session",side_effect=[first,second]) as factory,patch.object(quotes.requests.utils,"getproxies",return_value={}),patch.dict(quotes.os.environ,{"MARKET_HTTP_MODE":"direct"}):
            quotes._request_get("https://example.invalid",_reuse_session=True)
            with self.assertRaises(requests.ConnectionError):
                quotes._request_get("https://example.invalid",_reuse_session=True)
            quotes._request_get("https://example.invalid",_reuse_session=True)
        self.assertEqual(factory.call_count,2)
        first.close.assert_called_once()

    def test_partial_round_automatically_continues_without_second_user_action(self):
        rounds = []
        def fetch(*args,**kwargs):
            if kwargs.get("local_only"):
                raise RuntimeError("empty")
            rounds.append(kwargs)
            return PARTIAL if len(rounds)==1 else COMPLETE
        # Advance scheduled wait to zero only in this deterministic fault test.
        with patch.object(tasks,"fetch_sector_capital_flow",side_effect=RuntimeError("offline")),patch.object(tasks,"fetch_sector_capital_flow_report",side_effect=fetch),patch.object(tasks,"max",side_effect=lambda *values: 0 if values in ((30,0),(90,0)) else max(*values),create=True):
            job = self.manager.start(self.db,"concept","2026-09-30",lambda fn:fn)
            done = self.await_finished(job)
        self.assertEqual(done["status"],"complete")
        self.assertEqual(len(rounds),2)
        self.assertEqual(done["round"],2)
        self.assertTrue(all(round["retry_missing"] for round in rounds))

    def test_round_progress_accumulates_attempts_successes_and_failures(self):
        rounds = []
        def fetch(*args,**kwargs):
            if kwargs.get("local_only"):
                raise RuntimeError("empty")
            rounds.append(kwargs)
            index = len(rounds)
            kwargs["progress"]({"attempted":2,"downloaded":0 if index == 1 else 1,
                                "failed":2 if index == 1 else 1,"total":519})
            return PARTIAL if index == 1 else COMPLETE
        with patch.object(tasks,"fetch_sector_capital_flow",side_effect=RuntimeError("offline")),patch.object(tasks,"fetch_sector_capital_flow_report",side_effect=fetch),patch.object(tasks,"max",side_effect=lambda *values: 0 if values in ((30,0),(90,0)) else max(*values),create=True):
            job = self.manager.start(self.db,"concept","2026-09-30",lambda fn:fn)
            done = self.await_finished(job)
        self.assertEqual(done["status"],"complete")
        self.assertEqual(done["round"],2)
        self.assertEqual(done["attempted"],2)  # current-round progress remains available
        self.assertEqual(done["attempted_total"],4)
        self.assertEqual(done["downloaded_total"],1)
        self.assertEqual(done["failed_total"],3)

    def test_retries_are_bounded_to_three_rounds(self):
        calls = []
        def fetch(*args,**kwargs):
            if kwargs.get("local_only"):
                raise RuntimeError("empty")
            calls.append(1)
            return PARTIAL
        with patch.object(tasks,"fetch_sector_capital_flow",side_effect=RuntimeError("offline")),patch.object(tasks,"fetch_sector_capital_flow_report",side_effect=fetch),patch.object(tasks,"max",side_effect=lambda *values: 0 if values in ((30,0),(90,0)) else max(*values),create=True):
            job = self.manager.start(self.db,"concept","2026-09-30",lambda fn:fn)
            done = self.await_finished(job)
        self.assertEqual(len(calls),3)
        self.assertEqual(done["status"],"partial")
        self.assertEqual(self.db.pending_flow_jobs(),[])

    def test_expired_process_lease_resumes_saved_round_with_same_id(self):
        self.seed()
        with patch.object(tasks,"fetch_sector_capital_flow",side_effect=RuntimeError("offline")),patch.object(tasks,"fetch_sector_capital_flow_report",return_value=COMPLETE):
            resumed = self.manager.resume_pending(Database(self.db.path),lambda fn:fn)
            done = self.await_finished(resumed[0])
        self.assertEqual(done["id"],"resume-test")
        self.assertTrue(done["resumed"])
        self.assertEqual(done["round"],2)
        self.assertEqual(Database(self.db.path).get_flow_job(done["id"])["status"],"complete")

    def test_expired_task_never_restarts_network_and_cancelled_task_never_resumes(self):
        self.seed(expires_at=time.time()-1)
        with patch.object(tasks,"fetch_sector_capital_flow") as net:
            pending = self.manager.resume_pending(self.db,lambda fn:fn)
            done = self.await_finished(pending[0])
        net.assert_not_called()
        self.assertEqual(done["status"],"partial")
        self.assertEqual(self.manager.resume_pending(self.db,lambda fn:fn),[])

    def test_other_window_follows_existing_lease_and_cannot_clear_active_job(self):
        state = self.seed(round=0)
        with self.db.connection() as conn:
            conn.execute("UPDATE capital_flow_jobs SET lease_until=?",(time.time()+120,))
        with patch.object(tasks,"fetch_sector_capital_flow") as net:
            job = self.manager.start(self.db,"concept","2026-09-30",lambda fn:fn)
        self.assertEqual(job["id"],state["id"])
        net.assert_not_called()
        with self.assertRaises(RuntimeError):
            self.db.clear_capital_flow_data()
        self.manager.cancel(job["id"],self.db)
        self.assertTrue(Database(self.db.path).get_flow_job(job["id"])["cancel_requested"])

    def test_explicit_click_after_expired_task_starts_fresh_without_second_click(self):
        self.seed(expires_at=time.time()-1)
        with patch.object(tasks,"fetch_sector_capital_flow",side_effect=RuntimeError("offline")),patch.object(tasks,"fetch_sector_capital_flow_report",return_value=COMPLETE):
            job = self.manager.start(self.db,"concept","2026-09-30",lambda fn:fn)
            done = self.await_finished(job)
        self.assertNotEqual(done["id"],"resume-test")
        self.assertEqual(done["status"],"complete")
        self.assertEqual(done["round"],1)
        self.assertEqual(self.db.get_flow_job("resume-test")["status"],"partial")

    def test_stale_heartbeat_cannot_revert_complete_job_to_running(self):
        state = self.seed()
        self.db.save_flow_job({**state,"status":"complete"},"old-process")
        self.assertFalse(self.db.save_flow_job(state,"old-process"))
        self.assertEqual(self.db.get_flow_job(state["id"])["status"],"complete")

    def test_cancel_during_cooldown_finishes_without_waiting_full_delay(self):
        def fetch(*args,**kwargs):
            if kwargs.get("local_only"):
                raise RuntimeError("empty")
            return PARTIAL
        with patch.object(tasks,"fetch_sector_capital_flow",side_effect=RuntimeError("offline")),patch.object(tasks,"fetch_sector_capital_flow_report",side_effect=fetch):
            job = self.manager.start(self.db,"concept","2026-09-30",lambda fn:fn)
            for _ in range(200):
                if self.manager.snapshot(job["id"]).get("stage")=="cooldown": break
                time.sleep(.005)
            self.manager.cancel(job["id"])
            done = self.await_finished(job)
        self.assertEqual(done["status"],"cancelled")
        self.assertEqual(self.db.pending_flow_jobs(),[])

    def test_shutdown_preserves_cooldown_and_only_user_started_task_is_pending(self):
        def fetch(*args,**kwargs):
            if kwargs.get("local_only"):
                raise RuntimeError("empty")
            return {**PARTIAL,"retry_after_seconds":300}
        with patch.object(tasks,"fetch_sector_capital_flow",side_effect=RuntimeError("offline")),patch.object(tasks,"fetch_sector_capital_flow_report",side_effect=fetch):
            job = self.manager.start(self.db,"concept","2026-09-30",lambda fn:fn)
            for _ in range(200):
                state = self.manager.snapshot(job["id"])
                if state.get("stage")=="cooldown": break
                time.sleep(.005)
            retry_at = state["next_retry_at"]
            self.manager.shutdown()
        saved = Database(self.db.path).get_flow_job(job["id"])
        self.assertEqual(saved["status"],"paused")
        self.assertGreaterEqual(saved["next_retry_at"],retry_at)
        self.assertNotIn("result",saved)
        self.assertEqual(len(self.db.pending_flow_jobs()),1)

    def test_no_history_network_failure_can_retry_then_succeed(self):
        calls = []
        def fetch(*args,**kwargs):
            if kwargs.get("local_only"):
                raise RuntimeError("empty")
            calls.append(1)
            if len(calls)==1:
                raise FlowHistoryUnavailable("no history yet",retryable=True)
            return COMPLETE
        with patch.object(tasks,"fetch_sector_capital_flow",side_effect=RuntimeError("offline")),patch.object(tasks,"fetch_sector_capital_flow_report",side_effect=fetch),patch.object(tasks,"max",side_effect=lambda *values: 0 if values in ((30,0),(90,0)) else max(*values),create=True):
            job = self.manager.start(self.db,"concept","2026-09-30",lambda fn:fn)
            done = self.await_finished(job)
        self.assertEqual(done["status"],"complete")
        self.assertEqual(len(calls),2)


if __name__ == '__main__':
    unittest.main()
