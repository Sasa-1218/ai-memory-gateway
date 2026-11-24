"""MCP route must be explicitly enabled and authenticated before reading memories."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
import main


class DummyRequest:
    def __init__(self, body, key=""):
        self.headers={"x-gateway-key":key}
        self.json=AsyncMock(return_value=body)


class MCPGatewayAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_returns_404(self):
        with patch.object(main,"MCP_READONLY_ENABLED",False):
            with self.assertRaises(HTTPException) as error:
                await main.mcp_readonly_endpoint(DummyRequest({}))
        self.assertEqual(error.exception.status_code,404)

    async def test_missing_server_secret_returns_503(self):
        with (patch.object(main,"MCP_READONLY_ENABLED",True),
              patch.object(main,"GATEWAY_SECRET","")):
            with self.assertRaises(HTTPException) as error:
                await main.mcp_readonly_endpoint(DummyRequest({}))
        self.assertEqual(error.exception.status_code,503)

    async def test_bad_key_denied_before_json_or_database(self):
        request=DummyRequest({},key="bad")
        with (patch.object(main,"MCP_READONLY_ENABLED",True),
              patch.object(main,"GATEWAY_SECRET","strong-key")):
            with self.assertRaises(HTTPException) as error:
                await main.mcp_readonly_endpoint(request)
        self.assertEqual(error.exception.status_code,401)
        request.json.assert_not_awaited()

    async def test_good_key_can_list_readonly_tools(self):
        request=DummyRequest({"jsonrpc":"2.0","id":1,"method":"tools/list"},key="strong-key")
        with (patch.object(main,"MCP_READONLY_ENABLED",True),
              patch.object(main,"GATEWAY_SECRET","strong-key")):
            response=await main.mcp_readonly_endpoint(request)
        payload=json.loads(response.body)
        self.assertEqual(response.status_code,200)
        self.assertEqual([item["name"] for item in payload["result"]["tools"]],
                         ["search_memories","memory_lineage"])


if __name__=="__main__":
    unittest.main()
