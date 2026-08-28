from __future__ import annotations
import re
import json
import time
from typing import Optional
from emotion_state import emotion_sm, EmotionStateMachine


SENTIMENT_KEYWORDS = {
    "strong_positive": ["太棒了", "太美了", "震撼", "感动", "完美", "绝了", "惊叹", "了不起", "非常喜欢", "超级"],
    "positive": ["好", "喜欢", "棒", "赞", "谢谢", "不错", "满意", "太好了", "真棒", "开心", "高兴", "很棒", "优秀", "感谢", "很有意思", "有趣", "精彩", "漂亮", "美", "赞", "厉害", "牛"],
    "slight_positive": ["可以", "还行", "还好", "不错", "挺好"],
    "slight_negative": ["一般", "普通", "还行吧", "没什么", "凑合"],
    "negative": ["差", "不好", "糟糕", "失望", "无聊", "讨厌", "后悔", "没意思", "无语", "太差", "垃圾", "难看", "难吃", "糟糕透顶", "烦", "烦人", "差劲"],
    "strong_negative": ["太差了", "最差", "恶心", "受不了", "愤怒", "气人", "过分", "无法忍受"],
    "concern": ["迷路", "找不到", "累", "饿", "渴", "热", "冷", "担心", "怕", "害怕", "紧张", "不舒服", "晕", "肚子疼", "头疼"],
    "urgent": ["快", "急", "赶时间", "马上", "立刻", "赶紧", "快点", "来不及了", "要迟到了", "赶不上"],
    "curiosity": ["为什么", "怎么", "什么", "怎么回事", "好奇", "想知道", "能不能讲讲", "解释", "说明"],
    "appreciation": ["谢谢", "感谢", "辛苦了", "太感谢", "多谢", "感恩"],
}

CONTEXT_ACTION_KEYWORDS = {
    "greeting":    ["你好", "您好", "嗨", "哈喽", "hello", "hi", "早上好", "下午好", "晚上好"],
    "agreement":   ["是的", "对的", "没错", "当然", "确实", "很好", "好的", "没问题", "可以", "行", "好"],
    "disagreement":["不", "没", "别", "不行", "不能", "不对", "不是", "不要"],
    "suggestion":  ["推荐", "建议", "可以", "适合", "值得", "可以试试", "试试"],
    "introduction":["是", "就是", "位于", "坐落", "建于", "这是", "这里有", "我们有", "景区", "景点", "叫做", "称为"],
    "thinking":    ["呢", "吗", "什么", "怎么", "为什么", "如何", "考虑", "嗯", "让我想想", "这个"],
    "farewell":    ["再见", "拜拜", "回头见", "下次见", "bye"],
}


def analyze_sentiment_score(text: str) -> dict:
    if not text:
        return {"score": 0, "label": "neutral", "intensity": 0.3}
    lower = text.lower()
    score = 0
    for w in SENTIMENT_KEYWORDS["strong_positive"]:
        if w in lower: score += 3
    for w in SENTIMENT_KEYWORDS["positive"]:
        if w in lower: score += 1
    for w in SENTIMENT_KEYWORDS["appreciation"]:
        if w in lower: score += 2
    for w in SENTIMENT_KEYWORDS["curiosity"]:
        if w in lower: score += 1
    for w in SENTIMENT_KEYWORDS["slight_positive"]:
        if w in lower: score += 0.5
    for w in SENTIMENT_KEYWORDS["slight_negative"]:
        if w in lower: score -= 0.5
    for w in SENTIMENT_KEYWORDS["negative"]:
        if w in lower: score -= 1
    for w in SENTIMENT_KEYWORDS["strong_negative"]:
        if w in lower: score -= 3
    for w in SENTIMENT_KEYWORDS["concern"]:
        if w in lower: score -= 1
    for w in SENTIMENT_KEYWORDS["urgent"]:
        if w in lower: score += 2

    intensity = min(1.0, abs(score) * 0.15 + 0.3)
    if score >= 3:    return {"score": score, "label": "strong_positive", "intensity": intensity, "mood": "joy"}
    if score >= 1:    return {"score": score, "label": "positive", "intensity": intensity, "mood": "joy"}
    if score <= -3:   return {"score": score, "label": "strong_negative", "intensity": intensity, "mood": "anger"}
    if score <= -1:   return {"score": score, "label": "negative", "intensity": intensity, "mood": "sadness"}
    return {"score": score, "label": "neutral", "intensity": 0.3, "mood": "trust"}


def detect_user_intent(text: str) -> str | None:
    lower = text.lower()
    for intent, keywords in CONTEXT_ACTION_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                return intent
    return None


def parse_llm_json_reply(reply: str) -> dict:
    json_pattern = r'\{[^{}]*"reply"[^{}]*\}'
    match = re.search(json_pattern, reply, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            return data
        except json.JSONDecodeError:
            pass
    emotion_match = re.search(r'\[emotion:\s*(\w+)(?::(\w+))?(?::([\d.]+))?\]', reply)
    action_matches = re.findall(r'\[action:\s*(\w+)(?::(\d+))?\]', reply)
    clean = reply
    clean = re.sub(r'\[emotion:\s*\w+(?::\w+)?(?:[\d.]+)?\]', '', clean)
    clean = re.sub(r'\[action:\s*\w+(?::\d+)?\]', '', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    emotion_data = {}
    if emotion_match:
        primary = emotion_match.group(1)
        secondary = emotion_match.group(2) if emotion_match.group(2) else None
        intensity = float(emotion_match.group(3)) if emotion_match.group(3) else 0.6
        emotion_data = {
            "primary": primary,
            "secondary": secondary,
            "intensity": min(1.0, intensity),
        }
    actions = []
    for a in action_matches:
        action_name = a[0]
        priority = int(a[1]) if a[1] else 1
        actions.append({"type": action_name, "priority": priority})
    return {
        "reply": clean,
        "emotion": emotion_data,
        "actions": actions,
    }


def estimate_tts_duration_ms(text: str, ms_per_char: float = 220.0) -> int:
    """根据文本长度与标点估算 TTS 时长 (ms).

    edge-tts / CosyVoice 实际朗读速度约 200-260ms/字, 句末标点会引入额外停顿.
    比直接 len(text)*80 更接近真实音频长度, 让动作时间轴与音频更同步.
    若有真实 mp3 字节, 用 mp3_duration_ms() 优先.
    """
    if not text:
        return 0
    base = len(text) * ms_per_char
    pauses = 0
    for ch in text:
        if ch in "，、；,;":
            pauses += 180
        elif ch in "。！？!?\n":
            pauses += 450
        elif ch in "：:·":
            pauses += 220
    return int(base + pauses)


def mp3_duration_ms(data: bytes) -> int | None:
    """从 mp3 字节流解析出实际时长 (ms). 不依赖 ffmpeg, 仅解析 frame 头.

    edge-tts 默认输出 MPEG1 Layer3 / 24kbps mono 24kHz, 这里兼容常见格式.
    若解析失败返回 None, 调用方应回退到 estimate_tts_duration_ms().
    """
    if not data or len(data) < 32:
        return None
    # MPEG1 Layer3 bitrate table (kbps) by index
    bitrate_table_v1_l3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
    # MPEG2/2.5 Layer3 bitrate table
    bitrate_table_v2_l3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
    sample_rate_table_v1 = [44100, 48000, 32000, 0]
    sample_rate_table_v2 = [22050, 24000, 16000, 0]

    # 跳过 ID3v2 标签 (前 10 字节: "ID3" + version(2) + flags(1) + size(4))
    pos = 0
    if data[:3] == b"ID3":
        try:
            size_bytes = data[6:10]
            size = (size_bytes[0] << 21) | (size_bytes[1] << 14) | (size_bytes[2] << 7) | size_bytes[3]
            pos = 10 + size
        except Exception:
            pos = 0

    total_frames = 0
    total_samples = 0  # 每帧的样本数累加, 之后除以采样率得到秒
    n = len(data)
    while pos < n - 4:
        # 找 11-bit 同步字 (0xFFE00000 的高 11 位 = 0x7FF)
        if data[pos] != 0xFF:
            pos += 1
            continue
        b1, b2, b3 = data[pos + 1], data[pos + 2], data[pos + 3]
        if (b1 & 0xE0) != 0xE0:
            pos += 1
            continue
        version_id = (b1 >> 3) & 0x3   # 0=MPEG2.5, 2=MPEG2, 3=MPEG1
        layer = (b1 >> 1) & 0x3        # 1=LayerIII
        bitrate_idx = (b2 >> 4) & 0xF
        sr_idx = (b2 >> 2) & 0x3
        padding = (b2 >> 1) & 0x1
        if layer != 1:
            pos += 1
            continue
        if version_id == 3:  # MPEG1
            bitrate = bitrate_table_v1_l3[bitrate_idx] * 1000
            sample_rate = sample_rate_table_v1[sr_idx]
            samples_per_frame = 1152
        else:  # MPEG2 / 2.5
            bitrate = bitrate_table_v2_l3[bitrate_idx] * 1000
            sample_rate = sample_rate_table_v2[sr_idx]
            samples_per_frame = 576
        if bitrate <= 0 or sample_rate <= 0:
            pos += 1
            continue
        frame_size = (samples_per_frame // 8 * bitrate // sample_rate) + padding
        if frame_size < 24:
            pos += 1
            continue
        total_frames += 1
        total_samples += samples_per_frame
        pos += frame_size
        # 早停: 已扫到 256 帧以上, 数据已经足够, 按帧率估算剩余
        if total_frames >= 256:
            # 总字节 - 当前 pos ≈ 剩余字节, 按当前 bitrate 推算
            remaining_bytes = max(0, n - pos)
            if bitrate > 0:
                est_remaining_ms = (remaining_bytes * 8 * 1000) // bitrate
                cur_ms = (total_samples * 1000) // sample_rate
                return cur_ms + est_remaining_ms
    if total_frames == 0 or sample_rate == 0:
        return None
    return int((total_samples * 1000) // sample_rate)


def build_action_timeline(
    llm_actions: list[dict] | None,
    suggested_actions: list[str],
    reply_text: str,
    tts_duration_ms: int,
    intent: str | None,
) -> list[dict]:
    import random
    timeline = []
    used = set()
    spoken_ms = max(tts_duration_ms, len(reply_text) * 80)

    # 对 action 加入少量随机抖动，让同一个回复每次表现略有不同
    rng_seed = random.Random(len(reply_text) + int(time.time() * 1000) % 30000)

    for a in (llm_actions or []):
        atype = a.get("type", "")
        if atype not in used and atype in {"wave", "nod", "shake", "bow", "tilt", "gesture", "spread", "point", "think"}:
            jitter = rng_seed.randint(0, 400)
            start = len(timeline) * 1200 + jitter
            duration = 800 + rng_seed.randint(0, 600)
            timeline.append({"type": atype, "startMs": start, "endMs": start + duration, "priority": a.get("priority", 1)})
            used.add(atype)

    intent_action_map = {
        "greeting": "wave", "agreement": "nod", "disagreement": "shake",
        "suggestion": "spread", "introduction": "gesture", "thinking": "think", "farewell": "wave",
    }
    if intent and intent in intent_action_map:
        action_name = intent_action_map[intent]
        if action_name not in used:
            start = 300 + rng_seed.randint(0, 300)
            timeline.append({"type": action_name, "startMs": start, "endMs": start + 1200, "priority": 2})
            used.add(action_name)

    for sa in suggested_actions:
        if sa not in used and spoken_ms > 2000:
            start = max(1800, spoken_ms - 1200) + rng_seed.randint(-200, 200)
            timeline.append({"type": sa, "startMs": start, "endMs": start + 1200, "priority": 1})
            used.add(sa)

    if not timeline and spoken_ms > 1500:
        gesture_count = min(2, max(1, spoken_ms // 3000))
        spread_ms = max(200, (spoken_ms - 1500) // gesture_count)
        for i in range(gesture_count):
            start = 800 + i * spread_ms + rng_seed.randint(-100, 100)
            timeline.append({"type": "gesture" if gesture_count > 1 else ("nod" if rng_seed.random() < 0.5 else "gesture"), "startMs": max(0, start), "endMs": max(0, start) + 1000, "priority": 0})
    return timeline


def build_emotion_response(
    reply_text: str,
    llm_emotion: dict | None,
    llm_actions: list[dict],
    tts_duration_ms: int,
    user_message: str | None = None,
) -> dict:
    if user_message:
        sent = analyze_sentiment_score(user_message)
        if sent["intensity"] > 0.4 and sent["mood"]:
            emotion_sm.update_from_mood(sent["mood"], sent["intensity"])

    if llm_emotion and llm_emotion.get("primary"):
        emotion_sm.update_from_llm(
            llm_emotion["primary"],
            llm_emotion.get("secondary"),
            llm_emotion.get("intensity", 0.6),
        )
    else:
        emotion_sm.decay_step()

    intent = detect_user_intent(reply_text) if user_message else None

    suggested = emotion_sm.get_suggested_actions()
    timeline = build_action_timeline(llm_actions, suggested, reply_text, tts_duration_ms, intent)
    expression = emotion_sm.get_expression_blend()
    emotion = emotion_sm.current

    payload = {
        "emotion": emotion.to_dict(),
        "actions": timeline,
        "expression": expression,
    }
    return payload
