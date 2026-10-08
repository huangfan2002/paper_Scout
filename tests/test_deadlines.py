"""Deadline clocks, conference identity, filters and snapshot durability (offline)."""
import copy
import io
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from arxiv_demo.client import Cancelled, RemoteError
from arxiv_demo.conferences import Catalogue
from arxiv_demo.deadlines import DeadlineClient, deadline_time, normalize
from arxiv_demo.navigation import load_navigation
from arxiv_demo.query import ValidationError
from arxiv_demo.service import Job

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = Catalogue(ROOT / "config/ccf_conferences.json")
AT = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def record(key, title, dblp, rank="A", deadline="2026-12-01 23:59:59", year=2027):
    return {"conference_key": key, "sub": key.split("/")[0], "title": title,
            "description": title + " conference", "dblp": dblp, "rank": {"ccf": rank},
            "confs": [{"year": year, "id": title.lower() + str(year), "link": "https://example.org/",
                       "date": "May 2027", "place": "Example", "timezone": "AoE",
                       "timeline": [{"deadline": deadline}]}]}


def fixtures():
    rows = [record("AI/iclr", "ICLR", "iclr", "C", "2026-09-25 23:59:59"),
            record("SC/fse", "FSE", "fse", "B"),
            record("SE/fse", "FSE", "sigsoft", "A", "2026-10-02 23:59:59"),
            record("DS/sec", "SEC", "ieeesec", "C", "2026-10-09 23:59:59", 2026),
            record("SC/sec", "SEC", "sec", "C", "TBD")]
    rows[0]["confs"][0]["timeline"][0]["rebuttal_deadline"] = "2026-11-18 23:59:59"
    rows[1]["confs"][0]["timeline"].insert(0, {"deadline": "2026-09-01 23:59:59", "comment": "round 1"})
    return rows


class ClockTests(unittest.TestCase):
    def test_aoe_is_previous_zone_not_utc(self):
        self.assertEqual(deadline_time("2026-10-08 23:59:59", "AoE")["utc"], "2026-10-09T11:59:59Z")

    def test_fixed_offsets_and_half_hours(self):
        self.assertEqual(deadline_time("2026-10-08 12:00:00", "UTC+8")["utc"], "2026-10-08T04:00:00Z")
        self.assertEqual(deadline_time("2026-10-08 12:00:00", "UTC-03:30")["utc"], "2026-10-08T15:30:00Z")

    def test_pt_summer_and_winter(self):
        self.assertEqual(deadline_time("2026-07-01 23:59:59", "PT")["utc"], "2026-07-02T06:59:59Z")
        self.assertEqual(deadline_time("2026-12-01 23:59:59", "PT")["utc"], "2026-12-02T07:59:59Z")

    def test_pt_transition_edge(self):
        self.assertEqual(deadline_time("2026-03-08 01:59:59", "PT")["utc"], "2026-03-08T09:59:59Z")
        self.assertEqual(deadline_time("2026-03-08 03:00:00", "PT")["utc"], "2026-03-08T10:00:00Z")

    def test_ambiguous_and_nonexistent_pt_are_not_guessed(self):
        for clock in ("2026-03-08 02:30:00", "2026-11-01 01:30:00"):
            parsed = deadline_time(clock, "PT")
            self.assertIsNone(parsed["utc"])
            self.assertTrue(parsed["warning"])

    def test_tbd_and_bad_inputs_do_not_invent_dates(self):
        self.assertIsNone(deadline_time("TBD", "AoE")["utc"])
        for clock, zone in (("2026-02-30 23:59:59", "UTC"), ("2026-10-08", "UTC"), ("2026-10-08 12:00:00", "UTC+99"), ("2026-10-08 12:00:00", "unknown")):
            self.assertTrue(deadline_time(clock, zone)["warning"])


class NavigationTests(unittest.TestCase):
    def test_every_official_venue_appears_once(self):
        majors = CATALOGUE.taxonomy["majors"]
        assigned = [key for m in majors for s in m["subfields"] for key in s["venue_ids"]]
        self.assertEqual(len(majors), 10)
        self.assertEqual(sum(len(m["subfields"]) for m in majors), 47)
        self.assertEqual(set(assigned), set(CATALOGUE.by_id))
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual(CATALOGUE.public()["counts"], {"A":58,"B":132,"C":196})

    def test_duplicate_acronyms_have_distinct_topics(self):
        fse = [v for v in CATALOGUE.venues if v["acronym"] == "FSE"]
        self.assertEqual({v["subfield_id"] for v in fse}, {"SC:crypto", "SE:engineering"})

    def test_new_venue_remains_visible_in_other_group(self):
        venues = [{"id":"custom", "domain":"New field", "rank":"C"}]
        data = load_navigation(Path("a-file-that-does-not-exist.json"), venues)
        self.assertEqual(data["majors"][0]["subfields"][0]["venue_ids"], ["custom"])
        self.assertEqual(venues[0]["subfield_name"], "综合 / 待归类")


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        self.snapshot = {"metadata":{"fetched_at":"2026-10-08T00:00:00+00:00"}, "conferences":fixtures()}
        (self.root / "config/ccfddl_snapshot.json").write_text(json.dumps(self.snapshot), encoding="utf-8")
        self.client = DeadlineClient(self.root, self.root / "data", CATALOGUE)

    def tearDown(self):
        self.temp.cleanup()

    def search(self, **values):
        return self.client.search(values, at=AT)

    def test_authoritative_grade_and_duplicate_names(self):
        data = self.search(status="all")
        by_key = {x["conference_key"]: x for x in data["items"]}
        self.assertEqual(by_key["AI/iclr"]["ccf_rank"], "A")
        self.assertEqual(by_key["SC/fse"]["ccf_rank"], "B")
        self.assertEqual(by_key["SE/fse"]["ccf_rank"], "A")
        self.assertNotEqual(by_key["SC/sec"]["venue_id"], by_key["DS/sec"]["venue_id"])
        self.assertEqual(data["metadata"]["rank_differences"][0]["key"], "AI/iclr")

    def test_upcoming_and_tbd_preserve_past_rounds(self):
        data = self.search()
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["items"][0]["conference_key"], "DS/sec")
        fse = next(x for x in data["items"] if x["conference_key"] == "SC/fse")
        self.assertEqual(len(fse["timeline"]), 2)
        self.assertEqual(fse["next_utc"], "2026-12-02T11:59:59Z")
        self.assertEqual(data["items"][-1]["status"], "tbd")
        self.assertIsNone(data["items"][-1]["next_utc"])

    def test_rebuttal_is_independent_of_submission(self):
        data = self.search(stage="rebuttal_deadline", major="AI")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["next_utc"], "2026-11-19T11:59:59Z")

    def test_hierarchy_and_rank_filters_intersect(self):
        self.assertEqual(self.search(major="SC", subfield="SC:crypto", rank="B")["total"], 1)
        self.assertEqual(self.search(major="SC", subfield="SC:crypto", rank="A")["total"], 0)
        self.assertEqual(self.search(major="AI", subfield="SC:crypto", status="all")["total"], 0)
        self.assertEqual(self.search(rank="A,B", status="all")["total"], 3)

    def test_past_tbd_year_and_query(self):
        self.assertEqual(self.search(status="past")["total"], 2)
        self.assertEqual(self.search(status="tbd")["total"], 1)
        self.assertEqual(self.search(year="2026")["total"], 1)
        self.assertEqual(self.search(q="fse")["total"], 1)

    def test_filter_by_identity_and_paginate(self):
        venue = next(v for v in CATALOGUE.venues if v["acronym"] == "SEC" and v["major_id"] == "SC")
        data = self.search(venue=venue["id"], status="all")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["conference_key"], "SC/sec")
        first = self.search(limit=1)
        second = self.search(limit=1, page=2)
        self.assertEqual(first["total"], second["total"])
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])

    def test_invalid_parameters_are_rejected(self):
        for values in ({"major":"oops"}, {"subfield":"AI:oops"}, {"venue":"oops"}, {"rank":"D"}, {"stage":"opening"}, {"status":"oops"}, {"year":"tomorrow"}, {"page":"0"}, {"limit":101}):
            with self.assertRaises(ValidationError): self.search(**values)

    def test_unmapped_grade_not_copied_and_urls_sanitized(self):
        rows = [record("AI/newconf", "New conference", "not-in-ccf", "A")]
        rows[0]["confs"][0]["link"] = "javascript:alert(1)"
        entries, counts = normalize(rows, CATALOGUE)
        self.assertIsNone(entries[0]["ccf_rank"])
        self.assertEqual(entries[0]["upstream_rank"], "A")
        self.assertEqual(entries[0]["url"], "")
        self.assertEqual(counts["unmatched_venues"], 1)

    def test_duplicate_ids_and_malformed_timeline_rejected(self):
        for change in (lambda x:x.append(copy.deepcopy(x[0])), lambda x:x[0]["confs"].append(copy.deepcopy(x[0]["confs"][0])), lambda x:x[0]["confs"][0].update(timeline=[{}]), lambda x:x[0].update(rank=None)):
            rows = fixtures(); change(rows)
            with self.assertRaises(ValueError): normalize(rows, CATALOGUE)

    def test_failed_refresh_keeps_previous_snapshot(self):
        self.client.opener = mock.Mock()
        self.client.opener.open.side_effect = URLError("offline")
        before = self.client.public()
        with self.assertRaises(RemoteError): self.client.refresh(Job("deadlines"))
        self.assertEqual(self.client.public(), before)
        self.assertFalse(self.client.cache_file.exists())

    def test_invalid_json_and_oversized_response_keep_previous(self):
        for body, limit in ((b"<html>unavailable</html>", 1000), (b"x" * 11, 10)):
            self.client.opener = mock.Mock()
            self.client.opener.open.return_value = io.BytesIO(body)
            with mock.patch("arxiv_demo.deadlines.MAX_BYTES", limit), self.assertRaises(RemoteError):
                self.client.refresh(Job("deadlines"))
            self.assertFalse(self.client.cache_file.exists())

    def test_successful_refresh_reloads_and_uses_cooldown(self):
        self.client.opener = mock.Mock()
        body = json.dumps(fixtures()).encode()
        self.client.opener.open.return_value = io.BytesIO(body)
        with mock.patch("arxiv_demo.deadlines.time.monotonic", return_value=10):
            self.client.refresh(Job("deadlines")); self.client.refresh(Job("deadlines"))
        self.assertEqual(self.client.opener.open.call_count, 1)
        self.assertTrue(self.client.cache_file.exists())
        self.assertFalse(self.client.cache_file.with_suffix(".json.tmp").exists())
        recovered = DeadlineClient(self.root, self.root / "data", CATALOGUE)
        self.assertEqual(recovered.public()["cache"], "updated")
        self.assertEqual(recovered.public()["venue_count"], 5)

    def test_cancel_during_transfer_keeps_existing_snapshot(self):
        job = Job("deadlines")
        opener = mock.Mock()
        def response(*args, **kwargs):
            job.cancel.set()
            return io.BytesIO(json.dumps(fixtures()).encode())
        opener.open.side_effect = response
        self.client.opener = opener
        with self.assertRaises(Cancelled): self.client.refresh(job)
        self.assertFalse(self.client.cache_file.exists())

    def test_corrupt_cache_falls_back_to_bundled_data(self):
        self.client.cache_file.parent.mkdir()
        self.client.cache_file.write_text("invalid", encoding="utf-8")
        recovered = DeadlineClient(self.root, self.root / "data", CATALOGUE)
        self.assertEqual(recovered.public()["cache"], "bundled")
        self.assertTrue(recovered.public()["warning"])

    def test_bundled_real_data_is_consistent(self):
        client = DeadlineClient(ROOT, self.root / "other-data", CATALOGUE)
        self.assertGreater(client.public()["venue_count"], 300)
        self.assertGreater(client.public()["edition_count"], 2000)
        self.assertEqual(client.public()["time_warnings"], [])


if __name__ == "__main__":
    unittest.main()
