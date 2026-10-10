"""Read-only evaluation of saved watchlists, never retroactively select stocks."""
from datetime import date, datetime, timedelta
import json
import math
import re
from . import formula_screen as fs
from .flow_calendar import SHANGHAI, shanghai_now, latest_closed_trading_day, is_trading_day

NOTE = '仅为已保存候选的未复权价格表现，不是实盘收益或荐股成功率。不含交易费用、分红及可成交性核验；除权除息可能影响结果。缺失、停牌或观察期未满不纳入上涨占比。'

def saved_time(record, result):
    try:
        value = datetime.fromisoformat(result['saved_at'])
        if value.tzinfo is None: raise ValueError('naive')
        return value.astimezone(SHANGHAI), '候选保存时间'
    except (KeyError, TypeError, ValueError):
        return datetime.fromtimestamp(record['created_at'], SHANGHAI), '旧记录仅有任务创建时间（非精确候选生成时间）'

def snapshot(database, run_id):
    with database.connection() as conn:
        record = conn.execute('SELECT id,state_json,created_at FROM candidate_runs WHERE id=?', (run_id,)).fetchone()
    if not record: raise LookupError('找不到已保存候选名单')
    run = json.loads(record['state_json'])
    result = run.get('result')
    if not isinstance(result, dict): raise LookupError('该任务尚未保存候选名单')
    stamp, basis = saved_time(record, result)
    return run, stamp, basis

def list_history(database, provider, sector_type, saved_date=None):
    if saved_date: date.fromisoformat(saved_date)
    with database.connection() as conn:
        records = conn.execute('SELECT id,state_json,created_at FROM candidate_runs WHERE provider=? AND sector_type=? ORDER BY created_at DESC', (provider, sector_type)).fetchall()
    items=[]
    for record in records:
        run=json.loads(record['state_json']); result=run.get('result')
        if not isinstance(result,dict): continue
        stamp,basis=saved_time(record,result)
        if saved_date and stamp.date().isoformat()!=saved_date: continue
        items.append({'id':record['id'],'saved_at':stamp.isoformat(),'saved_date':stamp.date().isoformat(),
                      'time_basis':basis,'report_date':result['report_date'],'count':len(result.get('rows',[])),
                      'status':run['status'],'provider':provider,'sector_type':sector_type})
    return items

def next_days(after, count):
    days=[]; current=after
    for _ in range(120):
        current+=timedelta(days=1)
        state=is_trading_day(current)
        if state is None: return None
        if state: days.append(current.isoformat())
        if len(days)==count: return days
    return None

def price(value):
    try: value=float(value)
    except (TypeError,ValueError): return None
    return value if math.isfinite(value) and value>0 else None

def evaluate_history(database, run_id, days=5):
    if days not in (5,10,20): raise ValueError('观察期仅支持5、10或20个交易日')
    run,stamp,basis=snapshot(database,run_id)
    result=run['result']; report_date=date.fromisoformat(result['report_date'])
    # Never buy at the old report date when the actual list was saved later.
    window=next_days(max(stamp.date(),report_date),days)
    now=shanghai_now(); cutoff,_=latest_closed_trading_day(now.date(),now)
    file_map=dict(fs._discover_day_files())
    items=[]; seen=set()
    for row in result.get('rows',[]):
        code=str(row.get('code',''))
        if code in seen: continue
        seen.add(code)
        item={'code':code,'name':row.get('name',code),'score':row.get('score'),
              'ai':row.get('ai',{}),'reasons':row.get('reasons',[]),'status':'unavailable',
              'return_pct':None,'drawdown_pct':None,'message':''}
        items.append(item)
        if window is None:
            item['message']='交易日历未覆盖该年份，暂不能计算'; continue
        if window[-1]>cutoff.isoformat():
            item.update(status='pending',message='观察期未满，暂不纳入统计'); continue
        if not re.fullmatch(r'\d{6}',code) or code not in file_map:
            item['message']='本机缺少该个股日线，请在通达信下载'; continue
        try:
            bars=fs._read_daily(file_map[code])
            indexed={str(b['date'])[:10]:b for b in bars}
            selected=[indexed.get(day) for day in window]
            if any(b is None for b in selected):
                item['message']='固定观察窗口内缺少行情，不以更早或更晚日期替代'; continue
            if any(price(b.get('volume')) is None for b in selected):
                item['message']='观察窗口内有停牌或无成交记录，暂不纳入统计'; continue
            start=price(selected[0].get('open')); closes=[price(b.get('close')) for b in selected]
            if start is None or any(v is None for v in closes):
                item['message']='价格无效，暂不纳入统计'; continue
            change=(closes[-1]/start-1)*100
            peak=start; drawdown=0
            for close in closes:
                peak=max(peak,close); drawdown=min(drawdown,(close/peak-1)*100)
            if not math.isfinite(change):
                item['message']='价格异常，暂不纳入统计'; continue
            item.update(status='complete',message='未复权价格观察，未验证可成交性',entry_price=start,
                        exit_price=closes[-1],return_pct=change,drawdown_pct=drawdown)
        except Exception:
            item['message']='本地日线读取失败，原名单保留'
    complete=[i for i in items if i['status']=='complete']
    ups=sum(i['return_pct']>0 for i in complete)
    return {'id':run_id,'provider':result['provider'],'sector_type':result['sector_type'],
            'saved_at':stamp.isoformat(),'time_basis':basis,'report_date':result['report_date'],
            'days':days,'entry_date':window[0] if window else None,'exit_date':window[-1] if window else None,
            'data_cutoff':cutoff.isoformat(),'rows':items,'note':NOTE,
            'summary':{'total':len(items),'complete':len(complete),'pending':sum(i['status']=='pending' for i in items),
                       'unavailable':sum(i['status']=='unavailable' for i in items),'up':ups,
                       'down':sum(i['return_pct']<0 for i in complete),'flat':sum(i['return_pct']==0 for i in complete),
                       'up_ratio_pct':ups/len(complete)*100 if complete else None,
                       'average_return_pct':sum(i['return_pct'] for i in complete)/len(complete) if complete else None}}
