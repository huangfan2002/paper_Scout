"""Search, export and optionally download without opening the web UI."""

import argparse
import json
import sys
from pathlib import Path

from arxiv_demo.client import Cancelled, RemoteError
from arxiv_demo.exports import export_search
from arxiv_demo.query import ValidationError
from arxiv_demo.service import Job, Service
from arxiv_demo.scholar import scholar_url


def main():
    parser = argparse.ArgumentParser(description="arXiv / Google Scholar / CCF 文献检索命令行")
    parser.add_argument("--source", choices=["arxiv", "google_scholar"], default="arxiv")
    parser.add_argument("--keywords", default="")
    parser.add_argument("--field", choices=["all", "title", "abstract", "author"], default="all")
    parser.add_argument("--match", choices=["all", "any"], default="all")
    parser.add_argument("--date-from", default="")
    parser.add_argument("--date-to", default="")
    parser.add_argument("--year-from", default="", help="Scholar 起始发表年份")
    parser.add_argument("--year-to", default="", help="Scholar 结束发表年份")
    parser.add_argument("--scholar-language", choices=["en", "zh-CN"], default="en")
    parser.add_argument("--scholar-link", action="store_true", help="只输出 Google Scholar 检索链接，无需 Key")
    parser.add_argument("--import-scholar", type=Path, help="导入已保存的 Scholar HTML / SerpApi JSON（UTF-8，1 MB 内）")
    parser.add_argument("--categories", nargs="+", default=[])
    parser.add_argument("--ccf", nargs="+", choices=["A", "B", "C", "unknown"], default=[])
    parser.add_argument("--conferences", nargs="+", default=[], help="会议简称（以目录列表为准）")
    parser.add_argument("--evidence", choices=["strict", "mentions"], default="strict")
    parser.add_argument("--max-results", type=int, default=30)
    parser.add_argument("--scan-limit", type=int, default=300)
    parser.add_argument("--sort-by", choices=["submittedDate", "lastUpdatedDate", "relevance"], default=None)
    parser.add_argument("--sort-order", choices=["ascending", "descending"], default="descending")
    parser.add_argument("--verify-dblp", action="store_true")
    parser.add_argument("--dblp-limit", type=int, default=30)
    parser.add_argument("--format", choices=["json", "csv", "bibtex"], default="json")
    parser.add_argument("--output", type=Path, help="导出文件路径；默认在 exports/ 下按结果 ID 保存")
    parser.add_argument("--download", action="store_true", help="下载全部结果（每次最多 100 篇）")
    parser.add_argument("--download-dir", type=Path)
    parser.add_argument("--sample", action="store_true", help="加载离线样例；不执行网络检索")
    parser.add_argument("--list-conferences", action="store_true", help="输出 CCF 会议目录")
    parser.add_argument("--major", default="", help="大方向 ID，例如 AI、SC、DS（目录和截止日期）")
    parser.add_argument("--subfield", default="", help="小方向 ID，例如 AI:ml、SC:crypto")
    parser.add_argument("--deadlines", action="store_true", help="显示会议截止日期，无需 API Key")
    parser.add_argument("--refresh-deadlines", action="store_true", help="同步 CCFDDL 官方站点的最新快照")
    parser.add_argument("--deadline-status", choices=["upcoming", "past", "tbd", "all"], default="upcoming")
    parser.add_argument("--deadline-stage", choices=["deadline", "abstract_deadline", "rebuttal_deadline", "decision_deadline"], default="deadline")
    parser.add_argument("--deadline-year", default="", help="会议举办年份，例如 2027")
    parser.add_argument("--deadline-page", type=int, default=1, help="截止日期结果页，每页最多 100 条")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    service = Service(root, download_dir=args.download_dir.resolve() if args.download_dir else None)
    if args.refresh_deadlines or args.deadlines:
        try:
            if args.refresh_deadlines:
                service.deadlines.refresh(Job("deadlines"))
            data = service.deadlines.search({"major": args.major, "subfield": args.subfield,
                "rank": ",".join("N" if r == "unknown" else r for r in args.ccf), "q": args.keywords,
                "status": args.deadline_status, "stage": args.deadline_stage, "year": args.deadline_year,
                "page": args.deadline_page, "limit": 100})
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        except (RemoteError, ValidationError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.list_conferences:
        for venue in service.catalogue.venues:
            if (not args.ccf or venue["rank"] in args.ccf) and (not args.major or venue["major_id"] == args.major) and (not args.subfield or venue["subfield_id"] == args.subfield):
                print(f"{venue['major_name']} / {venue['subfield_name']}\tCCF {venue['rank']}\t{venue['acronym']}\t{venue['name']}")
        return 0
    options_payload = vars(args).copy()
    if args.import_scholar or args.scholar_link:
        options_payload["source"] = "google_scholar"
    options_payload["sort_by"] = args.sort_by or ("relevance" if options_payload["source"] == "google_scholar" else "submittedDate")
    options_payload["ccf_ranks"] = args.ccf
    aliases = {v["acronym"].casefold(): v["id"] for v in service.catalogue.venues}
    try:
        conference_ids = []
        for name in args.conferences:
            if name.casefold() not in aliases:
                raise ValidationError(f"会议 {name} 不在目录中，请用 --list-conferences 查看。")
            conference_ids.append(aliases[name.casefold()])
        options_payload["conferences"] = conference_ids
        if args.sample:
            result = service.scholar_sample() if args.source == "google_scholar" else service.sample()
        elif args.scholar_link:
            print(scholar_url(service.options(options_payload)))
            return 0
        elif args.import_scholar:
            if args.import_scholar.stat().st_size > 1024 * 1024:
                raise ValidationError("导入文件不能超过 1 MB。")
            result = service.import_scholar({"content": args.import_scholar.read_text(encoding="utf-8-sig"), "options": options_payload})
        else:
            options = service.options(options_payload)
            job = Job("search")
            print("正在检索 " + ("Google Scholar（SerpApi）…" if options.source == "google_scholar" else "arXiv，请求间隔至少 3.1 秒…"), flush=True)
            result = service.search(options, job)
        content, _, extension = export_search(result, args.format)
        prefix = "scholar" if result["source"] in {"google_scholar", "scholar_import", "scholar_sample"} else "arxiv"
        output = args.output or root / "exports" / f"{prefix}_{result['id'][:8]}.{extension}"
        output = output.resolve()
        if output.exists():
            raise ValidationError(f"导出文件已存在：{output}，请使用其他文件名。")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
        print(f"找到 {len(result['papers'])} 篇；扫描 {result['scanned']} 条候选。\n导出：{output}")
        for warning in result["warnings"]:
            print("提示：" + warning)
        for paper in result["papers"][:10]:
            ccf = paper["ccf"]
            print(f"[{ccf['rank'] or '未知'}] {paper['id']} {paper['title']}")
        if args.download:
            if len(result["papers"]) > 100:
                raise ValidationError("命令行单批下载最多 100 篇，请降低 --max-results。")
            available = [p for p in result["papers"] if p.get("source") != "google_scholar" or p.get("arxiv_id")]
            if len(available) < len(result["papers"]):
                print("其他来源请打开导出文件中的出版页面、PDF 或其他版本链接；批量下载支持 arXiv PDF。")
            downloads = service.download(available, Job("download"))
            print(json.dumps(downloads, ensure_ascii=False, indent=2))
            return 1 if downloads["failed"] else 0
    except (ValidationError, RemoteError, Cancelled, OSError, UnicodeDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中止。已完成的 PDF 保留。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
