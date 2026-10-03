import http.client
import json
import threading
import unittest

from transfer_web.server import App, make_server

from .helpers import TempDirTestCase, local_config


class ServerTests(TempDirTestCase):
    def start(self, **overrides):
        config = local_config(self.tmp, **overrides)
        (self.tmp / "a" / "hello_world.txt").write_text("hi")
        self.app = App(config)
        self.server = make_server(self.app)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        if hasattr(self, "server"):
            self.server.shutdown()
            self.server.server_close()
            self.app.shutdown()
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response, data

    def post_json(self, path, payload, **headers):
        return self.request("POST", path, json.dumps(payload), {"Content-Type": "application/json", **headers})

    def test_config_and_listing(self):
        self.start()
        response, data = self.request("GET", "/api/config")
        config = json.loads(data)
        self.assertEqual([endpoint["name"] for endpoint in config["endpoints"]], ["甲", "乙"])
        self.assertEqual(config["transferable"], {"a": ["b"], "b": ["a"]})
        response, data = self.request("GET", "/api/list?endpoint=a&path=")
        self.assertEqual([entry["name"] for entry in json.loads(data)["entries"]], ["hello_world.txt"])
        response, _ = self.request("GET", "/api/list?endpoint=a&path=../")
        self.assertEqual(response.status, 400)

    def test_csrf_guards(self):
        self.start()
        response, _ = self.request("POST", "/api/transfer", "{}", {"Content-Type": "text/plain"})
        self.assertEqual(response.status, 415)
        response, _ = self.post_json("/api/transfer", {}, **{"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status, 403)
        response, _ = self.post_json("/api/transfer", {}, Origin="https://evil.example")
        self.assertEqual(response.status, 403)
        response, _ = self.post_json("/api/transfer", {}, Origin="null")
        self.assertEqual(response.status, 403)
        # Same origin passes the guard and reaches validation.
        response, _ = self.post_json("/api/transfer", {}, Origin=f"http://127.0.0.1:{self.port}", **{"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status, 400)

    def test_tailscale_allowlist(self):
        self.start(allowed_users=["me@example.com"])
        response, _ = self.request("GET", "/api/config")
        self.assertEqual(response.status, 403)
        response, _ = self.request("GET", "/", headers={"Tailscale-User-Login": "someone@else.com"})
        self.assertEqual(response.status, 403)
        response, data = self.request("GET", "/api/config", headers={"Tailscale-User-Login": "me@example.com"})
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(data)["user"], "me@example.com")

    def test_static_files_headers_and_etag(self):
        self.start()
        response, body = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn(b"<main", body)
        self.assertIsNone(response.getheader("Clear-Site-Data"))
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
        etag = response.getheader("ETag")
        response, _ = self.request("GET", "/", headers={"If-None-Match": etag})
        self.assertEqual(response.status, 304)
        response, _ = self.request("GET", "/static/../transfer_web/config.py")
        self.assertEqual(response.status, 404)
        for legacy in ("/assets/index.js", "/api/olivetin.api.v1.GetDashboard"):
            response, _ = self.request("GET", legacy)
            self.assertEqual(response.status, 404)

    def test_status_and_health(self):
        self.start()
        response, data = self.request("GET", "/api/status")
        self.assertEqual(json.loads(data)["active"], None)
        response, data = self.request("GET", "/api/health")
        self.assertTrue(all(item["ok"] for item in json.loads(data)["endpoints"].values()))


if __name__ == "__main__":
    unittest.main()
