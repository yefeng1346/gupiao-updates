# 本地股票板块复盘工具：更新发布仓库

用户可直接从[GitHub发布页](https://github.com/yefeng1346/gupiao-updates/releases/latest)的Assets下载完整ZIP，关闭软件后覆盖原目录的两个EXE及完整`_internal`目录；不要删除`.env`、`data`、`reports`。不要选择“Source code”。原有在线更新入口仍保留，latest.json兼容旧版本；受安装HTTP 500影响的旧版本应手动升级一次。

最新版本：[1.7.0说明及手动升级步骤](v1.7.0-release-notes.md)。重做左侧导航和今日概览，新增跟随系统/浅色/深色主题、平静复盘与本机复盘日记。升级保留本机Key、配置、历史数据库、公式、复盘日记和选股结果；公开更新包不包含用户凭据或本机数据。

source目录保留历史源码快照；本次1.7.0源码不放在公开更新仓库。维护者在私有开发仓库构建新版本，用户通过GitHub Release下载完整更新包。

资金归档与历史同步说明：[维护文档](source/tools/FLOW_ARCHIVE.md)。云端公开采集默认关闭，必须先确认来源分发授权和云端可用性。当前没有已部署的公共历史数据集，历史接口不可用的过去日期不能凭空补出。
