"""Contract and integration checks using explicitly synthetic Scholar records."""

import csv
from contextlib import closing
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from arxiv_demo.client import Cancelled, RemoteError
from arxiv_demo.conferences import classify
from arxiv_demo.exports import export_search
from arxiv_demo.query import SearchOptions, ValidationError
from arxiv_demo.scholar import (ScholarClient, arxiv_id_from_url, normalize_page, normalize_record,
                               parse_import, safe_url, scholar_parameters, scholar_url)
from arxiv_demo.service import Job, Service

ROOT = Path(__file__).resolve().parents[1]


def record(identifier="testA", venue="NeurIPS 2024", title="Synthetic test paper"):
    return {"result_id": identifier, "title": title, "link": "https://example.org/paper",
            "snippet": "This is a synthetic test snippet, not a real paper.",
            "publication_info": {"summary": f"A Smith, B Jones - {venue} - example.org"},
            "inline_links": {"cited_by": {"total": 0, "link": "https://scholar.google.com/scholar?cites=123"},
                             "versions": {"total": 3, "link": "https://scholar.google.com/scholar?cluster=123"}}}


SAVED_HTML = '''<!doctype html><html><body>
<script>window.fakeSecret="do not execute";</script>
<div class="gs_r gs_or gs_scl" data-cid="testHtml">
 <div class="gs_or_ggsm"><a href="https://arxiv.org/pdf/2401.00001.pdf">[PDF]</a></div>
 <div class="gs_ri"><h3 class="gs_rt"><a href="https://example.org/paper">Synthetic &amp; safe paper</a></h3>
 <div class="gs_a"><a href="/citations?user=test">A Smith</a>, B Jones - ICML 2024 - example.org</div>
 <div class="gs_rs">Mentions EMNLP 2024 only as related work.</div>
 <div class="gs_fl"><a href="/scholar?cites=123">Cited by 1,234</a>
 <a href="/scholar?q=related:test:scholar.google.com/">Related articles</a>
 <a href="/scholar?cluster=123">All 5 versions</a></div></div>
</div></body></html>'''


class ScholarQueryTests(unittest.TestCase):
    def test_query_years_and_keywords(self):
        options = SearchOptions.parse({"source": "google_scholar", "keywords": '"federated learning" privacy',
                                       "field": "title", "match": "any", "year_from": "2020", "year_to": "2026"})
        params = scholar_parameters(options)
        self.assertEqual(params["q"], 'intitle:"federated learning" OR intitle:"privacy"')
        self.assertEqual(params["as_ylo"], "2020")
        self.assertIn("as_yhi=2026", scholar_url(options))
        self.assertNotIn("ccf", scholar_url(options))

    def test_invalid_or_incompatible_options(self):
        for change in ({"year_from": "202x"}, {"year_from": "2026", "year_to": "2020"},
                       {"date_from": "2025-01-01"}, {"categories": ["cs.LG"]},
                       {"field": "abstract"}, {"sort_by": "submittedDate"}, {"scan_limit": 1001}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                SearchOptions.parse({"source": "google_scholar", "keywords": "learning", **change})

    def test_unknown_provider_rejected(self):
        with self.assertRaises(ValidationError):
            SearchOptions.parse({"source": "fake", "keywords": "learning"})


class ScholarRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(ROOT, data_dir=Path(self.temp.name) / "data", download_dir=Path(self.temp.name) / "downloads")

    def tearDown(self):
        self.temp.cleanup()

    def test_publication_venue_and_snippet_are_separate(self):
        p = normalize_record(record())
        self.assertEqual(p["publication_year"], "2024")
        self.assertEqual(p["published"], "")
        self.assertEqual(p["abstract_kind"], "snippet")
        ccf = classify(p, self.service.catalogue)
        self.assertEqual(ccf["rank"], "A")
        self.assertEqual(ccf["status"], "publication_record")
        self.assertFalse(ccf["full_paper_verified"])
        self.assertEqual(p["citations"], 0)

    def test_mentions_in_title_or_snippet_do_not_assign_venue(self):
        r = record(venue="Unknown venue 2024", title="NeurIPS methods")
        r["snippet"] = "Accepted at ICML 2024 appears only as related work."
        self.assertIsNone(classify(normalize_record(r), self.service.catalogue)["rank"])

    def test_findings_workshop_and_submissions_excluded(self):
        for venue in ("Findings of EMNLP 2024", "ICML Workshop 2024", "Submitted to ICLR 2024"):
            with self.subTest(venue=venue):
                ccf = classify(normalize_record(record(venue=venue)), self.service.catalogue)
                self.assertEqual(ccf["status"], "excluded")
                self.assertIsNone(ccf["rank"])

    def test_missing_year_is_not_guessed(self):
        p = normalize_record({"title": "Synthetic paper", "publication_info": {"summary": "A Smith - ICML - example.org"}})
        self.assertEqual(p["publication_year"], "")
        self.assertIsNone(classify(p, self.service.catalogue)["rank"])

    def test_import_saved_html_without_executing_scripts(self):
        page = parse_import(SAVED_HTML)
        self.assertEqual(len(page["papers"]), 1)
        p = page["papers"][0]
        self.assertEqual(p["title"], "Synthetic & safe paper")
        self.assertEqual(p["publication_year"], "2024")
        self.assertEqual(p["citations"], 1234)
        self.assertEqual(p["versions_count"], 5)
        self.assertEqual(p["arxiv_id"], "2401.00001")
        self.assertEqual(classify(p, self.service.catalogue)["conference"], "ICML")
        self.assertIn("related", p["related_url"])

    def test_invalid_import_and_challenge_page(self):
        for content in ('{"bad": true}', "<html>Verify you are human</html>", "{" * 100, "x" * (1024 * 1024 + 1)):
            with self.subTest(content=content[:25]), self.assertRaises((ValidationError, RemoteError)):
                parse_import(content)

    def test_import_dedup_year_and_ccf_filters(self):
        records = [record(), record(), record("b", "EMNLP 2024", "Synthetic B"), record("old", "NeurIPS 2020", "Synthetic old")]
        result = self.service.import_scholar({"content": json.dumps({"organic_results": records}),
            "options": {"keywords": "learning", "year_from": "2024", "ccf_ranks": ["A"]}})
        self.assertEqual(len(result["papers"]), 1)
        self.assertEqual(result["source"], "scholar_import")
        self.assertEqual(self.service.store.history()[0]["source"], "scholar_import")
        self.assertIsNone(result["total_remote"])

    def test_exports_do_not_label_scholar_as_arxiv_or_make_up_a_date(self):
        result = self.service.import_scholar({"content": json.dumps([record()])})
        rows = list(csv.DictReader(io.StringIO(export_search(result, "csv")[0].decode("utf-8-sig"))))
        self.assertEqual(rows[0]["source"], "google_scholar")
        self.assertEqual(rows[0]["arxiv_id"], "")
        self.assertEqual(rows[0]["published_utc"], "")
        self.assertEqual(rows[0]["publication_year"], "2024")
        self.assertEqual(rows[0]["citations"], "0")
        bib = export_search(result, "bibtex")[0].decode()
        self.assertNotIn("archivePrefix", bib)
        self.assertNotIn("booktitle", bib)
        self.assertIn("year = {2024}", bib)

    def test_non_arxiv_download_is_rejected_before_queue(self):
        result = self.service.import_scholar({"content": json.dumps([record()])})
        with self.assertRaises(ValidationError):
            self.service.submit_download({"search_id": result["id"], "ids": [result["papers"][0]["id"]]})

    def test_arxiv_only_download_keeps_scholar_id(self):
        result = self.service.import_scholar({"content": SAVED_HTML})
        calls = []
        def download(identifier, target, cancel):
            calls.append((identifier, target.name))
            return {"status": "downloaded", "id": identifier, "filename": target.name}
        self.service.transport.download = download
        outcome = self.service.download(result["papers"], Job("download"))
        self.assertEqual(calls, [("2401.00001", "2401.00001.pdf")])
        self.assertEqual(outcome["files"][0]["id"], result["papers"][0]["id"])

    def test_unsafe_links_and_credentials_removed(self):
        for url in ("javascript:alert(1)", "file:///C:/secret", "http://localhost:8000/", "https://127.0.0.1/", "https://x:y@example.org/", "https://serpapi.com/search?api_key=secret"):
            self.assertEqual(safe_url(url), "")
        self.assertEqual(arxiv_id_from_url("https://example.org/pdf/2401.00001"), "")
        self.assertEqual(arxiv_id_from_url("https://arxiv.org/pdf/cs/9901001v2.pdf"), "cs/9901001v2")


class ScholarClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(ROOT, data_dir=Path(self.temp.name) / "data")
        self.client = ScholarClient(self.service.store, Path(self.temp.name))
        self.options = SearchOptions.parse({"source": "google_scholar", "keywords": "learning"})
        self.client.gate.interval = 0

    def tearDown(self):
        self.temp.cleanup()

    def test_api_key_never_enters_public_config_or_cache(self):
        payload = {"organic_results": [record()], "search_parameters": {"api_key": "test-key-secret"},
                   "search_information": {"total_results": 1}}
        request_urls = []
        def respond(req, timeout):
            request_urls.append(req.full_url)
            return io.BytesIO(json.dumps(payload).encode())
        self.client.opener.open = respond
        with mock.patch.dict("os.environ", {"SERPAPI_API_KEY": "test-key-secret"}):
            page, cached = self.client.page(self.options, 0, 20)
            self.assertFalse(cached)
            page2, cached2 = self.client.page(self.options, 0, 20)
            self.assertTrue(cached2)
            self.assertEqual(page, page2)
            self.assertTrue(self.client.public()["configured"])
            self.assertNotIn("test-key-secret", json.dumps(self.client.public()))
        with closing(self.service.store.connect()) as db:
            data = db.execute("SELECT key,value FROM cache").fetchall()
        self.assertNotIn("test-key-secret", str(data))
        self.assertEqual(len(request_urls), 1)

    def test_missing_key_clear_error(self):
        with mock.patch.dict("os.environ", {"SERPAPI_API_KEY": ""}), self.assertRaises(ValidationError):
            self.client.page(self.options, 0, 20)

    def test_error_text_cannot_leak_key(self):
        with self.assertRaises(RemoteError) as error:
            normalize_page({"error": "https://serpapi.com?api_key=secret"})
        self.assertNotIn("secret", str(error.exception))

    def test_cancel_before_request(self):
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(Cancelled):
            self.client.page(self.options, 0, 20, cancel)

    def test_http_error_is_sanitized(self):
        self.client.opener.open = mock.Mock(side_effect=urllib.error.HTTPError("https://serpapi.com?api_key=secret", 429, "quota", {}, io.BytesIO()))
        with mock.patch.dict("os.environ", {"SERPAPI_API_KEY": "secret"}), self.assertRaises(RemoteError) as error:
            self.client.page(self.options, 0, 20)
        self.assertIn("429", str(error.exception))
        self.assertNotIn("secret", str(error.exception))

    def test_pagination_filtering_and_estimated_total(self):
        first = normalize_page({"organic_results": [record("b", "EMNLP 2024", "Synthetic B")],
            "search_information": {"total_results": 1000}, "serpapi_pagination": {"next": "ignored"}})
        second = normalize_page({"organic_results": [record()], "search_information": {"total_results": 1000}})
        self.service.scholar.page = mock.Mock(side_effect=[(first, False), (second, True)])
        options = SearchOptions.parse({"source": "google_scholar", "keywords": "learning", "ccf_ranks": ["A"], "max_results": 1, "scan_limit": 20})
        result = self.service.search(options, Job("search"))
        self.assertEqual(result["scanned"], 2)
        self.assertEqual(len(result["papers"]), 1)
        self.assertEqual(result["papers"][0]["ccf"]["rank"], "A")
        self.assertEqual(result["cache_pages"], 1)
        self.assertEqual(self.service.scholar.page.call_args_list[1].args[1], 1)

    def test_repeated_pages_stop(self):
        page = normalize_page({"organic_results": [record()], "search_information": {"total_results": 1000}, "serpapi_pagination": {"next": "ignored"}})
        self.service.scholar.page = lambda *args: (page, False)
        with self.assertRaises(RemoteError):
            self.service.search(SearchOptions(source="google_scholar", keywords="x", max_results=2, scan_limit=20), Job("search"))


if __name__ == "__main__":
    unittest.main()
