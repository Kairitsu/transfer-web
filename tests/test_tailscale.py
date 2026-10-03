import unittest

from transfer_web.tailscale import serve_urls


class ServeUrlTests(unittest.TestCase):
    def test_finds_root_handlers_proxying_to_this_port(self):
        config = {
            "TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}},
            "Web": {
                "box.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8756"}}},
                "box.tail1234.ts.net:8443": {"Handlers": {"/": {"Proxy": "localhost:8756"}}},
            },
        }
        self.assertEqual(
            serve_urls(config, 8756),
            ["https://box.tail1234.ts.net", "https://box.tail1234.ts.net:8443"],
        )

    def test_ignores_other_targets(self):
        config = {
            "Web": {
                "box.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}},
                "box.ts.net:8443": {"Handlers": {"/sub": {"Proxy": "http://127.0.0.1:8756"}}},
                "box.ts.net:9443": {"Handlers": {"/": {"Proxy": "http://10.0.0.5:8756"}}},
                "box.ts.net:10000": {"Handlers": {"/": {"Path": "/srv/www"}}},
            },
        }
        self.assertEqual(serve_urls(config, 8756), [])
        self.assertEqual(serve_urls(None, 8756), [])
        self.assertEqual(serve_urls({}, 8756), [])


if __name__ == "__main__":
    unittest.main()
