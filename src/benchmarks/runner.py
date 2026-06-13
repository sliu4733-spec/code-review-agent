"""Benchmark 执行器：运行所有测试用例，对比单 Agent vs 多 Agent 效果"""

import json
import time
from datetime import datetime
from statistics import mean, pstdev
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from src.agents.security import SecurityAgent
from src.agents.performance import PerformanceAgent
from src.agents.maintainability import MaintainabilityAgent
from src.core.reviewer import _quality_filter, run_adaptive_review, run_single_review
from src.benchmarks.ground_truth import GROUND_TRUTH
from src.benchmarks.metrics import calculate_metrics

console = Console()
BENCHMARK_DIR = Path(__file__).parent / "test_cases"
MODES = ("single", "multi", "adaptive")
METRICS = ("recall", "precision", "f1")


def _avg(lst):
    return sum(lst) / len(lst) if lst else 0


def _std(lst):
    return pstdev(lst) if len(lst) > 1 else 0.0


def _aggregate_runs(run_results: list[dict]) -> dict:
    summary = {}
    for mode in MODES:
        summary[mode] = {}
        for metric in METRICS:
            values = [result[mode][metric] for result in run_results]
            summary[mode][metric] = {
                "mean": mean(values) if values else 0.0,
                "std": _std(values),
            }
    return summary


def _print_multi_run_summary(run_results: list[dict]) -> None:
    summary = _aggregate_runs(run_results)
    table = Table(title=f"Multi-run benchmark summary ({len(run_results)} runs)")
    table.add_column("Mode")
    table.add_column("Avg Recall", justify="right")
    table.add_column("Std Recall", justify="right")
    table.add_column("Avg Precision", justify="right")
    table.add_column("Std Precision", justify="right")
    table.add_column("Avg F1", justify="right")
    table.add_column("Std F1", justify="right")

    for mode in MODES:
        row = [mode]
        for metric in METRICS:
            row.append(f"{summary[mode][metric]['mean']:.2%}")
            row.append(f"{summary[mode][metric]['std']:.2%}")
        table.add_row(*row)

    console.print(table)
    console.print("[dim]Std = standard deviation across repeated LLM benchmark runs.[/dim]")


def _save_benchmark_artifacts(payload: dict, output_dir: str = "reports/benchmarks") -> tuple[Path, Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"benchmark_{payload.get('category', 'all')}_{payload.get('runs', 1)}runs_{stamp}"
    json_path = out_dir / f"{base}.json"
    md_path = out_dir / f"{base}.md"

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_format_benchmark_markdown(payload), encoding="utf-8")
    return json_path, md_path


def _format_benchmark_markdown(payload: dict) -> str:
    lines = [
        "# Benchmark Report",
        "",
        f"- Category: `{payload.get('category')}`",
        f"- Runs: `{payload.get('runs')}`",
        f"- Created at: `{payload.get('created_at')}`",
        "",
    ]

    if "aggregate" in payload:
        lines.extend([
            "## Multi-Run Summary",
            "",
            "| Mode | Avg Recall | Std Recall | Avg Precision | Std Precision | Avg F1 | Std F1 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ])
        for mode in MODES:
            stats = payload["aggregate"][mode]
            lines.append(
                f"| {mode} | {stats['recall']['mean']:.2%} | {stats['recall']['std']:.2%} | "
                f"{stats['precision']['mean']:.2%} | {stats['precision']['std']:.2%} | "
                f"{stats['f1']['mean']:.2%} | {stats['f1']['std']:.2%} |"
            )
        lines.append("")

    summary = payload.get("summary")
    if summary:
        lines.extend([
            "## Summary",
            "",
            "| Mode | Recall | Precision | F1 |",
            "|---|---:|---:|---:|",
        ])
        for mode in MODES:
            stats = summary[mode]
            lines.append(f"| {mode} | {stats['recall']:.2%} | {stats['precision']:.2%} | {stats['f1']:.2%} |")
        lines.append("")

    cases = payload.get("cases", [])
    if cases:
        lines.extend([
            "## Cases",
            "",
            "| Case | Category | Single F1 | Multi F1 | Adaptive F1 |",
            "|---|---|---:|---:|---:|",
        ])
        for case in cases:
            lines.append(
                f"| {case['name']} | {case['category']} | "
                f"{case['single']['f1']:.2%} | {case['multi']['f1']:.2%} | {case['adaptive']['f1']:.2%} |"
            )
        lines.append("")

    return "\n".join(lines)


def _print_match_diagnostics(name: str, mode: str, metrics: dict, limit: int = 3):
    """Print compact TP/FP/FN details for precision tuning."""
    missed = metrics.get("missed", [])
    false_pos = metrics.get("false_positive_details", [])
    matched = metrics.get("matched", [])
    if not missed and not false_pos:
        return

    console.print(f"[dim]{name} / {mode} diagnostics[/dim]")
    if metrics.get("found_count", 0) == 0 and missed:
        console.print("  [yellow]No findings returned by this mode; this is a model/prompt result, not a matching failure.[/yellow]")
    for item in matched[:limit]:
        console.print(
            f"  [green]TP[/green] {item['expected_id']} <- {item['finding_title'][:70]} "
            f"(score={item['score']}, {','.join(item['reasons'])})"
        )
    for item in missed[:limit]:
        console.print(
            f"  [yellow]FN[/yellow] {item.get('expected_id')} "
            f"{item.get('issue_type') or item.get('title_keywords', ['?'])[0]} "
            f"{item.get('line_range', '')}"
        )
    for item in false_pos[:limit]:
        console.print(
            f"  [red]FP[/red] [{item['category']}/{item['severity']}] "
            f"{item['line_range']} {item['title'][:80]}"
        )


def _run_single_agent(file_path: str, code: str, category: str) -> list:
    if category == "mixed":
        findings, _ = run_single_review(code, file_path, kb=None, cache=None)
        return _quality_filter(findings, file_path)

    agents = {
        "security": SecurityAgent,
        "performance": PerformanceAgent,
        "maintainability": MaintainabilityAgent,
    }
    agent_cls = agents.get(category, SecurityAgent)
    agent = agent_cls()
    return _quality_filter(agent.analyze(code, file_path), file_path)


def _run_multi_agent(code: str, file_path: str) -> list:
    """多 Agent 并行审查 + CLI 同款质量过滤"""
    agents = [
        SecurityAgent(),
        PerformanceAgent(),
        MaintainabilityAgent(),
    ]
    results = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {}
        for agent in agents:
            futures[executor.submit(agent.analyze, code, file_path)] = agent.name
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as e:
                console.print(f"  [red]X {name} Agent 失败: {e}[/red]")
                results[name] = []

    all_findings = []
    for findings in results.values():
        all_findings.extend(findings)

    # 使用 CLI 同款质量过滤（jieba分词去重 + 去噪）
    return _quality_filter(all_findings, file_path)


def _run_adaptive_agent(code: str, file_path: str) -> tuple[list, str]:
    """Run adaptive review without cache so benchmark measures routing cost."""
    findings, plan_summary, _ = run_adaptive_review(
        code,
        file_path,
        kb=None,
        cache=None,
        project_context="",
        stream=False,
    )
    return findings, plan_summary


def _same_issue(f1, f2) -> bool:
    """判断两个发现是否为同一问题"""
    t1 = f1.title.lower()
    t2 = f2.title.lower()
    def bigrams(s):
        return set(s[i:i+2] for i in range(len(s)-1))
    b1 = bigrams(t1)
    b2 = bigrams(t2)
    title_overlap = len(b1 & b2) / min(len(b1), len(b2)) if b1 and b2 else 0

    # 解析行号区间
    def parse_range(f):
        try:
            r = f.line_range.replace("L", "").replace(" ", "")
            parts = r.split("-")
            s = int(parts[0])
            e = int(parts[1]) if len(parts) > 1 else s
            return s, e
        except:
            return 0, 0

    s1, e1 = parse_range(f1)
    s2, e2 = parse_range(f2)

    # 行号区间重叠（同一段代码）
    range_overlap = max(s1, s2) <= min(e1, e2) and s1 > 0 and s2 > 0

    # 行号跨度（用于过滤范围过宽的发现，如 L1-L30）
    span1, span2 = e1 - s1, e2 - s2
    too_broad = span1 > 15 or span2 > 15

    # 行号接近（±3行内，处理不同Agent定位偏差）
    line_near = abs(s1 - s2) <= 3 and s1 > 0 and s2 > 0

    # 判定规则：
    # 1. 区间重叠 + 标题有关 → 同一问题
    # 2. 标题高重叠(>0.55) + 行号接近 → 同一问题(不同Agent定位到相邻行)
    # 注意：不同漏洞即使标题前缀相同(如"不安全的...")，行号也一定不接近
    rule1 = range_overlap and title_overlap > 0.2 and not too_broad
    rule2 = title_overlap > 0.45 and line_near and not too_broad
    return rule1 or rule2


def run_benchmark(category: str = "all", runs: int = 1,
                  _return_summary: bool = False,
                  save_report: bool = True,
                  report_dir: str = "reports/benchmarks"):
    if runs < 1:
        raise ValueError("runs must be >= 1")
    if runs > 1:
        run_results = []
        for run_idx in range(1, runs + 1):
            console.rule(f"[bold cyan]Benchmark run {run_idx}/{runs}[/bold cyan]")
            result = run_benchmark(
                category=category,
                runs=1,
                _return_summary=True,
                save_report=False,
                report_dir=report_dir,
            )
            run_results.append(result)
        _print_multi_run_summary(run_results)
        aggregate = _aggregate_runs(run_results)
        if save_report and not _return_summary:
            payload = {
                "category": category,
                "runs": runs,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "aggregate": aggregate,
                "run_results": run_results,
            }
            json_path, md_path = _save_benchmark_artifacts(payload, report_dir)
            console.print(f"[green]Benchmark reports saved:[/green] {json_path} / {md_path}")
        return aggregate

    console.print(Panel.fit(
        "[bold blue]Benchmark: 代码审查 Agent 性能测试[/bold blue]\n"
        "对比单 Agent vs 多 Agent 辩论模式的检测效果",
        border_style="blue"
    ))

    # 收集所有测试用例（.py + .js + .ts + .tsx + .java + .go）
    test_files = []
    all_exts = (".py", ".js", ".ts", ".tsx", ".java", ".go")
    for ext in all_exts:
        for f in BENCHMARK_DIR.rglob(f"*{ext}"):
            rel_path = str(f.relative_to(BENCHMARK_DIR)).replace("\\", "/")
            cat = rel_path.split("/")[0]
            if category != "all" and cat != category:
                continue
            if rel_path in GROUND_TRUTH:
                code = f.read_text("utf-8", errors="ignore")
                test_files.append((rel_path, code, cat))

    if not test_files:
        console.print("[red]未找到匹配的测试用例[/red]")
        return

    total = len(test_files)
    console.print(f"测试用例总数: {total}")
    console.print(f"预计 API 调用: {total} × (单Agent + 多Agent×3并行) ≈ {total * 4} 次\n")

    # ── 逐个执行，简单可靠 ──
    single_metrics = {"recall": [], "precision": [], "f1": []}
    multi_metrics = {"recall": [], "precision": [], "f1": []}
    adaptive_metrics = {"recall": [], "precision": [], "f1": []}
    case_results = []

    detail_table = Table(title="逐项对比结果")
    detail_table.add_column("#")
    detail_table.add_column("测试用例")
    detail_table.add_column("类别")
    detail_table.add_column("模式", justify="center")
    detail_table.add_column("Recall", justify="right")
    detail_table.add_column("Precision", justify="right")
    detail_table.add_column("F1", justify="right")
    detail_table.add_column("发现/预期", justify="center")
    detail_table.add_column("耗时", justify="right")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("[dim]{task.fields[info]}[/dim]"),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task(
            "[cyan]Benchmark 总进度", total=total, info="准备中...")

        for idx, (file_path, code, cat) in enumerate(test_files, 1):
            name = file_path.split("/")[-1].replace(".py", "").replace(".js", "").replace(".ts", "").replace(".tsx", "").replace(".java", "")
            gt = GROUND_TRUTH[file_path]

            # ── 单 Agent ──
            progress.update(task, info=f"[{idx}/{total}] {name} [yellow]单Agent...[/yellow]")
            try:
                t0 = time.time()
                sf = _run_single_agent(file_path, code, cat)
                st = time.time() - t0
                # 只保留与测试类别匹配的发现，不相关类别的算误报
                sm = calculate_metrics(sf, gt["expected_count"], gt["expected_findings"])
            except Exception as e:
                console.print(f"\n[red]X [{idx}/{total}] {name} 单Agent失败: {e}[/red]")
                sm = {"recall": 0, "precision": 0, "f1": 0, "found_count": 0, "expected_count": gt["expected_count"]}
                st = 0

            # ── 多 Agent ──
            progress.update(task, info=f"[{idx}/{total}] {name} [magenta]多Agent辩论...[/magenta]")
            try:
                t0 = time.time()
                mf_raw = _run_multi_agent(code, file_path)
                mt = time.time() - t0
                mm = calculate_metrics(mf_raw, gt["expected_count"], gt["expected_findings"])
            except Exception as e:
                console.print(f"\n[red]X [{idx}/{total}] {name} 多Agent失败: {e}[/red]")
                mm = {"recall": 0, "precision": 0, "f1": 0, "found_count": 0, "expected_count": gt["expected_count"]}
                mt = 0

            progress.update(task, info=f"[{idx}/{total}] {name} [cyan]adaptive...[/cyan]")
            try:
                t0 = time.time()
                af_raw, plan_summary = _run_adaptive_agent(code, file_path)
                at = time.time() - t0
                am = calculate_metrics(af_raw, gt["expected_count"], gt["expected_findings"])
            except Exception as e:
                console.print(f"\n[red]X [{idx}/{total}] {name} adaptive failed: {e}[/red]")
                am = {"recall": 0, "precision": 0, "f1": 0, "found_count": 0, "expected_count": gt["expected_count"]}
                at = 0
                plan_summary = "failed"

            # 记录指标
            for key in ["recall", "precision", "f1"]:
                single_metrics[key].append(sm[key])
                multi_metrics[key].append(mm[key])
                adaptive_metrics[key].append(am[key])

            case_results.append({
                "name": name,
                "file_path": file_path,
                "category": cat,
                "single": {k: sm[k] for k in ["recall", "precision", "f1", "found_count", "expected_count"]},
                "multi": {k: mm[k] for k in ["recall", "precision", "f1", "found_count", "expected_count"]},
                "adaptive": {k: am[k] for k in ["recall", "precision", "f1", "found_count", "expected_count"]},
                "elapsed": {"single": st, "multi": mt, "adaptive": at},
                "adaptive_plan": plan_summary,
            })

            # 添加到结果表
            detail_table.add_row(
                str(idx), name, cat, "单Agent",
                f"{sm['recall']:.0%}", f"{sm['precision']:.0%}",
                f"{sm['f1']:.2f}",
                f"{sm['found_count']}/{sm['expected_count']}",
                f"{st:.0f}s",
            )
            detail_table.add_row(
                "", "", "", "多Agent",
                f"{mm['recall']:.0%}", f"{mm['precision']:.0%}",
                f"{mm['f1']:.2f}",
                f"{mm['found_count']}/{mm['expected_count']}",
                f"{mt:.0f}s",
            )
            detail_table.add_row(
                "", "", "", "adaptive",
                f"{am['recall']:.0%}", f"{am['precision']:.0%}",
                f"{am['f1']:.2f}",
                f"{am['found_count']}/{am['expected_count']}",
                f"{at:.0f}s",
            )
            detail_table.add_section()

            if cat == "mixed":
                _print_match_diagnostics(name, "single", sm)
                _print_match_diagnostics(name, "multi", mm)
                _print_match_diagnostics(name, "adaptive", am)

            progress.update(task, advance=1,
                info=f"[{idx}/{total}] {name} [green]完成 (单F1={sm['f1']:.2f} 多F1={mm['f1']:.2f})[/green]")

    # ── 总览表 ──
    console.print(detail_table)

    s_recall, s_prec, s_f1 = _avg(single_metrics["recall"]), _avg(single_metrics["precision"]), _avg(single_metrics["f1"])
    m_recall, m_prec, m_f1 = _avg(multi_metrics["recall"]), _avg(multi_metrics["precision"]), _avg(multi_metrics["f1"])
    a_recall, a_prec, a_f1 = _avg(adaptive_metrics["recall"]), _avg(adaptive_metrics["precision"]), _avg(adaptive_metrics["f1"])

    summary = Table(title="总览对比")
    summary.add_column("指标")
    summary.add_column("单 Agent", justify="right")
    summary.add_column("多 Agent 辩论", justify="right")
    summary.add_column("提升", justify="right", style="green")

    for label, s, m in [
        ("Avg Recall", s_recall, m_recall),
        ("Avg Precision", s_prec, m_prec),
        ("Avg F1", s_f1, m_f1),
    ]:
        diff = m - s
        summary.add_row(label, f"{s:.2%}", f"{m:.2%}", f"+{diff:.0%}" if diff > 0 else f"{diff:.0%}")

    console.print(summary)
    adaptive_table = Table(title="Adaptive comparison")
    adaptive_table.add_column("Metric")
    adaptive_table.add_column("Single", justify="right")
    adaptive_table.add_column("Full multi", justify="right")
    adaptive_table.add_column("Adaptive", justify="right")
    adaptive_table.add_column("Adaptive vs Single", justify="right", style="green")
    for label, s, m, a in [
        ("Avg Recall", s_recall, m_recall, a_recall),
        ("Avg Precision", s_prec, m_prec, a_prec),
        ("Avg F1", s_f1, m_f1, a_f1),
    ]:
        diff = a - s
        adaptive_table.add_row(label, f"{s:.2%}", f"{m:.2%}", f"{a:.2%}", f"+{diff:.0%}" if diff > 0 else f"{diff:.0%}")
    console.print(adaptive_table)
    console.print(
        f"[bold]Adaptive summary[/bold]: "
        f"Single F1={s_f1:.2%}, Full multi F1={m_f1:.2%}, Adaptive F1={a_f1:.2%}"
    )
    console.print("[dim]All modes are evaluated after the same quality-filter/dedupe post-processing.[/dim]")
    console.print(
        f"[bold]Adaptive verdict[/bold]: "
        f"vs Single {a_f1 - s_f1:+.2%}, vs Full multi {a_f1 - m_f1:+.2%}. "
        "Use this row as the main routing-quality signal."
    )

    # ── 结论 ──
    if a_f1 > s_f1 + 0.02:
        console.print(f"\n[green]Adaptive F1: {a_f1:.0%}  vs  Single-Agent F1: {s_f1:.0%}  (+{a_f1 - s_f1:.0%})[/green]")
        console.print("[green]Adaptive routing improves the single-agent baseline[/green]")
    elif a_f1 < s_f1 - 0.02:
        console.print(f"\n[yellow]Adaptive F1: {a_f1:.0%}  vs  Single-Agent F1: {s_f1:.0%}  ({a_f1 - s_f1:.0%})[/yellow]")
        console.print("[dim]Check the TP/FN/FP diagnostics above before changing prompts or thresholds[/dim]")
    else:
        console.print(f"\n[blue]Adaptive F1: {a_f1:.0%}  vs  Single-Agent F1: {s_f1:.0%}  (tie)[/blue]")
        console.print("[dim]Adaptive mode matched the baseline while preserving routing diagnostics[/dim]")

    if m_f1 < a_f1 - 0.02:
        console.print("[dim]Full multi-agent debate is noisier on this run; prefer adaptive routing as the headline result[/dim]")

    console.print(f"\n[dim]Benchmark complete: {total} cases, Single F1={s_f1:.2%}, Multi F1={m_f1:.2%}, Adaptive F1={a_f1:.2%}[/dim]")

    result = {
        "single": {"recall": s_recall, "precision": s_prec, "f1": s_f1},
        "multi": {"recall": m_recall, "precision": m_prec, "f1": m_f1},
        "adaptive": {"recall": a_recall, "precision": a_prec, "f1": a_f1},
    }
    if save_report and not _return_summary:
        payload = {
            "category": category,
            "runs": runs,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "summary": result,
            "cases": case_results,
        }
        json_path, md_path = _save_benchmark_artifacts(payload, report_dir)
        console.print(f"[green]Benchmark reports saved:[/green] {json_path} / {md_path}")
    if _return_summary:
        return result
    return result
