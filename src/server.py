import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from src.catalog import load_context
from src.dispute import explain_dispute
from src.manifest import build_manifest
from src.scenario import build_yard
from src.views import project_batch

_TZ = timezone(timedelta(hours=8))
_MANIFEST_AT = datetime(2026, 9, 26, 18, 45, tzinfo=_TZ)  # 场景截关后的清单生成时刻


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        # 请求行按 latin-1 解码，中文查询参数需还原为 UTF-8
        path = urlparse(self.path).path
        query = {k: [v.encode("latin-1").decode("utf-8", "replace") for v in vs]
                 for k, vs in parse_qs(urlparse(self.path).query).items()}
        # 演示端点每次重建场景，保证重复请求结果一致
        yard, manifest_train = build_yard()
        try:
            if path == "/health":
                payload = {"status": "ok"}
            elif path == "/context":
                payload = load_context()
            elif path == "/manifest":
                payload = build_manifest(yard, manifest_train, _MANIFEST_AT)
            elif path.startswith("/dispute/"):
                payload = explain_dispute(yard, path.rsplit("/", 1)[1])
            elif path.startswith("/view/"):
                batch_id = path.rsplit("/", 1)[1]
                payload = project_batch(
                    yard, batch_id,
                    role=query.get("role", ["railway"])[0],
                    requester=query.get("requester", [None])[0],
                )
            else:
                self._reply(404, {"error": "未找到"})
                return
        except PermissionError as exc:
            self._reply(403, {"error": str(exc)})
            return
        except (KeyError, ValueError) as exc:
            self._reply(400, {"error": str(exc)})
            return
        self._reply(200, payload)

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
