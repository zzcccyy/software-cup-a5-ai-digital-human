"""Static contract for sentence-safe LLM SSE text and final fallback."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest


MAIN = Path(__file__).resolve().parents[1] / "main.py"


class SseFinalTextSafetyContractTests(unittest.TestCase):
    def load_sentence_splitter(self):
        tree = ast.parse(MAIN.read_text(encoding="utf-8"), filename=str(MAIN))
        helper = next(
            (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "pop_complete_stream_sentences"),
            None,
        )
        if helper is None:
            raise AssertionError("pop_complete_stream_sentences is required for safe visible streaming")
        namespace = {"re": re}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(MAIN), "exec"), namespace)
        return namespace["pop_complete_stream_sentences"]

    def llm_block(self) -> str:
        source = MAIN.read_text(encoding="utf-8")
        llm_start = source.index("# LLM streaming path")
        llm_end = source.index("# --- Finalize ---", llm_start)
        return source[llm_start:llm_end]

    def test_llm_sse_emits_only_completed_sanitized_sentences(self):
        llm_block = self.llm_block()
        token_loop_start = llm_block.index("for token in chat_with_api_stream")
        final_validation_start = llm_block.index("raw_final_reply = accumulated.strip()", token_loop_start)
        token_loop = llm_block[token_loop_start:final_validation_start]
        self.assertIn("pop_complete_stream_sentences", token_loop)
        self.assertIn("sanitize_final_visible_text", token_loop)
        self.assertIn('yield f"event: text', token_loop)

    def test_llm_sse_keeps_final_full_reply_safety_fallback(self):
        llm_block = self.llm_block()
        sanitize_at = llm_block.index("final_reply = sanitize_final_visible_text")
        fallback_at = llm_block.index("final_reply = safe_visitor_reply(local_answer[\"reply\"])", sanitize_at)
        self.assertGreater(fallback_at, sanitize_at)

    def test_sentence_splitter_keeps_an_unfinished_provider_tail_private(self):
        split = self.load_sentence_splitter()

        sentences, pending = split("第一句。第二句！第三句还没完成")

        self.assertEqual(sentences, ["第一句。", "第二句！"])
        self.assertEqual(pending, "第三句还没完成")

    def test_final_tts_removes_a_whitespace_only_streamed_prefix(self):
        source = MAIN.read_text(encoding="utf-8")

        self.assertIn("def strip_streamed_tts_prefix", source)
        self.assertIn("remaining_tts_text = strip_streamed_tts_prefix", source)
        self.assertIn("safe_sentence = sanitize_final_visible_text(compress_reply(sentence))", source)

        tree = ast.parse(source, filename=str(MAIN))
        helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "strip_streamed_tts_prefix")
        namespace = {}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(MAIN), "exec"), namespace)
        strip_prefix = namespace["strip_streamed_tts_prefix"]
        self.assertEqual(strip_prefix("第一句。 第二句。", "第一句。\n"), "第二句。")

        helpers = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in {"_strip_markdown", "sanitize_reply_text", "compress_reply"}
        }
        normalization_namespace = {"re": re, "_strip_control_json": lambda text: text}
        for name in ("_strip_markdown", "sanitize_reply_text", "compress_reply"):
            exec(compile(ast.Module(body=[helpers[name]], type_ignores=[]), str(MAIN), "exec"), normalization_namespace)
        canonical_streamed = normalization_namespace["compress_reply"]("**欢迎。**")
        canonical_final = normalization_namespace["compress_reply"]("欢迎。下一句。")
        self.assertEqual(strip_prefix(canonical_final, canonical_streamed), "。下一句")
