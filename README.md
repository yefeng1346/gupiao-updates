# 本地股票板块复盘工具：更新发布仓库

用户直接在软件点击“检查更新 → 立即更新”。latest.json是兼容旧版本的更新清单，压缩包同时提供GitHub Raw和Release下载地址。

最新版本：[1.6.0说明](v1.6.0-release-notes.md)。升级保留本机Key、配置、历史数据库、公式和选股结果；公开包不含私有凭据和用户数据。

source目录保存本次公开程序源码。维护者可在该目录安装requirements.txt依赖并运行main.py对应FastAPI服务，桌面入口为desktop_app.py。

资金归档与历史同步说明：[维护文档](source/tools/FLOW_ARCHIVE.md)。云端公开采集默认关闭，必须先确认来源分发授权和云端可用性。当前没有已部署的公共历史数据集，历史接口不可用的过去日期不能凭空补出。
