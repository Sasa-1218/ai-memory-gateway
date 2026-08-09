import unittest
from unittest.mock import patch

import memory_extractor


class MemoryExtractorConfigTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_globals = {
            "API_BASE_URL": memory_extractor.API_BASE_URL,
            "API_KEY": memory_extractor.API_KEY,
            "MEMORY_API_BASE_URL": memory_extractor.MEMORY_API_BASE_URL,
            "MEMORY_API_KEY": memory_extractor.MEMORY_API_KEY,
            "MEMORY_MODEL": memory_extractor.MEMORY_MODEL,
            "MEMORY_API_THINKING": memory_extractor.MEMORY_API_THINKING,
        }

    def tearDown(self):
        for key, value in self.original_globals.items():
            setattr(memory_extractor, key, value)

    async def test_missing_memory_config_does_not_call_model(self):
        for key in self.original_globals:
            setattr(memory_extractor, key, "")

        with patch.object(memory_extractor.httpx, "AsyncClient") as client_mock, \
             patch("builtins.print") as print_mock:
            result = await memory_extractor.extract_memories(
                [{"role": "user", "content": "hello"}],
                existing_memories=[],
            )

        self.assertEqual(result, [])
        client_mock.assert_not_called()
        self.assertTrue(
            any(
                "memory_config_missing" in str(call.args[0])
                for call in print_mock.call_args_list
            )
        )

    async def test_extract_memories_uses_dedicated_memory_config(self):
        captured = {}

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": "[]"}}]}

        class FakeClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, headers, json):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                return FakeResponse()

        memory_extractor.API_BASE_URL = "https://chat.example/v1/chat/completions"
        memory_extractor.API_KEY = "chat-key"
        memory_extractor.MEMORY_API_BASE_URL = "https://memory.example/v1/chat/completions"
        memory_extractor.MEMORY_API_KEY = "memory-key"
        memory_extractor.MEMORY_MODEL = "deepseek-v4-flash"
        memory_extractor.MEMORY_API_THINKING = "false"

        with patch.object(memory_extractor.httpx, "AsyncClient", FakeClient):
            result = await memory_extractor.extract_memories(
                [{"role": "user", "content": "hello"}],
                existing_memories=[],
            )

        self.assertEqual(result, [])
        self.assertEqual(captured["url"], "https://memory.example/v1/chat/completions")
        self.assertEqual(captured["json"]["model"], "deepseek-v4-flash")
        self.assertIs(captured["json"]["thinking"], False)
        self.assertEqual(captured["headers"]["Authorization"], "Bearer memory-key")

    def test_official_deepseek_v4_uses_object_thinking_format(self):
        memory_extractor.MEMORY_API_BASE_URL = "https://api.deepseek.com/chat/completions"
        memory_extractor.MEMORY_MODEL = "deepseek-v4-flash"

        for configured, expected in (
            ("", {"type": "disabled"}),
            ("false", {"type": "disabled"}),
            ("true", {"type": "enabled"}),
        ):
            with self.subTest(configured=configured):
                memory_extractor.MEMORY_API_THINKING = configured
                payload = {}
                memory_extractor._apply_memory_thinking_option(payload)
                self.assertEqual(payload["thinking"], expected)

    def test_non_deepseek_provider_keeps_boolean_thinking_format(self):
        memory_extractor.MEMORY_API_BASE_URL = "https://memory.example/v1/chat/completions"
        memory_extractor.MEMORY_MODEL = "deepseek-v4-flash"
        memory_extractor.MEMORY_API_THINKING = "false"
        payload = {}

        memory_extractor._apply_memory_thinking_option(payload)

        self.assertIs(payload["thinking"], False)


if __name__ == "__main__":
    unittest.main()
