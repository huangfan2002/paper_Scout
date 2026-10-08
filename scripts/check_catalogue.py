"""Check an edited catalogue before starting the demo. Standard library only."""

import json
import sys
from collections import Counter
from pathlib import Path


def main():
    path = Path(__file__).resolve().parents[1] / "config" / "ccf_conferences.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["metadata"]["version"], "metadata.version 缺失"
        ids = set()
        for item in data["conferences"]:
            assert item["id"] not in ids, f"重复 ID: {item['id']}"
            ids.add(item["id"])
            assert item["rank"] in "ABC" and len(item["rank"]) == 1, f"无效等级: {item['id']}"
            assert item["acronym"] and item["name"] and item["domain"], f"必要字段缺失: {item['id']}"
            for key in ("aliases", "dblp_keys"):
                assert isinstance(item.get(key, []), list) and all(isinstance(x, str) for x in item.get(key, [])), f"{key} 应为文本列表"
        counts = Counter(v["rank"] for v in data["conferences"])
        print(f"目录有效：{data['metadata']['version']}")
        print(f"会议数 {len(ids)}；A {counts['A']} / B {counts['B']} / C {counts['C']}")
        return 0
    except (ValueError, KeyError, AssertionError, OSError, TypeError) as exc:
        print(f"目录检查失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
