from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import time
import math


PLUTCHIK_WHEEL = {
    "joy":         {"polar": (0.8, 0.6), "fallback_action": "wave",   "antagonist": "sadness"},
    "trust":       {"polar": (0.6, 0.4), "fallback_action": "nod",    "antagonist": "disgust"},
    "fear":        {"polar": (-0.6, 0.6),"fallback_action": "tilt",   "antagonist": "anger"},
    "surprise":    {"polar": (0.2, 0.9), "fallback_action": "tilt",   "antagonist": "anticipation"},
    "sadness":     {"polar": (-0.8, -0.4),"fallback_action": "bow",   "antagonist": "joy"},
    "disgust":     {"polar": (-0.5, 0.2),"fallback_action": "shake",  "antagonist": "trust"},
    "anger":       {"polar": (-0.4, 0.8),"fallback_action": "point",  "antagonist": "fear"},
    "anticipation":{"polar": (0.4, 0.3), "fallback_action": "gesture","antagonist": "surprise"},
}

EMOTION_INTENSITIES = {
    "joy":      ["serenity", "joy", "ecstasy"],
    "trust":    ["acceptance", "trust", "admiration"],
    "fear":     ["apprehension", "fear", "terror"],
    "surprise": ["distraction", "surprise", "amazement"],
    "sadness":  ["pensiveness", "sadness", "grief"],
    "disgust":  ["boredom", "disgust", "loathing"],
    "anger":    ["annoyance", "anger", "rage"],
    "anticipation": ["interest", "anticipation", "vigilance"],
}

EMOTION_VALID_ACTIONS = {
    "joy":  ["wave", "spread", "nod", "tilt", "openHand"],
    "trust":["nod", "gesture", "wave", "spread", "comfort"],
    "fear": ["tilt", "shake", "bow", "crossArms"],
    "surprise": ["tilt", "wave", "spread", "gesture", "openHand"],
    "sadness":  ["bow", "shake", "nod", "comfort"],
    "disgust":  ["shake", "point", "tilt", "crossArms"],
    "anger":    ["point", "shake", "wave", "crossArms"],
    "anticipation": ["gesture", "spread", "think", "nod", "point"],
}


@dataclass
class Emotion:
    primary: str = "trust"
    secondary: Optional[str] = None
    intensity: float = 0.5
    energy: float = 0.4
    valence: float = 0.6

    def to_dict(self) -> dict:
        return {
            "primary": self.primary,
            "secondary": self.secondary,
            "intensity": round(self.intensity, 2),
            "energy": round(self.energy, 2),
            "valence": round(self.valence, 2),
        }


class EmotionStateMachine:
    def __init__(self, persona_bias: str = "trust", decay_rate: float = 0.02, volatility: float = 0.3):
        polar = PLUTCHIK_WHEEL.get(persona_bias, PLUTCHIK_WHEEL["trust"])
        self._decay_rate = decay_rate
        self._persona_bias = persona_bias
        self._volatility = volatility  # 0 稳定 → 1 易变
        self._last_update = time.time()
        v, e = polar["polar"]
        self._emotion = Emotion(
            primary=persona_bias,
            intensity=0.4,
            valence=v,
            energy=e,
        )
        self._emotional_inertia = 0.0

    @property
    def current(self) -> Emotion:
        return self._emotion

    def update_from_mood(self, mood: str, intensity: float):
        if mood not in PLUTCHIK_WHEEL:
            return
        polar = PLUTCHIK_WHEEL[mood]
        v, e = polar["polar"]
        self._emotion = Emotion(
            primary=mood,
            secondary=self._emotion.primary if mood != self._emotion.primary else self._emotion.secondary,
            intensity=min(1.0, intensity),
            valence=v * intensity + self._emotion.valence * (1 - intensity),
            energy=e * intensity + self._emotion.energy * (1 - intensity),
        )
        self._last_update = time.time()

    def update_from_llm(self, primary: str, secondary: str | None, intensity: float):
        if primary not in PLUTCHIK_WHEEL:
            return
        # 情绪惯性：当前情绪强烈时更难转变
        inertia_bonus = self._emotional_inertia * 0.3
        effective_intensity = intensity * (1 - inertia_bonus * 0.5)
        if primary == self._emotion.primary:
            effective_intensity += 0.15  # 同一情绪更容易巩固

        polar = PLUTCHIK_WHEEL[primary]
        v, e = polar["polar"]
        if effective_intensity >= 0.6:
            self._emotion = Emotion(
                primary=primary,
                secondary=secondary or self._emotion.primary,
                intensity=min(1.0, intensity),
                valence=v,
                energy=e,
            )
            self._emotional_inertia = min(1.0, self._emotional_inertia + 0.15)
        elif intensity >= 0.3:
            alpha = effective_intensity
            self._emotion = Emotion(
                primary=primary,
                secondary=secondary or self._emotion.primary,
                intensity=min(1.0, self._emotion.intensity * (1-alpha) + intensity * alpha),
                valence=self._emotion.valence * (1-alpha*0.5) + v * alpha*0.5,
                energy=self._emotion.energy * (1-alpha*0.5) + e * alpha*0.5,
            )
            self._emotional_inertia = max(0, self._emotional_inertia - 0.1)
        else:
            self._emotional_inertia = max(0, self._emotional_inertia - 0.15)
        self._last_update = time.time()

    def decay_step(self):
        now = time.time()
        elapsed = now - self._last_update
        if elapsed > 30:
            decay = min(1.0, (elapsed - 30) * self._decay_rate)
            polar = PLUTCHIK_WHEEL.get(self._persona_bias, PLUTCHIK_WHEEL["trust"])
            v, e = polar["polar"]
            self._emotion.intensity = max(0.2, self._emotion.intensity * (1 - decay))
            self._emotion.valence = self._emotion.valence * (1 - decay*0.3) + v * decay*0.3
            self._emotion.energy = self._emotion.energy * (1 - decay*0.3) + e * decay*0.3
            self._emotional_inertia = max(0, self._emotional_inertia - decay * 0.5)
            if self._emotion.intensity < 0.25:
                self._emotion.primary = self._persona_bias
                self._emotion.secondary = None

    def get_suggested_actions(self, max_count: int = 2) -> list[str]:
        primary_actions = EMOTION_VALID_ACTIONS.get(self._emotion.primary, ["gesture"])
        secondary_actions = EMOTION_VALID_ACTIONS.get(self._emotion.secondary, []) if self._emotion.secondary else []
        combined = primary_actions + [a for a in secondary_actions if a not in primary_actions]
        return combined[:max_count]

    def get_expression_blend(self) -> dict:
        # 关键修复: VRM 1.0 模型只支持 14 个标准 preset
        # (happy/angry/sad/relaxed/surprised/aa/ih/ou/ee/oh/blink/blinkLeft/blinkRight/neutral)
        # 之前的 ARKit 名 (mouthSmile/browInnerUp/eyeWideLeft...) 在模型上都是静默 no-op.
        # 这里改写为只用标准 preset 名. 前端 setBlendShapeTarget 仍有 ARKit 兜底映射, 双保险.
        base = {
            "joy":       {"happy": 0.85, "surprised": 0.1},
            "trust":     {"happy": 0.45, "relaxed": 0.35},
            "fear":      {"surprised": 0.6, "angry": 0.15, "aa": 0.2},
            "surprise":  {"surprised": 0.9, "aa": 0.25},
            "sadness":   {"sad": 0.85, "relaxed": 0.15},
            "disgust":   {"angry": 0.55, "sad": 0.3},
            "anger":     {"angry": 0.9, "aa": 0.1},
            "anticipation": {"happy": 0.35, "surprised": 0.4, "relaxed": 0.2},
        }
        primary_blend = base.get(self._emotion.primary, {})
        secondary_blend = base.get(self._emotion.secondary) if self._emotion.secondary else {}
        i = self._emotion.intensity
        blended = {}
        for k, v in primary_blend.items():
            blended[k] = round(v * i, 2)
        for k, v in secondary_blend.items():
            if k in blended:
                blended[k] = round(min(1.0, blended[k] + v * i * 0.3), 2)
            else:
                blended[k] = round(v * i * 0.3, 2)
        return blended


emotion_sm = EmotionStateMachine()
