"""Loopback-only API for the interactive plan controls in saved HTML reports."""

from __future__ import annotations

import json
import logging
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import plan_store


LOG = logging.getLogger("plan_service")
TOKEN = plan_store.ensure_token()
MUTATION_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        LOG.info("%s %s", self.address_string(), format % args)

    def cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin in ("null", f"http://127.0.0.1:{plan_store.PORT}"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Plan-Token")

    def send_json(self, status: int, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self) -> bool:
        if self.headers.get("X-Plan-Token") != TOKEN:
            self.send_json(403, {"error": "无权更新计划"})
            return False
        return True

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/health":
            self.send_json(200, {"status": "ok"})
            return
        if urlsplit(self.path).path != "/api/plans":
            self.send_json(404, {"error": "不存在的路径"})
            return
        if not self.authorized():
            return
        try:
            self.send_json(200, {"plans": plan_store.read_plans(date.today(), include_done=True)})
        except Exception:
            LOG.exception("读取计划失败")
            self.send_json(500, {"error": "读取计划失败"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path != "/api/plans" and not path.startswith("/api/plans/"):
            self.send_json(404, {"error": "不存在的路径"})
            return
        if not self.authorized():
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError("请求体大小无效")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("请求内容须为对象")
            with MUTATION_LOCK:
                if path == "/api/plans":
                    updated = plan_store.add_plan(body.get("title"), body.get("due"))
                else:
                    updated = plan_store.update_plan(
                        path.removeprefix("/api/plans/"), body.get("action", ""),
                        progress=body.get("progress"), note=body.get("note"),
                    )
            self.send_json(201 if path == "/api/plans" else 200, {"plan": updated})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception:
            LOG.exception("更新计划失败")
            self.send_json(500, {"error": "更新计划失败"})


def main() -> None:
    log_path = plan_store.ROOT / "logs" / "plan_service.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, filename=log_path, encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", plan_store.PORT), Handler)
    LOG.info("plan service listening on 127.0.0.1:%s", plan_store.PORT)
    server.serve_forever(poll_interval=1)


if __name__ == "__main__":
    main()
