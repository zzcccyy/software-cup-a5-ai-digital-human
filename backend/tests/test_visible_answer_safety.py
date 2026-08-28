"""Regression coverage for the visitor-visible LLM answer contract."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest


AI_SERVICE = Path(__file__).resolve().parents[1] / "ai_service.py"
MAIN = Path(__file__).resolve().parents[1] / "main.py"


def load_function(name: str):
    tree = ast.parse(AI_SERVICE.read_text(encoding="utf-8"), filename=str(AI_SERVICE))
    function = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name),
        None,
    )
    if function is None:
        raise AssertionError(f"{name} is required for visible-answer safety")
    namespace = {"re": re}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(AI_SERVICE), "exec"), namespace)
    return namespace[name]


def load_main_safe_reply_helper():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"), filename=str(MAIN))
    helper = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "safe_visitor_reply"),
        None,
    )
    if helper is None:
        raise AssertionError("safe_visitor_reply is required at every response boundary")
    namespace = {
        "sanitize_final_visible_text": load_function("sanitize_final_visible_text"),
        "compress_reply": lambda value: value,
    }
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(MAIN), "exec"), namespace)
    return namespace["safe_visitor_reply"]


class VisibleAnswerSafetyTests(unittest.TestCase):
    def test_prompt_uses_a_concise_chinese_only_visitor_contract(self):
        get_system_prompt = load_function("_get_system_prompt")
        prompt = get_system_prompt()

        self.assertIn("只输出面向游客的自然中文回答", prompt)
        self.assertNotIn("回复末尾可附加JSON块", prompt)
        self.assertNotIn("emotion primary:", prompt)

    def test_screenshot_like_control_leak_fails_closed(self):
        sanitize = load_function("sanitize_final_visible_text")
        leaked = "您好，emotion: joy，primary: trust，secondary: calm，intensity: 0.7，actions: wave"

        self.assertIsNone(sanitize(leaked))
        self.assertIsNone(sanitize('欢迎您。```json{"emotion":"joy"'))
        self.assertIsNone(sanitize("语言标签 zh-CN；拼音 pinyin"))

    def test_normal_chinese_guide_reply_remains_visible(self):
        sanitize = load_function("sanitize_final_visible_text")
        reply = "灵山大佛位于景区核心区域，建议您沿中轴线慢慢游览。"

        self.assertEqual(sanitize(reply), reply)

    def test_response_boundary_uses_a_safe_local_fallback(self):
        safe_reply = load_main_safe_reply_helper()
        leaked = "emotion: joy; primary: trust; actions: wave"
        fallback = "灵山大佛是景区的标志性景点，建议您从中轴线慢慢游览。"

        self.assertEqual(safe_reply(leaked, fallback), fallback)

    def test_text_and_voice_endpoints_apply_the_safe_reply_boundary(self):
        source = MAIN.read_text(encoding="utf-8")

        self.assertGreaterEqual(source.count('answer["reply"] = safe_visitor_reply('), 2)
