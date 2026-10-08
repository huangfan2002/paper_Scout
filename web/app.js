"use strict";

const $ = (id) => document.getElementById(id);
const state = { config: null, result: null, selected: new Set(), conferences: [], page: 1,
  perPage: 15, searchJob: null, downloadJob: null, pollTimers: new Map(), currentSource: "",
  directions: {catalogue:{major:"AI", subfield:""}, deadline:{major:"", subfield:""}},
  ddlPage:1, ddlRequest:0, ddlData:null, ddlVenue:"", ddlJob:null };
const statusNames = { verified: "DBLP 主会记录", author_reported: "作者录用声明", publication_record: "Scholar 出版记录", mentioned: "仅会议提及", excluded: "非主会 / 投稿信息", unknown: "会议信息未知" };
const html = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const date = (value) => value ? String(value).slice(0, 10) : "—";
const bytes = (value) => value > 1024 * 1024 ? (value / 1024 / 1024).toFixed(1) + " MB" : Math.ceil(value / 1024) + " KB";
const external = (value) => {
  try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? value : "#"; }
  catch (_) { return "#"; }
};
const isScholar = (p) => p.source === "google_scholar";
const canDownload = (p) => !isScholar(p) || !!p.arxiv_id;
const sourceName = (value) => ({arxiv:"arXiv", sample:"arXiv 离线样例", google_scholar:"Google Scholar", scholar_import:"Scholar 导入", scholar_sample:"Scholar 文档样例"}[value] || value);

function updateScholarLink() {
  const prefix = {all:"", title:"intitle:", author:"author:"}[$("search-field").value] || "";
  const words = ($("keywords").value.match(/"[^"]*"|\S+/g) || []).map((x) => prefix + '"' + x.replace(/["\\:()\[\]{}]/g, " ").trim() + '"');
  const params = new URLSearchParams({q:words.join($("match-mode").value === "any" ? " OR " : " "), hl:$("scholar-language").value, as_sdt:"0", as_vis:"1"});
  if ($("year-from").value) params.set("as_ylo", $("year-from").value);
  if ($("year-to").value) params.set("as_yhi", $("year-to").value);
  $("scholar-open").href = "https://scholar.google.com/scholar?" + params;
}

function searchAvailability() {
  const unavailable = $("source").value === "google_scholar" && !state.config?.scholar?.configured;
  $("search-button").disabled = !!state.searchJob || unavailable;
  $("search-button").title = unavailable ? "未配置 Key，可打开 Google 学术并导入保存的结果页" : "";
  $("sample-button").disabled = !!state.searchJob;
}

function updateSource() {
  const source = $("source").value;
  const scholar = source === "google_scholar";
  if (state.currentSource !== source) {
    $("sort-by").innerHTML = scholar ? '<option value="relevance">相关性 · Scholar</option>' : '<option value="submittedDate">首次提交日期</option><option value="lastUpdatedDate">最近更新日期</option><option value="relevance">相关性</option>';
    $("sort-order").value = "descending";
    if (scholar && $("search-field").value === "abstract") $("search-field").value = "all";
    state.currentSource = source;
  }
  $("search-field").querySelector('option[value="abstract"]').disabled = scholar;
  $("sort-order").disabled = scholar;
  $("arxiv-date-field").hidden = scholar; $("scholar-year-field").hidden = !scholar;
  $("categories-field").hidden = scholar; $("scholar-language-field").hidden = !scholar;
  $("scholar-tools").hidden = !scholar;
  $("scan-limit").max = scholar ? "1000" : "2000";
  if (scholar && Number($("scan-limit").value) > 1000) $("scan-limit").value = "1000";
  $("source-status").textContent = scholar ? (state.config?.scholar?.configured ? "自动检索已配置" : "无需 Key：浏览器检索 + 结果导入") : "无需 API Key";
  $("scholar-status").textContent = state.config?.scholar?.configured ? "已配置自动检索接口，也可直接打开 Scholar 或导入保存的结果。" : (state.config?.scholar?.message || "尚未配置自动检索 Key。先打开 Google 学术，按条件检索并保存结果页，再导入这里。");
  $("sample-button").textContent = scholar ? "体验 Scholar 文档样例" : "体验离线样例";
  searchAvailability(); updateScholarLink();
}

let toastTimer;
function toast(message, error = false) {
  $("toast").textContent = message;
  $("toast").classList.toggle("error", error);
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 8000 : 4500);
}

async function api(path, body) {
  const options = { headers: {} };
  if (body !== undefined) {
    if (!state.config) throw new Error("配置尚未加载，请稍后再试。");
    options.method = "POST";
    options.headers = { "Content-Type": "application/json", "X-Session-Token": state.config.token };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let result;
  try { result = await response.json(); }
  catch (_) { throw new Error("服务返回了无法解析的响应，请检查服务是否仍在运行。"); }
  if (!response.ok) throw new Error(result.error || `请求失败 (${response.status})`);
  return result;
}

function run(action) {
  Promise.resolve().then(action).catch((error) => toast(error.message, true));
}

function rankBadge(ccf) {
  if (ccf.rank) return `<span class="badge badge-${ccf.rank.toLowerCase()}">CCF ${html(ccf.rank)} · ${html(ccf.conference)}</span>`;
  if (ccf.status === "mentioned") return `<span class="badge badge-candidate">CCF ${html(ccf.candidates[0]?.rank || "?")} 候选 · ${html(ccf.conference)} · 待核实</span>`;
  return `<span class="badge badge-muted">${html(statusNames[ccf.status])}</span>`;
}

function selectedTools() {
  const count = state.selected.size;
  const available = (state.result?.papers || []).filter((p) => state.selected.has(p.id) && canDownload(p)).length;
  $("selected-count").textContent = available;
  $("download-button").disabled = available === 0 || !!state.downloadJob;
  $("download-button").title = "批量下载支持可定位的 arXiv PDF；Scholar 其他来源请打开全文链接";
  $("export-button").disabled = !state.result || !state.result.papers.length;
  $("select-all").disabled = !state.result || !state.result.papers.length;
  const total = state.result?.papers.length || 0;
  $("select-all").checked = total > 0 && count === total;
  $("select-all").indeterminate = count > 0 && count < total;
  $("export-button").textContent = count ? `导出所选 ${count}` : "导出全部";
}

function renderResults() {
  if (!state.result) return;
  const { papers } = state.result;
  $("result-count").textContent = papers.length;
  const total = state.result.total_remote;
  const scholar = ["google_scholar", "scholar_import", "scholar_sample"].includes(state.result.source);
  const prefix = ["sample", "scholar_sample"].includes(state.result.source) ? `离线样例 ${date(state.result.snapshot_at)} · ` : "";
  $("result-description").textContent = `${prefix}${sourceName(state.result.source)} · 已扫描 ${state.result.scanned} 条候选${total !== null && total !== undefined ? ` / ${scholar ? "估计命中" : "命中"} ${total.toLocaleString()}` : ""} · 当前保留 ${papers.length} 篇`;
  $("data-source-note").textContent = scholar ? "Google Scholar · 出版摘要与引用信息保留在详情中" : "数据来自 arXiv 开放接口";
  $("cache-note").textContent = scholar ? "API 元数据缓存 24 小时 · 导入文件在本机解析" : "arXiv 请求间隔 ≥ 3 秒 · 元数据缓存 24 小时";
  $("warning-box").hidden = !state.result.warnings?.length;
  $("warning-box").innerHTML = (state.result.warnings || []).map((x) => `<div>${html(x)}</div>`).join("");
  $("query-details").hidden = false;
  $("query-text").textContent = JSON.stringify({ query: state.result.query, options: state.result.options, catalogue: state.result.catalogue.version,
    scan_limited: state.result.scan_limited, target_reached: state.result.target_reached, cache_pages: state.result.cache_pages }, null, 2);
  if (!papers.length) {
    $("result-list").innerHTML = `<div class="empty-state"><h3>没有找到满足条件的文献</h3><p>可以放宽日期或会议筛选，提高候选扫描上限，<br>或在“更多筛选”中选择“含会议提及”。</p></div>`;
  } else {
    const start = (state.page - 1) * state.perPage;
    $("result-list").innerHTML = papers.slice(start, start + state.perPage).map((p) => {
      const authorText = p.authors.slice(0, 4).join(", ") + (p.authors.length > 4 ? ` 等 ${p.authors.length} 位作者` : "");
      const scholar = isScholar(p);
      const time = scholar ? `<span>发表年份 ${html(p.publication_year || "未知")}</span><span class="source-count">引用 ${p.citations ?? "未知"}</span>${p.versions_count !== null ? `<span>${p.versions_count} 个版本</span>` : ""}` : `<span>首次提交 ${date(p.published)}</span><span>更新 ${date(p.updated)}</span>`;
      const actions = (p.abstract_url ? `<a href="${html(external(p.abstract_url))}" target="_blank" rel="noopener noreferrer">${scholar ? "出版页面" : "arXiv"} ↗</a>` : "") +
        (scholar ? `<a href="${html(external(p.scholar_url))}" target="_blank" rel="noopener noreferrer">Google Scholar ↗</a>` : "") +
        (canDownload(p) ? `<button type="button" class="text-button" data-download="${html(p.id)}">下载 PDF ↓</button>` : p.pdf_url ? `<a href="${html(external(p.pdf_url))}" target="_blank" rel="noopener noreferrer">打开 PDF ↗</a>` : "");
      return `<article class="paper-card"><label class="paper-select"><input type="checkbox" data-paper="${html(p.id)}" aria-label="选择 ${html(p.title)}" ${state.selected.has(p.id) ? "checked" : ""}></label><div class="paper-main"><div class="paper-topline"><span class="paper-id">${scholar ? "Google Scholar" : "arXiv:" + html(p.id)}</span>${rankBadge(p.ccf)}${p.ccf.rank ? `<span class="badge badge-muted">${html(statusNames[p.ccf.status])}</span>` : ""}<span class="paper-category">${html(p.primary_category)}</span></div><button class="paper-title" data-detail="${html(p.id)}">${html(p.title)}</button><p class="paper-authors">${html(authorText || "作者信息未提供")}</p><p class="paper-abstract">${html(p.abstract || (scholar ? p.publication_summary : ""))}</p><div class="paper-bottom"><span>${time}</span><div class="paper-links"><button type="button" class="text-button" data-detail="${html(p.id)}">${scholar ? "片段与证据" : "摘要与证据"}</button>${actions}</div></div></div></article>`;
    }).join("");
  }
  const pages = Math.ceil(papers.length / state.perPage);
  $("pagination").hidden = pages <= 1;
  $("page-indicator").textContent = `${state.page} / ${pages} 页`;
  $("previous-page").disabled = state.page <= 1;
  $("next-page").disabled = state.page >= pages;
  selectedTools();
}

function setResult(result) {
  state.result = result;
  state.selected.clear();
  state.page = 1;
  renderResults();
}

function showDetail(id) {
  const p = state.result?.papers.find((x) => x.id === id);
  if (!p) return;
  const scholar = isScholar(p);
  const evidence = p.ccf.candidates.length ? p.ccf.candidates.map((c) => `<div class="detail-evidence"><strong>CCF ${html(c.rank)} · ${html(c.conference)} · ${html(statusNames[c.status])}</strong><blockquote>${html(c.evidence)}</blockquote><p>${html(c.reason)}</p><p>来源：${html(c.source)} · 论文类型：${c.paper_type === "excluded" ? "已知非主会类型" : "尚未核实正式长文"} · <a href="${html(external(c.url || p.scholar_url))}" target="_blank" rel="noopener noreferrer">核对原记录 ↗</a></p></div>`).join("") : `<div class="detail-evidence">检索元数据中没有可匹配的 CCF 会议信息。此状态无法判断论文是否已被录用。</div>`;
  const metadata = scholar ? `<span>Google Scholar</span><span>发表年份 ${html(p.publication_year || "未知")}</span><span>引用 ${p.citations ?? "未知"}</span>` : `<span>arXiv:${html(p.id)}</span><span>${html(p.categories.join(" / "))}</span><span>首次提交 ${date(p.published)} UTC</span><span>更新 ${date(p.updated)} UTC</span>`;
  const links = [[p.abstract_url, scholar ? "出版页面" : "arXiv 原文"], [p.pdf_url, "打开远程 PDF"], [p.scholar_url, "Google Scholar"], [p.cited_by_url, "引用此文的论文"], [p.related_url, "相关论文"], [p.versions_url, "其他版本"]].filter(([url]) => url).map(([url,label]) => `<a href="${html(external(url))}" target="_blank" rel="noopener noreferrer">${label} ↗</a>`).join("");
  $("paper-detail").innerHTML = `<h2 class="detail-title">${html(p.title)}</h2><p class="detail-authors">${html(p.authors.join(", "))}</p><div class="detail-meta">${metadata}</div>${scholar ? '<p class="detail-abstract-label">检索片段（可能截断，不是完整摘要）</p>' : ""}<p class="detail-abstract">${html(p.abstract || "未提供")}</p>${evidence}<div class="detail-evidence"><strong>原始元数据</strong>${scholar ? `<p>Scholar 出版摘要：${html(p.publication_summary || "未提供")}</p>` : `<p>Comments：${html(p.comment || "未填写")}</p><p>Journal reference：${html(p.journal_ref || "未填写")}</p>`}<p>DOI：${html(p.doi || "未填写")}</p><p>目录版本：${html(p.ccf.catalogue_version)}；等级按此版本映射。</p></div><div class="detail-links">${links}</div>`;
  $("paper-dialog").showModal();
}

function filterHint() {
  const ranks = [...document.querySelectorAll('input[name="ccf"]:checked')].map((x) => x.value === "unknown" ? "未知" : `CCF ${x.value}`);
  $("rank-hint").textContent = ranks.length ? `筛选 ${ranks.join(" + ")} · 未知与非主会信息单独处理` : "未选择等级时检索全部文献";
}

function collectOptions() {
  const scholar = $("source").value === "google_scholar";
  return { source:$("source").value, keywords: $("keywords").value.trim(), field: $("search-field").value, match: $("match-mode").value,
    date_from: scholar ? "" : $("date-from").value, date_to: scholar ? "" : $("date-to").value,
    year_from:scholar ? $("year-from").value : "", year_to:scholar ? $("year-to").value : "", scholar_language:$("scholar-language").value,
    categories: scholar ? [] : $("categories").value.split(/[\s,，]+/).filter(Boolean),
    ccf_ranks: [...document.querySelectorAll('input[name="ccf"]:checked')].map((x) => x.value),
    conferences: state.conferences.slice(), evidence: $("evidence").value,
    sort_by: $("sort-by").value, sort_order: $("sort-order").value,
    max_results: Number($("max-results").value), scan_limit: Number($("scan-limit").value),
    verify_dblp: $("verify-dblp").checked, dblp_limit: 30 };
}

function restoreOptions(options) {
  if (!options || !Object.keys(options).length) return;
  $("source").value = options.source || "arxiv"; updateSource();
  const mapping = { keywords: "keywords", field: "search-field", match: "match-mode", date_from: "date-from", date_to: "date-to",
    evidence: "evidence", sort_by: "sort-by", sort_order: "sort-order", max_results: "max-results", scan_limit: "scan-limit",
    year_from:"year-from", year_to:"year-to", scholar_language:"scholar-language" };
  for (const [key, id] of Object.entries(mapping)) if (options[key] !== undefined) $(id).value = options[key];
  $("categories").value = (options.categories || []).join(", ");
  $("verify-dblp").checked = !!options.verify_dblp;
  document.querySelectorAll('input[name="ccf"]').forEach((x) => { x.checked = (options.ccf_ranks || []).includes(x.value); });
  state.conferences = (options.conferences || []).filter((id) => state.config.catalogue.conferences.some((v) => v.id === id));
  renderVenueChips(); filterHint(); updateScholarLink();
}

function renderVenueChips() {
  $("venue-chips").innerHTML = state.conferences.map((id) => {
    const venue = state.config.catalogue.conferences.find((v) => v.id === id);
    return `<span class="chip">${html(venue.acronym)} <button type="button" data-remove-venue="${html(id)}" aria-label="移除 ${html(venue.acronym)}">×</button></span>`;
  }).join("");
}

function addVenue() {
  const name = $("venue-input").value.trim().toLocaleLowerCase();
  const venue = state.config.catalogue.conferences.find((v) => [v.acronym, v.name, ...(v.aliases || [])].some((x) => x.toLocaleLowerCase() === name));
  if (!venue) throw new Error("请从会议目录中选择有效会议简称。");
  if (!state.conferences.includes(venue.id)) state.conferences.push(venue.id);
  $("venue-input").value = "";
  renderVenueChips();
}

function taskDisplay(job) {
  // Prefer showing a running search; downloads still poll in the background.
  if (state.searchJob && job.kind === "download") return;
  $("task-panel").hidden = false;
  $("task-title").textContent = job.kind === "search" ? "检索任务" : "下载任务";
  $("task-message").textContent = job.error || job.message;
  $("task-progress").value = job.progress || 0;
  $("task-detail").textContent = job.kind === "search" ? `已扫描 ${job.scanned} 条 · 已保留 ${job.matched} 篇 · 命中缓存 ${job.cache_pages} 页` : `已处理 ${job.scanned} / ${job.target} 篇 · 成功 ${job.matched} 篇`;
  const active = ["pending", "running"].includes(job.status);
  $("cancel-task").hidden = !active;
  $("cancel-task").dataset.job = job.id;
  $("task-spinner").hidden = !active;
}

async function pollJob(id) {
  try {
    const job = await api(`/api/jobs/${id}`);
    taskDisplay(job);
    if (["pending", "running"].includes(job.status)) {
      state.pollTimers.set(id, setTimeout(() => { pollJob(id); }, 1000));
      return;
    }
    state.pollTimers.delete(id);
    if (job.kind === "search") {
      state.searchJob = null;
      searchAvailability();
      if (job.status === "completed") { setResult(job.result); toast(`检索完成，保留 ${job.result.papers.length} 篇文献。`); }
    } else {
      state.downloadJob = null;
      if (job.status === "completed") {
        toast(`下载完成：成功 ${job.result.succeeded} 篇，失败 ${job.result.failed} 篇。`, job.result.failed > 0);
        if (job.warnings.length) { $("warning-box").innerHTML = job.warnings.map((x) => `<div>${html(x)}</div>`).join(""); $("warning-box").hidden = false; }
        await loadDownloads();
      }
    }
    selectedTools();
    if (job.status === "failed") toast(job.error || "任务未完成。", true);
    if (job.status === "cancelled") toast("任务已取消，已完成的 PDF 保留。");
  } catch (error) {
    // Stop polling after a broken local connection; don't keep spamming it.
    state.pollTimers.delete(id);
    if (state.searchJob === id) { state.searchJob = null; searchAvailability(); }
    if (state.downloadJob === id) state.downloadJob = null;
    $("task-message").textContent = error.message;
    $("task-spinner").hidden = true; $("cancel-task").hidden = true;
    selectedTools(); toast(error.message, true);
  }
}

async function search() {
  if (state.searchJob) return;
  const job = await api("/api/search", collectOptions());
  state.searchJob = job.id;
  $("search-button").disabled = true; $("sample-button").disabled = true;
  $("warning-box").hidden = true;
  taskDisplay(job); pollJob(job.id);
}

async function sample() {
  if (state.searchJob) throw new Error("请等待检索完成或先取消任务。");
  const result = await api("/api/sample", {source:$("source").value});
  setResult(result); $("task-panel").hidden = true;
  toast($("source").value === "google_scholar" ? "已加载 Scholar 历史文档样例；真实结果可从 Scholar 页面保存后导入。" : "已加载离线样例；实时检索请点击“检索文献”。");
}

async function download(ids) {
  if (state.downloadJob) throw new Error("已有下载任务，请等待完成。");
  const requested = ids.length;
  ids = ids.filter((id) => state.result.papers.some((p) => p.id === id && canDownload(p)));
  if (!ids.length) throw new Error("所选论文没有可定位的 arXiv PDF，请打开出版页面或其他版本。");
  if (ids.length < requested) toast(`本次下载 ${ids.length} 篇 arXiv PDF；其他来源可从详情中的全文链接获取。`);
  if (ids.length > 100) throw new Error("单批最多下载 100 篇，请减少勾选数量。");
  const job = await api("/api/download", { ids, search_id: state.result.id });
  state.downloadJob = job.id; selectedTools(); taskDisplay(job); pollJob(job.id);
}

async function loadDownloads() {
  const data = await api("/api/downloads");
  $("download-count").textContent = data.files.length;
  $("download-directory").textContent = data.directory;
  $("download-list").innerHTML = data.files.length ? data.files.map((file) => `<article class="paper-card"><div class="paper-main"><div class="paper-topline"><span class="paper-id">${html(file.filename)}</span><span class="badge badge-a">已保存</span></div><h3 class="paper-title">${html(file.title)}</h3><div class="paper-bottom"><span class="file-size">${bytes(file.bytes)}</span><a href="/downloads/${encodeURIComponent(file.filename)}" target="_blank" rel="noopener noreferrer">打开本地 PDF ↗</a></div></div></article>`).join("") : `<div class="empty-state"><h3>文库还没有论文</h3><p>在检索结果中勾选论文，点击“下载所选”，PDF 会保存到上方目录。</p></div>`;
}

async function loadHistory() {
  const data = await api("/api/history");
  $("history-list").innerHTML = data.items.length ? data.items.map((item) => `<article class="history-card"><div><h3>${html(item.options.keywords || "离线样例")} <span class="badge badge-muted">${html(sourceName(item.source))}</span></h3><p>${date(item.created_at)} · ${item.count} 篇${item.options.ccf_ranks?.length ? " · " + html(item.options.ccf_ranks.join(" / ")) : ""}</p><p>${html(item.query)}</p></div><button class="button secondary compact" data-history="${html(item.id)}">加载结果 →</button></article>`).join("") : `<div class="empty-state"><h3>还没有检索历史</h3><p>完成一次检索后，结果会自动保存在这里。</p></div>`;
}

function renderDirections(prefix) {
  const selected = state.directions[prefix];
  const majors = state.config.catalogue.taxonomy.majors;
  $(prefix + "-majors").innerHTML = majors.map((m) => `<button type="button" class="major-option ${selected.major === m.id ? "selected" : ""}" data-direction="${prefix}" data-major="${html(m.id)}" aria-pressed="${selected.major === m.id}" title="${html(m.domain)}"><span class="major-code">${html(m.id)}</span><span>${html(m.name)}<small>${m.count} 个 CCF 会议</small></span></button>`).join("");
  const major = majors.find((m) => m.id === selected.major);
  $(prefix + "-subfields").innerHTML = major ? `<button type="button" class="subfield-option ${!selected.subfield ? "selected" : ""}" data-direction="${prefix}" data-subfield="" aria-pressed="${!selected.subfield}">全部小方向</button>` + major.subfields.map((s) => `<button type="button" class="subfield-option ${selected.subfield === s.id ? "selected" : ""}" data-direction="${prefix}" data-subfield="${html(s.id)}" aria-pressed="${selected.subfield === s.id}">${html(s.name)} <span>${s.count}</span></button>`).join("") + (prefix === "deadline" ? `<button type="button" class="subfield-option ${selected.subfield === major.id + ":unmapped" ? "selected" : ""}" data-direction="deadline" data-subfield="${html(major.id)}:unmapped">未匹配当前目录</button>` : "") : '<span class="muted">点击上方大方向，展开细分研究主题。</span>';
}

function catalogueRows() {
  const {major, subfield} = state.directions.catalogue;
  const search = $("catalogue-search").value.trim().toLocaleLowerCase();
  const rank = $("catalogue-rank").value;
  return state.config.catalogue.conferences.filter((v) => (!rank || v.rank === rank) && (!major || v.major_id === major) && (!subfield || v.subfield_id === subfield) &&
    (!search || [v.acronym, v.name, v.domain, v.subfield_name, ...(v.aliases || [])].join(" ").toLocaleLowerCase().includes(search)));
}

function renderCatalogue() {
  if (!state.config) return;
  const catalogue = state.config.catalogue;
  const rows = catalogueRows();
  renderDirections("catalogue");
  $("catalogue-description").textContent = `${catalogue.metadata.version} · 共 ${catalogue.conferences.length} 个会议 · A ${catalogue.counts.A} / B ${catalogue.counts.B} / C ${catalogue.counts.C}`;
  const direction = state.directions.catalogue;
  const major = catalogue.taxonomy.majors.find((m) => m.id === direction.major);
  const subfield = major?.subfields.find((s) => s.id === direction.subfield);
  $("catalogue-note").textContent = `会议目录 / ${major?.name || "全部方向"}${subfield ? " / " + subfield.name : ""} · 当前显示 ${rows.length} 个会议`;
  $("catalogue-use-direction").disabled = !rows.length;
  $("catalogue-body").innerHTML = catalogue.taxonomy.majors.map((m) => m.subfields.map((s) => {
    const group = rows.filter((v) => v.subfield_id === s.id);
    return group.length ? `<tr class="catalogue-group"><th colspan="4">${html(m.name)} <span>/</span> ${html(s.name)} <small>${group.length} 个会议</small></th></tr>` + group.map((v) => `<tr><td><span class="badge badge-${v.rank.toLowerCase()}">CCF ${v.rank}</span></td><td><strong>${html(v.acronym)}</strong></td><td>${html(v.name)}</td><td><div class="catalogue-actions"><button class="text-button" data-venue-papers="${html(v.id)}">论文筛选 →</button><button class="text-button" data-venue-deadlines="${html(v.id)}">截止日期 ↗</button></div></td></tr>`).join("") : "";
  }).join("")).join("") || '<tr><td colspan="4" class="catalogue-empty">没有满足条件的会议，可以调整方向、等级或关键词。</td></tr>';
}

async function useConferences(ids) {
  if (state.searchJob) throw new Error("请等待当前检索完成或先取消任务。");
  state.conferences = [...new Set(ids)];
  const picked = state.config.catalogue.conferences.filter((v) => state.conferences.includes(v.id));
  const sameMajor = picked.length && picked.every((v) => v.major_id === picked[0].major_id);
  $("search-major").value = sameMajor ? picked[0].major_id : "";
  searchDirectionOptions();
  if (sameMajor && picked.every((v) => v.subfield_id === picked[0].subfield_id)) $("search-subfield").value = picked[0].subfield_id;
  document.querySelectorAll('input[name="ccf"]').forEach((x) => { x.checked = false; });
  $("advanced-filters").hidden = false;
  $("advanced-toggle").setAttribute("aria-expanded", "true");
  renderVenueChips(); filterHint();
  await view("search");
  toast(`已选择 ${ids.length} 个会议。设置关键词与时间后即可检索，也可切换 Google Scholar。`);
}

function searchDirectionOptions() {
  const major = state.config.catalogue.taxonomy.majors.find((m) => m.id === $("search-major").value);
  $("search-subfield").innerHTML = '<option value="">全部小方向</option>' + (major?.subfields || []).map((s) => `<option value="${html(s.id)}">${html(s.name)} · ${s.count}</option>`).join("");
}

function deadlineParams() {
  return new URLSearchParams({major:state.directions.deadline.major, subfield:state.directions.deadline.subfield,
    venue:state.ddlVenue, q:$("deadline-q").value.trim(), rank:$("deadline-rank").value,
    stage:$("deadline-stage").value, status:$("deadline-status").value, year:$("deadline-year").value,
    page:state.ddlPage, limit:24});
}

async function loadDeadlines(reset = false) {
  if (reset) state.ddlPage = 1;
  const request = ++state.ddlRequest;
  $("deadline-description").textContent = "正在读取本地会议快照…";
  const data = await api("/api/deadlines?" + deadlineParams());
  if (request !== state.ddlRequest) return;
  const selectedYear = $("deadline-year").value;
  $("deadline-year").innerHTML = '<option value="">全部年份</option>' + data.years.map((year) => `<option>${year}</option>`).join("");
  $("deadline-year").value = selectedYear;
  state.ddlData = data;
  renderDeadlines();
}

function clockText(value, zone) {
  if (!value) return "";
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {timeZone:zone, year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit", hourCycle:"h23"}).formatToParts(new Date(value)).map((p) => [p.type,p.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

function eventClock(event) {
  if (!event) return "尚未公布此节点";
  if (!event.utc) return event.raw.toUpperCase() === "TBD" ? "TBD · 待公布" : `${event.raw} ${event.timezone} · 待核对`;
  return $("deadline-timezone").value === "source" ? `${event.raw} ${event.timezone}` : clockText(event.utc, $("deadline-timezone").value);
}

function countdown(value) {
  const seconds = Math.floor((new Date(value).getTime() - Date.now()) / 1000);
  if (seconds < 0) return "已截止";
  const pad = (n) => String(n).padStart(2,"0");
  return `${Math.floor(seconds / 86400)} 天 ${pad(Math.floor(seconds / 3600) % 24)}:${pad(Math.floor(seconds / 60) % 60)}:${pad(seconds % 60)}`;
}

function updateCountdowns() {
  document.querySelectorAll("[data-countdown]").forEach((node) => {
    node.textContent = countdown(node.dataset.countdown);
    node.classList.toggle("elapsed", new Date(node.dataset.countdown) < new Date());
  });
}

function renderDeadlines() {
  renderDirections("deadline");
  if (!state.ddlData) return;
  const data = state.ddlData, stage = data.stage;
  const names = {deadline:"正文投稿", abstract_deadline:"摘要注册", rebuttal_deadline:"作者回复", decision_deadline:"录用通知"};
  const zone = $("deadline-timezone");
  $("deadline-zone-label").textContent = zone.selectedOptions[0].textContent;
  $("deadline-count").textContent = data.total;
  $("deadline-description").textContent = `按${names[stage]}时间排序 · 共 ${data.total} 届会议 · 每页 ${data.limit} 条`;
  const venue = state.config.catalogue.conferences.find((v) => v.id === state.ddlVenue);
  $("deadline-venue-filter").hidden = !venue;
  $("deadline-venue-filter").innerHTML = venue ? `<span class="chip">限定会议：${html(venue.acronym)} · ${html(venue.major_name)} <button data-clear-deadline-venue="1" aria-label="清除限定会议">×</button></span>` : "";
  $("deadline-list").innerHTML = data.items.map((entry) => {
    const events = entry.timeline.flatMap((p) => p.events[stage] ? [p.events[stage]] : []);
    const primary = events.find((e) => e.utc === entry.next_utc && e.utc) || (entry.status === "tbd" ? events.find((e) => e.raw.toUpperCase() === "TBD") : null) || events.filter((e) => e.utc).sort((a,b) => b.utc.localeCompare(a.utc))[0] || events[0];
    const pending = !primary?.utc;
    const remaining = primary?.utc ? new Date(primary.utc).getTime() - Date.now() : null;
    const urgent = remaining !== null && remaining >= 0 && remaining < 7 * 86400000;
    const rounds = entry.timeline.map((point) => `<div class="deadline-round"><strong>第 ${point.round} 轮${point.comment ? " · " + html(point.comment) : ""}</strong>${Object.entries(point.events).map(([key,event]) => `<div class="timeline-event ${key === stage ? "focused" : ""}"><span>${names[key]}</span><time title="原始：${html(event.raw)} ${html(event.timezone)}${event.warning ? " · " + html(event.warning) : ""}">${html(eventClock(event))}</time></div>`).join("")}</div>`).join("");
    return `<article class="deadline-card ${urgent ? "urgent" : ""}"><div class="deadline-card-top"><span class="badge ${entry.ccf_rank ? "badge-" + entry.ccf_rank.toLowerCase() : "badge-muted"}">${entry.ccf_rank ? "CCF " + entry.ccf_rank : "未收录 / 未匹配"}</span><span class="muted">${html(entry.major_id)} / ${html(entry.subfield_name)}</span></div><h3>${html(entry.title)} <span>${entry.year}</span></h3><p class="deadline-fullname">${html(entry.description)}</p><div class="deadline-primary"><span class="deadline-stage-label">${names[stage]}${entry.next_utc ? " · 下一轮" : ""}</span><time>${html(eventClock(primary))}</time><span class="countdown ${entry.status === "past" ? "elapsed" : ""}" ${primary?.utc ? `data-countdown="${html(primary.utc)}"` : ""}>${pending ? (entry.status === "tbd" ? "日期待公布" : "尚无可换算日期") : countdown(primary.utc)}</span><small>原始时区：${html(entry.timezone)}${primary?.utc ? " · " + html(primary.raw) : ""}</small></div><p class="deadline-place">${html(entry.date || "会议日期待公布")}<br>${html(entry.place || "地点待公布")}</p><details class="deadline-timeline"><summary>所有轮次与时间节点 <span>${entry.timeline.length} 轮</span></summary>${rounds}</details><div class="deadline-links">${entry.url ? `<a href="${html(external(entry.url))}" target="_blank" rel="noopener noreferrer">会议官网 ↗</a>` : ""}<a href="${html(external(entry.source_url))}" target="_blank" rel="noopener noreferrer">数据来源 ↗</a>${entry.venue_id ? `<button class="text-button" data-venue-papers="${html(entry.venue_id)}">论文筛选 →</button>` : ""}</div></article>`;
  }).join("") || '<div class="empty-state"><h3>当前条件下没有会议时间线</h3><p>可以调整研究方向、等级与时间节点，或选择“全部（含往期）”。部分目录会议尚未被 CCFDDL 收录。</p></div>';
  const pages = Math.ceil(data.total / data.limit);
  $("deadline-pagination").hidden = pages <= 1;
  $("deadline-page").textContent = `${data.page} / ${pages} 页`;
  $("deadline-previous").disabled = data.page <= 1;
  $("deadline-next").disabled = data.page >= pages;
  const meta = data.metadata;
  $("deadline-source-note").textContent = `${meta.cache === "updated" ? "本地同步快照" : "随附离线快照"} · 更新于 ${clockText(meta.fetched_at,"Asia/Shanghai")} 北京时间 · ${meta.venue_count} 个会议 / ${meta.edition_count} 条届次 · ${meta.matched_venues} 个已匹配 CCF 2026，${meta.unmatched_venues} 个未匹配。${meta.rank_differences.length ? "等级差异按 CCF 2026 目录显示。" : ""}${meta.warning || ""}`;
  updateCountdowns();
}

async function pollDeadlineSync(id) {
  try {
    const job = await api(`/api/jobs/${id}`);
    $("deadline-sync-message").textContent = job.error || `${job.message} ${job.progress || 0}%`;
    if (["pending","running"].includes(job.status)) { setTimeout(() => pollDeadlineSync(id),1000); return; }
    state.ddlJob = null; $("refresh-deadlines").disabled = false; $("cancel-deadline-sync").hidden = true;
    if (job.status === "completed") {
      state.config.deadlines = job.result;
      $("deadline-sync-panel").hidden = true;
      await loadDeadlines(); toast(`已同步 ${job.result.venue_count} 个会议、${job.result.edition_count} 条届次。`);
    } else toast(job.error || "同步已取消，保留已有快照。",job.status === "failed");
  } catch (error) {
    state.ddlJob = null; $("refresh-deadlines").disabled = false; $("cancel-deadline-sync").hidden = true;
    $("deadline-sync-message").textContent = error.message; toast(error.message,true);
  }
}

async function refreshDeadlines() {
  if (state.ddlJob) return;
  $("refresh-deadlines").disabled = true;
  try {
    const job = await api("/api/deadlines/refresh",{});
    state.ddlJob = job.id;
    $("deadline-sync-panel").hidden = false; $("cancel-deadline-sync").hidden = false;
    $("deadline-sync-message").textContent = "等待同步任务…";
    pollDeadlineSync(job.id);
  } catch (error) { $("refresh-deadlines").disabled = false; throw error; }
}

async function view(name) {
  if (!["search", "downloads", "history", "catalogue", "deadlines"].includes(name)) return;
  document.querySelectorAll(".view").forEach((x) => { x.hidden = x.id !== "view-" + name; });
  document.querySelectorAll(".nav-item").forEach((x) => x.classList.toggle("active", x.dataset.view === name));
  $("breadcrumb").textContent = "工作空间 / " + ({ search: "文献检索", downloads: "下载文库", history: "检索历史", catalogue: "CCF 会议目录", deadlines:"会议截止日期" }[name]);
  if (name === "downloads") await loadDownloads();
  if (name === "history") await loadHistory();
  if (name === "catalogue") renderCatalogue();
  if (name === "deadlines") await loadDeadlines();
}

document.addEventListener("click", (event) => {
  const target = event.target.closest("button, a[data-view]");
  if (!target) return;
  if (target.dataset.view) run(() => view(target.dataset.view));
  if (target.dataset.keyword) { $("keywords").value = target.dataset.keyword; $("keywords").focus(); updateScholarLink(); }
  if (target.dataset.detail) showDetail(target.dataset.detail);
  if (target.dataset.download) run(() => download([target.dataset.download]));
  if (target.dataset.removeVenue) { state.conferences = state.conferences.filter((id) => id !== target.dataset.removeVenue); renderVenueChips(); }
  if (target.dataset.direction) run(async () => {
    const prefix = target.dataset.direction;
    if ("major" in target.dataset) { state.directions[prefix].major = target.dataset.major; state.directions[prefix].subfield = ""; }
    else state.directions[prefix].subfield = target.dataset.subfield;
    if (prefix === "catalogue") renderCatalogue();
    else { state.ddlVenue = ""; renderDirections("deadline"); await loadDeadlines(true); }
  });
  if (target.dataset.venuePapers) run(() => useConferences([target.dataset.venuePapers]));
  if (target.dataset.venueDeadlines) run(async () => {
    const venue = state.config.catalogue.conferences.find((v) => v.id === target.dataset.venueDeadlines);
    state.ddlVenue = venue.id; state.directions.deadline = {major:venue.major_id, subfield:""}; state.ddlPage = 1;
    $("deadline-q").value = ""; $("deadline-year").value = ""; $("deadline-rank").value = ""; $("deadline-status").value = "all"; $("deadline-stage").value = "deadline";
    await view("deadlines");
  });
  if (target.dataset.clearDeadlineVenue) run(async () => { state.ddlVenue = ""; await loadDeadlines(true); });
  if (target.dataset.history) run(async () => {
    if (state.searchJob) throw new Error("请等待当前检索完成或先取消任务。");
    const result = await api(`/api/results/${target.dataset.history}`);
    restoreOptions(result.options); setResult(result); await view("search");
    $("task-panel").hidden = true;
  });
});
$("search-form").addEventListener("submit", (event) => { event.preventDefault(); run(search); });
$("source").addEventListener("change", updateSource);
["keywords", "search-field", "match-mode", "year-from", "year-to", "scholar-language"].forEach((id) => $(id).addEventListener("input", updateScholarLink));
$("scholar-import").addEventListener("click", () => { if (state.searchJob) return toast("请等待当前检索完成。",true); $("import-file").click(); });
$("import-file").addEventListener("change", () => run(async () => {
  const file = $("import-file").files[0];
  if (!file) return;
  try {
    if (file.size > 1024 * 1024) throw new Error("请选择 1 MB 内的 UTF-8 HTML / JSON 文件。");
    const result = await api("/api/scholar/import", {content:await file.text(), options:collectOptions()});
    setResult(result); $("task-panel").hidden = true; toast(`已导入并筛选 ${result.papers.length} 篇 Scholar 文献。`);
  } finally { $("import-file").value = ""; }
}));
$("sample-button").addEventListener("click", () => run(sample));
$("empty-sample").addEventListener("click", () => run(sample));
$("advanced-toggle").addEventListener("click", () => { const open = $("advanced-filters").hidden; $("advanced-filters").hidden = !open; $("advanced-toggle").setAttribute("aria-expanded", String(open)); });
document.querySelectorAll('input[name="ccf"]').forEach((x) => x.addEventListener("change", filterHint));
$("add-venue").addEventListener("click", () => run(addVenue));
$("venue-input").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); run(addVenue); } });
$("select-all").addEventListener("change", () => { state.selected = $("select-all").checked ? new Set(state.result.papers.map((p) => p.id)) : new Set(); renderResults(); });
$("result-list").addEventListener("change", (event) => { if (event.target.dataset.paper) { const id = event.target.dataset.paper; event.target.checked ? state.selected.add(id) : state.selected.delete(id); selectedTools(); } });
$("download-button").addEventListener("click", () => run(() => download([...state.selected])));
$("export-button").addEventListener("click", () => {
  if (!state.result) return;
  const params = new URLSearchParams({ search_id: state.result.id, format: $("export-format").value });
  if (state.selected.size) params.set("ids", [...state.selected].join(","));
  const anchor = document.createElement("a"); anchor.href = "/api/export?" + params; anchor.download = "";
  document.body.appendChild(anchor); anchor.click(); anchor.remove();
});
$("cancel-task").addEventListener("click", () => run(async () => { await api(`/api/jobs/${$("cancel-task").dataset.job}/cancel`, {}); toast("已请求取消；正在传输的响应会在超时或下一检查点结束。"); }));
$("previous-page").addEventListener("click", () => { state.page--; renderResults(); });
$("next-page").addEventListener("click", () => { state.page++; renderResults(); });
$("close-dialog").addEventListener("click", () => $("paper-dialog").close());
$("paper-dialog").addEventListener("click", (event) => { if (event.target === $("paper-dialog")) $("paper-dialog").close(); });
$("refresh-downloads").addEventListener("click", () => run(loadDownloads));
["catalogue-search", "catalogue-rank"].forEach((id) => $(id).addEventListener("input", renderCatalogue));
$("catalogue-use-direction").addEventListener("click", () => run(() => useConferences(catalogueRows().map((v) => v.id))));
$("search-major").addEventListener("change", searchDirectionOptions);
$("use-search-direction").addEventListener("click", () => run(async () => {
  const major = $("search-major").value, subfield = $("search-subfield").value;
  if (!major) throw new Error("请先选择大方向。");
  await useConferences(state.config.catalogue.conferences.filter((v) => v.major_id === major && (!subfield || v.subfield_id === subfield)).map((v) => v.id));
}));
["deadline-rank","deadline-status","deadline-stage","deadline-year"].forEach((id) => $(id).addEventListener("change",() => run(() => loadDeadlines(true))));
let deadlineInputTimer;
$("deadline-q").addEventListener("input", () => { clearTimeout(deadlineInputTimer); deadlineInputTimer = setTimeout(() => run(() => loadDeadlines(true)),300); });
$("deadline-timezone").addEventListener("change",renderDeadlines);
$("deadline-previous").addEventListener("click",() => run(async () => { state.ddlPage--; await loadDeadlines(); }));
$("deadline-next").addEventListener("click",() => run(async () => { state.ddlPage++; await loadDeadlines(); }));
$("refresh-deadlines").addEventListener("click",() => run(refreshDeadlines));
$("cancel-deadline-sync").addEventListener("click",() => run(async () => { if (state.ddlJob) await api(`/api/jobs/${state.ddlJob}/cancel`,{}); }));
setInterval(() => { if (!$("view-deadlines").hidden) updateCountdowns(); },1000);

run(async () => {
  state.config = await api("/api/config");
  updateSource();
  const venues = state.config.catalogue.conferences;
  $("venue-suggestions").innerHTML = venues.map((v) => `<option value="${html(v.acronym)}">CCF ${html(v.rank)} · ${html(v.domain)}</option>`).join("");
  $("search-major").innerHTML += state.config.catalogue.taxonomy.majors.map((m) => `<option value="${html(m.id)}">${html(m.name)} · ${m.count}</option>`).join("");
  $("download-directory").textContent = state.config.download_dir;
  await loadDownloads(); renderCatalogue(); renderDirections("deadline");
});
