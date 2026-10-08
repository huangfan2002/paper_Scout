"""Google Scholar links, saved-page imports, and an optional SerpApi adapter.

No automated requests are sent to scholar.google.com. Credentials remain on
the server; only normalized public bibliographic fields enter the cache.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .client import ID_PATTERN, RateGate, RemoteError, check_cancel, normalized_text
from .query import SearchOptions, ValidationError, split_keywords

MAX_IMPORT_BYTES = 1024 * 1024


def safe_url(value) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        return ""
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"https", "http"} or not host or parsed.username or parsed.password:
            return ""
        if parsed.port not in {None, 80, 443} or host in {"localhost", "serpapi.com"} or host.endswith((".localhost", ".local")):
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        return urlunsplit(parsed)
    except ValueError:
        return ""


def scholar_query(options: SearchOptions) -> str:
    prefix = {"all": "", "title": "intitle:", "author": "author:"}.get(options.field)
    if prefix is None:
        raise ValidationError("Google Scholar 不支持仅摘要检索。")
    terms = []
    for word in split_keywords(options.keywords):
        cleaned = re.sub(r'["\\:()\[\]{}]', " ", word).strip()
        if cleaned:
            terms.append(prefix + '"' + cleaned + '"')
    if not terms:
        raise ValidationError("关键词没有可检索的内容。")
    return (" OR " if options.match == "any" else " ").join(terms)


def scholar_parameters(options: SearchOptions) -> dict:
    params = {"q": scholar_query(options), "hl": options.scholar_language, "as_sdt": "0", "as_vis": "1"}
    if options.year_from:
        params["as_ylo"] = options.year_from
    if options.year_to:
        params["as_yhi"] = options.year_to
    return params


def scholar_url(options: SearchOptions) -> str:
    return "https://scholar.google.com/scholar?" + urlencode(scholar_parameters(options))


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _number(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, str)) and str(value).isdigit():
        return int(value)
    return None


def arxiv_id_from_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.hostname not in {"arxiv.org", "export.arxiv.org"}:
        return ""
    match = re.fullmatch(r"/(?:abs|pdf)/(.+?)(?:\.pdf)?/?", parsed.path)
    return match.group(1) if match and ID_PATTERN.fullmatch(match.group(1)) else ""


def normalize_record(record: dict) -> dict:
    if not isinstance(record, dict) or not isinstance(record.get("title"), str) or not record["title"].strip():
        raise RemoteError("Scholar 条目缺少有效标题。")
    title = normalized_text(record["title"])[:2000]
    publication = _mapping(record.get("publication_info"))
    summary = normalized_text(publication.get("summary") if isinstance(publication.get("summary"), str) else "")[:4000]
    parts = re.split(r"\s+[–—-]\s+", summary)
    venue = " - ".join(parts[1:-1]) if len(parts) >= 3 else parts[1] if len(parts) == 2 else ""
    authors = [normalized_text(a.get("name", "")) for a in publication.get("authors", [])
               if isinstance(a, dict) and isinstance(a.get("name"), str)] if isinstance(publication.get("authors"), list) else []
    if not authors and len(parts) >= 2:
        authors = [x.strip() for x in parts[0].split(",") if x.strip()]
    year = _number(publication.get("year"))
    years = re.findall(r"(?<!\d)(?:1\d{3}|20\d{2}|21\d{2})(?!\d)", " - ".join(parts[1:]))
    year = str(year) if year and 1000 <= year <= 9999 else years[-1] if years else ""
    link = safe_url(record.get("link"))
    inline = _mapping(record.get("inline_links"))
    cited = _mapping(inline.get("cited_by"))
    versions = _mapping(inline.get("versions"))
    resources = record.get("resources", [])
    pdfs = [safe_url(r.get("link")) for r in resources if isinstance(r, dict) and str(r.get("file_format", "")).upper() == "PDF"] if isinstance(resources, list) else []
    pdfs = [x for x in pdfs if x]
    if link and urlsplit(link).path.lower().endswith(".pdf") and link not in pdfs:
        pdfs.append(link)
    arxiv_id = next((identifier for x in [link, *pdfs] if (identifier := arxiv_id_from_url(x))), "")
    result_id = record.get("result_id", "")
    result_id = result_id if isinstance(result_id, str) and re.fullmatch(r"[\w-]{1,80}", result_id, re.ASCII) else ""
    digest = hashlib.sha256((title.casefold() + "|" + "|".join(authors).casefold() + "|" + year).encode()).hexdigest()[:24]
    return {"id": "scholar_" + (result_id or digest), "base_id": "scholar_" + digest,
            "source": "google_scholar", "arxiv_id": arxiv_id, "title": title, "authors": authors[:100],
            "abstract": normalized_text(record.get("snippet") if isinstance(record.get("snippet"), str) else "")[:10000],
            "abstract_kind": "snippet", "published": "", "updated": "", "publication_year": year,
            "categories": [], "primary_category": "", "comment": "", "journal_ref": "", "doi": "",
            "publication_summary": summary, "publication_venue": venue,
            "abstract_url": link, "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else (pdfs[0] if pdfs else ""),
            "pdf_links": pdfs, "downloadable": bool(arxiv_id), "citations": _number(cited.get("total")),
            "cited_by_url": safe_url(cited.get("link")), "related_url": safe_url(inline.get("related_pages_link")),
            "versions_url": safe_url(versions.get("link")), "versions_count": _number(versions.get("total")),
            "scholar_url": "https://scholar.google.com/scholar?" + urlencode({"q": '"' + title + '"'})}


def normalize_page(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise RemoteError("Scholar 接口没有返回 JSON 对象。")
    if payload.get("error"):
        # Never expose remote text: it may echo the request URL and API key.
        raise RemoteError("Scholar 接口返回错误，请检查本机 Key、账户配额或检索条件。")
    if _mapping(payload.get("search_metadata")).get("status") in {"Processing", "Error"}:
        raise RemoteError("Scholar 接口尚未完成或检索失败，请稍后重试。")
    records = payload.get("organic_results")
    if records is None and _number(_mapping(payload.get("search_information")).get("total_results")) == 0:
        records = []
    if not isinstance(records, list) or len(records) > 1000:
        raise RemoteError("需要 organic_results 数组（最多 1000 条）；响应可能不是 Scholar 检索结果。")
    papers = [normalize_record(r) for r in records]
    total = _number(_mapping(payload.get("search_information")).get("total_results"))
    pagination = _mapping(payload.get("serpapi_pagination")) or _mapping(payload.get("pagination"))
    return {"papers": papers, "total": total, "has_next": bool(pagination.get("next") or pagination.get("next_link"))}


class SavedScholarPage(HTMLParser):
    """Parse a user-saved Scholar results page without executing its scripts."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.records = []
        self.current = None
        self.depth = 0
        self.field = None
        self.field_depth = 0
        self.authors = []
        self.pdf_depth = -1

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"meta", "link", "img", "input", "br", "hr", "source", "wbr"}:
            return
        self.stack.append(tag)
        classes = attrs.get("class", "").split()
        if "gs_r" in classes and "gs_scl" in classes:
            self.current = {"title": "", "snippet": "", "publication_info": {}, "inline_links": {}, "resources": [],
                            "result_id": attrs.get("data-cid", "")}
            self.depth = len(self.stack)
            self.authors = []
        if self.current is None:
            return
        if "gs_or_ggsm" in classes:
            self.pdf_depth = len(self.stack)
        for css, field in (("gs_rt", "title"), ("gs_a", "summary"), ("gs_rs", "snippet")):
            if css in classes:
                self.field, self.field_depth = field, len(self.stack)
                if field == "summary":
                    self.current["publication_info"]["summary"] = ""
        if tag == "a":
            href = attrs.get("href", "")
            if href.startswith("/"):
                href = "https://scholar.google.com" + href
            if self.field == "title":
                self.current["link"] = href
            elif self.field == "summary":
                self.authors.append({"name": ""})
                self.author_depth = len(self.stack)
            elif "cites=" in href:
                self.current["inline_links"]["cited_by"] = {"link": href}
            elif "cluster=" in href:
                self.current["inline_links"]["versions"] = {"link": href}
            elif "related" in href:
                self.current["inline_links"]["related_pages_link"] = href
            elif self.pdf_depth > 0:
                self.current["resources"].append({"file_format": "PDF", "link": href})
            self.anchor = href
            self.anchor_depth = len(self.stack)
            self.anchor_text = ""

    def handle_endtag(self, tag):
        if tag not in self.stack:
            return
        if self.current is not None:
            if tag == "a" and getattr(self, "anchor_depth", -1) == len(self.stack):
                number = re.search(r"\d[\d,]*", self.anchor_text)
                if number:
                    if "cites=" in self.anchor:
                        self.current["inline_links"]["cited_by"]["total"] = int(number.group().replace(",", ""))
                    elif "cluster=" in self.anchor:
                        self.current["inline_links"]["versions"]["total"] = int(number.group().replace(",", ""))
                self.anchor_depth = -1
                self.author_depth = -1
            if self.field_depth == len(self.stack):
                self.field = None
            if self.pdf_depth == len(self.stack):
                self.pdf_depth = -1
            if self.depth == len(self.stack):
                if self.authors:
                    self.current["publication_info"]["authors"] = self.authors
                self.current["title"] = re.sub(r"^\s*\[(?:PDF|HTML|BOOK|CITATION|图书|引用)\]\s*", "", self.current["title"])
                if self.current["title"].strip():
                    self.records.append(self.current)
                self.current = None
        # Well formed saved pages are expected, but do not hang on malformed HTML.
        index = len(self.stack) - 1 - self.stack[::-1].index(tag)
        del self.stack[index:]

    def handle_data(self, data):
        if self.current is None:
            return
        if getattr(self, "anchor_depth", -1) > 0:
            self.anchor_text += data
        if self.field == "summary":
            self.current["publication_info"]["summary"] += data
            if getattr(self, "author_depth", -1) > 0 and self.authors:
                self.authors[-1]["name"] += data
        elif self.field:
            self.current[self.field] += data


def parse_import(content: str) -> dict:
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ValidationError("导入文件必须是 UTF-8 文本，大小不超过 1 MB。")
    if content.lstrip("\ufeff \r\n\t").startswith(("{", "[")):
        try:
            payload = json.loads(content.lstrip("\ufeff"))
        except ValueError as exc:
            raise ValidationError("导入 JSON 格式错误。") from exc
        if isinstance(payload, list):
            payload = {"organic_results": payload}
        return normalize_page(payload)
    parser = SavedScholarPage()
    try:
        parser.feed(content)
        parser.close()
    except (ValueError, TypeError) as exc:
        raise ValidationError("无法解析保存的 Scholar 网页。") from exc
    if not parser.records:
        raise ValidationError("没有找到 Google Scholar 结果卡片；请保存检索结果页，而非验证页。")
    return normalize_page({"organic_results": parser.records})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RemoteError("Scholar 接口出现重定向；请检查网络或服务状态。")


class ScholarClient:
    def __init__(self, store, root: Path):
        self.store = store
        self.root = root
        self.gate = RateGate(1.1)
        self.opener = urllib.request.build_opener(NoRedirect())

    def api_key(self) -> str:
        key = os.environ.get("SERPAPI_API_KEY", "").strip()
        if not key:
            path = self.root / "config" / "scholar.local.json"
            if path.exists():
                try:
                    key = json.loads(path.read_text(encoding="utf-8-sig")).get("api_key", "")
                except (ValueError, OSError, AttributeError):
                    raise ValidationError("config/scholar.local.json 格式错误。") from None
        if not isinstance(key, str) or len(key) > 256 or any(ord(c) < 32 for c in key):
            raise ValidationError("Scholar API Key 格式无效，请检查本机配置。")
        return key.strip()

    def public(self) -> dict:
        try:
            return {"provider": "serpapi", "configured": bool(self.api_key()), "message": ""}
        except ValidationError as exc:
            return {"provider": "serpapi", "configured": False, "message": str(exc)}

    def page(self, options: SearchOptions, start: int, count: int, cancel=None):
        check_cancel(cancel)
        key = self.api_key()
        if not key:
            raise ValidationError("Google Scholar 自动检索需要 SerpApi Key。当前可使用“打开 Google 学术”并导入保存的结果页。")
        params = {"engine": "google_scholar", **scholar_parameters(options), "start": start, "num": min(20, count)}
        cache_key = "scholar:v1:" + hashlib.sha256((urlencode(params) + "|" + key).encode()).hexdigest()
        cached = self.store.cached(cache_key)
        if cached is not None:
            return json.loads(cached), True

        def action():
            req = urllib.request.Request("https://serpapi.com/search.json?" + urlencode({**params, "api_key": key}),
                headers={"Accept": "application/json", "User-Agent": "PaperScout/1.1"})
            try:
                with self.opener.open(req, timeout=30) as response:
                    content = response.read(2 * MAX_IMPORT_BYTES + 1)
            except urllib.error.HTTPError as exc:
                code = exc.code
                exc.close()
                detail = "请检查 Key" if code in {401, 403} else "请检查账户配额或稍后重试" if code == 429 else "请稍后重试"
                raise RemoteError(f"Scholar 接口 HTTP {code}，{detail}。") from None
            except (urllib.error.URLError, OSError, TimeoutError, socket.timeout):
                raise RemoteError("无法连接 Scholar 检索接口，请检查代理或网络设置。") from None
            if len(content) > 2 * MAX_IMPORT_BYTES:
                raise RemoteError("Scholar 接口响应超过 2 MB 限制。")
            try:
                result = normalize_page(json.loads(content))
            except (ValueError, UnicodeDecodeError):
                raise RemoteError("Scholar 接口返回无效 JSON 或验证页面。") from None
            check_cancel(cancel)
            return result

        result = self.gate.execute(action, cancel)
        # Deliberately omit search_parameters, API URLs, tokens and raw payload.
        self.store.cache(cache_key, json.dumps(result, ensure_ascii=False).encode(), 86400)
        return result, False
