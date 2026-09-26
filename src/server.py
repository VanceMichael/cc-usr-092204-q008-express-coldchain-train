"""HTTP 入口。

鉴权以请求头模拟（生产环境替换为真实身份层）：
- X-Role: RAIL / SHIPPER / FEEDER / DEVICE
- X-Actor-Id: 角色主体标识（发货人编号、接驳商承运商标识、设备号）

路由：
- GET  /health
- GET  /context
- POST /events                     上报事件（设备/铁路）
- POST /services/{id}/plan         生成截关前可执行清单（?as_of=ISO）
- POST /reserves/{id}/commit       装车确认
- POST /services/{id}/close        截关发车
- GET  /batches/{id}/timeline      货损争议时间线
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from src.app import ServiceApp
from src.catalog import load_context
from src.ledger import LedgerError
from src.views import AccessDenied

app = ServiceApp()


def _json_default(obj: object) -> str:
    from datetime import datetime
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"不可序列化: {type(obj)}")


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _identity(self) -> tuple[str, str]:
        return self.headers.get("X-Role", "RAIL"), self.headers.get("X-Actor-Id", "rail-ops")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, {"status": "ok"})
        elif path == "/context":
            self._send(200, load_context())
        elif path.startswith("/batches/") and path.endswith("/timeline"):
            batch_id = path.split("/")[2]
            role, actor = self._identity()
            try:
                self._send(200, app.timeline(batch_id, role, actor))
            except AccessDenied as e:
                self._send(403, {"error": str(e)})
            except KeyError:
                self._send(404, {"error": f"未知批次: {batch_id}"})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        role, actor = self._identity()
        try:
            if path == "/events":
                self._send(202, app.post_event(self._read_json(), role))
            elif path.startswith("/services/") and path.endswith("/plan"):
                service_id = path.split("/")[2]
                as_of = self._read_json().get("as_of")
                self._send(200, app.make_plan(service_id, as_of, role, actor))
            elif path.startswith("/reserves/") and path.endswith("/commit"):
                reserve_id = path.split("/")[2]
                body = self._read_json()
                self._send(200, app.commit(reserve_id, body["at"], role,
                                           body.get("pallet_ids")))
            elif path.startswith("/services/") and path.endswith("/close"):
                service_id = path.split("/")[2]
                body = self._read_json()
                self._send(200, app.close(service_id, body["at"], role))
            else:
                self.send_error(404)
        except AccessDenied as e:
            self._send(403, {"error": str(e)})
        except (LedgerError, ValueError) as e:
            self._send(400, {"error": str(e)})

    def log_message(self, fmt: str, *args) -> None:  # 静默
        return


def build_app() -> ServiceApp:
    return app


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
