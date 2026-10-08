"""Lossless JSON, spreadsheet-safe CSV and arXiv-preprint BibTeX exports."""

import csv
import io
import json
import re


def csv_cell(value):
    text = "" if value is None else str(value)
    # Paper metadata is untrusted; opening a CSV must not execute formulas.
    if text.lstrip().startswith(("=", "+", "-", "@")):
        text = "'" + text
    return text


def tex(value: str) -> str:
    escapes = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "&": r"\&", "%": r"\%", "#": r"\#", "_": r"\_", "$": r"\$", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(escapes.get(c, c) for c in str(value))


def export_search(search: dict, format_name: str) -> tuple[bytes, str, str]:
    papers = search["papers"]
    if format_name == "json":
        return json.dumps(search, ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8", "json"
    if format_name == "csv":
        fields = ["arxiv_id", "title", "authors", "published_utc", "updated_utc", "categories",
                  "ccf_rank", "conference", "evidence_status", "evidence", "paper_type",
                  "full_paper_verified", "catalogue_version", "doi", "abstract_url", "pdf_url", "abstract",
                  "source", "paper_id", "publication_year", "publication_summary", "citations", "versions_count",
                  "cited_by_url", "related_url", "versions_url", "abstract_kind"]
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(fields)
        for p in papers:
            c = p["ccf"]
            best = c["candidates"][0] if c["candidates"] else {}
            source = p.get("source", "arxiv")
            values = [p.get("arxiv_id", "") if source == "google_scholar" else p["id"], p["title"], "; ".join(p["authors"]), p["published"], p["updated"],
                      "; ".join(p["categories"]), c["rank"] or "unknown", c["conference"], c["status"],
                      best.get("evidence", ""), best.get("paper_type", ""), "false",
                      c["catalogue_version"], p["doi"], p["abstract_url"], p["pdf_url"], p["abstract"], source, p["id"],
                      p.get("publication_year", ""), p.get("publication_summary", ""), p.get("citations", ""), p.get("versions_count", ""),
                      p.get("cited_by_url", ""), p.get("related_url", ""), p.get("versions_url", ""), p.get("abstract_kind", "full_abstract")]
            writer.writerow([csv_cell(v) for v in values])
        return ("\ufeff" + stream.getvalue()).encode("utf-8"), "text/csv; charset=utf-8", "csv"
    if format_name == "bibtex":
        entries = []
        for p in papers:
            scholar = p.get("source") == "google_scholar"
            key = ("scholar" if scholar else "arxiv") + re.sub(r"[^a-zA-Z0-9]", "", p["id"])
            # arXiv metadata cannot supply a complete conference citation. Do
            # not fabricate booktitle, pages, DOI or proceedings publisher.
            fields = {"title": p["title"], "author": " and ".join(p["authors"]),
                      "year": p["published"][:4], "eprint": p["base_id"], "archivePrefix": "arXiv",
                      "primaryClass": p["primary_category"], "url": p["abstract_url"]}
            if scholar:
                fields = {"title": p["title"], "author": " and ".join(p["authors"]),
                          "year": p.get("publication_year", ""), "url": p["abstract_url"],
                          "note": "Google Scholar search record; bibliographic fields require verification"}
            lines = [f"  {name} = {{{tex(value)}}}" for name, value in fields.items() if value]
            entries.append("@misc{" + key + ",\n" + ",\n".join(lines) + "\n}")
        return ("\n\n".join(entries) + "\n").encode("utf-8"), "application/x-bibtex; charset=utf-8", "bib"
    raise ValueError("导出格式只支持 csv、json、bibtex。")
