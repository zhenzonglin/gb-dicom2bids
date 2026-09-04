"""Loopback-only QC HTTP service with local assets, bounded requests and CSRF protection."""

from __future__ import annotations

import argparse
import hmac
import json
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import load_config
from .qc_review import ReviewService
from .qc_state import BusyError, ConflictError, file_lock


def handler_class(service: ReviewService, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *args) -> None:
            # URLs and candidate identifiers are private, not console access logs.
            return

        def send(self, status: int, value: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(value)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' blob:; "
                "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(value)

        def json_response(self, status: int, value) -> None:
            self.send(
                status,
                json.dumps(value, ensure_ascii=False).encode(),
                "application/json; charset=utf-8",
            )

        def allowed(self, *, api: bool, write: bool = False) -> bool:
            port = self.server.server_port
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            host = self.headers.get("Host", "")
            if host not in hosts:
                return False
            if api and not hmac.compare_digest(self.headers.get("X-QC-Token", ""), token):
                return False
            return not write or self.headers.get("Origin") == f"http://{host}"

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            query = {key: values[0] for key, values in parse_qs(url.query).items()}
            if not self.allowed(api=url.path.startswith("/api/")):
                self.json_response(403, {"error": "local origin/token required"})
                return
            try:
                assets = Path(__file__).with_name("qc_web")
                if url.path == "/":
                    html = (
                        (assets / "index.html")
                        .read_text(encoding="utf-8")
                        .replace("__QC_TOKEN__", token)
                    )
                    self.send(200, html.encode(), "text/html; charset=utf-8")
                elif url.path in {"/app.js", "/style.css"}:
                    self.send(
                        200,
                        (assets / url.path[1:]).read_bytes(),
                        "text/javascript" if url.path.endswith(".js") else "text/css",
                    )
                elif url.path == "/api/subjects":
                    self.json_response(200, service.list_subjects(query))
                elif url.path == "/api/subject":
                    self.json_response(200, service.subject(query.get("id", "")))
                elif url.path == "/api/candidate":
                    uid = query.get("id", "")
                    service.require_uid(uid)
                    artifact = service.artifact(uid)
                    value = (
                        {"state": "ready", "metadata": artifact["metadata"]}
                        if artifact
                        else service.jobs.get(uid, {"state": "not_prepared"})
                    )
                    self.json_response(200, dict(value, log=service.log_text(uid)))
                elif url.path == "/api/slice":
                    uid = query.get("id", "")
                    artifact = service.artifact(uid)
                    if not artifact:
                        raise ValueError("candidate is not ready")
                    content = service.volumes.slice_png(
                        Path(artifact["image"]),
                        int(query["index"]),
                        float(query["low"]),
                        float(query["high"]),
                    )
                    self.send(200, content, "image/png")
                else:
                    self.json_response(404, {"error": "not found"})
            except (ValueError, KeyError, OSError, TypeError) as exc:
                self.json_response(400, {"error": str(exc)})

        def do_POST(self) -> None:
            if not self.allowed(api=True, write=True):
                self.json_response(403, {"error": "local origin/token required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1_048_576:
                    raise ValueError("request body must be 1 byte to 1 MiB")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("object body required")
                if self.path == "/api/prepare":
                    self.json_response(
                        200, service.prepare(str(body.get("id", "")), body.get("retry") is True)
                    )
                elif self.path == "/api/save":
                    self.json_response(
                        200, service.save(str(body.get("subject", "")), body.get("decision", {}))
                    )
                else:
                    self.json_response(404, {"error": "not found"})
            except ConflictError as exc:
                self.json_response(409, {"error": str(exc)})
            except BusyError as exc:
                self.json_response(423, {"error": str(exc)})
            except (ValueError, KeyError, OSError, TypeError) as exc:
                self.json_response(400, {"error": str(exc)})

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/config.local.yaml"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run and not args.apply:
        parser.error("--dry-run requires --apply")
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    service = None
    try:
        service = ReviewService(load_config(args.config), args.workers, activate=not args.dry_run)
        if args.apply:
            actions = service.apply(dry_run=args.dry_run)
            print(
                json.dumps(
                    {"dry_run": args.dry_run, "groups": len(actions), "actions": actions},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        with file_lock(service.root / ".viewer.lock"):
            server = ThreadingHTTPServer(
                ("127.0.0.1", args.port), handler_class(service, secrets.token_urlsafe(32))
            )
            server.daemon_threads = True
            url = f"http://127.0.0.1:{args.port}"
            print(
                f"Visual QC: {url}\n"
                "Save decisions here; apply from a separate terminal after review.",
                flush=True,
            )
            if not args.no_browser:
                webbrowser.open(url)
            try:
                server.serve_forever(poll_interval=0.5)
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", flush=True)
        return 2
    finally:
        if service is not None:
            service.close()
