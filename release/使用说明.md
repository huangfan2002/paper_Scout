# Paper Scout — arXiv / Google Scholar / CCFDDL 文献与会议工作台

项目目录：`D:\BaiduNetdiskDownload\demo`

本地网页 + 命令行工具，支持 arXiv 与 Google Scholar，按关键词、时间、CCF A/B/C 会议等级筛选；接入 CCFDDL 会议截止日期，支持“大方向 → 小方向 → 会议”导航。Python **3.10+** 即可运行，运行时没有第三方依赖。arXiv 官方 API、CCFDDL 数据同步与 Scholar 浏览器检索 / 保存结果导入均无需 Key；Scholar 自动检索通过可选的 SerpApi 接口，需要自行配置 Key。无需 Node.js 或数据库服务器。

## 1. 启动

在 Windows 中双击 **start.bat**，启动服务并打开默认浏览器。

也可以在 PowerShell 中运行：

```powershell
cd D:\BaiduNetdiskDownload\demo
python -X utf8 app.py --open
```

访问 **http://127.0.0.1:8000**。终端中按 `Ctrl+C` 停止服务。请保持启动终端运行；关闭终端会结束服务。重复双击 `start.bat` 会打开已在运行的本 demo。

如果服务在后台运行，可在项目目录执行 `python -X utf8 app.py --stop` 停止；自定义端口时添加 `--port 8001`。

如果 8000 端口已被占用：

```powershell
python -X utf8 app.py --port 8001 --open
```

如果没有 Python，请先从 [Python 官网](https://www.python.org/downloads/) 安装 3.10 或以上版本，并启用 “Add Python to PATH”。没有需要执行的 `pip install` 步骤。

## 2. 网页用法

“数据来源”可选择 arXiv 或 Google Scholar。下列提交日期与自动 PDF 下载说明以 arXiv 为例；Scholar 的无需 Key 用法见第 11 节。

1. 输入英文关键词，例如 `federated learning`、`privacy`；精确短语输入 `"federated learning"`。多个关键词可选择 AND 或 OR。
2. 按需设置搜索字段，以及开始/结束日期。日期指 **arXiv 首次提交日期，UTC**，包含所选起止日；不是会议举办日期或正式发表日期。
3. 勾选 **A 类、B 类、C 类**，可组合选择。未勾选表示不限等级。“未知”包含没有可靠会议录用信息的论文，不能据此推断论文未被录用。
4. “更多筛选”中可以指定 arXiv 分类、特定会议、证据范围、排序、目标结果数及候选扫描上限。例如分类 `cs.LG, cs.CR`，会议 `NeurIPS` 或 `CCS`。
5. 点击“检索文献”。后台任务显示扫描进度；可以取消。取消时已经下载完成的 PDF 保留。
6. 点击标题或“摘要与证据”，查看完整摘要、原始 Comments / Journal reference 和会议判断依据。
7. 勾选文献，点击“下载所选”。PDF 和对应 JSON 元数据保存在 `downloads/`。重复下载有效 PDF 会跳过。
8. 导出 CSV、JSON、BibTeX。勾选时导出所选论文；未勾选时导出本次全部结果。CSV 使用 UTF-8 BOM，便于 Excel 打开中文。

“下载文库”可打开已保存的本地 PDF。“检索历史”保留最近 20 次完成的检索，可以恢复结果和筛选条件。“CCF 会议目录”支持大方向、小方向、会议名称及等级组合筛选。“会议截止日期”显示 CCFDDL 时间线、倒计时与多个时区，具体见第 12 节。

首次使用可以点击 **体验离线样例**。随项目保存的是 2026-10-08 从 arXiv 官方 API 获取的 19 篇真实元数据快照，覆盖 A/B/C、未知、Findings、Workshop 和投稿状态。样例明确标记为离线快照，不代表当前实时检索结果；下载样例 PDF 仍然需要联网。

## 3. CCF 等级与证据的含义

随附目录采用 **CCF 2026 第七版正式版，2026-04-09 勘误更新**，仅包含会议：**A 58、B 132、C 196，共 386 条**。目录简称、全称、等级及参考链接已逐项对照正式版 PDF 表格；保留每条会议的原 PDF 页码和原 PDF SHA-256。

正式版来源：[CCF 官方发布页面](https://www.ccf.org.cn/Academic_Evaluation/By_category/)。官方分类网页可能仍显示旧表，以正式版 PDF 为准。项目使用的 PDF 镜像、结构化数据参考和校对结果见 `config/ccf_conferences.json` 与 `config/catalogue_verification.json`。

按本目录，ICLR 为 A、IJCAI 为 B、HPDC 为 A。所有论文统一按这份目录版本映射，**不根据论文发表年份追溯当年的历史等级**。

arXiv 没有权威的“顶会录用”字段，因此把**会议等级**与**论文录用证据**分开：

| 状态 | 判断依据 | 默认进入 A/B/C 筛选 |
| --- | --- | --- |
| 作者录用声明 | Comments 的 Accepted / To appear 等，或 Journal reference 中的会议出处 | 是；作者填写，仍需核对 |
| Scholar 出版记录 | Scholar 出版摘要中匹配到会议，且记录包含发表年份 | 是；仍需核对原出版页面与长文类型 |
| DBLP 主会记录 | 可选 DBLP 查询中标题和作者匹配，记录 key 对应主会，排除已知 Workshop / Findings | 是；论文正式长文类型仍需核对 |
| 仅会议提及 | 只有会议名称，没有明确录用或发表声明 | 否；选择“也包含会议提及”后作为候选纳入 |
| 非主会 / 投稿信息 | 已知 Workshop、Findings、Short paper、Demo、非存档等类型，或 Submitted / Under review / Rejected / Withdrawn | 否 |
| 会议信息未知 | 元数据没有可匹配证据，或补充核验未匹配 | 仅在“未知”或不限等级时纳入 |

CCF 对会议的范围限定为正式发表的 Full / Regular paper。**仅凭作者的一句 Accepted 或 DBLP 的主会 key，不能证明论文符合正式长文要求。** 所以程序记录 `full_paper_verified: false`，详情界面也提示论文类型待核实。默认筛选用于文献发现，不能代替正式成果认定。

arXiv 会议判断只读取 Comments / Journal reference；Scholar 会议判断只读取出版摘要的出处部分。两者都不从标题或摘要的相关工作中猜测录用会议。若元数据没有填写或截断会议名称，会发生漏检；例如 arXiv 样例中的 “Attention Is All You Need” 在这次快照中没有会议出处，因此显示未知。

**DBLP 为可选补充。** 默认关闭，每次最多核验 30 篇缺失会议信息的候选。当前网络实测 DBLP 搜索接口可能返回机器人验证页，程序会提示并暂停本次补充核验，继续使用 arXiv 元数据。没有绕过验证页，也不会把补充核验失败当作“非 CCF”。

## 4. 为什么区分目标数和扫描上限

arXiv API 可直接筛选关键词与提交日期，但不能可靠地直接筛选 CCF 等级。流程是：

```text
检索条件 → 官方 API 分页取候选 → 会议证据识别 → CCF 等级筛选 → 保留结果 → 导出 / 下载
```

“目标结果数 50”表示最多保留 50 篇；“候选扫描上限 300”表示最多检查 300 条候选。如果会议信息稀疏，可能扫描 300 条仍不足 50 篇。这时会显示扫描上限提示，**不会把候选总数冒充为筛选后的总数**。可以缩小关键词/日期范围或提高扫描上限（最多 2000 条）。

API 按实际返回条数继续分页；短页不会直接被当作全部结果结束。按论文基础 ID 去重，并检测错误空页或异常重复分页。

## 5. 命令行

无需先启动网页服务：

```powershell
# 联邦学习，按首次提交日期筛选，保留 CCF A/B 结果
python -X utf8 cli.py --keywords "federated learning" --date-from 2025-01-01 --date-to 2026-10-08 --ccf A B --max-results 20 --scan-limit 300 --format csv

# 只看 AISTATS（CCF C），并下载匹配结果
python -X utf8 cli.py --keywords "learning" --ccf C --conferences AISTATS --max-results 5 --scan-limit 300 --download

# 包含只有会议提及的候选
python -X utf8 cli.py --keywords "privacy" --ccf A --evidence mentions --format bibtex

# 不联网，导出离线样例
python -X utf8 cli.py --sample --format json

# 查看 CCF A 会议目录
python -X utf8 cli.py --list-conferences --ccf A

# 查看全部参数
python -X utf8 cli.py --help
```

默认导出到 `exports/arxiv_<结果ID>.<格式>`。可以使用 `--output "D:\your-path\papers.csv"` 或 `--download-dir "D:\your-path\pdfs"` 指定路径。已有导出文件不会覆盖。

`--sample` 加载固定样例，其他检索条件不会对该样例执行实时检索或重新筛选。网页中勾选等级后点击“检索文献”才会按条件查询。

## 6. 项目结构

```text
demo/
├── app.py                     # 本地 HTTP 服务；仅绑定 127.0.0.1
├── cli.py                     # 命令行检索、导出、下载
├── start.bat / start.ps1       # Windows 启动入口
├── arxiv_demo/
│   ├── query.py               # 参数验证与 arXiv 查询构造
│   ├── client.py              # Atom 解析、限速、重试、PDF 下载
│   ├── conferences.py         # CCF 名称/别名与录用证据判定、可选 DBLP
│   ├── navigation.py          # 大方向、小方向与会议归类
│   ├── deadlines.py           # CCFDDL 时间线、时区换算与快照同步
│   ├── scholar.py             # Scholar 网页入口、HTML/JSON 导入、可选 SerpApi
│   ├── service.py             # 分页筛选与后台任务队列
│   ├── storage.py             # SQLite 缓存、论文元数据、检索历史
│   └── exports.py             # CSV / JSON / BibTeX
├── web/                       # 原生 HTML / CSS / JavaScript，无构建步骤
├── config/
│   ├── ccf_conferences.json    # 386 个会议，含版本、来源、页码、别名
│   ├── conference_taxonomy.json # 10 个大方向、47 个小方向
│   ├── ccfddl_snapshot.json    # 随附 CCFDDL 会议与历年届次快照
│   ├── scholar.example.json   # 可选接口配置示例
│   └── catalogue_verification.json
├── examples/sample_papers.json # 真实元数据的离线样例
├── scripts/check_catalogue.py  # 目录结构检查
├── tests/                     # 标准库 unittest，不访问互联网
├── data/arxiv.sqlite3          # 运行时自动生成
├── data/server.log            # 服务日志
├── data/ccfddl_snapshot.json   # 用户同步后的快照，优先于随附数据
├── downloads/                 # PDF + 对应元数据 JSON
└── exports/                   # 命令行导出文件
```

## 7. 修改会议名单

编辑 `config/ccf_conferences.json`，保存后重启服务。每条记录包含：

```json
{
  "id": "custom-myconf",
  "acronym": "MyConf",
  "name": "Example Conference on Research",
  "rank": "B",
  "domain": "自定义",
  "aliases": ["MYCONF"],
  "dblp_keys": ["myconf"]
}
```

`id` 必须唯一；`rank` 为 A/B/C。`aliases` 用于匹配 arXiv 的会议名称变体；`dblp_keys` 是 DBLP 的主会目录 key，例如 `conf/nips/...` 对应 `nips`，不能填 Workshop 的 key。添加或改级属于本地自定义口径，请同步修改目录 `metadata.version`，避免误称官方等级。

## 8. 网络与运行细节

- 使用 `https://export.arxiv.org/api/query`，不解析网页 HTML。API 与 PDF 请求共享串行限速器，完成请求后等待至少 3.1 秒再发下一次请求。
- 相同 API 查询页缓存 24 小时；DBLP 响应缓存 7 天。429、部分 5xx 和连接失败最多尝试 3 次，尊重数字形式的 Retry-After。
- PDF 流式写入 `.pdf.part`，验证 PDF 文件头和 Content-Length；完整后原子改名。失败或取消不会留下冒充完整 PDF 的临时文件。
- 单批下载最多 100 篇，单 PDF 上限 150 MB。文件名使用版本化 arXiv ID，避免论文标题中的非法 Windows 字符。
- 浏览器界面支持取消，但已进入同步网络读操作时，取消会在下一个读取检查点或连接超时后生效。
- 使用本地 SQLite，无云存储。只绑定回环地址，接口带会话校验和请求参数校验；不是公网生产部署服务。
- 一个服务实例内使用单个后台任务队列。**请不要同时运行多个服务实例或在网页抓取时另开 CLI 网络抓取**，否则实例之间不能共享限速状态。大量、长期抓取应使用 arXiv 官方建议的批量接口。

如果网络不能访问 arXiv，可按本机代理端口设置（以下端口仅为示例）：

```powershell
$env:HTTPS_PROXY = "http://127.0.0.1:7890"
$env:HTTP_PROXY = "http://127.0.0.1:7890"
$env:NO_PROXY = "127.0.0.1,localhost"
python -X utf8 app.py --open
```

仅支持 urllib 可使用的 HTTP(S) 代理。不要在项目源码中写入代理密码。可选 `ARXIV_USER_AGENT` 环境变量可指定应用标识及联系信息。浏览器端不向第三方发送请求，实际抓取由 Python 后端完成。

## 9. 验证

```powershell
python -X utf8 -m unittest discover -s tests -v
python -X utf8 scripts/check_catalogue.py
```

离线测试覆盖查询语法与日期、三档等级、2026 目录变动、会议别名、投稿/拒稿/Workshop/Findings 排除、同一会议同时满足等级与名称条件、DBLP 标题作者匹配、分页去重与扫描上限、取消与缓存、CSV 公式防护、BibTeX 转义、PDF 完整写入与重复跳过、本地 HTTP 接口、会话验证和路径检查。

实际运行记录见 `VALIDATION.md`。离线测试不依赖外部网站；真实服务状态可能变化。

## 10. 参考与来源

- [arXiv API 用户手册](https://info.arxiv.org/help/api/user-manual.html)：关键词字段、布尔查询、submittedDate、分页与元数据。
- [arXiv API 使用条款](https://info.arxiv.org/help/api/tou.html)：至少三秒间隔与单连接要求。
- [lukasschwab/arxiv.py](https://github.com/lukasschwab/arxiv.py)：参考官方 API 客户端的分页、延迟与重试组织方式。
- [karpathy/arxiv-sanity-lite](https://github.com/karpathy/arxiv-sanity-lite)：参考本地论文列表与文献浏览交互。
- [CCF 官方目录发布](https://www.ccf.org.cn/Academic_Evaluation/By_category/)：第七版正式目录及正式长文范围。
- [haozhou-wong/ccf-recommended-list-2026-markdown](https://github.com/haozhou-wong/ccf-recommended-list-2026-markdown)：CCF PDF 的结构化数据参考。项目中的 386 条会议已重新对照 PDF 校验。
- [LovenSar/CCF_RANK](https://github.com/LovenSar/CCF_RANK)：原始正式版 PDF 的公开镜像；不依赖其其他期刊或排名数据。
- [DBLP 搜索 API](https://dblp.org/faq/How%2Bto%2Buse%2Bthe%2Bdblp%2Bsearch%2BAPI.html)：可选补充核验接口。

应用代码为独立实现，MIT 许可证见 `LICENSE`。CCF 名单保留其来源与权益说明，元数据使用应遵循相应来源的条款。这里没有附带完整 CCF PDF 或整批论文 PDF。

## 11. Google Scholar（谷歌学术）

### 无 API Key：浏览器检索 + 保存结果导入

1. 在网页“数据来源”中选择 **Google Scholar · 谷歌学术**。
2. 输入关键词，可选全部、标题、作者，设置 AND / OR 和**发表年份范围**。Scholar 不提供本 demo 所需的精确提交日期，界面不会把年份伪装成某年 1 月 1 日。仅摘要与 arXiv 分类在该来源中不可用。
3. 点击 **打开 Google 学术**。链接包含关键词、搜索字段、界面语言与起止年份；CCF 等级需在结果导入后筛选，不由 Google 直接筛选。
4. 在浏览器得到结果后，用 `Ctrl+S` 将**检索结果页**保存为 HTML（完整网页或仅 HTML 均可），或保存已有的 SerpApi JSON。选用 UTF-8，单文件最多 1 MB。请保存实际结果页，验证页面不能导入。
5. 回到项目，勾选所需 CCF 等级 / 会议，点击 **导入保存的结果**，选择文件。导入模式只应用本地年份和 CCF 筛选，不重新运行关键词或 DBLP 查询；关键词应先在 Scholar 页面搜索。缺失年份的条目不会被纳入有年份范围的结果。
6. 可以查看**引用次数、相关论文、引用此文的论文、其他版本、出版页面和 PDF 链接**，并导出 CSV / JSON / BibTeX、保存检索历史。

只导入保存文件中的结果。例如保存第一页，不代表收集了整个查询的所有结果。HTML 在服务器中作为文本解析，不执行页面脚本，也不会加载其远程资源。Citation / 作者列表 / 出版摘要可能被截断；结果中的检索片段不是完整摘要。导入 JSON 接受 `organic_results` 数组或直接的条目数组，不是任意论文 JSON 或 BibTeX。

自动批量下载支持能从 Scholar 出版 / PDF 链接定位到 **arXiv ID** 的条目。其他来源通过“出版页面”“打开 PDF”“其他版本”访问；无法获取全文的条目仍可保存元数据与导出。Publisher 的登录、访问条件和 PDF 可用性由相应网站决定。

点击 **体验 Scholar 文档样例** 可离线查看功能。两个条目的书目信息与历史引用数取自 [SerpApi 官方接口文档](https://serpapi.com/google-scholar-api) 的 2019-09-26 biology 示例；未附带其原摘要。它是历史文档示例，不是当前实时检索，且这两个示例属于非 CCF 会议文献。

### 可选：配置自动检索

Google Scholar 的 [官方帮助](https://scholar.google.com/intl/en/scholar/help.html) 说明不提供批量记录访问，自动访问需遵守其 robots.txt。本项目自动检索使用 [SerpApi Google Scholar 接口](https://serpapi.com/google-scholar-api)，不会自动请求 Google 验证页面。SerpApi 是第三方服务，账户额度及费用请在其官方账户中确认；程序不会注册账户或购买服务。

将 `config/scholar.example.json` 复制为 `config/scholar.local.json`，只在本机填写：

```json
{"api_key": "你的 SerpApi Key"}
```

也可在启动服务前设置环境变量 `SERPAPI_API_KEY`（优先于本机 JSON）。配置后刷新页面，“自动检索已配置”表示可使用“检索文献”。配置文件不进入 Git / 源码包，Key 不发送给前端、不写入任务结果、缓存或导出文件。接口错误不会回显完整 URL 或 Key。只有自动检索会向 SerpApi 发送检索条件；API 请求可能消耗该账户额度。

自动检索按相关性分页，每页最多 20 条，支持年份范围。候选扫描最多 1000 条，并在本机匹配 CCF；命中总数显示为估计值。缓存 24 小时、同一进程内请求串行，网络失败不自动重试以免重复消耗额度。遇到 Key 无效、额度耗尽或验证 / 非 JSON 响应会显示错误。可选 DBLP 补充核验与 arXiv 模式相同。

当前用户未配置 Key，所以 **SerpApi 自动检索已通过模拟接口与本地集成测试，尚未做实际账户的联网检索**。无需 Key 的页面入口、文件导入和导出可直接使用。

### 命令行

```powershell
# 无需 Key，仅输出条件对应的 Scholar 网页检索链接
python -X utf8 cli.py --source google_scholar --keywords "federated learning" --year-from 2024 --year-to 2026 --scholar-link

# 无需 Key，导入自己保存的结果页，筛选 A/B 会议
python -X utf8 cli.py --import-scholar "D:\your-path\scholar.html" --ccf A B --year-from 2024 --format csv

# 离线查看官方文档示例
python -X utf8 cli.py --source google_scholar --sample --format json

# 配置 Key 后进行自动检索
python -X utf8 cli.py --source google_scholar --keywords "federated learning" --year-from 2024 --ccf A B --max-results 20 --scan-limit 200 --format csv
```

新增代码在 `arxiv_demo/scholar.py`；配置示例在 `config/scholar.example.json`；离线样例在 `examples/scholar_sample.json`；新增回归测试在 `tests/test_scholar.py`。两种来源共用 CCF 目录、SQLite 历史与导出流程。CSV 明确区分 `source` / `paper_id` / `arxiv_id` / `publication_year`，不会把 Scholar 记录标成 arXiv 论文；Scholar 的 BibTeX 采用待核对的 `misc`，不编造会议卷册或页码。

## 12. CCFDDL 集成与分层会议目录（v1.2）

集成来源是 [ccfddl/ccf-deadlines](https://github.com/ccfddl/ccf-deadlines)。当前 demo 接入其数据与方向分类，保留本地 Python 启动方式。无需 CCFDDL 账号或 API Key。

### 大方向 → 小方向 → 会议

“CCF 会议目录”先显示 10 个大方向，再显示其小方向，最后按小方向展示会议；可与名称和 A/B/C 等级组合筛选。例如：

- 人工智能 → 机器学习与统计学习 → NeurIPS、ICML、ICLR、COLT、AISTATS 等。
- 人工智能 → 自然语言处理 → ACL、EMNLP、COLING、NAACL 等。
- 网络与信息安全 → 密码学 → CRYPTO、EUROCRYPT、ASIACRYPT、FSE 等。

10 个大方向对齐 CCFDDL 的 `conference/types.yml`。**47 个小方向由本 demo 按研究主题整理，属于可编辑导航，不是 CCF 或 CCFDDL 官方细分。** 386 个官方会议均归入一个主方向，跨领域会议可能同时涉及其他主题。FSE、SEC 等重名会议以大方向与 DBLP 标识区分。

分组保存在 `config/conference_taxonomy.json`：`majors` 中的 `subfields` 包含稳定 ID、中文名称与 `venue_ids`。可移动同一大方向下会议的 ID、增删小方向或更改名称，保存后重启服务。新加入且尚未分组的目录会议自动显示在“综合 / 待归类”，不会消失。等级和会议别名仍在 `config/ccf_conferences.json` 中维护。

点击会议行的“论文筛选”或“检索当前方向的论文”，将会议加入文献检索条件，并打开“更多筛选”。原关键词和时间保留，等级复选框清空以免与所选会议冲突；可以自行调整后点击“检索文献”。方向可以用于 arXiv 检索及 Scholar 导入后的本地筛选，Scholar 网页链接本身只携带关键词与年份。在“更多筛选”中也可直接按大方向、小方向选择会议。

### 会议截止日期

左侧“会议截止日期”展示社区维护的会议时间线，支持方向、CCF 等级、会议举办年份、名称、城市、时间节点和截止状态筛选；每页 24 条。**会议举办年份与论文的提交日期 / 发表年份是不同的字段。** 默认显示尚未截止或最新届次待公布的正文截稿，可以切换到往期、摘要注册、作者回复和录用通知。

- 原始数据来自 [CCFDDL 官方站点 JSON](https://ccfddl.com/conference/allconf.json)。随附 2026-10-08 快照包含 **374 个会议、2,301 条届次记录**，断网时仍可浏览。
- “同步最新数据”后台读取官方 JSON，校验后原子写入 `data/ccfddl_snapshot.json`；失败或取消保留旧快照。同一进程内 60 秒内重复同步使用刚同步的快照。同步任务与文献任务共用队列，可以取消。
- 默认显示北京时间，可切换 UTC、美国太平洋时间或原始时区。AoE 为 UTC−12，PT 按美国太平洋夏令时规则转换；倒计时每秒更新。TBD 保持待公布，未支持 / 有歧义的时刻保留原文供核对，不推算为确定日期。
- 多轮截稿保留全部轮次，可展开“所有轮次与时间节点”；卡片优先显示所选节点的下一轮时间。直接进入某会截止日期时，也能查看其历年记录。
- 会议官网与仓库原始文件都有链接，“论文筛选”可将已匹配会议带回 arXiv / Scholar 检索。

这次快照中 **329 个会议匹配当前 CCF 2026 目录，45 个未匹配**；未匹配条目仍可浏览，标记为“未收录 / 未匹配”，不将上游等级冒充当前官方等级。上游 AsiaCCS 为 C，当前核验目录为 A，所以界面按 **CCF 2026 的 A** 展示。部分当前目录会议没有 CCFDDL 时间线，目录入口会显示空结果提示。

截止时间来源于社区维护，程序没有逐一向所有主办方核验。上游约定：官方仅给日期时默认 `23:59:59`，未给时区时默认 `AoE`；页面明确展示该规则并提供原始出处。投稿前以会议官网最终公告为准。日期未公布时不沿用去年的日期估算。

### 命令行与接口

```powershell
# 查看 AI 机器学习方向的目录
python -X utf8 cli.py --list-conferences --major AI --subfield AI:ml

# 查看视觉方向 A/B 会议的 2027 年截稿信息
python -X utf8 cli.py --deadlines --major AI --subfield AI:vision --ccf A B --deadline-year 2027

# 同步，并查看安全方向；每页最多 100 条，下一页添加 --deadline-page 2
python -X utf8 cli.py --refresh-deadlines --major SC

# 查看已经截止的往期会议
python -X utf8 cli.py --deadlines --deadline-status past --keywords ICLR
```

- `GET /api/config`：包含带 `taxonomy` 的 CCF 目录，以及截止日期快照信息。
- `GET /api/deadlines`：支持 `major`、`subfield`、`venue`（当前目录稳定 ID）、`rank`（A/B/C/N，逗号分隔可多选）、`q`、`year`、`stage`、`status`、`page`、`limit`。返回该页会议、完整轮次、UTC 时间、快照来源和年份选项；网页读取始终使用本地快照。
- `POST /api/deadlines/refresh`：带本机会话 Token，返回后台任务 ID；任务状态和取消接口与文献任务共用。

实现位于 `arxiv_demo/deadlines.py` 与 `arxiv_demo/navigation.py`。上游 MIT 许可随附 `LICENSE-CCFDDL.txt`，数据归属及来源见 `THIRD_PARTY.md`。
