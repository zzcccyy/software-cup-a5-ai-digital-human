"""Regression coverage for UTF-8 provider SSE parsing."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


AI_SERVICE = Path(__file__).resolve().parents[1] / "ai_service.py"


def load_sse_decoder():
    tree = ast.parse(AI_SERVICE.read_text(encoding="utf-8"), filename=str(AI_SERVICE))
    helper = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_decode_sse_line"),
        None,
    )
    if helper is None:
        raise AssertionError("_decode_sse_line is required for provider SSE bytes")
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(AI_SERVICE), "exec"), namespace)
    return namespace["_decode_sse_line"]


class ProviderSseUtf8Tests(unittest.TestCase):
    def test_utf8_bytes_are_decoded_before_sse_json_parsing(self):
        decode = load_sse_decoder()
        line = b'data: {"choices":[{"delta":{"content":"\xe5\xa5\xbd"}}]}'
        self.assertEqual(decode(line), 'data: {"choices":[{"delta":{"content":"好"}}]}')

    def test_both_http_streams_use_the_explicit_bytes_decoder(self):
        source = AI_SERVICE.read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("iter_lines(decode_unicode=False)"), 2)
        self.assertGreaterEqual(source.count("_decode_sse_line(raw_line)"), 2)
