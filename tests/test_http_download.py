"""Local HTTP contract and PDF write-integrity tests. No Internet required."""

import io
import contextlib
import json
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request
from pathlib import Path

from app import DemoServer, main as app_main
from arxiv_demo.client import Cancelled, RemoteError
from arxiv_demo.service import Service

ROOT = Path(__file__).resolve().parents[1]


class Response(io.BytesIO):
    def __init__(self, data, length=None):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data) if length is None else length)}


class FakeOpener:
    def __init__(self, data, length=None):
        self.data = data
        self.length = length
        self.calls = 0

    def open(self, *args, **kwargs):
        self.calls += 1
        return Response(self.data, self.length)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = Service(ROOT, data_dir=self.root / "data", download_dir=self.root / "downloads")
        self.service.transport.arxiv_gate.interval = 0

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_pdf_saved_and_duplicate_skipped(self):
        opener = FakeOpener(b"%PDF-1.4\nexample\n%%EOF")
        self.service.transport.opener = opener
        path = self.root / "downloads" / "2401.00001v1.pdf"
        self.assertEqual(self.service.transport.download("2401.00001v1", path)["status"], "downloaded")
        self.assertTrue(path.read_bytes().startswith(b"%PDF-"))
        self.assertEqual(self.service.transport.download("2401.00001v1", path)["status"], "skipped")
        self.assertEqual(opener.calls, 1)
        self.assertFalse(path.with_suffix(".pdf.part").exists())

    def test_html_is_not_saved_as_pdf(self):
        self.service.transport.opener = FakeOpener(b"<html>challenge</html>")
        path = self.root / "downloads" / "2401.00001v1.pdf"
        with self.assertRaises(RemoteError):
            self.service.transport.download("2401.00001v1", path)
        self.assertFalse(path.exists())
        self.assertFalse(path.with_suffix(".pdf.part").exists())

    def test_partial_content_is_not_saved(self):
        self.service.transport.opener = FakeOpener(b"%PDF-1.4\npartial", length=1000)
        path = self.root / "downloads" / "2401.00001v1.pdf"
        with self.assertRaises(RemoteError):
            self.service.transport.download("2401.00001v1", path)
        self.assertFalse(path.exists())
        self.assertFalse(path.with_suffix(".pdf.part").exists())

    def test_existing_non_pdf_is_not_overwritten(self):
        path = self.root / "downloads" / "2401.00001v1.pdf"
        path.parent.mkdir()
        path.write_bytes(b"existing content")
        with self.assertRaises(RemoteError):
            self.service.transport.download("2401.00001v1", path)
        self.assertEqual(path.read_bytes(), b"existing content")

    def test_invalid_id_and_cancel(self):
        path = self.root / "downloads" / "paper.pdf"
        with self.assertRaises(ValueError):
            self.service.transport.download("../../secret", path)
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(Cancelled):
            self.service.transport.download("2401.00001v1", path, cancel)
        self.assertFalse(path.exists())


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.service = Service(ROOT, data_dir=cls.root / "data", download_dir=cls.root / "downloads")
        cls.server = DemoServer(("127.0.0.1", 0), cls.service)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
        cls.temp.cleanup()

    def request(self, path, payload=None, token=True, raw=None, headers=None):
        request_headers = dict(headers or {})
        data = None
        if payload is not None or raw is not None:
            data = raw if raw is not None else json.dumps(payload).encode()
            request_headers["Content-Type"] = "application/json"
            if token: request_headers["X-Session-Token"] = self.server.token
        request = urllib.request.Request(self.base + path, data=data, headers=request_headers)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as response:
            with response:
                return response.code, response.headers, response.read()

    def test_ui_and_health(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Paper Scout", body)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(json.loads(self.request("/api/health")[2])["status"], "ok")

    def test_deadline_api_hierarchy_and_validation(self):
        config = json.loads(self.request("/api/config")[2])
        self.assertEqual(len(config["catalogue"]["taxonomy"]["majors"]), 10)
        self.assertGreater(config["deadlines"]["venue_count"], 300)
        status, _, body = self.request("/api/deadlines?major=AI&subfield=AI:vision&rank=A&status=all&limit=5")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(len(data["items"]), 5)
        self.assertTrue(all(x["major_id"] == "AI" and x["subfield_id"] == "AI:vision" and x["ccf_rank"] == "A" for x in data["items"]))
        self.assertEqual(self.request("/api/deadlines?stage=invalid")[0], 400)
        self.assertEqual(self.request("/api/deadlines/refresh", {}, token=False)[0], 403)

    def test_deadline_refresh_is_async(self):
        with mock.patch.object(self.service.deadlines, "refresh", lambda job:self.service.deadlines.public()):
            status, _, body = self.request("/api/deadlines/refresh", {})
            self.assertEqual(status, 202)
            key = json.loads(body)["id"]
            for _ in range(30):
                data = json.loads(self.request("/api/jobs/" + key)[2])
                if data["status"] not in {"running", "pending"}: break
                time.sleep(0.01)
            self.assertEqual(data["status"], "completed")
            self.assertEqual(data["kind"], "deadlines")
            self.assertGreater(data["result"]["venue_count"], 300)

    def test_no_token_and_cross_origin_rejected(self):
        self.assertEqual(self.request("/api/sample", {}, token=False)[0], 403)
        self.assertEqual(self.request("/api/sample", {}, headers={"Origin": "https://other.example"})[0], 403)
        self.assertEqual(self.request("/api/health", headers={"Host": "other.example"})[0], 403)

    def test_invalid_json_and_options(self):
        self.assertEqual(self.request("/api/search", raw=b"not-json")[0], 400)
        self.assertEqual(self.request("/api/search", {"keywords": "x", "date_from": "wrong"})[0], 400)

    def test_sample_results_export_and_history(self):
        status, _, body = self.request("/api/sample", {})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["source"], "sample")
        key = data["id"]
        self.assertEqual(self.request("/api/results/" + key)[0], 200)
        for name in ("csv", "json", "bibtex"):
            status, headers, content = self.request(f"/api/export?search_id={key}&format={name}")
            self.assertEqual(status, 200)
            self.assertIn("attachment", headers["Content-Disposition"])
            self.assertTrue(content)
        self.assertTrue(json.loads(self.request("/api/history")[2])["items"])
        self.assertEqual(self.request(f"/api/export?search_id={key}&ids=unknown")[0], 400)

    def test_download_path_traversal_rejected(self):
        for path in ("/downloads/../../app.py", "/downloads/%2e%2e%5capp.py", "/downloads/missing.pdf"):
            self.assertEqual(self.request(path)[0], 404)

    def test_async_search_job(self):
        self.service.arxiv.page = lambda *args: ([], 0, False)
        status, _, body = self.request("/api/search", {"keywords": "no-result"})
        self.assertEqual(status, 202)
        key = json.loads(body)["id"]
        for _ in range(30):
            data = json.loads(self.request("/api/jobs/" + key)[2])
            if data["status"] not in {"running", "pending"}:
                break
            time.sleep(0.01)
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["result"]["papers"], [])

    def test_reopen_running_demo(self):
        with mock.patch("sys.argv", ["app.py", "--port", str(self.server.server_port), "--open"]), \
             mock.patch("app.Service", return_value=self.service), mock.patch("app.webbrowser.open") as opened, \
             contextlib.redirect_stdout(io.StringIO()):
            app_main()
        opened.assert_called_once_with(self.base)

    def test_scholar_source_configuration_has_no_secret(self):
        body = json.loads(self.request("/api/config")[2])
        self.assertEqual(body["scholar"]["provider"], "serpapi")
        self.assertNotIn("api_key", body["scholar"])

    def test_scholar_browser_link_and_import_export(self):
        payload = {"source": "google_scholar", "keywords": "federated learning", "year_from": "2024"}
        status, _, content = self.request("/api/scholar/link", payload)
        self.assertEqual(status, 200)
        self.assertIn("as_ylo=2024", json.loads(content)["url"])
        sample = (ROOT / "examples" / "scholar_sample.json").read_text(encoding="utf-8")
        status, _, content = self.request("/api/scholar/import", {"content": sample})
        self.assertEqual(status, 200)
        result = json.loads(content)
        self.assertEqual(result["source"], "scholar_import")
        self.assertEqual(len(result["papers"]), 2)
        status, headers, _ = self.request(f"/api/export?search_id={result['id']}&format=csv")
        self.assertEqual(status, 200)
        self.assertIn("scholar_", headers["Content-Disposition"])

    def test_scholar_sample_is_marked_offline(self):
        status, _, content = self.request("/api/sample", {"source": "google_scholar"})
        self.assertEqual(status, 200)
        result = json.loads(content)
        self.assertEqual(result["source"], "scholar_sample")
        self.assertTrue(result["warnings"])
        self.assertEqual(self.request("/api/scholar/import", {"content": "<html>captcha</html>"})[0], 400)

    def test_stop_command_shuts_down_only_demo(self):
        isolated = DemoServer(("127.0.0.1", 0), self.service)
        worker = threading.Thread(target=isolated.serve_forever, daemon=True)
        worker.start()
        try:
            with mock.patch("sys.argv", ["app.py", "--port", str(isolated.server_port), "--stop"]), \
                 contextlib.redirect_stdout(io.StringIO()):
                app_main()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        finally:
            isolated.shutdown(); isolated.server_close()


if __name__ == "__main__":
    unittest.main()
