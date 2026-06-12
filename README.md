# Code Review Agent — 自优化多智能体代码审查系统

[![Python](https://img.shields.io/badge/Python-3.10+-blue)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

基于 LLM 的多 Agent 协作代码审查工具。三个专业 Agent（安全/性能/可维护）并行审查，知识库自学习，CLI + Web 双入口。

## 快速开始

```bash
git clone https://github.com/sliu4733-spec/code-review-agent.git
cd code-review-agent
pip install -r requirements.txt
cp .env.example .env  # 编辑 .env 填入 API Key
python -m src web      # 浏览器打开 http://127.0.0.1:8000
```

## 使用方式

```bash
# CLI 审查
python -m src review "src/" --stream           # 审查整个目录
python -m src review "app.py" --mode single    # 单Agent快速模式
python -m src review "src/" --prefer "只关注安全漏洞" --stream  # 自然语言偏好

# Web 界面
python -m src web                               # 可视化审查

# 自定义规则
python -m src rules                             # 交互式规则管理

# 测试
python -m pytest tests/ -v                      # 单元测试
python -m src benchmark                         # 量化评估
```

## 架构

```
代码 → 三个专业Agent并行审查(安全/性能/可维护)
     → 质量过滤 + 冲突检测
     → 仲裁裁决(LLM仅出指令,不改原始数据)
     → Markdown报告 + ChromaDB三层知识库
```

## 特性

- **多Agent协作**: SecurityAgent(OWASP Top 10) + PerformanceAgent(N+1/连接池) + MaintainabilityAgent(上帝类/重复代码)
- **知识库自学习**: ChromaDB 三层架构 — 原始记录(权重衰减) → 蒸馏规则(自动聚类) → 项目画像
- **Prompt 模板化**: 编辑 `prompts/*.md` 即可定制审查规则
- **自然语言规则**: `--prefer "多关注安全问题, 不用管代码风格"`
- **双入口**: CLI 流式输出 + WebSocket 实时推送
- **5 语言**: Python / JavaScript / TypeScript / Java / Go
- **Benchmark**: 12 测试用例 + Recall/Precision/F1 量化
- **全局安装**: `pip install -e .` → `code-review "src/" --stream`

## 配置

```env
# .env
PROVIDER=openai                     # openai / anthropic / ollama
OPENAI_API_KEY=sk-your-key-here     # DeepSeek / OpenAI
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-chat

# Ollama 本地模式(完全离线)
# PROVIDER=ollama
# OLLAMA_MODEL=qwen2.5:7b
```

## 项目结构

```
src/agents/         # 3 个专业 Agent + 仲裁
src/core/           # 审查编排 + 知识库 + 辩论 + 缓存
src/benchmarks/     # 12 测试用例 + 评估指标
prompts/            # Agent Prompt 模板(Markdown)
config/             # 用户自定义审查规则(YAML)
```

## 详细文档

[项目说明书 (F:/XM/CodeReviewAgent_项目说明书.md)](F:/XM/CodeReviewAgent_项目说明书.md)
