"""CCF catalogue matching. Venue rating and paper acceptance remain separate."""

from __future__ import annotations

import html
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from .client import RemoteError, check_cancel
from .query import SearchOptions
from .navigation import load_navigation


NON_MAIN = re.compile(
    r"\b(?:workshops?|findings|short[ -]papers?|demo[ -]papers?|demonstrations?|"
    r"technical[ -]briefs?|extended[ -]abstracts?|doctoral[ -](?:consortium|symposium)|"
    r"companion|non[ -]archival|industry[ -]track)\b", re.I)
NEGATIVE = re.compile(
    r"\b(?:not\s+(?:yet\s+)?accepted|not\s+to\s+appear|not\s+published|"
    r"rejected|withdrawn|retracted|under\s+(?:review|submission)|"
    r"submitted\s+(?:to|at|for)|submission\s+(?:to|at|for)|will\s+(?:be\s+)?submit)\b", re.I)
ACCEPTED = re.compile(
    r"\b(?:accepted\b|to\s+appear\b|published\s+(?:in|at)\b|"
    r"appeared\s+(?:in|at)\b|presented\s+at\b|proceedings\s+of\b)" , re.I)


class Catalogue:
    def __init__(self, path: Path):
        self.path = path
        self.load()

    def load(self):
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.metadata = data["metadata"]
        self.venues = data["conferences"]
        self.by_id = {v["id"]: v for v in self.venues}
        self.taxonomy = load_navigation(self.path.parent / "conference_taxonomy.json", self.venues)
        self.patterns = []
        for venue in self.venues:
            aliases = list(dict.fromkeys([venue["acronym"], venue["name"], *venue.get("aliases", [])]))
            for alias in aliases:
                if not alias:
                    continue
                body = r"\s+".join(re.escape(part) for part in alias.split())
                # Short acronyms such as SC/ER/MM are ordinary words in prose.
                # Require their literal casing and a nearby conference context.
                short = (len(re.sub(r"\W", "", alias)) <= 3 and alias.isalpha()) or alias in {"FAST", "PADS", "SEA", "SAC", "CLOUD", "CLUSTER"}
                flags = 0 if short else re.I
                ending = r"(?=$|[^\w]|(?:19|20)\d{2}\b)"
                self.patterns.append((venue, alias, re.compile(r"(?<!\w)" + body + ending, flags), short))

    def public(self):
        return {"metadata": self.metadata, "conferences": self.venues, "taxonomy": self.taxonomy,
                "counts": {rank: sum(v["rank"] == rank for v in self.venues) for rank in "ABC"}}

    def matches(self, text: str):
        found = []
        for venue, alias, pattern, short in self.patterns:
            for match in pattern.finditer(text):
                if venue["acronym"] == "SIGGRAPH" and re.match(r"\s+Asia\b", text[match.end():], re.I):
                    # SIGGRAPH Asia is a different venue and is not a separate
                    # entry in the bundled CCF conference catalogue.
                    continue
                around = text[max(0, match.start() - 110): min(len(text), match.end() + 110)]
                if short and not re.search(r"\b(?:19|20)\d{2}\b|conference|symposium|proceedings|accepted|appear", around, re.I):
                    continue
                found.append((venue, match))
        # Prefer the longest alias when full names overlap acronyms.
        found.sort(key=lambda pair: (pair[1].start(), -(pair[1].end() - pair[1].start())))
        unique = []
        seen = set()
        for venue, match in found:
            if any(other["id"] != venue["id"] and longer.start() <= match.start() and
                   longer.end() >= match.end() and longer.end() - longer.start() > match.end() - match.start()
                   for other, longer in found):
                continue
            key = (venue["id"], match.start())
            if key not in seen:
                unique.append((venue, match))
                seen.add(key)
        return unique


def evidence_window(text: str, match: re.Match) -> str:
    # Each venue is evaluated in its own sentence/clause. A rejected venue in a
    # different sentence must not invalidate a subsequent acceptance statement.
    before = text[:match.start()]
    after = text[match.end():]
    boundary_before = max(before.rfind(";"), before.rfind(". "), before.rfind("\n"))
    boundaries = [p for p in (after.find(";"), after.find(". "), after.find("\n")) if p >= 0]
    boundary_after = min(boundaries) if boundaries else len(after)
    start = max(boundary_before + 1, match.start() - 180)
    end = min(match.end() + boundary_after, match.end() + 180)
    return text[start:end].strip()


def classify(paper: dict, catalogue: Catalogue, dblp_record: dict | None = None) -> dict:
    candidates = []
    for field in ("journal_ref", "comment", "publication_venue"):
        text = paper.get(field, "")
        for venue, match in catalogue.matches(text):
            snippet = evidence_window(text, match)
            non_main = bool(NON_MAIN.search(snippet))
            negative = bool(NEGATIVE.search(snippet))
            declared = bool(ACCEPTED.search(snippet)) or (field == "journal_ref" and bool(re.search(r"\b(?:19|20)\d{2}\b", snippet)))
            status = "excluded" if non_main or negative else "author_reported" if declared else "mentioned"
            reason = "非主会正式长文类型" if non_main else "投稿、拒稿或撤稿信息" if negative else "作者在 arXiv 元数据中声明录用/发表" if declared else "只提及会议，尚无录用证据"
            years = re.findall(r"\b(?:19|20)\d{2}\b", snippet)
            source = f"arxiv.{field}"
            if field == "publication_venue":
                source = "google_scholar.publication_summary"
                if not non_main and not negative and paper.get("publication_year"):
                    status = "publication_record"
                    reason = "Scholar 出版摘要映射到该会议；出处与正式长文类型仍需核对"
            candidates.append({"conference_id": venue["id"], "conference": venue["acronym"],
                               "rank": venue["rank"], "domain": venue["domain"], "status": status,
                               "source": source, "evidence": snippet, "reason": reason,
                               "year": years[0] if years else "", "url": paper["abstract_url"],
                               "paper_type": "excluded" if non_main else "unspecified",
                               "full_paper_verified": False})
    if dblp_record:
        # DBLP keys must identify the exact main proceedings, not a workshop
        # sharing the parent conference's acronym in a venue string.
        key = dblp_record.get("key", "")
        venue_text = dblp_record.get("venue", "")
        non_main = bool(NON_MAIN.search(venue_text + " " + dblp_record.get("title", "")))
        for venue in catalogue.venues:
            prefixes = venue.get("dblp_keys", [])
            if not non_main and any(key.startswith(f"conf/{prefix}/") for prefix in prefixes):
                candidates.append({"conference_id": venue["id"], "conference": venue["acronym"],
                                   "rank": venue["rank"], "domain": venue["domain"], "status": "verified",
                                   "source": "dblp", "evidence": f"{dblp_record.get('title', '')} · {venue_text} {dblp_record.get('year', '')}",
                                   "reason": "DBLP 标题及作者匹配到该会议的主会记录（长文类型仍需人工核对）",
                                   "year": dblp_record.get("year", ""), "url": dblp_record.get("url", ""),
                                   "paper_type": "unspecified", "full_paper_verified": False})
    priority = {"verified": 0, "author_reported": 1, "publication_record": 1, "mentioned": 2, "excluded": 3}
    candidates.sort(key=lambda item: (priority[item["status"]], item["rank"]))
    unique = []
    seen = set()
    for candidate in candidates:
        identity = (candidate["conference_id"], candidate["status"], candidate["source"])
        if identity not in seen:
            unique.append(candidate)
            seen.add(identity)
    reliable = [c for c in unique if c["status"] in {"verified", "author_reported", "publication_record"}]
    selected = reliable[0] if reliable else unique[0] if unique else None
    return {"rank": selected["rank"] if selected and selected["status"] in {"verified", "author_reported", "publication_record"} else None,
            "conference": selected["conference"] if selected else "",
            "status": selected["status"] if selected else "unknown",
            "candidates": unique, "catalogue_version": catalogue.metadata["version"],
            "full_paper_verified": False}


def matches_filters(classification: dict, options: SearchOptions) -> bool:
    if not options.ccf_ranks and not options.conferences:
        return True
    accepted_statuses = {"verified", "author_reported", "publication_record"}
    if options.evidence == "mentions":
        accepted_statuses.add("mentioned")
    candidates = [c for c in classification["candidates"] if c["status"] in accepted_statuses]
    rank_match = not options.ccf_ranks or any(c["rank"] in options.ccf_ranks for c in candidates)
    if "unknown" in options.ccf_ranks and not any(c["status"] in {"verified", "author_reported", "publication_record"} for c in classification["candidates"]):
        rank_match = True
    if not rank_match:
        return False
    # Both filters must be satisfied by the SAME conference record.
    if options.conferences:
        return any(c["conference_id"] in options.conferences and
                   (not options.ccf_ranks or c["rank"] in options.ccf_ranks) for c in candidates)
    return rank_match


def normalized_title(value: str) -> str:
    value = html.unescape(unicodedata.normalize("NFKD", value)).lower()
    return " ".join(re.findall(r"[a-z0-9]+", value))


def surname(value: str) -> str:
    normalized = normalized_title(re.sub(r"\s+\d{4}$", "", value))
    return normalized.split()[-1] if normalized else ""


def choose_dblp_record(paper: dict, payload: dict) -> dict | None:
    hits = payload.get("result", {}).get("hits", {}).get("hit", [])
    if isinstance(hits, dict):
        hits = [hits]
    matches = []
    for hit in hits:
        info = hit.get("info", {})
        if normalized_title(info.get("title", "")) != normalized_title(paper["title"]):
            continue
        if info.get("type") != "Conference and Workshop Papers":
            continue
        authors = info.get("authors", {}).get("author", [])
        if isinstance(authors, (str, dict)):
            authors = [authors]
        names = [author.get("text", "") if isinstance(author, dict) else author for author in authors]
        expected = {surname(name) for name in paper.get("authors", [])} - {""}
        observed = {surname(name) for name in names} - {""}
        # Reject title-only homonyms. Require first-author identity and >=2
        # surname matches for multi-author papers; remain conservative.
        required = min(2, len(expected))
        if not expected or not names or surname(paper["authors"][0]) != surname(names[0]):
            continue
        if len(expected & observed) < required:
            continue
        venue = info.get("venue", "")
        if isinstance(venue, list):
            venue = " / ".join(venue)
        record = {key: info.get(key, "") for key in ("title", "year", "key", "url")}
        record["venue"] = venue
        record["checked_at"] = datetime.now(timezone.utc).isoformat()
        matches.append(record)
    # A title matching multiple different main venues is ambiguous; do not
    # choose the first hit and accidentally attach the wrong grade.
    main = [m for m in matches if not NON_MAIN.search(m["venue"]) and
            re.fullmatch(r"conf/[^/]+/[^/]+", m["key"])]
    if len({m["key"] for m in main}) == 1:
        return main[0]
    return None


class DblpVerifier:
    def __init__(self, transport):
        self.transport = transport

    def verify(self, paper: dict, cancel=None) -> dict | None:
        check_cancel(cancel)
        url = "https://dblp.org/search/publ/api?" + urlencode({
            "q": normalized_title(paper["title"]), "format": "json", "h": 10,
        })
        data, _ = self.transport.get(url, cancel, ttl=7 * 86400)
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeDecodeError) as exc:
            raise RemoteError("DBLP 返回了无效 JSON。") from exc
        return choose_dblp_record(paper, payload)
