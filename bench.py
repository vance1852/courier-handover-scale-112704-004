"""本地设备资料回放。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def observations(path=None):
    path = path or ROOT / "observations.ndjson"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if [r["seq"] for r in rows] != list(range(1, len(rows) + 1)):
        raise ValueError("观测序号须连续")
    return rows


def listen(rows, port):
    devices = read("devices.json")

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = urlsplit(self.path)
            if path.path == "/devices":
                self.reply(200, devices)
                return
            if path.path != "/observations":
                self.reply(404, {"error": "路径不存在"})
                return
            query = parse_qs(path.query)
            try:
                after = int(query.get("after", [0])[0])
                limit = int(query.get("limit", [16])[0])
                if after < 0 or not 1 <= limit <= 100:
                    raise ValueError
            except ValueError:
                self.reply(400, {"error": "无效游标"})
                return
            page = rows[after:after + limit]
            self.reply(200, {"items": page, "cursor": page[-1]["seq"] if page else after})

        def do_POST(self):
            if self.path != "/intents":
                self.reply(404, {"error": "路径不存在"})
                return
            try:
                size = int(self.headers["Content-Length"])
                if not 0 < size <= 65536:
                    raise ValueError
                data = json.loads(self.rfile.read(size))
                if data["device"] not in devices or not all(
                    isinstance(data[k], str) and data[k] for k in ("id", "action")
                ):
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                self.reply(400, {"error": "消息缺少有效设备或动作标识"})
                return
            self.reply(202, {"id": data["id"], "device": data["device"], "effect": "received-only"})

        def log_message(self, format, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    cli = argparse.ArgumentParser(description="本地设备资料回放")
    cli.add_argument("--listen", type=int, help="回环地址的本机端口")
    cli.add_argument("--scene", type=Path, help="同格式的观测记录")
    args = cli.parse_args()
    events = observations(args.scene)
    if args.listen is None:
        print(f"读取 {len(read('devices.json'))} 个设备或来源，{len(events)} 条观测")
    else:
        with listen(events, args.listen) as server:
            print(f"监听 127.0.0.1:{server.server_port}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
