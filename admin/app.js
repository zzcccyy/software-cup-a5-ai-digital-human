(function () {
    const ADMIN_TOKEN_KEY = "admin_token";
    const state = {
        adminToken: window.localStorage.getItem(ADMIN_TOKEN_KEY) || "",
        adminUser: "",
        currentPage: "dashboard",
        knowledge: [],
        faq: [],
        conversations: [],
        report: null,
        activeKnowledgeSearch: "",
        convPage: 1,
        convPageSize: 20,
        convTotal: 0,
        convPeriod: "",
        convEmotion: "",
        convInterest: "",
        convSatisfaction: "",
        conversationAnalysis: null,
        conversationAnalysisLoading: false,
        conversationAnalysisStale: false,
        conversationAnalysisRequestId: 0,
        conversationRequestId: 0,
        conversationView: "list",
        logPage: 1,
		logPageSize: 10,
		logTotal: 0,
        logFilterAction: "",
        logFilterResource: "",
    };

    function safeArray(value) {
        return Array.isArray(value) ? value : [];
    }

    function escapeHtml(value) {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function showToast(title, message, type = "success") {
        const stack = document.getElementById("toast-stack");
        if (!stack) return;

        const toast = document.createElement("div");
        toast.className = `toast ${type}`;
        toast.innerHTML = `<strong>${escapeHtml(title)}</strong><p>${escapeHtml(message)}</p>`;
        stack.appendChild(toast);

        window.setTimeout(() => {
            toast.remove();
        }, 3200);
    }

    async function api(url, options = {}) {
        const response = await fetch(`/api/v1${url}`, {
            headers: {
                "Content-Type": "application/json",
                ...(state.adminToken ? { "X-ADMIN-TOKEN": state.adminToken } : {}),
                ...(options.headers || {}),
            },
            ...options,
        });

        const json = await response.json().catch(() => ({}));
        if (!response.ok) {
            if (response.status === 401 && !url.startsWith("/admin/auth/")) {
                clearAuth();
                setAuthView(false);
            }
            throw new Error(json.message || "请求失败");
        }
        return json;
    }

    function setAuthView(isAuthenticated) {
        document.body.classList.toggle("authenticated", isAuthenticated);
        document.body.classList.toggle("auth-required", !isAuthenticated);
    }

    function clearAuth() {
        state.adminToken = "";
        state.adminUser = "";
        window.localStorage.removeItem(ADMIN_TOKEN_KEY);
        const errorEl = document.getElementById("login-error");
        if (errorEl) errorEl.textContent = "";
        const userEl = document.getElementById("admin-user");
        if (userEl) userEl.textContent = "管理员";
    }

    async function restoreAuth() {
        if (!state.adminToken) {
            setAuthView(false);
            return false;
        }
        try {
            const res = await api("/admin/auth/me");
            state.adminUser = res.data?.username || "管理员";
            document.getElementById("admin-user").textContent = state.adminUser;
            setAuthView(true);
            return true;
        } catch (error) {
            clearAuth();
            setAuthView(false);
            return false;
        }
    }

    async function login(username, password) {
        const response = await fetch("/api/v1/admin/auth/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password }),
        });
        const json = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(json.message || "登录失败");
        }
        state.adminToken = json.data?.token || "";
        state.adminUser = json.data?.username || username;
        window.localStorage.setItem(ADMIN_TOKEN_KEY, state.adminToken);
        document.getElementById("admin-user").textContent = state.adminUser;
        setAuthView(true);
    }

    async function logout() {
        try {
            if (state.adminToken) {
                await api("/admin/auth/logout", { method: "POST" });
            }
        } finally {
            clearAuth();
            setAuthView(false);
        }
    }

    async function runAction(task, successTitle, successMessage, trigger = null) {
        // trigger: 可选，触发本次操作的按钮（或 DOM 事件）。传入后会在请求期间
        // 自动禁用按钮并显示“处理中…”，防止重复点击导致的多次提交。
        let btn = null;
        if (trigger) {
            btn = trigger instanceof Event
                ? (trigger.currentTarget || trigger.target)
                : trigger;
        }
        let prevText = "";
        if (btn && btn.tagName === "BUTTON") {
            prevText = btn.textContent;
            btn.disabled = true;
            btn.dataset.loading = "1";
            btn.textContent = "处理中…";
        }
        try {
            const result = await task();
            if (successTitle && !result?.stale) {
                showToast(successTitle, successMessage || "操作已完成");
            }
            return result;
        } catch (error) {
            showToast("操作失败", error.message || "请稍后重试", "error");
            throw error;
        } finally {
            if (btn && btn.tagName === "BUTTON") {
                btn.disabled = false;
                delete btn.dataset.loading;
                btn.textContent = prevText;
            }
        }
    }

    // 通用确认弹窗（替代原生 confirm，复用 <dialog> 视觉）。返回 Promise<boolean>。
    function confirmDialog(message, { title = "请确认", confirmText = "确定", danger = false } = {}) {
        return new Promise((resolve) => {
            const dialog = document.getElementById("dialog");
            const titleEl = document.getElementById("dialog-title");
            const body = document.getElementById("dialog-body");
            const form = document.getElementById("dialog-form");
            if (!dialog || !form) {
                // 兜底：没有弹窗组件时退回原生 confirm
                resolve(window.confirm(message));
                return;
            }
            // 标记为确认模式，避免触发 dialog-form 的保存逻辑
            dialog.dataset.mode = "confirm";
            titleEl.textContent = title;
            body.innerHTML = `<p class="confirm-text">${escapeHtml(message)}</p>`;

            const footer = form.querySelector(".dialog-footer");
            const originalFooter = footer.innerHTML;
            footer.innerHTML = `
                <button class="btn" type="button" data-confirm="cancel">取消</button>
                <button class="btn ${danger ? "btn-danger" : "btn-primary"}" type="button" data-confirm="ok">${escapeHtml(confirmText)}</button>
            `;

            const cleanup = (result) => {
                footer.innerHTML = originalFooter;
                delete dialog.dataset.mode;
                dialog.removeEventListener("close", onClose);
                dialog.close();
                resolve(result);
            };
            const onClose = () => cleanup(false); // Esc / 点遮罩关闭 = 取消
            dialog.addEventListener("close", onClose);
            footer.querySelector('[data-confirm="cancel"]').addEventListener("click", () => cleanup(false));
            footer.querySelector('[data-confirm="ok"]').addEventListener("click", () => cleanup(true));

            dialog.showModal();
        });
    }

    function setPageMeta(page) {
        const activeNav = document.querySelector(`.nav-item[data-page="${page}"]`);
        document.getElementById("page-title").textContent = activeNav?.dataset.title || "管理后台";
        document.getElementById("page-desc").textContent = activeNav?.dataset.desc || "统一管理后台内容。";
    }

    function switchPage(page) {
        state.currentPage = page;
        setPageMeta(page);

        document.querySelectorAll(".nav-item").forEach((item) => {
            item.classList.toggle("active", item.dataset.page === page);
        });

        document.querySelectorAll(".page").forEach((item) => {
            item.classList.toggle("active", item.id === `page-${page}`);
        });

        if (page === "logs") {
            state.logPage = 1;
            loadOperationLogs();
        }
    }

    function renderEmpty(targetId, text) {
        const target = document.getElementById(targetId);
        if (!target) return;
        target.innerHTML = `<div class="empty-state">${escapeHtml(text)}</div>`;
    }

    let echartsInstances = {};
    const dashboardChartIds = {
        trend: "trend-chart",
        sentiment: "sentiment-chart",
        topic: "topic-chart",
        heat: "heatmap-chart",
        hourly: "hourly-chart",
        profile: "profile-chart",
    };

    function discardDashboardChart(key) {
        const chart = echartsInstances[key];
        try {
            chart?.dispose?.();
        } catch (error) {
            console.warn("dashboard chart dispose failed", key, error);
        }
        delete echartsInstances[key];
        const element = document.getElementById(dashboardChartIds[key]);
        if (element) element._echartsInited = false;
    }

    function initDashboardChart(key, element, options = {}) {
        if (!element || element._echartsInited || typeof echarts === "undefined") return;
        element.style.minHeight = options.minHeight || "200px";
        if (options.position) element.style.position = options.position;
        try {
            const chart = echarts.init(element);
            if (!chart) throw new Error("图表实例创建失败");
            echartsInstances[key] = chart;
            element._echartsInited = true;
        } catch (error) {
            element._echartsInited = false;
            delete echartsInstances[key];
            console.warn("dashboard chart unavailable", key, error);
        }
    }

    function resizeEChartsSafely() {
        Object.entries(echartsInstances).forEach(([key, chart]) => {
            try {
                chart?.resize();
            } catch (error) {
                console.warn("dashboard chart resize failed", key, error);
                if (conversationAnalysisChartIds?.[key]) {
                    disposeConversationAnalysisChart(key);
                } else {
                    discardDashboardChart(key);
                }
            }
        });
    }

    function initECharts() {
        if (typeof echarts === 'undefined') return;
        initDashboardChart("trend", document.getElementById("trend-chart"), { position: "relative" });
        initDashboardChart("sentiment", document.getElementById("sentiment-chart"));
        initDashboardChart("topic", document.getElementById("topic-chart"));
        initDashboardChart("heat", document.getElementById("heatmap-chart"));
        initDashboardChart("hourly", document.getElementById("hourly-chart"));
        initDashboardChart("profile", document.getElementById("profile-chart"));
        if (!window._echartsResizeInited) {
            window._echartsResizeInited = true;
            window.addEventListener('resize', resizeEChartsSafely);
        }
    }

    const conversationAnalysisChartIds = {
        analysisInterest: "conversation-analysis-interest-chart",
        analysisEmotion: "conversation-analysis-emotion-chart",
        analysisSatisfaction: "conversation-analysis-satisfaction-chart",
        analysisTrend: "conversation-analysis-trend-chart",
    };

    function isConversationAnalysisChartUsable(chart) {
        return Boolean(chart) && (typeof chart.isDisposed !== "function" || !chart.isDisposed());
    }

    function disposeConversationAnalysisChart(key) {
        const chart = echartsInstances[key];
        try {
            chart?.dispose?.();
        } catch (error) {
            console.warn("conversation analysis chart dispose failed", key, error);
        }
        delete echartsInstances[key];
        const element = document.getElementById(conversationAnalysisChartIds[key]);
        if (element) element._echartsInited = false;
    }

    function disposeConversationAnalysisCharts() {
        Object.keys(conversationAnalysisChartIds).forEach(disposeConversationAnalysisChart);
    }

    function getConversationAnalysisChart(key, element) {
        const known = echartsInstances[key];
        if (isConversationAnalysisChartUsable(known)) return known;
        if (typeof echarts?.getInstanceByDom === "function") {
            const existing = echarts.getInstanceByDom(element);
            if (isConversationAnalysisChartUsable(existing)) {
                echartsInstances[key] = existing;
                element._echartsInited = true;
                return existing;
            }
        }
        return null;
    }

    function initConversationAnalysisCharts() {
        if (typeof echarts === "undefined") return false;
        const report = document.getElementById("conversation-analysis-report");
        if (!report || report.hidden) return false;
        let ready = true;
        Object.entries(conversationAnalysisChartIds).forEach(([key, id]) => {
            const element = document.getElementById(id);
            if (!element) {
                ready = false;
                return;
            }
            const existing = getConversationAnalysisChart(key, element);
            if (existing) {
                try {
                    existing.resize();
                } catch (error) {
                    ready = false;
                    console.warn("conversation analysis chart resize failed", key, error);
                    disposeConversationAnalysisChart(key);
                }
                return;
            }
            disposeConversationAnalysisChart(key);
            element.style.minHeight = "250px";
            try {
                element.innerHTML = "";
                const chart = echarts.init(element);
                if (!chart) throw new Error("图表实例创建失败");
                echartsInstances[key] = chart;
                element._echartsInited = true;
            } catch (error) {
                ready = false;
                element._echartsInited = false;
                console.warn("conversation analysis chart unavailable", key, error);
            }
        });
        return ready;
    }

    function setAnalysisChartEmpty(chart, text) {
        if (!isConversationAnalysisChartUsable(chart)) return;
        chart.clear();
        chart.setOption({
            animation: false,
            title: {
                text,
                left: "center",
                top: "middle",
                textStyle: { color: "#a89b8c", fontSize: 12, fontWeight: 500 },
            },
        }, true);
    }

    function renderAnalysisPieChart(chart, values) {
        if (!isConversationAnalysisChartUsable(chart)) return;
        const items = safeArray(values);
        const hasValue = items.some((item) => Number(item?.value) > 0);
        if (!items.length || !hasValue) {
            setAnalysisChartEmpty(chart, "暂无偏好数据");
            return;
        }
        const colors = ["#b8923a", "#5a8a6a", "#c4956a", "#8b6d50", "#7c8fa8", "#9a789a"];
        chart.setOption({
            animation: false,
            tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
            legend: {
                type: "scroll",
                bottom: 0,
                left: "center",
                textStyle: { color: "#8c8174", fontSize: 11 },
            },
            series: [{
                type: "pie",
                progressive: 0,
                animation: false,
                radius: ["34%", "68%"],
                center: ["50%", "45%"],
                minAngle: 4,
                avoidLabelOverlap: true,
                itemStyle: { borderRadius: 6, borderColor: "#fffdf9", borderWidth: 3 },
                label: { show: false },
                emphasis: { label: { show: true, fontSize: 13, fontWeight: "bold" } },
                data: items.map((item, index) => ({
                    name: analysisLabel(item?.name),
                    value: Number(item?.value) || 0,
                    itemStyle: { color: colors[index % colors.length] },
                })),
            }],
        }, true);
    }

    function renderAnalysisBarChart(chart, values, color, emptyText) {
        if (!isConversationAnalysisChartUsable(chart)) return;
        const items = safeArray(values);
        const hasValue = items.some((item) => Number(item?.value) > 0);
        if (!items.length || !hasValue) {
            setAnalysisChartEmpty(chart, emptyText);
            return;
        }
        chart.setOption({
            animation: false,
            tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
            grid: { left: "4%", right: "4%", bottom: "12%", top: "8%", containLabel: true },
            xAxis: {
                type: "category",
                data: items.map((item) => analysisLabel(item?.name)),
                axisLabel: { color: "#8c8174", fontSize: 11, interval: 0, rotate: items.length > 4 ? 24 : 0 },
                axisLine: { lineStyle: { color: "rgba(184, 146, 58, 0.2)" } },
            },
            yAxis: {
                type: "value",
                minInterval: 1,
                splitLine: { lineStyle: { color: "rgba(184, 146, 58, 0.1)" } },
                axisLabel: { color: "#8c8174", fontSize: 11 },
            },
            series: [{
                type: "bar",
                progressive: 0,
                animation: false,
                data: items.map((item) => Number(item?.value) || 0),
                barMaxWidth: 30,
                itemStyle: { color, borderRadius: [7, 7, 0, 0] },
                label: { show: true, position: "top", color: "#8c8174", fontSize: 11 },
            }],
        }, true);
    }

    function renderAnalysisTrendChart(chart, values) {
        if (!isConversationAnalysisChartUsable(chart)) return;
        const items = safeArray(values);
        const hasValue = items.some((item) => Number(item?.value) > 0);
        if (!items.length || !hasValue) {
            setAnalysisChartEmpty(chart, "暂无趋势数据");
            return;
        }
        chart.setOption({
            animation: false,
            tooltip: { trigger: "axis" },
            grid: { left: "4%", right: "4%", bottom: "10%", top: "8%", containLabel: true },
            xAxis: {
                type: "category",
                data: items.map((item) => analysisDateLabel(item?.name)),
                axisLabel: {
                    color: "#8c8174",
                    fontSize: 11,
                    interval: items.length > 8 ? Math.ceil(items.length / 8) - 1 : 0,
                    rotate: items.length > 6 ? 24 : 0,
                    hideOverlap: true,
                },
                axisLine: { lineStyle: { color: "rgba(184, 146, 58, 0.2)" } },
            },
            yAxis: {
                type: "value",
                minInterval: 1,
                splitLine: { lineStyle: { color: "rgba(184, 146, 58, 0.1)" } },
                axisLabel: { color: "#8c8174", fontSize: 11 },
            },
            series: [{
                type: "line",
                progressive: 0,
                animation: false,
                smooth: true,
                symbolSize: 8,
                data: items.map((item) => Number(item?.value) || 0),
                lineStyle: { color: "#5a8a6a", width: 3 },
                itemStyle: { color: "#5a8a6a" },
                areaStyle: { color: "rgba(90, 138, 106, 0.12)" },
            }],
        }, true);
    }

    function renderAnalysisFallbackPieChart(targetId, values) {
        const target = document.getElementById(targetId);
        if (!target) return;
        const items = safeArray(values)
            .map((item) => ({ name: analysisLabel(item?.name), value: Number(item?.value) || 0 }))
            .filter((item) => item.value > 0);
        if (!items.length) {
            target.innerHTML = '<div class="empty-state">暂无有效偏好数据</div>';
            return;
        }
        const colors = ["#b8923a", "#5a8a6a", "#c4956a", "#8b6d50", "#7c8fa8", "#9a789a"];
        const total = items.reduce((sum, item) => sum + item.value, 0);
        const radius = 110;
        const circumference = 2 * Math.PI * radius;
        let offset = 0;
        const segments = items.map((item, index) => {
            const length = circumference * item.value / total;
            const segment = `<circle cx="145" cy="200" r="${radius}" fill="none" stroke="${colors[index % colors.length]}" stroke-width="48" stroke-dasharray="${length} ${circumference - length}" stroke-dashoffset="${-offset}" />`;
            offset += length;
            return segment;
        }).join("");
        const legend = items.map((item, index) => `
            <g transform="translate(305, ${48 + index * 28})">
                <rect width="10" height="10" rx="3" fill="${colors[index % colors.length]}" />
                <text x="18" y="10" fill="#6f665c" font-size="12">${escapeHtml(item.name)} · ${item.value}</text>
            </g>
        `).join("");
        target.innerHTML = `<svg class="analysis-fallback-svg analysis-pie-svg" viewBox="0 0 520 400" role="img" aria-label="偏好分布饼图">
            <title>偏好分布</title>
            <g transform="rotate(-90 145 200)">${segments}</g>
            <circle cx="145" cy="200" r="78" fill="#fffdf9" />
            ${legend}
        </svg>`;
    }

    function renderAnalysisFallbackBarChart(targetId, values, color) {
        const target = document.getElementById(targetId);
        if (!target) return;
        const items = safeArray(values)
            .map((item) => ({ name: analysisLabel(item?.name), value: Number(item?.value) || 0 }))
            .slice(0, 8);
        if (!items.some((item) => item.value > 0)) {
            target.innerHTML = '<div class="empty-state">暂无有效分布数据</div>';
            return;
        }
        const max = Math.max(...items.map((item) => item.value), 1);
        const chartX = 50;
        const chartY = 30;
        const chartW = 440;
        const chartH = 290;
        const slot = chartW / items.length;
        const barW = Math.min(36, slot * 0.56);
        const grid = [0, 1, 2, 3, 4].map((step) => {
            const y = chartY + chartH - (step / 4) * chartH;
            const value = Math.round(max * step / 4);
            return `<line x1="${chartX}" y1="${y}" x2="${chartX + chartW}" y2="${y}" stroke="rgba(184,146,58,0.14)" />
                <text x="${chartX - 10}" y="${y + 4}" text-anchor="end" fill="#8c8174" font-size="11">${value}</text>`;
        }).join("");
        const bars = items.map((item, index) => {
            const height = Math.max(2, item.value / max * chartH);
            const x = chartX + slot * index + (slot - barW) / 2;
            const y = chartY + chartH - height;
            const labelX = chartX + slot * index + slot / 2;
            return `<rect x="${x}" y="${y}" width="${barW}" height="${height}" rx="6" fill="${color}" />
                <text x="${labelX}" y="${y - 7}" text-anchor="middle" fill="#8c8174" font-size="11">${item.value}</text>
                <text x="${labelX}" y="${chartY + chartH + 28}" text-anchor="middle" fill="#8c8174" font-size="12">${escapeHtml(item.name)}</text>`;
        }).join("");
        target.innerHTML = `<svg class="analysis-fallback-svg analysis-axis-svg" viewBox="0 0 520 400" role="img" aria-label="分布柱状图">
            <title>分布柱状图</title>
            ${grid}${bars}
        </svg>`;
    }

    function renderAnalysisFallbackLineChart(targetId, values) {
        const target = document.getElementById(targetId);
        if (!target) return;
        const items = safeArray(values)
            .map((item) => ({ name: analysisDateLabel(item?.name), value: Number(item?.value) || 0 }))
            .slice(0, 30);
        if (!items.some((item) => item.value > 0)) {
            target.innerHTML = '<div class="empty-state">暂无有效趋势数据</div>';
            return;
        }
        const max = Math.max(...items.map((item) => item.value), 1);
        const chartX = 50;
        const chartY = 30;
        const chartW = 440;
        const chartH = 290;
        const stepX = items.length === 1 ? chartW / 2 : chartW / (items.length - 1);
        const grid = [0, 1, 2, 3, 4].map((step) => {
            const y = chartY + chartH - (step / 4) * chartH;
            const value = Math.round(max * step / 4);
            return `<line x1="${chartX}" y1="${y}" x2="${chartX + chartW}" y2="${y}" stroke="rgba(184,146,58,0.14)" />
                <text x="${chartX - 10}" y="${y + 4}" text-anchor="end" fill="#8c8174" font-size="11">${value}</text>`;
        }).join("");
        const points = items.map((item, index) => {
            const x = items.length === 1 ? chartX + chartW / 2 : chartX + stepX * index;
            const y = chartY + chartH - item.value / max * chartH;
            return { x, y, item };
        });
        const pointString = points.map((point) => `${point.x},${point.y}`).join(" ");
        const labelStep = Math.max(1, Math.ceil(points.length / 8));
        const marks = points.map((point, index) => `
            <circle cx="${point.x}" cy="${point.y}" r="4" fill="#5a8a6a" />
            ${index % labelStep === 0 || index === points.length - 1
                ? `<text x="${point.x}" y="${chartY + chartH + 28}" text-anchor="middle" fill="#8c8174" font-size="12">${escapeHtml(point.item.name)}</text>`
                : ""}
        `).join("");
        target.innerHTML = `<svg class="analysis-fallback-svg analysis-axis-svg" viewBox="0 0 520 400" role="img" aria-label="对话量趋势折线图">
            <title>对话量趋势</title>
            ${grid}
            <polyline points="${pointString}" fill="none" stroke="#5a8a6a" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" />
            ${marks}
        </svg>`;
    }

    function renderConversationAnalysisFallbackCharts(interest, emotions, satisfaction, trend) {
        renderAnalysisFallbackPieChart("conversation-analysis-interest-chart", interest);
        renderAnalysisFallbackBarChart("conversation-analysis-emotion-chart", emotions, "#b8923a");
        renderAnalysisFallbackBarChart("conversation-analysis-satisfaction-chart", satisfaction, "#c4956a");
        renderAnalysisFallbackLineChart("conversation-analysis-trend-chart", trend);
    }

    function renderConversationAnalysisCharts(metrics) {
        const interest = safeArray(metrics?.interestDistribution);
        const emotions = safeArray(metrics?.emotionDistribution);
        const satisfaction = safeArray(metrics?.satisfactionDistribution);
        const trend = safeArray(metrics?.dailyTrend);
        const analysisView = document.getElementById("conversation-analysis-view");
        const report = document.getElementById("conversation-analysis-report");
        const canUseECharts = typeof echarts !== "undefined"
            && analysisView && !analysisView.hidden
            && report && !report.hidden;

        if (!canUseECharts) {
            renderConversationAnalysisFallbackCharts(interest, emotions, satisfaction, trend);
            return;
        }

        try {
            const chartsReady = initConversationAnalysisCharts();
            if (!chartsReady) {
                disposeConversationAnalysisCharts();
                renderConversationAnalysisFallbackCharts(interest, emotions, satisfaction, trend);
                return;
            }
            renderAnalysisPieChart(echartsInstances.analysisInterest, interest);
            renderAnalysisBarChart(echartsInstances.analysisEmotion, emotions, "#b8923a", "暂无情绪数据");
            renderAnalysisBarChart(echartsInstances.analysisSatisfaction, satisfaction, "#c4956a", "暂无评分数据");
            renderAnalysisTrendChart(echartsInstances.analysisTrend, trend);
        } catch (error) {
            console.warn("conversation analysis chart render failed", error);
            disposeConversationAnalysisCharts();
            renderConversationAnalysisFallbackCharts(interest, emotions, satisfaction, trend);
        }
    }

    function analysisDateLabel(value) {
        const text = String(value ?? "-").trim();
        const match = text.match(/(?:\d{4}[-/年])(\d{1,2})[-/月](\d{1,2})/);
        if (match) {
            return `${match[1].padStart(2, "0")}-${match[2].padStart(2, "0")}`;
        }
        return text
            .replace(/^\d{4}[-/]?/, "")
            .replace(/年/g, "")
            .replace(/月/g, "-")
            .replace(/日/g, "");
    }

    function renderConversationAnalysisKeywordCloud(values) {
        const target = document.getElementById("conversation-analysis-keyword-cloud");
        if (!target) return;
        const items = safeArray(values)
            .slice()
            .sort((a, b) => (Number(b?.value) || 0) - (Number(a?.value) || 0))
            .slice(0, 30);
        if (!items.length) {
            target.innerHTML = '<div class="empty-state">暂无足够的游客问题关键词</div>';
            return;
        }
        const colors = ["#b8923a", "#5a8a6a", "#a56855", "#7c8fa8", "#8b6d50", "#93749d"];
        const max = Math.max(...items.map((item) => Number(item?.value) || 0), 1);
        target.innerHTML = items.map((item, index) => {
            const value = Number(item?.value) || 0;
            const fontSize = 14 + Math.round((value / max) * 30);
            const rotation = index % 5 === 0 ? -6 : index % 4 === 0 ? 5 : 0;
            const name = String(item?.name ?? "").trim() || "未标记";
            return `<span class="analysis-keyword" style="font-size:${fontSize}px;color:${colors[index % colors.length]};transform:rotate(${rotation}deg)" title="${escapeHtml(name)}：${escapeHtml(value)} 次">${escapeHtml(name)}</span>`;
        }).join("");
    }

    function renderMetrics(data) {
        const metrics = document.getElementById("metrics");
        const cards = [
            ["今日访客", data.todayVisitors || 0],
            ["近7日访客", data.weekVisitors || 0],
            ["平均满意度", data.avgSatisfaction || "-"],
            ["累计对话", data.totalChats || 0],
            ["知识条数", data.knowledgeCount || 0],
            ["路线数量", data.routeCount || 0],
        ];

        metrics.innerHTML = cards.map(([label, value]) => `
            <div class="metric">
                <span>${escapeHtml(label)}</span>
                <strong>${escapeHtml(value)}</strong>
            </div>
        `).join("");

        // ECharts 趋势图
        try {
            const trend = safeArray(data.trend);
            if (echartsInstances.trend) {
                if (trend.length) {
                    echartsInstances.trend.setOption({
                    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
                    grid: { left: '3%', right: '4%', bottom: '3%', top: '8%', containLabel: true },
                    xAxis: { type: 'category', data: trend.map(i => i.day), axisLabel: { color: '#a89b8c', fontSize: 11 }, axisLine: { lineStyle: { color: 'rgba(212,175,120,0.15)' } } },
                    yAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(212,175,120,0.08)' } }, axisLabel: { color: '#a89b8c', fontSize: 11 } },
                    series: [{
                        type: 'bar',
                        data: trend.map(i => i.count),
                        itemStyle: {
                            borderRadius: [6, 6, 0, 0],
                            color: {
                                type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
                                colorStops: [
                                    { offset: 0, color: '#d4af6a' },
                                    { offset: 1, color: '#c4956a' },
                                ],
                            },
                        },
                        barMaxWidth: 36,
                    }],
                    }, true);
                } else {
                    echartsInstances.trend.clear();
                }
            } else {
                // Fallback for trend
                if (!trend.length) {
                    renderEmpty("trend-chart", "暂无趋势数据");
                } else {
                    const max = Math.max(...trend.map((item) => item.count || 0), 1);
                    document.getElementById("trend-chart").innerHTML = trend.map((item) => `
                    <div class="bar">
                        <strong>${escapeHtml(item.count || 0)}</strong>
                        <span style="height:${Math.max(12, ((item.count || 0) / max) * 144)}px"></span>
                        <small>${escapeHtml(item.day || "-")}</small>
                    </div>
                `).join("");
                }
            }
        } catch (error) {
            console.warn("dashboard trend chart failed", error);
            discardDashboardChart("trend");
            renderEmpty("trend-chart", "趋势图暂时不可用");
        }

        const hotQuestions = safeArray(data.hotQuestions);
        if (!hotQuestions.length) {
            renderEmpty("hot-list", "暂无高频问题");
        } else {
            document.getElementById("hot-list").innerHTML = hotQuestions.map((item) => `
                <div class="stack-item">
                    <span>${escapeHtml(item.question || "-")}</span>
                    <strong>${escapeHtml(item.count || 0)}</strong>
                </div>
            `).join("");
        }

        const sentiment = data.sentiment || {};
        const sentimentTotal = (sentiment.positive || 0) + (sentiment.neutral || 0) + (sentiment.negative || 0);
        document.getElementById("sentiment").innerHTML = `
            <div class="sentiment-item"><span>正向</span><strong>${escapeHtml(sentiment.positive || 0)}</strong></div>
            <div class="sentiment-item"><span>中性</span><strong>${escapeHtml(sentiment.neutral || 0)}</strong></div>
            <div class="sentiment-item"><span>待安抚</span><strong>${escapeHtml(sentiment.negative || 0)}</strong></div>
        `;

        // ECharts 情绪分布饼图
        try {
            if (echartsInstances.sentiment && sentimentTotal > 0) {
                echartsInstances.sentiment.setOption({
                tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
                series: [{
                    type: 'pie',
                    radius: ['40%', '70%'],
                    center: ['50%', '55%'],
                    avoidLabelOverlap: false,
                    itemStyle: { borderRadius: 6, borderColor: '#252018', borderWidth: 2 },
                    label: { show: true, formatter: '{b}\n{d}%', color: '#a89b8c', fontSize: 11 },
                    emphasis: { label: { show: true, fontSize: 14, fontWeight: 'bold' } },
                    data: [
                        { value: sentiment.positive || 0, name: '正向', itemStyle: { color: '#4a7c6f' } },
                        { value: sentiment.neutral || 0, name: '中性', itemStyle: { color: '#c4956a' } },
                        { value: sentiment.negative || 0, name: '待安抚', itemStyle: { color: '#b96857' } },
                    ],
                }],
                }, true);
            } else if (echartsInstances.sentiment) {
                echartsInstances.sentiment.clear();
            }
        } catch (error) {
            console.warn("dashboard sentiment chart failed", error);
            discardDashboardChart("sentiment");
            renderEmpty("sentiment-chart", "情绪图暂时不可用");
        }

        const topicFocus = safeArray(data.topicFocus);
        // ECharts 主题关注柱状图
        try {
            if (echartsInstances.topic && topicFocus.length) {
                echartsInstances.topic.setOption({
                tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
                grid: { left: '3%', right: '10%', bottom: '3%', top: '8%', containLabel: true },
                xAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(212,175,120,0.08)' } }, axisLabel: { color: '#a89b8c', fontSize: 11 } },
                yAxis: { type: 'category', data: topicFocus.map(i => i.name).reverse(), axisLabel: { color: '#a89b8c', fontSize: 11 }, axisLine: { lineStyle: { color: 'rgba(212,175,120,0.15)' } } },
                series: [{
                    type: 'bar',
                    data: topicFocus.map(i => i.value).reverse(),
                    itemStyle: {
                        borderRadius: [0, 6, 6, 0],
                        color: {
                            type: 'linear', x: 0, y: 0, x2: 1, y2: 0,
                            colorStops: [
                                { offset: 0, color: '#c4956a' },
                                { offset: 1, color: '#d4af6a' },
                            ],
                        },
                    },
                    barMaxWidth: 24,
                    label: { show: true, position: 'right', color: '#a89b8c', fontSize: 11 },
                }],
                }, true);
            } else if (echartsInstances.topic) {
                echartsInstances.topic.clear();
            }
        } catch (error) {
            console.warn("dashboard topic chart failed", error);
            discardDashboardChart("topic");
            renderEmpty("topic-chart", "主题图暂时不可用");
        }

        if (!topicFocus.length) {
            renderEmpty("tags", "暂无主题标签");
        } else {
            document.getElementById("tags").innerHTML = topicFocus.map((item) => `
                <span class="tag">${escapeHtml(item.name || "未命名")} · ${escapeHtml(item.value || 0)}</span>
            `).join("");
        }
    }

    function renderReport(report) {
        state.report = report || null;
        const summary = report?.summary || {};
        const topicFocus = safeArray(report?.topicFocus);
        const suggestions = safeArray(report?.suggestions);

        document.getElementById("report-summary").innerHTML = `
            <div class="summary-item"><span>统计周期</span><strong>${escapeHtml(report?.period || "-")}</strong></div>
            <div class="summary-item"><span>近 7 日对话</span><strong>${escapeHtml(summary.totalConversations || 0)}</strong></div>
            <div class="summary-item"><span>平均满意度</span><strong>${escapeHtml(summary.avgSatisfaction || "-")}</strong></div>
            <div class="summary-item"><span>高峰时段</span><strong>${escapeHtml(summary.servicePeak || "-")}</strong></div>
            <div class="summary-item"><span>响应目标</span><strong>${escapeHtml(summary.responseTarget || "-")}</strong></div>
            <div class="summary-item"><span>高关注主题</span><strong>${escapeHtml(topicFocus.map((item) => item.name).join("、") || "暂无")}</strong></div>
        `;

        if (!suggestions.length) {
            renderEmpty("report-suggestions", "暂无运营建议");
        } else {
            document.getElementById("report-suggestions").innerHTML = suggestions.map((item) => `
                <div class="stack-item">
                    <span>${escapeHtml(item)}</span>
                </div>
            `).join("");
        }
    }

    function renderKnowledge(items, total = null, keyword = "") {
        state.knowledge = safeArray(items);
        const tbody = document.getElementById("knowledge-tbody");
        const meta = document.getElementById("knowledge-meta");

        meta.textContent = keyword
            ? `当前关键字“${keyword}”，共 ${total ?? state.knowledge.length} 条结果`
            : `共 ${total ?? state.knowledge.length} 条知识内容`;

        if (!state.knowledge.length) {
            tbody.innerHTML = `<tr><td colspan="6"><div class="empty-state">还没有知识内容，先新增一条吧。</div></td></tr>`;
            return;
        }

        tbody.innerHTML = state.knowledge.map((item) => `
            <tr>
                <td>${escapeHtml(item.title || "-")}</td>
                <td>${escapeHtml(item.category || "-")}</td>
                <td>${safeArray(item.tags).map((tag) => `<span class="pill">${escapeHtml(tag)}</span>`).join("")}</td>
                <td>${escapeHtml(item.source || "-")}</td>
                <td>${escapeHtml(item.updated_at || "-")}</td>
                <td>
                    <div class="action-group">
                        <button class="btn" data-action="edit-knowledge" data-id="${escapeHtml(item.id)}" type="button">编辑</button>
                        <button class="btn btn-danger" data-action="delete-knowledge" data-id="${escapeHtml(item.id)}" type="button">删除</button>
                    </div>
                </td>
            </tr>
        `).join("");
    }

    function renderFaq(items) {
        state.faq = safeArray(items);
        const tbody = document.getElementById("faq-tbody");
        const meta = document.getElementById("faq-meta");
        meta.textContent = `共 ${state.faq.length} 条高频问题`;

        if (!state.faq.length) {
            tbody.innerHTML = `<tr><td colspan="5"><div class="empty-state">高频问题还没有内容，先补充常见问题吧。</div></td></tr>`;
            return;
        }

        tbody.innerHTML = state.faq.map((item) => `
            <tr>
                <td>${escapeHtml(item.question || "-")}</td>
                <td>${escapeHtml(item.category || "-")}</td>
                <td>${escapeHtml(item.usage_count || 0)}</td>
                <td>${escapeHtml((item.answer || "-").slice(0, 80))}${(item.answer || "").length > 80 ? "..." : ""}</td>
                <td>
                    <div class="action-group">
                        <button class="btn" data-action="edit-faq" data-id="${escapeHtml(item.id)}" type="button">编辑</button>
                        <button class="btn btn-danger" data-action="delete-faq" data-id="${escapeHtml(item.id)}" type="button">删除</button>
                    </div>
                </td>
            </tr>
        `).join("");
    }

    function renderProfileSelector(profiles, activeId) {
        const list = document.getElementById("profile-list");
        const profileList = safeArray(profiles);

        if (!profileList.length) {
            list.innerHTML = `<div class="empty-state">暂无角色配置</div>`;
            return;
        }

        const updateSummary = (profile) => {
            document.getElementById("profile-name").textContent = profile?.name || profile?.id || "未命名角色";
            document.getElementById("profile-desc").textContent = `${profile?.style || "未设置风格"} · ${profile?.voice || "未设置音色"}`;
            document.getElementById("profile-detail").textContent = `${profile?.outfit || "未设置服装"} · ${profile?.expressionBias || "neutral"} · ${profile?.id || "-"}`;
        };

        list.innerHTML = profileList.map((profile) => `
            <div class="profile-item ${profile.id === activeId ? "active" : ""}" data-id="${escapeHtml(profile.id)}">
                <strong>${escapeHtml(profile.name || profile.id)}</strong>
                <span>${escapeHtml(profile.style || "未设置风格")}</span>
            </div>
        `).join("");

        updateSummary(profileList.find((item) => item.id === activeId) || profileList[0]);

        list.querySelectorAll(".profile-item").forEach((item) => {
            item.addEventListener("click", () => {
                list.querySelectorAll(".profile-item").forEach((node) => node.classList.remove("active"));
                item.classList.add("active");
                document.getElementById("avatar-active").value = item.dataset.id;
                updateSummary(profileList.find((entry) => entry.id === item.dataset.id));
            });
        });
    }

    function renderConversations(resp) {
        const items = safeArray(resp?.list);
        state.conversations = items;
        state.convTotal = resp?.total ?? 0;
        renderConversationAnalysisScope();
        const totalPages = Math.max(1, Math.ceil(state.convTotal / state.convPageSize));
        const list = document.getElementById("conversation-list");
        const hasFilters = Boolean(
            state.convPeriod || state.convEmotion || state.convInterest || state.convSatisfaction
        );

        if (!items.length) {
            list.innerHTML = `<div class="empty-state">${hasFilters ? "未找到符合条件的记录" : "暂无对话记录"}</div>`;
            document.getElementById("conversation-pagination").innerHTML = "";
            return;
        }

        list.innerHTML = items.map((item) => `
            <div class="conversation-item">
                <strong>游客：${escapeHtml(item.message || "-")}</strong>
                <p>数字人：${escapeHtml(item.reply || "-")}</p>
                <div class="conversation-meta">
                    <span>${escapeHtml(item.timestamp || "-")}</span>
                    <span>偏好：${escapeHtml(item.interest || "-")}</span>
                    <span>情绪：${escapeHtml(item.emotion || "-")}</span>
                    <span>评分：${escapeHtml(item.satisfaction ?? "未评")}</span>
                </div>
            </div>
        `).join("");

        const pag = document.getElementById("conversation-pagination");
        if (totalPages <= 1) {
            pag.innerHTML = `<span class="pagination-info">共 ${state.convTotal} 条记录</span>`;
            return;
        }
        let html = `<span class="pagination-info">共 ${state.convTotal} 条记录</span><div class="pagination-controls">`;
        html += `<button class="btn btn-sm" data-page="${state.convPage - 1}" ${state.convPage <= 1 ? "disabled" : ""}>上一页</button>`;
        for (let p = 1; p <= totalPages; p++) {
            if (p === state.convPage) {
                html += `<span class="pagination-current">${p}</span>`;
            } else if (p === 1 || p === totalPages || Math.abs(p - state.convPage) <= 2) {
                html += `<button class="btn btn-sm" data-page="${p}">${p}</button>`;
            } else if (Math.abs(p - state.convPage) === 3) {
                html += `<span class="pagination-ellipsis">…</span>`;
            }
        }
        html += `<button class="btn btn-sm" data-page="${state.convPage + 1}" ${state.convPage >= totalPages ? "disabled" : ""}>下一页</button>`;
        html += `</div>`;
        pag.innerHTML = html;

        pag.querySelectorAll("[data-page]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const page = parseInt(btn.dataset.page, 10);
                if (page >= 1 && page <= totalPages) {
                    state.convPage = page;
                    loadConversations();
                }
            });
        });
    }

    const analysisLabels = {
        warm: "亲和讲解",
        delighted: "积极回应",
        focused: "高效导览",
        caring: "安抚陪伴",
        neutral: "正常讲解",
        joy: "积极情绪",
        trust: "信任",
        anticipation: "期待推荐",
        history: "历史文化",
        nature: "自然风光",
        family: "亲子互动",
        relax: "舒缓漫游",
        "未评分": "未评分",
        "未标记": "未标记",
    };

    function getConversationFilters() {
        return {
            period: state.convPeriod,
            emotion: state.convEmotion,
            interest: state.convInterest,
            satisfaction: state.convSatisfaction,
        };
    }

    function renderConversationAnalysisScope() {
        const target = document.getElementById("conversation-analysis-scope");
        if (!target) return;
        const filters = getConversationFilters();
        const labels = [];
        if (filters.period) labels.push({ day: "近一天", week: "近一周", month: "近一个月" }[filters.period] || filters.period);
        if (filters.emotion) labels.push(analysisLabels[filters.emotion] || filters.emotion);
        if (filters.interest) labels.push(analysisLabels[filters.interest] || filters.interest);
        if (filters.satisfaction) labels.push(`${filters.satisfaction} 分`);
        const scope = labels.length ? labels.join(" · ") : "全部对话";
        target.textContent = `当前筛选范围：${scope} · 匹配 ${state.convTotal} 条`;
    }

    function markConversationAnalysisStale() {
        state.conversationAnalysisRequestId += 1;
        const wasLoading = state.conversationAnalysisLoading;
        state.conversationAnalysisLoading = false;
        const report = document.getElementById("conversation-analysis-report");
        const status = document.getElementById("conversation-analysis-status");
        if (state.conversationAnalysis) {
            state.conversationAnalysisStale = true;
            if (report) report.hidden = true;
        }
        if (status && (state.conversationAnalysis || wasLoading)) {
            status.className = "conversation-analysis-status is-warning";
            status.textContent = "筛选条件已变化，请重新运行分析";
        }
    }

    function switchConversationView(view) {
        state.conversationView = view;
        const isAnalysis = view === "analysis";
        const listView = document.getElementById("conversation-list-view");
        const analysisView = document.getElementById("conversation-analysis-view");
        const listButton = document.getElementById("btn-show-conversation-list");
        const analysisButton = document.getElementById("btn-show-conversation-analysis");
        if (listView) listView.hidden = isAnalysis;
        if (analysisView) analysisView.hidden = !isAnalysis;
        if (listButton) {
            listButton.classList.toggle("active", !isAnalysis);
            listButton.setAttribute("aria-selected", String(!isAnalysis));
        }
        if (analysisButton) {
            analysisButton.classList.toggle("active", isAnalysis);
            analysisButton.setAttribute("aria-selected", String(isAnalysis));
        }
        if (isAnalysis) {
            if (state.conversationAnalysis && !state.conversationAnalysisStale) {
                renderConversationAnalysisCharts(state.conversationAnalysis.metrics || {});
                renderConversationAnalysisKeywordCloud(state.conversationAnalysis.metrics?.keywordDistribution);
            }
        }
        renderConversationAnalysisScope();
    }

    function analysisLabel(value) {
        const text = String(value ?? "");
        return analysisLabels[text] || text || "未标记";
    }

    function renderAnalysisMetrics(metrics, scope) {
        const target = document.getElementById("conversation-analysis-metrics");
        if (!target) return;
        const cards = [
            ["匹配对话", scope?.totalConversations ?? 0],
            ["已评分", metrics?.ratedConversations ?? 0],
            ["平均满意度", metrics?.avgSatisfaction ?? "暂无"],
            ["正向情绪", metrics?.positiveEmotionRate == null ? "暂无" : `${metrics.positiveEmotionRate}%`],
            ["评分覆盖率", metrics?.ratingCoverage == null ? "暂无" : `${metrics.ratingCoverage}%`],
        ];
        target.innerHTML = cards.map(([label, value]) => `
            <div class="metric analysis-metric">
                <span>${escapeHtml(label)}</span>
                <strong>${escapeHtml(value)}</strong>
            </div>
        `).join("");
    }

    function renderAnalysisBars(targetId, values) {
        const target = document.getElementById(targetId);
        if (!target) return;
        const items = safeArray(values);
        if (!items.length) {
            target.innerHTML = '<div class="empty-state">暂无足够数据</div>';
            return;
        }
        target.innerHTML = items.slice(0, 8).map((item) => {
            const value = Number(item?.value) || 0;
            const percentage = Math.max(0, Math.min(100, Number(item?.percentage) || 0));
            return `
                <div class="analysis-bar">
                    <div class="analysis-bar-head">
                        <span>${escapeHtml(analysisLabel(item?.name))}</span>
                        <strong>${escapeHtml(value)} <small>(${escapeHtml(percentage)}%)</small></strong>
                    </div>
                    <div class="analysis-bar-track"><span style="width:${percentage}%"></span></div>
                </div>
            `;
        }).join("");
    }

    function renderAnalysisItems(targetId, items, emptyText, renderItem) {
        const target = document.getElementById(targetId);
        if (!target) return;
        const safeItems = safeArray(items);
        target.innerHTML = safeItems.length
            ? safeItems.map(renderItem).join("")
            : `<div class="empty-state">${escapeHtml(emptyText)}</div>`;
    }

    function severityLabel(value) {
        return { high: "高", medium: "中", low: "低" }[value] || "中";
    }

    function renderConversationAnalysis(report) {
        state.conversationAnalysis = report || null;
        state.conversationAnalysisStale = false;
        const reportEl = document.getElementById("conversation-analysis-report");
        const status = document.getElementById("conversation-analysis-status");
        if (!reportEl || !status) return;
        if (!report) {
            reportEl.hidden = true;
            status.className = "conversation-analysis-status";
            status.textContent = "选择筛选条件后运行分析，报告会展示在这里。";
            return;
        }

        reportEl.hidden = false;
        const meta = report.meta || {};
        const scope = report.scope || {};
        const modeText = meta.mode === "ai" ? "AI 分析已完成" : "基础统计报告已生成";
        const sampleText = scope.sampledConversations == null ? "" : ` · 抽样 ${scope.sampledConversations} 条`;
        status.className = `conversation-analysis-status ${meta.mode === "ai" ? "is-success" : "is-warning"}`;
        status.textContent = `${modeText}${sampleText}${meta.generatedAt ? ` · ${meta.generatedAt}` : ""}`;

        const metrics = report.metrics || {};
        renderAnalysisMetrics(metrics, scope);
        renderConversationAnalysisCharts(metrics);
        renderConversationAnalysisKeywordCloud(metrics.keywordDistribution);
        document.getElementById("conversation-analysis-summary").innerHTML = `<p>${escapeHtml(report.executiveSummary || "暂无结论")}</p>`;

        renderAnalysisItems("conversation-analysis-findings", report.findings, "暂无明显发现", (item) => `
            <div class="analysis-item">
                <div class="analysis-item-head"><strong>${escapeHtml(item.title)}</strong><span class="analysis-pill analysis-${escapeHtml(item.severity || "medium")}">${escapeHtml(severityLabel(item.severity))}</span></div>
                <p>${escapeHtml(item.detail)}</p>
            </div>
        `);
        renderAnalysisItems("conversation-analysis-gaps", report.knowledgeGaps, "暂无明确知识盲区", (item) => `
            <div class="analysis-item">
                <div class="analysis-item-head"><strong>${escapeHtml(item.title)}</strong><span class="analysis-pill analysis-${escapeHtml(item.severity || "medium")}">${escapeHtml(severityLabel(item.severity))}</span></div>
                <p>${escapeHtml(item.detail)}</p><small>建议：${escapeHtml(item.action)}</small>
            </div>
        `);
        renderAnalysisItems("conversation-analysis-suggestions", report.suggestions, "暂无改进建议", (item) => `
            <div class="analysis-item">
                <div class="analysis-item-head"><strong>${escapeHtml(item.title)}</strong><span class="analysis-pill analysis-${escapeHtml(item.priority || "medium")}">${escapeHtml(severityLabel(item.priority))}</span></div>
                <p>${escapeHtml(item.action)}</p><small>预期影响：${escapeHtml(item.impact)}</small>
            </div>
        `);
        renderAnalysisItems("conversation-analysis-cases", report.cases, "暂无典型案例", (item) => `
            <article class="analysis-case">
                <span class="analysis-case-type">${escapeHtml({ positive: "高质量", needs_attention: "待复盘", typical: "典型" }[item.type] || "案例")}</span>
                <p><strong>游客：</strong>${escapeHtml(item.message)}</p>
                <p><strong>数字人：</strong>${escapeHtml(item.reply)}</p>
                <small>${escapeHtml(item.insight)}</small>
            </article>
        `);
        renderAnalysisItems("conversation-analysis-limitations", report.limitations, "暂无额外说明", (item) => `
            <div class="analysis-item analysis-item-muted"><p>${escapeHtml(item)}</p></div>
        `);
    }

    async function loadConversationAnalysis() {
        const status = document.getElementById("conversation-analysis-status");
        const requestId = ++state.conversationAnalysisRequestId;
        const requestFilters = getConversationFilters();
        state.conversationAnalysisLoading = true;
        if (status) {
            status.className = "conversation-analysis-status is-loading";
            status.textContent = "正在读取筛选结果并生成分析报告，请稍候…";
        }
        try {
            const res = await api("/admin/conversations/analyze", {
                method: "POST",
                body: JSON.stringify({ filters: requestFilters, sampleLimit: 60 }),
            });
            if (requestId !== state.conversationAnalysisRequestId) {
                return { stale: true };
            }
            renderConversationAnalysis(res.data || null);
            return res;
        } catch (error) {
            if (requestId !== state.conversationAnalysisRequestId) {
                return { stale: true };
            }
            const report = document.getElementById("conversation-analysis-report");
            if (report) report.hidden = true;
            if (status) {
                status.className = "conversation-analysis-status is-error";
                status.textContent = error.message || "分析失败，请稍后重试";
            }
            throw error;
        } finally {
            if (requestId === state.conversationAnalysisRequestId) {
                state.conversationAnalysisLoading = false;
            }
        }
    }

    function openDialog(type, data = {}) {
        const dialog = document.getElementById("dialog");
        const title = document.getElementById("dialog-title");
        const body = document.getElementById("dialog-body");

        if (type === "knowledge") {
            title.textContent = data.id ? "编辑知识" : "新增知识";
            body.innerHTML = `
                <input class="input" name="title" type="text" value="${escapeHtml(data.title || "")}" placeholder="标题">
                <input class="input" name="category" type="text" value="${escapeHtml(data.category || "")}" placeholder="分类">
                <input class="input" name="source" type="text" value="${escapeHtml(data.source || "后台录入")}" placeholder="来源">
                <input class="input" name="tags" type="text" value="${escapeHtml(safeArray(data.tags).join(","))}" placeholder="标签，用逗号分隔">
                <textarea class="input textarea" name="content" rows="8" placeholder="请输入内容">${escapeHtml(data.content || "")}</textarea>
            `;
        }

        if (type === "faq") {
            title.textContent = data.id ? "编辑高频问题" : "新增高频问题";
            body.innerHTML = `
                <input class="input" name="question" type="text" value="${escapeHtml(data.question || "")}" placeholder="问题">
                <input class="input" name="category" type="text" value="${escapeHtml(data.category || "")}" placeholder="分类">
                <textarea class="input textarea" name="answer" rows="8" placeholder="请输入答案">${escapeHtml(data.answer || "")}</textarea>
            `;
        }

        dialog.dataset.type = type;
        dialog.dataset.id = data.id || "";
        dialog.showModal();
    }

    async function loadDashboard() {
        const [dashboardRes, reportRes] = await Promise.all([
            api("/admin/dashboard/overview"),
            api("/admin/report"),
        ]);
        initECharts();
        renderMetrics(dashboardRes.data || {});
        renderReport(reportRes.data || {});
        loadDeepReport();
    }

    async function loadDeepReport() {
        try {
            const res = await api("/admin/report/deep");
            const data = res.data || {};
            renderHeatmap(safeArray(data.spotHeatmap));
            renderHourly(safeArray(data.hourlyDistribution));
            renderProfiles(data.visitorProfiles || {});
        } catch (e) {
            console.warn("deep report not available", e);
        }
    }

    function renderHeatmap(spots) {
        if (!echartsInstances.heat) return;
        try {
            const visibleSpots = spots.filter((item) => (item.mentions || 0) > 0);
            if (!visibleSpots.length) {
                echartsInstances.heat.clear();
                echartsInstances.heat.setOption({ title: { text: "暂无景点提及数据", left: "center", top: "middle", textStyle: { color: "#a89b8c", fontSize: 12 } } }, true);
                return;
            }
            echartsInstances.heat.setOption({
            tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
            grid: { left: '3%', right: '10%', bottom: '3%', top: '8%', containLabel: true },
            xAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(212,175,120,0.08)' } }, axisLabel: { color: '#a89b8c', fontSize: 11 } },
            yAxis: { type: 'category', data: visibleSpots.map(i => i.spot).reverse(), axisLabel: { color: '#a89b8c', fontSize: 11 } },
            series: [{
                type: 'bar', data: visibleSpots.map(i => i.mentions || 0).reverse(), barMaxWidth: 24,
                itemStyle: {
                    borderRadius: [0, 6, 6, 0],
                    color: { type: 'linear', x: 0, y: 0, x2: 1, y2: 0, colorStops: [{ offset: 0, color: '#4a7c6f' }, { offset: 1, color: '#6b9e8a' }] },
                },
                label: { show: true, position: 'right', color: '#a89b8c', fontSize: 11, formatter: (p) => `${p.value}次` },
            }],
            }, true);
        } catch (error) {
            console.warn("dashboard heatmap chart failed", error);
            discardDashboardChart("heat");
            renderEmpty("heatmap-chart", "景点图暂时不可用");
        }
    }

    function renderHourly(hours) {
        if (!echartsInstances.hourly || !hours.length) return;
        try {
            echartsInstances.hourly.setOption({
            tooltip: { trigger: 'axis' },
            grid: { left: '3%', right: '4%', bottom: '3%', top: '8%', containLabel: true },
            xAxis: { type: 'category', data: hours.map(i => `${i.hour}:00`), axisLabel: { color: '#a89b8c', fontSize: 11 } },
            yAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(212,175,120,0.08)' } }, axisLabel: { color: '#a89b8c', fontSize: 11 } },
            series: [{
                type: 'line', data: hours.map(i => i.count), smooth: true, areaStyle: { opacity: 0.2 },
                lineStyle: { color: '#c4956a', width: 2 },
                itemStyle: { color: '#d4af6a' },
            }],
            }, true);
        } catch (error) {
            console.warn("dashboard hourly chart failed", error);
            discardDashboardChart("hourly");
            renderEmpty("hourly-chart", "时段图暂时不可用");
        }
    }

    function renderProfiles(profiles) {
        if (!echartsInstances.profile) return;
        const keys = Object.keys(profiles);
        if (!keys.length) return;
        try {
            const labelMap = { history: '历史文化', nature: '自然风光', family: '亲子互动', relax: '舒缓漫游' };
            const names = keys.map(k => labelMap[k] || k);
            const values = keys.map(k => profiles[k].count || 0);
            echartsInstances.profile.setOption({
            tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
            series: [{
                type: 'pie', radius: ['40%', '70%'], center: ['50%', '55%'],
                itemStyle: { borderRadius: 6, borderColor: '#252018', borderWidth: 2 },
                label: { show: true, formatter: '{b}\n{d}%', color: '#a89b8c', fontSize: 11 },
                data: names.map((n, i) => ({ value: values[i], name: n })),
            }],
            }, true);
        } catch (error) {
            console.warn("dashboard profile chart failed", error);
            discardDashboardChart("profile");
            renderEmpty("profile-chart", "游客画像图暂时不可用");
        }
    }

    async function loadKnowledge(search = "") {
        state.activeKnowledgeSearch = search;
        const query = search ? `?search=${encodeURIComponent(search)}&page=1&page_size=50` : "?page=1&page_size=50";
        const res = await api(`/admin/knowledge${query}`);
        renderKnowledge(res.data?.list || [], res.data?.total || 0, search);
    }

    async function loadFaq() {
        const res = await api("/admin/faq");
        renderFaq(res.data?.list || []);
    }

    // Avatar administration is intentionally limited to model availability.
    // The fixed guide-preset fields returned by the API are display-only.
    async function loadAvatar() {
        const modelsRes = await api("/admin/avatar/models");
        renderVrmModels(modelsRes.data || []);
    }

    function renderVrmModels(models) {
        const tbody = document.getElementById("vrm-tbody");
        const meta = document.getElementById("vrm-meta");
        if (!tbody || !meta) return;
        if (!models.length) {
            tbody.innerHTML = '<tr><td colspan="7"><div class="empty-state">暂无 VRM 模型</div></td></tr>';
            meta.textContent = "暂无模型";
            return;
        }
        meta.textContent = `共 ${models.length} 个模型；至少保留一个启用模型`;
        const expressionLabels = {
            warm: "温暖亲和",
            calm: "沉稳专业",
            delighted: "开朗活泼",
            caring: "关怀体贴",
            neutral: "自然平和",
        };
        tbody.innerHTML = models.map((model) => {
            const enabled = Boolean(model.enabled);
            const size = model.size > 1024 * 1024
                ? `${(model.size / 1024 / 1024).toFixed(1)} MB`
                : `${(model.size / 1024).toFixed(1)} KB`;
            const outfit = model.outfit || "未设置";
            const voice = model.voice || "未设置";
            const style = model.style || "未设置";
            const expression = expressionLabels[model.expressionBias] || model.expressionBias || "未设置";
            return `<tr class="${enabled ? "active-row" : ""}">
                <td>${escapeHtml(model.name)}</td>
                <td class="model-fixed-cell" title="${escapeHtml(outfit)}">${escapeHtml(outfit)}</td>
                <td class="model-fixed-cell" title="${escapeHtml(voice)}">${escapeHtml(voice)}</td>
                <td class="model-fixed-cell" title="${escapeHtml(style)}">${escapeHtml(style)}</td>
                <td class="model-fixed-cell" title="${escapeHtml(expression)}">${escapeHtml(expression)}</td>
                <td>${escapeHtml(size)}</td>
                <td class="model-action-cell"><span class="pill">${enabled ? "已启用" : "已禁用"}</span>
                    <button class="btn" type="button" data-action="toggle-vrm" data-model="${escapeHtml(model.name)}" data-enabled="${enabled ? "false" : "true"}" aria-label="${enabled ? "禁用" : "启用"} ${escapeHtml(model.name)}">
                        ${enabled ? "禁用" : "启用"}
                    </button>
                </td>
            </tr>`;
        }).join("");
    }

    async function loadConversations() {
        const list = document.getElementById("conversation-list");
        const requestId = ++state.conversationRequestId;
        list?.setAttribute("aria-busy", "true");
        const query = new URLSearchParams({
            page: state.convPage,
            page_size: state.convPageSize,
            period: state.convPeriod,
            emotion: state.convEmotion,
            interest: state.convInterest,
            satisfaction: state.convSatisfaction,
        }).toString();
        try {
            const res = await api(`/admin/conversations?${query}`);
            if (requestId !== state.conversationRequestId) {
                return { stale: true };
            }
            renderConversations(res.data);
            return res;
        } catch (error) {
            if (requestId !== state.conversationRequestId) {
                return { stale: true };
            }
            throw error;
        } finally {
            if (requestId === state.conversationRequestId) {
                list?.setAttribute("aria-busy", "false");
            }
        }
    }

    async function loadSettings() {
        const res = await api("/admin/settings");
        const data = res.data || {};
        document.getElementById("setting-model").value = data.aiModel || data._meta?.currentProvider || "";
        document.getElementById("setting-tts-voice").value = data.ttsVoice || "";
        document.getElementById("setting-knowledge").value = data.knowledgeMode || "";
        document.getElementById("setting-latency").value = data.responseTargetMs ?? "由实际链路测量，无固定目标";
        document.getElementById("setting-emotion").value = data.emotionEngine || "";
        document.getElementById("setting-asr").value = data.asrMode || "";
        document.getElementById("setting-admin-user").value = data.admin_username || data.adminUser || "";
        document.getElementById("setting-admin-password").value = "";
        document.getElementById("setting-tts").checked = data.ttsEnabled === true ||
            String(data.ttsEnabled).toLowerCase() === "true";
    }

    function renderLogs(resp) {
        const items = resp?.list || [];
        state.logTotal = resp?.total ?? 0;
        const totalPages = Math.max(1, Math.ceil(state.logTotal / state.logPageSize));
        const meta = document.getElementById("logs-meta");
        const tbody = document.getElementById("logs-tbody");

        meta.textContent = `共 ${state.logTotal} 条记录`;

        if (!items.length) {
            tbody.innerHTML = `<tr><td colspan="7"><div class="empty-state">暂无操作日志</div></td></tr>`;
            document.getElementById("logs-pagination").innerHTML = "";
            return;
        }

        const actionLabel = { create: "新增", update: "修改", delete: "删除", login: "登录", logout: "登出", export: "导出" };
        const statusLabel = { success: "成功", failure: "失败" };
        const statusClass = { success: "pill", failure: "pill pill-danger" };
        const resourceLabel = {
            knowledge: "知识库",
            faq: "高频问题",
            avatar: "数字人",
            settings: "系统设置",
            "guide-preset": "导游预设",
            "vrm-model": "VRM模型",
            auth: "认证",
            "operation-logs": "操作日志",
        };

        tbody.innerHTML = items.map((item) => `
            <tr>
                <td>${escapeHtml(item.timestamp || "-")}</td>
                <td>${escapeHtml(item.admin_user || "-")}</td>
                <td>${escapeHtml(actionLabel[item.action] || item.action)}</td>
                <td>${escapeHtml(resourceLabel[item.resource] || item.resource)}</td>
                <td>${escapeHtml((item.detail || "").slice(0, 60))}</td>
                <td>${escapeHtml(item.ip_address || "-")}</td>
                <td><span class="${statusClass[item.result] || 'pill'}">${escapeHtml(statusLabel[item.result] || item.result)}</span></td>
            </tr>
        `).join("");

        const pag = document.getElementById("logs-pagination");
        if (totalPages <= 1) {
            pag.innerHTML = `<span class="pagination-info">共 ${state.logTotal} 条记录</span>`;
            return;
        }
        let html = `<span class="pagination-info">共 ${state.logTotal} 条记录</span><div class="pagination-controls">`;
        html += `<button class="btn btn-sm" data-logpage="${state.logPage - 1}" ${state.logPage <= 1 ? "disabled" : ""}>上一页</button>`;
        for (let p = 1; p <= totalPages; p++) {
            if (p === state.logPage) {
                html += `<span class="pagination-current">${p}</span>`;
            } else if (p === 1 || p === totalPages || Math.abs(p - state.logPage) <= 2) {
                html += `<button class="btn btn-sm" data-logpage="${p}">${p}</button>`;
            } else if (Math.abs(p - state.logPage) === 3) {
                html += `<span class="pagination-ellipsis">…</span>`;
            }
        }
        html += `<button class="btn btn-sm" data-logpage="${state.logPage + 1}" ${state.logPage >= totalPages ? "disabled" : ""}>下一页</button>`;
        html += `</div>`;
        pag.innerHTML = html;

        pag.querySelectorAll("[data-logpage]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const page = parseInt(btn.dataset.logpage, 10);
                if (page >= 1 && page <= totalPages) {
                    state.logPage = page;
                    loadOperationLogs();
                }
            });
        });
    }

    async function loadOperationLogs() {
        const query = new URLSearchParams({
            page: state.logPage,
            page_size: state.logPageSize,
            action: state.logFilterAction,
            resource: state.logFilterResource,
        }).toString();
        const res = await api(`/admin/operation-logs?${query}`);
        renderLogs(res.data);
    }

    async function refreshAll() {
        await Promise.all([
            loadDashboard(),
            loadKnowledge(state.activeKnowledgeSearch),
            loadFaq(),
            loadAvatar(),
            loadConversations(),
            loadSettings(),
            loadOperationLogs(),
        ]);
    }

    function buildReportText() {
        const report = state.report;
        if (!report) return "暂无可复制的周报摘要。";

        const summary = report.summary || {};
        const topics = safeArray(report.topicFocus).map((item) => `${item.name}(${item.value})`).join("、") || "暂无";
        const suggestions = safeArray(report.suggestions).map((item, index) => `${index + 1}. ${item}`).join("\n") || "暂无";

        return [
            `统计周期：${report.period || "-"}`,
            `近 7 日对话：${summary.totalConversations || 0}`,
            `平均满意度：${summary.avgSatisfaction || "-"}`,
            `服务高峰：${summary.servicePeak || "-"}`,
            `响应目标：${summary.responseTarget || "-"}`,
            `高关注主题：${topics}`,
            "",
            "运营建议：",
            suggestions,
        ].join("\n");
    }

    function bindEvents() {
        document.getElementById("login-form")?.addEventListener("submit", (event) => {
            event.preventDefault();
            const errorEl = document.getElementById("login-error");
            const username = document.getElementById("login-username").value.trim();
            const password = document.getElementById("login-password").value;
            errorEl.textContent = "";
            runAction(async () => {
                await login(username, password);
                await refreshAll();
            }, "登录成功", "欢迎进入管理后台").catch((error) => {
                errorEl.textContent = error.message || "登录失败";
            });
        });

        document.getElementById("btn-logout")?.addEventListener("click", () => {
            runAction(logout, "已退出", "管理员登录状态已清除");
        });

        document.querySelectorAll(".nav-item").forEach((item) => {
            item.addEventListener("click", () => {
                switchPage(item.dataset.page);
            });
        });

        document.getElementById("sidebarToggle")?.addEventListener("click", () => {
            const shell = document.querySelector(".admin-shell");
            const toggle = document.getElementById("sidebarToggle");
            shell.classList.toggle("collapsed");
            toggle.title = shell.classList.contains("collapsed") ? "展开侧栏" : "收起侧栏";
        });

        document.getElementById("btn-refresh")?.addEventListener("click", () => {
            runAction(loadDashboard, "刷新成功", "总览数据已更新");
        });

        document.getElementById("btn-copy-report")?.addEventListener("click", async () => {
            try {
                await navigator.clipboard.writeText(buildReportText());
                showToast("复制成功", "周报摘要已复制到剪贴板");
            } catch (error) {
                showToast("复制失败", "当前环境不支持剪贴板写入", "error");
            }
        });

        document.getElementById("btn-export-report")?.addEventListener("click", (ev) => runAction(async () => {
            const response = await fetch("/api/v1/admin/export/report?format=json", {
                headers: { "X-ADMIN-TOKEN": state.adminToken },
            });
            if (!response.ok) throw new Error("导出失败");
            const blob = await response.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `report_${new Date().toISOString().slice(0, 10)}.json`;
            a.click();
            URL.revokeObjectURL(url);
        }, "导出成功", "运营报告 JSON 已下载", ev));

        document.getElementById("btn-add-knowledge")?.addEventListener("click", () => openDialog("knowledge"));
        document.getElementById("btn-add-faq")?.addEventListener("click", () => openDialog("faq"));

        document.getElementById("btn-search")?.addEventListener("click", () => {
            runAction(() => loadKnowledge(document.getElementById("search-knowledge").value.trim()));
        });

        document.getElementById("btn-reset-knowledge")?.addEventListener("click", () => {
            document.getElementById("search-knowledge").value = "";
            runAction(() => loadKnowledge(""), "已重置", "知识库搜索条件已清空");
        });

        document.getElementById("search-knowledge")?.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                runAction(() => loadKnowledge(event.target.value.trim()));
            }
        });

        document.getElementById("conversation-filters")?.addEventListener("submit", (event) => {
            event.preventDefault();
            state.convPeriod = document.getElementById("conversation-filter-period").value;
            state.convEmotion = document.getElementById("conversation-filter-emotion").value;
            state.convInterest = document.getElementById("conversation-filter-interest").value;
            state.convSatisfaction = document.getElementById("conversation-filter-satisfaction").value;
            state.convPage = 1;
            markConversationAnalysisStale();
            renderConversationAnalysisScope();
            runAction(loadConversations, "查询完成", "对话记录已按条件更新");
        });

        document.getElementById("btn-reset-conv-filters")?.addEventListener("click", () => {
            [
                "conversation-filter-period",
                "conversation-filter-emotion",
                "conversation-filter-interest",
                "conversation-filter-satisfaction",
            ].forEach((id) => {
                document.getElementById(id).value = "";
            });
            state.convPeriod = "";
            state.convEmotion = "";
            state.convInterest = "";
            state.convSatisfaction = "";
            state.convPage = 1;
            markConversationAnalysisStale();
            renderConversationAnalysisScope();
            runAction(loadConversations, "已重置", "对话记录筛选条件已清空");
        });

        document.getElementById("btn-refresh-convs")?.addEventListener("click", () => {
            state.convPage = 1;
            markConversationAnalysisStale();
            runAction(loadConversations, "刷新成功", "对话记录已更新");
        });

        document.getElementById("btn-show-conversation-list")?.addEventListener("click", () => {
            switchConversationView("list");
        });

        document.getElementById("btn-show-conversation-analysis")?.addEventListener("click", () => {
            switchConversationView("analysis");
        });

        document.getElementById("btn-run-conversation-analysis")?.addEventListener("click", (event) => {
            runAction(
                loadConversationAnalysis,
                "分析完成",
                "筛选结果分析报告已生成",
                event,
            );
        });

        document.getElementById("btn-refresh-avatar-models")?.addEventListener("click", () => {
            runAction(loadAvatar, "刷新成功", "模型列表已更新");
        });

        document.getElementById("btn-enable-all-vrm")?.addEventListener("click", (ev) => runAction(async () => {
            await api("/admin/avatar/models/status/batch", {
                method: "PUT",
                body: JSON.stringify({ enabled: true }),
            });
            await loadAvatar();
        }, "启用成功", "全部模型已启用", ev));

        document.getElementById("btn-save-settings")?.addEventListener("click", (ev) => runAction(async () => {
            const settingsPayload = {
                ttsVoice: document.getElementById("setting-tts-voice").value.trim(),
                adminUser: document.getElementById("setting-admin-user").value.trim(),
            };
            const nextPassword = document.getElementById("setting-admin-password").value;
            if (nextPassword.trim()) {
                settingsPayload.admin_password = nextPassword;
            }
            await api("/admin/settings", {
                method: "PUT",
                body: JSON.stringify(settingsPayload),
            });
            await loadSettings();
        }, "保存成功", "系统设置已更新", ev));

        document.getElementById("btn-search-logs")?.addEventListener("click", () => {
            state.logPage = 1;
            state.logFilterAction = document.getElementById("log-filter-action").value;
            state.logFilterResource = document.getElementById("log-filter-resource").value;
            runAction(loadOperationLogs, "查询完成", "操作日志已更新");
        });

        document.getElementById("btn-export-logs")?.addEventListener("click", (ev) => runAction(async () => {
            const action = document.getElementById("log-filter-action").value;
            const resource = document.getElementById("log-filter-resource").value;
            const query = new URLSearchParams({ action, resource }).toString();
            const response = await fetch(`/api/v1/admin/operation-logs/export?${query}`, {
                headers: { "X-ADMIN-TOKEN": state.adminToken },
            });
            if (!response.ok) throw new Error("导出失败");
            const blob = await response.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `operation_logs_${new Date().toISOString().slice(0, 10)}.csv`;
            a.click();
            URL.revokeObjectURL(url);
        }, "导出成功", "操作日志 CSV 已下载", ev));

        document.body.addEventListener("click", (event) => {
            const target = event.target.closest("[data-action]");
            if (!target) return;

            const { action, id } = target.dataset;

            if (action === "edit-knowledge") {
                openDialog("knowledge", state.knowledge.find((item) => String(item.id) === String(id)));
                return;
            }

            if (action === "delete-knowledge") {
                confirmDialog("确定删除这条知识内容吗？删除后不可恢复。", { title: "删除知识", confirmText: "删除", danger: true })
                    .then((ok) => {
                        if (!ok) return;
                        runAction(async () => {
                            await api(`/admin/knowledge/${id}`, { method: "DELETE" });
                            await loadKnowledge(state.activeKnowledgeSearch);
                        }, "删除成功", "知识内容已移除", target);
                    })
                    .catch((e) => console.error("delete-knowledge error:", e));
                return;
            }

            if (action === "edit-faq") {
                openDialog("faq", state.faq.find((item) => String(item.id) === String(id)));
                return;
            }

            if (action === "delete-faq") {
                confirmDialog("确定删除这条高频问题吗？删除后不可恢复。", { title: "删除高频问题", confirmText: "删除", danger: true })
                    .then((ok) => {
                        if (!ok) return;
                        runAction(async () => {
                            await api(`/admin/faq/${id}`, { method: "DELETE" });
                            await loadFaq();
                        }, "删除成功", "高频问题已移除", target);
                    })
                    .catch((e) => console.error("delete-faq error:", e));
                return;
            }

            if (action === "toggle-vrm") {
                const modelId = target.dataset.model;
                const enabled = target.dataset.enabled === "true";
                runAction(async () => {
                    await api("/admin/avatar/models/status", {
                        method: "PUT",
                        body: JSON.stringify({ modelId, enabled }),
                    });
                    await loadAvatar();
                }, enabled ? "启用成功" : "禁用成功", `${modelId} 已${enabled ? "启用" : "禁用"}`, target);
            }
        });

        document.getElementById("dialog-form")?.addEventListener("submit", (event) => {
            event.preventDefault();
            const dialog = document.getElementById("dialog");
            if (dialog.dataset.mode === "confirm") return; // 确认模式由 confirmDialog 自行处理
            const type = dialog.dataset.type;
            const id = dialog.dataset.id;
            const payload = {};

            new FormData(event.target).forEach((value, key) => {
                payload[key] = value;
            });

            if (type === "knowledge" && payload.tags) {
                payload.tags = String(payload.tags).split(",").map((item) => item.trim()).filter(Boolean);
            }

            const method = id ? "PUT" : "POST";
            const url = id ? `/admin/${type}/${id}` : `/admin/${type}`;

            runAction(async () => {
                await api(url, {
                    method,
                    body: JSON.stringify(payload),
                });
                dialog.close();
                if (type === "knowledge") {
                    await loadKnowledge(state.activeKnowledgeSearch);
                } else if (type === "faq") {
                    await loadFaq();
                }
            }, "保存成功", "内容已更新");
        });

        document.getElementById("btn-close")?.addEventListener("click", () => {
            document.getElementById("dialog").close();
        });

        document.getElementById("btn-cancel")?.addEventListener("click", () => {
            document.getElementById("dialog").close();
        });

        // 点击遮罩（弹窗内容区域之外）关闭弹窗。用坐标判断更可靠，
        // 因为 ::backdrop 的点击事件 target 仍是 dialog 本身。
        document.getElementById("dialog")?.addEventListener("click", (event) => {
            const dialog = event.currentTarget;
            if (dialog.dataset.mode === "confirm") return; // 确认弹窗只允许按钮操作
            const form = dialog.querySelector("form");
            const rect = (form || dialog).getBoundingClientRect();
            const inside = event.clientX >= rect.left && event.clientX <= rect.right
                && event.clientY >= rect.top && event.clientY <= rect.bottom;
            if (!inside) dialog.close();
        });
    }

    async function init() {
        bindEvents();
        setPageMeta("dashboard");
        switchPage("dashboard");
        setAuthView(false);

        try {
            const authenticated = await restoreAuth();
            if (!authenticated) {
                return;
            }
            await refreshAll();
            showToast("加载完成", "后台数据已同步");
        } catch (error) {
            showToast("初始化失败", error.message || "请刷新后重试", "error");
        }
    }

    init();
})();
