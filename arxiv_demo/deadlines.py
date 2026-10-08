"""CCFDDL JSON integration, explicit timezones, and atomic local snapshots."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .client import RemoteError, check_cancel
from .query import ValidationError, integer_value, string_value

SOURCE_URL = "https://ccfddl.com/conference/allconf.json"
SOURCE_REPO = "https://github.com/ccfddl/ccf-deadlines"
MAX_BYTES = 12 * 1024 * 1024
STAGES = {"deadline": "正文投稿", "abstract_deadline": "摘要注册", "rebuttal_deadline": "作者回复", "decision_deadline": "录用通知"}
UTC = timezone.utc


def text(value, maximum=2000):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("CCFDDL 文本字段无效或过长。")
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value).strip()


def link(value):
    value = text(value)
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        return ""
    return value


def _sunday(year, month, number):
    first = datetime(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (number - 1), hours=2)


def deadline_time(raw: str, zone: str) -> dict:
    """PT uses US rules from 2007; ambiguous/nonexistent clocks stay unconverted."""
    result = {"raw": raw, "timezone": zone, "utc": None, "warning": ""}
    if not raw or raw.upper() == "TBD":
        return result
    try:
        clock = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        name = zone.strip().upper()
        if name == "AOE":
            offset = -12 * 60
        elif name == "PT":
            if clock.year < 2007:
                raise ValueError("2007 年前的 PT 时区规则未支持")
            start, end = _sunday(clock.year, 3, 2), _sunday(clock.year, 11, 1)
            if start <= clock < start + timedelta(hours=1):
                raise ValueError("PT 夏令时跳时，原始时刻不存在")
            if end - timedelta(hours=1) <= clock < end:
                raise ValueError("PT 夏令时结束，该时刻有两种解释")
            offset = (-7 if start + timedelta(hours=1) <= clock < end - timedelta(hours=1) else -8) * 60
        else:
            match = re.fullmatch(r"UTC(?:([+-])(\d{1,2})(?::(\d{2}))?)?", name)
            if not match:
                raise ValueError("未支持的原始时区")
            hours, minutes = int(match[2] or 0), int(match[3] or 0)
            if hours > 14 or minutes > 59 or (hours == 14 and minutes):
                raise ValueError("原始 UTC 偏移量无效")
            offset = (hours * 60 + minutes) * (-1 if match[1] == "-" else 1)
        result["utc"] = clock.replace(tzinfo=timezone(timedelta(minutes=offset))).astimezone(UTC).isoformat().replace("+00:00", "Z")
    except ValueError as exc:
        result["warning"] = str(exc)
    return result


def _name(value):
    return re.sub(r"[^\w]", "", value.casefold())


def match_venue(record, catalogue):
    candidates = [v for v in catalogue.venues if v["major_id"] == record["sub"]]
    key = text(record.get("dblp")).casefold()
    exact = [v for v in candidates if key and key in {x.casefold() for x in v.get("dblp_keys", [])}]
    if len(exact) == 1:
        return exact[0]
    aliases = [v for v in candidates if _name(record["title"]) in {_name(x) for x in [v["acronym"], *v.get("aliases", [])]}]
    return aliases[0] if len(aliases) == 1 and not exact else None


def normalize(records, catalogue):
    if not isinstance(records, list) or not 1 <= len(records) <= 2000:
        raise ValueError("CCFDDL 必须返回非空的会议 JSON 列表。")
    majors = {m["id"]: m for m in catalogue.taxonomy["majors"]}
    entries, keys, edition_keys = [], set(), set()
    mapped, differences, warnings = 0, [], []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("CCFDDL 会议条目必须为对象。")
        key = text(record.get("conference_key"), 160)
        sub = text(record.get("sub"), 30)
        if not re.fullmatch(r"[A-Z]{2}/[\w&+-]+", key) or key in keys or not key.startswith(sub + "/"):
            raise ValueError("CCFDDL 会议身份缺失或重复。")
        if sub not in majors:
            raise ValueError("CCFDDL 包含尚未配置的大方向：" + sub)
        keys.add(key)
        title, description = text(record.get("title"), 180), text(record.get("description"))
        if not title:
            raise ValueError("CCFDDL 会议简称为空。")
        venue = match_venue(record, catalogue)
        if not isinstance(record.get("rank"), dict):
            raise ValueError("CCFDDL 等级字段无效。")
        source_rank = text(record["rank"].get("ccf"), 10)
        if venue:
            mapped += 1
            if source_rank != venue["rank"]:
                differences.append({"key": key, "ccfddl": source_rank, "ccf_2026": venue["rank"]})
        editions = record.get("confs")
        if not isinstance(editions, list) or len(editions) > 100 or any(not isinstance(e, dict) for e in editions):
            raise ValueError("CCFDDL 届次列表无效。")
        if any(isinstance(e.get("year"), bool) or not isinstance(e.get("year"), int) for e in editions):
            raise ValueError("CCFDDL 届次年份必须是整数。")
        latest = max((e.get("year", 0) for e in editions), default=0)
        for edition in editions:
            year = edition.get("year")
            edition_id = text(edition.get("id"), 160)
            identity = key + "/" + edition_id
            if isinstance(year, bool) or not isinstance(year, int) or not 1990 <= year <= 2200 or not edition_id or identity in edition_keys:
                raise ValueError("CCFDDL 届次年份或身份无效。")
            edition_keys.add(identity)
            zone = text(edition.get("timezone"), 60)
            timeline = edition.get("timeline")
            if not isinstance(timeline, list) or not 1 <= len(timeline) <= 50:
                raise ValueError("CCFDDL 时间线为空或无效。")
            rounds = []
            for index, point in enumerate(timeline):
                if not isinstance(point, dict):
                    raise ValueError("CCFDDL 时间节点必须为对象。")
                row = {"round": index + 1, "comment": text(point.get("comment")), "events": {}}
                for stage in STAGES:
                    if point.get(stage) is not None:
                        event = deadline_time(text(point[stage], 80), zone)
                        row["events"][stage] = event
                        if event["warning"]:
                            warnings.append(identity + " / " + stage + ": " + event["warning"])
                if "deadline" not in row["events"]:
                    raise ValueError("CCFDDL 时间线缺少正文截止字段。")
                rounds.append(row)
            entries.append({
                "id": identity, "conference_key": key, "edition_id": edition_id, "title": title,
                "description": description, "year": year, "latest": year == latest,
                "major_id": sub, "major_name": majors[sub]["name"],
                "subfield_id": venue["subfield_id"] if venue else sub + ":unmapped",
                "subfield_name": venue["subfield_name"] if venue else "未匹配当前 CCF 目录",
                "venue_id": venue["id"] if venue else None, "ccf_rank": venue["rank"] if venue else None,
                "upstream_rank": source_rank, "timezone": zone, "date": text(edition.get("date")),
                "place": text(edition.get("place")), "url": link(edition.get("link")),
                "source_url": SOURCE_REPO + "/blob/main/conference/" + key + ".yml", "timeline": rounds,
            })
    return entries, {"venue_count": len(records), "edition_count": len(entries), "matched_venues": mapped,
                     "unmatched_venues": len(records) - mapped, "rank_differences": differences, "time_warnings": warnings}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class DeadlineClient:
    def __init__(self, root: Path, data_dir: Path, catalogue):
        self.catalogue, self.cache_file = catalogue, data_dir / "ccfddl_snapshot.json"
        self.lock = threading.RLock()
        self.last_refresh = 0.0
        self.opener = urllib.request.build_opener(NoRedirect())
        bundled = root / "config" / "ccfddl_snapshot.json"
        self.load_warning = ""
        try:
            self._load(self.cache_file if self.cache_file.exists() else bundled)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.load_warning = "本地更新缓存无法解析，已使用随附快照。"
            self._load(bundled)

    def _load(self, path):
        data = json.loads(path.read_text(encoding="utf-8"))
        entries, counts = normalize(data["conferences"], self.catalogue)
        self.entries = entries
        self.metadata = {**data["metadata"], **counts, "cache": "updated" if path == self.cache_file else "bundled"}

    def public(self):
        with self.lock:
            return {**copy.deepcopy(self.metadata), "warning": self.load_warning, "source_url": SOURCE_URL,
                    "repository": SOURCE_REPO, "grade_policy": "等级按已匹配的 CCF 2026 目录；未匹配会议显示为未收录。"}

    def refresh(self, job):
        if self.last_refresh and time.monotonic() - self.last_refresh < 60:
            job.update(message="刚刚已同步，使用本地更新快照。", progress=100)
            return self.public()
        check_cancel(job.cancel)
        job.update(message="读取 CCFDDL 官方站点的会议 JSON…", progress=15)
        request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "PaperScoutDemo/1.2 (+local academic demo)", "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=25) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("响应超过 12 MB")
            check_cancel(job.cancel)
            records = json.loads(raw)
            entries, counts = normalize(records, self.catalogue)
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
            raise RemoteError("CCFDDL 同步失败，已有快照仍可用：" + str(exc)) from exc
        metadata = {"fetched_at": datetime.now(UTC).isoformat(), "source_url": SOURCE_URL,
                    "sha256": hashlib.sha256(raw).hexdigest(), "license": "MIT", "repository": SOURCE_REPO}
        snapshot = {"metadata": metadata, "conferences": records}
        job.update(message="会议时间线已校验，正在保存本地快照…", progress=85)
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_file.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            check_cancel(job.cancel)
            with self.lock:
                temporary.replace(self.cache_file)
                self.entries, self.metadata = entries, {**metadata, **counts, "cache": "updated"}
                self.load_warning = ""
                self.last_refresh = time.monotonic()
        finally:
            temporary.unlink(missing_ok=True)
        job.update(progress=100, matched=counts["venue_count"], scanned=counts["edition_count"])
        return self.public()

    def search(self, params: dict, at=None):
        clock = (at or datetime.now(UTC)).astimezone(UTC)
        stage = string_value(params, "stage", "deadline")
        status = string_value(params, "status", "upcoming")
        rank = string_value(params, "rank")
        ranks = set(rank.split(",")) if rank else set()
        major, subfield = string_value(params, "major"), string_value(params, "subfield")
        venue_id = string_value(params, "venue")
        query, year = string_value(params, "q").casefold(), string_value(params, "year")
        limit = integer_value(params, "limit", 24, 1, 100)
        page = integer_value(params, "page", 1, 1, 10000)
        if stage not in STAGES or status not in {"upcoming", "past", "tbd", "all"} or ranks - {"A", "B", "C", "N"}:
            raise ValidationError("截止日期筛选选项无效。")
        majors = self.catalogue.taxonomy["majors"]
        if major and major not in {m["id"] for m in majors}:
            raise ValidationError("大方向不存在。")
        if venue_id and venue_id not in self.catalogue.by_id:
            raise ValidationError("会议目录 ID 不存在。")
        if subfield and subfield not in {s["id"] for m in majors for s in m["subfields"]} | {m["id"] + ":unmapped" for m in majors}:
            raise ValidationError("小方向不存在。")
        if len(query) > 150 or (year and not re.fullmatch(r"(?:19|20|21)\d{2}|2200", year)):
            raise ValidationError("检索词过长或会议年份无效。")
        rows = []
        with self.lock:
            entries = self.entries
        for entry in entries:
            if venue_id and entry["venue_id"] != venue_id:
                continue
            if major and entry["major_id"] != major or subfield and entry["subfield_id"] != subfield:
                continue
            if ranks and (entry["ccf_rank"] or "N") not in ranks or year and str(entry["year"]) != year:
                continue
            if query and query not in " ".join(str(entry[k]) for k in ("title", "description", "major_name", "subfield_name", "place", "conference_key")).casefold():
                continue
            events = [point["events"][stage] for point in entry["timeline"] if stage in point["events"]]
            times = sorted(datetime.fromisoformat(e["utc"].replace("Z", "+00:00")) for e in events if e["utc"])
            future = [value for value in times if value >= clock]
            tbd = any(e["raw"].upper() == "TBD" for e in events)
            eligible_tbd = tbd and entry["latest"] and entry["year"] >= clock.year
            if status == "upcoming" and not (future or eligible_tbd):
                continue
            if status == "past" and (not times or future or tbd):
                continue
            if status == "tbd" and not tbd:
                continue
            next_time = future[0] if future else None
            row = copy.deepcopy(entry)
            row["next_utc"] = next_time.isoformat().replace("+00:00", "Z") if next_time else None
            row["status"] = "upcoming" if next_time else "tbd" if tbd else "past" if times else "unavailable"
            # Upcoming first; historical editions ordered by their most recent date.
            row["_sort"] = (0, next_time.timestamp(), entry["title"]) if next_time else (1 if tbd else 2, -(times[-1].timestamp() if times else entry["year"]), entry["title"])
            rows.append(row)
        rows.sort(key=lambda x: x.pop("_sort"))
        total = len(rows)
        start = (page - 1) * limit
        return {"items": rows[start:start + limit], "total": total, "page": page, "limit": limit,
                "stage": stage, "as_of": clock.isoformat(), "metadata": self.public(),
                "years": sorted({e["year"] for e in entries}, reverse=True)}
