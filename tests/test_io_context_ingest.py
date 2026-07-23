import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main


class IoContextIngestTest(unittest.TestCase):
    def setUp(self):
        self.previous_secret = main.IO_INGEST_SECRET
        main.IO_INGEST_SECRET = "test-io-key"
        self.client = TestClient(main.app)

    def tearDown(self):
        main.IO_INGEST_SECRET = self.previous_secret

    def payload(self):
        return {
            "schema_version": 1,
            "device_id": "ios-device-a",
            "app_instance_id": "io-install-a",
            "timezone": "Asia/Shanghai",
            "events": [
                {
                    "client_event_id": "event-1",
                    "event_type": "device.battery",
                    "observed_at": "2026-07-24T00:30:00+08:00",
                    "permission_state": "granted",
                    "payload": {"level": 82, "charging": True},
                }
            ],
        }

    def test_correct_io_key_can_write_events(self):
        async def fake_save_io_context_events(**kwargs):
            self.assertEqual(kwargs["device_id"], "ios-device-a")
            self.assertEqual(kwargs["source_client"], "io")
            self.assertEqual(len(kwargs["events"]), 1)
            return {
                "received": 1,
                "inserted": 1,
                "duplicates": 0,
                "skipped": 0,
                "latest_updated": 1,
            }

        with patch.object(main, "save_io_context_events", fake_save_io_context_events):
            response = self.client.post(
                "/v1/io/context/events",
                headers={"X-IO-Key": "test-io-key"},
                json=self.payload(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["inserted"], 1)

    def test_wrong_io_key_is_rejected(self):
        save_mock = AsyncMock()
        with patch.object(main, "save_io_context_events", save_mock):
            response = self.client.post(
                "/v1/io/context/events",
                headers={"X-IO-Key": "wrong"},
                json=self.payload(),
            )

        self.assertEqual(response.status_code, 401)
        save_mock.assert_not_called()

    def test_io_events_do_not_enter_conversations(self):
        async def fake_save_io_context_events(**kwargs):
            return {
                "received": 1,
                "inserted": 1,
                "duplicates": 0,
                "skipped": 0,
                "latest_updated": 1,
            }

        with patch.object(main, "save_io_context_events", fake_save_io_context_events), \
             patch.object(main, "save_message", AsyncMock()) as save_message_mock:
            response = self.client.post(
                "/v1/io/context/events",
                headers={"X-IO-Key": "test-io-key"},
                json=self.payload(),
            )

        self.assertEqual(response.status_code, 200)
        save_message_mock.assert_not_called()

    def test_io_events_do_not_trigger_push_or_bark(self):
        async def fake_save_io_context_events(**kwargs):
            return {
                "received": 1,
                "inserted": 1,
                "duplicates": 0,
                "skipped": 0,
                "latest_updated": 1,
            }

        with patch.object(main, "save_io_context_events", fake_save_io_context_events), \
             patch.object(main, "generate_shadow_push", AsyncMock()) as push_mock, \
             patch.object(main, "deliver_bark_push", AsyncMock()) as bark_mock:
            response = self.client.post(
                "/v1/io/context/events",
                headers={"X-IO-Key": "test-io-key"},
                json=self.payload(),
            )

        self.assertEqual(response.status_code, 200)
        push_mock.assert_not_called()
        bark_mock.assert_not_called()

    def test_io_path_does_not_use_gateway_key_auth(self):
        self.assertTrue(main._is_io_key_path("/v1/io/context/events"))
        self.assertFalse(main._is_gateway_key_path("/v1/io/context/events"))
        self.assertFalse(main._is_dashboard_access_path("/v1/io/context/events"))


if __name__ == "__main__":
    unittest.main()
