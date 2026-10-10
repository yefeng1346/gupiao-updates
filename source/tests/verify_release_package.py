"""Validate a generated patch without installing it into a user's tool folder."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

from app.db import Database
from updater_app import _install, _safe_extract
from app.update_service import promote_bundled_updater


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').digest()


def verify(archive: Path) -> None:
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        assert "GupiaoStockTool.exe" in names
        assert "_internal/web/index.html" in names
        assert "_internal/web/candidates.js" in names
        assert all(name in {"GupiaoStockTool.exe","GupiaoUpdater.exe"} or name.startswith("_internal/") for name in names)
        assert "_internal/GupiaoUpdater.next.exe" in names
        assert not any(name.endswith((".env", ".db", ".sqlite")) for name in names)
        assert package.testzip() is None
    with tempfile.TemporaryDirectory(prefix="gupiao-release-check-") as folder:
        root = Path(folder)
        target = root / "existing-user-install"
        target.mkdir()
        (target / ".env").write_text("ARK_API_KEY=test-placeholder-preserve\nTDX_ROOT=D:/test\n", encoding="utf-8")
        reports = target / "reports"
        reports.mkdir()
        (reports / "saved-report.txt").write_text("existing report", encoding="utf-8")
        db = Database(target / "data" / "stocks.db")
        with db.connection() as conn:
            conn.execute("INSERT INTO formula_definitions (name, formula, created_at, updated_at) VALUES ('saved strategy','X:C>1;','old','old')")
            conn.execute("CREATE TABLE preserved_selection (code TEXT)")
            conn.execute("INSERT INTO preserved_selection VALUES ('000001')")
        (target / "GupiaoStockTool.exe").write_bytes(b"old program")
        (target / "GupiaoUpdater.exe").write_bytes(b"existing updater")
        preserved = [target / ".env", reports / "saved-report.txt", db.path, target / "GupiaoUpdater.exe"]
        before = {p: digest(p) for p in preserved}
        staged = _safe_extract(archive, root / "staged")
        _install(staged, target)
        for path in preserved:
            assert digest(path) == before[path], path.name
        assert promote_bundled_updater(target)
        assert digest(target/'GupiaoUpdater.exe')==digest(target/'_internal/GupiaoUpdater.next.exe')
        for path in preserved[:-1]:
            assert digest(path)==before[path],path.name
        assert (target / "GupiaoStockTool.exe").stat().st_size > 1_000_000
        html = (target / "_internal" / "web" / "index.html").read_text(encoding="utf-8")
        assert 'id="candidateSection"' in html
        assert '/static/candidates.js' in html
        assert 'id="retryCapitalFlowBtn"' in html
        assert 'id="clearCapitalFlowBtn"' in html
        assert '/api/sector-capital-flow/clear' in html
        assert 'id="currentFlowRefresh"' in html
        assert 'id="capitalFlowWindowDays"' in html
        assert 'id="capitalFlowMinInflowDays"' in html
        assert '<summary><strong>高级：历史补缺、来源文件、删除历史档案</strong></summary>' in html
        assert html.count('key:"three_day_main_net_inflow",label:"3日资金净流入"') == 2
        assert 'loadSectorLeaders(report, tenDayRows' not in html
        assert "retry_missing=true" in html
        assert '/api/update/status' in html
        assert '/api/update/install' in html
        assert '历史不足/不可计算' in html
        env = dict(os.environ,DATABASE_PATH=str(db.path),REPORT_DIR=str(reports),AUTO_SYNC_ON_OPEN='false',AUTO_CLOSE_SYNC='false',AUTO_FLOW_ARCHIVE='false',ARK_API_KEY='test-placeholder-preserve')
        process = subprocess.run([str(target/'GupiaoStockTool.exe'),'--smoke-test'],cwd=target,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),capture_output=True,timeout=60)
        assert process.returncode==0,(process.returncode,process.stderr[-1000:])
        assert digest(target/'.env')==before[target/'.env']
        with db.connection() as conn:
            assert conn.execute('SELECT code FROM preserved_selection').fetchone()[0]=='000001'
            assert conn.execute("SELECT COUNT(*) FROM formula_definitions WHERE name='saved strategy'").fetchone()[0]==1
        assert digest(reports/'saved-report.txt')==before[reports/'saved-report.txt']
        assert not (target/'desktop-startup-error.log').exists()
    print("PASS: patch contents, extraction, program replacement, API-key/config/database/formula/result preservation")


if __name__ == "__main__":
    verify(Path(sys.argv[1]).resolve())
