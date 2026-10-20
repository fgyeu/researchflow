# ResearchFlow

> 基于异步任务调度与持久化执行的 AI 研究助手

一个可以接收研究问题、后台执行多步任务、把每一步都记录下来、最后产出研究报告的服务。

它不是"套壳问答"：核心要证明的是 **怎么把一次几十秒的 Agent 执行，做成一个稳定、可查、可观测的后端系统**。

---

## 现在做到哪一步了？

当前进度：**阶段 0 —— 最小可跑闭环** ✅

阶段 0 的目标**不是**做出一个聪明的 Agent，而是先证明这条链路真的通：

```
提交任务 → 立刻返回 run_id → 后台真的在跑 → 状态自己会变 → 每一步都留痕 → 拿到报告
```

为了把"链路通不通"和"模型聪不聪明"这两件事分开，阶段 0 的 Worker 用的是一个**假剧本**（`worker.py` 里的 `_PLAN`），四步各睡一会儿，不调用任何模型。

| 阶段 | 内容 | 状态 |
|---|---|---|
| **0** | 最小闭环：FastAPI + SQLite + 异步 Worker + trace | ✅ 已完成 |
| 1 | 换成真 Agent Loop：接入模型 + 真工具（搜索、读网页） | ⬜ 待做 |
| 2 | 后端的硬骨头：幂等提交、并发控制、缓存、超时重试 | ⬜ 待做 |
| 3 | 可观测性与实验：Trace 分析、固定任务集、对照实验数据 | ⬜ 待做 |
| 4 | 交付物：README、架构图、实验报告、Docker 打包 | ⬜ 待做 |

---

## 怎么跑起来

> **强烈建议在魔搭 ModelScope Notebook 上跑**，而不是本地笔记本。原因见下面
> 「为什么用魔搭而不是本地」一节。本地也能跑，但环境准备会很痛苦。

### 方案 A：魔搭 ModelScope Notebook（推荐）

1. 打开 <https://modelscope.cn> → 我的 Notebook → 新建，选 **CPU 环境**（8 核 / 32GB，**长期使用，不消耗 GPU 额度**）
2. 启动后打开 Terminal，把 `researchflow/` 这个目录传上去（git clone 或直接拖进 JupyterLab 文件树）
3. 一行命令把环境配好：

    ```bash
    cd researchflow
    bash scripts/bootstrap.sh
    ```

    这个脚本会：用清华镜像装包 → 建独立的 `.venv`（不动预装环境）→ 装依赖 → 语法自检。

4. 开两个终端：

    ```bash
    # 终端 1：启动服务
    .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

    # 终端 2：端到端冒烟测试
    .venv/bin/python scripts/smoke_test.py
    ```

    （魔搭上必须加 `--host 0.0.0.0` 才能从预览/转发访问。）

换镜像：`MIRROR=https://mirrors.aliyun.com/pypi/simple bash scripts/bootstrap.sh`

### 方案 B：本地 Windows

第一次先装依赖（**务必指定国内镜像，否则会非常慢**）：

```powershell
cd D:\Desktop\project\researchflow
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**终端 1** —— 启动服务：

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

**终端 2** —— 跑冒烟测试：

```powershell
.venv\Scripts\python.exe scripts\smoke_test.py
```

### 玩一玩

也可以直接打开自动生成的接口文档 <http://127.0.0.1:8000/docs>，在网页上点着玩。

手动玩一遍：

```powershell
# 提交一个任务
curl.exe -X POST http://127.0.0.1:8000/api/v1/research -H "Content-Type: application/json" -d "{\"question\":\"对比一下主流 Agent 框架的 Memory 机制\"}"

# 拿返回的 run_id 去查（多查几次，能看到状态在变）
curl.exe http://127.0.0.1:8000/api/v1/research/<run_id>

# 看健康状态
curl.exe http://127.0.0.1:8000/health
```

---

## 目录结构

```
researchflow/
├── app/
│   ├── main.py           # API 层：接请求、校验、立刻返回 run_id、查状态
│   ├── worker.py         # 执行层：后台跑研究流程（阶段 0 是假剧本）
│   └── db.py             # 存储层：SQLite 建表与读写
├── scripts/
│   ├── bootstrap.sh      # 环境引导（Linux / 魔搭）
│   └── smoke_test.py     # 端到端冒烟测试
├── requirements.txt
└── researchflow.db       # 跑起来后自动生成
```

**为什么分成三层？** 因为阶段的推进方式就是"只换其中一层"：
阶段 1 只改 `worker.py`（假剧本 → 真 Agent），阶段 2 主要改 `db.py` 和新增队列逻辑。
如果全写在一个文件里，每次都得推倒重来。

---

## 阶段 0 的三问（做完请自问）

跑通之后，你应该能回答这三个问题。答不上来就说明还没真懂，别急着进阶段 1：

1. **为什么 `POST /api/v1/research` 返回的是 `202` 而不是 `200`？**
   提示：想想如果返回 200，调用方会默认什么。

2. **任务状态存在哪里？如果我在任务跑到一半时把服务杀掉，重启后状态会是什么？**
   提示：看看 `researchflow.db` 里 `research_runs` 表的 `status` 字段。
   （剧透：会永远卡在 `running`。这正是阶段 2 要解决的问题 —— 而且它就是面试里
   "持久化 ≠ 可恢复"那句话的真实含义。）

3. **`worker.py` 里的 `MAX_CONCURRENCY` 是干什么的？如果去掉会发生什么？**
   提示：想想同时提交 100 个任务时，你的机器和模型 API 会发生什么。

---

## 已知的简化（都是故意留的）

这些不是 bug，是**留给后面阶段亲手踩的坑**：

| 简化 | 阶段 0 为什么可以这样 | 什么时候必须改 |
|---|---|---|
| 数据库读写是同步阻塞的 | 数据量小，感受不到 | 阶段 2，高并发时它会变成瓶颈 |
| 没有幂等：重复提交就重复创建任务 | 先让你亲眼看到问题 | 阶段 2 |
| Worker 崩溃后任务永远卡在 `running` | 先让你亲眼看到问题 | 阶段 2 |
| 单进程内存队列，没有真正的外部队列 | 少装一个组件 | 阶段 2（多 Worker 时） |
| 假 Agent，不调用模型 | 先把链路和模型的复杂度分开 | 阶段 1 |

---

## 为什么用魔搭而不是本地？

本地机器体检结果：

| 项目 | 实际情况 | 结论 |
|---|---|---|
| CPU | Intel i5-1035G1（4 核 8 线程，低压 U 系列） | 能跑，但别指望快 |
| 内存 | 8 GB（可用约 6.7 GB） | Docker Desktop 会吃紧 |
| 显卡 | 无独立 GPU | 好在**这个项目根本不需要 GPU** |
| Docker | 未安装 | 要装 Docker Desktop + WSL2 |
| WSL | 有 `wsl.exe`，但没有安装任何发行版 | 还得先下发行版镜像 |
| 磁盘 | C 盘剩 15.3 GB、D 盘剩 12.8 GB | Docker 镜像动辄十几 GB，很危险 |
| 实测 | `pip install fastapi uvicorn` 跑了 25 分钟没装完 | **网络环境是最大的瓶颈** |

魔搭 CPU 环境：**8 核 / 32GB / 长期使用不消耗 GPU 额度 / 预装 conda + Python 3.12 + JupyterLab + Terminal**。
关键是它是 **Linux** —— Docker、Redis、PostgreSQL 到了阶段 2 都是一条命令的事，而本地要折腾一整天。

**代价（要诚实知道）：**

- 免费实例**会关机，不是 7×24 常驻服务**。所以"丢一个在线链接给面试官"这条路走不通。
  但开发、跑实验、出数据、截图、录屏，完全够用 —— 而这些才是简历项目真正要的东西。
- 重要代码要及时 `git push`。这反过来逼你养成好习惯，而面试官确实爱看 commit 历史。
- 因此**阶段 2 才引入的 Docker/Redis/PostgreSQL**，在魔搭上做会很顺；本地做会很痛。

> 面试时的说法：*"我的开发环境是云端 CPU 实例，8 核 32G。项目本身是 I/O 密集型 ——
> 时间几乎都花在等模型 API 和网络请求上，所以不需要 GPU。"* 这句话本身就说明你懂系统。

---

## 为什么阶段 0 不用 Redis / Docker / PostgreSQL？

因为顺序应该是：**先用最少组件把闭环跑通，等系统逻辑稳定了，再判断哪些组件真的值得引入。**

这也恰好是判断"该不该加一个技术"的正确顺序 —— 先有需求，再有组件。

> 面试时的说法：*"我先用 SQLite 跑通闭环，把并发和可靠性问题暴露出来之后，再判断
> 哪些地方需要换成 PostgreSQL 和外部队列 —— 而不是一上来就堆组件。"*

---

## 配置说明

阶段 0 **不需要任何 API Key**（因为不调用模型）。阶段 1 接入模型时才需要配置。