"""Official Atom API client with caching, one connection and >=3s spacing."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlencode, urlparse

from .storage import Store

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
OPEN = "{http://a9.com/-/spec/opensearch/1.1/}"
ID_PATTERN = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z][a-z.-]*/\d{7})(?:v[1-9]\d*)?", re.I)
USER_AGENT = os.environ.get("ARXIV_USER_AGENT", "ArxivCCFDemo/1.0 (local research demo)")


class Cancelled(Exception):
    pass


class RemoteError(Exception):
    pass


def check_cancel(event: threading.Event | None):
    if event is not None and event.is_set():
        raise Cancelled("任务已取消。")


def pause(seconds: float, event: threading.Event | None):
    if event:
        if event.wait(max(0, seconds)):
            raise Cancelled("任务已取消。")
    else:
        time.sleep(max(0, seconds))


class RateGate:
    """Hold the gate until the response is fully consumed, including PDF streams."""

    def __init__(self, interval: float):
        self.interval = interval
        self.lock = threading.Lock()
        self.last_finished = 0.0

    def execute(self, action, cancel: threading.Event | None = None):
        while not self.lock.acquire(timeout=0.2):
            check_cancel(cancel)
        try:
            check_cancel(cancel)
            pause(self.interval - (time.perf_counter() - self.last_finished), cancel)
            return action()
        finally:
            self.last_finished = time.perf_counter()
            self.lock.release()


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme != "https" or parsed.hostname not in {
            "arxiv.org", "export.arxiv.org", "dblp.org", "dblp.uni-trier.de"
        }:
            raise RemoteError("远端返回了不支持的重定向地址。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Transport:
    def __init__(self, store: Store):
        self.store = store
        self.arxiv_gate = RateGate(3.1)
        self.dblp_gate = RateGate(3.1)
        self.opener = urllib.request.build_opener(RestrictedRedirect())

    def _run(self, url: str, action, cancel=None):
        gate = self.dblp_gate if urlparse(url).hostname == "dblp.org" else self.arxiv_gate
        for attempt in range(3):
            try:
                return gate.execute(action, cancel)
            except urllib.error.HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After", "0")
                exc.close()
                if status not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise RemoteError(f"远端服务返回 HTTP {status}。请稍后重试，或检查代理设置。") from exc
                seconds = min(120, float(retry_after)) if retry_after.isdigit() else 0
                pause(max(seconds, 4 * 2 ** attempt), cancel)
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, http.client.HTTPException) as exc:
                if attempt == 2:
                    raise RemoteError(f"连接远端服务失败：{exc}。可通过 HTTPS_PROXY 配置代理。") from exc
                pause(4 * 2 ** attempt, cancel)
        raise RemoteError("远端请求失败。")

    def get(self, url: str, cancel=None, ttl: float = 86400) -> tuple[bytes, bool]:
        key = "url:" + hashlib.sha256(url.encode()).hexdigest()
        check_cancel(cancel)
        cached = self.store.cached(key)
        if cached is not None:
            return cached, True

        def action():
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml, application/json"})
            with self.opener.open(req, timeout=25) as response:
                data = response.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise RemoteError("元数据响应过大，请缩小检索范围。")
            check_cancel(cancel)
            return data

        value = self._run(url, action, cancel)
        # Cache only payloads that have the expected structure, never challenge HTML.
        if "dblp.org" in url:
            try:
                parsed = json.loads(value)
                if not isinstance(parsed, dict) or "result" not in parsed:
                    raise ValueError
            except (ValueError, UnicodeDecodeError) as exc:
                raise RemoteError("DBLP 返回了验证页面或无效响应，会议补充核验暂不可用。") from exc
        else:
            parse_feed(value)
        self.store.cache(key, value, ttl)
        return value, False

    def download(self, paper_id: str, target: Path, cancel=None) -> dict:
        if not ID_PATTERN.fullmatch(paper_id):
            raise ValueError("arXiv ID 无效。")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            with target.open("rb") as stream:
                if stream.read(5) == b"%PDF-":
                    return {"id": paper_id, "filename": target.name, "status": "skipped", "bytes": target.stat().st_size}
            raise RemoteError(f"已有文件 {target.name} 不是有效 PDF，请重命名该文件后再下载。")
        url = f"https://arxiv.org/pdf/{paper_id}"
        part = target.with_suffix(".pdf.part")

        def action():
            size = 0
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf"})
            try:
                with self.opener.open(req, timeout=30) as response, part.open("wb") as stream:
                    first = True
                    while True:
                        check_cancel(cancel)
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        if first and not chunk.startswith(b"%PDF-"):
                            raise RemoteError("下载地址没有返回 PDF，可能暂时被限流。")
                        first = False
                        size += len(chunk)
                        if size > 150 * 1024 * 1024:
                            raise RemoteError("PDF 超过 demo 的 150 MB 单文件限制。")
                        stream.write(chunk)
                    expected = response.headers.get("Content-Length")
                    if expected and expected.isdigit() and size != int(expected):
                        raise RemoteError("PDF 下载不完整，请重试。")
                if size < 5:
                    raise RemoteError("PDF 响应为空。")
                check_cancel(cancel)
                part.replace(target)
                return {"id": paper_id, "filename": target.name, "status": "downloaded", "bytes": size}
            finally:
                part.unlink(missing_ok=True)

        return self._run(url, action, cancel)


def normalized_text(value: str | None) -> str:
    return " ".join((value or "").split())


def parse_feed(data: bytes) -> tuple[list[dict], int, int | None]:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise RemoteError("元数据 XML 包含不支持的声明。")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise RemoteError("arXiv 没有返回有效的 Atom XML，请稍后重试。") from exc
    if root.tag != ATOM + "feed":
        raise RemoteError("arXiv 返回了非 Atom 响应，可能是代理或验证页面。")
    papers = []
    for entry in root.findall(ATOM + "entry"):
        entry_url = entry.findtext(ATOM + "id", "")
        if "/api/errors" in entry_url:
            raise RemoteError("arXiv 查询错误：" + normalized_text(entry.findtext(ATOM + "summary")))
        match = re.search(r"/abs/(.+)$", entry_url)
        if not match or not ID_PATTERN.fullmatch(match.group(1)):
            raise RemoteError("arXiv 返回了无法识别的论文 ID。")
        paper_id = match.group(1)
        primary = entry.find(ARXIV + "primary_category")
        papers.append({
            "id": paper_id, "base_id": re.sub(r"v\d+$", "", paper_id),
            "title": normalized_text(entry.findtext(ATOM + "title")),
            "authors": [normalized_text(a.findtext(ATOM + "name")) for a in entry.findall(ATOM + "author")],
            "abstract": normalized_text(entry.findtext(ATOM + "summary")),
            "published": entry.findtext(ATOM + "published", ""),
            "updated": entry.findtext(ATOM + "updated", ""),
            "categories": [c.attrib.get("term", "") for c in entry.findall(ATOM + "category")],
            "primary_category": primary.attrib.get("term", "") if primary is not None else "",
            "comment": normalized_text(entry.findtext(ARXIV + "comment")),
            "journal_ref": normalized_text(entry.findtext(ARXIV + "journal_ref")),
            "doi": normalized_text(entry.findtext(ARXIV + "doi")),
            "abstract_url": f"https://arxiv.org/abs/{paper_id}",
            "pdf_url": f"https://arxiv.org/pdf/{paper_id}",
        })
    try:
        total = int(root.findtext(OPEN + "totalResults", str(len(papers))))
        start_text = root.findtext(OPEN + "startIndex")
        start = int(start_text) if start_text is not None else None
    except ValueError as exc:
        raise RemoteError("arXiv 分页元数据无效。") from exc
    return papers, total, start


class ArxivClient:
    def __init__(self, transport: Transport):
        self.transport = transport

    def page(self, query: str, start: int, count: int, sort_by: str, sort_order: str, cancel=None):
        url = "https://export.arxiv.org/api/query?" + urlencode({
            "search_query": query, "start": start, "max_results": count,
            "sortBy": sort_by, "sortOrder": sort_order,
        })
        data, cached = self.transport.get(url, cancel)
        papers, total, actual_start = parse_feed(data)
        if actual_start is not None and actual_start != start:
            raise RemoteError("arXiv 返回了不一致的分页起点，请稍后重试。")
        return papers, total, cached
