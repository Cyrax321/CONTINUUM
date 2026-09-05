<p align="center">
  <img src="docs/assets/readme-img.png" alt="CONTINUUM 横幅" width="100%" />
</p>

<p align="center">
  <strong>CONTINUUM：面向长时间运行 AI 智能体的可验证语义恢复。</strong>
  语义检查点（而非对话转储）、拒绝重复副作用的幂等动作账本，以及哈希链防篡改事件日志，全部通过默认拒绝的 MCP 服务器暴露。框架无关，支持 Python 3.11+。
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+" /></a>
  <a href="https://pypi.org/project/continuum-agent/"><img src="https://img.shields.io/pypi/v/continuum-agent?style=flat-square&label=PyPI" alt="PyPI" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-blue?style=flat-square" alt="License" /></a>
  <a href="https://pydantic.dev"><img src="https://img.shields.io/badge/pydantic-v2-E92063?style=flat-square&logo=pydantic&logoColor=white" alt="Pydantic v2" /></a>
  <a href="https://continuum-nu-six.vercel.app/"><img src="https://img.shields.io/badge/website-live_demo-E06D53?style=flat-square" alt="Website Demo" /></a>
  <a href="https://github.com/Cyrax321/CONTINUUM/actions/workflows/ci.yml"><img src="https://github.com/Cyrax321/CONTINUUM/actions/workflows/ci.yml/badge.svg" alt="CI 状态" /></a>
  <a href="https://app.codecov.io/gh/Cyrax321/CONTINUUM"><img src="https://img.shields.io/codecov/c/github/Cyrax321/CONTINUUM?style=flat-square&logo=codecov" alt="Coverage" /></a>
</p>

<p align="center" style="margin-bottom: 6px;">
  <a href="https://continuum-nu-six.vercel.app/"><strong>访问 CONTINUUM 网站</strong></a>
</p>

<p align="center" style="margin-top: 6px;">
  <a href="https://app.ona.com/#https://github.com/Cyrax321/CONTINUUM"><img src="https://ona.com/build-with-ona.svg" alt="Build with Ona" /></a>
</p>

<p align="center">
  <sub>如果 CONTINUUM 帮助你的智能体可靠恢复，请为本仓库点亮 Star，这能帮助更多人发现它并持续带来优质的 good first issue。</sub>
</p>

<p align="center">
  <sub><a href="README.md">English</a> | <strong>简体中文</strong> | <a href="README.es.md">Español</a> | <a href="README.ja.md">日本語</a> | <a href="README.pt-BR.md">Português</a> | <a href="README.ko.md">한국어</a></sub>
</p>

---

## 目录

[为什么](#为什么) · [快速开始](#快速开始) · [工作原理](#工作原理) · [CONTINUUM 的位置](#continuum-的位置) · [功能特性](#功能特性) · [安全扩展](#安全扩展) · [实证验证](#实证验证) · [MCP 集成](#mcp-集成) · [框架集成](#框架集成) · [核心概念](#核心概念) · [架构](#架构) · [API 和 CLI](#api-和-cli) · [路线图](#路线图) · [CONTINUUM 不是什么](#continuum-不是什么) · [相关工作](#相关工作) · [状态与局限](#状态与局限) · [贡献](#贡献) · [许可证](#许可证)

---

## 为什么

现代 AI 智能体执行长时间任务（数百次 LLM 调用、工具调用、文件和数据库写入）。当它们崩溃时，常见的处理方式是从头开始重放，这会重复工作、重复副作用、浪费 token 并丢失决策。

CONTINUUM 提出一个更窄但更难的问题：智能体能否从任务状态的紧凑语义表示中恢复，同时独立验证该状态在当前环境中仍然有效？其差异化体现在三部分：

- **语义检查点**：所需继续执行的最小化、带版本的紧凑表示，而非对话转储。
- **独立的环境重验证**：恢复前每个检查点组件都会对照当前环境进行验证，过期状态会通过依赖图传播。
- **可溯源的状态**：每个事实都可追溯到其来源，因此智能体报告的进度永远不会自我认证。

## 快速开始

以 `continuum-agent` 0.1.0 发布至 PyPI，执行 `pip install continuum-agent` 即可（固定版本请用 `pip install continuum-agent==0.1.0`）。发布标签还会将构建好的 wheel 附加到 [GitHub Releases](https://github.com/Cyrax321/CONTINUUM/releases)。

零配置路径（无需克隆、无需安装、无需发布）：

| 路径 | 方法 |
|:--|:--|
| 从 PyPI 安装 | `pip install continuum-agent==0.1.0`，然后执行 `continuum --help` |
| 端到端观看崩溃恢复 | `docker run --rm ghcr.io/cyrax321/continuum` |
| 通过 Docker 使用 CLI | `docker run --rm ghcr.io/cyrax321/continuum continuum --help` |
| 无需克隆即可运行 CLI | `uvx --from git+https://github.com/Cyrax321/CONTINUUM.git continuum --help` |
| Windows PowerShell（在克隆中） | `powershell -ExecutionPolicy Bypass -File .\try-it.ps1` 或 `powershell -ExecutionPolicy Bypass -File .\try-it.ps1 cli --help` |
| 浏览器中的完整开发环境 | [![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/Cyrax321/CONTINUUM?quickstart=1) |

Docker 镜像由 CI 在每次推送到 `main` 和每个发布标签时发布到 GHCR（`.github/workflows/docker-publish.yml`）。Codespace 在 `.devcontainer/` 中定义。

```bash
git clone https://github.com/Cyrax321/CONTINUUM.git
cd CONTINUUM

uv venv && source .venv/bin/activate     # macOS / Linux; Windows: .venv\Scripts\activate

# 贡献者（推荐）：库 + CLI + 全部测试工具 + 全部适配器
uv pip install -e ".[dev]"

# 或按需选择：.（最小）、[mcp]、[otel]、[langgraph]、
# [openai]、[langchain]、[attest]、[postgres]

# 或完全跳过克隆：
uv pip install git+https://github.com/Cyrax321/CONTINUUM.git
uv pip install "continuum-agent[mcp] @ git+https://github.com/Cyrax321/CONTINUUM.git"
```

> **pip 回退：** 将上面所有命令中的 `uv pip install` 替换为 `pip install`。

验证：

```bash
continuum --help                 # CLI 入口
continuum-mcp --help             # MCP 服务器入口（需要 [mcp] 或 [dev]）
pytest -q                        # 约 1,380 个用例被收集（具体数量和跳过数因环境而异）
ruff check src/ tests/ examples/ && ruff format --check src/ tests/ examples/
mypy src/continuum               # CI 强制的三扇门禁
```

核心库只有一个运行时依赖（`pydantic>=2.7`），其余均为可选。完整的包映射、extras 矩阵、Postgres 测试配置和按命令验证说明见 [references/install.md](references/install.md)。

### 两分钟接入编码智能体

对于 Claude Code、Gemini CLI 或 Codex，你无需编写 Python 也无需提示词文件：

```bash
continuum start my-task --goal "智能体应该做什么"
continuum hooks install claude-code --with-gate   # 同样支持：gemini、codex
```

此后智能体写入的每个文件都会被捕获为哈希链证据，会话开始时自动获得状态简报，在 `.continuum/gate.json` 中注册的未声明副作用会在触发前被拒绝，而任何崩溃后的全新会话都会带着可执行的下一步恢复。无需 CLAUDE.md。

最小化库示例，记录与恢复：

```python
from continuum import EventType, Run, SQLiteStorage, project

store = SQLiteStorage("agent.db")
store.create_run(Run(run_id="run_4821", goal="分析 10,000 份文档"))
store.append_event("run_4821", EventType.RUN_STARTED, {"goal": "分析 10,000 份文档", "total": 10_000})

for i, doc in enumerate(documents):
    analyze(doc)
    store.append_event("run_4821", EventType.WORK_COMPLETED, {"doc": i})

# 崩溃后，新进程从停止的地方精确接续：
state = project("run_4821", store.read_events("run_4821"))
print(state.progress.completed)            # 已完成，不会重复
print(store.verify_events("run_4821").ok)  # True，崩溃后链仍然完整
```

**亲自运行验证：**

```bash
python examples/crash_recovery_agent.py   # 真实进程杀死，真实副作用
python examples/context_compaction.py     # 转录丢失，检查点存活
python examples/model_switch.py           # 模型 A 死亡，模型 B 安全接管
python scripts/mcp_smoke.py               # 真实子进程，真实 JSON-RPC 流量
```

`e2e-autonomy-test/` 套件脚本化了一个真实的发票批量任务、在运行中硬杀，以及全新的恢复会话，然后在带外对 outbox、账本和事件链进行评分。Run 1 对真实的 Claude Code 会话取得了 **7/7 机制** 评分。完整 walkthrough 见 [references/e2e.md](references/e2e.md)。

## 工作原理

CONTINUUM 将 **LLM 上下文**（临时）与 **持久任务状态**（永久）分离开来。它不保存对话历史，而是构造语义检查点，即继续执行所需的最小化已验证信息。

![CONTINUUM 工作原理](docs/assets/architecture.svg)

详细说明、投影模型和恢复上下文见 [references/architecture.md](references/architecture.md)。

## CONTINUUM 的位置

四个关注点在每个长时间运行的智能体中重叠。CONTINUUM 只拥有最后一项，并通过显式接缝触及其他三项。不点名任何竞品，也不做没有已交付模块或已发布套件支撑的主张。

| 层 | 回答的问题 | 如何连接（已交付模块或已发布输出） |
|:--|:--|:--|
| Harness | 智能体如何调用工具并朝目标推进？ | 在 CONTINUUM 之外。接线点在 `src/continuum/adapters/generic.py`（`GenericAgentAdapter`）、`src/continuum/adapters/thin.py`（CrewAI、AutoGen、Pydantic AI 钩子）、`src/continuum/mcp/server.py`（MCP stdio）、`src/continuum/hooks.py` 和 `src/continuum/clienthooks.py`（编码 CLI 生命周期钩子）、`src/continuum/gateway.py`（面向任意语言的强制 HTTP 代理）和 `src/continuum/otel.py`（OpenTelemetry 桥）中交付。配方见 `docs/recipes/` 和 `references/adapters.md`。 |
| 持久执行 | 崩溃前发生了什么，哪些工作可以在不丢失的情况下重放？ | 哈希链事件日志 `src/continuum/events.py` 带 `verify()` 和 `trusted_through`，持久存储 `src/continuum/storage/sqlite.py`（WAL，`synchronous=FULL`，schema v6）和 `src/continuum/storage/postgres.py` 加上 `src/continuum/storage/migrations.py`，策略驱动的检查点 `src/continuum/checkpoint/manager.py` 和 `src/continuum/checkpoint/policy.py` 在 `restore()` 时重放间隔。Walkthrough 在 `docs/recovery_walkthrough.md`（`examples/recovery_walkthrough.py` 的输出）中。 |
| 控制面 | 哪个 run 处于活动状态、谁可以操作它、输出去向何处？ | Run 注册表和父子层级 `src/continuum/storage/` 与 `src/continuum/recovery/family.py`（`continuum tree`），allowlist 鉴权 `src/continuum/mcp/authz.py`（`CONTINUUM_MCP_MUTATING_CLIENTS` / `CONTINUUM_MCP_TOKEN`），展示面 `src/continuum/dashboard/app.py` 和 `src/continuum/serve/server.py`，CLI `src/continuum/cli/main.py`（`continuum runs`、`continuum tree`、`continuum health`）。 |
| 验证基座 | 给定时间 T 的检查点和当下的世界，继续是否仍然安全和正确？ | `src/continuum/state/validator.py`（过期性 `dependency -> evidence -> finding -> decision` 加上 `PlanStep.depends_on`）、`src/continuum/provenance_map.py`（`Origin` 到 `REQUIRES_REVIEW` 直至 `REVIEW_CONFIRMED`）、`src/continuum/actions/ledger.py` 配合 `src/continuum/actions/idempotency.py` 与 `src/continuum/gate.py` / `src/continuum/gateway.py`（先声明再触发，拒绝重复并为对账抛出 `UnknownSideEffect`）、`src/continuum/replayguard.py`（可移植守卫）、`src/continuum/pinning.py` 与 `src/continuum/replay_similarity.py`（重放正确性）、`src/continuum/budgets.py`（重试上限）、`src/continuum/recovery/engine.py` + `src/continuum/recovery/contract.py` + `src/continuum/recovery/planner.py` + `src/continuum/recovery/observations.py`（最大严重度 `RESUME < ... < ABORT`，带 `evidence` / `reason` / `next_allowed_action` / `human_steps` 的密封合约）、`src/continuum/checkpoint/rewind.py`（原子双状态回滚）、`src/continuum/analysis/prefix_trust.py`（建议性信任）。已发布的检查：`docs/recovery_walkthrough.md`、`benchmarks/fault_injection/`（打印 `detection_rate` / `unsafe_resume_rate` 的套件）、`src/continuum/benchmark/phase6/`（恢复正确性套件）、`docs/RESULTS.md` 以及下方可再生的可视化。 |

该表中的每一行都可在打标签的提交上追溯到 `main` 上存在的路径。表格中不复述任何基准数字，基准只活在已打印它们的套件输出中。完整的已发布套件和设计文档列表见 `docs/research.md`。

### 崩溃恢复，真实发生

下面的图片不是模型图。它是 `python demo-run/generate_crash_visual.py` 的输出，该脚本让 `demo-run/worker.py` 运行至第 399 篇文档时执行 `os._exit(9)`，调用 `continuum resume --env dataset=v4` 并展示拒绝路径（`REQUEST_HUMAN`、`safe:false`、exit 20），用探针调和不确定的副作用，然后从同一数据库恢复并在无重复工作的情况下完成。转录也保存为 `docs/assets/crash-recovery.txt` 供审计。

重新生成：

```bash
python demo-run/generate_crash_visual.py
# 或：python scripts/generate_crash_visual.py
```

![崩溃恢复：批量中硬杀、拒绝、调和、恢复](docs/assets/crash-recovery.svg)

带代码的完整 walkthrough 见 `docs/recovery_walkthrough.md`（`examples/recovery_walkthrough.py`）。最小化 bench harness 见 `references/bench.md`（`continuum benchmark`）。

## 功能特性

| 能力 | 它为你带来什么 |
|:--|:--|
| 语义检查点 | 紧凑、可版本化、可检查的状态，而非转录转储 |
| 幂等动作账本 | 拒绝重复的外部副作用，对不确定的结果进行对账展示 |
| 环境重验证 | 恢复前每个检查点组件都会对照当前世界进行验证 |
| 可溯源状态 | 智能体报告的进度被标记为 `REQUIRES_REVIEW`，永不自我认证 |
| 恢复引擎 | 七种恢复模式，带有确定性的密封下一步合约 |
| 默认拒绝的 MCP 服务器 | 十一个工具，读写与变更分离，调用者 allowlist |
| 框架适配器 | 通用 Python、OpenAI Agents SDK、LangGraph 和 LangChain 集成 |
| 安全规划循环 | 双信号观测验证将高风险分支提升至 REQUIRES_REVIEW |
| 周期性重验证 | 按计划重新检查环境，在一个周期内捕获运行中漂移 |
| 防篡改日志 | 哈希链事件日志（36 种事件类型）带完整性验证 |
| 强制门控 | 未声明的副作用调用在触发前被拒绝，拒绝信息会教授声明协议 |
| 观测钩子 | 编码 CLI 写入的每个文件都会成为摘要验证的证据，位于模型控制之外 |
| 会话简报 | 全新会话在开始时确定性地学习运行状态，包括上一会话的推理摘要 |
| 调和探针 | 已注册的命令自动结算不确定的副作用，人类只看到其余部分 |
| 可执行指引 | Resume 和 validate 将下一步渲染为可运行的命令，而非状态 |
| 强制 HTTP 网关 | 任何语言的外发调用都需要声明，网关从真实状态码结算 |
| OpenTelemetry 桥 | 来自生产追踪的工具调用跨度零代码改动成为证据 |
| 动作索引 | 跨 run 的幂等查询是索引读取，而非全日志扫描 |
| 版本钉扎 | 调用者断言的 prompt、工具和模型哈希按声明存储，漂移在恢复时显露 |
| 重试预算 | 按动作类型在声明时强制执行尝试上限，智能体可看到剩余次数 |
| 多智能体父子 | 父级恢复组合家庭最差状态，不确定的子级会阻塞父级 |
| 带信息重试 | 引擎撰写的失败摘要被注入到恢复后的重试中 |
| 分支语义 | 发散的延续分支为带有全新权威的子 run |
| 日志压缩 | 预锚点前缀按原文归档，活跃日志为长达数月的运行保持有界 |
| 已消耗授权追踪 | 单次授权引用在终态时被标记为已消耗，恢复后重用会被拒绝（`GRANT_DENIED`），防御检查点恢复路径上的授权复活 |
| 链证明 | `continuum attest` 使用 Ed25519 签署 run 的链头，外部验证者可证明历史在已知密钥下未被篡改 |
| HITL 仪表板 | 确认、对账和完成按钮，与 CLI 保持审计一致性 |

## 安全扩展

两个增量式安全扩展位于恢复和检查点基座之上。它们不改变恢复、重放或现有的崩溃时重验证路径。

- **安全规划循环**：观测携带可溯源性并由两个独立信号验证（`verified` / `unverified` / `contested`）。由未验证或有争议观测所门控的计划分支会被提升至 `REQUIRES_REVIEW`。决策作为 `PERCEPTION_OBSERVED` 和 `BRANCH_RESOLVED` 事件追加到账本。
- **周期性重验证**：按步骤间隔（默认 25）和应用切换时复用恢复引擎，因此运行中环境漂移在一个周期内就会被捕获，而不是等到下一次崩溃。

见 [docs/PROBLEM.md](docs/PROBLEM.md)、[docs/RESULTS.md](docs/RESULTS.md) 和 [STATUS.md](STATUS.md)。

## 实证验证

CONTINUUM 针对真实 LLM 智能体、真实协议边界和硬进程崩溃进行验证，而不仅仅是模拟单元测试。

- **真实智能体**：多会话 Claude Code 发票批次在运行中 `SIGKILL`，在机制上取得 7/7 评分，恢复会话查询了 `continuum_resume`、通过两阶段账本路由副作用、拒绝重复已验证写入并尊重 `request_human`。真实测试暴露了 prompt 漂移去重缺口，已通过 `ActionLedger.claim()` 中的规范路径归一化和基于 token 的回退关闭。
- **第三方客户端**：Gemini CLI 和 Kilo Code 通过 stdio JSON-RPC 对接真实 SQLite 存储，验证多智能体共存和鉴权隔离。
- **协议合规**：使用 `@modelcontextprotocol/inspector --cli` 在进程死亡间端到端驱动，变更工具默认拒绝并位于 `CONTINUUM_MCP_MUTATING_CLIENTS` 后，外部声明降级为 `REQUIRES_REVIEW`（`safe: false`）。
- **自愈**：硬杀的服务器在启动时通过单次重试清理孤立的 SQLite `-wal`/`-shm` 伴生文件来恢复。
- **规模**：约 1,380 个测试被收集（约 1,360 通过，其余在缺少可选服务时跳过），覆盖 Python 3.11、3.12 和 3.13（单元、`hypothesis` 属性测试、并发、对抗）。CONTINUUM-Bench 运行五个崩溃场景加一个专门的参数漂移场景，对 CONTINUUM 测量到 0 重复工作和 0 重复副作用，而对朴素重放则为完全重复，另有一个 12 场景恢复正确性套件（`continuum.benchmark.phase6`）将持久执行调研中的崩溃点编码为可执行断言。
- **对抗审计**：完整 MCP 面已在真实协议上被审计，发现并修复了三个缺陷。方法和复现步骤见 [test.md](test.md)。

## MCP 集成

CONTINUUM 交付 MCP 服务器，因此智能体可以在不嵌入库的情况下记录进度、打检查点并通过账本路由外部副作用：

```bash
uv pip install -e ".[mcp]"
CONTINUUM_MCP_MUTATING_CLIENTS=your-client-name continuum-mcp
```

通过 stdio 的十一个工具。其中三个是只读的（`continuum_validate`、`continuum_resume`、`continuum_list_actions`），八个会变更。副作用采用两阶段（声明、执行、完成），变更工具默认位于 allowlist 之后。智能体报告的状态以 `Origin.EXTERNAL_AGENT` 可溯源性记录并标记为 `REQUIRES_REVIEW`。

验证细节，包括启动时的崩溃恢复和端到端 Claude Code 测试，见 [references/mcp.md](references/mcp.md)。如果已注册的服务器报告 `CONNECTION_CLOSED`，原因几乎总是 `PATH` 解析而非服务器本身：[docs/api/mcp.md](docs/api/mcp.md#troubleshooting) 有诊断和两种修复方法。

## 框架集成

`src/continuum/adapters/` 中交付九个适配器（一个进程内门面加上八个集成），全部为可选安装，因此核心保持仅标准库：

| 适配器 | 类 | 说明 |
|:--|:--|:--|
| 通用 Python 智能体 | `GenericAgentAdapter` | 进程内门面，写入可信（`Origin.DETERMINISTIC`）状态。 |
| 文件系统沙箱 | `FilesystemSandboxAdapter` | 本地目录沙箱，无外部服务，文档和 CI 的默认值。 |
| Python 进程内 | `PythonInProcAdapter` | 在临时工作目录中运行 Python，通过账本记录。 |
| 容器 | `ContainerAdapter` | Docker 后端，当 `docker` 缺席时受保护跳过。 |
| 浏览器 | `BrowserAdapter` | Playwright 后端，未安装时受保护跳过。 |
| Kubernetes | `KubernetesAdapter` | `kubectl` 后端，未配置时受保护跳过。 |
| OpenAI Agents SDK | `OpenAIAgentAdapter` | 实验性。钩入 `ToolContext` / `RunHooks`，可选 `openai-agents`。 |
| LangGraph | `LangGraphAgentAdapter` | 实验性。包装 `StateGraph`，可选 `langgraph`。 |
| LangChain | `LangChainAgentAdapter` | 实验性。将 `checkpoint_node` 放入 LCEL `Runnable` 流水线和 `create_agent` 工具调用循环，可选 `langchain`。 |

每个适配器都通过账本记录进度，并通过两阶段拦截和完成协议路由外部效应。全部三个框架适配器都有端到端集成测试，并已针对 **真实 OpenRouter 模型** 驱动，在此过程中发现并关闭了 LLM 参数漂移去重缺口和两个 OpenAI 适配器缺陷，包括每个适配器一次真实硬崩溃（`os._exit(137)` 在副作用中）证明。每个适配器的完整用法、真实模型结果和可运行示例见 [references/adapters.md](references/adapters.md)。

生产级 LangGraph 应用也可以保留其原生持久化 API：`make_continuum_checkpointer(storage)` 实现了 LangGraph 的 `BaseCheckpointSaver` 并基于 CONTINUUM 存储，因此每次 put 都落在同一哈希链、可溯源的事件日志中（见 [references/adapters.md](references/adapters.md)）。

另外三个生产框架由 [`adapters/thin.py`](src/continuum/adapters/thin.py) 中无需 SDK 的薄钩子面覆盖：

| 框架 | 拦截面 | 入口 |
|:--|:--|:--|
| CrewAI | 全局工具调用前后钩子 | `install_crewai_hooks(storage, run_id)` |
| AutoGen core | 原地包装 `FunctionTool.run_json` | `wrap_autogen_tool(tool, storage, run_id)` |
| Pydantic AI | 异步 Hooks 能力 | `Agent(capabilities=[wrap_pydantic_ai_hooks(storage, run_id)])` |

对于这些都未覆盖的栈：`continuum gateway` 对来自任意语言的外发 HTTP 强制声明，`continuum.otel.make_span_processor(storage)` 将现有的 OpenTelemetry 工具跨度转为证据，而 `continuum serve` 以与 MCP 工具相同的操作通过语言无关的 JSON 线路协议暴露（stdio，或通过 `--transport http` 的 HTTP 并使用 `CONTINUUM_SERVE_TOKEN` 鉴权）。

### 恢复由智能体或 MCP 报告的运行

通过 MCP 或通过 OpenAI 适配器报告的状态携带 `Origin.EXTERNAL_AGENT` 可溯源性，并在确认前解析为 `request_human`。LangGraph 和 LangChain 运行使用 `Origin.DETERMINISTIC` 并直接恢复。要清除复审并恢复：

```bash
continuum confirm <run_id>   # 记录 REVIEW_CONFIRMED，然后重新评估
continuum resume <run_id>    # 现在报告 RESUME
```

在 MCP 上等价的是 `continuum_confirm` 工具后跟 `continuum_resume`。确认是一次性的、经人类证明的事件：自我认证安全的逃生舱，因此外部驱动的 run 永远不会永久卡住。

## 核心概念

每个概念的深度参考见 [references/concepts.md](references/concepts.md)。

- **语义检查点**，继续所需内容的紧凑、带版本的表示。
- **状态验证**，每个组件独立验证，过期性通过依赖图传播。
- **幂等动作账本**，外部副作用被追踪和去重，不确定的结果会抛出而非静默重试。
- **恢复模式**，`RESUME`、`REPAIR_AND_RESUME`、`ROLLBACK`、`WAIT`、`REQUEST_HUMAN`、`ABORT`（加上 `REPLAN`）。
- **恢复合约**，确定性的、完整性密封的、门控的下一步。

## 架构

CONTINUUM 围绕一个不变量组织：**每个事实都携带其来源，信任是挣得的，从不假设。** 为什么这对初创公司重要：运行数周的智能体不能在上下文丢失时丢失工作，也不能浪费 token、成本或对同一工具触发两次。

### 系统一览，通用适配器、单一日志、任意 Harness

任意 Harness 都接入同一哈希链日志。同一 run 可以由 Claude Code 写入、由 LangGraph 恢复、由 CLI 检查并在仪表板上批准。无需框架协作。

```text
  Claude Code ─┐
  Gemini CLI ──┤
  Codex ───────┤
  LangGraph ───┼── 5 个接缝 ──►  单一持久日志  ──►  恢复 + 仪表板 + CLI
  LangChain ───┤                （哈希链、        （密封合约、
  OpenAI SDK ──┤                 可溯源、          验证、健康、
  CrewAI ──────┤                 精确一次）        家族）
  任意 HTTP ───┤
  任意 OTel 应用┘

  接缝：1 进程内  2 MCP  3 CLI 钩子  4 网关  5 OTel
```

### 三大保证（演示逐一证明）

1. **无自我认证。** 智能体报告的状态为 `EXTERNAL_AGENT` 并降级为 `REQUIRES_REVIEW` 直至人类 `REVIEW_CONFIRMED`。只有可信写入者产生 `DETERMINISTIC` 状态。
2. **副作用需要声明。** 每个外部效应在触发前在幂等账本中声明。未声明的效应在边界被阻止，重复被拒绝，不确定的结果会抛出以供调和。
3. **恢复对照现实进行验证。** 恢复前会检查文件摘要、依赖版本和模型身份，然后再说安全。过期性传播 `dependency -> evidence -> finding -> decision` 加上 `PlanStep.depends_on`，因此只有受影响的步骤需要修复。

### 五个集成接缝

| 接缝 | 如何连接 | 它为你带来什么 |
|:--|:--|:--|
| 1 进程内 | `GenericAgentAdapter.intercept_action(...)` 和 `wrap_tool(key_fn=...)` 用于 LangChain、LangGraph、OpenAI Agents SDK | Python 框架，可信写入 |
| 2 MCP 服务器 | `continuum-mcp` 通过 stdio 的 12 个工具（`continuum_record_progress`、`continuum_intercept_action`、`continuum_complete_action` 等） | 任意支持 MCP 的客户端，3 个只读 + 8 个变更，allowlist `CONTINUUM_MCP_MUTATING_CLIENTS` |
| 3 CLI 生命周期钩子 | `continuum hooks install claude-code --with-gate` 也支持 `gemini` 和 `codex` | 编码 CLI：`SessionStart briefing`、`PostToolUse observe`、`PreToolUse gate`，无需 CLAUDE.md |
| 4 强制 HTTP 网关 | `continuum gateway --port 8765` 配合 `.continuum/gateway.json` | 任意语言，任意外发 HTTP 必须有声明，网关从真实状态码结算 |
| 5 OpenTelemetry 桥 | `make_span_processor(storage)` | 任意已追踪应用，跨度成为 `TOOL_COMPLETED` 证据 |

面向 CrewAI、AutoGen、Pydantic AI 的薄钩子面位于 `adapters/thin.py`，无需 SDK。

### 强制流水线，为何无重复且无无效调用

门控到观测的流水线在 Harness 边界关闭缺口。这正是节省 token 和成本并阻止无效工具调用的原因。

```text
PreToolUse 钩子                    PostToolUse 钩子
    |                                    |
    v                                    v
continuum gate                    continuum observe
    |                                    |
    |-- 无声明？拒绝（exit 2）          |-- TOOL_COMPLETED 事件：
    |   + 声明指引                      |     路径、字节、当前磁盘上的 sha256
    |                                    |
    |-- 存在有效声明？放行                |-- 磁盘检查状态：
    |                                    |     已验证 / 已变更 / 缺失
    v
智能体执行效应
    |
    v
continuum_complete_action  （从现实结算，而非从报告）
    |
    v
账本标记为 COMPLETED，下一次重放返回缓存结果而非第二次触发
```

未知主机被默认拒绝为失败关闭，而非开放中继。Shell `Bash/curl` 是文档化的 v1 盲点。

### 恢复决策树，数周直至完成，正确且精确

引擎采纳最谨慎的信号，因此安全永远胜过便利。

```text
RESUME < REPAIR_AND_RESUME < REPLAN < WAIT < REQUEST_HUMAN < ROLLBACK < ABORT
```

每个 `continuum resume` 都会返回密封合约，包含：恢复状态和 `safe`、已验证和已失效组件、可执行的 `human_steps`（要运行的精确 shell）、检查点后观测的磁盘检查、钉扎漂移，以及面向多智能体的 `continuum tree` 家族聚合。简报 `continuum briefing` 在每个全新的 `claude` SessionStart 时注入该合约，因此在你杀掉终端后说 `hi` 也会从最后一个良好前缀恢复。

### 为何这能节省 token、成本和无效调用

* **Token：** 语义检查点存储 `Goal + Plan + Progress` 而非转录转储。简报仅提供已验证状态加上 4096 字符上限的推理摘要，而非导致下一会话退化的错误尾部。带信息的重试 `recovery/summary.py` 注入引擎撰写的摘要，而非原始历史。
* **成本：** 账本 `action_index` 即使在参数漂移如相对与绝对路径（`invoice:INV-001` 稳定键）下也拒绝重复副作用，因此同一 API 在恢复后不会被二次付费。预算 `budgets.py` 在声明时限制重试风暴。`continuum benchmark` 为 continuum 打印 `0 重复`，而对朴素方案则为 `50`。
* **无效调用：** 门控、网关和 `replayguard` 的 `langgraph_protected_node` 在触发前阻止未声明或被重放的工具调用。钉扎 `pinning.py` 在恢复时显露 prompt 或工具漂移。

### 存储架构

Schema v6。SQLite 为主，Postgres 经 CI 验证。单一日志，多重投影。

| 表 | 用途 |
|:--|:--|
| `events` | 哈希链仅追加日志（v0.2 中 44 种事件类型） |
| `runs` | Run 元数据，带 `parent_run_id` 用于多智能体 |
| `versions` | 每个检查点的 SemanticState 快照 |
| `checkpoints` | 带 `RECOVERY` 锚点的密封检查点记录 |
| `action_index` | 跨 run 幂等投影（schema v3+），索引读取而非全表扫描 |
| `events_archive` | 压缩前缀存储（schema v5+），`continuum compact` 为长达数月的运行限制活跃日志 |
| `lg_checkpoints` / `lg_writes` | LangGraph 原生持久化（schema v4+），`make_continuum_checkpointer(storage)` |

### 模块映射，一库多面

CONTINUUM 是一个库（`src/continuum`，104 个模块）加上大型测试套件（98 个测试文件，约 1,380 个测试）。所有模块追加并重放同一个哈希链事件日志：

| 模块 | 职责 |
|:--|:--|
| `events.py` | 仅追加、哈希链事件日志和 `verify() trusted_through` |
| `state/` | 投影 `project()`、验证、提取，过期性传播 |
| `storage/` | `SQLiteStorage` v6、`postgres.py`、`migrations.py`、`actionindex.py` |
| `actions/` | 幂等账本 `claim/complete/reconcile`、`idempotency.py` 键与规范化与 token 回退、已消耗授权追踪 `GRANT_DENIED` |
| `checkpoint/` | 策略驱动检查点 `manager.py` `policy.py` 带 `RECOVERY` 锚点和 `prune` |
| `recovery/` | 引擎、规划器、密封合约 `contract.py`、`guidance` `human_steps`、`observations` 磁盘检查、`family` 聚合、`fork` 语义、`summary` 带信息重试 |
| `gate.py` | 工具前强制：基于账本声明放行或拒绝 |
| `gateway.py` | 强制 HTTP 代理：外发请求需先声明 |
| `replayguard.py` | 可移植守卫：`evaluate、protected_call、langgraph_protected_node`，关闭 ACRFence 重放风险 |
| `hooks.py` `clienthooks.py` | 共享检查点钩子和安装器档案 `claude-code gemini codex` |
| `budgets.py` | 按动作类型重试预算注册与评估 |
| `pinning.py` | 恢复时版本钉扎归一化与漂移检测 |
| `replay_similarity.py` | 重放与分支的语义相似度后端 exact、fuzzy、embedding |
| `reconcilers.py` | 探针注册表 `.continuum/reconcilers.json` 用于自动结算 |
| `adapters/` | 9 个类适配器 + 薄钩子 `thin.py` 覆盖 CrewAI、AutoGen、Pydantic AI + LangGraph 存储 |
| `mcp/` | 12 个 stdio 工具加上鉴权 `authz.py` token 鉴权、allowlist、确认 token |
| `serve/` | Sidecar stdio JSON 线路 + HTTP `CONTINUUM_SERVE_TOKEN` |
| `dashboard/` | Web 仪表板 `app.py` `hitl.py` 带 HITL 按钮确认、对账和完成，前缀信任建议，钉扎 |
| `cli/` | 38 个 argparse 命令，退出码即裁决，`runs、start、inspect、resume、verify、health、tree、benchmark、attest、dashboard` |
| `otel.py` | OpenTelemetry 跨度处理器桥 |
| `benchmark/` | CONTINUUM-Bench  harness，5 个崩溃场景 + 参数漂移 + 12 场景恢复套件 |

### 诚实的局限

- 门控无法洞察 shell 命令内部（Bash 和 curl 绕过结构化工具声明）
- Postgres 后端经 CI 测试但尚未在生产中久经考验
- 尚无针对 `request_human` 通知的 webhook 外发（#305）
- v1 仅支持一层多智能体层级
- 大负载卸载（#254）尚未实现
- 数周尺度的基准与 token 成本表落在 #550 看板（#568 至 #570）

完整参考见 [references/architecture.md](references/architecture.md)。而构建于此之上的数月平面，溯源因果图、授权复活、可采纳性、活性，均作为看板 #550 及其 20 个子 issue #551 至 #570 被钉住。

## API 和 CLI

Python 接口（`EventType`、`Run`、`SQLiteStorage`、`diff_states`、`project`）和适配器 API 在 [references/api.md](references/api.md) 中配有可运行示例。CLI 是同一接口的 shell 形式：

```bash
continuum runs                                   # 列出 runs
continuum inspect <run_id>                       # 语义状态
continuum validate <run_id> --env dataset=v4     # 验证，只读
continuum resume <run_id> --env dataset=v4       # 恢复决策 + 合约 + 下一步
continuum checkpoint <run_id>                    # 强制检查点，会变更
continuum actions <run_id>                       # 外部副作用
continuum reconcile <run_id>                     # 使用探针结算不确定效应
continuum complete <run_id>                      # 从键盘将 run 关闭为完成
continuum verify <run_id>                        # 重新审计事件哈希链
continuum budget <run_id>                        # 按动作类型的重试预算使用
continuum compact <run_id>                       # 归档预锚点日志前缀
continuum tree <parent_run_id>                   # 展示父级 + 子级及恢复状态
continuum attest <run_id> --key signer.pem       # 为外部验证者签署链头
```

所有接线都在主机侧，模型的配合是可选的：

```bash
continuum hooks install claude-code --with-gate   # 编码 CLI：证据、简报、门控
continuum gateway --port 8765                     # 面向其他一切的强制 HTTP 代理
provider.add_span_processor(continuum.otel.make_span_processor(storage))  # OTel 转证据
continuum-mcp                                     # 任意支持 MCP 的端：十一工具服务器
continuum briefing                                # 会话开始上下文注入
continuum budget <run_id>                        # 重试预算使用报告
continuum tree <parent_run_id>                   # 多智能体层级视图
```

可选注册表位于代码旁且是数据而非代码：`.continuum/gate.json`（副作用工具 + 稳定键模板）、`.continuum/reconcilers.json`（检查外部系统的探针）、`.continuum/gateway.json`（上游路由）。

每个命令都接受 `--json`，且只读命令永不写入，因此在智能体运行中对活跃数据库也是安全的。退出码是安全合约（只有经验证安全的 run 才以 0 退出）。完整命令列表、退出码表和状态差异输出见 [references/cli.md](references/cli.md)。

## 路线图

| 阶段 | 组件 | 状态 |
|:--:|:--|:--|
| 1-11 | 数据模型、语义状态、持久化、检查点、验证、动作账本、恢复引擎、CLI、崩溃恢复示例、环境快照和差异、框架适配器 | 完成 |
| 12 | 基准套件（CONTINUUM-Bench） | 完成（最小化 harness） |
| 13 | 云 API（FastAPI + PostgreSQL） | 部分：PostgreSQL 存储后端和 HTTP sidecar 传输（`continuum serve --transport http`）已交付并经 CI 测试，托管的多租户服务尚未开始 |
| 14 | 仪表板 | 完成（`continuum dashboard`） |
| 15+ | 强制持久化：观测钩子、门控、会话简报、调和探针、强制网关、OTel 桥、动作索引、可执行指引、多客户端安装器、语义重放检测、版本钉扎、重试预算、日志压缩、HITL 面、分支语义、带信息重试、多智能体聚合 | 完成（见 issue #213） |
| 下一步 | 数月尺度持久平面：里程碑锚定计划（#312）、结构化尝试记忆（#313）、原子双状态回滚（#292）、公开恢复正确性基准（#293）、webhook 外发通知（#305） | 规划中（草案规格见 [docs/UPGRADE_SPEC.md](docs/UPGRADE_SPEC.md)） |

在原始计划之外：MCP 服务器、MCP 鉴权和调用者认证层、可溯源性和反自我认证、社区文件、带前向迁移的 schema 版本化、有界恢复上下文、已消耗授权追踪、Ed25519 事件链证明、原生 LangGraph 检查点器，以及每次推送到 `main` 时的 wheel 产物均已交付。见 [STATUS.md](STATUS.md) 中的已验证与被认为的细分以及开放的正确性缺陷。

## CONTINUUM 不是什么

| 不是这个 | 而是这个 |
|:--|:--|
| LLM | 面向使用 LLM 的智能体的可靠性层 |
| 智能体框架 | 可插入任意框架的恢复层 |
| 向量数据库 | 结构化语义状态，而非嵌入 |
| RAG 系统 | 已验证的检查点，而非检索增强记忆 |
| 工作流引擎 | 恢复层，而非编排器 |

核心抽象：`语义状态 + 环境验证 + 动作对账 = 安全恢复`。

## 相关工作

CONTINUUM 位于持久执行、幂等副作用追踪和针对 LLM 智能体的崩溃恢复的交叉点。最近的邻域是经机器检查的恢复合约（Khan 2026）、带约束门控准入的智能体事务处理（Mnemosyne 2026）、检查点回滚攻击分析（ACRFence 2026）和设计级 prompt 注入防御（CaMeL 2025）。完整的带注释列表、基础和引用审计见 [references/related-work.md](references/related-work.md)。

## 状态与局限

- **已测试**：在 2026-08-24 对本树的完整运行中为 1,360 通过 + 23 跳过，CI 在 Python 3.11、3.12 和 3.13 上强制执行套件，计数因平台和 Postgres 等可选服务而异（见 [STATUS.md](STATUS.md)）。MCP 面也已在真实协议上被对抗性审计，见 [test.md](test.md)。
- **在 PyPI 上为 `continuum-agent` 0.1.0**（`pip install continuum-agent`，克隆仍可通过 `pip install .` 见 Quick Start）。
- **MCP 调用者认证按部署可选。** 当设置 `CONTINUUM_MCP_TOKEN` 时，服务器会拒绝每个变更工具，除非调用者在 `initialize` 握手的 `_meta.authToken` 中出示该共享密钥，通过 `CONTINUUM_MCP_CLIENT_TOKENS`（`name:secret` 对）支持按调用者的密钥。未配置任何 token 时，鉴权仅按声明身份（历史默认值，为本地单用户使用保留）。
- **通过 MCP 确认自我报告状态需要单独的密钥。** `continuum_confirm` 会拒绝每个调用者，直至操作员设置 `CONTINUUM_MCP_CONFIRM_TOKEN`，因为被允许记录进度的智能体不能同时被允许确认它。默认路径保持人类驱动：在主机上运行 `continuum confirm <run_id>`。
- **未构建组件**：云 API（阶段 13）。
- **Shell 命令强制缺口**：门控对结构化工具调用强制声明，但无法洞察 Bash 和 curl 命令内部。文档化为 v1 范围拒止。
- **框架适配器仍为实验性。** 全部三个框架适配器现在都携带真实模型的软恢复和硬崩溃证明（OpenRouter，`gpt-4o-mini`），包括阻塞在不确定副作用上的崩溃合约，并且现在拥有与通用门面一致的崩溃与恢复验证测试（Refs #285）。生产恢复请优先使用 `GenericAgentAdapter`。
- **智能体和 MCP 运行在自动恢复前需要显式确认。** 外部报告的状态为 `REQUIRES_REVIEW`，因此 `continuum resume` 会返回 `request_human` 直至人类确认。按设计如此，不是缺陷，见 [框架集成](#框架集成)。
- **e2e 自主测试系列**（issue [#6](https://github.com/Cyrax321/CONTINUUM/issues/6)）：三轮完整 Claude Code 运行在机制上取得 7/7 评分，观察到无提示的恢复行为。跨多样 prompt 风格的进一步迭代仍开放。

## 关于

在 2026 年初，我看到长时间运行的智能体在恢复而非推理上失败。检查点被视为继续的证明，而非待验证的证据。调研 Temporal、LangGraph、ACRFence 2603.20625 和 self conditioning 2509.09677 后，我发现缺口是一个可移植的验证基座，它会问：给定时间 T 的状态和当下的世界，继续是否仍然安全。

在三周内，我从一个不变量出发构建了 CONTINUUM：每个事实都携带其来源。结果是一个带 `verify()` 的哈希链日志、带稳定键去重的账本、阻止未声明效应的门控与网关，以及密封合约的恢复引擎。五个接缝将同一日志暴露给 Claude Code、LangGraph、LangChain、OpenAI、HTTP 和 OpenTelemetry。经真实杀死和 1380 个测试验证，它在朴素重放打印 `50` 的地方打印 `0 重复`。

CONTINUUM 由 **Anandhu P Shaji**（[@Cyrax321](https://github.com/Cyrax321) · [LinkedIn](https://www.linkedin.com/in/anandhupshaji/)）创建并由原始创建者维护。基于 [Apache-2.0](LICENSE) 开源。社区贡献欢迎通过 [CONTRIBUTING.md](CONTRIBUTING.md)，并在 [AUTHORS.md](AUTHORS.md) 和 [graphs/contributors](https://github.com/Cyrax321/CONTINUUM/graphs/contributors) 中致谢。

## 贡献

本项目基于 Apache 2.0 开源，并有意构建为可扩展：面向验证恢复语义的研究者、面向将账本或 MCP 服务器移植到其他框架或语言的工程师，以及面向将规划路线图变为现实的任何人。一个好的起点是 issue 跟踪器上的 `good first issue` 标签，或 STATUS.md 中列出的开放正确性缺陷。

在提交大型 PR 前请先开 issue。完整贡献指南见 [CONTRIBUTING.md](CONTRIBUTING.md)，包括 [Code of Conduct](CODE_OF_CONDUCT.md)。

### 贡献者

<a href="https://github.com/Cyrax321/CONTINUUM/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=Cyrax321/CONTINUUM" />
</a>

## 赞助

如果 CONTINUUM 帮助你的智能体可靠恢复，请考虑赞助以支持长期维护。

<p align="center">
  <a href="https://github.com/sponsors/Cyrax321"><img src="https://img.shields.io/badge/Sponsor-❤-ff69b4?style=for-the-badge&logo=githubsponsors" alt="Sponsor Cyrax321" /></a>
</p>

<p align="center">
  <a href="https://github.com/sponsors/Cyrax321">成为赞助者</a>，GitHub Sponsors，或在 FUNDING.yml 中添加自定义链接如果你偏好其他平台。
</p>

## 许可证

Apache 2.0，见 [LICENSE](LICENSE)。

---

深度参考资料：

- [references/install.md](references/install.md) - 前置条件、安装层级、包映射、验证
- [references/concepts.md](references/concepts.md) - 语义检查点、验证、账本、恢复模式、合约
- [references/architecture.md](references/architecture.md) - 数据模型、事件日志、投影、存储、检查点、恢复引擎、安全
- [references/adapters.md](references/adapters.md) - 框架适配器用法和真实模型验证结果
- [references/api.md](references/api.md) - Python 和适配器 API
- [references/cli.md](references/cli.md) - 完整 CLI 命令列表、退出码、状态差异
- [references/mcp.md](references/mcp.md) - MCP 服务器状态、验证、开放问题
- [references/bench.md](references/bench.md) - CONTINUUM-Bench 设计
- [references/quickstart.md](references/quickstart.md) - 安装、示例、验证脚本
- [references/e2e.md](references/e2e.md) - 端到端自主测试 walkthrough
- [references/testing.md](references/testing.md) - 测试套件布局和约定
- [references/related-work.md](references/related-work.md) - 带注释的相关工作和引用审计
