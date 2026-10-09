from datetime import date, datetime
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.analytics import build_report
from app.db import Database
from app.flow_calendar import trading_window
from app.flow_summary import attach_five_day_flow


class FiveDaySummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name)/"summary.db")
        self.days = trading_window(date(2026,9,30),5)
        self.db.save_sector_capital_flow_catalog("concept",[
            {"sector_code":"BK0001","sector_name":"人形机器人"},
            {"sector_code":"BK0002","sector_name":"工业软件"},
            {"sector_code":"BK0003","sector_name":"重复名称"},
            {"sector_code":"BK0004","sector_name":"重复名称"}])
        self.db.upsert_sector_capital_flow_daily([
            {"sector_type":"concept","sector_code":code,"sector_name":name,"trade_date":day,"main_net_inflow":value}
            for code,name,values in (("BK0001","人形机器人",[100,-50,0,30,-10]),("BK0002","工业软件",[-10]*5))
            for day,value in zip(self.days,values)])

    def summarize(self,rows,day="2026-09-30",kind="concept"):
        module = {"rows":rows}
        attach_five_day_flow(self.db,kind,day,module)
        return module

    def test_exact_code_and_unique_name_match_same_sum(self):
        result = self.summarize([{"sector_code":"BK0001","sector_name":"人形机器人"},
            {"sector_code":"880729","sector_name":"人形机器人"}, {"sector_code":"880123","sector_name":"工业软件"}])
        self.assertEqual([r["five_day_main_net_inflow"] for r in result["rows"]],[70,70,-50])
        self.assertEqual(result["rows"][0]["five_day_flow_match"],"code")
        self.assertEqual(result["rows"][1]["five_day_flow_match"],"unique_name")
        self.assertEqual(result["five_day_flow"]["window_dates"],self.days)
        self.assertEqual(result["five_day_flow"]["complete_boards"],3)

    def test_ambiguous_or_fuzzy_names_are_not_matched(self):
        rows = [{"sector_code":"880001","sector_name":name} for name in ("重复名称","机器人","工业软件概念")]
        result = self.summarize(rows)
        self.assertTrue(all(row["five_day_flow_status"]=="unmatched" for row in result["rows"]))
        self.assertTrue(all(row["five_day_main_net_inflow"] is None for row in rows))

    def test_missing_middle_day_not_partial_total_or_older_date_substitution(self):
        with self.db.connection() as conn:
            conn.execute("DELETE FROM sector_capital_flow_daily WHERE sector_code='BK0001' AND trade_date=?",(self.days[2],))
        self.db.upsert_sector_capital_flow_daily([{"sector_type":"concept","sector_code":"BK0001","sector_name":"人形机器人","trade_date":"2026-09-21","main_net_inflow":999}])
        result = self.summarize([{"sector_code":"BK0001"}])
        self.assertEqual(result["rows"][0]["five_day_flow_available_days"],4)
        self.assertIsNone(result["rows"][0]["five_day_main_net_inflow"])

    def test_zero_total_is_a_valid_complete_value(self):
        self.db.upsert_sector_capital_flow_daily([{"sector_type":"concept","sector_code":"BK0001","sector_name":"人形机器人","trade_date":day,"main_net_inflow":0} for day in self.days])
        row = self.summarize([{"sector_code":"BK0001"}])["rows"][0]
        self.assertEqual(row["five_day_main_net_inflow"],0)
        self.assertEqual(row["five_day_flow_status"],"complete")

    def test_report_date_and_sector_type_are_not_substituted(self):
        row = self.summarize([{"sector_code":"BK0001"}],"2026-09-29")["rows"][0]
        self.assertIsNone(row["five_day_main_net_inflow"])
        row = self.summarize([{"sector_code":"BK0001"}],kind="industry")["rows"][0]
        self.assertEqual(row["five_day_flow_status"],"unmatched")

    def test_intraday_is_not_replaced_with_yesterdays_close(self):
        with patch("app.flow_summary.shanghai_now",return_value=datetime.fromisoformat("2026-09-30T14:00:00+08:00")):
            result = self.summarize([{"sector_code":"BK0001"}])
        self.assertEqual(result["rows"][0]["five_day_flow_status"],"not_closed")
        self.assertEqual(result["five_day_flow"]["window_dates"],[])

    def test_analytics_report_has_real_supplement_and_keeps_rank_rule(self):
        for dataset,code in (("tdx_standard","880729"),("akshare","BK0001")):
            self.db.upsert_sector_daily([
                {"dataset_id":dataset,"data_source":"snapshot","trade_date":day,"sector_type":"concept",
                 "sector_code":code,"sector_name":"人形机器人","close":100+index,"pct_change":1,
                 "source_rank":100-index*5,"source_rps50":50+index,"amount":1000,"volume":10}
                for index,day in enumerate(trading_window(date(2026,9,30),10))])
            report = build_report(self.db,"concept",report_date="2026-09-30",dataset_id=dataset)
            module = report["modules"]["ten_day_six_up"]
            self.assertEqual(len(module["rows"]),1)
            self.assertEqual(module["rows"][0]["up_days_10d"],9)
            self.assertEqual(module["rows"][0]["five_day_main_net_inflow"],70)
            self.assertIs(module,report["module7_ten_day_six_up"])
            self.assertEqual(module["min_advance_days"],6)
            # Exercise the production HTTP boundary without running startup jobs
            # or opening the user's database.
            with patch("app.db.Database",return_value=self.db),patch("app.config.ensure_directories"):
                app_main = importlib.import_module("main")
            from test_capital_flow_api import LocalAsgiClient
            with patch.object(app_main,"database",self.db):
                response = LocalAsgiClient(app_main.app).get("/api/report",{"sector_type":"concept","date":"2026-09-30","provider":dataset})
            self.assertEqual(response.status_code,200)
            api_rows = response.json()["modules"]["ten_day_six_up"]["rows"]
            self.assertEqual(api_rows[0]["five_day_main_net_inflow"],70)
        restored = self.summarize([{"sector_code":"BK0001"}])
        self.assertEqual(restored["five_day_flow"]["complete_boards"],1)


if __name__ == "__main__": unittest.main()
