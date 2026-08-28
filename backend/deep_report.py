from __future__ import annotations

from typing import Any

def compute_deep_report() -> dict[str, Any]:
    try:
        from analyzer import SentimentAnalyzer
        sa = SentimentAnalyzer()
        return {
            "emotionTrend": sa.emotion_trend(days=14),
            "topicRanking": sa.topic_ranking(top_k=10),
            "visitorProfiles": sa.visitor_profiling(),
            "improvementSuggestions": sa.improvement_suggestions(),
            "hourlyDistribution": sa.hourly_distribution(),
            "spotHeatmap": sa.spot_heatmap(),
        }
    except ImportError:
        return {
            "emotionTrend": [],
            "topicRanking": [],
            "visitorProfiles": {},
            "improvementSuggestions": ["分析引擎未加载"],
            "hourlyDistribution": [],
            "spotHeatmap": [],
        }
