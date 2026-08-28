(function () {
    const state = {
        sessionId: `session-${Date.now()}`,
        userId: `user-${Math.random().toString(36).slice(2, 8)}`,
        interest: "history",
        speechEnabled: true,
        lastConversationId: null,
        selectedFeedback: null,
        lastReplyText: "",
        isListening: false,
        currentEmotion: "warm",
        emotionHistory: [],
        webcamStream: null,
        modelsLoaded: false,
        avatarMood: "neutral",
        lastAudioUrl: null,
        lastAudioBase64: null,
        gpsWatchId: null,
        gpsCoords: null,
        gpsEnabled: false,
        streamMode: true,
        activeController: null,
        requestSeq: 0,
        // 关键修复: 跟踪自动播放定时器, 防止多定时器互相打断
        pendingAudioTimer: null,
        // 关键修复: AudioManager 内部 generation 计数, 防止 onended 误抹状态
        audioGen: 0,
        lastMentionedSpots: [],
        // 关键修复: 静音期间被保留的整条回复音频, 重新开启播报时由 resumeSpeech 重放
        mutedResume: null,
        modelId: "",
        modelOptions: [],
        modelSwitching: false,
    };

    const emotionConfig = {
        warm: { label: "亲和讲解", avatarState: "smile", cssClass: "emotion-warm" },
        delighted: { label: "积极回应", avatarState: "bright-smile", cssClass: "emotion-delighted" },
        focused: { label: "高效导览", avatarState: "attentive", cssClass: "emotion-focused" },
        caring: { label: "安抚陪伴", avatarState: "gentle", cssClass: "emotion-caring" },
        sad: { label: "同情理解", avatarState: "sad", cssClass: "emotion-sad" },
        neutral: { label: "正常讲解", avatarState: "neutral", cssClass: "emotion-neutral" },
        speaking: { label: "讲解中", avatarState: "speaking", cssClass: "emotion-speaking" },
    };

    const chineseSentimentWords = {
        positive: ["好", "喜欢", "棒", "赞", "谢谢", "不错", "满意", "太好了", "真棒", "完美", "开心", "高兴", "很棒", "优秀", "感谢", "太棒了", "很有意思", "有趣", "精彩"],
        negative: ["差", "不好", "糟糕", "失望", "无聊", "讨厌", "失望", "后悔", "没意思", "无语", "太差", "垃圾", "烂", "难看", "难吃", "糟糕透顶"],
        concern: ["迷路", "找不到", "累", "饿", "渴", "热", "冷", "担心", "怕", "害怕", "紧张", "不舒服", "晕", "肚子疼", "头疼"],
        urgent: ["快", "急", "赶时间", "马上", "立刻", "赶紧", "快点", "来不及了", "要迟到了", "赶不上"],
    };

    function analyzeSentiment(text) {
        if (!text) return "neutral";
        const lower = text.toLowerCase();
        let score = 0;
        chineseSentimentWords.positive.forEach(w => { if (lower.includes(w)) score++; });
        chineseSentimentWords.negative.forEach(w => { if (lower.includes(w)) score--; });
        chineseSentimentWords.urgent.forEach(w => { if (lower.includes(w)) score += 2; });
        if (score >= 2) return "delighted";
        if (score >= 1) return "warm";
        if (score <= -2) return "sad";
        if (score <= -1) return "caring";
        return "neutral";
    }

    function detectEmotionFromMessage(text) {
        if (!text) return { emotion: "neutral", emotionPayload: emotionConfig.neutral };
        const hasConcern = chineseSentimentWords.concern.some(w => text.includes(w));
        const hasUrgent = chineseSentimentWords.urgent.some(w => text.includes(w));
        const hasPositive = chineseSentimentWords.positive.some(w => text.includes(w));
        if (hasUrgent) return { emotion: "focused", emotionPayload: emotionConfig.focused };
        if (hasConcern) return { emotion: "caring", emotionPayload: emotionConfig.caring };
        if (hasPositive) return { emotion: "delighted", emotionPayload: emotionConfig.delighted };
        const score = analyzeSentiment(text);
        return { emotion: score, emotionPayload: emotionConfig[score] || emotionConfig.neutral };
    }

    const els = {
        chatLog: document.getElementById("chat-log"),
        chatInput: document.getElementById("chat-input"),
        sendBtn: document.getElementById("send-btn"),
        voiceBtn: document.getElementById("voice-btn"),
        speakToggle: document.getElementById("speak-toggle"),
        avatarStatus: document.getElementById("avatar-status"),
        emotionLabel: document.getElementById("emotion-label"),
        routeName: document.getElementById("route-name"),
        routeDuration: document.getElementById("route-duration"),
        routePitch: document.getElementById("route-pitch"),
        routeStops: document.getElementById("route-stops"),
        interestGrid: document.getElementById("interest-grid"),
        avatarStage: document.getElementById("avatar-stage"),
        avatarModelSelect: document.getElementById("avatar-model-select"),
        avatarModelStatus: document.getElementById("avatar-model-status"),
        briefName: document.getElementById("brief-name"),
        briefPositioning: document.getElementById("brief-positioning"),
        capabilityChips: document.getElementById("capability-chips"),
        feedbackBar: document.getElementById("feedback-bar"),
        feedbackLabel: document.getElementById("feedback-label"),
        traceSources: document.getElementById("trace-sources"),
        answerModeLabel: document.getElementById("answer-mode-label"),
        starBtns: document.querySelectorAll(".star-btn"),
        feedbackSubmit: document.getElementById("btn-feedback-submit"),
        mapContainer: document.getElementById("amap-container"),
        mapInfo: document.getElementById("map-info"),
        mapStatus: document.getElementById("map-status"),
        mapBody: document.getElementById("map-body"),
        mapToggle: document.getElementById("map-toggle"),
        mapToggleIcon: document.getElementById("map-toggle-icon"),
        mapSection: document.getElementById("map-section"),
        mapFullscreenClose: document.getElementById("map-fullscreen-close"),
        mapExpandBtn: document.getElementById("map-expand-btn"),
        weatherSection: document.getElementById("weather-section"),
        weatherIcon: document.getElementById("weather-icon"),
        weatherTemp: document.getElementById("weather-temp"),
        weatherDesc: document.getElementById("weather-desc"),
        weatherHumidity: document.getElementById("weather-humidity"),
        weatherWind: document.getElementById("weather-wind"),
        weatherFeels: document.getElementById("weather-feels"),
        weatherTip: document.getElementById("weather-tip"),
        weatherLoc: document.getElementById("weather-loc"),
    };

    const apiBaseUrl = String(window.APP_CONFIG?.apiBaseUrl || "").trim().replace(/\/+$/, "");

    function apiUrl(path) {
        return apiBaseUrl && path.startsWith("/api/") ? `${apiBaseUrl}${path}` : path;
    }

    function backendUrl(path) {
        return apiBaseUrl ? `${apiBaseUrl}${path}` : path;
    }

    function normalizeBackendAudioUrl(url) {
        if (!url || /^(?:data:|blob:|[a-z][a-z0-9+.-]*:)/i.test(url)) return url;
        return url.startsWith("/") ? backendUrl(url) : url;
    }

    function createPlaybackAudio(url) {
        const audio = new Audio();
        const source = normalizeBackendAudioUrl(url);
        if (!/^(?:data:|blob:)/i.test(source)) audio.crossOrigin = "anonymous";
        audio.src = source;
        return audio;
    }

    const adminLink = document.getElementById("admin-link");
    if (adminLink) adminLink.href = backendUrl("/admin");

    async function api(url, options = {}) {
        const response = await fetch(apiUrl(url), {
            headers: { "Content-Type": "application/json", ...(options.headers || {}) },
            ...options,
        });
        if (!response.ok) {
            const text = await response.text().catch(() => "");
            throw new Error(`API ${response.status}: ${text || response.statusText}`);
        }
        return response.json();
    }

    function updateAvatarMood(mood) {
        state.avatarMood = mood;
        state.emotionHistory.push({ mood, time: Date.now() });
        if (els.avatarStage) {
            els.avatarStage.className = "avatar-stage";
            els.avatarStage.classList.add(`mood-${mood}`);
        }
        const avatar = document.querySelector(".avatar");
        if (avatar) {
            avatar.className = "avatar";
            avatar.classList.add(`mood-${mood}`);
            if (mood === "speaking") avatar.classList.add("speaking");
        }
        const mouth = document.querySelector(".avatar-mouth");
        if (mouth) {
            mouth.className = "avatar-mouth";
            mouth.classList.add(`mouth-${mood}`);
        }
        applyVRMBlendShape(mood);
    }

  function applyVRMBlendShape(mood) {

    const vrm = state.vrm;

    if (!vrm || !vrm.expressionManager) return;

    // 先清空旧表情
    const expressions = [
        'happy',
        'angry',
        'sad',
        'relaxed',
        'surprised',
        'neutral'
    ];

   expressions.forEach(name => {

    // 不要重置嘴型
    if (['aa','ih','ou','ee','oh'].includes(name)) {
        return;
    }

    try {
        vrm.expressionManager.setValue(name, 0);
    } catch (e) {}
    });

    // 获取当前情绪表情
    const blendshapes = emotionBlendShapes[mood];

    if (!blendshapes) return;

    // 设置新表情
    Object.entries(blendshapes).forEach(([key, value]) => {
        try {
            vrm.expressionManager.setValue(key, value);
        } catch (e) {}
    });
}

    function renderRoute(route) {
        if (!route) return;
        if (els.routeName) els.routeName.textContent = route.name || "推荐路线";
        if (els.routeDuration) els.routeDuration.textContent = route.duration || "-";
        if (els.routePitch) els.routePitch.textContent = route.pitch || "系统根据您的偏好推荐路线。";
        if (els.routeStops) {
            els.routeStops.innerHTML = (route.stops || []).slice(0, 4).map((stop) =>
                `<span class="route-stop">${stop.name}</span>`
            ).join("");
        }
    }

    function setEmotion(emotion) {
        state.currentEmotion = emotion.label || "亲和讲解";
        if (els.emotionLabel) els.emotionLabel.textContent = emotion.label || "亲和讲解";
        if (els.avatarStatus) els.avatarStatus.textContent = "在线讲解中";
        const cssClass = emotion.cssClass || emotionConfig.warm.cssClass;
        const mood = cssClass.replace("emotion-", "");
        updateAvatarMood(mood);
        if (window.triggerActionByEmotion) window.triggerActionByEmotion(mood);
    }

    // ========== 中文拼音韵母 → VRM Viseme 映射表 ==========
    // 基于汉字常见读音的韵母分类, 用于文本驱动的口型预测
    const CHINESE_VISEME_MAP = (() => {
        const m = {};
        // aa 组: 开元音 a/ai/ao/an/ang/ia/ua
        '啊阿哎哀爱安暗按案岸昂八把巴吧拔爸怕爬帕大达打搭发法伐罚哈蛤卡喀拉啦辣蜡妈妈嘛麻骂码玛拿那哪纳娜趴撒洒萨他她它踏塔扎炸渣杂咋查茶察岔傻啥差杀沙纱丫呀压鸭牙芽涯瓦挖蛙哇花华画划化话瓜挂刮夸下夏虾峡辖狭瞎霞匣加家架价假佳夹甲嫁驾颊钾'.split('').forEach(c => m[c]='aa');
        // ee 组: 扁元音 i/in/ing
        '一以意议义益亿艺易衣医依已你里理利力例立历丽礼起气器弃汽期七妻齐几机级极积基记计纪及即际集技击激季己继既地弟第低底滴敌笛系细席习洗喜西希析息夕惜溪比必笔毕闭壁碧逼鼻皮脾匹僻屁疲迷米密蜜秘觅咪题提体替踢梯剔泥尼逆匿溺离璃篱厘黎梨李奇骑旗棋崎泣膝极集即急级及'.split('').forEach(c => m[c]='ee');
        // ou 组: 圆元音 u/uo/ou/ong/ü
        '不步部布补簿捕出初除楚础储触畜处都读独度渡杜肚堵赌福服伏府辅付附复副父富符幅赴古故顾鼓骨谷固雇菇咕沽湖胡户互护呼忽虎糊弧壶哭库裤酷枯窟骷鲁路炉露录陆鹿禄赂母目木幕慕墓牧穆亩普扑铺葡仆瀑如入乳汝辱蠕苏素速诉宿肃酥粟塑溯土突图途吐兔屠秃无五武务物雾舞屋吴午悟乌巫芜族足卒阻组租祖助住主注柱驻祝筑竹逐烛嘱瞩贮中终钟众重种忠衷从丛聪葱匆东冬动懂冻洞栋洪红宏虹鸿弘轰烘龙笼隆聋弄珑胧空孔控恐穹同通痛统铜筒桐瞳容溶熔蓉荣融绒冗工功共供攻贡巩宫弓恭送宋颂松耸诵讼用永泳勇涌拥佣庸'.split('').forEach(c => m[c]='ou');
        // ih 组: 中性元音 e/ei/en/eng/üe
        '的得德这着者折哲浙遮辄车扯彻撤澈社设射涉蛇奢赦摄热惹则责泽择色涩瑟个各哥歌鸽阁搁格格隔可刻客课壳渴克科棵颗和合河何荷核盒禾贺褐乐了勒么呢讷特北被备背悲杯碑贝倍辈每美没妹梅煤霉媒眉媚内馁配陪培赔裴沛飞非肥费肺废匪沸雷类累泪蕾磊垒给黑嘿很恨痕狠真针珍阵震振镇诊枕斟深身神审申伸慎肾渗绅人称认任何仁忍任森们门闷分芬纷氛粉份愤奋焚坟尘晨趁衬辰肯啃恳垦根跟怎恩嗯'.split('').forEach(c => m[c]='ih');
        return m;
    })();
    // 将文本转换为 viseme 时间线: [{char, viseme, duration}]
    function buildVisemeTimeline(text) {
        if (!text) return [];
        const timeline = [];
        const charsPerSec = 8; // 平均每秒 8 个汉字
        for (let i = 0; i < text.length; i++) {
            const ch = text[i];
            const viseme = CHINESE_VISEME_MAP[ch] || 'aa'; // 未映射的默认开元音
            timeline.push({ char: ch, viseme });
        }
        return timeline;
    }

    // ========== Audio Manager (单例: 复用AudioContext + 杜绝重叠) ==========
    class AudioManager {
        constructor() {
            this.currentAudio = null;
            this.audioContext = null;
            this.active = false;
        }

        getContext() {
            if (!this.audioContext || this.audioContext.state === 'closed') {
                this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
            }
            if (this.audioContext.state === 'suspended') {
                this.audioContext.resume();
            }
            return this.audioContext;
        }

        stop() {
            this.active = false;
            this._lipDriver = null;
            window.currentLipValue = 0;
            window.currentViseme = null;
            window.vrmAudioElement = null;
            window._audioFirstPlayTime = 0;
            // 关键修复: 每次 stop 自增 generation, 让过期 onended 失效
            state.audioGen++;
            if (this.currentAudio) {
                try {
                    // 关键修复: 先解绑 handler, 避免 stop 期间 onended 误触
                    this.currentAudio.onended = null;
                    this.currentAudio.onerror = null;
                    this.currentAudio.pause();
                    this.currentAudio.currentTime = 0;
                    // 优化: 移除 removeAttribute('src')+load(), 它们触发浏览器网络中断, 纯浪费时间
                } catch (e) {}
                this.currentAudio = null;
            }
            if (typeof window.speechSynthesis !== 'undefined') {
                try { window.speechSynthesis.cancel(); } catch (e) {}
            }
            if (typeof segmentPlayer !== 'undefined' && segmentPlayer.currentAudio) {
                try { segmentPlayer.currentAudio.pause(); } catch (e) {}
            }
        }

        playAudio(url, fallbackText, onResult) {
            if (!state.speechEnabled) return;
            const normalizedUrl = normalizeBackendAudioUrl(url);
            // 关键修复: 正在播同样 URL 时, 避免重启
            if (this.currentAudio && !this.currentAudio.ended && this.currentAudio.src === normalizedUrl) {
                if (typeof onResult === 'function') onResult(true);
                return;
            }
            this.stop();
            this.active = true;
            const myGen = state.audioGen;
            let resultReported = false;
            const reportResult = (ok) => {
                if (resultReported) return;
                resultReported = true;
                if (typeof onResult === 'function') onResult(ok);
            };
            try {
                // 关键修复: URL 加 cache-busting, 防止浏览器命中过期缓存
                const cacheBustUrl = normalizedUrl + (normalizedUrl.includes('?') ? '&' : '?') + '_t=' + Date.now();
                const audio = createPlaybackAudio(cacheBustUrl);
                this.currentAudio = audio;
                window.vrmAudioElement = audio;
                audio.addEventListener('ended', () => {
                    // 关键修复: generation 检查, 过期事件直接 return
                    if (myGen !== state.audioGen) return;
                    this.active = false;
                    this._lipDriver = null;
                    window.currentLipValue = 0;
                    window.currentViseme = null;
                    updateAvatarMood("warm");
                    const avatar = document.querySelector(".avatar");
                    if (avatar) avatar.classList.remove("speaking");
                });
                audio.addEventListener('error', (e) => {
                    if (myGen !== state.audioGen) return;
                    console.warn('[Audio] play error:', e);
                    this.active = false;
                    reportResult(false);
                });
                audio.addEventListener('pause', () => {
                    if (myGen !== state.audioGen) return;
                    this.active = false;
                    window.currentLipValue = 0;
                    window.currentViseme = null;
                });
                this._connectAudio(audio);
                audio.play().then(() => {
                    if (myGen !== state.audioGen) return;
                    if (!window._audioFirstPlayTime) {
                        window._audioFirstPlayTime = performance.now();
                        console.log(`[Audio] first play timestamp recorded: ${window._audioFirstPlayTime.toFixed(0)}`);
                    }
                    this._syncLipWithAudio(audio, fallbackText);
                    reportResult(true);
                }).catch((e) => {
                    if (myGen !== state.audioGen) return;
                    console.warn('[Audio] play() rejected:', e);
                    this.active = false;
                    reportResult(false);
                });
            } catch (error) {
                console.warn('audio playback failed', error);
                this.active = false;
                reportResult(false);
            }
        }

        _speak(text) {
            if (!state.speechEnabled || !('speechSynthesis' in window)) return;
            let clean = (text || '').trim();
            clean = clean.replace(/\*\*(.+?)\*\*/g, '$1').replace(/\*(.+?)\*/g, '$1');
            clean = clean.replace(/__(.+?)__/g, '$1').replace(/_(.+?)_/g, '$1');
            clean = clean.replace(/`(.+?)`/g, '$1').replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
            clean = clean.replace(/#{1,6}\s+/g, '').replace(/\*/g, '');
            if (!clean) return;
            this.stop();
            this.active = true;
            // 关键修复: Web Speech 长度切分, Chrome 单次 ~200字/Safari ~30s 上限
            // 按句号/问号/感叹号切, 每段不超过 120 字
            const CHUNK_SIZE = 120;
            const chunks = [];
            if (clean.length <= CHUNK_SIZE) {
                chunks.push(clean);
            } else {
                // 先按句切, 太长再按标点切
                const sentences = clean.split(/(?<=[。！？；;])/);
                let cur = '';
                for (const s of sentences) {
                    if ((cur + s).length > CHUNK_SIZE && cur) {
                        chunks.push(cur);
                        cur = s;
                    } else {
                        cur += s;
                    }
                }
                if (cur) chunks.push(cur);
            }
            // 关键修复: 用 onend 链式触发, 而不是排队 (speechSynthesis 内部会按顺序播)
            const myGen = state.audioGen;
            let i = 0;
            const speakNext = () => {
                if (myGen !== state.audioGen) return; // 已被 stop
                if (i >= chunks.length) {
                    this.active = false;
                    return;
                }
                const chunk = chunks[i++];
                const utter = new SpeechSynthesisUtterance(chunk);
                utter.lang = 'zh-CN';
                utter.rate = 1;
                utter.pitch = 1;
                utter.volume = 1;
                utter.onstart = () => { if (myGen === state.audioGen) this._simulateLip(chunk); };
                utter.onend = () => { speakNext(); };
                utter.onerror = () => { speakNext(); };
                try { speechSynthesis.speak(utter); }
                catch (e) { speakNext(); }
            };
            // 关键修复: Chrome 在 cancel() 后立刻 speak() 偶尔静默失败, 先 resume() 解除暂停/卡死状态
            if (window.speechSynthesis && window.speechSynthesis.resume && window.speechSynthesis.paused) {
                try { window.speechSynthesis.resume(); } catch (e) {}
            }
            speakNext();
        }

        // 时间驱动口型：根据文本长度估算说话时长，在该时长内用节奏函数写入
        // window.currentLipValue（开口度）和 window.currentViseme（嘴型分类）。
        // 当有文本可用时，利用中文拼音 viseme 映射表预测每个汉字的韵母口型，
        // 使降级时（无音频频谱）的嘴型也更贴近实际发音内容。
        _simulateLip(text) {
            this._lipDriver = 'time';
            const duration = Math.max((text ? text.length : 6) * 120, 800);
            const startTime = Date.now();
            // 中文 viseme 时间线：将文本音节映射到韵母口型
            const timeline = text ? buildVisemeTimeline(text) : [];
            const charDuration = timeline.length > 0 ? duration / timeline.length : 120;
            let charIndex = 0;

            const tick = () => {
                if (!this.active || this._lipDriver !== 'time') {
                    window.currentLipValue = 0;
                    window.currentViseme = null;
                    return;
                }
                const elapsed = Date.now() - startTime;
                if (elapsed >= duration) {
                    window.currentLipValue = 0;
                    window.currentViseme = null;
                    return;
                }
                const progress = elapsed / duration;
                const openAmount = 0.35 + 0.45 * Math.abs(Math.sin(progress * Math.PI * 6)) * (0.5 + Math.random() * 0.5);
                window.currentLipValue = Math.max(0, Math.min(1, openAmount));

                // 文本驱动 viseme：根据当前播放位置从时间线取对应汉字的韵母口型
                if (timeline.length > 0) {
                    const idx = Math.min(Math.floor(elapsed / charDuration), timeline.length - 1);
                    if (idx !== charIndex) charIndex = idx;
                    window.currentViseme = timeline[charIndex].viseme;
                } else {
                    window.currentViseme = null;
                }

                requestAnimationFrame(tick);
            };
            tick();
        }

        // 在 play() 前连接 AudioContext，避免播放中途 reroute 导致前几个字被切断
        _connectAudio(audioElement) {
            if (!audioElement || audioElement._lipSyncConnected) return;
            try {
                const AudioContextClass = window.AudioContext || window.webkitAudioContext;
                if (!AudioContextClass) return;
                const ctx = this.getContext();
                const source = ctx.createMediaElementSource(audioElement);
                const analyser = ctx.createAnalyser();
                analyser.fftSize = 256;
                source.connect(analyser);
                analyser.connect(ctx.destination);
                audioElement._analyser = analyser;
                audioElement._lipSyncConnected = true;
            } catch (e) {
                console.warn('AudioContext 连接失败（播放时降级）:', e);
            }
        }

        // 音频驱动口型：分析实时音量和频谱，写入 window.currentLipValue（开口度）
        // 和 window.currentViseme（嘴型分类），由渲染循环统一驱动 VRM BlendShape。
        // 频域分析基于中文元音共振峰特征：低频(后元音o/u)→ou, 中频(开元音a)→aa, 高频(前元音i/ü)→ee
        // 若播放开始后短时间内检测不到任何音频能量，自动降级到基于时长的 _simulateLip(text)。
        _syncLipWithAudio(audioElement, fallbackText) {
            window.currentLipValue = 0;
            window.currentViseme = null;
            this._lipDriver = 'audio';

            if (!audioElement) {
                this._simulateLip(fallbackText || '');
                return;
            }

            const analyser = audioElement._analyser;
            if (!analyser) {
                this._simulateLip(fallbackText || '');
                return;
            }

            const dataArray = new Uint8Array(analyser.frequencyBinCount);
            let smoothed = 0;
            let bandLow = 0, bandMid = 0, bandHigh = 0;
            const SMOOTHING = 0.6;

            const OBSERVE_MS = 420;
            const startTime = performance.now();
            let energyAccum = 0;
            let sampled = false;

            const tick = () => {
                if (this._lipDriver !== 'audio') return;
                if (audioElement.paused || audioElement.ended) {
                    window.currentLipValue = 0;
                    window.currentViseme = null;
                    return;
                }

                analyser.getByteFrequencyData(dataArray);
                const len = dataArray.length;

                // ---- 总能量（用于开口度，不变） ----
                let sum = 0;
                const maxBin = Math.min(len, 40);
                for (let i = 0; i < len; i++) sum += dataArray[i];
                const avg = sum / len / 255;

                // ---- 频段能量分析（新增：用于 viseme 分类） ----
                // 采样率 48kHz / fftSize 256 = 187.5Hz/bin
                //   低频 bin 1-5  (187~937Hz)  → 后元音 o/u → ou/oh
                //   中频 bin 5-12 (937~2250Hz) → 开元音 a   → aa
                //   高频 bin 12-40 (2250~7500Hz) → 前元音 i/ü → ee/ih
                let lowSum = 0, midSum = 0, highSum = 0;
                for (let i = 1; i < maxBin; i++) {
                    const v = dataArray[i];
                    if (i < 5) lowSum += v;
                    else if (i < 12) midSum += v;
                    else highSum += v;
                }
                const lowCount = 4, midCount = 7, highCount = maxBin - 12;
                const lowAvg = lowSum / lowCount;
                const midAvg = midSum / midCount;
                const highAvg = highCount > 0 ? highSum / highCount : 0;

                bandLow = bandLow * SMOOTHING + lowAvg * (1 - SMOOTHING);
                bandMid = bandMid * SMOOTHING + midAvg * (1 - SMOOTHING);
                bandHigh = bandHigh * SMOOTHING + highAvg * (1 - SMOOTHING);

                // 哑音检测（不变）
                if (!sampled) {
                    energyAccum += avg;
                    if (performance.now() - startTime > OBSERVE_MS) {
                        sampled = true;
                        if (energyAccum < 0.02) {
                            console.warn('未检测到有效音频波形，降级到时间驱动口型');
                            this._lipDriver = 'time';
                            window.currentLipValue = 0;
                            window.currentViseme = null;
                            this._simulateLip(fallbackText || '');
                            return;
                        }
                    }
                }

                // ---- Viseme 分类（新增） ----
                const totalBandEnergy = bandLow + bandMid + bandHigh;
                if (totalBandEnergy > 15) {
                    const avgBand = totalBandEnergy / 3;
                    let maxBand = bandLow;
                    let viseme = 'ou';
                    if (bandMid > maxBand) { maxBand = bandMid; viseme = 'aa'; }
                    if (bandHigh > maxBand) { maxBand = bandHigh; viseme = 'ee'; }
                    // 置信度：主导频段显著高于平均才切换，否则用 aa（默认元音）
                    const confidence = maxBand / Math.max(1, avgBand);
                    window.currentViseme = confidence > 1.25 ? viseme : 'aa';
                } else if (totalBandEnergy > 8) {
                    // 微能量时默认开元音，避免与时间模式间闪烁
                    window.currentViseme = 'aa';
                }
                // totalBandEnergy <= 8: 保持上次分类，由开口度控制闭口

                // 开口度映射（不变）
                const raw = Math.pow(Math.min(1, avg * 2.4), 0.7);
                smoothed = smoothed * SMOOTHING + raw * (1 - SMOOTHING);
                window.currentLipValue = Math.max(0, Math.min(1, smoothed));

                requestAnimationFrame(tick);
            };
            tick();
        }
    }

    const audioManager = new AudioManager();

    class SegmentPlayer {
        constructor() {
            this.segments = [];
            this.segBase64 = [];
            this.segTexts = [];
            this.playing = false;
            this.currentIndex = 0;
            this.total = 0;
            this.currentAudio = null;
            this.playedAny = false;
            this.fallbackUrl = null;
            this.fallbackBase64 = null;
            this._fallbackPlayed = false;
            this._fallbackText = '';
            this._receivedSegmentEvent = false;
            this._segmentStreamDone = false;
            this._currentSegmentIndex = null;
            this._pausedSegmentIndex = null;
            this._pausedSegmentTime = 0;
            this._doneFallbackText = '';
            this._speechFallbackPlayed = false;
            this._generation = 0;
            this._playAttemptToken = 0;
            this._watchdog = null;
            this._stallCheck = null;
        }
        reset(total) {
            this._generation++;
            this._playAttemptToken++;
            this.segments = [];
            this.segBase64 = [];
            this.segTexts = [];
            this.playing = false;
            this.currentIndex = 0;
            this.total = total;
            this.playedAny = false;
            this.fallbackUrl = null;
            this.fallbackBase64 = null;
            this._fallbackPlayed = false;
            this._fallbackText = '';
            this._receivedSegmentEvent = false;
            this._segmentStreamDone = false;
            this._currentSegmentIndex = null;
            this._pausedSegmentIndex = null;
            this._pausedSegmentTime = 0;
            this._doneFallbackText = '';
            this._speechFallbackPlayed = false;
            this._clearTimers();
            if (this.currentAudio) {
                this.currentAudio.pause();
                this.currentAudio.onended = null;
                this.currentAudio.onerror = null;
                this.currentAudio = null;
            }
            window.vrmAudioElement = null;
            window._audioFirstPlayTime = 0;
        }
        stop() {
            this._generation++;
            this._playAttemptToken++;
            this.playing = false;
            this.total = 0;
            this.segments = [];
            this.segBase64 = [];
            this.segTexts = [];
            this.playedAny = false;
            this.fallbackUrl = null;
            this.fallbackBase64 = null;
            this._fallbackPlayed = false;
            this._fallbackText = '';
            this._receivedSegmentEvent = false;
            this._segmentStreamDone = false;
            this._currentSegmentIndex = null;
            this._pausedSegmentIndex = null;
            this._pausedSegmentTime = 0;
            this._doneFallbackText = '';
            this._speechFallbackPlayed = false;
            this._clearTimers();
            if (this.currentAudio) {
                this.currentAudio.pause();
                this.currentAudio.onended = null;
                this.currentAudio.onerror = null;
                this.currentAudio = null;
            }
            window.vrmAudioElement = null;
            window._audioFirstPlayTime = 0;
        }
        _clearTimers() {
            if (this._watchdog) { clearTimeout(this._watchdog); this._watchdog = null; }
            if (this._stallCheck) { clearInterval(this._stallCheck); this._stallCheck = null; }
        }
        _startStallCheck() {
            if (this._stallCheck) return;
            let lastTime = -1;
            let stallCount = 0;
            this._stallCheck = setInterval(() => {
                const a = this.currentAudio;
                if (!a || !this.playing || a.ended) { this._stopStallCheck(); return; }
                const ct = a.currentTime || 0;
                if (ct > 0.1) {
                    if (ct === lastTime) {
                        stallCount++;
                        if (stallCount >= 10) {
                            console.warn(`[Seg] stall: audio stuck at ${ct.toFixed(1)}s for ${stallCount * 0.5}s, advancing`);
                            this._stopStallCheck();
                            a.dispatchEvent(new Event('ended'));
                        }
                    } else {
                        stallCount = 0;
                        lastTime = ct;
                    }
                }
            }, 500);
        }
        _stopStallCheck() {
            if (this._stallCheck) { clearInterval(this._stallCheck); this._stallCheck = null; }
        }
        add(index, url, b64, text) {
            if (this._segmentStreamDone) {
                console.warn(`[Seg] ignoring late segment idx=${index} after done`);
                return;
            }
            this._receivedSegmentEvent = true;
            if (this._fallbackPlayed) return;
            this.segments[index] = url || null;
            this.segBase64[index] = b64 || null;
            this.segTexts[index] = text || '';
            // 累积所有段文本, 供最后兜底播放 done 音频时驱动唇形
            this._fallbackText = (this._fallbackText || '') + (text || '');
            if (this.total === 0) {
                this.total = index + 1;
            } else if (index + 1 > this.total) {
                this.total = index + 1;
            }
            console.log(`[Seg] add: idx=${index} total=${this.total} playing=${this.playing} curIdx=${this.currentIndex} gen=${this._generation} textLen=${(text||'').length}`);
            this._playNext();
        }
        finalizeSegmentStream(doneText = '') {
            if (!this._receivedSegmentEvent || this._segmentStreamDone) return;
            this._segmentStreamDone = true;
            this._doneFallbackText = doneText || '';
            // The backend sends one audio_segment for every TTS slot, including failures.
            // Treat any missing slot at done as a failed slot so the queue cannot wait forever.
            for (let i = 0; i < this.total; i++) {
                if (this.segments[i] === undefined && this.segBase64[i] === undefined) {
                    this.segments[i] = null;
                    this.segBase64[i] = null;
                }
            }
        }
        pauseForMute() {
            this._playAttemptToken++;
            this.playing = false;
            this._clearTimers();
            const audio = this.currentAudio;
            const currentIndex = this._currentSegmentIndex;
            this._pausedSegmentIndex = currentIndex;
            this._pausedSegmentTime = audio ? Math.max(0, Number(audio.currentTime) || 0) : 0;
            this.currentAudio = null;
            this._currentSegmentIndex = null;
            if (currentIndex !== null && currentIndex !== undefined) {
                this.currentIndex = Math.min(this.currentIndex, currentIndex);
            }
            if (audio) {
                try { audio.pause(); } catch (e) {}
            }
            window.vrmAudioElement = null;
            window._audioFirstPlayTime = 0;
        }
        _maybeSpeakExhaustedFallback() {
            if (!this._segmentStreamDone
                || this._speechFallbackPlayed
                || this.playing
                || this.playedAny
                || this.currentIndex < this.total
                || !state.speechEnabled
                || !this._doneFallbackText) return;
            this._speechFallbackPlayed = true;
            console.warn(`[Seg] no playable segment after done, using Web Speech fallback (len=${this._doneFallbackText.length})`);
            speak(this._doneFallbackText);
        }
        _playNext() {
            // 关键修复: 静音时不去开播(只由 add() 缓冲段), 由 resumeSpeech 在重新开启后续播.
            // 若在此直接播放, "静音→立刻开启" 窗口内的段会立刻出声, 违背静音意图.
            if (!state.speechEnabled) return;
            if (this.playing) return;
            const gen = this._generation;
            while (this.currentIndex < this.total) {
                if (gen !== this._generation) return;
                const url = this.segments[this.currentIndex];
                const b64 = this.segBase64[this.currentIndex];
                const segText = this.segTexts[this.currentIndex] || '';
                if (url === undefined && b64 === undefined) {
                    console.log(`[Seg] _playNext: waiting for idx=${this.currentIndex} (undefined)`);
                    break;
                }
                this.currentIndex++;
                const audioSrc = b64 ? `data:audio/mp3;base64,${b64}` : normalizeBackendAudioUrl(url);
                if (!audioSrc) {
                    this._currentSegmentIndex = null;
                    continue;
                }
                this.playing = true;
                const segIdx = this.currentIndex - 1;
                this._currentSegmentIndex = segIdx;
                const attemptToken = ++this._playAttemptToken;
                console.log(`[Seg] _playNext: playing idx=${segIdx}/${this.total} textLen=${segText.length}`);
                let audio;
                try {
                    audio = createPlaybackAudio(audioSrc);
                    const resumeAt = this._pausedSegmentIndex === segIdx ? this._pausedSegmentTime : 0;
                    if (this._pausedSegmentIndex === segIdx) {
                        this._pausedSegmentIndex = null;
                        this._pausedSegmentTime = 0;
                    }
                    if (resumeAt > 0) {
                        let applied = false;
                        const applyResumePosition = () => {
                            if (applied || !audio.readyState) return;
                            try {
                                audio.currentTime = resumeAt;
                                applied = true;
                            } catch (e) {}
                        };
                        audio.addEventListener('loadedmetadata', applyResumePosition);
                        applyResumePosition();
                    }
                    this.currentAudio = audio;
                    window.vrmAudioElement = audio;
                } catch (e) {
                    console.warn('Segment Audio() constructor failed:', e);
                    this.playing = false;
                    this._currentSegmentIndex = null;
                    continue;
                }
                // 先连接 AudioContext (供 analyser 读取波形驱动嘴型)
                try { audioManager._connectAudio(audio); } catch (e) { console.warn('[Seg] _connectAudio failed:', e); }
                let attemptActive = true;
                const isCurrentAttempt = () => attemptActive
                    && gen === this._generation
                    && this.currentAudio === audio
                    && this._playAttemptToken === attemptToken;
                const advance = () => {
                    const isCurrent = isCurrentAttempt();
                    attemptActive = false;
                    if (!isCurrent) {
                        try { audio.pause(); } catch (e) {}
                        return;
                    }
                    this._clearTimers();
                    this._playAttemptToken++;
                    try { audio.pause(); } catch (e) {}
                    audioManager.active = false;
                    window.currentLipValue = 0;
                    window.currentViseme = null;
                    this.currentAudio = null;
                    this._currentSegmentIndex = null;
                    this.playing = false;
                    console.log(`[Seg] advance: finished idx=${segIdx} next=${this.currentIndex}/${this.total}`);
                    this._playNext();
                };
                let advanced = false;
                const safeAdvance = () => {
                    if (advanced) return;
                    advanced = true;
                    advance();
                };
                audio.addEventListener('ended', safeAdvance);
                audio.addEventListener('error', (e) => {
                    console.warn(`[Seg] audio error idx=${segIdx}:`, e);
                    if (gen !== this._generation) return;
                    if (this.currentAudio !== audio) return;
                    safeAdvance();
                });
                this._clearTimers();
                this._watchdog = setTimeout(() => {
                    if (gen === this._generation && this.currentAudio === audio && !audio.ended) {
                        console.warn(`[Seg] watchdog: idx=${segIdx} forced advance after 30s`);
                        safeAdvance();
                    }
                }, 30000);
                const startPlayback = () => {
                    if (!isCurrentAttempt() || !state.speechEnabled) {
                        try { audio.pause(); } catch (e) {}
                        return;
                    }
                    console.log(`[Seg] calling play() idx=${segIdx}, audioSrc type=${audioSrc.startsWith('data:') ? 'base64' : 'url'}, readyState=${audio.readyState}, networkState=${audio.networkState}`);
                    const playStarted = Date.now();
                    audio.play().then(() => {
                        console.log(`[Seg] play() resolved idx=${segIdx} in ${Date.now()-playStarted}ms`);
                        if (!isCurrentAttempt()) { try { audio.pause(); } catch(e) {} return; }
                        this.playedAny = true;
                        audioManager.active = true;
                        // 关键修复: 记录首次开播时刻, 给 setActionTimeline 当时间轴锚点
                        if (!window._audioFirstPlayTime) {
                            window._audioFirstPlayTime = performance.now();
                            console.log(`[Seg] first play timestamp recorded: ${window._audioFirstPlayTime.toFixed(0)}`);
                        }
                        animateSpeaking();
                        // 关键修复: 把本段文本作为 fallback 传给 _syncLipWithAudio,
                        // 防止 analyser 失败时退到空文本导致唇形卡死/提前结束
                        audioManager._syncLipWithAudio(audio, segText);
                        this._startStallCheck();
                        // 防 Chrome 静默批准 muted 音频：1s 后若 currentTime 仍 < 0.05，视为假播放
                        setTimeout(() => {
                            if (isCurrentAttempt() && !audio.ended && audio.currentTime < 0.05) {
                                console.warn(`[Seg] fake-play detected idx=${segIdx} (currentTime=${audio.currentTime.toFixed(2)} after 1s), forcing advance`);
                                this.playedAny = false;
                                safeAdvance();
                            }
                        }, 1000);
                    }).catch((err) => {
                        if (!isCurrentAttempt()) { try { audio.pause(); } catch(e) {} return; }
                        console.warn(`[Seg] audio play rejected idx=${segIdx}:`, err);
                        safeAdvance();
                    });
                    // 关键修复: 缩短 play() 超时从 5s 到 2s, 减少静默等待时间
                    setTimeout(() => {
                        if (isCurrentAttempt() && !this.playedAny) {
                            console.warn(`[Seg] play() timeout idx=${segIdx} (${Date.now()-playStarted}ms, ctx=${audioManager.getContext().state}), forcing advance`);
                            safeAdvance();
                        }
                    }, 2000);
                };
                // 关键修复: 先等 AudioContext resume 完成再 play(), 否则首段 play() 被 suspended 状态拒绝
                try {
                    const ctx = audioManager.getContext();
                    if (ctx.state === 'suspended') {
                        console.log(`[Seg] AudioContext suspended, awaiting resume before play idx=${segIdx}`);
                        ctx.resume().then(() => {
                            if (isCurrentAttempt()) startPlayback();
                            else { try { audio.pause(); } catch (e) {} }
                        }).catch(e => {
                            console.warn(`[Seg] AudioContext resume failed, trying play anyway:`, e);
                            if (isCurrentAttempt()) startPlayback();
                        });
                    } else {
                        startPlayback();
                    }
                } catch(e) {
                    console.warn(`[Seg] AudioContext error, trying play anyway:`, e);
                    startPlayback();
                }
                return;
            }
            if (!this._receivedSegmentEvent && !this.playedAny && !this._fallbackPlayed && (this.fallbackUrl || this.fallbackBase64) && state.speechEnabled) {
                this._fallbackPlayed = true;
                console.log(`[Seg] fallback: playing done audio for segments ${this.currentIndex}/${this.total}`);
                const onFallbackResult = (ok) => {
                    if (ok) {
                        this.playedAny = true;
                        return;
                    }
                    if (!this._speechFallbackPlayed && this._fallbackText && state.speechEnabled) {
                        this._speechFallbackPlayed = true;
                        speak(this._fallbackText);
                    }
                };
                if (this.fallbackBase64) {
                    audioManager.playAudio(`data:audio/mp3;base64,${this.fallbackBase64}`, this._fallbackText || '', onFallbackResult);
                } else if (this.fallbackUrl) {
                    audioManager.playAudio(this.fallbackUrl, this._fallbackText || '', onFallbackResult);
                }
            } else if (!this._fallbackPlayed && this.currentIndex >= this.total && !this.playedAny) {
                console.warn(`[Seg] CHAIN-BREAK: currentIndex=${this.currentIndex} total=${this.total} playedAny=${this.playedAny} fallbackPlayed=${this._fallbackPlayed} fallbackUrl=${!!this.fallbackUrl} gen=${this._generation}`);
            }
            this._maybeSpeakExhaustedFallback();
        }
    }

    const segmentPlayer = new SegmentPlayer();

    function speak(text) {
        audioManager._speak(text);
    }

    function playAudioReply(audioUrl, fallbackText) {
        if (!state.speechEnabled) return;
        if (audioUrl) {
            audioManager.playAudio(audioUrl, fallbackText);
        }
    }

    // 关键修复: 重新开启播报时, 优先续播静音期间被缓冲的音频段,
    // 否则重放 done 时被保留的整条回复音频 (data.audioUrl/base64).
    function resumeSpeech() {
        if (!state.speechEnabled) return;
        const heldForReplay = state.mutedResume;
        state.mutedResume = null;
        if (typeof segmentPlayer === 'undefined') return;
        if (segmentPlayer.playing) return;
        if (segmentPlayer._receivedSegmentEvent) {
            console.log(`[Resume] resume segment stream without held done fallback idx=${segmentPlayer.currentIndex}/${segmentPlayer.total}`);
            segmentPlayer._playNext();
            if (!segmentPlayer.playing
                && !segmentPlayer.playedAny
                && !segmentPlayer._speechFallbackPlayed
                && segmentPlayer.currentIndex >= segmentPlayer.total
                && heldForReplay?.text) {
                segmentPlayer._speechFallbackPlayed = true;
                console.log('[Resume] segment stream produced no playable audio, using Web Speech text fallback');
                speak(heldForReplay.text);
            }
            return;
        }
        // 1) 有缓冲未播段 → 交给 _playNext 续播; 段全部无效时其内部会落到 fallback 分支自动兜底
        if (segmentPlayer.currentIndex < segmentPlayer.total) {
            console.log(`[Resume] resume buffered segments idx=${segmentPlayer.currentIndex}/${segmentPlayer.total}`);
            segmentPlayer._playNext();
            return;
        }
        // 2) 无段可播(如 done 只带整体音频) → 重放静音期间保留的回复音频
        const held = heldForReplay;
        if (held && (held.url || held.base64)) {
            state.mutedResume = null;
            console.log(`[Resume] replay held reply audio (hasUrl=${!!held.url} hasB64=${!!held.base64}, textLen=${(held.text || '').length})`);
            const onHeldReplayResult = (ok) => {
                if (!ok && held.text && state.speechEnabled) speak(held.text);
            };
            if (held.base64) {
                audioManager.playAudio(`data:audio/mp3;base64,${held.base64}`, held.text, onHeldReplayResult);
            } else if (held.url) {
                audioManager.playAudio(held.url, held.text, onHeldReplayResult);
            }
        } else if (held && held.text) {
            // 3) 该回复没有任何可播音频 → Web Speech 兜底朗读(仅当本次回复确实被静音保留过)
            console.log('[Resume] no held audio, Web Speech fallback for muted reply');
            speak(held.text);
        }
    }

    function animateSpeaking() {
        updateAvatarMood("speaking");
        const avatar = document.querySelector(".avatar");
        if (!avatar) return;
        avatar.classList.add("speaking");

        // 关键修复: 监听 audio 元素事件驱动停止, 不再用 setInterval 8 次硬清
        const microActions = ['nod', 'tilt', 'gesture'];
        let speakCount = 0;
        const speakInterval = setInterval(() => {
            if (!avatar.classList.contains("speaking")) {
                clearInterval(speakInterval);
                return;
            }
            const randomAction = microActions[Math.floor(Math.random() * microActions.length)];
            if (window.playAction) {
                window.playAction(randomAction, 0);
            }
            speakCount++;
            // 关键修复: 上限提到 30 次 (15s), 适配长 TTS
            if (speakCount > 30) {
                clearInterval(speakInterval);
            }
        }, 1000 + Math.random() * 800);

        // 关键修复: 同步一个 idle-check, 任何音频 onended 会移除 speaking class
        // 这里只保底 (3 分钟强制清), 实际由 audio onended 触发
        setTimeout(() => {
            if (avatar.classList.contains("speaking")) {
                clearInterval(speakInterval);
                avatar.classList.remove("speaking");
            }
        }, 180000);
    }

    // 网站 Logo（莲花）SVG，作为 AI 助手消息头像
    const ASSISTANT_LOGO_SVG = `<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><g fill="none" stroke-linecap="round" stroke-linejoin="round"><path d="M16 5 Q18 12 16 19 Q14 12 16 5Z" fill="#B8923A" stroke="#8A6A20" stroke-width="0.6"/><path d="M8 9 Q12 13 14 19 Q10 15 8 9Z" fill="#D4A66A" stroke="#8A6A20" stroke-width="0.5"/><path d="M24 9 Q20 13 18 19 Q22 15 24 9Z" fill="#D4A66A" stroke="#8A6A20" stroke-width="0.5"/><path d="M4 13 Q9 13 12 18 Q8 16 4 13Z" fill="#E8C896" stroke="#8A6A20" stroke-width="0.4"/><path d="M28 13 Q23 13 20 18 Q24 16 28 13Z" fill="#E8C896" stroke="#8A6A20" stroke-width="0.4"/><circle cx="16" cy="14" r="2.2" fill="#B8923A" stroke="#8A6A20" stroke-width="0.5"/><path d="M16 19 Q15 25 16 29" stroke="#5A7E68" stroke-width="0.8" fill="none"/><path d="M10 27 Q13 25 16 27 Q19 25 22 27" stroke="#5A7E68" stroke-width="0.6" fill="none"/></g></svg>`;

    function addMessage(role, text, meta = []) {
        const log = els.chatLog;
        if (!log) return;
        // 关键修复: 取消上一个挂起的自动播放定时器, 防止连续提问互相打断
        if (state.pendingAudioTimer) {
            clearTimeout(state.pendingAudioTimer);
            state.pendingAudioTimer = null;
        }
        const msg = document.createElement("div");
        msg.className = `message ${role}`;
        msg.innerHTML = `
            <div class="message-avatar">${role === "user" ? "我" : ASSISTANT_LOGO_SVG}</div>
            <div class="message-content">
                <p></p>
            </div>
        `;
        if (msg.lastElementChild?.lastElementChild) {
            msg.lastElementChild.lastElementChild.textContent = text;
        }
        log.appendChild(msg);
        log.scrollTop = log.scrollHeight;
        if (role === "assistant") {
            const spots = _extractSpotNames(text);
            if (spots.length > 0) {
                state.lastMentionedSpots = spots;
            }
            if (state.speechEnabled && !state.streamMode) {
                animateSpeaking();
                // 关键修复: 0 延迟 + 可取消, 防止连续提问时 500ms 延迟导致旧定时器把新音频打断
                state.pendingAudioTimer = setTimeout(() => {
                    state.pendingAudioTimer = null;
                    // 关键修复: 再次检查 speechEnabled (用户在 0ms 内可能已关闭)
                    if (!state.speechEnabled) return;
                    playAudioReply(state.lastAudioUrl, text);
                }, 0);
            }
        }
    }

    // 关键修复: 每个流式消息绑定自己的请求序号(mySeq), 不再被其它请求(被取消的旧流/
    // 新流)用同名 id "streaming-msg" 误命中, 导致新回答文本写不进去 / 旧文本被覆盖.
    function currentStreamingEl(mySeq) {
        const el = document.getElementById("streaming-msg");
        if (el && el.dataset && el.dataset.seq === String(mySeq)) return el;
        return null;
    }

    function getOrCreateStreamingMessage(mySeq) {
        const log = els.chatLog;
        let existing = document.getElementById("streaming-msg");
        if (existing) {
            if (mySeq !== undefined) existing.dataset.seq = String(mySeq);
            return existing;
        }
        const msg = document.createElement("div");
        msg.className = "message assistant streaming";
        msg.id = "streaming-msg";
        if (mySeq !== undefined) msg.dataset.seq = String(mySeq);
        msg.innerHTML = `<div class="message-avatar">${ASSISTANT_LOGO_SVG}</div><div class="message-content"><p class="stream-text"></p><span class="stream-cursor">▌</span></div>`;
        log.appendChild(msg);
        log.scrollTop = log.scrollHeight;
        return msg;
    }

    function updateStreamingText(text, mySeq) {
        const msg = currentStreamingEl(mySeq);
        if (!msg) return;
        const p = msg.querySelector(".stream-text");
        if (p) p.textContent = text;
        if (els.chatLog) els.chatLog.scrollTop = els.chatLog.scrollHeight;
    }

    // Text completion is independent from the later TTS/audio metadata completion.
    // Keep the streaming node alive so the subsequent done event can still finalize
    // the message and apply emotion/audio metadata without leaving the cursor visible.
    function markStreamingTextComplete(completeText, mySeq) {
        const msg = currentStreamingEl(mySeq);
        if (!msg) return;
        const p = msg.querySelector(".stream-text");
        const finalText = completeText || p?.textContent || "";
        if (p && finalText) p.textContent = finalText;
        const cursor = msg.querySelector(".stream-cursor");
        if (cursor) cursor.remove();
    }

    function finalizeStreamingMessage(completeText, emotion, mySeq) {
        const msg = currentStreamingEl(mySeq);
        if (msg) {
            msg.id = "";
            msg.classList.remove("streaming");
            const p = msg.querySelector(".stream-text");
            if (p) p.textContent = completeText;
            const cursor = msg.querySelector(".stream-cursor");
            if (cursor) cursor.remove();
        }
        if (completeText) {
            const spots = _extractSpotNames(completeText);
            if (spots.length > 0) {
                state.lastMentionedSpots = spots;
            }
        }
        if (emotion) setEmotion(emotion);
    }

    async function sendMessageStream(text) {

        const message = (text || els.chatInput?.value || "").trim();
        if (!message) return;
        if (els.chatInput) els.chatInput.value = "";
        if (state.activeController) {
            state.activeController.abort();
        }
        // 关键修复: 不再删除上一轮仍在流式输出的回答, 而是把它定稿为普通消息保留在对话里,
        // 这样用户连发新问题时, 前面的回答不会消失.
        const oldStreaming = document.getElementById("streaming-msg");
        if (oldStreaming) {
            const oldSeq = oldStreaming.dataset && oldStreaming.dataset.seq;
            const partialText = oldStreaming.querySelector(".stream-text")?.textContent || "";
            finalizeStreamingMessage(partialText, null, oldSeq ? Number(oldSeq) : state.requestSeq);
        }
        state.requestSeq++;
        const mySeq = state.requestSeq;
        const controller = new AbortController();
        let streamTimeout;
        state.activeController = controller;
        // 关键修复: sendBtn 防抖, 用 try/finally 兜底, 防止异常时按钮卡死
        els.sendBtn.disabled = true;
        // 关键修复: 取消挂起的自动播放, 防止旧定时器干扰
        if (state.pendingAudioTimer) {
            clearTimeout(state.pendingAudioTimer);
            state.pendingAudioTimer = null;
        }
        // 关键修复: 清掉 state.lastAudioUrl 残留
        state.lastAudioUrl = null;
        state.lastAudioBase64 = null;
        // 关键修复: 新请求清掉静音期间保留的旧回复音频, 避免下次开启播报时重放过期内容
        state.mutedResume = null;
        audioManager.stop();
        segmentPlayer.reset(0);
        console.log(`[Stream] NEW REQUEST: msg="${message.substring(0,30)}..." speechEnabled=${state.speechEnabled} gen=${segmentPlayer._generation}`);
        try {
            const ctx = audioManager.getContext();
            if (ctx.state === 'suspended') await ctx.resume();
        } catch (e) {}

        addMessage("user", message);
        hideFeedbackBar();
        resetFeedback();

        // Auto-trigger navigation for location queries
        if (isNavQuery(message)) {
            handleNavQuery(message);
        }

        const { emotion, emotionPayload } = detectEmotionFromMessage(message);
        setEmotion(emotionPayload);

        if (window.playAction) {
            if (message.includes("点头") || message.includes("是的") || message.includes("好的")) window.playAction("nod", 1);
            else if (message.includes("挥手") || message.includes("再见") || message.includes("拜拜")) window.playAction("wave", 1);
            else if (message.includes("摇头") || message.includes("不对") || message.includes("不是")) window.playAction("shake", 1);
            else if (message.includes("谢谢") || message.includes("感谢")) window.playAction("bow", 1);
        }

        getOrCreateStreamingMessage(mySeq);

        try {
            const requestStart = Date.now();
            const response = await fetch(apiUrl("/api/v1/chat/text-stream"), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    message,
                    sessionId: state.sessionId,
                    userId: state.userId,
                    modelId: selectedModelId(),
                    interest: state.interest,
                    gps: state.gpsCoords,
                    emotion,
                }),
                signal: controller.signal,
            });

            // 关键修复: 非 2xx 立即降级, 不让前端 hang 120s
            if (!response.ok) {
                console.warn(`[Stream] HTTP ${response.status} ${response.statusText}`);
                addMessage("assistant", "音频服务暂时不可用，请稍后重试。");
                setEmotion(emotionConfig.neutral);
                if (els.avatarStatus) els.avatarStatus.textContent = "在线讲解中";
                return;
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = "";
            let receivedDone = false;
            let pendingEvent = null;
            // 45 秒静默超时：给上游短暂波动留出余量，同时避免页面长期无响应。
            const STREAM_TIMEOUT_MS = 45000;
            streamTimeout = setTimeout(() => {
                if (!receivedDone) {
                    console.warn(`[Stream] ${STREAM_TIMEOUT_MS/1000}s 超时, 主动 abort`);
                    controller.abort();
                }
            }, STREAM_TIMEOUT_MS);

            function tryFinalizeStreaming(text, mySeq) {
                const msg = currentStreamingEl(mySeq);
                if (!msg) return;
                const p = msg.querySelector(".stream-text");
                const partialText = text || p?.textContent || "";
                if (partialText) {
                    state.lastReplyText = partialText;
                    finalizeStreamingMessage(partialText, null, mySeq);
                } else {
                    msg.remove();
                }
            }

            while (true) {
                const { done, value } = await reader.read();
                if (done) {
                    if (!receivedDone) {
                        tryFinalizeStreaming(undefined, mySeq);
                    }
                    break;
                }
                if (mySeq !== state.requestSeq) { controller.abort(); return; }
                buffer += decoder.decode(value, { stream: true });
                // 关键修复: 按 \r\n 切行 (SSE 规范) 同时兼容 \n (老后端 fallback)
                const lines = buffer.split(/\r\n|\n/);
                buffer = lines.pop() || "";

                // 关键修复: 重置超时, 仅在收到数据时续期
                clearTimeout(streamTimeout);
                streamTimeout = setTimeout(() => {
                    if (!receivedDone) {
                        console.warn(`[Stream] ${STREAM_TIMEOUT_MS/1000}s 静默超时, 主动 abort`);
                        controller.abort();
                    }
                }, STREAM_TIMEOUT_MS);

                // 关键修复: 上一 chunk 的 event: 行缺少 data: → 在下一 chunk 补 event: 头
                if (pendingEvent !== null) {
                    for (let j = 0; j < lines.length; j++) {
                        if (!lines[j]) continue;
                        if (lines[j].startsWith("data: ")) {
                            console.log(`[SSE-RAW] injecting event: ${pendingEvent} before data line (chunk boundary repair)`);
                            lines.splice(j, 0, "event: " + pendingEvent);
                            pendingEvent = null;
                        }
                        break;
                    }
                    if (pendingEvent !== null) {
                        console.warn(`[SSE-RAW] pending event ${pendingEvent} still unresolved, data line not yet arrived`);
                    }
                }

                for (let i = 0; i < lines.length; i++) {
                    if (mySeq !== state.requestSeq) { controller.abort(); return; }
                    const line = lines[i];
                    if (!line) continue;  // skip empty lines
                    if (!line.startsWith("event: ")) { continue; }
                    const eventType = line.slice(7).trim();
                    const nextLine = lines[i + 1];
                    i++;
                    if (!nextLine || !nextLine.startsWith("data: ")) {
                        console.warn(`[SSE-RAW] event ${eventType} data deferred to next chunk`);
                        pendingEvent = eventType;
                        continue;
                    }
                    try {
                        const data = JSON.parse(nextLine.slice(6));
                        if (mySeq !== state.requestSeq) { controller.abort(); return; }
                        if (eventType === "status") {
                            if (data.phase === "searching" && els.avatarStatus) els.avatarStatus.textContent = "查询知识库中...";
                            else if (data.phase === "generating" && els.avatarStatus) els.avatarStatus.textContent = "大模型生成中...";
                            const statusText = data.phase === "searching" ? "正在查询知识库..." : data.phase === "generating" ? "大模型生成中..." : "";
                            if (statusText) updateStreamingText(statusText, mySeq);
                            continue;
                        }
                        // 关键修复: 错误事件通知用户
                        if (eventType === "error") {
                            console.warn(`[Stream] server error event:`, data);
                            if (data.message && !receivedDone) {
                                addMessage("assistant", data.message);
                            }
                            continue;
                        }
                        if (eventType === "text") {
                            updateStreamingText(data.accumulated || data.text, mySeq);
                        } else if (eventType === "text_done") {
                            markStreamingTextComplete(data.completeReply || data.accumulated || data.text, mySeq);
                        } else if (eventType === "done") {
                            receivedDone = true;
                            state.lastConversationId = data.conversationId;
                            showFeedbackBar();
                            // 关键修复: 用 done 的值, 不要用 || 保留旧值 (会播错内容)
                            state.lastAudioUrl = data.audioUrl || null;
                            state.lastAudioBase64 = data.audioBase64 || null;
                            state.lastReplyText = data.completeReply;
                            finalizeStreamingMessage(data.completeReply, data.emotion, mySeq);
                            segmentPlayer.finalizeSegmentStream(data.completeReply || '');
                            if (data.audioUrl || data.audioBase64) {
                                segmentPlayer.fallbackUrl = data.audioUrl;
                                segmentPlayer.fallbackBase64 = data.audioBase64 || null;
                                if (state.speechEnabled) {
                                    // 只有完全没有分段流时, done 音频才可作为整段兜底播放
                                    if (!segmentPlayer._receivedSegmentEvent && !segmentPlayer.playing && !segmentPlayer.playedAny) {
                                        console.log(`[Done] playing fallback directly via audioManager`);
                                        segmentPlayer._fallbackPlayed = true;
                                        const onDoneFallbackResult = (ok) => {
                                            if (ok) {
                                                segmentPlayer.playedAny = true;
                                            } else if (!segmentPlayer._speechFallbackPlayed && data.completeReply && state.speechEnabled) {
                                                segmentPlayer._speechFallbackPlayed = true;
                                                speak(data.completeReply);
                                            }
                                        };
                                        if (data.audioBase64) {
                                            audioManager.playAudio(`data:audio/mp3;base64,${data.audioBase64}`, data.completeReply || '', onDoneFallbackResult);
                                        } else if (data.audioUrl) {
                                            audioManager.playAudio(data.audioUrl, data.completeReply || '', onDoneFallbackResult);
                                        }
                                    } else if (segmentPlayer._receivedSegmentEvent) {
                                        console.log(`[Done] segment playing, fallback queued for later`);
                                        segmentPlayer._playNext();
                                    }
                                } else {
                                    // 关键修复: 静音期间不标记已播, 保留 fallback 供 resumeSpeech 重放;
                                    // 并记下整条回复音频作为兜底, 避免 "静音→立刻开启" 后该回复彻底无声.
                                    state.mutedResume = { url: data.audioUrl, base64: data.audioBase64, text: data.completeReply || '' };
                                    console.log(`[Done] speech disabled, reply audio held for re-enable (hasUrl=${!!data.audioUrl} hasB64=${!!data.audioBase64})`);
                                }
                            }
                            if (segmentPlayer._receivedSegmentEvent && state.speechEnabled && !segmentPlayer.playing) {
                                segmentPlayer._playNext();
                            }
                            // 纯文本回复(无任何 TTS 音频)且静音中 → 也记入 mutedResume, 供重新开启时 Web Speech 兜底
                            if (!segmentPlayer.playedAny && !state.speechEnabled) {
                                state.mutedResume = { url: data.audioUrl || null, base64: data.audioBase64 || null, text: data.completeReply || '' };
                            }
                            // 终极兜底：所有 audio_segment 都没成功播、且没有正在播放的音频 → Web Speech
                            // 关键修复: 不再依赖 !data.audioUrl 判断, 改为检查 segmentPlayer 实际播放状态
                            if (!segmentPlayer.playedAny
                                && !segmentPlayer.playing
                                && !segmentPlayer._speechFallbackPlayed
                                && (!segmentPlayer._receivedSegmentEvent || segmentPlayer.currentIndex >= segmentPlayer.total)) {
                                const replyText = (data.completeReply || "").trim();
                                if (replyText && state.speechEnabled) {
                                    console.warn(`[Done] no audio played (playedAny=false, playing=false), falling back to Web Speech (len=${replyText.length})`);
                                    speak(replyText);
                                }
                            }

                            if (data.emotionState && window.updateEmotionState) {
                                window.updateEmotionState(data.emotionState, data.ttsTotalMs, data.expression);
                            }
                            if (data.actions && data.actions.length > 0 && window.setActionTimeline) {
                                const isNewFormat = typeof data.actions[0] === 'object' && data.actions[0].type;
                                if (isNewFormat) {
                                    window.setActionTimeline(data.actions);
                                } else if (window.playAction) {
                                    data.actions.forEach((actionName, index) => {
                                        setTimeout(() => window.playAction(actionName, index === 0 ? 2 : 1), index * 1500);
                                    });
                                }
                            } else if (window.triggerGesturesForReply) {
                                window.triggerGesturesForReply(data.completeReply);
                            }
                            if (data.expression && window.updateEmotionBlend) {
                                const exp = { ...data.expression };
                                exp._startTime = performance.now();
                                const baseDur = (exp.intensity || 0.6) >= 0.7 ? 2500 : (exp.intensity || 0.6) >= 0.4 ? 1800 : 1200;
                                const audioDur = (typeof data.ttsTotalMs === 'number' && data.ttsTotalMs > 0) ? data.ttsTotalMs : 0;
                                exp._duration = Math.max(baseDur, audioDur + ((exp.intensity || 0.6) >= 0.7 ? 1000 : 500));
                                if (!window.updateEmotionState || !data.emotionState) {
                                    window.updateEmotionBlend(exp);
                                }
                            }
                            renderResponseMeta(data);
                            if (els.avatarStatus) els.avatarStatus.textContent = "在线讲解中";
                        } else if (eventType === "audio_segment") {
                console.log(`[AudioSeg] RECEIVED event: idx=${data.index}/${data.total} speechEnabled=${state.speechEnabled} hasUrl=${!!data.audioUrl} hasB64=${!!data.audioBase64} receivedDone=${receivedDone} segTotal=${segmentPlayer.total} playing=${segmentPlayer.playing} playedAny=${segmentPlayer.playedAny}`);
                // 关键修复: 静音时也缓存音频段, 不丢弃. 重新开启播报后由 resumeSpeech 续播, 
                // 否则 "静音→立刻开启" 会因为早到的段被丢弃而永久无声音.
                if (typeof segmentPlayer !== 'undefined' && data.index !== undefined) {
                    if (segmentPlayer.total === 0 || data.total > segmentPlayer.total) {
                        segmentPlayer.total = data.total;
                    }
                    if (data.audioUrl) state.lastAudioUrl = data.audioUrl;
                    console.log(`[AudioSeg] buffer idx=${data.index}/${data.total} done=${receivedDone} played=${segmentPlayer.playedAny} hasAudio=${!!(data.audioUrl || data.audioBase64)} textLen=${(data.text||'').length} speechEnabled=${state.speechEnabled}`);
                    if (data.audioUrl || data.audioBase64) {
                        // 诊断: 检查首段 AudioContext 状态
                        if (data.index === 0) {
                            try {
                                const ctx = audioManager.getContext();
                                console.log(`[AudioSeg-DIAG] AudioContext state=${ctx.state} created_at=${audioManager.audioContext ? 'existing' : 'new'}`);
                            } catch(e) {
                                console.log(`[AudioSeg-DIAG] AudioContext error: ${e}`);
                            }
                        }
                    }
                    // Register null entries too: _playNext advances these slots without playback.
                    segmentPlayer.add(data.index, data.audioUrl, data.audioBase64, data.text || '');
                }
                        }
                    } catch (e) {
                        console.warn("SSE parse error:", e);
                    }
                }

                if (els.avatarStatus && els.avatarStatus.textContent === "查询知识库中...") {
                    els.avatarStatus.textContent = "在线讲解中";
                }
            }
        } catch (error) {
            clearTimeout(streamTimeout);
            if (error?.name === 'AbortError' || mySeq !== state.requestSeq) {
                tryFinalizeStreaming(undefined, mySeq);
                return;
            }
            const msg = currentStreamingEl(mySeq);
            if (msg) {
                const p = msg.querySelector(".stream-text");
                const partialText = p?.textContent || "";
                msg.id = "";
                msg.classList.remove("streaming");
                if (partialText) {
                    state.lastReplyText = partialText;
                    finalizeStreamingMessage(partialText, null, mySeq);
                } else {
                    msg.remove();
                    addMessage("assistant", "服务暂时不可用，请稍后重试。");
                }
            } else {
                addMessage("assistant", "服务暂时不可用，请稍后重试。");
            }
            setEmotion(emotionConfig.neutral);
            if (els.avatarStatus) els.avatarStatus.textContent = "在线讲解中";
        } finally {
            clearTimeout(streamTimeout);
            // 关键修复: 只有当前 seq 才恢复按钮, 防止旧 seq 取消新请求的 disabled
            if (mySeq === state.requestSeq) {
                els.sendBtn.disabled = false;
            }
        }
    }

    function renderScenicBrief(data) {
        if (!data) return;
        if (els.briefName) els.briefName.textContent = data.name || "智慧景区导览系统";
        if (els.briefPositioning) els.briefPositioning.textContent = data.positioning || "多模态数字人导览平台";
        if (els.capabilityChips) {
            els.capabilityChips.innerHTML = (data.capabilities || []).map((item) => `<span class="capability-chip">${item}</span>`).join("");
        }
    }

    function renderResponseMeta(data = {}) {
        state.lastAudioUrl = data.audioUrl || null;
        state.lastReplyText = data.completeReply || "";
    }

    function showFeedbackBar() {
        if (els.feedbackBar) els.feedbackBar.style.display = "flex";
    }

    function hideFeedbackBar() {
        if (els.feedbackBar) els.feedbackBar.style.display = "none";
    }

    function resetFeedback() {
        state.selectedFeedback = null;
        if (els.feedbackLabel) els.feedbackLabel.textContent = "请对本次回答评分";
        els.starBtns?.forEach(b => b.classList.remove("active"));
        if (els.feedbackSubmit) { els.feedbackSubmit.disabled = true; els.feedbackSubmit.textContent = "提交评价"; }
    }

    async function submitFeedback(score) {
        if (!state.lastConversationId) return;
        await api("/api/v1/feedback", {
            method: "POST",
            body: JSON.stringify({ conversationId: state.lastConversationId, sessionId: state.sessionId, satisfaction: score }),
        });
        if (els.feedbackLabel) els.feedbackLabel.textContent = "✅ 感谢评分！";
        els.starBtns?.forEach(b => b.classList.remove("active"));
        if (els.feedbackSubmit) { els.feedbackSubmit.disabled = true; els.feedbackSubmit.textContent = "已提交 ✓"; }
        state.selectedFeedback = null;
        setTimeout(() => hideFeedbackBar(), 2000);
    }

    async function sendMessage(text) {
        if (state.streamMode) {
            await sendMessageStream(text);
            return;
        }
        // 关键修复: 非流式入口也要清 SegmentPlayer, 避免切模式即泄漏
        audioManager.stop();
        if (typeof segmentPlayer !== 'undefined') segmentPlayer.stop();
        // 关键修复: 取消挂起的自动播放
        if (state.pendingAudioTimer) {
            clearTimeout(state.pendingAudioTimer);
            state.pendingAudioTimer = null;
        }
        state.lastAudioUrl = null;
        state.lastAudioBase64 = null;
        els.sendBtn.disabled = true;
        const message = (text || els.chatInput?.value || "").trim();
        if (!message) { els.sendBtn.disabled = false; return; }
        if (els.chatInput) els.chatInput.value = "";

        // Auto-trigger navigation for location queries
        if (isNavQuery(message)) {
            handleNavQuery(message);
        }

        addMessage("user", message);
        hideFeedbackBar();
        resetFeedback();

        const { emotion, emotionPayload } = detectEmotionFromMessage(message);
        setEmotion(emotionPayload);

        if (window.playAction) {
            if (message.includes("点头") || message.includes("是的") || message.includes("好的")) window.playAction("nod", 1);
            else if (message.includes("挥手") || message.includes("再见") || message.includes("拜拜")) window.playAction("wave", 1);
            else if (message.includes("摇头") || message.includes("不对") || message.includes("不是")) window.playAction("shake", 1);
            else if (message.includes("谢谢") || message.includes("感谢")) window.playAction("bow", 1);
        }

        const chatLog = els.chatLog;
        const loadingMsg = document.createElement("div");
        loadingMsg.className = "message assistant";
        loadingMsg.id = "loading-msg";
        loadingMsg.innerHTML = `<div class="message-avatar">${ASSISTANT_LOGO_SVG}</div><div class="message-content"><p>思考中<span class="dots"></span></p></div>`;
        chatLog.appendChild(loadingMsg);
        chatLog.scrollTop = chatLog.scrollHeight;

        try {
            const requestStart = Date.now();
            const result = await api("/api/v1/chat/text", {
                method: "POST",
                body: JSON.stringify({
                    message, sessionId: state.sessionId, userId: state.userId,
                    modelId: selectedModelId(),
                    interest: state.interest, gps: state.gpsCoords,
                    emotion,
                }),
            });

            const loadingEl = document.getElementById("loading-msg");
            if (loadingEl) loadingEl.remove();

            if (result.code !== 0) {
                addMessage("assistant", result.message || "服务暂时不可用");
                setEmotion(emotionConfig.neutral);
                return;
            }

            const data = result.data || {};
            state.lastConversationId = data.conversationId;
            showFeedbackBar();
            // 关键修复: 用 fetched data.audioUrl, 不要保留旧的 (会播错内容)
            state.lastAudioUrl = data.audioUrl || null;
            state.lastAudioBase64 = data.audioBase64 || null;

            if (data.emotion) setEmotion(data.emotion);
            const replyText = data.reply || '好的，我已经为您处理。';

            // New emotion state system
            if (data.emotionState && window.updateEmotionState) {
                window.updateEmotionState(data.emotionState, data.ttsTotalMs, data.expression);
            }
            // New action timeline system
            if (data.actions && data.actions.length > 0 && window.setActionTimeline) {
                const isNewFormat = typeof data.actions[0] === 'object' && data.actions[0].type;
                if (isNewFormat) {
                    window.setActionTimeline(data.actions);
                } else if (window.playAction) {
                    data.actions.forEach((actionName, index) => {
                        setTimeout(() => window.playAction(actionName, index === 0 ? 2 : 1), index * 1500);
                    });
                }
            }
            // New expression override
            if (data.expression && window.vrmBlendShapeTargets) {
                const exp = { ...data.expression };
                exp._startTime = performance.now();
                // 表情渐隐时长: 情绪强度越高维持越久
                exp._duration = (exp.intensity || 0.6) >= 0.7 ? 2500 : (exp.intensity || 0.6) >= 0.4 ? 1800 : 1200;
                for (const [k, v] of Object.entries(exp)) {
                    if (!k.startsWith('_')) window.vrmBlendShapeTargets[k] = v;
                }
            }

            if (window.triggerGesturesForReply) window.triggerGesturesForReply(replyText);
            if (!data.actions && window.triggerGesturesForReply) {
                window.triggerGesturesForReply(replyText);
            }

            state.lastAudioUrl = data.audioUrl || null;
            state.lastReplyText = replyText;
            addMessage("assistant", replyText);
        } catch (e) {
            console.error("非流式响应异常:", e);
            const loadingEl = document.getElementById("loading-msg");
            if (loadingEl) loadingEl.remove();
            addMessage("assistant", "服务暂时不可用，请稍后重试。");
            setEmotion(emotionConfig.neutral);
        } finally {
            els.sendBtn.disabled = false;
        }
    }

    function setupInterestSwitcher() {
        if (!els.interestGrid) return;
        els.interestGrid.addEventListener("click", async (event) => {
            const card = event.target.closest(".interest-card");
            if (!card) return;
            state.interest = card.dataset.interest;
            els.interestGrid.querySelectorAll(".interest-card").forEach(c => c.classList.remove("active"));
            card.classList.add("active");
            const result = await api("/api/v1/scenic/routes");
            const route = (result.data || []).find(r => r.interest === state.interest);
            renderRoute(route);
            updateAvatarMood("warm");
            const text = `已切换为"${card.textContent}"偏好，我会按这个方向为您推荐路线。`;
            if (state.streamMode) {
                getOrCreateStreamingMessage(state.requestSeq);
                updateStreamingText(text, state.requestSeq);
                finalizeStreamingMessage(text, emotionConfig.warm, state.requestSeq);
            } else {
                addMessage("assistant", text);
            }
        });
    }

    function setupQuickActions() {
        document.querySelectorAll(".quick-questions button").forEach(btn => {
            btn.addEventListener("click", () => sendMessage(btn.dataset.question));
        });
    }

    function setupVoiceInput() {
        if (!els.voiceBtn) return;
        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SpeechRecognition && (!window.MediaRecorder || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia)) {
            els.voiceBtn.textContent = "不支持语音";
            els.voiceBtn.disabled = true;
            return;
        }
        let recognition = null;
        let isRecognizing = false;
        let finalText = "";
        let interimText = "";
        let recognitionFailed = false;
        let useRecorderFallback = !SpeechRecognition;
        let recorder = null;
        let recorderStream = null;
        let recorderChunks = [];
        let recorderStartPending = false;
        let cancelPendingRecorderStart = false;

        function setVoiceStatus(text, resetDelay = 0) {
            const statusEl = document.getElementById("avatar-status");
            if (!statusEl) return;
            statusEl.textContent = text;
            if (resetDelay) {
                window.setTimeout(() => {
                    if (!isRecognizing) statusEl.textContent = "在线讲解中";
                }, resetDelay);
            }
        }

        function stopRecorderFallback() {
            if (recorder && recorder.state !== "inactive") recorder.stop();
            else if (recorderStartPending) cancelPendingRecorderStart = true;
        }

        function recorderFilename(mimeType) {
            if ((mimeType || "").includes("mp4")) return "voice.m4a";
            if ((mimeType || "").includes("ogg")) return "voice.ogg";
            return "voice.webm";
        }

        async function startRecorderFallback() {
            if (!window.MediaRecorder || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                setVoiceStatus("当前浏览器不支持语音输入", 2500);
                return;
            }
            if (recorderStartPending) return;
            recorderStartPending = true;
            cancelPendingRecorderStart = false;
            try {
                recorderStream = await navigator.mediaDevices.getUserMedia({ audio: true });
                if (cancelPendingRecorderStart) {
                    recorderStream?.getTracks().forEach(track => track.stop());
                    recorderStream = null;
                    return;
                }
                const mimeType = [
                    "audio/webm;codecs=opus",
                    "audio/webm",
                    "audio/mp4",
                    "audio/ogg;codecs=opus",
                    "audio/ogg",
                ].find(type => MediaRecorder.isTypeSupported(type)) || "";
                recorder = mimeType ? new MediaRecorder(recorderStream, { mimeType }) : new MediaRecorder(recorderStream);
                recorderChunks = [];
                isRecognizing = true;
                els.voiceBtn.classList.add("recording");
                setVoiceStatus("请说话...");
                recorder.ondataavailable = (event) => {
                    if (event.data && event.data.size) recorderChunks.push(event.data);
                };
                recorder.onerror = () => setVoiceStatus("录音失败，请重试", 2500);
                recorder.onstop = async () => {
                    isRecognizing = false;
                    els.voiceBtn.classList.remove("recording");
                    recorderStream?.getTracks().forEach(track => track.stop());
                    recorderStream = null;
                    const recordedMimeType = recorder.mimeType || mimeType || "audio/webm";
                    const audioBlob = new Blob(recorderChunks, { type: recordedMimeType });
                    recorder = null;
                    if (!audioBlob.size) {
                        setVoiceStatus("未录到语音，请重试", 2500);
                        return;
                    }
                    try {
                        const form = new FormData();
                        form.append("file", audioBlob, recorderFilename(recordedMimeType));
                        form.append("modelId", selectedModelId());
                        form.append("sessionId", state.sessionId);
                        form.append("interest", state.interest);
                        const response = await fetch(apiUrl("/api/v1/chat/transcribe-upload"), { method: "POST", body: form });
                        const result = await response.json();
                        const text = (result?.data?.text || "").trim();
                        if (!response.ok || result.code !== 0 || !text) {
                            setVoiceStatus(result.message || "语音转写失败，请重试", 2500);
                            return;
                        }
                        if (els.chatInput) els.chatInput.value = text;
                        setVoiceStatus("在线讲解中");
                        sendMessage(text);
                    } catch (error) {
                        setVoiceStatus("语音转写服务不可用，请重试", 2500);
                    }
                };
                recorder.start();
            } catch (error) {
                isRecognizing = false;
                els.voiceBtn.classList.remove("recording");
                if (!cancelPendingRecorderStart) setVoiceStatus("未检测到可用麦克风", 2500);
            } finally {
                recorderStartPending = false;
            }
        }

        function startListening() {
            if (isRecognizing) {
                try { stopListening(); } catch (e) { /* ignore */ }
                return;
            }
            if (!SpeechRecognition || useRecorderFallback) {
                startRecorderFallback();
                return;
            }
            recognition = new SpeechRecognition();
            recognition.lang = "zh-CN";
            recognition.continuous = false;
            recognition.interimResults = true;
            recognition.maxAlternatives = 1;
            isRecognizing = true;
            finalText = "";
            interimText = "";
            recognitionFailed = false;
            els.voiceBtn.classList.add("recording");
            setVoiceStatus("请说话...");
            recognition.onresult = (event) => {
                interimText = "";
                for (let i = event.resultIndex; i < event.results.length; i++) {
                    const transcript = event.results[i][0].transcript;
                    if (event.results[i].isFinal) finalText += transcript;
                    else interimText = transcript;
                }
                if (els.chatInput) els.chatInput.value = (finalText + interimText).trim();
            };
            recognition.onerror = (event) => {
                recognitionFailed = true;
                isRecognizing = false;
                els.voiceBtn.classList.remove("recording");
                if (["network", "service-not-allowed"].includes(event.error)) {
                    useRecorderFallback = true;
                }
                const messages = {
                    "no-speech": "未听到语音，请重试",
                    "audio-capture": "未检测到麦克风",
                    "not-allowed": "请允许浏览器使用麦克风",
                    "service-not-allowed": "浏览器语音服务不可用",
                    "network": "浏览器语音服务网络异常",
                };
                if (event.error === "aborted") setVoiceStatus("在线讲解中");
                else if (useRecorderFallback) setVoiceStatus("浏览器语音服务不可用，下次将使用录音转写", 2500);
                else setVoiceStatus(messages[event.error] || "语音识别失败，请重试", 2500);
            };
            recognition.onend = () => {
                isRecognizing = false;
                els.voiceBtn.classList.remove("recording");
                const text = (finalText || interimText).trim();
                if (!recognitionFailed) {
                    setVoiceStatus("在线讲解中");
                    if (text && text.length >= 2) sendMessage(text);
                }
                finalText = "";
                interimText = "";
                recognition = null;
            };
            try {
                recognition.start();
            } catch (error) {
                isRecognizing = false;
                els.voiceBtn.classList.remove("recording");
                useRecorderFallback = true;
                setVoiceStatus("语音服务启动失败，下次将使用录音转写", 2500);
            }
        }
        function stopListening() {
            if ((recorder && recorder.state !== "inactive") || recorderStartPending) stopRecorderFallback();
            else if (recognition && isRecognizing) recognition.stop();
        }
        els.voiceBtn.addEventListener("mousedown", (e) => { e.preventDefault(); startListening(); });
        els.voiceBtn.addEventListener("mouseup", () => stopListening());
        els.voiceBtn.addEventListener("mouseleave", () => stopListening());
        els.voiceBtn.addEventListener("touchstart", (e) => { e.preventDefault(); startListening(); }, { passive: false });
        els.voiceBtn.addEventListener("touchend", (e) => { e.preventDefault(); stopListening(); }, { passive: false });
        els.voiceBtn.addEventListener("touchcancel", () => stopListening());
    }

    const AVATAR_MODEL_SESSION_KEY = "tourist-avatar-model-id";
    const AVATAR_STYLE_LABELS_BY_MODEL_ID = Object.freeze({
        "景.vrm": "新中式·亲和导览",
        "区.vrm": "现代休闲·活力互动",
        "灵.vrm": "传统汉服·文化讲解",
        "山.vrm": "户外山野·文雅导览",
    });

    const AVATAR_STYLE_LABELS = Object.freeze({
        "新中式导览服|亲和讲解员": "新中式·亲和导览",
        "现代休闲装|活泼互动型": "现代休闲·活力互动",
        "传统汉服|知识型讲解": "传统汉服·文化讲解",
        "户外登山服|文雅讲解": "户外山野·文雅导览",
    });

    function containsModelFilename(text) {
        return /\.(?:vrm|glb|fbx)(?:$|[^a-z0-9])/i.test(text);
    }

    function getAvatarStyleLabel(model) {
        const modelId = String(model?.modelId || "").trim();
        const outfit = String(model?.outfit || "").trim();
        const style = String(model?.style || "").trim();
        const label = AVATAR_STYLE_LABELS_BY_MODEL_ID[modelId]
            || AVATAR_STYLE_LABELS[`${outfit}|${style}`]
            || style
            || outfit
            || "经典导览风格";
        return containsModelFilename(label) ? "经典导览风格" : label;
    }

    let avatarConfig = { name: "小灵", style: "亲和讲解员", vrmModel: "", modelId: "" };

    function setAvatarModelStatus(message, isError = false) {
        if (!els.avatarModelStatus) return;
        els.avatarModelStatus.textContent = message;
        els.avatarModelStatus.classList.toggle("error", isError);
    }

    function selectedModelId() {
        return state.modelId || "";
    }

    async function fetchAvatarConfig(modelId) {
        const query = modelId ? `?modelId=${encodeURIComponent(modelId)}` : "";
        const res = await api(`/api/v1/avatar/config${query}`);
        if (res.code !== 0 || !res.data) throw new Error(res.message || "模型配置不可用");
        return res.data;
    }

    async function loadAvatarConfig(modelId = selectedModelId()) {
        try {
            const data = await fetchAvatarConfig(modelId);
            avatarConfig = data;
            state.modelId = data.modelId || modelId || "";
            window.avatarConfig = data;
            applyAvatarConfig(data);
            return data;
        } catch (e) {
            console.warn("avatar config not available", e);
            throw e;
        }
    }

    async function loadAvatarModels() {
        setAvatarModelStatus("正在加载风格列表...");
        if (els.avatarModelSelect) els.avatarModelSelect.disabled = true;
        try {
            const res = await api("/api/v1/avatar/models");
            const models = Array.isArray(res.data) ? res.data : [];
            state.modelOptions = models;
            if (!models.length) {
                if (els.avatarModelSelect) els.avatarModelSelect.innerHTML = '<option value="">暂无可用风格</option>';
                setAvatarModelStatus("暂无可用风格，请稍后重试", true);
                return;
            }
            const saved = sessionStorage.getItem(AVATAR_MODEL_SESSION_KEY) || "";
            const selected = models.find(model => model.modelId === saved)?.modelId || models[0].modelId;
            if (els.avatarModelSelect) {
                els.avatarModelSelect.replaceChildren(...models.map(model => {
                    const option = document.createElement("option");
                    option.value = String(model.modelId || "");
                    option.textContent = getAvatarStyleLabel(model);
                    return option;
                }));
                els.avatarModelSelect.value = selected;
                els.avatarModelSelect.disabled = false;
            }
            state.modelId = selected;
            sessionStorage.setItem(AVATAR_MODEL_SESSION_KEY, selected);
            await loadAvatarConfig(selected);
            setAvatarModelStatus("已加载当前风格，可按需切换");
        } catch (e) {
            if (els.avatarModelSelect) {
                els.avatarModelSelect.innerHTML = '<option value="">风格加载失败</option>';
                els.avatarModelSelect.disabled = true;
            }
            setAvatarModelStatus("风格加载失败，请刷新重试", true);
        }
    }

    async function switchAvatarModel(nextModelId) {
        const previousModelId = state.modelId;
        if (!nextModelId || nextModelId === previousModelId || state.modelSwitching) return;
        state.modelSwitching = true;
        if (els.avatarModelSelect) els.avatarModelSelect.disabled = true;
        setAvatarModelStatus("正在切换风格...");
        try {
            const nextConfig = await fetchAvatarConfig(nextModelId);
            if (!window.reloadVRMModel) throw new Error("3D 模型加载器尚未就绪");
            await enqueueReload(new URL(String(nextConfig.vrmModel).replace(/^\/+/, ""), document.baseURI).toString());
            state.modelId = nextModelId;
            sessionStorage.setItem(AVATAR_MODEL_SESSION_KEY, nextModelId);
            avatarConfig = nextConfig;
            window.avatarConfig = nextConfig;
            applyAvatarConfig(nextConfig);
            setAvatarModelStatus("风格已切换");
        } catch (e) {
            if (els.avatarModelSelect) els.avatarModelSelect.value = previousModelId;
            setAvatarModelStatus("切换失败，已保留原风格", true);
        } finally {
            state.modelSwitching = false;
            if (els.avatarModelSelect && state.modelOptions.length) els.avatarModelSelect.disabled = false;
        }
    }

    function setupAvatarModelPicker() {
        els.avatarModelSelect?.addEventListener("change", (event) => switchAvatarModel(event.target.value));
    }

    function enqueueReload(vrmPath) {
        const prev = enqueueReload._queue || Promise.resolve();
        const next = prev.then(() => window.reloadVRMModel(vrmPath));
        enqueueReload._queue = next.catch(() => {});
        return next;
    }

    function applyAvatarConfig(config) {
        const name = config.name || "小灵";
        const style = config.style || "";
        const voice = config.voice || "";
        const outfit = config.outfit || "";

        const titleSpan = document.getElementById("avatar-title-name");
        if (titleSpan) titleSpan.textContent = '\u201c' + name + '\u201d';

        document.querySelectorAll(".avatar-dynamic-name").forEach(el => {
            el.textContent = '\u201c' + name + '\u201d';
        });

        const emotionLabel = document.getElementById("emotion-label");
        if (emotionLabel) {
            const parts = [name, style].filter(Boolean);
            emotionLabel.textContent = parts.join(" · ") || "VRM 数字人";
        }
        const dynTitle = document.getElementById("avatar-dynamic-title");
        if (dynTitle) {
            const parts = [name, style].filter(Boolean);
            dynTitle.textContent = parts.join(" · ") || "VRM 数字人";
        }

        const avatarStatus = document.getElementById("avatar-status");
        if (avatarStatus && style) {
            avatarStatus.textContent = style + "\u4e2d";
        }
    }

    async function bootstrap() {
        setupAvatarModelPicker();
        const [routes, brief, configResp] = await Promise.all([
            api("/api/v1/scenic/routes"),
            api("/api/v1/scenic/brief"),
            api("/api/v1/config"),
            loadAvatarModels(),
        ]);
        const clientCfg = configResp?.data || {};
        if (clientCfg.amapKey) {
            window._AMAP_KEY = clientCfg.amapKey;
        }
        renderScenicBrief(brief.data || {});
        renderRoute((routes.data || [])[0]);
        updateAvatarMood("warm");
        const { emotion, emotionPayload } = detectEmotionFromMessage("你好");
        setEmotion(emotionPayload);
        startGPSWatch();
        initMap();
        initWeather();
    }

    function startGPSWatch() {
        if (!navigator.geolocation) { console.log("GPS not supported by browser"); return; }
        state.gpsWatchId = navigator.geolocation.watchPosition(
            (pos) => {
                state.gpsCoords = { lat: pos.coords.latitude, lng: pos.coords.longitude, accuracy: pos.coords.accuracy };
                state.gpsEnabled = true;
            },
            (err) => { console.log("GPS error:", err.message); state.gpsEnabled = false; state.gpsCoords = null; },
            { enableHighAccuracy: false, timeout: 10000, maximumAge: 60000 }
        );
    }

    // ========== MAP (高德) NAVIGATION ==========

    const KNOWN_SPOTS = [
        "灵山大佛", "梵宫", "九龙灌浴", "祥符禅寺", "五印坛城",
        "佛足坛", "五明桥", "无尽意斋", "五智门", "灵山广场",
        "拈花广场", "梵天花海", "香月花街", "拈花堂", "五灯湖",
        "鹿鸣谷", "游客中心", "湖景步道", "观景平台",
        "静心休憩区", "文化商店", "休闲补给区", "马山", "马山镇",
    ];

    function _extractSpotNames(text) {
        const found = [];
        for (const name of KNOWN_SPOTS) {
            if (text.includes(name)) {
                found.push(name);
            }
        }
        return found;
    }

    function _autoShowMapPanel() {
        if (!els.mapBody || !els.mapSection) return;
        els.mapBody.classList.remove('hidden');
        if (els.mapToggleIcon) els.mapToggleIcon.classList.remove('collapsed');
        els.mapSection.classList.remove('fullscreen');
        if (els.mapFullscreenClose) els.mapFullscreenClose.classList.add('hidden');
        if (amapInstance) setTimeout(() => amapInstance.resize(), 350);
    }

    let amapInstance = null;
    let amapMarkers = [];
    let amapSdkPromise = null;

    // ========== WEATHER (高德天气API via backend proxy) ==========

    const WEATHER_ICON_MAP = {
        '晴': { icon: '☀️', tip: '阳光充足，记得防晒补水' },
        '多云': { icon: '⛅', tip: '云层适宜，体感舒适，适合户外游览' },
        '阴': { icon: '☁️', tip: '云量较多，光线柔和，适合拍照留念' },
        '小雨': { icon: '️', tip: '有零星小雨，建议携带雨具' },
        '中雨': { icon: '🌧️', tip: '雨势较大，建议室内景点优先' },
        '大雨': { icon: '🌧️', tip: '雨势较大，请注意防滑，建议室内景点优先' },
        '暴雨': { icon: '⛈️', tip: '暴雨天气，建议暂缓户外游览' },
        '雷阵雨': { icon: '⛈️', tip: '雷电天气，请避开空旷区域，注意安全' },
        '小雪': { icon: '�️', tip: '有降雪，注意保暖防滑' },
        '大雪': { icon: '❄️', tip: '雪量较大，注意保暖，路径可能湿滑' },
        '雾': { icon: '�️', tip: '能见度低，请注意游园安全' },
        '霾': { icon: '�️', tip: '空气质量较差，敏感人群注意防护' },
    };

    function applyWeatherData(data) {
        if (!data) return;
        const weatherText = data.weather || '';
        const mapped = WEATHER_ICON_MAP[weatherText] || { icon: '⛅', tip: '请根据天气合理安排游览' };

        if (els.weatherIcon) els.weatherIcon.textContent = mapped.icon;
        if (els.weatherTemp) els.weatherTemp.textContent = `${data.temperature || '--'}°`;
        if (els.weatherDesc) els.weatherDesc.textContent = weatherText || '未知';
        if (els.weatherHumidity) els.weatherHumidity.textContent = `${data.humidity || '--'}%`;
        if (els.weatherWind) {
            const wd = data.wind_direction || '';
            const wp = data.wind_power || '';
            els.weatherWind.textContent = wp ? `${wd} ${wp}级`.trim() : (wd || '微风');
        }
        // 体感温度：高德API不直接返回体感，这里用温度±2估算
        if (els.weatherFeels) {
            const temp = parseFloat(data.temperature);
            if (!isNaN(temp)) {
                const season = new Date().getMonth() + 1;
                const adjust = (season >= 12 || season <= 2) ? -2 : (season >= 6 && season <= 8) ? 2 : 0;
                els.weatherFeels.textContent = `${temp + adjust}°`;
            } else {
                els.weatherFeels.textContent = '--°';
            }
        }
        if (els.weatherTip) els.weatherTip.textContent = mapped.tip;
        if (els.weatherLoc) {
            const parts = [data.province, data.city].filter(Boolean);
            els.weatherLoc.textContent = parts.join('·') || '无锡·灵山胜境';
        }
    }

    function applyDefaultWeather() {
        if (els.weatherIcon) els.weatherIcon.textContent = '⛅';
        if (els.weatherTemp) els.weatherTemp.textContent = '--°';
        if (els.weatherDesc) els.weatherDesc.textContent = '正在获取天气信息…';
        if (els.weatherHumidity) els.weatherHumidity.textContent = '--%';
        if (els.weatherWind) els.weatherWind.textContent = '--';
        if (els.weatherFeels) els.weatherFeels.textContent = '--°';
        if (els.weatherTip) els.weatherTip.textContent = '适宜游览，记得防晒补水';
    }

    async function initWeather() {
        if (!els.weatherSection) return;
        applyDefaultWeather();
        try {
            const res = await api('/api/v1/weather');
            if (res && res.code === 0 && res.data) {
                applyWeatherData(res.data);
            } else {
                throw new Error(res?.msg || 'weather api error');
            }
        } catch (e) {
            console.warn('Weather fetch failed:', e.message);
            if (els.weatherDesc) els.weatherDesc.textContent = '天气获取失败';
        }
        // 每小时刷新一次
        setTimeout(initWeather, 60 * 60 * 1000);
    }

    function initMap() {
        if (!els.mapContainer) return;
        // 容器不可见或无尺寸时延迟重试
        if (!els.mapContainer.offsetWidth || !els.mapContainer.offsetHeight) {
            setTimeout(() => {
                if (amapInstance) {
                    try { amapInstance.resize(); } catch(e) {}
                } else {
                    initMap();
                }
            }, 300);
            return;
        }
        if (typeof AMap === 'undefined') {
            renderMapFallback('正在连接地图服务…');
            loadAmapSDK()
                .then(() => setTimeout(initMap, 100))
                .catch(() => renderMapFallback());
            return;
        }
        try {
            if (amapInstance) {
                amapInstance.resize();
                return;
            }
            amapInstance = new AMap.Map(els.mapContainer, {
                zoom: 15,
                center: [120.095, 31.424],
                mapStyle: 'amap://styles/fresh',
                resizeEnable: true,
            });
            loadScenicSpots();
            if (els.mapStatus) els.mapStatus.textContent = "已加载景区地图";
            setupMapToggle();
        } catch (e) {
            console.warn("Map init error:", e);
            if (els.mapStatus) els.mapStatus.textContent = "地图加载失败";
        }
    }

    function loadAmapSDK() {
        if (typeof AMap !== 'undefined') return Promise.resolve();
        if (amapSdkPromise) return amapSdkPromise;
        const key = window._AMAP_KEY;
        if (!key) {
            if (els.mapStatus) els.mapStatus.textContent = "地图未配置";
            return Promise.reject(new Error("AMAP_KEY_MISSING"));
        }
        amapSdkPromise = new Promise((resolve, reject) => {
            let attempt = 0;
            const load = () => {
                attempt += 1;
                const script = document.createElement("script");
                // Load through our same-origin proxy. Direct browser requests to
                // webapi.amap.com are reset on some local desktop networks.
                script.src = apiUrl(`/api/v1/map/amap-sdk.js?_=${Date.now()}`);
                script.async = true;
                script.onload = () => typeof AMap !== 'undefined' ? resolve() : fail();
                script.onerror = fail;
                document.head.appendChild(script);
            };
            const fail = () => {
                if (attempt < 3) {
                    setTimeout(load, attempt * 800);
                } else {
                    if (els.mapStatus) els.mapStatus.textContent = "地图 SDK 不可用，已切换为景点导航列表";
                    reject(new Error("AMAP_SDK_LOAD_FAILED"));
                }
            };
            load();
        });
        return amapSdkPromise;
    }

    async function renderMapFallback(statusText = '地图服务暂时不可用，您仍可选择景点并打开导航。') {
        if (!els.mapContainer || amapInstance) return;
        if (els.mapContainer.dataset.fallbackReady === 'true') return;
        els.mapContainer.dataset.fallbackReady = 'true';
        els.mapContainer.innerHTML = `<div class="map-fallback"><strong>景点导航</strong><p>${statusText}</p><div class="map-fallback-spots"></div></div>`;
        const list = els.mapContainer.querySelector('.map-fallback-spots');
        try {
            const res = await api('/api/v1/navigation/scenic-spots');
            (res.data || []).slice(0, 12).forEach((spot) => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'nav-btn';
                button.textContent = spot.name;
                button.addEventListener('click', () => showNavigationTo(spot.name, spot.lat, spot.lng));
                list?.appendChild(button);
            });
        } catch (error) {
            if (list) list.textContent = '景点列表暂时无法加载。';
        }
    }

    async function loadScenicSpots() {
        if (!amapInstance) return;
        try {
            const res = await api("/api/v1/navigation/scenic-spots");
            const spots = res.data || [];
            spots.forEach(spot => {
                const marker = new AMap.Marker({
                    position: [spot.lng, spot.lat],
                    map: amapInstance,
                    label: { content: spot.name, direction: 'top', offset: new AMap.Pixel(0, -8) },
                });
                marker.on('click', () => {
                    showNavigationTo(spot.name, spot.lat, spot.lng);
                });
                amapMarkers.push(marker);
            });
        } catch (e) {
            console.warn("Load scenic spots error:", e);
        }
    }

    function showNavigationTo(dstName, dstLat, dstLng) {
        if (amapInstance) {
            amapInstance.setCenter([dstLng, dstLat]);
            amapInstance.setZoom(16);
        }
        if (els.mapInfo) {
            const srcLng = state.gpsCoords ? state.gpsCoords.lng : 120.095;
            const srcLat = state.gpsCoords ? state.gpsCoords.lat : 31.424;
            const amapUrl = `https://uri.amap.com/navigation?to=${dstLng},${dstLat},${encodeURIComponent(dstName)}&mode=walking&coordinate=gaode&from=${srcLng},${srcLat},我的位置`;
            els.mapInfo.innerHTML = `
                <span>📍 ${dstName}</span>
                <a class="nav-btn" href="${amapUrl}" target="_blank" title="在高德地图中查看导航">高德导航</a>
                <button class="nav-btn" onclick="window.toggleMap()">展开地图</button>
            `;
        }
        if (els.mapStatus) els.mapStatus.textContent = dstName;
    }

    function setupMapToggle() {
        // 地图常驻显示：点击 expand 按钮触发全屏切换
        if (els.mapExpandBtn) {
            els.mapExpandBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                window.toggleMap();
            });
        }
        // 兼容旧逻辑：保留对 map-toggle 的监听，但只触发全屏（不再折叠）
        if (els.mapToggle) {
            els.mapToggle.addEventListener('click', (e) => {
                if (e.target === els.mapExpandBtn) return;
                window.toggleMap();
            });
        }
    }

    window.toggleMap = function() {
        if (!els.mapBody || !els.mapSection) return;
        const isFullscreen = els.mapSection.classList.contains('fullscreen');
        if (isFullscreen) {
            els.mapSection.classList.remove('fullscreen');
            if (els.mapFullscreenClose) els.mapFullscreenClose.classList.add('hidden');
            if (amapInstance) setTimeout(() => amapInstance.resize(), 200);
            return;
        }
        els.mapBody.classList.remove('hidden');
        if (els.mapToggleIcon) els.mapToggleIcon.classList.remove('collapsed');
        els.mapSection.classList.add('fullscreen');
        if (els.mapFullscreenClose) els.mapFullscreenClose.classList.remove('hidden');
        if (amapInstance) setTimeout(() => amapInstance.resize(), 200);
    };
    if (els.mapFullscreenClose) {
        els.mapFullscreenClose.addEventListener('click', () => window.toggleMap());
    }

    // Check if user message is navigation-related
    const NAV_KEYWORDS = ['怎么走', '怎么去', '在哪', '在哪里', '位置', '导航', '多远', '距离', '路线', '方向', '步行', '走过去', '想去', '要去', '怎么到', '如何到', '带我去', '指路'];

    function isNavQuery(text) {
        const norm = text.replace(/\s/g, '');
        return NAV_KEYWORDS.some(k => norm.includes(k));
    }

    async function handleNavQuery(text) {
        try {
            const spotsInText = _extractSpotNames(text);
            const fallbackSpot = spotsInText.length > 0
                ? spotsInText[0]
                : (state.lastMentionedSpots.length > 0 ? state.lastMentionedSpots[0] : null);
            const body = { message: text, sessionId: state.sessionId };
            if (fallbackSpot && spotsInText.length === 0) {
                body.message = `导航到${fallbackSpot} ${text}`;
            }
            const res = await api("/api/v1/navigation/query", {
                method: "POST",
                body: JSON.stringify(body),
            });
            if (res.code === 0 && res.data && res.data.destination) {
                const d = res.data.destination;
                showNavigationTo(d.name, d.lat, d.lng);
                _autoShowMapPanel();
                if (window.toggleMap) {
                    setTimeout(() => window.toggleMap(), 400);
                }
            }
        } catch (e) {
            console.warn("Nav query error:", e);
        }
    }

    els.sendBtn?.addEventListener("click", () => sendMessage());
    els.chatInput?.addEventListener("keydown", e => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });
    els.speakToggle?.addEventListener("click", () => {
        state.speechEnabled = !state.speechEnabled;
        // 关键修复: 关闭播报时立即停掉所有音频, 不让已开始的播完
        if (!state.speechEnabled) {
            // 关键修复: 只暂停不清理 segmentPlayer 缓冲区, 保留已到达的音频段,
            // 否则 "静音→立刻开启" 时早到的段已丢失, 无法续播
            if (typeof segmentPlayer !== 'undefined') {
                segmentPlayer.pauseForMute();
            }
            audioManager.stop();
            if (window.speechSynthesis) {
                try { window.speechSynthesis.cancel(); } catch (e) {}
            }
            if (state.pendingAudioTimer) {
                clearTimeout(state.pendingAudioTimer);
                state.pendingAudioTimer = null;
            }
        } else {
            // 关键修复: 重新开启时恢复语音引擎, 并续播静音期间缓存的回复音频
            if (window.speechSynthesis && window.speechSynthesis.resume) {
                try { window.speechSynthesis.resume(); } catch (e) {}
            }
            if (typeof segmentPlayer !== 'undefined' && typeof resumeSpeech === 'function') {
                setTimeout(resumeSpeech, 0);
            }
        }
        els.speakToggle.classList.toggle("active", state.speechEnabled);
        els.speakToggle.classList.toggle("off", !state.speechEnabled);
        els.speakToggle.title = state.speechEnabled ? "播报开" : "播报关";
    });

    setupInterestSwitcher();
    setupQuickActions();
    setupVoiceInput();
    function selectFeedback(score) {
        state.selectedFeedback = score;
        els.starBtns?.forEach(b => b.classList.toggle("active", parseInt(b.dataset.score) <= score));
        if (els.feedbackSubmit) els.feedbackSubmit.disabled = false;
    }

    els.starBtns?.forEach(b => b.addEventListener("click", () => selectFeedback(parseInt(b.dataset.score))));
    els.feedbackSubmit?.addEventListener("click", () => {
        if (state.selectedFeedback) submitFeedback(state.selectedFeedback);
    });
    window.applyVRMBlendShape = applyVRMBlendShape;
    bootstrap();
})();

const emotionBlendShapes = {
    warm: {
        happy: 0.4,
        relaxed: 0.2
    },

    delighted: {
        happy: 0.8
    },

    focused: {
        neutral: 1
    },

    caring: {
        relaxed: 0.5,
        sad: 0.2
    },

    sad: {
        sad: 0.7
    },

    surprised: {
        surprised: 0.8,
        aa: 0.3
    },

    neutral: {
        neutral: 1
    },

 
};
window.emotionBlendShapes = emotionBlendShapes;
