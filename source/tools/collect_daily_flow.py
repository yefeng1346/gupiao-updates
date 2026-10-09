"""Local daily collector; public publishing requires explicit data rights.

Run from the project root: python -m tools.collect_daily_flow --output PATH
Does not load .env, keys, formula data or application database by default.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import tempfile

from app.db import Database
from app.flow_archive import collect_current, validate_daily
from app.flow_calendar import shanghai_now, is_trading_day


def atomic_json(path, payload):
    data = (json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n").encode("utf-8")
    path.parent.mkdir(parents=True,exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent,prefix=".flow-",suffix=".tmp")
    try:
        with os.fdopen(fd,"wb") as stream: stream.write(data)
        os.replace(temp,path)
    finally:
        if os.path.exists(temp): os.unlink(temp)
    return hashlib.sha256(data).hexdigest()


def export_daily(report, output, base_urls):
    day = report.get("confirmed_trade_date")
    if not day or not report.get("archive_complete"):
        raise ValueError("当天完整收盘数据未确认，未输出公开档案")
    payload = {"schema":1,"source":"eastmoney","metric":"main_net_inflow","unit":"CNY",
               "sector_type":report["sector_type"],"trade_date":day,"closed":True,
               "captured_at":report["captured_at"],"total":len(report["rows"]),"rows":report["rows"]}
    validate_daily(payload,report["sector_type"],day)
    filename = f"{day}/{report['sector_type']}.json"
    sha = atomic_json(output/filename,payload)
    return {"trade_date":day,"sector_type":report["sector_type"],"sha256":sha,
            "urls":[url.rstrip("/")+"/"+filename for url in base_urls]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--database",type=Path,default=Path(".collector/flow.db"))
    parser.add_argument("--base-url",action="append",default=[])
    parser.add_argument("--public",action="store_true")
    parser.add_argument("--redistribution-authorized",action="store_true")
    args = parser.parse_args()
    if args.public and (not args.redistribution_authorized or not args.base_url):
        parser.error("公开分发必须已取得来源授权，并提供下载基址；未采集/上传任何数据")
    now = shanghai_now()
    if is_trading_day(now.date()) is not True or (now.hour,now.minute)<(15,10):
        print(json.dumps({"skipped":True,"reason":"不是已确认的收盘后交易日"},ensure_ascii=True)); return
    output = args.output.resolve()
    index_path = output/"index.json"
    entries = json.loads(index_path.read_text("utf-8")).get("entries",[]) if index_path.exists() else []
    db = Database(args.database)
    errors = []
    for sector_type in ("concept","industry"):
        try:
            report = collect_current(db,sector_type)
            if report.get("confirmed_trade_date") != now.date().isoformat(): raise ValueError("来源不是当天收盘数据")
            entry = export_daily(report,output,args.base_url)
            entries = [old for old in entries if (old["trade_date"],old["sector_type"]) != (entry["trade_date"],entry["sector_type"])]+[entry]
        except Exception as exc: errors.append({"sector_type":sector_type,"error":str(exc)})
    if entries:
        atomic_json(index_path,{"schema":1,"source":"eastmoney","metric":"main_net_inflow","unit":"CNY",
                                "redistribution_authorized":bool(args.public and args.redistribution_authorized),
                                "updated_at":datetime.now(timezone.utc).isoformat(),"entries":sorted(entries,key=lambda e:(e["trade_date"],e["sector_type"]))})
    print(json.dumps({"entries":len(entries),"errors":errors},ensure_ascii=True))
    if errors: raise SystemExit(1)


if __name__ == "__main__": main()
