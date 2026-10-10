from datetime import datetime, date
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app.db import Database
from app.candidate_history import evaluate_history, list_history, next_days
from app.flow_calendar import SHANGHAI
from test_capital_flow_api import LocalAsgiClient

class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.db=Database(Path(self.folder.name)/'history.db')
        self.stamp=datetime(2026,9,30,16,tzinfo=SHANGHAI)
        self.result={'report_date':'2026-09-30','provider':'akshare','sector_type':'concept','saved_at':self.stamp.isoformat(),
                     'rows':[{'code':code,'name':name,'score':80,'ai':{'status':'disabled'}} for code,name in [('600001','上涨样本'),('600002','下跌样本'),('600003','缺失样本')]]}
        self.save()
        self.days=next_days(date(2026,9,30),5)
        self.bars={code:[{'date':day,'open':100,'close':100+direction*(i+1),'volume':100,'amount':1000} for i,day in enumerate(self.days)] for code,direction in [('600001',1),('600002',-1)]}
        for target,kwargs in [('app.candidate_history.shanghai_now',{'return_value':datetime(2026,10,30,16,tzinfo=SHANGHAI)}),
                              ('app.candidate_history.fs._discover_day_files',{'return_value':[(c,Path(c)) for c in self.bars]}),
                              ('app.candidate_history.fs._read_daily',{'side_effect':lambda path:self.bars[str(path)]})]:
            p=patch(target,**kwargs);p.start();self.addCleanup(p.stop)

    def save(self, id='run', provider='akshare', created=None):
        state={'id':id,'status':'completed','result':self.result}
        stamp=(created or self.stamp).timestamp()
        with self.db.connection() as conn:
            conn.execute('INSERT OR REPLACE INTO candidate_runs(id,request_key,provider,sector_type,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                         (id,id,provider,'concept',json.dumps(state,ensure_ascii=False),stamp,stamp))

    def test_fixed_trade_window_denominator_and_drawdown(self):
        data=evaluate_history(self.db,'run',5)
        self.assertEqual(data['entry_date'],'2026-10-08')
        self.assertEqual(data['exit_date'],'2026-10-14')
        s=data['summary'];self.assertEqual(s['complete'],2);self.assertEqual(s['unavailable'],1)
        self.assertEqual(s['up_ratio_pct'],50)
        self.assertAlmostEqual(s['average_return_pct'],0)
        self.assertAlmostEqual(data['rows'][1]['drawdown_pct'],-5)

    def test_no_reselection_and_no_history_mutation(self):
        with self.db.connection() as conn: before=conn.execute('SELECT state_json FROM candidate_runs').fetchone()[0]
        with patch('app.candidates.generate_candidates',side_effect=AssertionError('must not reselect')):
            evaluate_history(self.db,'run')
        with self.db.connection() as conn:
            self.assertEqual(conn.execute('SELECT state_json FROM candidate_runs').fetchone()[0],before)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM candidate_runs').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM candidate_ai_cache').fetchone()[0],0)

    def test_late_saved_list_never_uses_earlier_report_entry(self):
        self.result['saved_at']='2026-10-10T16:00:00+08:00';self.save()
        data=evaluate_history(self.db,'run')
        self.assertEqual(data['entry_date'],'2026-10-12')
        self.assertEqual(data['summary']['complete'],0)
        self.assertIsNone(data['summary']['up_ratio_pct'])

    def test_pending_missing_and_suspension_not_zero(self):
        with patch('app.candidate_history.shanghai_now',return_value=datetime(2026,10,9,16,tzinfo=SHANGHAI)):
            data=evaluate_history(self.db,'run')
        self.assertEqual(data['summary']['pending'],3)
        self.assertIsNone(data['summary']['average_return_pct'])
        self.bars['600001'][2]['volume']=0
        self.bars['600002'].pop(2)
        data=evaluate_history(self.db,'run')
        self.assertEqual(data['summary']['complete'],0)
        self.assertIn('停牌',data['rows'][0]['message'])
        self.assertIn('缺少行情',data['rows'][1]['message'])

    def test_scope_date_multiple_runs_empty_and_legacy_timestamp(self):
        self.save('second');self.save('other','efinance')
        items=list_history(self.db,'akshare','concept','2026-09-30')
        self.assertEqual(len(items),2)
        self.assertEqual(list_history(self.db,'akshare','concept','2026-09-29'),[])
        del self.result['saved_at'];self.save('legacy',created=datetime(2026,10,10,16,tzinfo=SHANGHAI))
        data=evaluate_history(self.db,'legacy')
        self.assertEqual(data['entry_date'],'2026-10-12')
        self.assertIn('旧记录',data['time_basis'])
        self.result['rows']=[];self.save('empty')
        self.assertEqual(evaluate_history(self.db,'empty')['summary']['total'],0)

    def test_unknown_calendar_invalid_prices_and_period(self):
        self.bars['600001'][0]['open']=float('nan')
        self.assertEqual(evaluate_history(self.db,'run')['rows'][0]['status'],'unavailable')
        self.result['saved_at']='2026-12-31T16:00:00+08:00';self.save()
        self.assertIsNone(evaluate_history(self.db,'run')['entry_date'])
        with self.assertRaises(ValueError):evaluate_history(self.db,'run',6)

    def test_actual_api_parameter_validation_and_saved_scope(self):
        with patch('app.db.Database',return_value=self.db),patch('app.config.ensure_directories'):
            main=importlib.import_module('main')
        with patch.object(main,'database',self.db):
            client=LocalAsgiClient(main.app)
            response=client.get('/api/candidates/history',params={'provider':'akshare','saved_date':'2026-09-30'})
            self.assertEqual(len(response.json()['runs']),1)
            self.assertEqual(client.get('/api/candidates/history',params={'saved_date':'bad'}).status_code,422)
            response=client.get('/api/candidates/runs/run/performance',params={'days':5})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['summary']['complete'],2)
            self.assertEqual(client.get('/api/candidates/runs/run/performance',params={'days':6}).status_code,422)
            self.assertEqual(client.get('/api/candidates/runs/unknown/performance').status_code,404)

if __name__=='__main__':unittest.main()
