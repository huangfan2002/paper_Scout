"""Run the local web demo: python app.py --open"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import secrets
import socket
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from arxiv_demo.client import RemoteError
from arxiv_demo.exports import export_search
from arxiv_demo.query import ValidationError
from arxiv_demo.service import Service
from arxiv_demo.scholar import scholar_url, MAX_IMPORT_BYTES

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("arxiv-demo")


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR on Windows can let a second demo bind the same listening
    # address. Use exclusive ownership so repeated starts open the first one.
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, service):
        self.service = service
        self.token = secrets.token_urlsafe(32)
        super().__init__(address, Handler)

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    server_version = "ArxivCCFDemo/1.2"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def _discard_small_body(self):
        # Read ordinary rejected POST bodies before closing the connection,
        # otherwise Windows may reset it before the client receives the 403.
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if 0 < size <= 65536:
                self.rfile.read(size)
        except (ValueError, OSError):
            pass

    def log_message(self, fmt, *args):
        LOG.info(fmt, *args)

    def _local_request(self):
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self.json_response({"error": "请通过 localhost 或 127.0.0.1 访问。"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            self.json_response({"error": "不支持跨站请求。"}, 403)
            return False
        return True

    def send_bytes(self, data: bytes, content_type: str, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json_response(self, data, status=200):
        self.send_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def do_GET(self):
        if not self._local_request():
            return
        service = self.server.service
        url = urlsplit(self.path)
        path = unquote(url.path)
        try:
            if path in {"/", "/index.html", "/style.css", "/app.js", "/favicon.svg"}:
                name = "index.html" if path == "/" else path[1:]
                content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
                if content_type.startswith("text/") or name.endswith(".js"):
                    content_type += "; charset=utf-8"
                self.send_bytes((ROOT / "web" / name).read_bytes(), content_type)
            elif path == "/api/config":
                self.json_response({"catalogue": service.catalogue.public(), "token": self.server.token,
                                    "download_dir": str(service.download_dir), "python_only": True,
                                    "api_interval": 3.1, "cache_hours": 24, "scholar": service.scholar.public(),
                                    "deadlines": service.deadlines.public()})
            elif path == "/api/health":
                self.json_response({"status": "ok", "version": "1.2.0"})
            elif path == "/api/deadlines":
                params = {key: values[0] for key, values in parse_qs(url.query).items()}
                self.json_response(service.deadlines.search(params))
            elif path == "/api/history":
                self.json_response({"items": service.store.history()})
            elif path.startswith("/api/jobs/"):
                job = service.jobs.get(path.removeprefix("/api/jobs/"))
                self.json_response(job.snapshot() if job else {"error": "任务不存在。"}, 200 if job else 404)
            elif path.startswith("/api/results/"):
                result = service.store.search(path.removeprefix("/api/results/"))
                self.json_response(result if result else {"error": "结果不存在。"}, 200 if result else 404)
            elif path == "/api/export":
                params = parse_qs(url.query)
                search_id = params.get("search_id", [""])[0]
                result = service.store.search(search_id)
                if not result:
                    raise ValidationError("请先检索或加载历史结果。")
                ids = params.get("ids", [""])[0]
                if ids:
                    requested = set(ids.split(","))
                    by_id = {p["id"]: p for p in result["papers"]}
                    if requested - set(by_id):
                        raise ValidationError("所选论文不在检索结果中。")
                    result["papers"] = [p for p in result["papers"] if p["id"] in requested]
                data, content_type, extension = export_search(result, params.get("format", ["csv"])[0])
                prefix = "scholar" if result.get("source") in {"google_scholar", "scholar_import", "scholar_sample"} else "arxiv"
                self.send_bytes(data, content_type, filename=f"{prefix}_{search_id[:8]}.{extension}")
            elif path == "/api/downloads":
                files = []
                if service.download_dir.exists():
                    for target in sorted(service.download_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True):
                        metadata = target.with_suffix(".json")
                        title = target.stem
                        if metadata.exists():
                            try:
                                title = json.loads(metadata.read_text(encoding="utf-8")).get("title", title)
                            except (ValueError, OSError):
                                pass
                        files.append({"filename": target.name, "title": title, "bytes": target.stat().st_size})
                self.json_response({"files": files, "directory": str(service.download_dir)})
            elif path.startswith("/downloads/"):
                name = path.removeprefix("/downloads/")
                target = (service.download_dir / name).resolve()
                if not name or Path(name).name != name or target.parent != service.download_dir.resolve() or target.suffix != ".pdf" or not target.is_file():
                    self.json_response({"error": "文件不存在。"}, 404)
                    return
                self.send_bytes(target.read_bytes(), "application/pdf")
            else:
                self.json_response({"error": "接口不存在。"}, 404)
        except (ValidationError, ValueError) as exc:
            self.json_response({"error": str(exc)}, 400)
        except FileNotFoundError:
            self.json_response({"error": "文件不存在。"}, 404)
        except Exception:
            LOG.exception("GET request failed")
            self.json_response({"error": "服务器处理失败，详细信息见终端或 data/server.log。"}, 500)

    def do_POST(self):
        if not self._local_request():
            self._discard_small_body()
            return
        if not secrets.compare_digest(self.headers.get("X-Session-Token", ""), self.server.token):
            self._discard_small_body()
            self.json_response({"error": "会话已更新，请刷新页面。"}, 403)
            return
        try:
            if "application/json" not in self.headers.get("Content-Type", ""):
                raise ValidationError("请使用 application/json。")
            path = urlsplit(self.path).path
            size = int(self.headers.get("Content-Length", "0"))
            maximum = 2 * MAX_IMPORT_BYTES if path == "/api/scholar/import" else 65536
            if size < 1 or size > maximum:
                raise ValidationError(f"JSON 请求大小必须在 1–{maximum} 字节之间。")
            payload = json.loads(self.rfile.read(size))
            path = urlsplit(self.path).path
            service = self.server.service
            if path == "/api/search":
                self.json_response(service.submit_search(payload), 202)
            elif path == "/api/deadlines/refresh":
                self.json_response(service.refresh_deadlines(), 202)
            elif path == "/api/scholar/link":
                options = service.options(payload)
                if options.source != "google_scholar":
                    raise ValidationError("请选择 Google Scholar 数据源。")
                self.json_response({"url": scholar_url(options)})
            elif path == "/api/scholar/import":
                self.json_response(service.import_scholar(payload))
            elif path == "/api/sample":
                self.json_response(service.scholar_sample() if isinstance(payload, dict) and payload.get("source") == "google_scholar" else service.sample())
            elif path == "/api/shutdown":
                self.json_response({"status": "stopping"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif path == "/api/download":
                self.json_response(service.submit_download(payload), 202)
            elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job = service.jobs.get(path.split("/")[3])
                if not job:
                    self.json_response({"error": "任务不存在。"}, 404)
                    return
                if job.snapshot()["status"] in {"pending", "running"}:
                    job.cancel.set()
                self.json_response(job.snapshot())
            else:
                self.json_response({"error": "接口不存在。"}, 404)
        except (ValidationError, ValueError, UnicodeDecodeError) as exc:
            self.json_response({"error": str(exc)}, 400)
        except RemoteError as exc:
            self.json_response({"error": str(exc)}, 502)
        except Exception:
            LOG.exception("POST request failed")
            self.json_response({"error": "服务器处理失败，详细信息见终端或 data/server.log。"}, 500)


def main():
    parser = argparse.ArgumentParser(description="arXiv + CCF A/B/C 本地文献检索 demo")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--open", action="store_true", help="启动后打开默认浏览器")
    parser.add_argument("--stop", action="store_true", help="停止指定端口上正在运行的本 demo")
    parser.add_argument("--data-dir", type=Path, help="可选 SQLite/日志目录")
    parser.add_argument("--download-dir", type=Path, help="可选 PDF 保存目录")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间。")
    url = f"http://127.0.0.1:{args.port}"
    local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if args.stop:
        try:
            with local_opener.open(url + "/api/config", timeout=3) as response:
                if not response.headers.get("Server", "").startswith("ArxivCCFDemo/"):
                    raise ValueError("该端口的服务不是本 demo")
                token = json.load(response)["token"]
            req = urllib.request.Request(url + "/api/shutdown", data=b"{}",
                headers={"Content-Type": "application/json", "X-Session-Token": token})
            with local_opener.open(req, timeout=3) as response:
                json.load(response)
            print("已请求停止 demo 服务。")
            return
        except (OSError, ValueError, KeyError) as exc:
            parser.exit(1, f"无法停止服务：{exc}\n")
    data_dir = (args.data_dir or ROOT / "data").resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(data_dir / "server.log", encoding="utf-8")])
    service = Service(ROOT, data_dir=data_dir, download_dir=(args.download_dir or ROOT / "downloads").resolve())
    try:
        server = DemoServer(("127.0.0.1", args.port), service)
    except OSError as exc:
        if args.open:
            try:
                with local_opener.open(url + "/api/health", timeout=3) as response:
                    valid = response.headers.get("Server", "").startswith("ArxivCCFDemo/") and json.load(response).get("status") == "ok"
                if valid:
                    webbrowser.open(url)
                    print(f"demo 已在运行，已打开：{url}")
                    return
            except (OSError, ValueError):
                pass
        parser.exit(1, f"无法启动端口 {args.port}：{exc}\n请尝试 python app.py --port 8001 --open\n")
    print(f"\narXiv CCF demo 已启动：{url}\nPDF 保存到：{service.download_dir}\n按 Ctrl+C 停止。\n", flush=True)
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        with service.jobs.lock:
            for job in service.jobs.jobs.values():
                job.cancel.set()
        server.server_close()


if __name__ == "__main__":
    main()
