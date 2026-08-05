# 本地部署手册：Ollama + Docker + Quote Worker（草案）

- **文档编号**：RUN-LOCAL-001
- **版本**：0.1.0-draft
- **目标设备**：Windows 11 + NVIDIA RTX 4070 Super（12GB VRAM）

> **特别注意 1：不要将 Ollama 端口暴露到公网或局域网。** 仅允许本机 Worker 访问。
>
> **特别注意 2：Docker Desktop 必须启用 WSL2 后端和 NVIDIA GPU 支持。**
>
> **特别注意 3：`.env` 包含 Agent Token，绝不能上传 Git 或发给客户。**

## 1. 前置要求

| 项目 | MVP 版本/条件 |
|---|---|
| 操作系统 | Windows 11 64-bit |
| GPU | RTX 4070 Super，12GB 显存 |
| NVIDIA 驱动 | 使用当前稳定版 Studio 或 Game Ready 驱动 |
| Python | 3.11.x |
| Docker Desktop | 当前稳定版，启用 WSL2 |
| Ollama | 当前稳定版 Windows 版本 |
| Git | 当前稳定版 |
| 磁盘空间 | 至少预留 80GB SSD（模型、Docker、日志、临时文件） |
| 内存 | 建议 32GB；16GB 可启动但 14B 模型和 OCR 并发体验较差 |

## 2. 推荐运行模式

### 模式 A：Ollama 运行在 Windows 主机，Worker 运行在 Python 虚拟环境（推荐 MVP）

```text
Worker → http://127.0.0.1:11434 → Ollama
```

优点：最简单，GPU 和文件系统权限问题最少。

### 模式 B：Ollama 在 Windows 主机，Worker 在 Docker 容器

```text
Worker container → http://host.docker.internal:11434 → Ollama
```

> 容器内不要使用 `localhost:11434`；它会指向容器自身，而不是 Windows 主机。

## 3. 模型建议

MVP 选择原则：中文能力、指令跟随、JSON 稳定性优先。

| 用途 | 推荐规模 | 显存建议 | 备注 |
|---|---:|---:|---|
| 需求提取/分类/审查 | 7B–8B 4-bit | 约 5–7GB | 首选，适合 4070 Super |
| 更复杂文档汇总 | 14B 4-bit | 约 9–12GB+ | 可测试，速度和内存压力更高 |
| Embedding（第二阶段） | 小型 embedding 模型 | 低 | 仅用于 RAG |

安装模型示例（以你实际选择的 Ollama 模型名为准）：

```powershell
ollama pull <你的7B或8B中文指令模型>
ollama list
ollama run <你的模型名>
```

模型验证：要求模型只返回 JSON。若频繁出现格式错误，应优先换模型或强化 Schema，而不是增加随机性。

## 4. 项目配置

创建 `.env`：

```env
CLOUD_API_BASE_URL=https://<你的腾讯云网站域名>/api/internal/agent
AGENT_ID=office-4070super-01
AGENT_API_TOKEN=请从云端后台生成的长随机Token

OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=替换成你已下载的模型名

WORK_DIR=./data/incoming
SQLITE_PATH=./data/agent.db
LOG_LEVEL=INFO
RAG_ENABLED=false
```

> `CLOUD_API_BASE_URL` 和 `AGENT_API_TOKEN` 必须替换为真实部署值；此处不能根据未知腾讯云域名伪造。

## 5. Python 运行步骤

```powershell
git clone <你的代码仓库地址>
cd mechanical-quote-agent
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts\validate_rules.py
python scripts\test_quote.py
python scripts\run_worker.py
```

建议 Windows 任务计划程序设置：用户登录后启动 Worker；程序异常退出后每 5 分钟重启一次。

## 6. Docker Compose 示例

`docker-compose.yml`（Worker 容器化时使用）：

```yaml
services:
  quote-worker:
    build: .
    env_file: .env
    environment:
      OLLAMA_BASE_URL: http://host.docker.internal:11434
    volumes:
      - ./data:/app/data
      - ./config:/app/config:ro
    restart: unless-stopped
```

启动：

```powershell
docker compose up -d --build
docker compose logs -f quote-worker
```

> MVP 不建议同时用 Docker 跑 Worker、Ollama、OCR、向量库等全部服务。先让 Ollama 和 Python Worker直接运行稳定，再逐步容器化。

## 7. 健康检查

| 检查项 | 命令/方式 | 成功标准 |
|---|---|---|
| Ollama 可用 | `ollama list` | 可显示已下载模型 |
| 模型推理 | `ollama run <model>` | 能返回测试结果 |
| GPU 可用 | `nvidia-smi` | 可看到 GPU、显存使用 |
| Worker 日志 | 查看 `data/audit_logs` | 成功领取/心跳/回传 |
| 云端连通 | Worker 健康检查 API | HTTPS 请求成功 |
| 规则有效 | `python scripts/validate_rules.py` | 返回通过 |

## 8. 常见故障

| 问题 | 可能原因 | 处理 |
|---|---|---|
| Worker 连不上 Ollama | Ollama 未启动或 URL 错误 | 检查 `OLLAMA_BASE_URL`、`ollama list` |
| Docker 内 Worker 连不上 Ollama | 使用了 `localhost` | 改用 `host.docker.internal:11434` |
| 显存不足/推理很慢 | 模型过大、其他程序占 GPU | 优先 7B/8B 4-bit，关闭占 GPU 程序 |
| API 返回 401 | Token 错误或失效 | 重新生成并更新 `.env` |
| 文件下载 403/410 | COS 签名 URL 过期 | 重新领取任务或请求刷新 URL |
| OCR 速度慢 | 高分辨率扫描件过多 | 降采样、限制页数、异步处理 |
| 回传重复报价 | 未使用幂等键 | 使用 task ID 作 `Idempotency-Key` |

## 9. 备份与升级

- 每周备份：`config/`、规则版本、审计数据库、报价快照；
- 不备份：客户临时附件、模型缓存（可重新下载）；
- 升级模型前，用测试集运行对比；不得直接替换生产默认模型；
- 升级规则前，先执行历史报价回放；
- 升级 Docker 镜像前，保留上一版本镜像或 Git Tag。
