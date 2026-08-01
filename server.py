#!/usr/bin/env python3
"""
Logical Branch Trace — local UI server (no AI, no build step).

  python server.py [--port 8765] [--root /optional/allowlist]

Open http://127.0.0.1:8765 — paste a directory (or .py file), Analyze.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from analyze_branches import (
    analyze_target,
    compare_logical_branches,
    diff_report_to_markdown,
    report_to_markdown,
    workspace_markdown,
)

ROOT = Path(__file__).resolve().parent
UI_DIR = ROOT / "ui"


class Handler(BaseHTTPRequestHandler):
    allow_roots: list[Path] = []

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self._send(code, raw, "application/json; charset=utf-8")

    def _allowed(self, path: Path) -> bool:
        if not self.allow_roots:
            return True
        resolved = path.resolve()
        for root in self.allow_roots:
            if resolved == root:
                return True
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._json(200, {"ok": True})
            return

        if parsed.path == "/api/analyze":
            qs = parse_qs(parsed.query)
            path_raw = (qs.get("path") or [""])[0].strip()
            file_raw = (qs.get("file") or [""])[0].strip() or None
            self._run_analyze(path_raw, file_raw)
            return

        rel = parsed.path.lstrip("/") or "index.html"
        if ".." in rel or rel.startswith("/"):
            self._send(400, b"bad path", "text/plain")
            return
        file_path = (UI_DIR / rel).resolve()
        if not str(file_path).startswith(str(UI_DIR.resolve())):
            self._send(403, b"forbidden", "text/plain")
            return
        if not file_path.exists() or not file_path.is_file():
            self._send(404, b"not found", "text/plain")
            return
        data = file_path.read_bytes()
        ctype = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self._send(200, data, ctype)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/report":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid JSON body"})
                return
            self._run_report(body)
            return

        if parsed.path == "/api/diff":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid JSON body"})
                return
            self._run_diff(body)
            return

        if parsed.path != "/api/analyze":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON body"})
            return
        path_raw = str(body.get("path") or "").strip()
        file_raw = body.get("file")
        file_raw = str(file_raw).strip() if file_raw else None
        self._run_analyze(path_raw, file_raw)

    def _run_diff(self, body: dict) -> None:
        """Compare logical branches between two git commits under a path."""
        path_raw = str(body.get("path") or body.get("repo") or "").strip()
        base = str(body.get("base") or "").strip()
        head = str(body.get("head") or "").strip()
        path_filter = body.get("pathFilter") or body.get("path_filter")
        path_filter = str(path_filter).strip() if path_filter else None
        as_markdown = bool(body.get("markdown"))

        if not path_raw:
            self._json(400, {"error": "missing path (git repo, directory, or .py file)"})
            return
        if not base or not head:
            self._json(400, {"error": "missing base and/or head git revision"})
            return

        path = Path(path_raw).expanduser()
        path = path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
        if not path.exists():
            self._json(404, {"error": f"path not found: {path}"})
            return
        if not self._allowed(path):
            self._json(403, {"error": "path outside allowlisted roots"})
            return

        try:
            report = compare_logical_branches(
                path, base, head, path_filter=path_filter
            )
            if as_markdown:
                self._json(
                    200,
                    {
                        "markdown": diff_report_to_markdown(report),
                        "summary": report.get("summary"),
                        "kind": "diff",
                    },
                )
                return
            self._json(200, report)
        except FileNotFoundError as e:
            self._json(404, {"error": str(e)})
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            self._json(
                500,
                {
                    "error": "diff failed",
                    "detail": str(e),
                    "trace": traceback.format_exc(),
                },
            )

    def _run_report(self, body: dict) -> None:
        """Generate markdown report. Body: path, file?, scope=active|workspace, report?"""
        # Prefer client-supplied analyze payload for active file (fast)
        if body.get("report") and body.get("scope", "active") == "active":
            try:
                md = report_to_markdown(body["report"])
                self._json(200, {"markdown": md, "scope": "active"})
            except Exception as e:
                self._json(500, {"error": str(e), "trace": traceback.format_exc()})
            return

        path_raw = str(body.get("path") or "").strip()
        if not path_raw:
            self._json(400, {"error": "missing path or report"})
            return
        path = Path(path_raw).expanduser()
        path = path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
        if not path.exists():
            self._json(404, {"error": f"path not found: {path}"})
            return
        if not self._allowed(path):
            self._json(403, {"error": "path outside allowlisted roots"})
            return

        scope = str(body.get("scope") or "active")
        file_raw = body.get("file")
        file_raw = str(file_raw).strip() if file_raw else None
        try:
            if scope == "workspace" and path.is_dir():
                md = workspace_markdown(path)
                self._json(200, {"markdown": md, "scope": "workspace"})
                return
            report = analyze_target(path, file=file_raw)
            md = report_to_markdown(report)
            self._json(200, {"markdown": md, "scope": "active"})
        except Exception as e:
            self._json(
                500,
                {
                    "error": "report failed",
                    "detail": str(e),
                    "trace": traceback.format_exc(),
                },
            )

    def _run_analyze(self, path_raw: str, file_raw: str | None = None) -> None:
        if not path_raw:
            self._json(400, {"error": "missing path (directory or .py file)"})
            return
        path = Path(path_raw).expanduser()
        path = path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
        if not path.exists():
            self._json(404, {"error": f"path not found: {path}"})
            return
        if path.is_file() and path.suffix != ".py":
            self._json(400, {"error": "file path must be a .py file"})
            return
        if not (path.is_file() or path.is_dir()):
            self._json(400, {"error": "path must be a directory or .py file"})
            return
        if not self._allowed(path):
            self._json(
                403,
                {
                    "error": "path outside allowlisted roots",
                    "roots": [str(r) for r in self.allow_roots],
                },
            )
            return
        try:
            report = analyze_target(path, file=file_raw)
            self._json(200, report)
        except FileNotFoundError as e:
            self._json(404, {"error": str(e)})
        except SyntaxError as e:
            self._json(
                400,
                {
                    "error": "syntax error while parsing",
                    "detail": str(e),
                    "lineno": e.lineno,
                },
            )
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            self._json(
                500,
                {
                    "error": "analyze failed",
                    "detail": str(e),
                    "trace": traceback.format_exc(),
                },
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Allowlist root for readable paths (repeatable). Default: any path.",
    )
    args = parser.parse_args()

    Handler.allow_roots = [Path(r).expanduser().resolve() for r in args.root]

    if not UI_DIR.exists():
        print(f"UI directory missing: {UI_DIR}", file=sys.stderr)
        return 1

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Logical Branch Trace UI → http://{args.host}:{args.port}", flush=True)
    print("Point the UI at a directory (or .py file). Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
