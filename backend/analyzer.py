from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

import database as db


class SentimentAnalyzer:
    def emotion_trend(self, days: int = 30) -> list[dict]:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT emotion, timestamp, satisfaction FROM conversations WHERE timestamp >= ?",
                (cutoff,)
            ).fetchall()

        daily: dict[str, dict] = {}
        for r in rows:
            day = r["timestamp"][:10] if r["timestamp"] else ""
            if not day:
                continue
            if day not in daily:
                daily[day] = {"date": day, "positive": 0, "neutral": 0, "negative": 0, "count": 0, "satisfaction_sum": 0, "satisfaction_count": 0}
            e = r["emotion"] or "neutral"
            if e in ("delighted", "warm"):
                daily[day]["positive"] += 1
            elif e in ("caring", "sad", "surprised"):
                daily[day]["negative"] += 1
            else:
                daily[day]["neutral"] += 1
            daily[day]["count"] += 1
            if r["satisfaction"] is not None:
                daily[day]["satisfaction_sum"] += r["satisfaction"]
                daily[day]["satisfaction_count"] += 1

        result = sorted(daily.values(), key=lambda x: x["date"])
        for item in result:
            if item["satisfaction_count"] > 0:
                item["avgSatisfaction"] = round(item["satisfaction_sum"] / item["satisfaction_count"], 1)
            else:
                item["avgSatisfaction"] = None
        return result

    def topic_ranking(self, top_k: int = 10) -> list[dict]:
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT j.value AS topic, COUNT(*) AS cnt FROM conversations, json_each(topics) j "
                "WHERE topics IS NOT NULL AND json_valid(topics) AND j.value != '' "
                "GROUP BY j.value ORDER BY cnt DESC, j.value LIMIT ?",
                (top_k,)
            ).fetchall()
        return [{"name": r["topic"], "value": r["cnt"]} for r in rows]

    def visitor_profiling(self) -> dict:
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT interest, AVG(satisfaction) AS avg_satisfaction, COUNT(*) AS cnt "
                "FROM conversations GROUP BY interest"
            ).fetchall()

        profiles = {}
        for r in rows:
            interest = r["interest"] or "unknown"
            profiles[interest] = {
                "count": r["cnt"],
                "avgSatisfaction": (
                    round(float(r["avg_satisfaction"]), 1)
                    if r["avg_satisfaction"] is not None else None
                ),
            }
        return profiles

    def improvement_suggestions(self) -> list[str]:
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        with db.get_db() as conn:
            low_sat = conn.execute(
                "SELECT message, reply, topics FROM conversations WHERE satisfaction IS NOT NULL AND satisfaction <= 2 AND timestamp >= ?",
                (week_ago,)
            ).fetchall()

        suggestions = []
        low_sat_messages = [r["message"] for r in low_sat if r["message"]]
        if low_sat_messages:
            text = " ".join(low_sat_messages)
            ticket_issues = sum(1 for m in low_sat_messages if any(k in m for k in ["门票", "票价", "钱"]))
            route_issues = sum(1 for m in low_sat_messages if any(k in m for k in ["路线", "怎么", "找不到"]))
            if ticket_issues >= 2:
                suggestions.append(f"近7日有{ticket_issues}条关于门票/价格的低满意度对话，建议检查知识库中的票价信息是否准确完整。")
            if route_issues >= 2:
                suggestions.append(f"近7日有{route_issues}条关于路线/导航的低满意度对话，建议优化路线推荐逻辑和位置推断。")

        with db.get_db() as conn:
            faqs = conn.execute("SELECT question, usage_count FROM faq ORDER BY usage_count DESC LIMIT 3").fetchall()
        if faqs and faqs[0]["usage_count"] > 10:
            suggestions.append(f"高频问题\"{faqs[0]['question']}\"使用次数达{faqs[0]['usage_count']}次，建议将其知识条目前置到推荐回复中。")

        if not suggestions:
            suggestions.append("暂无足够反馈数据生成优化建议。")

        return suggestions

    def hourly_distribution(self) -> list[dict]:
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT timestamp FROM conversations WHERE timestamp >= ?",
                (week_ago,)
            ).fetchall()

        hourly: Counter = Counter()
        for r in rows:
            if r["timestamp"] and len(r["timestamp"]) >= 13:
                try:
                    hour = int(r["timestamp"][11:13])
                    hourly[hour] += 1
                except (ValueError, IndexError):
                    pass
        return [{"hour": h, "count": hourly[h]} for h in range(6, 22)]

    def spot_heatmap(self) -> list[dict]:
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT message, satisfaction FROM conversations"
            ).fetchall()

        spots = ["灵山大佛", "梵宫", "九龙灌浴", "祥符禅寺", "五印坛城",
                 "拈花广场", "梵天花海", "香月花街", "鹿鸣谷", "五灯湖"]
        heatmap = []
        for spot in spots:
            spot_rows = [r for r in rows if spot in (r["message"] or "")]
            sat_values = [r["satisfaction"] for r in spot_rows if r["satisfaction"] is not None]
            heatmap.append({
                "spot": spot,
                "mentions": len(spot_rows),
                "avgSatisfaction": round(sum(sat_values) / len(sat_values), 1) if sat_values else None,
            })
        heatmap.sort(key=lambda x: x["mentions"], reverse=True)
        return heatmap
