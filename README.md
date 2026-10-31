# 企业私有知识库 RAG 平台（Python + Milvus + 千问）

一个可私有化部署的企业知识库 Web 应用：上传内部文档（含扫描件）→ 自动 OCR / 切片 / 向量化 → 混合检索 + rerank → 千问基于资料回答并给出引用；带登录鉴权、用户/部门隔离、异步上传、审计和备份。

```
文档上传 -> 解析/OCR -> 切片 -> 向量化 -> Milvus
问题 -> 向量召回 + BM25 召回 -> RRF 融合 -> Cross-Encoder 重排
     -> 拼 Prompt -> 千问 -> 答案 + 引用来源
```

## 项目文档

- [完整项目介绍（Markdown）](docs/PROJECT_OVERVIEW.md)
- [产品介绍 HTML（可打印）](docs/project-overview.html)
- [产品介绍 PDF（18 页）](docs/企业私有知识库-RAG平台-项目介绍.pdf)
- [Milvus 架构与 RAG 流程（结合本项目）](docs/MILVUS_ARCHITECTURE_AND_RAG_FLOW.md)
- 架构图 / RAG 流程图 / 权限模型：[docs/images](docs/images)

## 目录结构

```
RAG-demo/
├── rag_demo.py          # 命令行版 RAG 逻辑（切片/向量化/检索/调模型）
├── app.py               # Web 后端：上传、解析、入库、问答 API
├── serve.py             # 生产模式（Waitress WSGI）
├── start_web_prod.bat   # Windows 生产模式一键启动
├── backup.py            # 备份 data/app.db + data/docs
├── download_reranker.py # 从 ModelScope 下载 BGE reranker
├── deploy/nginx.conf    # Nginx 反向代理 / HTTPS 示例
├── docs/                # 项目介绍（HTML / PDF / Markdown）与架构图、截图
├── web/                 # Web 前端（原生 HTML/CSS/JS，无需构建）
│   ├── index.html
│   ├── login.html       # 登录页
│   ├── style.css
│   ├── app.js
│   └── login.js
├── start_web.bat        # Windows 一键启动 Web 界面
├── docker-compose.yml   # Milvus standalone：etcd + MinIO + Milvus
├── requirements.txt
├── .env.example         # 配置模板，复制成 .env 后修改
├── data/
│   ├── docs/            # 知识库文件，按部门分目录
│   │   └── 公共/
│   │       ├── milvus.md
│   │       ├── rag.md
│   │       └── vector-search.md
│   ├── app.db           # 用户、文档元数据、会话历史（自动生成）
│   └── .secret_key      # 会话 Cookie 密钥（自动生成，勿提交）
└── README.md
```

## 前置条件

1. **Docker Desktop**（含 `docker compose`）
2. **Python 3.10+**
3. **Milvus 已在 `localhost:19530` 运行**（用本目录的 `docker-compose.yml` 启动，见下一节）。
4. 第一次运行 `ingest` 会自动下载嵌入模型（约 100MB）。

## 启动 Milvus

```bash
docker compose up -d
docker compose ps                 # 等 milvus-standalone 变成 healthy
curl http://localhost:9091/healthz
```

第一次会拉取 etcd、MinIO、Milvus 镜像（合计 1GB 左右），需要几分钟。

- `milvus-etcd`：元数据与协调中心，端口 2379（只在容器网络内）
- `milvus-minio`：对象存储，S3 API 9000，控制台 <http://localhost:9001>（账号密码都是 `minioadmin`）
- `milvus-standalone`：主服务，gRPC 端口 19530
- `milvus-volume-init`：一次性任务，负责修正数据目录权限，退出是正常的

数据保存在本目录的 `./volumes/` 下。`docker compose down` 只停容器、保留数据；要清空就把 `./volumes/` 删掉。

## Web 界面：上传资料 + 知识库问答（推荐）

```bash
# 1. 确认 Milvus 已启动
docker compose up -d

# 2. 启动 Web 服务（Windows 也可以双击 start_web.bat）
.venv\Scripts\python.exe app.py
```

浏览器打开 <http://127.0.0.1:8000>。

界面能力：

- **登录鉴权**：账号密码登录，Session Cookie；未登录会跳转 `/login`
- **用户 / 部门隔离**：普通用户只能看到「公共 + 本部门」资料；上传固定归本部门；会话互相隔离
- 管理员可创建 / 删除用户，可查看并管理全部部门的资料
- 左侧点击 / 拖拽上传资料，支持 **TXT、Markdown、PDF、Word (.docx)**，单文件 ≤ 20MB
- **扫描版 PDF 自动 OCR**：文本层过少的页面会渲染成图片交给 RapidOCR 识别（模型随依赖自带，不需要联网下载）
- **异步上传任务**：上传后立即返回，界面显示排队、解析 / OCR、向量化、写入 Milvus 的实时进度，大文件不会阻塞对话
- 上传后自动解析、切片、向量化并写入 Milvus，同名文件自动覆盖旧切片

> 上传任务由后台单个 worker 串行处理，避免多个 OCR / 向量化任务同时占满 CPU。
> 任务状态保存在内存中，重启服务会丢失未完成任务；已经入库的资料不受影响。
- 左侧列出已入库资料、切片数、所属部门和上传人，可单独删除（会同时删除向量）
- **历史会话自动保存**到 SQLite（`data/app.db`）：左侧「历史会话」页签可继续或删除；刷新页面、重启服务后自动恢复最近一次会话
- 右侧对话区、资料列表、会话列表都有可见滚动条（Windows 覆盖式滚动条也不会“消失”）
- **混合检索 + Cross-Encoder 重排**：向量召回 + BM25 关键词召回，RRF 融合后用 BGE reranker 精排；引用来源会标注“向量 / 关键词 / 混合召回”
- 右侧对话式提问，知识库有相关内容时只基于资料回答，并展示 Top-K 引用来源、相似度/重排分和召回方式
- 登录失败限流：同一用户名 10 分钟内失败 5 次锁定 10 分钟
- 管理员可重置用户密码；所有登录、上传、删除、用户管理、问答元数据写入审计日志
- 知识库**没有相关内容**时（最高相似度低于 `RAG_SCORE_THRESHOLD`，默认 0.35，且关键词覆盖率不足）：默认调用千问用通用知识回答，并在界面明确标注“不是企业资料”；把 `RAG_FALLBACK_TO_LLM` 设为 `false` 则只提示无相关资料
- 若检索分数过线、但千问判断资料无法回答（回复“资料中没有提到”），也会自动兜底为通用回答
- 可展开查看本次实际发送给大模型的 Prompt
- 左下角支持修改自己的密码、管理员用户管理、退出登录

### 初始账号

首次启动时如果 `data/app.db` 不存在，会自动创建以下账号：

| 账号 | 初始密码 | 部门 | 角色 |
| --- | --- | --- | --- |
| `admin` | `admin123` | 管理部 | 管理员 |
| `alice` | `demo123` | 研发部 | 普通用户 |
| `bob` | `demo123` | 财务部 | 普通用户 |

> **上线前务必**：登录 `admin` 后点左下角「改密」修改密码；演示账号可删除或改密。
> 也可以在首次启动前在 `.env` 设置 `ADMIN_PASSWORD`、`SEED_DEMO_USERS=false`。
> 账号只在首次初始化时创建，之后修改 `.env` 不会更新已有账号。

### 部门权限规则

- **管理员**：查看 / 管理全部资料，检索全部部门，创建 / 删除用户
- **普通用户**：只看到「公共 + 本部门」资料；上传的资料强制归本部门；只能删除自己上传的资料
- **公共资料**：由管理员上传，所有用户可见
- **会话历史**：只对创建者可见，管理员也看不到其他用户的会话

> Web 版使用独立集合 `rag_docs`，命令行版使用 `rag_demo`，两者互不影响。
> 网页资料保存在 `data/docs/<部门>/`，用户和会话在 `data/app.db`。

## 混合检索与重排

问答检索流程：

1. **向量召回**：Milvus 取 Top `HYBRID_DENSE_TOP_N`（默认 15）
2. **关键词召回**：jieba 分词 + BM25 取 Top `HYBRID_BM25_TOP_N`（默认 15）
3. **RRF 融合**：`1 / (RRF_K + rank)`，`RRF_K` 默认 60
4. **Cross-Encoder 重排**：BGE reranker 对融合后的前 `RERANK_TOP_N`（默认 8）打分，取 Top-K
5. **相关性判断**：向量最高分 ≥ `RAG_SCORE_THRESHOLD`，或关键词覆盖率 ≥ `KEYWORD_COVERAGE_THRESHOLD`，否则走通用回答

reranker 模型约 280MB，放在 `models/bge-reranker-base/`，可用：

```bash
.venv\Scripts\python.exe download_reranker.py
```

从 ModelScope 下载。模型不存在时自动退化为「混合检索（RRF）」，不影响使用；设置 `RERANK_ENABLED=false` 可强制关闭重排。

## 生产部署（Waitress + Nginx）

```bat
:: 生产模式（Windows）
start_web_prod.bat
:: 或者
.venv\Scripts\python.exe serve.py
```

- Waitress 监听 `0.0.0.0:8000`，线程数由 `WEB_THREADS` 控制（默认 8）
- Nginx 反向代理 / HTTPS 示例见 `deploy/nginx.conf`
- 上传任务队列和登录限流在单进程内存中，**只运行一个服务实例**
- SQLite 适合中小团队；更大规模建议迁移 PostgreSQL
- 生产环境请修改 `admin` 初始密码，并设置 `SEED_DEMO_USERS=false`

## 备份与恢复

```bash
.venv\Scripts\python.exe backup.py
```

生成 `backups/rag-backup-YYYYmmdd-HHMMSS.zip`，包含：

- `data/app.db`（用户、文档元数据、会话、审计日志）
- `data/docs/`（知识库原始文件）
- `data/.secret_key`（会话 Cookie 密钥）

恢复：停止服务 → 解压覆盖项目根目录的 `data/` → 重启。向量数据在 Milvus `./volumes/` 中；如需完整备份 Milvus，请使用 `milvus-backup` 工具或重新从 `data/docs` 入库。

## 当前局限与企业化路线

当前版本适合个人 / 小团队内网演示。正式企业环境建议按下面顺序补齐：

| 能力 | 现状 | 建议 |
| --- | --- | --- |
| 登录鉴权 | ✅ 已完成：账号密码 + Session Cookie | — |
| 用户 / 部门隔离 | ✅ 已完成：`department` 字段 + 检索过滤 + 会话隔离 | — |
| SSO / 密码找回 / 登录限流 | ✅ 登录限流 + 管理员重置密码；SSO、用户自助找回未做 | P1：企业微信/钉钉/LDAP |
| 扫描版 PDF OCR | ✅ 已完成：RapidOCR + pypdfium2，按页自动识别 | — |
| 异步上传进度 | ✅ 已完成：后台任务队列 + 轮询进度 | — |
| 混合检索 + rerank | ✅ 已完成：BM25 + 向量 RRF + BGE cross-encoder | — |
| 生产部署 | ✅ Waitress + Nginx 示例 | 按企业环境配置 HTTPS |
| CSRF / 审计 / 备份 | ✅ 审计日志 + 备份脚本；CSRF 未单独实现 | P2：CSRF Token、定时备份任务 |

## 命令行方式（可选）

```bash
# 1. 安装依赖（建议先建虚拟环境）
python -m venv .venv
.venv\Scripts\activate        # Windows；Git Bash 用 source .venv/Scripts/activate
python -m pip install -r requirements.txt

# 2. 准备配置
copy .env.example .env        # Git Bash 用 cp .env.example .env

# 3. 建库：切片 -> 向量化 -> 写入 Milvus
python rag_demo.py ingest

# 4. 提问：检索 -> 拼 Prompt -> 调大模型
python rag_demo.py ask "Milvus 为什么需要 etcd？"

# 只检索并打印 Prompt，不调用大模型
python rag_demo.py ask "RAG 的切片为什么要重叠？" --raw
```

> 命令行版使用 `rag_demo` 集合，会递归读取 `data/docs` 下的 .md/.txt（包括部门子目录），但不会影响 Web 版的 `rag_docs` 集合。

> 默认使用阿里云百炼 Token Plan 的 Anthropic 兼容接口（千问，模型 `qwen3.8-max`），在 `.env` 中填写 `ANTHROPIC_AUTH_TOKEN` 和 `ANTHROPIC_BASE_URL` 即可。也可以改用任意 OpenAI 兼容接口（填 `LLM_API_KEY` 等）。两者都不填时程序只打印实际发给大模型的 Prompt，方便观察“检索增强”到底增强了什么。

> 网络慢、bge 模型下载卡住？在 `.env` 里先设置 `EMBEDDING_BACKEND=hash` 跑通全流程（不下载模型），之后再切回 `fastembed` 体验真正的语义检索。

## 每一步对应代码里的哪里

| RAG 步骤 | 代码 | 说明 |
| --- | --- | --- |
| 文档切片 | `chunk_text()` | 固定窗口 + overlap，防止一句话被切断 |
| 文本向量化 | `Embedder.embed()` | 支持 fastembed / OpenAI 兼容接口 / hash |
| 建集合与索引 | `recreate_collection()` | schema：id、vector、text、source；HNSW + COSINE |
| 写入 | `cmd_ingest()` | 批量插入后 `flush` |
| 检索 | `cmd_ask()` 中的 `client.search()` | 返回 Top-K 及其相似度分数 |
| 拼 Prompt | `build_prompt()` | 把切片和问题放进同一个提示词 |
| 调大模型 | `ask_llm()` | Anthropic 兼容（千问）或 OpenAI 兼容接口 |

## 观察数据落在哪里（对应 Milvus 架构）

跑完 `ingest` 之后，可以打开另外两个容器验证一下它们的分工：

- **MinIO**：打开 <http://localhost:9001>，用 `minioadmin / minioadmin` 登录，进入 `a-bucket`，能看到 `files/insert_log/...` 之类的对象文件——向量和文本真正落地的地方。
- **etcd**：只存元数据（schema、segment、binlog 路径等）。可以在终端看 key（前缀是 `by-dev`）：

  ```bash
  docker compose exec etcd etcdctl get --prefix by-dev --keys-only | head -20
  ```

- **Milvus**：对外只暴露 19530。检索时 querynode 已把 segment 加载进内存，不会每次查询都读 MinIO。

## 三种向量化后端

在 `.env` 里改 `EMBEDDING_BACKEND`：

| 后端 | 是否需要 Key | 说明 |
| --- | --- | --- |
| `fastembed`（默认） | 否 | 本地 ONNX 模型，中文用 `BAAI/bge-small-zh-v1.5`（512 维） |
| `openai` | 是 | 任何 OpenAI 兼容 embedding 接口，如 DashScope、SiliconFlow |
| `hash` | 否 | 零依赖后备，字符 n-gram 哈希（256 维）；只能跑通流程，语义能力弱 |

改成别的嵌入模型后，**必须重新运行 `ingest`**，否则维度对不上，而且不同模型的向量空间不可混用。

## 换成自己的文档

把 `.md` 或 `.txt` 放进 `data/docs/`，重新运行：

```bash
python rag_demo.py ingest
```

`ingest` 每次都会删除并重建集合 `rag_demo`（演示用，避免重复数据）。真实项目里应该改成增量写入。

## 常见问题

**连不上 Milvus / Connection refused**

在项目目录确认容器状态，并看 Milvus 启动日志：

```bash
docker compose ps
docker compose logs -f standalone
curl http://localhost:9091/healthz
```

如果没装 Docker，先安装 Docker Desktop；也可以直接用 Zilliz Cloud 等托管服务，把 `MILVUS_URI` 和 `MILVUS_TOKEN` 改成服务商给的地址和 API Key。Docker Hub 或 quay.io 拉镜像慢时，给 Docker 配置镜像加速器。

**pymilvus 与服务端版本不兼容**

本项目的 compose 固定 Milvus `v3.0.5`，和安装的 pymilvus 3.x 匹配。如果你把 compose 里的镜像换成了 2.x，连接或建集合时报版本相关错误，可以安装匹配的 2.x 客户端：

```bash
python -m pip install "pymilvus>=2.5,<3"
```

**下载 bge 模型很慢或失败**

国内网络可以设置镜像后重试：

```bash
export HF_ENDPOINT=https://hf-mirror.com     # Git Bash
set HF_ENDPOINT=https://hf-mirror.com        # cmd
$env:HF_ENDPOINT="https://hf-mirror.com"     # PowerShell
```

实在装不上 `fastembed`，就在 `.env` 里设置 `EMBEDDING_BACKEND=hash`，先跑通流程。

**检索结果分数怎么理解**

使用 `COSINE` 时，`score` 越大越相似（通常 0~1）；如果换成 `L2`，则是越小越相似。建索引和检索的 `metric_type` 必须一致。

**能问示例文档以外的问题吗**

可以，但检索只会从 `data/docs/` 的内容里找答案。资料中没有的内容，提示词要求模型回答“资料中没有提到”。

## 这个示例故意省略了什么

这是学习用的最小实现。生产级 RAG 通常还会加入：PDF/Word 解析、按标题和句子边界切片、混合检索（向量 + 关键词）、rerank 重排、多路召回、查询改写、引用标注、离线评估等。先跑通这条最小链路，再按需加。
