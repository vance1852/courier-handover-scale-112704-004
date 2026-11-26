"""核对设备资料和本机消息往返。"""

import json
import threading
import unittest
from urllib.request import Request, urlopen

from bench import listen, observations, read


class BenchTest(unittest.TestCase):
    def test_http_exchange(self):
        rows = observations()
        devices = read("devices.json")
        self.assertTrue({r["device"] for r in rows} <= set(devices))
        with listen(rows, 0) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(url + "/observations?after=1&limit=2", timeout=4) as response:
                    self.assertEqual(json.load(response), {"items": rows[1:3], "cursor": 3})
                with urlopen(url + "/devices", timeout=4) as response:
                    self.assertEqual(json.load(response), devices)
                body = json.dumps({"device": rows[0]["device"], "id": "L1", "action": "probe"}).encode()
                with urlopen(Request(url + "/intents", data=body), timeout=4) as response:
                    self.assertEqual(response.status, 202)
                    self.assertEqual(json.load(response)["effect"], "received-only")
            finally:
                server.shutdown()
                thread.join()

    def test_observation_links(self):
        rows = observations()
        for row in rows:
            for reference in row.get("refs", []):
                self.assertTrue(0 < reference < row["seq"])
        self.check_scene(rows)


    def check_scene(self, rows):
        self.assertTrue(rows[6]['zero_id'] == rows[13]['zero_id'] and rows[6]['boot'] != rows[13]['boot'])
        self.assertTrue(rows[9]['refs'] == [7,9])
        self.assertTrue(rows[1]['tree'] != rows[12]['tree'])
