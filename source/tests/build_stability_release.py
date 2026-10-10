"""Create a full public portable package; never collect user configuration/data."""
from pathlib import Path
import hashlib
import shutil
import json
import zipfile
from PyInstaller.archive.readers import CArchiveReader

BASE = Path('D:/Codes/gupiao/release')
BUILD = BASE/'gupiao-windows-2026-10-10-update-install-fix-v1.6.4-build/dist/GupiaoStockTool'
NAME = 'gupiao-windows-2026-10-10-update-install-fix-v1.6.4.zip'
ARCHIVE = BASE/NAME

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()

def main():
    code = CArchiveReader(str(BUILD/'GupiaoStockTool.exe')).open_embedded_archive('PYZ.pyz')
    assert '1.6.4' in code.extract('app.update_service').co_consts
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
    manifest = {'version':'1.6.4','url':f'https://raw.githubusercontent.com/yefeng1346/gupiao-updates/main/{NAME}',
        'urls':[f'https://github.com/yefeng1346/gupiao-updates/releases/download/v1.6.4/{NAME}'],
        'sha256':sha,'published_at':'2026-10-10',
        'notes':'修复下载完成后安装接口因不存在的任务变量导致HTTP 500。资金任务运行时安全提示等待，未准备安装包或文件占用时不退出原版本；新增安装接口回归和打包EXE真实接口冒烟测试。保留1.6.3候选股票与AI解读、资金筛选等功能，以及Key、配置、历史数据、公式和选股结果。受影响旧版本请从GitHub Release下载完整ZIP，关闭同目录所有窗口后覆盖程序；不要删除.env、data和reports。'}
    (BASE/'v1.6.4-latest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'archive':str(ARCHIVE),'bytes':ARCHIVE.stat().st_size,'files':len(files),'sha256':sha},ensure_ascii=False))

if __name__=='__main__':main()
