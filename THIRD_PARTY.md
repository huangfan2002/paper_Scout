# 第三方数据与许可

本 demo 的 CCFDDL 集成读取其官方站点发布的 JSON，保留原始会议届次、轮次、评论、时区及官网链接；不执行上游仓库的程序。

- 项目：[ccfddl/ccf-deadlines](https://github.com/ccfddl/ccf-deadlines)，Copyright (c) 2021 CCFDDL，MIT License。完整许可随项目保存为 `LICENSE-CCFDDL.txt`。
- 截止日期来源：[官方站点 allconf.json](https://ccfddl.com/conference/allconf.json)。随附快照保存于 `config/ccfddl_snapshot.json`，下载时间、SHA-256 和来源位于 `metadata`。网站数据可能先于或晚于 GitHub 主分支构建，未将仓库提交号冒称为站点数据的构建版本。
- 大方向参照：[conference/types.yml](https://github.com/ccfddl/ccf-deadlines/blob/main/conference/types.yml)。核对时仓库提交为 `7c189b2a8281a67c2d1adbcd3326dfa9e9c5236b`，记录在 `config/conference_taxonomy.json`。47 个小方向由本 demo 整理，是可修改的导航分组。
- CCF 2026 目录的发布版本、来源、PDF 页码及核验记录保留于原目录文件。官方 CCF 文档及其权利不因本 demo 的 MIT 许可而改变。

CCFDDL 属于社区维护数据。程序保留 TBD 与原始时区，展示其日期默认规则，并提供会议官网和仓库文件链接供核对。未将社区维护的截止日期声明为会议主办方逐项核验的日期。
