"""Background searches and downloads shared by the web UI and CLI."""

from __future__ import annotations

import copy
import json
import logging
import queue
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .client import ArxivClient, Cancelled, RemoteError, Transport, ID_PATTERN, check_cancel
from .conferences import Catalogue, DblpVerifier, classify, matches_filters
from .query import SearchOptions, ValidationError, build_query
from .storage import Store
from .scholar import ScholarClient, parse_import, scholar_query, scholar_url
from .deadlines import DeadlineClient

LOG = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).isoformat()


class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex
        self.cancel = threading.Event()
        self.lock = threading.RLock()
        self.data = {"id": self.id, "kind": kind, "status": "pending", "message": "等待任务队列…",
                     "created_at": now(), "finished_at": None, "scanned": 0, "matched": 0,
                     "total_remote": None, "cache_pages": 0, "progress": 0, "target": 0,
                     "warnings": [], "result": None, "error": None}

    def update(self, **values):
        with self.lock:
            self.data.update(values)

    def warn(self, message: str):
        with self.lock:
            if message not in self.data["warnings"]:
                self.data["warnings"].append(message)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.queue = queue.Queue()
        self.worker = threading.Thread(target=self._work, name="arxiv-worker", daemon=True)
        self.worker.start()

    def submit(self, kind: str, task):
        with self.lock:
            active = sum(j.data["status"] in {"pending", "running"} for j in self.jobs.values())
            if active >= 10:
                raise ValidationError("队列最多同时保留 10 个任务，请等待或取消现有任务。")
            if len(self.jobs) >= 100:
                finished = [key for key, value in self.jobs.items() if value.data["status"] not in {"pending", "running"}]
                for key in finished[:20]:
                    del self.jobs[key]
            job = Job(kind)
            self.jobs[job.id] = job
            self.queue.put((job, task))
        return job.snapshot()

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def _work(self):
        while True:
            job, task = self.queue.get()
            try:
                check_cancel(job.cancel)
                job.update(status="running")
                result = task(job)
                # A late cancel must not hide a fully persisted result.
                job.update(status="completed", result=result, finished_at=now(), message="任务已完成")
            except Cancelled:
                job.update(status="cancelled", finished_at=now(), message="同步已取消，保留已有会议快照" if job.data["kind"] == "deadlines" else "任务已取消；已完成的 PDF 保留")
            except (RemoteError, ValidationError) as exc:
                job.update(status="failed", error=str(exc), finished_at=now(), message="任务未完成")
            except Exception as exc:
                LOG.exception("Unexpected job error")
                job.update(status="failed", error=f"任务处理失败：{type(exc).__name__}: {exc}", finished_at=now(), message="任务未完成")
            finally:
                self.queue.task_done()


class Service:
    def __init__(self, root: Path, data_dir: Path | None = None, download_dir: Path | None = None):
        self.root = root
        self.data_dir = data_dir or root / "data"
        self.download_dir = download_dir or root / "downloads"
        self.store = Store(self.data_dir / "arxiv.sqlite3")
        self.catalogue = Catalogue(root / "config" / "ccf_conferences.json")
        self.transport = Transport(self.store)
        self.arxiv = ArxivClient(self.transport)
        self.dblp = DblpVerifier(self.transport)
        self.scholar = ScholarClient(self.store, root)
        self.deadlines = DeadlineClient(root, self.data_dir, self.catalogue)
        self.jobs = JobManager()

    def refresh_deadlines(self):
        return self.jobs.submit("deadlines", self.deadlines.refresh)

    def options(self, payload) -> SearchOptions:
        return SearchOptions.parse(payload, set(self.catalogue.by_id))

    def search(self, options: SearchOptions, job: Job) -> dict:
        if options.source == "google_scholar":
            return self.search_scholar(options, job)
        query = build_query(options)
        results = []
        seen = set()
        scanned = start = cache_pages = checks = 0
        total = None
        exhausted = False
        dblp_available = True
        filtered = bool(options.ccf_ranks or options.conferences)
        job.update(target=options.max_results, message="连接 arXiv 官方 API…")
        while scanned < options.scan_limit and len(results) < options.max_results:
            check_cancel(job.cancel)
            count = min(100, options.scan_limit - scanned)
            if not filtered:
                count = min(count, options.max_results - len(results))
            job.update(message=f"正在读取第 {start + 1}–{start + count} 条候选文献…")
            page, total, cached = self.arxiv.page(query, start, count, options.sort_by, options.sort_order, job.cancel)
            cache_pages += int(cached)
            job.update(total_remote=total, cache_pages=cache_pages)
            if not page:
                if start < total:
                    raise RemoteError("arXiv 返回了意外的空页，结果尚不完整。请稍后重试。")
                exhausted = True
                break
            newly_seen = 0
            for paper in page:
                check_cancel(job.cancel)
                scanned += 1
                if paper["base_id"] in seen:
                    continue
                seen.add(paper["base_id"])
                newly_seen += 1
                ccf = classify(paper, self.catalogue)
                if options.verify_dblp and dblp_available and not ccf["rank"]:
                    if checks < options.dblp_limit:
                        checks += 1
                        job.update(message=f"DBLP 补充核验：{paper['title'][:65]}…")
                        try:
                            record = self.dblp.verify(paper, job.cancel)
                            if record:
                                paper["dblp_record"] = record
                                ccf = classify(paper, self.catalogue, record)
                        except RemoteError as exc:
                            dblp_available = False
                            job.warn(f"DBLP 补充核验暂停：{exc} 其余结果使用 arXiv 元数据。")
                    else:
                        job.warn(f"已达 DBLP 本次核验上限 {options.dblp_limit} 篇；其他候选仅依据 arXiv 元数据。")
                paper["ccf"] = ccf
                if matches_filters(ccf, options):
                    results.append(paper)
                job.update(scanned=scanned, matched=len(results),
                           progress=min(99, int(max(len(results) / options.max_results, scanned / options.scan_limit) * 100)))
                if len(results) >= options.max_results:
                    break
            if not newly_seen and start < total:
                raise RemoteError("arXiv 连续分页没有返回新的论文，已停止以避免重复抓取。")
            start += len(page)
            exhausted = start >= total and scanned >= total
            if start >= total:
                break
            # Short pages do not imply exhaustion: arXiv may return fewer
            # entries than requested. Continue at the number actually returned.
        scan_limited = scanned >= options.scan_limit and not exhausted and len(results) < options.max_results
        if scan_limited:
            job.warn("已达到候选扫描上限，当前结果不是完整的会议筛选结果；可提高上限或缩小关键词/日期范围。")
        if filtered and not results:
            job.warn("没有找到满足筛选的文献。会议信息缺失、仍在投稿或非主会类型不会进入严格等级筛选。可尝试“含会议提及”。")
        result = {"id": job.id, "created_at": now(), "source": "arxiv", "query": query,
                  "options": options.to_dict(), "papers": results, "scanned": scanned,
                  "total_remote": total, "cache_pages": cache_pages, "dblp_checks": checks,
                  "exhausted": exhausted, "scan_limited": scan_limited,
                  "target_reached": len(results) >= options.max_results,
                  "catalogue": self.catalogue.metadata, "warnings": job.snapshot()["warnings"]}
        check_cancel(job.cancel)
        self.store.save_papers(results)
        self.store.save_search(job.id, result)
        job.update(progress=100)
        return result

    def submit_search(self, payload):
        options = self.options(payload)
        if options.source == "google_scholar":
            scholar_query(options)
            if not self.scholar.api_key():
                raise ValidationError("尚未配置 SerpApi Key。可以先打开 Google 学术，保存结果页并使用“导入结果”。")
        else:
            build_query(options)  # Validate before placing a task in the queue.
        return self.jobs.submit("search", lambda job: self.search(options, job))

    def search_scholar(self, options: SearchOptions, job: Job) -> dict:
        query = scholar_query(options)
        results, seen = [], set()
        scanned = start = cache_pages = checks = 0
        total = None
        exhausted = False
        dblp_available = True
        job.update(target=options.max_results, message="连接 Google Scholar 检索接口…")
        job.warn("Scholar 的发表时间精度为年份，摘要是检索片段；引用次数与总命中数是检索时的快照。")
        while scanned < options.scan_limit and len(results) < options.max_results:
            check_cancel(job.cancel)
            count = min(20, options.scan_limit - scanned)
            job.update(message=f"读取 Scholar 第 {start + 1}–{start + count} 条候选…")
            page, cached = self.scholar.page(options, start, count, job.cancel)
            papers, total = page["papers"], page["total"]
            cache_pages += int(cached)
            job.update(total_remote=total, cache_pages=cache_pages)
            if not papers:
                if page["has_next"]:
                    raise RemoteError("Scholar 返回空页却声明有下一页，结果不完整，请稍后重试。")
                exhausted = True
                break
            fresh = 0
            for paper in papers:
                check_cancel(job.cancel)
                scanned += 1
                if paper["base_id"] in seen:
                    continue
                seen.add(paper["base_id"])
                fresh += 1
                ccf = classify(paper, self.catalogue)
                if options.verify_dblp and dblp_available and not ccf["rank"] and checks < options.dblp_limit:
                    checks += 1
                    try:
                        record = self.dblp.verify(paper, job.cancel)
                        if record:
                            paper["dblp_record"] = record
                            ccf = classify(paper, self.catalogue, record)
                    except RemoteError as exc:
                        dblp_available = False
                        job.warn(f"DBLP 补充核验暂停：{exc} 其余结果使用 Scholar 出版摘要。")
                paper["ccf"] = ccf
                if matches_filters(ccf, options):
                    results.append(paper)
                job.update(scanned=scanned, matched=len(results),
                           progress=min(99, int(max(scanned / options.scan_limit, len(results) / options.max_results) * 100)))
                if len(results) >= options.max_results:
                    break
            if not fresh:
                raise RemoteError("Scholar 分页没有返回新条目，已停止以避免重复抓取。")
            start += len(papers)
            if not page["has_next"] and (total is None or start >= min(total, 1000) or len(papers) < count):
                exhausted = True
                break
        scan_limited = scanned >= options.scan_limit and not exhausted and len(results) < options.max_results
        if scan_limited:
            job.warn("已达到 Scholar 候选扫描上限；当前保留的论文不是全部会议筛选结果。")
        if not results and (options.ccf_ranks or options.conferences):
            job.warn("未找到满足会议筛选的条目；Scholar 出版摘要可能省略会议全称，请查看原始记录或放宽筛选。")
        result = {"id": job.id, "created_at": now(), "source": "google_scholar", "provider": "serpapi",
                  "query": query, "scholar_url": scholar_url(options), "options": options.to_dict(), "papers": results,
                  "scanned": scanned, "total_remote": total, "cache_pages": cache_pages, "dblp_checks": checks,
                  "exhausted": exhausted, "scan_limited": scan_limited, "target_reached": len(results) >= options.max_results,
                  "catalogue": self.catalogue.metadata, "warnings": job.snapshot()["warnings"]}
        check_cancel(job.cancel)
        self.store.save_papers(results)
        self.store.save_search(job.id, result)
        job.update(progress=100)
        return result

    def import_scholar(self, payload):
        if not isinstance(payload, dict):
            raise ValidationError("导入请求必须是 JSON 对象。")
        if payload.get("options") is not None and not isinstance(payload["options"], dict):
            raise ValidationError("导入筛选条件必须是 JSON 对象。")
        data = dict(payload.get("options") or {})
        data.update(source="google_scholar", categories=[], date_from="", date_to="", sort_by="relevance", sort_order="descending")
        data.setdefault("keywords", "已保存的 Scholar 结果")
        if not data["keywords"]:
            data["keywords"] = "已保存的 Scholar 结果"
        options = self.options(data)
        page = parse_import(payload.get("content"))
        papers, seen = [], set()
        unknown_year = 0
        for paper in page["papers"]:
            if paper["base_id"] in seen:
                continue
            seen.add(paper["base_id"])
            year = paper["publication_year"]
            if options.year_from or options.year_to:
                if not year:
                    unknown_year += 1
                    continue
                if (options.year_from and year < options.year_from) or (options.year_to and year > options.year_to):
                    continue
            paper["ccf"] = classify(paper, self.catalogue)
            if matches_filters(paper["ccf"], options) and len(papers) < options.max_results:
                papers.append(paper)
        warnings = ["这是导入的已保存 Scholar 结果，只应用本地年份和 CCF 筛选；关键词应先在 Scholar 页面检索。引用数不代表当前实时值。"]
        if unknown_year:
            warnings.append(f"{unknown_year} 条记录没有发表年份，未纳入本次年份筛选。")
        result = {"id": uuid.uuid4().hex, "created_at": now(), "source": "scholar_import", "query": "导入已保存的 Google Scholar 结果",
                  "options": options.to_dict(), "papers": papers, "scanned": len(page["papers"]), "total_remote": None,
                  "cache_pages": 0, "catalogue": self.catalogue.metadata, "warnings": warnings}
        self.store.save_papers(papers)
        self.store.save_search(result["id"], result)
        return result

    def scholar_sample(self):
        payload = json.loads((self.root / "examples" / "scholar_sample.json").read_text(encoding="utf-8"))
        result = self.import_scholar({"content": json.dumps(payload), "options": {"keywords": "biology"}})
        result.update(source="scholar_sample", query="Scholar 接口文档样例（离线）", snapshot_at="2019-09-26",
                      warnings=["这是 SerpApi 官方文档的历史示例条目，不执行实时 Scholar 检索；引用次数仅用于演示。"])
        self.store.save_search(result["id"], result)
        return result

    def sample(self) -> dict:
        sample = json.loads((self.root / "examples" / "sample_papers.json").read_text(encoding="utf-8"))
        papers = sample["papers"]
        for paper in papers:
            paper["ccf"] = classify(paper, self.catalogue)
        result = {"id": uuid.uuid4().hex, "created_at": now(), "source": "sample",
                  "query": "离线样例：真实 arXiv 元数据快照", "options": {}, "papers": papers,
                  "scanned": len(papers), "total_remote": len(papers), "cache_pages": 0,
                  "snapshot_at": sample["snapshot_at"], "catalogue": self.catalogue.metadata,
                  "warnings": ["这是随项目保存的离线元数据快照；不会执行实时检索。会议等级采用当前随附目录。"]}
        self.store.save_papers(papers)
        self.store.save_search(result["id"], result)
        return result

    def submit_download(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("ids"), list):
            raise ValidationError("请提供要下载的论文 ID 列表。")
        ids = payload["ids"]
        if not ids or len(ids) > 100 or any(not isinstance(x, str) for x in ids):
            raise ValidationError("每次请选择 1–100 篇论文。")
        ids = list(dict.fromkeys(ids))
        search_id = payload.get("search_id")
        if not isinstance(search_id, str):
            raise ValidationError("请提供有效的检索结果 ID。")
        result = self.store.search(search_id)
        if not result:
            raise ValidationError("检索结果不存在，请先检索或加载历史结果。")
        by_id = {p["id"]: p for p in result["papers"]}
        if set(ids) - set(by_id):
            raise ValidationError("所选论文不在这次检索结果中。")
        papers = [by_id[key] for key in ids]
        if any(p.get("source") == "google_scholar" and not p.get("arxiv_id") for p in papers):
            raise ValidationError("所选 Scholar 论文有非 arXiv 来源，请通过“PDF / 出版页面”或“其他版本”打开获取全文；批量下载支持可定位的 arXiv PDF。")
        return self.jobs.submit("download", lambda job: self.download(papers, job))

    def download(self, papers: list[dict], job: Job):
        outcomes = []
        self.download_dir.mkdir(parents=True, exist_ok=True)
        job.update(target=len(papers))
        for index, paper in enumerate(papers):
            check_cancel(job.cancel)
            job.update(message=f"下载 {index + 1}/{len(papers)}：{paper['title'][:65]}…")
            # Only the ID determines the path; titles cannot inject paths or
            # reserved Windows filenames, and old-style slash IDs stay local.
            download_id = paper.get("arxiv_id") or paper["id"]
            if not ID_PATTERN.fullmatch(download_id):
                raise ValidationError("此记录没有可下载的 arXiv ID，请打开出版页面获取全文。")
            filename = download_id.replace("/", "_") + ".pdf"
            target = self.download_dir / filename
            try:
                outcome = self.transport.download(download_id, target, job.cancel)
                outcome["id"] = paper["id"]
                metadata = target.with_suffix(".json")
                if not metadata.exists():
                    part = metadata.with_suffix(".json.part")
                    try:
                        part.write_text(json.dumps(paper, ensure_ascii=False, indent=2), encoding="utf-8")
                        part.replace(metadata)
                    finally:
                        part.unlink(missing_ok=True)
            except RemoteError as exc:
                outcome = {"id": paper["id"], "filename": filename, "status": "failed", "error": str(exc)}
                job.warn(f"{paper['id']} 下载失败：{exc}")
            outcomes.append(outcome)
            job.update(scanned=index + 1, matched=sum(x["status"] != "failed" for x in outcomes),
                       progress=int((index + 1) / len(papers) * 100),
                       result={"files": copy.deepcopy(outcomes), "directory": str(self.download_dir)})
        return {"files": outcomes, "directory": str(self.download_dir),
                "succeeded": sum(x["status"] != "failed" for x in outcomes),
                "failed": sum(x["status"] == "failed" for x in outcomes)}
