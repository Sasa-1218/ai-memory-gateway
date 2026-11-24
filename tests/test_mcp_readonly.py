import unittest
from unittest.mock import AsyncMock

from mcp_readonly import dispatch


class ReadOnlyMCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.search = AsyncMock(return_value=[{"id":1,"content":"你好","importance":7}])
        self.lineage = AsyncMock(return_value={"exists":True,"source_memory_ids":[2]})

    async def send(self, method, params=None):
        return await dispatch({"jsonrpc":"2.0","id":1,"method":method,"params":params},
                              search=self.search,lineage=self.lineage)

    async def test_initialize(self):
        r=await self.send("initialize")
        self.assertIn("tools",r["result"]["capabilities"])

    async def test_list_tools_read_only(self):
        r=await self.send("tools/list")
        self.assertEqual({t["name"] for t in r["result"]["tools"]},
                         {"search_memories","memory_lineage"})

    async def test_search_does_not_touch_usage(self):
        r=await self.send("tools/call",{"name":"search_memories","arguments":{"query":"问候"}})
        self.assertIn("你好",r["result"]["content"][0]["text"])
        self.assertFalse(self.search.await_args.kwargs["touch_accessed"])

    async def test_lineage(self):
        r=await self.send("tools/call",{"name":"memory_lineage","arguments":{"memory_id":3}})
        self.assertIn("source_memory_ids",r["result"]["content"][0]["text"])

    async def test_invalid_tools_do_not_search(self):
        r=await self.send("tools/call",{"name":"forget_all","arguments":{}})
        self.assertEqual(r["error"]["code"],-32602)
        self.search.assert_not_awaited()

    async def test_invalid_query_bounded(self):
        r=await self.send("tools/call",{"name":"search_memories","arguments":{"query":"a"*501}})
        self.assertEqual(r["error"]["code"],-32602)

    async def test_notification(self):
        r=await dispatch({"jsonrpc":"2.0","method":"notifications/initialized"},
                         search=self.search,lineage=self.lineage)
        self.assertIsNone(r)


if __name__=="__main__":
    unittest.main()
