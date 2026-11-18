"""Guard against bulk consolidation silently hiding unrepresented fragments."""
import ast
import unittest
from pathlib import Path


def load_validator():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
    match = [node for node in tree.body
             if isinstance(node, ast.FunctionDef)
             and node.name == "_validate_consolidation_events"]
    assert len(match) == 1
    scope = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=match, type_ignores=[])),
                 "main.py", "exec"), scope)
    return scope["_validate_consolidation_events"]


class ConsolidationArchivingTests(unittest.TestCase):
    def setUp(self):
        self.validate = load_validator()

    def test_unmentioned_fragments_are_not_included(self):
        original = {1, 2, 3, 4}
        events = [{"title": "A", "content": "covers 1 and 3", "merged_ids": [1, 3]}]
        validated = self.validate(events, original)
        covered = {i for e in validated for i in e["merged_ids"]}
        self.assertEqual(covered, {1, 3})
        self.assertEqual(original - covered, {2, 4})

    def test_invalid_out_of_range_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown_fragment"):
            self.validate([{"content": "invented", "merged_ids": [1, 999]}], {1, 2})

    def test_boolean_ids_are_not_valid_integer_memory_ids(self):
        with self.assertRaises(ValueError):
            self.validate([{"content": "invented", "merged_ids": [True]}], {1})

    def test_duplicate_ids_are_deduplicated(self):
        result = self.validate([{"content": "valid", "merged_ids": [2, 2, 1]}], {1, 2})
        self.assertEqual(result[0]["merged_ids"], [2, 1])

    def test_empty_or_uncovered_model_output_does_not_archive(self):
        self.assertEqual(self.validate([], {1, 2}), [])
        self.assertEqual(self.validate([{"content": "no source", "merged_ids": []}], {1, 2}), [])

    def test_invalid_event_text_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "content_missing"):
            self.validate([{"content": "", "merged_ids": [1]}], {1})

    def test_invalid_model_shape_is_rejected(self):
        with self.assertRaises(ValueError):
            self.validate({"merged_ids": [1]}, {1})
        with self.assertRaises(ValueError):
            self.validate(["not an event"], {1})
        with self.assertRaises(ValueError):
            self.validate([{"content": "X", "merged_ids": "1"}], {1})

    def test_archival_code_only_uses_successfully_merged_ids(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
        start = source.index("async def consolidate_memories_for_date_range(")
        end = source.index('@app.post("/api/memories/consolidate")', start)
        block = source[start:end]
        self.assertIn("covered_ids.update(merged_ids)", block)
        self.assertIn("await deactivate_memories(sorted(covered_ids))", block)
        self.assertNotIn("await deactivate_memories(all_fragment_ids)", block)
        self.assertIn('"fragments_preserved": len(fragments) - len(covered_ids)', block)


if __name__ == "__main__":
    unittest.main()
