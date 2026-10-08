"""Offline tests for query syntax, CCF evidence rules, paging and exports."""

import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from arxiv_demo.client import Cancelled, RateGate, RemoteError, parse_feed
from arxiv_demo.conferences import Catalogue, choose_dblp_record, classify, matches_filters
from arxiv_demo.exports import export_search
from arxiv_demo.query import SearchOptions, ValidationError, build_query
from arxiv_demo.service import Job, Service
from arxiv_demo.storage import Store

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = Catalogue(ROOT / "config" / "ccf_conferences.json")


def paper(identifier="2401.00001v1", comment="", journal="", title="Example Research Paper"):
    return {"id": identifier, "base_id": identifier.split("v")[0], "title": title,
            "authors": ["Alice Smith", "Bob Jones"], "abstract": "An example abstract.",
            "published": "2024-01-01T12:00:00Z", "updated": "2025-01-01T12:00:00Z",
            "categories": ["cs.LG"], "primary_category": "cs.LG", "comment": comment,
            "journal_ref": journal, "doi": "", "abstract_url": f"https://arxiv.org/abs/{identifier}",
            "pdf_url": f"https://arxiv.org/pdf/{identifier}"}


class QueryTests(unittest.TestCase):
    def test_phrase_and_dates(self):
        options = SearchOptions.parse({"keywords": '"federated learning" privacy', "date_from": "2024-01-01", "date_to": "2024-12-31"})
        self.assertEqual(build_query(options), '(all:"federated learning" AND all:"privacy") AND submittedDate:[202401010000 TO 202412312359]')

    def test_or_categories(self):
        options = SearchOptions.parse({"keywords": "privacy security", "match": "any", "field": "title", "categories": ["cs.CR", "cs.LG"]})
        self.assertEqual(build_query(options), '(ti:"privacy" OR ti:"security") AND (cat:cs.CR OR cat:cs.LG)')

    def test_one_sided_dates(self):
        self.assertIn("199101010000 TO 202501012359", build_query(SearchOptions.parse({"date_to": "2025-01-01"})))
        self.assertIn("202501010000 TO 209912312359", build_query(SearchOptions.parse({"date_from": "2025-01-01"})))

    def test_plain_keywords_do_not_inject_query(self):
        query = build_query(SearchOptions.parse({"keywords": 'test OR cat:cs.CV'}))
        self.assertNotIn("OR cat:cs.CV", query)
        self.assertIn('all:"cat cs.CV"', query)

    def test_invalid_inputs(self):
        inputs = [None, [], {}, {"keywords": '"unfinished'}, {"keywords": "a", "date_from": "2024-02-30"},
                  {"keywords": "a", "date_from": "2025-01-01", "date_to": "2024-01-01"},
                  {"keywords": "a", "max_results": 501}, {"keywords": "a", "max_results": 50, "scan_limit": 10},
                  {"keywords": "a", "categories": ["cs.LG OR all:secret"]},
                  {"keywords": "a", "ccf_ranks": ["D"]}, {"keywords": "a", "verify_dblp": "yes"},
                  {"keywords": "a", "max_results": True}, {"keywords": "a", "max_results": 1.5},
                  {"keywords": "a", "categories": "cs.LG"}, {"keywords": "a", "date_from": "20240101"}]
        for payload in inputs:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                SearchOptions.parse(payload)

    def test_unknown_conference_rejected(self):
        with self.assertRaises(ValidationError):
            SearchOptions.parse({"keywords": "a", "conferences": ["fake"]}, set(CATALOGUE.by_id))


class ConferenceTests(unittest.TestCase):
    def test_catalogue_pdf_counts(self):
        self.assertEqual(CATALOGUE.public()["counts"], {"A": 58, "B": 132, "C": 196})
        self.assertEqual(len(CATALOGUE.by_id), 386)
        self.assertTrue(all(v["source_pdf_page"] for v in CATALOGUE.venues))

    def test_2026_changes(self):
        ranks = {v["acronym"]: v["rank"] for v in CATALOGUE.venues}
        self.assertEqual(ranks["ICLR"], "A")
        self.assertEqual(ranks["IJCAI"], "B")
        self.assertEqual(ranks["HPDC"], "A")

    def test_acceptance_all_three_ranks(self):
        for name, rank in [("NeurIPS", "A"), ("EMNLP", "B"), ("AISTATS", "C")]:
            with self.subTest(name=name):
                result = classify(paper(comment=f"Accepted at {name} 2026"), CATALOGUE)
                self.assertEqual(result["rank"], rank)
                self.assertEqual(result["status"], "author_reported")
                self.assertFalse(result["full_paper_verified"])

    def test_aliases(self):
        for name, expected in [("NIPS", "NeurIPS"), ("ACM Multimedia", "ACM MM"), ("KDD", "SIGKDD"),
                               ("IEEE Symposium on Security and Privacy", "S&P"), ("USENIX ATC", "ACM SIGOPS ATC")]:
            with self.subTest(name=name):
                result = classify(paper(comment=f"Accepted at {name} 2025"), CATALOGUE)
                self.assertEqual(result["conference"], expected)
                self.assertEqual(result["rank"], "A")

    def test_submission_is_not_acceptance(self):
        for comment in ["Submitted to NeurIPS 2026", "Under review at ICLR 2027", "Under submission at AISTATS 2027",
                        "Rejected from CVPR 2026", "Not accepted at CVPR 2026", "Not yet accepted at NeurIPS 2026",
                        "Withdrawn from ICML 2026", "Not to appear in NeurIPS 2026"]:
            with self.subTest(comment=comment):
                ccf = classify(paper(comment=comment), CATALOGUE)
                self.assertIsNone(ccf["rank"])
                self.assertEqual(ccf["status"], "excluded")

    def test_workshop_findings_short_excluded(self):
        for comment in ["Accepted to Findings of ACL 2026", "Accepted to EMNLP 2026 (Findings)",
                        "Accepted at the NeurIPS 2026 Workshop", "Accepted as a short paper at CVPR 2026",
                        "Demo paper accepted at WWW 2026", "Accepted at ICLR 2026, non-archival workshop"]:
            with self.subTest(comment=comment):
                ccf = classify(paper(comment=comment), CATALOGUE)
                self.assertIsNone(ccf["rank"])
                self.assertEqual(ccf["status"], "excluded")

    def test_main_declared_and_poster_allowed(self):
        for comment in ["Accepted to the main conference of EMNLP 2026", "Accepted to EMNLP 2026 (Main Conference)", "Accepted to NeurIPS 2026 as a poster"]:
            ccf = classify(paper(comment=comment), CATALOGUE)
            self.assertIsNotNone(ccf["rank"])

    def test_title_and_abstract_do_not_create_conference_evidence(self):
        p = paper(title="A Survey of NeurIPS and ICML Papers")
        p["abstract"] = "We evaluate benchmarks from CVPR, NeurIPS, and ACL."
        self.assertEqual(classify(p, CATALOGUE)["status"], "unknown")

    def test_mention_mode_and_unknown(self):
        ccf = classify(paper(comment="NeurIPS 2026"), CATALOGUE)
        self.assertIsNone(ccf["rank"])
        strict = SearchOptions(keywords="x", ccf_ranks=["A"])
        mentions = SearchOptions(keywords="x", ccf_ranks=["A"], evidence="mentions")
        self.assertFalse(matches_filters(ccf, strict))
        self.assertTrue(matches_filters(ccf, mentions))
        self.assertTrue(matches_filters(ccf, SearchOptions(keywords="x", ccf_ranks=["unknown"])))

    def test_rejection_does_not_override_other_sentence(self):
        ccf = classify(paper(comment="Rejected from NeurIPS 2025. Accepted at EMNLP 2026."), CATALOGUE)
        self.assertEqual(ccf["rank"], "B")
        self.assertEqual(ccf["conference"], "EMNLP")

    def test_both_filters_must_match_same_venue(self):
        ccf = classify(paper(comment="Accepted at NeurIPS 2025. Accepted at EMNLP 2026."), CATALOGUE)
        emnlp = next(v["id"] for v in CATALOGUE.venues if v["acronym"] == "EMNLP")
        self.assertFalse(matches_filters(ccf, SearchOptions(keywords="x", ccf_ranks=["A"], conferences=[emnlp])))
        self.assertTrue(matches_filters(ccf, SearchOptions(keywords="x", ccf_ranks=["B"], conferences=[emnlp])))

    def test_journal_reference(self):
        self.assertEqual(classify(paper(journal="ICML 2025, Proceedings of Machine Learning Research"), CATALOGUE)["rank"], "A")

    def test_short_acronym_is_not_prose(self):
        self.assertEqual(classify(paper(comment="We propose a fast implementation with mm precision."), CATALOGUE)["status"], "unknown")

    def test_nested_conference_name_prefers_specific_venue(self):
        ccf = classify(paper(comment="Accepted at SIGGRAPH ASIA 2026"), CATALOGUE)
        self.assertEqual(ccf["status"], "unknown")
        self.assertIsNone(ccf["rank"])
        ccf = classify(paper(comment="Accepted at North American Chapter of the Association for Computational Linguistics 2026"), CATALOGUE)
        self.assertEqual(ccf["rank"], "B")

    def test_conference_adjacent_year(self):
        self.assertEqual(classify(paper(comment="Accepted by NeurIPS2026"), CATALOGUE)["rank"], "A")

    def test_dblp_main_record(self):
        p = paper()
        payload = {"result": {"hits": {"hit": [{"info": {"title": p["title"] + ".", "type": "Conference and Workshop Papers",
            "authors": {"author": [{"text": "Alice Smith"}, {"text": "Bob Jones"}]}, "venue": "NeurIPS", "year": "2024",
            "key": "conf/nips/SmithJones24", "url": "https://dblp.org/rec/conf/nips/SmithJones24"}}]}}}
        record = choose_dblp_record(p, payload)
        self.assertIsNotNone(record)
        ccf = classify(p, CATALOGUE, record)
        self.assertEqual(ccf["status"], "verified")
        self.assertEqual(ccf["rank"], "A")

    def test_dblp_author_mismatch_and_workshop(self):
        p = paper()
        payload = {"result": {"hits": {"hit": [{"info": {"title": p["title"], "type": "Conference and Workshop Papers",
            "authors": {"author": [{"text": "Someone Else"}]}, "venue": "NeurIPS", "key": "conf/nips/Else24"}}]}}}
        self.assertIsNone(choose_dblp_record(p, payload))
        record = {"key": "conf/nipsw/Smith24", "venue": "NeurIPS Workshop", "title": p["title"], "year": "2024"}
        self.assertEqual(classify(p, CATALOGUE, record)["status"], "unknown")

    def test_dblp_ambiguous_titles(self):
        p = paper()
        info = {"title": p["title"], "type": "Conference and Workshop Papers", "authors": {"author": ["Alice Smith", "Bob Jones"]}}
        payload = {"result": {"hits": {"hit": [{"info": {**info, "venue": "ICML", "key": "conf/icml/Smith24"}},
                                               {"info": {**info, "venue": "NeurIPS", "key": "conf/nips/Smith24"}}]}}}
        self.assertIsNone(choose_dblp_record(p, payload))


class FeedTests(unittest.TestCase):
    def test_old_and_new_arxiv_ids(self):
        for identifier in ["hep-th/9901001v2", "2401.12345v1", "1706.03762v7"]:
            xml = f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/{identifier}</id><title> A\n title </title><summary>Text</summary></entry></feed>'.encode()
            papers, total, _ = parse_feed(xml)
            self.assertEqual(papers[0]["id"], identifier)
            self.assertEqual(papers[0]["title"], "A title")
            self.assertEqual(total, 1)

    def test_api_error(self):
        xml = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors#incorrect_id_format</id><summary>Invalid ID</summary></entry></feed>'
        with self.assertRaisesRegex(RemoteError, "Invalid ID"):
            parse_feed(xml)

    def test_invalid_xml_html_and_dtd(self):
        for data in [b"invalid", b"<html>challenge</html>", b'<!DOCTYPE feed [<!ENTITY x "a">]><feed/>',
                     b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/../../secret</id></entry></feed>']:
            with self.subTest(data=data), self.assertRaises(RemoteError):
                parse_feed(data)


class StoreAndServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = Service(ROOT, data_dir=Path(self.temp.name) / "data", download_dir=Path(self.temp.name) / "downloads")

    def tearDown(self):
        self.temp.cleanup()

    def test_expired_cache_not_returned(self):
        self.service.store.cache("x", b"abc", -1)
        self.assertIsNone(self.service.store.cached("x"))
        self.service.store.cache("x", b"def", 30)
        self.assertEqual(self.service.store.cached("x"), b"def")

    def test_search_fills_matches_across_short_pages(self):
        pages = {0: [paper("2401.00001v1", "Submitted to NeurIPS 2024")],
                 1: [paper("2401.00002v1", "Accepted at EMNLP 2024")],
                 2: [paper("2401.00003v1", "Accepted at NeurIPS 2024")]}
        starts = []
        def page(query, start, count, *rest):
            starts.append(start)
            return copy.deepcopy(pages[start]), 3, True
        self.service.arxiv.page = page
        options = SearchOptions(keywords="privacy", ccf_ranks=["A"], max_results=1, scan_limit=5)
        result = self.service.search(options, Job("search"))
        self.assertEqual(starts, [0, 1, 2])
        self.assertEqual(result["papers"][0]["id"], "2401.00003v1")
        self.assertEqual(result["scanned"], 3)
        self.assertIsNotNone(self.service.store.search(result["id"]))

    def test_scan_limit_warns_and_stops(self):
        self.service.arxiv.page = lambda *args: ([paper(), paper("2401.00002v1")], 100, False)
        result = self.service.search(SearchOptions(keywords="x", ccf_ranks=["A"], max_results=1, scan_limit=2), Job("search"))
        self.assertTrue(result["scan_limited"])
        self.assertTrue(result["warnings"])

    def test_deduplication(self):
        pages = {0: [paper("2401.00001v1")], 1: [paper("2401.00001v2"), paper("2401.00002v1")]}
        self.service.arxiv.page = lambda query, start, *args: (pages[start], 3, True)
        result = self.service.search(SearchOptions(keywords="x", max_results=3, scan_limit=10), Job("search"))
        self.assertEqual(len(result["papers"]), 2)

    def test_unexpected_empty_page_is_failure(self):
        self.service.arxiv.page = lambda *args: ([], 100, False)
        with self.assertRaises(RemoteError):
            self.service.search(SearchOptions(keywords="x"), Job("search"))

    def test_cancel_before_network(self):
        job = Job("search"); job.cancel.set()
        with self.assertRaises(Cancelled):
            self.service.search(SearchOptions(keywords="x"), job)

    def test_dblp_failure_degrades_once(self):
        self.service.arxiv.page = lambda *args: ([paper(), paper("2401.00002v1")], 2, False)
        calls = []
        def verify(*args):
            calls.append(1)
            raise RemoteError("challenge page")
        self.service.dblp.verify = verify
        result = self.service.search(SearchOptions(keywords="x", max_results=2, verify_dblp=True), Job("search"))
        self.assertEqual(len(result["papers"]), 2)
        self.assertEqual(len(calls), 1)
        self.assertIn("DBLP", result["warnings"][0])

    def test_samples_are_explicit_and_persistent(self):
        result = self.service.sample()
        self.assertEqual(result["source"], "sample")
        self.assertTrue(result["warnings"])
        self.assertGreater(len(result["papers"]), 5)
        self.assertEqual(self.service.store.history()[0]["id"], result["id"])

    def test_download_selection_must_belong_to_search(self):
        result = self.service.sample()
        with self.assertRaises(ValidationError):
            self.service.submit_download({"search_id": result["id"], "ids": ["9999.99999v1"]})


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.paper = paper(comment="Accepted at NeurIPS 2024", title="Title & More_{things}")
        self.paper["ccf"] = classify(self.paper, CATALOGUE)
        self.search = {"id": "test", "papers": [self.paper]}

    def test_json_lossless(self):
        data, _, extension = export_search(self.search, "json")
        self.assertEqual(extension, "json")
        self.assertEqual(json.loads(data), self.search)

    def test_csv_utf8_bom_and_formula_safety(self):
        self.paper["title"] = '=HYPERLINK("danger")'
        data, _, extension = export_search(self.search, "csv")
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertIn("'=HYPERLINK", data.decode("utf-8"))
        self.assertEqual(extension, "csv")

    def test_bibtex_escapes_and_no_fabricated_proceedings(self):
        data, _, extension = export_search(self.search, "bibtex")
        content = data.decode()
        self.assertIn(r"Title \& More\_\{things\}", content)
        self.assertIn("archivePrefix = {arXiv}", content)
        self.assertNotIn("booktitle", content)
        self.assertEqual(extension, "bib")

    def test_invalid_format(self):
        with self.assertRaises(ValueError):
            export_search(self.search, "exe")


class GateTests(unittest.TestCase):
    def test_spacing_and_single_connection(self):
        gate = RateGate(0.02)
        intervals = []
        def action():
            start = time.perf_counter(); time.sleep(0.005); intervals.append((start, time.perf_counter()))
        threads = [threading.Thread(target=lambda: gate.execute(action)) for _ in range(3)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        intervals.sort()
        for previous, following in zip(intervals, intervals[1:]):
            self.assertGreaterEqual(following[0] - previous[1], 0.018)

    def test_cancellable_gate_wait(self):
        gate = RateGate(1)
        gate.execute(lambda: None)
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(Cancelled):
            gate.execute(lambda: None, cancel)


if __name__ == "__main__":
    unittest.main()
