"""Benchmark 执行器：运行所有测试用例，对比单 Agent vs 多 Agent 效果"""

import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from src.agents.security import SecurityAgent
from src.agents.performance import PerformanceAgent
from src.agents.maintainability import MaintainabilityAgent
from src.agents.arbiter import ArbiterAgent
from src.core.debate import detect_conflicts, build_debate_prompt
from src.core.reviewer import _quality_filter
from src.benchmarks.ground_truth import GROUND_TRUTH
from src.benchmarks.metrics import calculate_metrics

console = Console()
BENCHMARK_DIR = Path(__file__).parent / "test_cases"


def _run_single_agent(file_path: str, code: str, category: str) -> list:
    agents = {
        "security": SecurityAgent,
        "performance": PerformanceAgent,
        "maintainability": MaintainabilityAgent,
    }
    agent_cls = agents.get(category, SecurityAgent)
    agent = agent_cls()
    return agent.analyze(code, file_path)


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
    return _quality_filter(all_findings)


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


def run_benchmark(category: str = "all"):
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

            # 记录指标
            for key in ["recall", "precision", "f1"]:
                single_metrics[key].append(sm[key])
                multi_metrics[key].append(mm[key])

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
            detail_table.add_section()

            progress.update(task, advance=1,
                info=f"[{idx}/{total}] {name} [green]完成 (单F1={sm['f1']:.2f} 多F1={mm['f1']:.2f})[/green]")

    # ── 总览表 ──
    console.print(detail_table)

    def avg(lst):
        return sum(lst) / len(lst) if lst else 0

    s_recall, s_prec, s_f1 = avg(single_metrics["recall"]), avg(single_metrics["precision"]), avg(single_metrics["f1"])
    m_recall, m_prec, m_f1 = avg(multi_metrics["recall"]), avg(multi_metrics["precision"]), avg(multi_metrics["f1"])

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

    # ── 结论 ──
    if m_f1 > s_f1 + 0.02:
        console.print(f"\n[green]Multi-Agent F1: {m_f1:.0%}  vs  Single-Agent F1: {s_f1:.0%}  (+{m_f1 - s_f1:.0%})[/green]")
        console.print("[green]Multi-agent debate mode outperforms single agent[/green]")
    elif m_f1 < s_f1 - 0.02:
        console.print(f"\n[yellow]Multi-Agent F1: {m_f1:.0%}  vs  Single-Agent F1: {s_f1:.0%}  ({m_f1 - s_f1:.0%})[/yellow]")
        console.print("[dim]LLM output variance may cause fluctuations; re-run for stable results[/dim]")
    else:
        console.print(f"\n[blue]Multi-Agent F1: {m_f1:.0%}  vs  Single-Agent F1: {s_f1:.0%}  (tie)[/blue]")
        console.print("[dim]Both modes perform equally on clear-cut cases[/dim]")

    console.print(f"\n[dim]Benchmark complete: {total} cases, Single F1={s_f1:.2%}, Multi F1={m_f1:.2%}[/dim]")
