# 贡献与仓库规范

## 开始使用

1. 使用 Python 3.11 或更高版本创建虚拟环境。
2. 安装后端依赖：`python -m pip install -r backend/requirements.txt`。
3. 复制 `backend/.env.example` 为 `backend/.env`，再填入你自己的 API Key。不要提交 `.env` 或任何真实凭据。
4. 本地启动：`python run.py`。

## 提交前检查

```bash
python -m compileall -q backend tests
python -m unittest tests.test_db_concurrency tests.test_fts_sync tests.test_indexes tests.test_mobile_responsive_contract
node --check admin/app.js
node --check tourist-client/app.js
node --check tourist-client/config.js
```

安装完整依赖后，再运行全部测试：

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

## 文件边界

- `backend/.env`、SQLite 数据库、日志和运行时导出属于本地文件，不提交。
- `backend/admin_data/*json` 中的知识、FAQ、路线和头像配置仅作为可复现种子数据；不得写入真实游客或管理员数据。
- `outputs/`、`.tmp/`、`backend/chroma_db/`、`backend/static/audio/`、SQLite WAL 文件和缓存不提交。
- 新增运行时数据时，优先确认它属于可复现种子数据还是本地生成物；生成物应加入 `.gitignore`。
- Python、前端、配置和文档遵循 `.editorconfig` 与 `.gitattributes`。

## 提交约定

提交信息使用简短的 Conventional Commits 风格前缀，例如：

- `feat:` 新功能
- `fix:` 缺陷修复
- `test:` 测试补充
- `docs:` 文档调整
- `chore:` 仓库与工具维护
