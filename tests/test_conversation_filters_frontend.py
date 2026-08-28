import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ConversationFiltersFrontendContractTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "admin" / "index.html").read_text(encoding="utf-8")
        self.js = (ROOT / "admin" / "app.js").read_text(encoding="utf-8")
        self.css = (ROOT / "admin" / "styles.css").read_text(encoding="utf-8")

    def test_conversation_page_exposes_time_and_dimension_filters(self):
        for element_id in (
            "conversation-filter-period",
            "conversation-filter-emotion",
            "conversation-filter-interest",
            "conversation-filter-satisfaction",
            "btn-apply-conv-filters",
            "btn-reset-conv-filters",
        ):
            self.assertIn(f'id="{element_id}"', self.html)

        for option in ("day", "week", "month"):
            self.assertIn(f'value="{option}"', self.html)

    def test_emotion_filter_only_exposes_currently_used_values(self):
        emotion_select = self.html.split('id="conversation-filter-emotion"', 1)[1].split("</select>", 1)[0]
        for value in ("warm", "neutral", "delighted", "joy", "trust", "caring", "anticipation", "focused"):
            self.assertIn(f'value="{value}"', emotion_select)
        for value in ("sad", "surprised", "thinking", "fear", "surprise", "sadness", "disgust", "anger"):
            self.assertNotIn(f'value="{value}"', emotion_select)

    def test_conversation_request_carries_all_filters_and_resets_page(self):
        for query_key in ("period", "emotion", "interest", "satisfaction"):
            self.assertIn(f'{query_key}: state.conv', self.js)
        self.assertIn("state.convPage = 1", self.js)
        self.assertIn("conversation-filters", self.js)
        self.assertIn("btn-reset-conv-filters", self.js)

    def test_conversation_filter_layout_has_narrow_screen_rule(self):
        self.assertIn("conversation-filters", self.css)
        self.assertIn("@media (max-width: 860px)", self.css)


if __name__ == "__main__":
    unittest.main()
