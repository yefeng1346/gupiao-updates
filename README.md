# 本地股票板块复盘工具：更新发布仓库

用户可直接从[GitHub发布页](https://github.com/yefeng1346/gupiao-updates/releases/latest)的Assets下载完整ZIP，关闭软件后覆盖原目录的两个EXE及完整`_internal`目录；不要删除`.env`、`data`、`reports`。不要选择“Source code”。原有在线更新入口仍保留，latest.json兼容旧版本；受安装HTTP 500影响的旧版本应手动升级一次。

最新版本：[1.6.4说明及手动升级步骤](v1.6.4-release-notes.md)。修复更新安装HTTP 500，补上安装接口回归和打包EXE真实接口检查。保留候选股票与AI解读、可调资金流入筛选及3日净流入等功能；升级保留本机Key、配置、历史数据库、公式和选股结果，公开包不含私有凭据和用户数据。

source目录保存本次公开程序源码。维护者可在该目录安装requirements.txt依赖并运行main.py对应FastAPI服务，桌面入口为desktop_app.py。

资金归档与历史同步说明：[维护文档](source/tools/FLOW_ARCHIVE.md)。云端公开采集默认关闭，必须先确认来源分发授权和云端可用性。当前没有已部署的公共历史数据集，历史接口不可用的过去日期不能凭空补出。
