import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ConversationAnalysisFrontendContractTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "admin" / "index.html").read_text(encoding="utf-8")
        self.js = (ROOT / "admin" / "app.js").read_text(encoding="utf-8")
        self.css = (ROOT / "admin" / "styles.css").read_text(encoding="utf-8")

    def test_conversation_page_exposes_analysis_view_and_report_sections(self):
        for element_id in (
            "btn-show-conversation-list",
            "btn-show-conversation-analysis",
            "conversation-analysis-view",
            "btn-run-conversation-analysis",
            "conversation-analysis-scope",
            "conversation-analysis-metrics",
            "conversation-analysis-interest-chart",
            "conversation-analysis-emotion-chart",
            "conversation-analysis-satisfaction-chart",
            "conversation-analysis-trend-chart",
            "conversation-analysis-keyword-cloud",
            "conversation-analysis-summary",
            "conversation-analysis-findings",
            "conversation-analysis-gaps",
            "conversation-analysis-suggestions",
            "conversation-analysis-cases",
            "conversation-analysis-limitations",
        ):
            self.assertIn(f'id="{element_id}"', self.html)

    def test_analysis_request_reuses_current_filters_and_has_loading_states(self):
        self.assertIn('/admin/conversations/analyze', self.js)
        for query_key in ("period", "emotion", "interest", "satisfaction"):
            self.assertIn(f"{query_key}: state.conv", self.js)
        self.assertIn("conversationAnalysisLoading", self.js)
        self.assertIn("筛选条件已变化，请重新运行分析", self.js)
        for marker in (
            "conversationAnalysisRequestId",
            "requestId !== state.conversationAnalysisRequestId",
            "conversationRequestId",
            "requestId !== state.conversationRequestId",
            "result?.stale",
            "state.conversationAnalysisLoading = false",
        ):
            self.assertIn(marker, self.js)

    def test_report_renders_dynamic_distributions_and_escapes_content(self):
        for marker in (
            "emotionDistribution",
            "interestDistribution",
            "satisfactionDistribution",
            "dailyTrend",
            "renderConversationAnalysisCharts",
            "renderConversationAnalysisKeywordCloud",
            "renderAnalysisFallbackPieChart",
            "renderAnalysisFallbackBarChart",
            "renderAnalysisFallbackLineChart",
            "renderConversationAnalysisFallbackCharts(interest, emotions, satisfaction, trend);",
            "analysis-fallback-svg",
            "keywordDistribution",
            "echarts.init",
            'const fontSize = 14 + Math.round((value / max) * 30);',
            "escapeHtml(analysisLabel(item?.name))",
            "conversation-analysis-report",
            "analysisDateLabel",
            "hideOverlap: true",
        ):
            self.assertIn(marker, self.js)

    def test_analysis_report_charts_use_echarts_with_safe_fallback(self):
        start = self.js.index("function renderConversationAnalysisCharts")
        end = self.js.index("function renderConversationAnalysisKeywordCloud", start)
        renderer = self.js[start:end]
        self.assertIn(
            "renderConversationAnalysisFallbackCharts(interest, emotions, satisfaction, trend);",
            renderer,
        )
        self.assertIn("initConversationAnalysisCharts()", renderer)
        self.assertIn("renderAnalysisPieChart", renderer)

        switch_start = self.js.index("function switchConversationView")
        switch_end = self.js.index("function analysisLabel", switch_start)
        switcher = self.js[switch_start:switch_end]
        self.assertIn("renderConversationAnalysisCharts", switcher)

    def test_analysis_echarts_disable_progressive_rendering_and_guard_instances(self):
        for marker in (
            "progressive: 0",
            "animation: false",
            "isDisposed",
            "conversation analysis chart render failed",
            "conversation analysis chart resize failed",
        ):
            self.assertIn(marker, self.js)

    def test_dashboard_charts_have_runtime_safety_guards(self):
        for marker in (
            "initDashboardChart",
            "resizeEChartsSafely",
            "discardDashboardChart",
            "dashboard trend chart failed",
            "dashboard heatmap chart failed",
        ):
            self.assertIn(marker, self.js)

    def test_analysis_layout_has_responsive_rules(self):
        self.assertIn("conversation-analysis-grid", self.css)
        self.assertIn("conversation-analysis-metrics", self.css)
        self.assertIn("analysis-card-wide", self.css)
        self.assertIn("analysis-keyword-cloud", self.css)
        self.assertIn("min-height: 250px", self.css)
        self.assertIn('viewBox="0 0 520 400"', self.js)
        self.assertIn("grid-column: span 2", self.css)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", self.css)
        self.assertIn("@media (max-width: 860px)", self.css)

    def test_analysis_samples_have_vertical_room_before_limitations(self):
        self.assertIn('class="card analysis-cases-card"', self.html)
        for marker in (
            ".analysis-cases-card",
            "margin-bottom: 18px",
            "min-height: 190px",
            "max-height: none",
            "overflow: visible",
            "padding: 16px",
        ):
            self.assertIn(marker, self.css)


if __name__ == "__main__":
    unittest.main()
