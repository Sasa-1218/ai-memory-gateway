import unittest

import main


class DashboardAccessAuthTest(unittest.IsolatedAsyncioTestCase):
    def test_dashboard_routes_use_cloudflare_access(self):
        self.assertTrue(main._is_dashboard_access_path("/dashboard"))
        self.assertTrue(main._is_dashboard_access_path("/api/push/status"))
        self.assertTrue(main._is_dashboard_access_path("/api/memories"))
        self.assertTrue(main._is_dashboard_access_path("/api/memory/extraction/recent"))
        self.assertTrue(main._is_dashboard_access_path("/api/io/context/recent"))
        self.assertTrue(main._is_dashboard_access_path("/import/text"))
        self.assertTrue(main._is_dashboard_access_path("/export/memories"))

    def test_program_routes_keep_gateway_header_auth(self):
        self.assertTrue(main._is_gateway_key_path("/v1/chat/completions"))
        self.assertTrue(main._is_gateway_key_path("/v1/models"))
        self.assertTrue(main._is_gateway_key_path("/api/health/push"))
        self.assertTrue(main._is_gateway_key_path("/api/status/push"))
        self.assertFalse(main._is_dashboard_access_path("/v1/chat/completions"))

    def test_public_routes_remain_public(self):
        self.assertTrue(main._is_public_path("/"))
        self.assertTrue(main._is_public_path("/static/js/dashboard.js"))
        self.assertTrue(main._is_public_path("/api/push/trigger"))

    def test_url_login_parameters_are_redacted(self):
        url = "/dashboard?gateway_key=secret&key=other&token=third&view=push"
        redacted = main._redact_gateway_key_in_text(url)
        self.assertIn("gateway_key=[REDACTED]", redacted)
        self.assertIn("key=[REDACTED]", redacted)
        self.assertIn("token=[REDACTED]", redacted)
        self.assertIn("view=push", redacted)
        self.assertNotIn("secret", redacted)
        self.assertNotIn("other", redacted)
        self.assertNotIn("third", redacted)

    async def test_missing_cloudflare_access_jwt_fails_closed(self):
        result = await main._verify_cf_access_jwt("")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "missing_jwt")
        self.assertEqual(result["status_code"], 401)


if __name__ == "__main__":
    unittest.main()
