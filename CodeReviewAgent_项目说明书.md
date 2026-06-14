# Adaptive Code Review Agent 项目说明书


## 1. 项目简介

Adaptive Code Review Agent 是一个面向代码评审流程的智能审查系统。项目基于大语言模型，结合静态分析工具、风险路由、多专家 Agent 协作、知识库记忆、冲突仲裁和 benchmark 评估，生成结构化代码审查报告。

项目最初采用全量多 Agent 并行审查方案，即 Security、Performance、Maintainability 三个专家 Agent 同时审查所有代码。但经过 benchmark 测试发现，全量多 Agent 并不天然优于单 Agent。在简单任务或单一领域问题上，多 Agent 会带来额外调用成本、误报增加和结果合并开销。

因此，项目被重新设计为自适应架构：

```text
简单代码 -> 单 Agent 快速审查
高风险代码 -> 按风险类型触发专家 Agent
混合风险代码 -> 多专家 Agent 协作
静态工具结果 -> 作为证据融合进最终报告
用户反馈 -> 写入知识库，影响后续审查提示
```

项目定位不是替代 CodeQL、Semgrep、SonarQube 等成熟静态分析器，而是构建一个面向代码评审流程的智能编排系统。

## 2. 背景与问题

规范代码评审通常围绕 Pull Request 或 Commit 变更展开，不只是检查语法错误，而是从多个维度判断代码是否可以合并。

常见评审维度包括：

- 功能正确性：逻辑是否实现预期，是否存在边界条件错误。
- 安全性：是否存在 SQL 注入、XSS、命令注入、路径遍历、反序列化、弱加密等风险。
- 性能：是否存在 N+1 查询、O(n²) 算法、大文件一次性读取、串行异步调用等问题。
- 可维护性：是否存在上帝类、过长函数、重复代码、深层嵌套、类型滥用等问题。
- 可靠性：异常处理、资源释放、并发访问是否稳健。
- 可测试性：代码是否容易测试，是否缺少必要测试覆盖。

已有技术大致分为三类。

### 2.1 静态分析工具

代表工具：

- CodeQL
- Semgrep
- SonarQube
- Bandit
- Ruff
- ESLint

优势：

- 快速、稳定、可重复。
- 适合 CI/CD 门禁。
- 对规则明确的问题检测效果好。

局限：

- 依赖规则覆盖。
- 业务语义理解弱。
- 对设计合理性、修复建议质量、跨文件上下文理解不足。

### 2.2 AI PR Reviewer

代表项目：

- PR-Agent
- CodeRabbit
- GitHub Copilot code review

优势：

- 能生成自然语言评审建议。
- 体验接近真人 reviewer。

局限：

- 容易产生无关评论、重复建议和误报。
- 成本和延迟不稳定。
- 缺少清晰的证据来源和审查路径解释。

### 2.3 多 Agent 代码评审

多 Agent 可以通过专家分工、交叉复核和仲裁提升复杂任务处理能力。

但直接让多个 Agent 审查所有代码会产生问题：

- 调用成本高。
- 上下文重复。
- 不同 Agent 输出重复或冲突。
- recall 可能上升，但 precision 下降。
- 总体 F1 不一定优于单 Agent。

本项目正是围绕这个问题进行改造：不再追求全量多 Agent，而是使用自适应多 Agent。

## 3. 设计目标

项目目标如下：

1. 支持三种审查模式

   ```text
   single   单 Agent 快速审查
   multi    三专家 Agent 全量并行审查
   adaptive General-first 自适应路由，通用审查兜底，按需触发专家增强
   ```

2. 降低无效多 Agent 调用

   使用 Router 在 LLM 调用前判断风险信号：简单代码只走通用审查，复杂代码在通用审查基础上按需追加专家。

3. 融合静态分析证据

   将 Bandit、Ruff、Semgrep 结果作为 evidence 合并到最终报告。

4. 建立代码审查知识库

   使用 ChromaDB 存储历史 findings、代码片段、来源、证据、用户反馈和风险模式。

5. 支持反馈学习

   用户 confirm / reject 的结果会影响后续相似案例检索和规则蒸馏。反馈入口包括 CLI 交互确认、`feedback` 子命令和 Web 页面确认/驳回按钮；benchmark 只做评测，不进入反馈学习。

6. 提供可量化 benchmark

   对比 single、multi、adaptive 在 recall、precision、F1、耗时等指标上的表现。

### 3.1 技术栈概览

本项目不是单一的大模型调用脚本，而是围绕代码审查流程构建的工程化系统。下表只列出项目中实际使用到的核心技术；其中大模型实验与主要运行使用 DeepSeek，其他模型接口仅作为代码层兼容能力保留。

| 技术方向 | 使用技术 | 在项目中的作用 |
|---------|----------|----------------|
| 开发语言 | Python 3.10+ | 实现 CLI、Web 后端、Agent 编排、benchmark、知识库和报告生成等核心逻辑 |
| 命令行交互 | argparse、Rich | `argparse` 用于解析 `review`、`benchmark`、`consensus`、`feedback`、`doctor` 等子命令；`Rich` 用于终端表格、进度条和结果展示 |
| Web 服务 | FastAPI、Uvicorn、Pydantic、WebSocket、HTML/CSS/JavaScript | `FastAPI` 提供后端接口，`Uvicorn` 启动 Web 服务，`Pydantic` 定义请求数据结构，`WebSocket` 支持流式审查过程展示，前端使用原生 HTML/CSS/JavaScript |
| 大模型接入 | DeepSeek、OpenAI-compatible API、OpenAI SDK | 当前实验和主要运行实际调用 DeepSeek；由于 DeepSeek 兼容 OpenAI 接口，代码层通过 OpenAI SDK 统一发起请求 |
| LLM 调用封装 | 自定义 LLMClient | 统一处理 DeepSeek 调用、缓存、重试和流式输出，并在代码层保留 provider 扩展点 |
| 多 Agent 架构 | General Reviewer、SecurityAgent、PerformanceAgent、MaintainabilityAgent、ArbiterAgent | 将代码审查拆分为通用审查、安全、性能、可维护性和冲突仲裁等角色 |
| 自适应路由 | Scoring Router、风险规则、阈值触发 | 在 LLM 调用前对文件进行 security、performance、maintainability 三类风险评分，决定是否追加专家 Agent |
| 静态证据层 | Bandit、Ruff、可选 Semgrep | Bandit 和 Ruff 作为 Python 项目的默认静态证据来源；Semgrep 在配置本地规则后作为可选跨语言规则扫描工具 |
| 知识库与反馈学习 | ChromaDB、本地 JSON、规则蒸馏、Seed Knowledge、Project Profile | 存储历史 findings、用户确认/驳回反馈、规则模式和项目级风险画像；ChromaDB 用于相似案例检索，本地 JSON 用于规则和项目画像持久化 |
| 文本与规则处理 | re、json、yaml、jieba | 用于 finding 解析、规则配置、中文关键词处理、相似度匹配和报告规范化 |
| 并发执行 | ThreadPoolExecutor | 用于多文件审查、多 Agent 调用、benchmark 执行和 Web 后端任务调度 |
| Benchmark 评估 | 自定义 ground truth、Recall、Precision、F1、标准差统计 | 对 single、multi、adaptive 三种模式进行可量化对比，并保存 Markdown / JSON 实验结果 |
| 多轮稳定性分析 | Repeated Review Consensus | 在真实项目没有 ground truth 时，对多次审查报告做共识分析，区分 stable、probable、volatile findings |
| 测试与持续集成 | pytest、GitHub Actions | 对核心模块、Router、指标计算、静态证据、consensus 和 benchmark 聚合逻辑进行自动化验证 |
| 配置与环境管理 | python-dotenv、`.env`、YAML 配置 | 管理模型 provider、API Key、规则开关、静态工具配置和运行参数 |
| 包管理与入口 | requirements.txt、setuptools、`python -m src` | 提供依赖安装、命令入口和模块化运行方式 |

这些技术栈共同支撑了项目的核心设计：使用大模型完成语义级代码审查，用 Router 控制审查资源分配，用静态分析工具提供可验证证据，用 benchmark 和 consensus 对审查效果进行评估。

## 4. 系统架构

整体架构如下：

```text
代码输入
  |
  v
Router / Triage
  |
  |-- 无明显风险 --> General Reviewer
  |
  |-- 风险信号 --> General Reviewer + Specialist Agents
                  |-- Security Agent
                  |-- Performance Agent
                  |-- Maintainability Agent
  |
  v
Static Evidence Layer
  |-- Bandit
  |-- Ruff
  |-- Semgrep
  |
  v
Knowledge Base
  |-- Similar Review Memories
  |-- Distilled Rules
  |-- Project Profiles
  |
  v
Quality Filter + Deduplication
  |
  v
Arbiter / Conflict Resolver
  |
  v
Markdown Review Report
```

核心目录：

```text
src/agents/
  base.py                ReviewFinding 与 Agent 基类
  security.py            安全专家 Agent
  performance.py         性能专家 Agent
  maintainability.py     可维护性专家 Agent
  arbiter.py             冲突仲裁与报告生成

src/core/
  reviewer.py            主审查编排逻辑
  router.py              自适应风险路由
  static_evidence.py     静态分析证据层
  knowledge.py           ChromaDB 代码审查知识库
  cache.py               审查缓存
  debate.py              多 Agent 冲突讨论兼容逻辑

src/benchmarks/
  runner.py              benchmark 执行器
  metrics.py             recall / precision / F1 计算
  ground_truth.py        标准答案定义
  test_cases/            测试样例
```

## 5. 核心模块设计

### 5.1 Adaptive Router

Router 是 adaptive 模式的核心。它在任何 LLM 调用之前运行，通过 scoring router 对代码风险进行打分，再决定是否在通用审查之外追加专家 Agent。

安全信号示例：

- SQL 字符串拼接
- os.system / subprocess / child_process
- innerHTML / dangerouslySetInnerHTML / render_template_string
- pickle.loads / yaml.load / eval
- MD5 / SHA1 / Math.random
- token / password / secret / API key

性能信号示例：

- 循环中数据库或 API 调用
- 嵌套循环
- readlines / readall / 大文件一次性读取
- 多个串行 await 且未使用 Promise.all

可维护性信号示例：

- 文件过长
- 函数或类过长
- 分支嵌套过深
- any / dict / object 滥用
- 空 catch / except Exception

Router 的目标不是做最终判断，而是决定审查资源如何分配。当前 adaptive 采用 `pre-routing + General baseline` 设计：Router 先在 LLM 调用前对代码进行轻量风险评分，分别计算 security、performance、maintainability 三类分数；随后 General Reviewer 始终作为基础审查者运行，超过阈值的风险类型才追加对应专家；低于强触发阈值但有一定风险的信号会作为 weak signal 保留在路由解释中。

为了避免专家噪声拉低 precision，adaptive 不再把专家结果无条件并入最终报告，而是采用 conservative merge：通用 reviewer 的结果作为基线保留，专家 finding 只有在高置信、非重复、能补足基线缺口时才作为补充进入报告。

### 5.2 多专家 Agent

系统内置三个专家 Agent。

Security Agent 关注：

- SQL 注入
- XSS
- 命令注入
- 路径遍历
- 反序列化
- 弱加密
- 硬编码密钥
- 敏感信息泄露

Performance Agent 关注：

- N+1 查询
- O(n²) 或更高复杂度
- 大文件一次性读取
- 串行异步调用
- 缓存缺失
- 不必要对象创建

Maintainability Agent 关注：

- 上帝类
- 过长函数
- 重复代码
- 深层嵌套
- 类型滥用
- 异常处理不当
- 代码组织问题

三个 Agent 输出统一的 ReviewFinding，便于后续合并、去重和报告生成。

### 5.3 Static Evidence Layer

项目新增静态证据层，支持将成熟工具的输出转换为统一 ReviewFinding。

当前支持：

| 工具 | 作用 |
| --- | --- |
| Bandit | Python 安全问题检测 |
| Ruff | Python lint 与可维护性检测 |
| Semgrep | 可选跨语言规则检测 |

设计原则：

- 工具不存在时安全跳过。
- 不阻塞主审查流程。
- 工具结果以 source 字段进入报告。
- Semgrep 通过 `SEMGREP_CONFIG` 指定本地规则集。

报告中可以区分 finding 来源：

```text
llm
bandit
ruff
semgrep
```

这让报告具有更清晰的证据链。

### 5.4 Review Knowledge Base

知识库是本轮优化后的重点模块。它基于 ChromaDB 构建，不再只是存储历史 findings，而是作为代码审查记忆层。

每条 finding 存储的关键信息包括：

```text
finding_id
project_id
file_path
language
category
severity
line_range
title
description
code_snippet
fix_suggestion
source
evidence
patterns
confidence
verdict
timestamp
```

其中：

- source 表示来源，如 llm、bandit、ruff、semgrep。
- evidence 表示静态工具规则 ID 或证据。
- patterns 表示识别出的风险模式。
- verdict 表示用户反馈状态，如 unknown、confirmed、rejected。

知识库分三层：

```text
Layer 1: raw_findings
存储每一次审查发现，包括代码片段、来源、证据、反馈状态。

Layer 2: distilled_rules
存储预置 seed rules，以及基于 language + category + pattern 聚合历史反馈沉淀出的模式级规则。

Layer 3: project_profiles
记录项目画像，包括语言分布、问题类别分布、严重程度分布、来源分布和高频风险模式。
```

为了避免知识库初始为空，项目支持 Seed Knowledge 初始化。它会预置常见代码审查模式，例如 SQL 注入、XSS、命令注入、路径遍历、不安全反序列化、N+1 查询、嵌套循环、串行 async、弱类型和空异常处理等。Seed Knowledge 不伪造历史审查记录，而是写入 `distilled_rules`，作为基础审查规则参与后续 prompt hints。

初始化命令：

```bash
python -m src seed-knowledge
```

相比旧版知识库，本轮优化包括：

- 修复项目画像没有真正参与检索的问题。
- 将规则蒸馏从粗粒度 `language + category` 升级为 `language + category + pattern`。
- 增加代码片段存储。
- 增加 LLM 与静态工具来源融合。
- 增加 confirmed / rejected 反馈状态。
- 增加 Seed Knowledge 初始化机制，使知识库启动阶段即可具备基础审查经验。
- few-shot 检索结合语言、类别、来源、严重级别、反馈状态和时间衰减排序。

知识库的作用：

```text
1. 为后续审查提供相似历史案例。
2. 根据用户反馈调整提示词。
3. 把高频风险模式沉淀成动态规则。
4. 给项目建立审查画像。
```

### 5.5 Arbiter 与报告生成

Arbiter 负责最终结果整理：

- 合并重复 finding。
- 处理同一行或相近行的冲突。
- 根据严重程度排序。
- 输出 Markdown 报告。
- 展示 source 和 evidence。
- 展示 Router 选择结果和静态工具参与情况。

报告产物：

- CLI 审查默认生成 `review_<目标名>.md`，也可以通过 `--output` 指定路径。
- Web UI 在页面展示报告的同时，会在本地 `data/reports/` 下保存 Markdown 报告。
- 页面仍保留下载按钮，方便用户把当前报告另存或分享。
- benchmark 输出用于模式评估，不生成用户确认/驳回流程。

报告行示例：

```text
HIGH | L10-L12 | security | llm | SQL Injection | description | fix
MED  | L20     | security | bandit | B602 subprocess_popen_with_shell_equals_true
LOW  | L35     | maintainability | ruff | F841 unused variable
```

## 6. Benchmark 设计

项目 benchmark 支持三种模式：

```text
single
multi
adaptive
```

评估指标：

- Recall
- Precision
- F1
- Found / Expected
- Elapsed Time

### 6.1 评估指标含义与计算公式

Benchmark 的核心思想是把模型输出与人工标注的 ground truth 进行结构化匹配。ground truth 是标准答案，记录每个测试文件中真实存在的问题；模型输出是 Agent 审查后生成的 findings。

基础计数如下：

```text
TP = True Positive，真正例
模型报出了某个问题，并且该问题能与 ground truth 中的真实问题匹配。

FP = False Positive，误报
模型报出了某个问题，但 ground truth 中没有对应问题，或匹配分数不足。

FN = False Negative，漏报
ground truth 中存在某个真实问题，但模型没有发现。
```

在本项目中，匹配不是简单关键词包含，而是综合比较：

```text
category
severity
line_range
risk pattern
sink / source
title keywords
description keywords
```

主要指标计算公式：

```text
Recall = TP / (TP + FN)
```

Recall 表示真实问题中有多少被模型找到了。Recall 越高，说明漏报越少。

```text
Precision = TP / (TP + FP)
```

Precision 表示模型报出的 findings 中有多少是真的。Precision 越高，说明误报越少。

```text
F1 = 2 * Precision * Recall / (Precision + Recall)
```

F1 是 Recall 与 Precision 的综合指标，用于平衡“找得全”和“报得准”。如果一个系统 Recall 很高但 Precision 很低，说明它能找很多问题但误报多；如果 Precision 很高但 Recall 很低，说明它报得很准但漏报多。

```text
Found / Expected = 模型输出 finding 数 / ground truth 问题数
```

该指标用于快速观察模型是否过度输出或过度保守。例如 `12/8` 表示模型报了 12 条，而标准答案中有 8 条真实问题，通常需要进一步查看 FP 明细。

```text
Elapsed Time = 当前模式完成审查所消耗的时间
```

Elapsed Time 用于评估不同审查模式的成本。多 Agent 或 adaptive 模式可能提高审查质量，但也可能增加调用耗时。

需要注意的是：真实项目通常没有提前写好的 ground truth，因此不能严格计算 Recall、Precision 和 F1。此时静态分析工具、人工确认结果和历史修复记录可以作为近似证据来源。本项目在 benchmark 中使用 ground truth 做量化评估，在真实审查中使用 Bandit、Ruff、Semgrep 和知识库反馈增强 finding 的可信度。

为了降低 LLM 输出随机性的影响，benchmark 支持多轮重复运行：

```bash
python -m src benchmark --category mixed --runs 3
```

多轮运行会输出每种模式的平均 Recall、Precision、F1，以及对应标准差。平均值用于观察整体能力，标准差用于观察结果稳定性。

新增 mixed benchmark，用于模拟真实业务文件中的混合风险。

mixed 样例包括：

```text
mixed/flask_account_service.py
mixed/react_admin_panel.tsx
mixed/batch_export_job.py
```

这些文件混合包含：

- SQL 注入
- XSS
- 硬编码密钥
- 命令注入
- 路径遍历
- 不安全反序列化
- N+1 查询
- O(n²) 嵌套循环
- 大文件一次性读取
- any 滥用
- 空 catch / except

## 7. 最新测试结果分析

### 7.1 Mixed benchmark：混合风险场景

实验命令：

```bash
python -m src benchmark --category mixed --runs 5
```

报告产物：

```text
reports/benchmarks/benchmark_mixed_5runs_20260613_171126.md
reports/benchmarks/benchmark_mixed_5runs_20260613_171126.json
```

5 轮平均结果：

| 模式 | Avg Recall | Std Recall | Avg Precision | Std Precision | Avg F1 | Std F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| single | 59.76% | 12.35% | 74.90% | 12.10% | 65.73% | 12.32% |
| multi | 85.36% | 4.30% | 68.64% | 4.37% | 75.28% | 3.20% |
| adaptive | 72.02% | 2.20% | 85.63% | 7.78% | 77.73% | 3.73% |

结论：

- full multi 的 recall 最高，说明全量专家更容易多找问题。
- full multi 的 precision 低于 adaptive，说明无条件多专家会引入更多噪声。
- adaptive 获得最高 Avg F1 与最高 Avg Precision，说明按需专家路由和 conservative merge 在混合风险文件中有效。
- single 的 Std F1 明显更高，说明单 Agent 在混合风险场景下结果波动更大。

### 7.2 All benchmark：全类别场景

实验命令：

```bash
python -m src benchmark --category all --runs 5
```

报告产物：

```text
reports/benchmarks/benchmark_all_5runs_20260613_175735.md
reports/benchmarks/benchmark_all_5runs_20260613_175735.json
```

5 轮平均结果：

| 模式 | Avg Recall | Std Recall | Avg Precision | Std Precision | Avg F1 | Std F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| single | 60.37% | 2.83% | 78.88% | 3.74% | 67.13% | 3.07% |
| multi | 64.02% | 1.91% | 68.64% | 2.24% | 64.27% | 2.18% |
| adaptive | 61.58% | 2.99% | 78.95% | 2.38% | 67.04% | 2.64% |

结论：

- 在全类别测试中，adaptive 与 single 的 F1 基本持平。
- adaptive 的 precision 与 single 接近，并明显高于 full multi。
- full multi 在 recall 上有优势，但 precision 下降，说明全量专家适合作为高召回对照，不适合作为默认生产策略。
- 该结果说明 adaptive 的价值不是在所有简单场景中碾压 single，而是在保留路由诊断和专家增强能力的同时避免 full multi 的噪声。

### 7.3 Web 真实项目 5 次重复审查

真实项目通常没有提前标注好的 ground truth，因此不能严格计算 Recall、Precision 和 F1。本项目使用 repeated review consensus 分析同一项目多次审查结果的稳定性。

实验对象：

```text
camput-lost-found-main
```

实验设置：

```text
Web UI
adaptive 模式
同一项目连续审查 5 次
不提交反馈，不修改代码，不切换模型
```

Consensus 报告：

```text
reports/consensus_campus_lost_found_5runs.md
reports/consensus_campus_lost_found_5runs.json
```

统计结果：

| 指标 | 结果 |
| --- | ---: |
| Runs | 5 |
| 平均正式 finding 数 | 9.60 |
| finding 数标准差 | 2.94 |
| Stable findings | 0 |
| Probable findings | 3 |
| Volatile findings | 42 |

重复出现的 probable 风险包括：

| 文件 | 风险 |
| --- | --- |
| `CorsConfig.java` | CORS 允许任意源并允许凭据，存在跨站请求风险 |
| `server.ts` | SQL 字符串拼接，存在 SQL 注入风险 |
| `vite.config.ts` | 前端打包暴露 API key / 环境变量风险 |

这轮 Web 实验还验证了最近的工程修复：

- Web 上传项目会先落盘到临时目录，再把真实路径传给静态分析工具，报告中不再出现 `static tools skipped: file not found`。
- 截断类误报消失，没有再出现 “文件截断 / 函数不完整 / docstring 未闭合” 等报告。
- 真实项目报告规模明显降低，说明质量过滤、测试夹具处理和 consensus 分层有助于减少噪声。

综合结论：

```text
mixed benchmark 中 adaptive 的 F1 与 Precision 最优；
all benchmark 中 adaptive 与 single 接近，但明显比 full multi 更少噪声；
真实项目中 consensus 能把重复出现的问题与一次性噪声区分开。
```

因此，本项目的核心结论不是“多 Agent 永远更强”，而是：

```text
多 Agent 需要风险路由、静态证据、去重、仲裁、反馈知识库和 consensus 分析共同约束。
adaptive routing 更适合作为真实项目审查的默认策略。
```

## 8. 当前优势

相比简单的 LLM 代码审查脚本，本项目具备以下优势：

1. 有系统架构

   包含 Router、Agents、Static Evidence、Knowledge Base、Arbiter、Benchmark。

2. 有工程权衡

   不盲目追求多 Agent，而是通过 benchmark 发现问题并改造架构。

3. 有证据融合

   LLM 输出与 Bandit / Ruff / Semgrep 结果可以统一进入报告。

4. 有反馈学习

   用户确认或驳回会进入知识库，并影响后续检索和规则蒸馏。

5. 有 benchmark 评估

   能比较 single、multi、adaptive 的 recall、precision、F1 和耗时。

6. 有真实问题意识

   最新结果没有强行宣称多 Agent 全面获胜，而是区分了 mixed、all benchmark 与真实项目 consensus 三类场景，给出了更稳健的实验结论。

7. 有工程化环境检查

   新增 `doctor` 命令，检查 Python 版本、依赖包、API Key、ChromaDB、Bandit、Ruff、Semgrep 等运行条件，降低换电脑运行失败的概率。

8. 有并发安全加固

   多模型投票不再修改全局模型配置；Web 端知识库读写使用锁保护，避免并发审查时出现配置串扰或 ChromaDB 写入冲突。

9. 有报告展示规范化

   增加报告语言规范化层，静态工具规则 ID 保留英文，问题标题与描述尽量统一为中文，减少中英混杂和 severity 空显示。

10. 有 repeated review consensus

   真实项目没有 ground truth 时，可以对同一项目多次审查报告做共识分析，将重复出现的问题标记为 probable/stable，将一次性输出归为 volatile，帮助用户优先复核高可信问题。

## 9. 当前不足

项目仍存在不足，但已经比上一版更接近可解释、可调优的工程系统：

1. Benchmark 数据规模仍偏小

   mixed benchmark 已能覆盖安全、性能、可维护性混合风险，但样本数量还不足以证明模型在更多真实项目上的稳定性。

2. Precision 仍需持续关注

   mixed benchmark 中 adaptive precision 已达到较好水平，但真实项目中仍可能出现一次性泛化建议。后续应继续根据 consensus 的 volatile findings 和用户反馈调阈值、提示词和证据权重。

3. Ground truth 需要继续细化

   已从简单关键词匹配升级为结构化 scoring match，包括 category、severity、line_range、risk pattern、sink/source 和描述关键词。后续可以继续增加代码片段定位、AST 节点类型和测试用例级别的验证。

4. Router 仍是确定性 scoring router

   当前 Router 已从“是否触发”的启发式规则升级为三类风险打分。下一步可以用 benchmark 结果训练或校准权重，让 Router 从规则评分进一步演进为可学习的 routing policy。

5. 真实 PR 场景还不够

   当前主要是文件级审查，未来可以扩展为 Pull Request diff 级审查。

6. 跨语言静态工具证据还可以加强

   当前 Bandit / Ruff 对 Python 支持较好，Web 上传项目也能正确把真实路径传给静态工具。但 TypeScript、Vue、Java 项目仍主要依赖 LLM 与可选 Semgrep，后续可以增加 ESLint、tsc、Checkstyle、SpotBugs 或更多 Semgrep 规则。

## 10. 后续优化方向

建议后续仍然重点优化 precision，但当前版本已经完成了三项关键基础设施升级：结构化匹配、scoring router、结果压噪。

### 10.1 Benchmark 明细输出

当前 mixed benchmark 已输出：

```text
matched findings
missed expected findings
unmatched findings
false positives
false negatives
```

这样可以知道 F1 下降到底是哪些 finding 导致。后续可以继续把这些明细保存为 JSON 报告，便于多次实验对比。

### 10.2 Adaptive 信号强度评分

当前 Router 已升级为评分制：

```text
security_score
performance_score
maintainability_score
```

只有超过阈值才触发对应 Agent。后续可以根据 benchmark 结果自动校准权重，例如把高 precision 的规则升权，把高 false positive 的规则降权。

### 10.3 结果压噪

可以增加：

```text
每个 category 最多保留 top N
低 confidence + low severity 过滤
同一 line_range + 同一 pattern 强制合并
无代码行号的 finding 降权
静态工具确认或多 Agent 确认时升权
```

### 10.4 细化评审分类

从三类扩展为：

```text
security
performance
maintainability
correctness
reliability
testability
```

### 10.5 PR Diff 级审查

未来支持：

```text
git diff
changed lines
surrounding context
PR title / description
CI result
test coverage
```

这会更接近真实 GitHub PR review。

## 11. 使用方式

安装依赖：

```bash
pip install -r requirements.txt
```

运行测试：

```bash
python -m pytest tests -q
```

运行审查：

```bash
python -m src review src --mode adaptive
```

指定报告文件：

```bash
python -m src review src --mode adaptive --output review_src.md
```

审查结束后进入确认/驳回反馈：

```bash
python -m src review src --mode adaptive --feedback
```

检查本地运行环境：

```bash
python -m src doctor
```

运行一键 Demo 流程：

```bash
python -m src demo --runs 5 --review-target src
```

如果后续想补充反馈，也可以使用 finding id 或标题提交：

```bash
python -m src feedback path/to/file.py --confirm <finding_id>
python -m src feedback path/to/file.py --reject <finding_id>
```

初始化知识库 Seed Knowledge：

```bash
python -m src seed-knowledge
```

运行 mixed benchmark：

```bash
python -m src benchmark --category mixed
```

运行 3 轮 mixed benchmark 并计算平均值与标准差：

```bash
python -m src benchmark --category mixed --runs 3
```

benchmark 默认会保存 Markdown 与 JSON 报告：

```text
reports/benchmarks/*.md
reports/benchmarks/*.json
```

对 5 份真实项目审查报告做 consensus 分析：

```bash
python -m src consensus "report1.md" "report2.md" "report3.md" "report4.md" "report5.md" --output reports/consensus_latest.md --json-output reports/consensus_latest.json
```

启动 Web UI：

```bash
python -m src web
```

## 12. 总结

Adaptive Code Review Agent 目前已经从一个简单的多 Agent 代码审查工具，升级为一个具备工程完整性的智能代码评审编排系统。

它的核心价值不是声称超过成熟静态分析工具，也不是声称多 Agent 一定更强，而是：

```text
发现多 Agent 的真实问题
通过 adaptive routing 控制成本和噪声
通过 static evidence 增强报告可信度
通过 knowledge base 记忆历史反馈
通过 benchmark 量化改造效果
```

当前测试结果说明系统还有优化空间，尤其是 precision 控制。但这也让项目更真实：它展示了从实验发现问题、到架构调整、再到下一步优化计划的完整工程思路。
