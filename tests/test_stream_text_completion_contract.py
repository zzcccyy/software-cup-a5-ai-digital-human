"""Regression contracts for hiding the streaming cursor at text completion."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend" / "main.py"
CLIENT = ROOT / "tourist-client" / "app.js"


class StreamTextCompletionContractTests(unittest.TestCase):
    def test_backend_announces_text_completion_before_waiting_for_tts(self):
        source = BACKEND.read_text(encoding="utf-8")
        llm_start = source.index("# LLM streaming path")
        llm_end = source.index("# --- Finalize ---", llm_start)
        llm_block = source[llm_start:llm_end]

        final_text = llm_block.index(
            "yield f\"event: text\\r\\ndata: {json.dumps({'text': final_reply"
        )
        text_done = llm_block.index('yield f"event: text_done', final_text)
        remaining_tts = llm_block.index("remaining_tts_text", text_done)

        self.assertLess(final_text, text_done)
        self.assertLess(text_done, remaining_tts)

    def test_local_path_also_announces_text_completion(self):
        source = BACKEND.read_text(encoding="utf-8")
        local_start = source.index("if is_local:")
        llm_start = source.index("else:\n            # LLM streaming path", local_start)
        local_block = source[local_start:llm_start]

        self.assertIn('yield f"event: text_done', local_block)

    def test_frontend_removes_cursor_on_text_done_but_keeps_done_for_metadata(self):
        source = CLIENT.read_text(encoding="utf-8")
        helper_start = source.index("function markStreamingTextComplete")
        helper_end = source.index("function finalizeStreamingMessage", helper_start)
        helper = source[helper_start:helper_end]
        self.assertIn("cursor.remove()", helper)
        self.assertNotIn('msg.id = ""', helper)

        text_done_start = source.index('eventType === "text_done"')
        audio_segment_start = source.index('eventType === "audio_segment"', text_done_start)
        text_done_handler = source[text_done_start:audio_segment_start]
        self.assertIn("markStreamingTextComplete", text_done_handler)
        self.assertIn('eventType === "done"', source)


if __name__ == "__main__":
    unittest.main()
