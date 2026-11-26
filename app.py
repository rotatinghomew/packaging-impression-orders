"""轻量 HTTP 适配；核心逻辑全部在 service.CommissionPrintingService。

标准库运行：
    python3 app.py --port 8000
"""

from __future__ import annotations

import argparse
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from service import CommissionPrintingService, jsonable, load_service, render_markdown


def create_handler(service: CommissionPrintingService):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload):
            body = json.dumps(jsonable(payload), ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send(self, status: int, payload):
            body = json.dumps(jsonable(payload), ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, status: int, text: str):
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path == "/":
                report = service.report()
                md = html.escape(render_markdown(report))
                page = f"""<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><title>委印服务</title>
<style>body{{font-family:system-ui,'Noto Sans CJK SC',sans-serif;margin:2rem;color:#1f2937}}pre{{white-space:pre-wrap;line-height:1.5}}a{{margin-right:1rem}}</style></head>
<body><h1>拼版委印服务</h1><nav><a href=\"/api/report\">all.json</a><a href=\"/api/purchasing\">采购</a><a href=\"/api/warehouse\">仓班</a><a href=\"/api/finance\">财务</a><a href=\"/api/certificates\">证明</a><a href=\"/health\">health</a></nav><pre>{md}</pre></body></html>"""
                self._send_html(200, page)
            elif path == "/health":
                self._send(200, {"ok": True})
            elif path in {"/api/report", "/api/reports/all"}:
                self._send(200, service.report())
            elif path == "/api/purchasing":
                self._send(200, service.purchasing_report())
            elif path == "/api/warehouse":
                self._send(200, service.warehouse_report())
            elif path == "/api/finance":
                self._send(200, service.finance_report())
            elif path == "/api/certificates":
                self._send(200, service.certificates_report())
            else:
                self._send(404, {"error": "not_found", "path": path})

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.rstrip("/") != "/api/orders/availability":
                self._send(404, {"error": "not_found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                order = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                qs = parse_qs(parsed.query)
                qty = int(qs["quantity"][0]) if "quantity" in qs else None
                self._send(200, service.order_availability(order, qty))
            except Exception as exc:  # noqa: BLE001 - HTTP boundary
                self._send(400, {"error": type(exc).__name__, "message": str(exc)})

        def log_message(self, fmt, *args):
            return

    return Handler


def serve(inputs_dir: str = "inputs", host: str = "127.0.0.1", port: int = 8000):
    service = load_service(inputs_dir)
    httpd = ThreadingHTTPServer((host, port), create_handler(service))
    print(f"委印服务监听 http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", default="inputs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    serve(args.inputs, args.host, args.port)


if __name__ == "__main__":
    main()
