"""Regression contract for the visitor page's narrow-screen layout."""

from __future__ import annotations

from pathlib import Path
import unittest


STYLES = Path(__file__).resolve().parents[1] / "tourist-client" / "styles.css"


class MobileResponsiveContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = STYLES.read_text(encoding="utf-8")

    def narrow_rules(self) -> str:
        start = self.css.index("@media (max-width: 640px)")
        end = self.css.index("@media (max-width: 480px)", start)
        return self.css[start:end]

    def test_phone_layout_uses_real_visitor_selectors(self):
        rules = self.narrow_rules()
        self.assertIn(".chat-section", rules)
        self.assertIn(".interest-grid", rules)
        self.assertNotIn(".chat-panel", rules)
        self.assertNotIn(".interest-cards", rules)

    def test_phone_layout_accounts_for_dynamic_viewport_and_safe_areas(self):
        rules = self.narrow_rules()
        self.assertIn("100dvh", rules)
        self.assertIn("env(safe-area-inset-bottom)", rules)

    def test_phone_controls_have_touch_sized_actions(self):
        rules = self.narrow_rules()
        self.assertIn("min-height: 44px", rules)
        self.assertIn(".quick-questions", rules)
        self.assertIn(".feedback-btn", rules)
        self.assertIn(".star-btn", rules)
        feedback_actions = rules[rules.index(".feedback-actions {"):]
        self.assertIn("flex-wrap: wrap", feedback_actions)


if __name__ == "__main__":
    unittest.main()
