from __future__ import annotations

import re
from typing import Any


# Patterns to extract factual claims from LLM answers
_NUMERIC_PATTERN = re.compile(r"(\d+[\.\d]*)\s*(米|元|吨|分|小时|分钟|层|个|块|条|公里|里|度)")
_HEIGHT_PATTERNS = [
    (re.compile(r"高\s*(\d+[\.\d]*)"), "高"),
    (re.compile(r"(\d+[\.\d]*)\s*米高"), "米高"),
    (re.compile(r"通高\s*(\d+[\.\d]*)"), "通高"),
    (re.compile(r"(\d+[\.\d]*)\s*吨"), "吨"),
    (re.compile(r"(\d+[\.\d]*)\s*元"), "元"),
    (re.compile(r"(\d+[\.\d]*)\s*层"), "层"),
]

_STOP_PATTERNS = [
    re.compile(r"建议"),
    re.compile(r"可以"),
    re.compile(r"可能"),
    re.compile(r"如果"),
    re.compile(r"建议你"),
]


def _extract_numbers(text: str) -> list[str]:
    """Extract all numeric expressions with units from text."""
    return [f"{m.group(1)}{m.group(2)}" for m in _NUMERIC_PATTERN.finditer(text)]


def _is_qualified(claim: str) -> bool:
    """Check if a claim is qualified with uncertainty (should not be checked strictly)."""
    return any(p.search(claim) for p in _STOP_PATTERNS)


def verify_grounding(answer: str, knowledge_context: list[dict[str, Any]]) -> dict:
    """Check if factual claims in LLM answer are supported by the knowledge base.

    Args:
        answer: The LLM's response text
        knowledge_context: List of knowledge items passed as context to the LLM

    Returns:
        dict with:
            - consistent: bool, whether all checked facts are supported
            - suspicious_facts: list of unsupported fact claims
            - checked_count: total number of numeric facts checked
            - supported_count: number of facts found in knowledge base
    """
    if not knowledge_context:
        return {"consistent": True, "suspicious_facts": [], "checked_count": 0, "supported_count": 0}

    # Build searchable knowledge text
    all_knowledge = " ".join(k.get("content", "") for k in knowledge_context)

    # Extract numeric facts from answer
    facts = _extract_numbers(answer)

    if not facts:
        return {"consistent": True, "suspicious_facts": [], "checked_count": 0, "supported_count": 0}

    suspicious = []
    supported = 0
    for fact in facts:
        if fact in all_knowledge:
            supported += 1
        else:
            # Fuzzy check: the number itself appears nearby in knowledge
            num_part = re.search(r"(\d+[\.\d]*)", fact)
            if num_part:
                if num_part.group(1) in all_knowledge:
                    supported += 1
                    continue
            suspicious.append(fact)

    reason = ""
    if suspicious:
        reason = f"facts unsupported: {suspicious}"
    elif facts and supported < len(facts):
        reason = f"{supported}/{len(facts)} facts verified"

    return {
        "consistent": len(suspicious) == 0,
        "pass": len(suspicious) == 0,
        "suspicious_facts": suspicious,
        "checked_count": len(facts),
        "supported_count": supported,
        "reason": reason,
    }


def check_spot_consistency(answer: str, query: str, knowledge_context: list[dict[str, Any]]) -> dict:
    """Check if the answer discusses the same spot(s) the user asked about."""
    # Extract spot names from both query and knowledge context
    spot_pattern = re.compile(r"(灵山大佛|灵山梵宫|梵宫|九龙灌浴|五印坛城|祥符禅寺|佛足坛|五明桥"
                              r"|五智门|菩提大道|降魔浮雕|阿育王柱|百子戏弥勒|无尽意斋"
                              r"|梵天花海|香月花街|拈花广场|拈花堂|鹿鸣谷|灵山精舍|曼飞龙塔"
                              r"|佛教文化博览馆|灵山大照壁)")

    # Common cross-spot associations (e.g., asking about 梵宫 might mention 九龙灌浴 in comparison)
    _CROSS_ASSOCIATIONS = {
        "梵宫": {"灵山梵宫", "五印坛城"},
        "灵山梵宫": {"五印坛城", "曼飞龙塔"},
        "九龙灌浴": {"菩提大道", "降魔浮雕"},
        "灵山大佛": {"祥符禅寺", "佛教文化博览馆"},
        "五印坛城": {"灵山梵宫", "曼飞龙塔"},
    }

    query_spots = set(spot_pattern.findall(query))
    answer_spots = set(spot_pattern.findall(answer))
    knowledge_spots = set()
    for k in knowledge_context:
        content = k.get("content", "")
        knowledge_spots.update(spot_pattern.findall(content))
        tags = k.get("tags", [])
        if isinstance(tags, list):
            knowledge_spots.update(tags)

    if not query_spots:
        return {"consistent": True, "issue": None}

    # Check: does the answer talk about what the user asked?
    missing = query_spots - answer_spots
    allowed = set()
    for qs in query_spots:
        allowed.update(_CROSS_ASSOCIATIONS.get(qs, set()))

    truly_missing = missing - allowed

    # If the answer doesn't mention the asked spot but the knowledge context does, it's suspicious
    if truly_missing and answer and len(answer) > 10:
        return {
            "consistent": False,
            "issue": f"用户询问{truly_missing}，但回答未提及",
        }

    return {"consistent": True, "issue": None}
