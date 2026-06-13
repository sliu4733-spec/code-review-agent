# 实验结论：Adaptive Code Review Agent

## 1. 核心问题

项目最初尝试用全量多 Agent 审查所有代码，即 Security、Performance、Maintainability 三个专家每次都参与。实际 benchmark 后发现，这种方式并不稳定优于单 Agent。

主要原因：

- 多个专家会重复发现同一问题，增加去重和仲裁成本。
- 专家 Agent 会引入额外误报，precision 可能下降。
- 简单文件不需要完整协作，固定多 Agent 会增加延迟和 API 成本。

因此，本项目从“无条件多 Agent”调整为“风险感知的 adaptive routing”。

## 2. 当前设计

当前系统将三种模式区分为实验对照和真实默认策略：

| 模式 | 定位 |
| --- | --- |
| `single` | 快速基线，只跑 General Reviewer |
| `multi` | 全量专家对照，所有文件都跑 Security / Performance / Maintainability |
| `adaptive` | 推荐默认策略，Router 先对每个文件评分，再按需追加专家 |

adaptive 的流程：

```text
项目 / 文件输入
  ↓
Project Orchestrator 选择整体策略
  ↓
adaptive 策略下：Router 对每个文件做轻量风险评分
  ↓
General Reviewer 始终作为基础审查者运行
  ↓
按 Router 结果追加 Security / Performance / Maintainability 专家
  ↓
融合静态分析证据与知识库提示
  ↓
质量过滤、去重、冲突处理、conservative merge
  ↓
Arbiter 生成审查报告
```

Router 不产出最终 finding，它只决定“要不要请专家、请哪些专家”。General Reviewer 和专家 Agent 才负责发现问题。

## 3. 评价方式

benchmark 同时比较三种模式：

- `single`：强单 Agent 基线。
- `multi`：全量专家协作上限成本方案。
- `adaptive`：通用 reviewer + 按需专家。

核心指标：

- Recall：真实问题中被找出的比例。
- Precision：模型发现中真实问题的比例。
- F1：Recall 和 Precision 的平衡指标。
- Std：多轮重复运行的波动程度。

真实项目通常没有 ground truth，因此不能严格计算 Recall、Precision 和 F1。本项目使用 repeated review consensus 对同一项目多次审查结果做稳定性分析，把重复出现的问题与一次性噪声分开。

## 4. Benchmark 结果

### 4.1 Mixed benchmark

命令：

```bash
python -m src benchmark --category mixed --runs 5
```

报告：

```text
reports/benchmarks/benchmark_mixed_5runs_20260613_171126.md
reports/benchmarks/benchmark_mixed_5runs_20260613_171126.json
```

结果：

| Mode | Avg Recall | Std Recall | Avg Precision | Std Precision | Avg F1 | Std F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| single | 59.76% | 12.35% | 74.90% | 12.10% | 65.73% | 12.32% |
| multi | 85.36% | 4.30% | 68.64% | 4.37% | 75.28% | 3.20% |
| adaptive | 72.02% | 2.20% | 85.63% | 7.78% | 77.73% | 3.73% |

结论：

- full multi 的 recall 最高，说明全量专家更容易多找问题。
- adaptive 的 precision 与 F1 最高，说明按需专家路由能减少噪声。
- single 的 Std F1 最大，说明单 Agent 在混合风险场景下更不稳定。

### 4.2 All benchmark

命令：

```bash
python -m src benchmark --category all --runs 5
```

报告：

```text
reports/benchmarks/benchmark_all_5runs_20260613_175735.md
reports/benchmarks/benchmark_all_5runs_20260613_175735.json
```

结果：

| Mode | Avg Recall | Std Recall | Avg Precision | Std Precision | Avg F1 | Std F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| single | 60.37% | 2.83% | 78.88% | 3.74% | 67.13% | 3.07% |
| multi | 64.02% | 1.91% | 68.64% | 2.24% | 64.27% | 2.18% |
| adaptive | 61.58% | 2.99% | 78.95% | 2.38% | 67.04% | 2.64% |

结论：

- 全类别场景中，adaptive 与 single 的 F1 基本持平。
- adaptive 的 precision 与 single 接近，并明显高于 full multi。
- full multi 虽然 recall 略高，但 precision 较低，不适合作为默认审查策略。

## 5. Web 真实项目 5 次审查

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

Consensus 命令：

```bash
python -m src consensus "review_report (13).md" "review_report (14).md" "review_report (15).md" "review_report (16).md" "review_report (17).md" --output reports/consensus_campus_lost_found_5runs.md --json-output reports/consensus_campus_lost_found_5runs.json
```

结果：

| 指标 | 结果 |
| --- | ---: |
| Runs | 5 |
| 平均正式 finding 数 | 9.60 |
| finding 数标准差 | 2.94 |
| Stable findings | 0 |
| Probable findings | 3 |
| Volatile findings | 42 |

重复出现的 probable 风险：

| 文件 | 风险 |
| --- | --- |
| `CorsConfig.java` | CORS 允许任意源并允许凭据，存在跨站请求风险 |
| `server.ts` | SQL 字符串拼接，存在 SQL 注入风险 |
| `vite.config.ts` | 前端打包暴露 API key / 环境变量风险 |

这轮实验验证了最近的工程优化：

- Web 上传项目会先落盘到临时目录，再把真实路径传给静态分析工具，不再出现 `static tools skipped: file not found`。
- 截断类误报消失，没有再出现“文件截断 / 函数不完整 / docstring 未闭合”等报告。
- 报告规模从早期大批量噪声降低到平均 9.60 条 finding，用户可以先复核 consensus 中重复出现的问题。

## 6. 综合结论

当前最稳妥的实验结论是：

```text
多 Agent 不是越多越好。
Full multi-agent 能提高召回，但会带来额外噪声。
Adaptive routing 在混合风险场景中取得更好的 Precision 和 F1。
在全量 benchmark 中，adaptive 与 single 基本持平，但避免了 full multi 的 precision 下降。
真实项目没有 ground truth 时，consensus 分析可以帮助区分重复风险和一次性噪声。
```

因此，本项目的价值不是证明“多 Agent 永远比单 Agent 强”，而是展示一个更真实的 LLM 工程系统：

```text
先用实验发现无条件多 Agent 的问题；
再引入 Router、静态证据、质量过滤、仲裁和知识库；
最后用 benchmark 与 repeated review consensus 验证系统是否更稳定、更可信。
```

## 7. 项目陈述摘要

可以这样概括本项目：

> 本项目没有直接假设多 Agent 一定更好，而是先通过 benchmark 对 single、full multi 和 adaptive 三种模式进行对照。实验发现 full multi 的召回通常更高，但 precision 会下降，因为专家会产生重复和噪声。因此系统改造成 adaptive routing：先用轻量 Router 对每个文件做风险评分，General Reviewer 作为基础审查者，再按需触发安全、性能、可维护性专家。最终结合静态分析证据、去重、冲突仲裁、反馈知识库和 repeated review consensus，提高审查结果的可信度和可解释性。

项目特点可以概括为：

```text
设计并实现风险感知的多 Agent 代码审查系统，支持 single / full multi / adaptive 三种模式；
在 mixed benchmark 5 轮实验中 adaptive 达到最高 F1=77.73%、Precision=85.63%；
在真实项目 5 次重复审查中引入 consensus 分析，将重复风险与一次性 LLM 噪声分层展示。
```
