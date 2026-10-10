"""Create a full public portable package; never collect user configuration/data."""
from pathlib import Path
import hashlib
import shutil
import json
import zipfile
from PyInstaller.archive.readers import CArchiveReader

BASE = Path('D:/Codes/gupiao/release')
BUILD = BASE/'gupiao-windows-2026-10-10-candidates-v1.6.3-build/dist/GupiaoStockTool'
NAME = 'gupiao-windows-2026-10-10-candidates-v1.6.3.zip'
ARCHIVE = BASE/NAME

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()

def main():
    code = CArchiveReader(str(BUILD/'GupiaoStockTool.exe')).open_embedded_archive('PYZ.pyz')
    assert '1.6.3' in code.extract('app.update_service').co_consts
    for module in ('app.candidates','app.flow_tasks','app.flow_transport','app.flow_archive','app.flow_summary','update_net','app.formula_screen'):
        assert code.extract(module) is not None,module
    updater = CArchiveReader(str(BUILD/'GupiaoUpdater.exe')).open_embedded_archive('PYZ.pyz')
    assert updater.extract('update_net') is not None
    shutil.copy2(BUILD/'GupiaoUpdater.exe',BUILD/'_internal/GupiaoUpdater.next.exe')
    files = sorted(p for p in BUILD.rglob('*') if p.is_file())
    assert not any(p.name in {'.env','data','reports'} or p.suffix in {'.db','.sqlite','.log'} for p in files)
    # Refuse to collect files outside the build output, even through symlinks.
    assert all(p.resolve().is_relative_to(BUILD.resolve()) for p in files)
    with zipfile.ZipFile(ARCHIVE,'x',zipfile.ZIP_DEFLATED,compresslevel=9) as package:
        for path in files:
            package.write(path,path.relative_to(BUILD).as_posix())
    sha = digest(ARCHIVE)
    manifest = {'version':'1.6.3','url':f'https://raw.githubusercontent.com/yefeng1346/gupiao-updates/main/{NAME}',
        'urls':[f'https://github.com/yefeng1346/gupiao-updates/releases/download/v1.6.3/{NAME}'],
        'sha256':sha,'published_at':'2026-10-10',
        'notes':'新增综合候选股票与AI解读：程序按板块排名/RPS、主线、已保存三日板块资金和通达信个股日线筛选，最多10只；右侧逐只加载现有模型的证据解读。支持不调用AI、取消、单只重试、关闭重开恢复及相同事实缓存，多窗口避免重复调用。AI失败不丢候选，日期不符和历史不足明确提示；须先在通达信补齐最新个股日线。候选和得分不是买卖指令或上涨概率；财务、公告与可成交性未核验。保留Key、配置、历史数据库、公式和选股结果。'}
    (BASE/'v1.6.3-latest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'archive':str(ARCHIVE),'bytes':ARCHIVE.stat().st_size,'files':len(files),'sha256':sha},ensure_ascii=False))

if __name__=='__main__':main()
