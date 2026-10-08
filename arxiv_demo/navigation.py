"""Editable research navigation; the official CCF catalogue stays authoritative."""
from __future__ import annotations

import json
from pathlib import Path


def load_navigation(path: Path, venues: list[dict]) -> dict:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"metadata": {}, "majors": []}
    majors = data["majors"]
    by_id = {v["id"]: v for v in venues}
    assigned = set()
    major_ids, subfield_ids = set(), set()
    for major in majors:
        if major["id"] in major_ids:
            raise ValueError("会议导航的大方向 ID 重复。")
        major_ids.add(major["id"])
        for group in major["subfields"]:
            if group["id"] in subfield_ids:
                raise ValueError("会议导航的小方向 ID 重复。")
            subfield_ids.add(group["id"])
            group["venue_ids"] = [key for key in group["venue_ids"] if key in by_id]
            for key in group["venue_ids"]:
                if key in assigned or by_id[key]["domain"] != major["domain"]:
                    raise ValueError("会议导航存在重复归类或跨学科归类：" + key)
                assigned.add(key)
    # Custom/new conferences remain visible even before a topic is assigned.
    for venue in venues:
        if venue["id"] in assigned:
            continue
        major = next((m for m in majors if m["domain"] == venue["domain"]), None)
        if major is None:
            key = "custom-" + str(len(majors) + 1)
            major = {"id": key, "name": venue["domain"], "domain": venue["domain"], "subfields": []}
            majors.append(major)
        group_id = major["id"] + ":other"
        group = next((s for s in major["subfields"] if s["id"] == group_id), None)
        if group is None:
            group = {"id": group_id, "name": "综合 / 待归类", "venue_ids": []}
            major["subfields"].append(group)
        group["venue_ids"].append(venue["id"])
    for major in majors:
        major["count"] = 0
        for group in major["subfields"]:
            group["count"] = len(group["venue_ids"])
            major["count"] += group["count"]
            for key in group["venue_ids"]:
                by_id[key].update(major_id=major["id"], major_name=major["name"],
                                  subfield_id=group["id"], subfield_name=group["name"])
    return data
