# 第十五届中国软件杯A5赛道国奖项目——AI数字人

基于大语言模型的智能数字人导览系统，为无锡灵山胜境景区提供AI驱动的3D虚拟导游服务。支持语音/文本多轮对话、情感感知表情驱动、RAG知识问答、路线推荐、天气查询、GPS导航、管理后台及数据运营大屏。

## 功能特性

- **智能对话导览** — 基于RAG（检索增强生成）的景区知识问答，支持多轮对话与流式输出（SSE），含per-session流式生成取消机制防止旧请求阻塞新请求
- **查询重写 + HyDE 增强检索** — 口语化查询自动重写为关键词句式（可选LLM驱动），结合假设文档嵌入（HyDE）提升向量检索召回率，512条LRU缓存
- **事实验证（Grounding Check）** — LLM回答中的数值和景点引用自动校验，含景点一致性检查（`check_spot_consistency()`），未通过时降级到本地知识，防止幻觉
- **分段TTS流式播放** — 长回复自动切分为多个音频段，通过SSE逐段推送，消除首句等待延迟，支持并发TTS合成
- **情感感知系统** — 基于Plutchik八维情感轮（8种情感×3级强度） + 极坐标映射 + 情感惯性 + 波动率控制 + VRM 1.0标准表情混合 + 动作时间轴，实时驱动数字人表情变化
- **口型同步** — 基于Web Audio频谱分析的viseme分类 + 中文拼音音素映射（~400字→4类viseme），实现音频驱动的口型同步，含静音检测和回退模拟
- **3D VRM数字人** — 基于Three.js + @pixiv/three-vrm的实时渲染3D虚拟形象，支持4模型切换（景/灵/区/山）、热重载、骨骼操控、表情混合、per-model Y轴偏移校准
- **语音交互** — ASR语音输入识别（SiliconFlow SenseVoiceSmall，含TeleSpeechASR备选）+ TTS语音合成（edge-tts主/CosyVoice2备/Web Speech API浏览器备选），支持8种中文语音
- **多层检索流水线** — FTS5全文检索 → ChromaDB向量检索（ONNX MiniLM-L6-V2 384维）→ HyDE假设文档检索 → LLM重写检索，四层融合去重 + 关键词BM25备选降级
- **路线推荐** — 基于GPS定位的周边景点实时推荐，内置高德地图16个景区精准坐标（灵山9 + 拈花湾7，双区域）
- **实时天气** — 高德天气API获取无锡实时天气，前端天气小组件展示温度/湿度/风力/体感/出行建议
- **GPS导航** — 持续GPS定位 + 高德地图SDK集成 + 自然语言景点识别 + 导航深度链接跳转高德APP
- **管理后台** — 知识库/FAQ CRUD、对话日志多维筛选（时段/情感/兴趣/满意度）、VRM模型启用/禁用管理、操作日志审计
- **AI对话分析** — LLM驱动的对话分析（确定性指标 + AI洞察），生成执行摘要、发现、知识缺口、建议、典型案例，含关键词云和多维图表
- **数据运营大屏** — 5指标卡片 + 6 ECharts图表（情感趋势、话题排行、时段分布、景点热度、游客画像），赛博朋克/科幻深蓝主题，30秒自动刷新
- **操作日志审计** — 管理后台所有操作（登录/登出、知识库/FAQ增删改、VRM切换、设置变更等）自动记录，支持筛选分页和CSV导出
- **DOCX批量知识导入** — 支持结构化DOCX文档批量导入知识库，自动按标题分块、提取事实类型（高度/票价/开放时间等）和标签
- **桌面一键启动** — Windows桌面启动器，自动启动服务 + 健康检查 + 打开浏览器，支持打包为exe分发
- **生产部署** — 支持Waitress（Windows）／Gunicorn（Linux）多worker部署 + Docker容器化

## 技术栈

| 模块 | 技术 |
|------|------|
| 后端框架 | Python 3.11+ / Flask 3.0+ |
| 数据库 | SQLite 3 (WAL模式, 9表 + 2个FTS5全文索引 + 10+索引) |
| LLM | DeepSeek Chat / SiliconFlow Qwen2.5-7B / 讯飞星火Spark 4.0Ultra |
| TTS | edge-tts（主，8种语音） / SiliconFlow CosyVoice2-0.5B（备，5种语音） / Web Speech API（浏览器备选） |
| ASR | SiliconFlow SenseVoiceSmall（主）/ TeleSpeechASR（备） |
| 向量检索 | ChromaDB + ONNX MiniLM-L6-V2 (384维) + 关键词BM25备选 |
| HyDE + 查询重写 | LLM假设回答 + 可选查询改写 → 向量检索 |
| 事实验证 | 数值提取 + 知识库交叉校验 + 景点一致性检查 |
| 情感系统 | Plutchik情感轮(8×3级) + 极坐标映射 + 情感惯性 + 波动率 + VRM 1.0标准表情混合 |
| 口型同步 | Web Audio频谱分析 + viseme分类(4类) + 拼音音素映射(~400字) |
| 前端 | Vanilla HTML/CSS/JS + GSAP 3.12动画（游客端禅意佛韵设计 / 管理后台扁平风格） |
| 数字人 | VRM 1.0 (Three.js + @pixiv/three-vrm 2.1.1), 4模型热重载, 预设管理 |
| 数据分析 | ECharts 5.4.3 (管理仪表盘 + 数据大屏, 6项分析指标) |
| 地图 | 高德地图Web API (16个POI点, 灵山+拈花湾双区域, SDK代理) |
| 检索增强 | FTS5全文搜索 + ChromaDB向量检索 + HyDE + 查询重写 + 关键词备选 |
| 部署 | Waitress (Windows) / Gunicorn (Linux) / Docker |

## 检索系统架构

多层检索流水线，从快到慢逐级降级：

```
用户输入 → 查询重写(query_rewriter.py, 可选LLM) → 并行执行：
  ├── FTS5全文检索 (SQLite FTS5, trigram分词, 零延迟)
  ├── ChromaDB向量检索 (ONNX MiniLM → 384维)
  └── HyDE检索 (事实关键词触发 → 向量检索, 512条LRU缓存)
→ 结果融合去重 → 关键词BM25备选降级
→ LLM生成回答 → 事实验证(grounding_check.py)
→ 通过 → 返回 | 未通过 → 降级到本地知识库/FAQ
```

| 层级 | 实现 | 特点 |
|------|------|------|
| 1. 闲聊/问候 | `match_smalltalk()` | 零成本，正则匹配 |
| 2. FAQ匹配 | FTS5全文搜索 | 28+预置FAQ，关键词标签匹配，使用计数统计 |
| 3. 精准事实 | `match_strict_fact()` | 门票/高度/时间等精准匹配 |
| 4. 向量检索 | ChromaDB + ONNX MiniLM | 384维，语义搜索，原子重建 |
| 5. HyDE | 事实关键词触发 | 14个事实指标词触发，提升召回率 |
| 6. LLM生成 | DeepSeek/SiliconFlow/讯飞 | 带知识上下文，256条LRU响应缓存 |
| 7. 事实验证 | `grounding_check.py` | 数值提取+景点一致性双重校验 |

## 情感系统

基于 **Plutchik八维情感轮** 的情感闭环，8种情感×3级强度，极坐标映射：

```
用户输入 → 关键词匹配(10类情感标签) → 情感向量
→ 极坐标映射(valence, energy) → 情感惯性 + 波动率控制
→ VRM 1.0标准表情混合(happy/angry/sad/relaxed/surprised/aa/ih/ou/ee/oh/blink/neutral)
→ 动作时间轴(wave/nod/shake/bow/tilt/gesture/spread/point/think/openHand/crossArms/comfort)
→ SSE → VRM表情驱动 + 口型同步
```

情感标签：`joy`（积极回应）, `trust`（亲和讲解）, `fear`（担忧关注）, `surprise`（惊喜回应）, `sadness`（同情理解）, `disgust`（耐心倾听）, `anger`（认真讲解）, `anticipation`（期待推荐）

意图检测：`greeting`（问候）, `agreement`（赞同）, `disagreement`（反对）, `suggestion`（建议）, `introduction`（介绍）, `thinking`（思考）, `farewell`（告别）

## 数据库

SQLite 3 WAL模式，共9表 + 2个FTS5全文索引 + 10+索引：

| 表名 | 用途 | 关键字段 |
|------|------|---------|
| `knowledge` | 知识库条目 | title, content, category, tags(JSON), source, source_hash(去重) |
| `faq` | FAQ问答对 | question, answer, keywords(JSON), category, usage_count |
| `routes` | 游览路线 | interest, name, duration, suitable_for(JSON), stops(JSON), pitch |
| `conversations` | 对话日志 | session_id, user_id, message, reply, emotion, satisfaction, interest, topics(JSON), latency_ms |
| `settings` | 应用设置 | key-value 存储 |
| `avatar_config` | 数字人形象配置 | theme, active_profile, profiles(JSON), vrm_model |
| `guide_presets` | 导游预设 | model_name(UNIQUE), voice, outfit, style, expression_bias, enabled |
| `admin_operation_logs` | 管理操作日志 | admin_user, action, resource, resource_id, detail, ip_address, result |
| `admin_sessions` | 管理会话 | token_hash(PK), username, expires_at, created_at |

FTS5索引：`knowledge_fts`（知识库trigram全文搜索）, `faq_fts`（FAQ trigram全文搜索），通过触发器自动同步

## API接口

### 游客端

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/chat/text` | 文本对话（同步），返回reply/emotion/actions/audioUrl等 |
| POST | `/api/v1/chat/text-stream` | 流式对话（SSE），含status/text/audio_segment/text_done/done事件 |
| POST | `/api/v1/chat/transcribe-upload` | 纯语音转文字（ASR），返回转录文本 |
| POST | `/api/v1/chat/voice-upload` | 语音对话（ASR→对话→TTS），返回完整回答+音频 |
| GET | `/api/v1/scenic/brief` | 景区简介（名称/定位/模型/能力/数据源） |
| GET | `/api/v1/scenic/routes` | 游览路线推荐（4条预设路线） |
| POST | `/api/v1/feedback` | 对话满意度评分（1-5星） |
| GET | `/api/v1/navigation/scenic-spots` | 高德POI坐标列表（16个景点） |
| POST | `/api/v1/navigation/query` | 自然语言→目标景点识别（返回名称+坐标） |
| GET | `/api/v1/avatar/config` | 数字人公开配置（名称/语音/模型），支持modelId参数 |
| GET | `/api/v1/avatar/models` | 公开VRM模型列表（含风格标签） |
| GET | `/api/v1/weather` | 无锡实时天气（温度/湿度/风力/天气状况/出行建议） |
| GET | `/api/v1/map/amap-sdk.js` | 高德JS SDK代理（防CORS，含Windows TLS回退） |
| GET | `/api/v1/config` | 客户端配置（高德API Key/SecurityCode） |
| GET | `/api/v1/health` | 健康检查（status/time/ready） |
| GET | `/api/v1/health/tts` | TTS健康探针（edge-tts状态/缓存/语音/错误） |

### 数据大屏

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/data-screen` | 数据大屏页面 |
| GET | `/api/v1/data-screen/overview` | 核心指标（去重访客、对话量、满意度、话题与服务类型占比） |
| GET | `/api/v1/data-screen/deep` | 深度分析（景点提及次数、时段分布、对话兴趣画像） |
| GET | `/api/v1/data-screen/feedback` | 最近20条已评分对话反馈 |

`overview` 的 `todayVisitors` / `weekVisitors` 按访客标识去重，`todayConversations` / `weekConversations` 统计对话记录数；`deep.spotHeatmap` 返回的是对话中的景点 `mentions`，不代表真实到访量。

### 管理端

| 方法 | 路径 | 说明 |
|------|------|------|
| **认证** | | |
| POST | `/api/v1/admin/auth/login` | 管理员登录（速率限制8次/15分钟/IP） |
| GET | `/api/v1/admin/auth/me` | 当前登录信息 |
| POST | `/api/v1/admin/auth/logout` | 登出（删除会话） |
| **仪表盘** | | |
| GET | `/api/v1/admin/dashboard/overview` | 仪表盘概览（指标卡片+趋势图+情感分布+热点问题+话题标签） |
| GET | `/api/v1/admin/report` | 周报（摘要+话题+兴趣分布+建议） |
| GET | `/api/v1/admin/report/deep` | 深度分析报告（6项分析：情感趋势/话题排行/游客画像/改进建议/时段分布/景点热度） |
| GET | `/api/v1/admin/export/report` | CSV/JSON导出（仪表盘+周报+深度分析） |
| **知识库** | | |
| GET/POST | `/api/v1/admin/knowledge` | 知识库列表（搜索+分页）/新增 |
| PUT/DELETE | `/api/v1/admin/knowledge/<id>` | 知识条目更新/删除 |
| **FAQ** | | |
| GET/POST | `/api/v1/admin/faq` | FAQ列表/新增 |
| PUT/DELETE | `/api/v1/admin/faq/<id>` | FAQ更新/删除 |
| **对话日志** | | |
| GET | `/api/v1/admin/conversations` | 对话日志列表（分页+多维筛选：时段/情感/兴趣/满意度） |
| POST | `/api/v1/admin/conversations/analyze` | AI对话分析（确定性指标+LLM洞察：摘要/发现/知识缺口/建议/案例） |
| **数字人** | | |
| GET | `/api/v1/admin/avatar` | VRM形象配置（只读） |
| GET | `/api/v1/admin/avatar/models` | VRM模型列表（含禁用模型） |
| PUT | `/api/v1/admin/avatar/models/status` | 单个VRM模型启用/禁用（至少保留1个启用） |
| PUT | `/api/v1/admin/avatar/models/status/batch` | 批量启用所有VRM模型 |
| **导游预设** | | |
| GET | `/api/v1/admin/guide-presets` | 预设列表（只读） |
| **系统设置** | | |
| GET/PUT | `/api/v1/admin/settings` | 应用设置（可写：adminUser/admin_password/ttsVoice） |
| **操作日志** | | |
| GET | `/api/v1/admin/operation-logs` | 操作日志列表（分页+筛选：操作类型/资源模块） |
| GET | `/api/v1/admin/operation-logs/export` | 操作日志CSV导出 |

## 开源版安全说明

- 仓库不包含任何真实 API Key、管理员密码、登录 Token、SQLite 运行库或游客历史数据。
- `backend/.env.example` 只有空值和公开默认配置；首次使用时复制成本地 `backend/.env`，只填写你自己申请的凭据。
- `backend/.env`、`backend/admin_data/scenic.db`、日志、音频缓存和 ChromaDB 索引均属于本机运行文件，不要提交。
- 如果曾经把凭据提交到其他分支、Fork、Issue 或日志，请先在对应供应商后台撤销/轮换，再继续部署。

## 快速开始

### 1. 准备环境

- Python 3.11 或更高版本
- Git
- Docker（仅 Docker 部署需要）
- Node.js 不参与服务启动，仅用于前端语法检查和质量检查

### 2. 克隆、创建虚拟环境并安装依赖

Windows PowerShell：

```powershell
git clone https://github.com/zzcccyy/software-cup-a5-ai-digital-human.git
Set-Location software-cup-a5-ai-digital-human
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
Copy-Item backend\.env.example backend\.env
```

Linux / macOS：

```bash
git clone https://github.com/zzcccyy/software-cup-a5-ai-digital-human.git
cd software-cup-a5-ai-digital-human
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
cp backend/.env.example backend/.env
```

### 3. 配置本地环境

编辑 `backend/.env`。所有密钥只放在这个本地文件或部署平台的 Secret 管理器中，不要写回 README、代码、Issue 或提交记录。

| 功能 | 配置要求 |
|------|----------|
| 本地页面、景区静态知识、路线和 FAQ | 不需要第三方 API Key |
| AI 对话、AI 分析、查询重写 | 配置 DeepSeek、SiliconFlow 或讯飞星火中的至少一家 |
| 语音输入（ASR） | 配置 `SILICONFLOW_API_KEY` |
| 语音输出（TTS） | 默认使用 edge-tts；失败时尝试 SiliconFlow 备用 TTS |
| 天气、地图 SDK、导航 | 配置高德 `AMAP_API_KEY`；如使用 Web 安全密钥，再配置 `AMAP_SECURITY_CODE` / `AMAP_WEB_API_KEY` |
| 管理后台登录 | 建议设置 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD`；`APP_ENV=production` 时必须设置 |

最小配置可以先只填写：

```env
APP_ENV=development
LLM_PROVIDER=deepseek
ADMIN_USERNAME=admin
ADMIN_PASSWORD=请替换为随机长密码
```

不配置 LLM Key 时，应用仍可启动并使用本地问答、路线和 FAQ；需要实时 AI 生成时，再补充对应供应商的 Key。

首次启动会自动创建 `backend/admin_data/scenic.db`，初始化表结构、路线、FAQ、导游预设和内置景区知识，并在后台导入“示范景区公开资料包”中的知识。删除这个数据库后重新启动，会得到一个新的本地实例；不会从仓库恢复任何游客对话或管理员运行记录。

### 4. 启动服务

在仓库根目录执行：

```bash
python run.py
```

脚本会启动 Flask 后端，等待 `/api/v1/health` 健康检查通过后打开浏览器。

如果只想手动启动后端：

```bash
python backend/main.py
```

Windows 生产进程使用 Waitress：

```bash
python backend/waitress_server.py
```

### 5. 访问系统

| 地址 | 说明 |
|------|------|
| `http://localhost:8088/` | 游客端：数字人、对话、路线、天气和地图 |
| `http://localhost:8088/admin` | 管理后台：知识库、FAQ、对话分析、模型和操作日志 |
| `http://localhost:8088/data-screen` | 数据运营大屏 |
| `http://localhost:8088/api/v1/health` | 健康检查 |

管理员使用 `backend/.env` 中配置的账号登录 `/admin`。如果在显式 `APP_ENV=development` 环境下不填写管理员配置，程序保留 `admin/admin123` 的本地兼容初始化逻辑；这只用于开发/测试，生产环境必须显式配置随机长密码，不能使用该默认值。

## 项目结构

```
├── run.py                         # 一键启动（启动后端 + 打开浏览器）
├── run_backend.py                 # 后端启动脚本
├── start_server.py                # 服务器启动脚本
├── start-backend.bat              # Windows批处理启动脚本
├── Dockerfile                     # Docker容器化配置
├── render.yaml                    # Render云平台部署配置
├── CONTRIBUTING.md                # 贡献指南
│
├── backend/                       # Python Flask 后端
│   ├── main.py                    # 主应用 — 游客端、聊天及公共 API 业务逻辑(2474行)
│   ├── blueprints/                # 管理端与数据大屏 API 蓝图
│   │   ├── admin_core.py          # 认证/仪表盘/报表/设置/会话分析
│   │   ├── admin_content.py       # 知识库/FAQ/数字人/预设/操作日志
│   │   ├── data_screen.py         # 数据大屏页面与API
│   │   └── common.py              # 共享工具(分页/CSV)
│   ├── ai_service.py              # LLM调用(3家)/TTS合成/ASR识别/响应缓存(256条LRU)
│   ├── database.py                # SQLite数据库层(9表+FTS5+10+索引+CRUD+报表+分析)
│   ├── emotion_engine.py          # 情感分析引擎(10类情感标签+意图检测)
│   ├── emotion_state.py           # 情感状态机(Plutchik轮×3级→极坐标→惯性→波动→VRM表情)
│   ├── rag_vector.py              # ChromaDB向量检索(ONNX MiniLM-L6-V2 384维, 原子重建)
│   ├── hyde_retriever.py          # HyDE检索增强(14个事实指标词触发, 512条LRU缓存)
│   ├── query_rewriter.py          # 查询重写(可选LLM驱动, 512条缓存)
│   ├── grounding_check.py         # 事实验证(数值提取+景点一致性检查)
│   ├── tts_service.py             # SiliconFlow CosyVoice2 TTS备选服务(5种语音)
│   ├── amap_service.py            # 高德地图景区坐标库(16个POI点+灵山/拈花湾双区域)
│   ├── analyzer.py                # 数据分析引擎(6项分析:情感趋势/话题排行/时段分布等)
│   ├── deep_report.py             # 深度分析报告聚合
│   ├── conversation_analysis.py   # LLM对话分析(确定性指标+AI洞察:摘要/发现/知识缺口/建议)
│   ├── bundle_importer.py         # DOCX批量知识导入(按标题分块+提取事实类型+打标签)
│   ├── knowledge_expand.py        # 知识扩展(28条景区结构化描述种子数据)
│   ├── runtime_paths.py           # 跨平台路径解析(支持打包为exe)
│   ├── desktop_launcher.py        # Windows桌面一键启动器(健康检查+自动打开浏览器)
│   ├── waitress_server.py         # Waitress生产部署(8线程, 300s超时)
│   ├── gunicorn_config.py         # Gunicorn生产配置(4worker×8线程)
│   ├── requirements.txt           # Python依赖(12个包)
│   ├── .env.example               # 环境变量模板（不含密钥）
│   ├── admin_data/                # 可复现种子数据与本地运行时数据
│   │   ├── scenic.db              # 本地运行时 SQLite（不会提交）
│   │   ├── knowledge.json         # 知识库数据导出
│   │   ├── faq.json               # FAQ数据导出
│   │   ├── routes.json            # 路线数据导出
│   │   └── avatar_config.json     # VRM模型配置
│   ├── knowledge/                 # 知识文档(导入源)
│   ├── chroma_db/                 # ChromaDB持久化存储
│   └── static/audio/              # TTS音频缓存(LRU 500文件, 含路径遍历防护)
│
├── tourist-client/                # 游客端前端
│   ├── index.html                 # 主页面(GSAP动画登陆页+VRM画布+对话+语音+路线+天气+地图)
│   ├── app.js                     # 前端逻辑(2533行: SSE流式+口型同步+地图+情感+反馈+天气+GPS)
│   ├── styles.css                 # 禅意佛韵设计系统(3129行: 深色东方禅意+灵山金+20+动画)
│   ├── config.js                  # 客户端配置(远程API地址)
│   └── images/                    # UI图片资源
│
├── admin/                         # 管理后台前端
│   ├── index.html                 # 单页后台(登录+仪表盘+知识库+FAQ+对话AI分析+VRM+设置+日志)
│   ├── data-screen.html           # 数据运营大屏(赛博朋克主题+5指标+6图表+30s刷新)
│   ├── app.js                     # 后台逻辑(1954行: API调用+状态管理+ECharts+AI分析+分页)
│   └── styles.css                 # 管理后台样式
│
├── 示范景区公开资料包/            # 景区数据源(DOCX结构化知识)
├── 景.vrm / 灵.vrm / 区.vrm / 山.vrm  # VRM 3D模型文件(4个)
├── tools/                         # 辅助工具脚本
├── tests/                         # 测试文件
├── outputs/                       # 输出目录
├── .github/                       # GitHub CI/CD配置
├── .gitignore / .dockerignore
├── backend/.env.example          # 无密钥环境变量模板；本地 .env 不提交
└── README.md
```

## TTS语音选项

| 语音名称 | 引擎 | 语音ID |
|----------|------|--------|
| 温柔女声 | edge-tts（主） | zh-CN-XiaoxiaoNeural |
| 活泼少女 | edge-tts（主） | zh-CN-XiaoyiNeural |
| 热情女声 | edge-tts（主） | zh-CN-XiaohanNeural |
| 知性女声 | edge-tts（主） | zh-CN-XiaomoNeural |
| 磁性男声 | edge-tts（主） | zh-CN-YunjianNeural |
| 深沉男声 | edge-tts（主） | zh-CN-YunyeNeural |
| 阳光男声 | edge-tts（主） | zh-CN-YunxiNeural |
| 稳重男声 | edge-tts（主） | zh-CN-YunyangNeural |
| 温柔女声 | CosyVoice2（备） | FunAudioLLM/CosyVoice2-0.5B |
| 活泼少女 | CosyVoice2（备） | FunAudioLLM/CosyVoice2-0.5B |
| 知性女声 | CosyVoice2（备） | FunAudioLLM/CosyVoice2-0.5B |
| 磁性男声 | CosyVoice2（备） | FunAudioLLM/CosyVoice2-0.5B |
| 沉稳男声 | CosyVoice2（备） | FunAudioLLM/CosyVoice2-0.5B |

**主备切换：** edge-tts → SiliconFlow CosyVoice2 → Web Speech API（浏览器备选）
**缓存策略：** MD5哈希缓存 + LRU淘汰（上限500文件）+ 异常自动降级
**并发控制：** Semaphore(4)限制并发TTS合成，8秒超时/段，per-hash单飞锁

## LLM供应商支持

| 供应商 | 模型 | 环境变量 | 特点 |
|--------|------|---------|------|
| DeepSeek | deepseek-chat | `DEEPSEEK_API_KEY` | 默认主选，支持流式 |
| SiliconFlow | Qwen2.5-7B-Instruct (可配置) | `SILICONFLOW_API_KEY` | 国内直连，支持流式 |
| 讯飞星火 | Spark 4.0Ultra | `XUNFEI_APP_ID/KEY/SECRET` | WebSocket协议，不支持流式 |

**动态温度控制：** 事实类0.1 / 推荐类0.4 / 创意类0.5
**LLM响应缓存：** 256条LRU，10分钟TTL，按message+interest+route+knowledge+history+draft+supporting_facts键控

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `APP_ENV` | 应用环境；生产环境必须配置管理员凭据 | `development` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 管理后台账号密码；生产环境必填 | - |
| `LLM_PROVIDER` | `deepseek`、`siliconflow` 或 `xunfei` | `deepseek` |
| `DEEPSEEK_API_KEY` | DeepSeek 对话密钥 | - |
| `DEEPSEEK_API_BASE` | DeepSeek API 地址 | `https://api.deepseek.com/v1` |
| `SILICONFLOW_API_KEY` | SiliconFlow 对话、ASR 和备用 TTS 密钥 | - |
| `SILICONFLOW_API_BASE` | SiliconFlow API 地址 | `https://api.siliconflow.cn/v1` |
| `CHAT_MODEL` | `LLM_PROVIDER=siliconflow` 时使用的模型 | `Qwen/Qwen2.5-7B-Instruct` |
| `LLM_STREAM_MAX_TOKENS` | 流式回答最大 Token 数 | `600` |
| `SILICONFLOW_ASR_MODEL` | SiliconFlow ASR 主模型 | `FunAudioLLM/SenseVoiceSmall` |
| `SILICONFLOW_ASR_FALLBACK_MODEL` | ASR 备用模型 | `TeleAI/TeleSpeechASR` |
| `XUNFEI_APP_ID` / `XUNFEI_API_KEY` / `XUNFEI_API_SECRET` | 讯飞星火凭据，使用讯飞时填写 | - |
| `AMAP_API_KEY` | 高德地图与天气 API Key | - |
| `AMAP_SECURITY_CODE` | 高德 Web 安全密钥，按高德应用类型填写 | - |
| `AMAP_WEB_API_KEY` | 高德 Web 服务 Key；未填写时回退到 `AMAP_API_KEY` | - |
| `QUERY_REWRITE_USE_LLM` | 是否启用 LLM 查询重写 | `0`（禁用） |
| `WAITRESS_THREADS` | Waitress 线程数 | `8` |
| `GUNICORN_WORKERS` | Gunicorn worker 数 | `4` |
| `GUNICORN_THREADS` | 每个 Gunicorn worker 的线程数 | `8` |
| `MAX_PRECACHE_TEXTS` | TTS 预缓存数量 | `30` |
| `EDGE_TTS_VOICE` | 默认 edge-tts 语音 | `zh-CN-XiaoxiaoNeural` |
| `CORS_ALLOWED_ORIGINS` | 允许的浏览器来源，逗号分隔 | - |
| `LINGSHAN_NO_BROWSER` | 设置为 `1` 时禁止启动器自动打开浏览器 | - |

## 部署

### Windows (Waitress)
```bash
cd backend && python waitress_server.py
```

### Windows (桌面一键启动)
```bash
cd backend && python desktop_launcher.py
```

### Linux (Gunicorn)
```bash
cd backend && gunicorn -c gunicorn_config.py main:app
```

### Docker
```bash
docker build -t ai-man .
docker run --rm -p 8088:8088 --env-file backend/.env ai-man
```

Docker 或云平台部署时，请把 `APP_ENV=production`、`ADMIN_USERNAME`、`ADMIN_PASSWORD` 和所需 API Key 配置在平台的 Secret 管理器中，不要把 `backend/.env` 复制进镜像或提交到 Git。

### Render云平台
项目包含 `render.yaml` 配置，可直接部署到Render云平台。

### 生产环境注意事项
- Nginx反向代理需禁用缓冲（`proxy_buffering off`）确保SSE正常推送
- 前端文件由Flask直接托管，无需额外Web服务器
- SQLite 数据库位于 `backend/admin_data/scenic.db`，只作为本地运行数据；云平台重启或重新部署可能丢失，正式生产应使用持久化磁盘或托管数据库
- 管理员登录速率限制：8次/15分钟/IP，密码修改后所有token失效

## 许可证

MIT License

Copyright (c) 2025

MIT 许可默认仅适用于本项目原创代码。VRM 模型、图片、景区资料、ONNX 模型、字体和其他第三方内容可能适用各自的许可证；重新分发前请查看 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 并确认授权。

