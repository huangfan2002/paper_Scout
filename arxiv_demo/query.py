"""Validate user filters and construct the official arXiv query syntax."""

from __future__ import annotations

import re
import shlex
from dataclasses import asdict, dataclass, field as dataclass_field
from datetime import date
from typing import Any


class ValidationError(ValueError):
    pass


def split_keywords(text: str) -> list[str]:
    try:
        words = shlex.split(text.strip())
    except ValueError as exc:
        raise ValidationError("关键词的双引号没有成对闭合。") from exc
    if len(words) > 30:
        raise ValidationError("一次最多输入 30 个关键词或短语。")
    # Operators are generated here; plain input never injects API query syntax.
    return [re.sub(r"[\x00-\x1f\x7f]", "", word) for word in words if word.strip()]


def string_value(data: dict, key: str, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ValidationError(f"{key} 必须是文本。")
    return value.strip()


def string_list(data: dict, key: str) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ValidationError(f"{key} 必须是文本列表。")
    return list(dict.fromkeys(x.strip() for x in value if x.strip()))


def integer_value(data: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValidationError(f"{key} 必须是整数。")
    try:
        value = int(value)
    except ValueError as exc:
        raise ValidationError(f"{key} 必须是整数。") from exc
    if not minimum <= value <= maximum:
        raise ValidationError(f"{key} 必须在 {minimum} 到 {maximum} 之间。")
    return value


@dataclass
class SearchOptions:
    source: str = "arxiv"
    keywords: str = ""
    field: str = "all"
    match: str = "all"
    date_from: str = ""
    date_to: str = ""
    categories: list[str] = dataclass_field(default_factory=list)
    ccf_ranks: list[str] = dataclass_field(default_factory=list)
    conferences: list[str] = dataclass_field(default_factory=list)
    evidence: str = "strict"
    sort_by: str = "submittedDate"
    sort_order: str = "descending"
    max_results: int = 50
    scan_limit: int = 300
    verify_dblp: bool = False
    dblp_limit: int = 30
    year_from: str = ""
    year_to: str = ""
    scholar_language: str = "en"

    @classmethod
    def parse(cls, data: Any, allowed_conferences: set[str] | None = None) -> "SearchOptions":
        if not isinstance(data, dict):
            raise ValidationError("请求内容必须是 JSON 对象。")
        # Explicit key handling keeps untrusted JSON out of internal state.
        values = {key: string_value(data, key, default) for key, default in {
            "source": "arxiv", "year_from": "", "year_to": "", "scholar_language": "en",
            "keywords": "", "field": "all", "match": "all", "date_from": "",
            "date_to": "", "evidence": "strict", "sort_by": "submittedDate",
            "sort_order": "descending",
        }.items()}
        if values["source"] == "google_scholar" and "sort_by" not in data:
            values["sort_by"] = "relevance"
        if len(values["keywords"]) > 500:
            raise ValidationError("关键词最多 500 个字符。")
        split_keywords(values["keywords"])
        for key, choices in {
            "source": {"arxiv", "google_scholar"},
            "scholar_language": {"en", "zh-CN"},
            "field": {"all", "title", "abstract", "author"},
            "match": {"all", "any"},
            "evidence": {"strict", "mentions"},
            "sort_by": {"submittedDate", "lastUpdatedDate", "relevance"},
            "sort_order": {"ascending", "descending"},
        }.items():
            if values[key] not in choices:
                raise ValidationError(f"{key} 选项无效。")
        for key in ("date_from", "date_to"):
            if values[key]:
                try:
                    parsed = date.fromisoformat(values[key])
                    if parsed.isoformat() != values[key]:
                        raise ValueError
                except ValueError as exc:
                    raise ValidationError("日期必须是有效的 YYYY-MM-DD 格式。") from exc
                if parsed.year < 1991:
                    raise ValidationError("arXiv 日期不能早于 1991 年。")
        if values["date_from"] and values["date_to"] and values["date_from"] > values["date_to"]:
            raise ValidationError("开始日期不能晚于结束日期。")
        categories = string_list(data, "categories")
        if len(categories) > 15 or any(not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]*(?:\.[a-zA-Z0-9-]+)?", x) for x in categories):
            raise ValidationError("arXiv 分类格式无效，例如 cs.LG、cs.CV、stat.ML。")
        ranks = string_list(data, "ccf_ranks")
        if any(x not in {"A", "B", "C", "unknown"} for x in ranks):
            raise ValidationError("CCF 等级只能为 A、B、C 或 unknown。")
        conferences = string_list(data, "conferences")
        if allowed_conferences is not None and set(conferences) - allowed_conferences:
            raise ValidationError("选择了目录中不存在的会议，请刷新目录。")
        for key in ("year_from", "year_to"):
            if values[key] and (not re.fullmatch(r"\d{4}", values[key]) or not 1000 <= int(values[key]) <= 9999):
                raise ValidationError("Google Scholar 年份必须是 1000–9999 之间的四位数字。")
        if values["year_from"] and values["year_to"] and values["year_from"] > values["year_to"]:
            raise ValidationError("起始年份不能晚于结束年份。")
        if values["source"] == "google_scholar":
            if not values["keywords"]:
                raise ValidationError("Google Scholar 检索需要输入关键词。")
            if categories or values["date_from"] or values["date_to"]:
                raise ValidationError("Google Scholar 请使用年份范围；arXiv 分类与提交日期仅适用于 arXiv。")
            if values["field"] == "abstract":
                raise ValidationError("Google Scholar 不支持仅摘要检索，请选择全部、标题或作者。")
            if values["sort_by"] != "relevance" or values["sort_order"] != "descending":
                raise ValidationError("Google Scholar 当前采用相关性排序，年份范围用于限制发表时间。")
        elif values["year_from"] or values["year_to"]:
            raise ValidationError("arXiv 请使用提交日期，而不是 Scholar 发表年份。")
        if not values["keywords"] and not categories and not values["date_from"] and not values["date_to"]:
            raise ValidationError("请至少输入关键词、arXiv 分类或日期范围之一。")
        maximum = integer_value(data, "max_results", 50, 1, 500)
        scan_limit = integer_value(data, "scan_limit", 300, 1, 2000)
        if scan_limit < maximum:
            raise ValidationError("候选扫描上限不能小于目标结果数。")
        if values["source"] == "google_scholar" and scan_limit > 1000:
            raise ValidationError("Google Scholar 单次查询最多扫描 1000 条结果。")
        verify = data.get("verify_dblp", False)
        if not isinstance(verify, bool):
            raise ValidationError("verify_dblp 必须为布尔值。")
        return cls(**values, categories=categories, ccf_ranks=ranks, conferences=conferences,
                   max_results=maximum, scan_limit=scan_limit, verify_dblp=verify,
                   dblp_limit=integer_value(data, "dblp_limit", 30, 1, 100))

    def to_dict(self) -> dict:
        return asdict(self)


def build_query(options: SearchOptions) -> str:
    pieces = []
    prefix = {"all": "all", "title": "ti", "abstract": "abs", "author": "au"}[options.field]
    words = split_keywords(options.keywords)
    if words:
        clauses = []
        for word in words:
            cleaned = re.sub(r'["\\:()\[\]{}]', " ", word).strip()
            if cleaned:
                clauses.append(f'{prefix}:"{cleaned}"')
        if clauses:
            operator = " AND " if options.match == "all" else " OR "
            pieces.append("(" + operator.join(clauses) + ")")
    if options.categories:
        pieces.append("(" + " OR ".join(f"cat:{c}" for c in options.categories) + ")")
    if options.date_from or options.date_to:
        start = (options.date_from or "1991-01-01").replace("-", "") + "0000"
        end = (options.date_to or "2099-12-31").replace("-", "") + "2359"
        pieces.append(f"submittedDate:[{start} TO {end}]")
    if not pieces:
        raise ValidationError("关键词没有可检索的内容。")
    return " AND ".join(pieces)
