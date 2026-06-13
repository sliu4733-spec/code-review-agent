# Adaptive Code Review Agent

An LLM-powered code review tool that starts with a fast single-agent path and
switches to specialist multi-agent collaboration only when the code actually
needs it.

The project was designed around a practical benchmark result: full multi-agent
review is not always faster or better. Simple single-domain files are often
handled best by one focused reviewer. The system therefore uses adaptive
routing to balance review quality, latency, and API cost.

## Highlights

- **General-first adaptive routing by default**: a deterministic scoring router
  inspects code signals, keeps the general reviewer as a recall baseline, and
  adds specialist agents only when their risk score justifies the extra call.
- **Specialist reviewers**: security, performance, and maintainability agents
  provide focused review prompts.
- **Static evidence layer**: optional Bandit, Ruff, and Semgrep integrations add
  reproducible tool findings to the adaptive review path when those tools are
  installed.
- **Conflict handling**: overlapping findings are deduplicated and conflicting
  recommendations can be adjudicated by an arbiter.
- **Benchmark support**: compares `single`, `multi`, and `adaptive` review
  modes with recall, precision, F1, elapsed time, and TP/FP/FN diagnostics.
- **Consensus analysis**: merges repeated Web/CLI reports and separates stable
  findings from one-off LLM noise.
- **CLI and Web entry points**: use the command line for automation or the web
  UI for interactive review.
- **Multi-language test cases**: Python, JavaScript, TypeScript, Java, and Go.

## Architecture

```text
Code input
  |
  v
Router / Triage
  |-- no strong signal --> General reviewer only
  |
  |-- risk signals -----> General reviewer + specialist agents
                         |-- Security
                         |-- Performance
                         |-- Maintainability
                                  |
                                  v
                         Quality filter + dedupe
                                  |
                                  v
                         Static evidence
                         Bandit / Ruff / Semgrep
                                  |
                                  v
                         Arbiter for conflicts
                                  |
                                  v
                         Markdown report
```

## Why Adaptive Multi-Agent?

The goal is not to prove that multi-agent review always wins. The goal is to
use collaboration where it is worth the overhead:

- Simple files should be reviewed quickly.
- Security-sensitive code should trigger the security expert.
- Loops, database calls, bulk I/O, and serial async work should trigger the
  performance expert.
- Large, deeply nested, or weakly typed code should trigger the maintainability
  expert.
- Mixed-risk files should use multiple experts and merge their findings.

This makes the project closer to a real engineering system: it has to trade off
quality, latency, and cost instead of always choosing the most expensive path.

## Quick Start

```bash
git clone https://github.com/sliu4733-spec/code-review-agent.git
cd code-review-agent
pip install -r requirements.txt
cp .env.example .env
python -m src review src --mode adaptive
```

Set your provider in `.env`:

```env
PROVIDER=openai
OPENAI_API_KEY=your_api_key_here
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-chat
```

## Usage

```bash
# Adaptive mode, recommended
python -m src review src --mode adaptive

# Save to a custom Markdown report
python -m src review src --mode adaptive --output review_src.md

# Enter confirm/reject feedback after review
python -m src review src --mode adaptive --feedback

# Fast baseline
python -m src review src --mode single

# Always run all specialists
python -m src review src --mode multi

# Run benchmark
python -m src benchmark

# Focus on mixed-risk demo cases
python -m src benchmark --category mixed

# Repeat benchmark runs and report mean/std deviation
python -m src benchmark --category mixed --runs 3

# Save reproducible benchmark reports as Markdown and JSON
python -m src benchmark --category mixed --runs 5 --report-dir reports/benchmarks

# Merge repeated review reports into a stability/consensus report
python -m src consensus report1.md report2.md report3.md report4.md report5.md \
  --output reports/consensus_latest.md --json-output reports/consensus_latest.json

# Initialize curated review seed knowledge
python -m src seed-knowledge

# Check local environment before demos or first run
python -m src doctor

# One-command showcase workflow
python -m src demo --runs 5 --review-target src

# Submit feedback later by finding id or title
python -m src feedback path/to/file.py --confirm <finding_id>
python -m src feedback path/to/file.py --reject <finding_id>

# Bandit and Ruff are included in requirements.txt
set SEMGREP_CONFIG=path/to/local/semgrep-rules  # optional

# Start web UI
python -m src web
```

CLI reviews write Markdown reports by default as `review_<target>.md`. The Web
UI also saves server-side Markdown reports under `data/reports/` and still keeps
the browser download button for convenience. Benchmark runs do not ask for
feedback because they are evaluation runs; feedback learning is attached to real
review findings through the CLI feedback prompt, the `feedback` command, or the
Web UI confirm/reject buttons.

`doctor` checks Python version, installed packages, API key configuration,
ChromaDB initialization, and local static-analysis tools. Bandit and Ruff can be
used either as shell commands or through `python -m bandit/ruff`, which makes
the static evidence layer more portable across machines.

Benchmark runs save reproducible artifacts under `reports/benchmarks/` by
default. Markdown is useful for README screenshots and interview discussion;
JSON keeps raw metrics for later comparison.

## Benchmark Story

The benchmark should be read as three different questions:

| Mode | Question |
| --- | --- |
| `single` | How strong is the fast baseline? |
| `multi` | What happens if every specialist reviews every file? |
| `adaptive` | Can routing keep cost lower while preserving quality? |

Expected behavior:

- On simple single-domain cases, `single` can win on speed and sometimes F1.
- On mixed-domain or complex files, `adaptive` keeps the general reviewer as a
  baseline and adds selected specialists to improve coverage without always
  paying full multi-agent cost.
- Specialist findings are merged conservatively: the general reviewer remains
  the baseline, while experts only add high-confidence supplements for gaps that
  the baseline did not already cover.
- `multi` is useful as an upper-cost comparison, not necessarily as the default
  production path.

### Latest 5-Run Results

The current benchmark artifacts are saved under `reports/benchmarks/`.

Mixed-risk benchmark (`benchmark_mixed_5runs_20260613_171126`):

| Mode | Avg Recall | Std Recall | Avg Precision | Std Precision | Avg F1 | Std F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| single | 59.76% | 12.35% | 74.90% | 12.10% | 65.73% | 12.32% |
| multi | 85.36% | 4.30% | 68.64% | 4.37% | 75.28% | 3.20% |
| adaptive | 72.02% | 2.20% | 85.63% | 7.78% | 77.73% | 3.73% |

All benchmark categories (`benchmark_all_5runs_20260613_175735`):

| Mode | Avg Recall | Std Recall | Avg Precision | Std Precision | Avg F1 | Std F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| single | 60.37% | 2.83% | 78.88% | 3.74% | 67.13% | 3.07% |
| multi | 64.02% | 1.91% | 68.64% | 2.24% | 64.27% | 2.18% |
| adaptive | 61.58% | 2.99% | 78.95% | 2.38% | 67.04% | 2.64% |

Interpretation:

- On mixed-risk files, adaptive achieves the best F1 and the best precision.
- On all categories, adaptive is close to the single-agent baseline while
  preserving routing diagnostics and avoiding full multi-agent noise.
- Full multi-agent review tends to increase recall, but its precision drops
  because every specialist is forced to comment on every file.
- The headline result is not "multi-agent always wins"; it is that adaptive
  routing gives a better quality/cost trade-off for realistic mixed-risk code.

The benchmark matcher uses structured scoring rather than only keyword hits. A
finding is matched against ground truth with category, severity, line range,
risk pattern, sink/source evidence, and description keywords. This makes the
precision number more meaningful and prints concrete false positives and missed
issues for the mixed benchmark cases.

The `mixed` benchmark category contains more realistic files where security,
performance, and maintainability issues appear together:

- `flask_account_service.py`: SQL injection, XSS, hardcoded secrets, command
  injection, N+1 queries, nested loops, and swallowed exceptions.
- `react_admin_panel.tsx`: unsafe HTML rendering, insecure random token
  generation, serial async requests, nested loops, `any` overuse, and empty
  catch blocks.
- `batch_export_job.py`: unsafe deserialization, SQL injection, path traversal,
  command injection, N+1 queries, memory-heavy file reads, and weak error
  handling.

## Static Evidence Layer

The project does not try to replace mature static analyzers. Instead, it treats
them as evidence providers:

| Tool | Role |
| --- | --- |
| Bandit | Python security findings |
| Ruff | Python lint and maintainability findings |
| Semgrep | Optional cross-language rules, enabled through `SEMGREP_CONFIG` |

When available, tool findings are merged into the adaptive review report with a
source label such as `bandit`, `ruff`, `semgrep`, or `llm`. This gives the final
report a clearer audit trail: reviewers can see which comments came from
deterministic tools and which came from LLM specialists.

For Web project uploads, files are materialized into a temporary server-side
directory before adaptive review. This lets Bandit, Ruff, and optional Semgrep
receive real file paths instead of browser-only relative names. The final report
still displays the original uploaded project paths.

## Repeated Review Consensus

Real projects usually do not have ground truth, so strict recall/precision/F1
cannot be computed. For those cases, the project provides repeated-report
consensus analysis:

```bash
python -m src consensus "review_report (13).md" "review_report (14).md" \
  "review_report (15).md" "review_report (16).md" "review_report (17).md" \
  --output reports/consensus_campus_lost_found_5runs.md \
  --json-output reports/consensus_campus_lost_found_5runs.json
```

In the `campus-lost-found` Web review experiment, five adaptive runs produced
an average of 9.60 findings with a standard deviation of 2.94. The consensus
report separated 3 probable repeated risks from 42 volatile one-off findings:

- permissive CORS with credentials in `CorsConfig.java`
- SQL construction risk in `server.ts`
- client-side API key exposure risk in `vite.config.ts`

This is the intended real-project workflow: use repeated adaptive reviews to
prioritize findings that recur, and treat one-off findings as low-confidence
signals unless static evidence or human feedback confirms them.

## Review Knowledge Base

The knowledge base is a review memory layer built on ChromaDB. It stores
historical findings with richer context than a simple cache:

- file path, project id, language, category, severity, and line range
- code snippet around the finding
- source label such as `llm`, `bandit`, `ruff`, or `semgrep`
- evidence ids from static tools
- user feedback verdicts: unknown, confirmed, or rejected
- detected risk patterns such as `sql-string-construction`,
  `loop-database-or-api-call`, or `xss-html-sink`

During later reviews, similar confirmed memories and distilled rules are added
to the prompt as few-shot guidance. This lets the system learn from reviewer
feedback without hard-coding every rule into the agent prompts.

## Project Structure

```text
src/agents/          Specialist agents and arbiter
src/core/            Review orchestration, router, consensus, cache, rules, knowledge base
src/benchmarks/      Benchmark runner, metrics, ground truth, test cases
prompts/             Editable prompts for each specialist
config/              User-defined review rules
tests/               Unit tests for parsing, debate, and routing
```
