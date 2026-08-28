"""Regression coverage for provider control JSON emitted one token at a time."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


AI_SERVICE = Path(__file__).resolve().parents[1] / "ai_service.py"


def load_split_helper():
    """Load the pure helper without importing provider SDK dependencies."""
    tree = ast.parse(AI_SERVICE.read_text(encoding="utf-8"), filename=str(AI_SERVICE))
    helper = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_split_provider_output"),
        None,
    )
    if helper is None:
        raise AssertionError("_split_provider_output is required for safe streaming")
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(AI_SERVICE), "exec"), namespace)
    return namespace["_split_provider_output"]


class StreamControlBlockTests(unittest.TestCase):
    def test_hides_open_and_closed_json_control_blocks_without_losing_chinese_text(self):
        split = load_split_helper()

        visible, pending = split("欢迎来到灵山胜境。``")
        self.assertEqual(visible, "欢迎来到灵山胜境。")
        self.assertEqual(pending, "``")

        visible, pending = split('欢迎来到灵山胜境。```j')
        self.assertEqual(visible, "欢迎来到灵山胜境。")
        self.assertEqual(pending, "```j")

        control = '```json{"emotion":{"primary":"joy"}}```'
        visible, pending = split(f"欢迎来到灵山胜境。{control}")
        self.assertEqual(visible, "欢迎来到灵山胜境。")
        self.assertEqual(pending, "")

    def test_preserves_visible_text_after_a_closed_control_block(self):
        split = load_split_helper()

        control = '```json{"emotion":{"primary":"joy"}}```'
        visible, pending = split(f"前半句。{control}后半句。")

        self.assertEqual(visible, "前半句。后半句。")
        self.assertEqual(pending, "")

    def test_incremental_content_does_not_expose_a_partial_control_fence(self):
        split = load_split_helper()
        raw = ""
        visible = []

        for chunk in ["欢迎。`", "``j", 'son{"emotion":', '"joy"}}```', "继续讲解。"]:
            raw += chunk
            now_visible, _pending = split(raw)
            visible.append(now_visible)

        self.assertEqual(visible, ["欢迎。", "欢迎。", "欢迎。", "欢迎。", "欢迎。继续讲解。"])
