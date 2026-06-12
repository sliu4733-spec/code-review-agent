"""主审查流程编排"""

import json
import os
import re
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.table import Table

from src.agents.security import SecurityAgent
from src.agents.performance import PerformanceAgent
from src.agents.maintainability import MaintainabilityAgent
from src.agents.arbiter import ArbiterAgent
from src.core.debate import detect_conflicts, build_debate_prompt, conduct_debate
from src.core.knowledge import KnowledgeBase
from src.core.cache import ReviewCache

console = Console()

# 支持的文件扩展名
PYTHON_EXTS = {".py"}
JS_EXTS = {".js", ".ts", ".jsx", ".tsx"}
JAVA_EXTS = {".java"}
GO_EXTS = {".go"}
SUPPORTED_EXTS = PYTHON_EXTS | JS_EXTS | JAVA_EXTS | GO_EXTS


def read_target(target: str) -> list[tuple[str, str]]:
    """读取目标文件，支持目录递归。跳过测试样本和缓存目录"""
    path = Path(target)
    skip_dirs = {"test_cases", "__pycache__", ".venv", "node_modules", ".git"}
    files = []
    if path.is_file():
        if path.suffix in SUPPORTED_EXTS:
            code = path.read_text(encoding="utf-8", errors="ignore")
            files.append((str(path), code))
    elif path.is_dir():
        for ext in SUPPORTED_EXTS:
            for f in path.rglob(f"*{ext}"):
                # 跳过测试样本和缓存
                if any(s in f.parts for s in skip_dirs):
                    continue
                code = f.read_text(encoding="utf-8", errors="ignore")
                files.append((str(f), code))
    return files


def _extract_signatures(code: str, file_path: str) -> dict:
    """提取单个文件的函数/类签名和导入信息"""
    sigs = {"path": file_path, "imports": [], "defs": [], "classes": []}
    for line in code.split("\n"):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            sigs["imports"].append(stripped)
        elif stripped.startswith("def ") or stripped.startswith("async def "):
            sigs["defs"].append(stripped.split(":")[0])
        elif stripped.startswith("class "):
            sigs["classes"].append(stripped.split(":")[0])
    return sigs


def _build_project_context(files: list[tuple[str, str]]) -> str:
    """构建项目结构摘要，供 Agent 了解全局上下文"""
    all_sigs = [_extract_signatures(code, path) for path, code in files]
    lines = ["## 项目结构概览", f"文件总数: {len(files)}", ""]
    for s in all_sigs:
        rel = Path(s["path"]).name
        lines.append(f"### {rel}")
        if s["imports"]:
            for imp in s["imports"][:10]:
                lines.append(f"  import: {imp}")
        for d in s["defs"]:
            lines.append(f"  {d}")
        for c in s["classes"]:
            lines.append(f"  {c}")
        lines.append("")
    lines.append("## 跨文件引用检查")
    lines.append("请关注: 导入的模块是否存在? 调用的函数/类是否已在其他文件中定义?")
    return "\n".join(lines)


def _inject_finding_ids(findings: list, file_path: str) -> list:
    """为每个 finding 注入基于内容的稳定 ID"""
    for f in findings:
        f.set_id(file_path)
    return findings


def _parse_line_range(f) -> tuple[int, int]:
    """解析行号范围为 (start, end) 元组"""
    try:
        r = f.line_range.replace("L", "").replace(" ", "")
        parts = r.split("-")
        s = int(parts[0])
        e = int(parts[1]) if len(parts) > 1 else s
        return s, e
    except:
        return 0, 0


def _group_by_line_conflict(findings: list) -> tuple[list, list]:
    """按 line_range 重叠分组：无冲突的直通，有冲突的分组"""
    if len(findings) <= 1:
        return findings, []

    clean = []
    conflict_groups = []
    used = set()

    for i, f1 in enumerate(findings):
        if i in used:
            continue
        s1, e1 = _parse_line_range(f1)
        if s1 == 0:
            clean.append(f1)
            continue

        group = [f1]
        for j, f2 in enumerate(findings):
            if j <= i or j in used:
                continue
            s2, e2 = _parse_line_range(f2)
            if s2 == 0:
                continue
            # 行号区间有交集 → 冲突
            if max(s1, s2) <= min(e1, e2) and f1.category != f2.category:
                group.append(f2)
                used.add(j)

        if len(group) > 1:
            conflict_groups.append(group)
        else:
            clean.append(f1)
        used.add(i)

    return clean, conflict_groups


def _apply_adjudication(conflict_groups: list[list], instructions: list[dict]) -> list:
    """执行裁决指令：keep → 保留, discard → 丢弃, merge → 合并"""
    # 建立 finding_id → finding 的索引
    id_map = {}
    for group in conflict_groups:
        for f in group:
            id_map[f.finding_id] = f

    kept_ids = set()
    discarded_ids = set()
    merged = []

    for inst in instructions:
        action = inst.get("action")
        fid = inst.get("finding_id", "")
        if not fid or fid not in id_map:
            continue

        if action == "keep":
            kept_ids.add(fid)
        elif action == "discard":
            discarded_ids.add(fid)
        elif action == "merge":
            primary_id = inst.get("primary_id", "")
            supplement_id = inst.get("supplement_id", "")
            if primary_id in id_map and supplement_id in id_map:
                primary = id_map[primary_id]
                supp = id_map[supplement_id]
                sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
                if sev_order.get(supp.severity, 5) < sev_order.get(primary.severity, 5):
                    primary.severity = supp.severity
                if supp.fix_suggestion and not primary.fix_suggestion:
                    primary.fix_suggestion = supp.fix_suggestion
                s1, e1 = _parse_line_range(primary)
                s2, e2 = _parse_line_range(supp)
                primary.line_range = f"L{min(s1,s2)}-L{max(e1,e2)}"
                kept_ids.add(primary_id)
                discarded_ids.add(supplement_id)

    # 未在指令中出现的 finding 全部保留（保守策略）
    result = []
    for group in conflict_groups:
        for f in group:
            if f.finding_id in discarded_ids:
                continue
            if f.finding_id in kept_ids or f.finding_id not in (kept_ids | discarded_ids):
                result.append(f)
    return result


def _quality_filter(findings: list) -> list:
    """统一的发现质量过滤：去重 + 去噪 + 排序。保证每次审查报告简洁准确。"""
    if not findings:
        return []

    # ---- 1. 剔除低质量发现 ----
    filtered = []
    for f in findings:
        # 跳过空标题
        if not f.title or not f.title.strip():
            continue
        # 跳过无行号的（质量太低）
        if f.line_range == "unknown" or not f.line_range:
            continue
        # 跳过低置信度且非严重的（Agent 自己都不确定）
        if f.confidence < 0.5 and f.severity not in ("critical", "high"):
            continue
        filtered.append(f)

    # ---- 2. 智能去重：标题关键词重叠 > 50% 且行号邻近 = 同一问题 ----
    def _lines(f):
        try:
            r = f.line_range.replace("L","").replace(" ","")
            parts = r.split("-")
            return int(parts[0]), int(parts[1]) if len(parts) > 1 else int(parts[0])
        except:
            return 0, 0

    def _title_words(f):
        """提取标题关键实词（jieba中文分词 + 英文分词）"""
        import re
        try:
            import jieba
        except ImportError:
            jieba = None
        t = f.title.lower()
        t = re.sub(r'[（）：:、，。！？\-_\[\]{}()\d+]', ' ', t)
        if jieba:
            words = set(jieba.cut(t))
        else:
            words = set(t.split())
        return {w.strip() for w in words if len(w.strip()) > 1}

    result = []
    used = set()
    for i, f1 in enumerate(filtered):
        if i in used:
            continue
        best = f1
        for j, f2 in enumerate(filtered):
            if j <= i or j in used:
                continue
            w1, w2 = _title_words(f1), _title_words(f2)
            if not w1 or not w2:
                continue
            overlap = len(w1 & w2) / min(len(w1), len(w2))
            s1, e1 = _lines(f1)
            s2, e2 = _lines(f2)
            line_near = abs(s1 - s2) <= 8 and s1 > 0 and s2 > 0
            same_cat = f1.category == f2.category
            # 合并规则（任一满足）：
            # 1. 同类别 + 标题高重叠(>0.6) → 同一问题不同措辞
            # 2. 同类别 + 行号邻近 + 标题中等重叠(>0.35) → 同一代码段
            # 3. 标题极高重叠(>0.75) → 即使行号不同也合并（不同Agent定位偏差）
            if same_cat and (overlap > 0.6 or (overlap > 0.35 and line_near)):
                used.add(j)
                if len(f2.description) > len(best.description):
                    best = f2
            elif overlap > 0.75:
                used.add(j)
                if len(f2.description) > len(best.description):
                    best = f2
        used.add(i)
        result.append(best)

    # ---- 3. 严重程度排序 ----
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    result.sort(key=lambda f: sev_order.get(f.severity, 4))
    return result


def _cross_file_check(files: list[tuple[str, str]], use_llm: bool = False) -> list:
    """跨文件引用检查：静态分析，跳过标准库和第三方包"""
    from src.agents.base import ReviewFinding
    # 标准库和常见第三方包，无需检查
    STDLIB = {"os", "sys", "re", "json", "time", "datetime", "pathlib", "abc",
              "concurrent", "concurrent.futures", "threading", "typing", "hashlib",
              "dataclasses", "functools", "collections", "itertools", "subprocess",
              "asyncio", "math", "random", "copy", "logging", "io", "textwrap",
              "enum", "inspect", "importlib", "unittest", "pytest", "argparse",
              "dotenv", "openai", "anthropic", "chromadb", "rich", "fastapi",
              "uvicorn", "pydantic", "flask", "werkzeug", "pymysql"}
    findings = []
    all_sigs = {Path(p).name: _extract_signatures(c, p) for p, c in files}
    all_defined = set()
    for sig in all_sigs.values():
        for d in sig["defs"]:
            all_defined.add(d.replace("def ", "").replace("async def ", "").split("(")[0].strip())
        for c in sig["classes"]:
            all_defined.add(c.replace("class ", "").split("(")[0].split(":")[0].strip())

    for sig in all_sigs.values():
        for imp in sig["imports"]:
            m = re.match(r"from\s+\.?(\S+)\s+import\s+(.+)", imp)
            if m:
                module = m.group(1)
                root = module.split(".")[0]
                if root in STDLIB:
                    continue  # 跳过标准库和第三方包
                imported = [x.strip() for x in m.group(2).split(",")]
                expected_file = module.replace(".", "/") + ".py"
                found = any(expected_file in p for p in [p for p, _ in files])
                if not found:
                    for name in imported:
                        if name != "*" and name not in all_defined:
                            findings.append(ReviewFinding(
                                category="maintainability", severity="low",
                                title=f"未解析的导入: {imp}",
                                description=f"'{module}' 未在项目中找到",
                                line_range="unknown",
                                fix_suggestion="",
                                confidence=0.3,))

    return findings

def run_single_review(code: str, file_path: str,
                      kb: KnowledgeBase | None = None,
                      cache: ReviewCache | None = None,
                      project_context: str = "",
                      prefer: str | None = None) -> tuple[list, float]:
    """单 Agent 模式：仅用通用 prompt 审查"""
    if cache:
        cached = cache.get(code, file_path, "single", project_context)
        if cached:
            console.print("  [dim]→ 缓存命中，跳过审查[/dim]")
            return cached, 0.0

    from src.agents.base import BaseAgent

    class GeneralAgent(BaseAgent):
        def get_system_prompt(self) -> str:
            return """你是一位资深代码审查专家，请全面审查代码的安全、性能、可维护性问题。
按严重程度分类，提供具体的修复建议和 CWE 编号（如适用）。
如果没发现问题，返回空的 findings 数组。"""

    agent = GeneralAgent(name="general", role_prompt="通用代码审查专家")
    hints = kb.get_dynamic_prompt_hints() if kb else ""
    few_shot = (hints + "\n" + kb.get_few_shot_examples(code)) if kb else ""
    enhanced = f"{project_context}\n\n---\n当前文件: {Path(file_path).name}\n---\n\n{code}" if project_context else code
    start = time.time()
    findings = agent.analyze(enhanced, file_path, few_shot, prefer=prefer)
    elapsed = time.time() - start

    if cache:
        cache.set(code, file_path, "single", findings, project_context)
    return findings, elapsed
#上面这段代码执行的核心就是:先读取代码文件列表->循环逐个文件调用单Agent审查

CHUNK_SIZE = 4000  # 每块最大字符数


def _chunk_code(code: str, max_chars: int = CHUNK_SIZE) -> list[tuple[int, str]]:
    """按函数/类边界智能分块，返回 [(行号偏移, 代码块), ...]"""
    if len(code) <= max_chars:
        return [(1, code)]

    chunks = []
    lines = code.split("\n")
    current = []
    current_len = 0
    line_start = 1

    for i, line in enumerate(lines, 1):
        current.append(line)
        current_len += len(line) + 1
        is_boundary = (
            line.startswith("def ") or line.startswith("class ") or
            line.startswith("async def ") or line.startswith("import ") or
            line.startswith("from ")
        )
        if current_len >= max_chars and is_boundary:
            chunks.append((line_start, "\n".join(current)))
            current = []
            current_len = 0
            line_start = i + 1

    if current:
        chunks.append((line_start, "\n".join(current)))

    return chunks


def _merge_chunk_findings(all_findings: list, chunk_offsets: list[int]) -> list:
    """合并分块审查结果，按标题去重并修正行号"""
    seen = set()
    merged = []
    for chunk_idx, findings in enumerate(all_findings):
        offset = chunk_offsets[chunk_idx] - 1
        for f in findings:
            key = f.title.lower()[:50]
            if key in seen:
                continue
            seen.add(key)
            # 尝试修正行号
            if "L" in f.line_range:
                try:
                    parts = f.line_range.replace("L", "").split("-")
                    start_l = int(parts[0]) + offset
                    if len(parts) > 1:
                        end_l = int(parts[1]) + offset
                        f.line_range = f"L{start_l}-L{end_l}"
                    else:
                        f.line_range = f"L{start_l}"
                except (ValueError, IndexError):
                    pass
            merged.append(f)
    return merged


def _agent_task(agent, code, file_path, few_shot, label, project_context="", stream=False, token_callback=None, prefer=None):
    """单个 Agent 的审查任务（供线程池调用）"""
    enhanced_code = code
    if project_context:
        enhanced_code = f"{project_context}\n\n---\n当前审查文件: {Path(file_path).name}\n---\n\n{code}"
    if stream and token_callback:
        findings = agent.analyze_stream(enhanced_code, file_path, few_shot, on_token=token_callback, prefer=prefer)
    else:
        findings = agent.analyze(enhanced_code, file_path, few_shot, prefer=prefer)
    return label, findings

def run_multi_review(code: str, file_path: str,
                     kb: KnowledgeBase | None = None,
                     cache: ReviewCache | None = None,
                     project_context: str = "",
                     stream: bool = False,
                     prefer: str | None = None) -> tuple[list, str, float]:
    """多 Agent 辩论模式（并行执行，大文件自动分块）"""
    if cache:
        cached = cache.get(code, file_path, "multi", project_context)
        if cached:
            console.print("  [dim]→ 缓存命中，跳过审查[/dim]")
            return cached, "", 0.0

    chunks = _chunk_code(code)
    use_chunking = len(chunks) > 1
    if use_chunking:
        console.print(f"  [dim]→ 大文件分块审查 ({len(chunks)} 块)[/dim]")

    hints = kb.get_dynamic_prompt_hints() if kb else ""
    few_shot = (hints + "\n" + kb.get_few_shot_examples(code)) if kb else ""
    all_sec, all_perf, all_maint = [], [], []
    chunk_offsets = []
    total_elapsed = 0.0

    for chunk_idx, (offset, chunk) in enumerate(chunks):
        if use_chunking:
            console.print(f"  [dim]  [{chunk_idx + 1}/{len(chunks)}] "
                          f"L{offset}-L{offset + len(chunk.splitlines())}[/dim]")

        agents = [
            (SecurityAgent(), "security"),
            (PerformanceAgent(), "performance"),
            (MaintainabilityAgent(), "maintainability"),
        ]

        start = time.time()
        results = {}

        # ── 3个Agent真正并行（max_workers=3），加速审查 ──
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(_agent_task, agent, chunk, file_path, few_shot, label, project_context, False, None, prefer): label
                for agent, label in agents
            }
            for future in as_completed(futures):
                label, findings = future.result()
                results[label] = findings
                if stream:
                    console.print(f"  [cyan]{label}[/cyan] 完成 → {len(findings)} 个发现")

        total_elapsed += time.time() - start
        all_sec.extend(results.get("security", []))
        all_perf.extend(results.get("performance", []))
        all_maint.extend(results.get("maintainability", []))
        chunk_offsets.append(offset)

    if use_chunking:
        all_sec = _merge_chunk_findings([[f for f in all_sec]], chunk_offsets)
        all_perf = _merge_chunk_findings([[f for f in all_perf]], chunk_offsets)
        all_maint = _merge_chunk_findings([[f for f in all_maint]], chunk_offsets)

    # ── 三步冲突处理：ID注入 → 分组 → 裁决(仅冲突部分) → 透传(无冲突部分) ──
    raw_findings = all_sec + all_perf + all_maint
    raw_findings = _inject_finding_ids(raw_findings, file_path)
    raw_findings = _quality_filter(raw_findings)

    # 按 line_range 重叠检测冲突
    clean_findings, conflict_groups = _group_by_line_conflict(raw_findings)

    if conflict_groups:
        # 只把冲突组发给 LLM 裁决
        arbiter = ArbiterAgent()
        instructions = arbiter.adjudicate(code, file_path, conflict_groups)
        resolved = _apply_adjudication(conflict_groups, instructions)
        final_findings = clean_findings + resolved
        debate_context = f"冲突裁决: {len(conflict_groups)}组 → 保留{len(resolved)}条"
    else:
        final_findings = clean_findings
        debate_context = ""

    if cache:
        cache.set(code, file_path, "multi", final_findings, project_context)
    return final_findings, debate_context, total_elapsed


def run_review(target: str, mode: str = "multi", output: str | None = None,
               use_cache: bool = True, stream: bool = False,
               prefer: str | None = None):
    """主审查入口"""
    files = read_target(target)
    if not files:
        console.print("[red]未找到可审查的文件[/red]")
        return

    target_path = Path(target)
    is_directory = target_path.is_dir()
    project_context = _build_project_context(files) if is_directory else ""

    # 自动生成报告文件名（同一目标始终覆盖，避免遗留旧文件）
    if not output:
        name = target_path.name if not is_directory else target_path.name
        output = f"review_{name}.md"

    console.print(f"[bold]审查: {target}[/bold] → 报告: {output}")
    console.print(f"模式: {'多Agent (3并行)' if mode == 'multi' else '单Agent'}  |  "
                  f"文件数: {len(files)}  |  {'流式输出' if stream else '静默等待...'}")
    console.print("-" * 50)

    kb = KnowledgeBase()
    cache = ReviewCache() if use_cache else None

    all_reports = []
    for file_path, code in files:
        console.print(f"[{len(all_reports) + 1}/{len(files)}] {Path(file_path).name} ", end="", highlight=False)

        if mode == "single":
            findings, elapsed = run_single_review(
                code, file_path, kb, cache, project_context, prefer)
            debate_summary = ""
        else:
            findings, debate_summary, elapsed = run_multi_review(
                code, file_path, kb, cache, project_context, stream=stream, prefer=prefer)

        # 统一质量过滤：去重 + 去噪
        findings = _quality_filter(findings)
        kb.store_review(file_path, code, findings)

        # 写报告
        if findings:
            arbiter = ArbiterAgent()
            report = arbiter.generate_report(
                findings, file_path, debate_summary, mode)
            all_reports.append((file_path, report, findings))

        # 终端摘要
        sev = {}
        for f in findings:
            sev[f.severity] = sev.get(f.severity, 0) + 1
        parts = [f"{c}={sev[c]}" for c in ["critical", "high", "medium", "low"] if c in sev]
        if parts:
            console.print(f"[green]OK[/green] ({len(findings)}个发现: {', '.join(parts)})  {elapsed:.0f}s")
        else:
            console.print(f"[dim]OK (0个发现)[/dim]  {elapsed:.0f}s")

    # 跨文件检查
    if is_directory:
        cross_findings = _cross_file_check(files)
        if cross_findings:
            arbiter = ArbiterAgent()
            cross_report = arbiter.generate_report(
                cross_findings, target, "# 跨文件引用检查", mode)
            all_reports.append((target, cross_report, cross_findings))

    # 写报告：先总览，再逐个文件
    total_findings = sum(len(f) for _, _, f in all_reports)
    summary_lines = [
        f"# 代码审查报告",
        f"",
        f"> {time.strftime('%Y-%m-%d %H:%M:%S')} | {mode} | {len(files)} 文件 | {total_findings} 个问题",
        f"",
        f"| 文件 | 严重 | 高危 | 中等 | 建议 |",
        f"|------|------|------|------|------|",
    ]
    seen_names = {}
    for fp, _, f_list in all_reports:
        n = Path(fp).name
        # 同名文件加父目录区分
        if n in seen_names:
            parent = Path(fp).parent.name
            display = f"{parent}/{n}"
        else:
            display = n
        seen_names[n] = seen_names.get(n, 0) + 1
        sev = {}
        for ff in f_list:
            sev[ff.severity] = sev.get(ff.severity, 0) + 1
        summary_lines.append(
            f"| {display} | {sev.get('critical',0)} | {sev.get('high',0)} | "
            f"{sev.get('medium',0)} | {sev.get('low',0)} |")
    summary_lines.append("")
    summary_lines.append("---")
    summary_lines.append("")

    combined = "\n".join(summary_lines) + "\n\n".join(r for _, r, _ in all_reports)
    Path(output).write_text(combined, encoding="utf-8")
    console.print("-" * 50)
    console.print(f"[bold green]报告已保存: {output}[/bold green]  ({total_findings} 个问题)")

    # ── 交互式反馈（可选） ──
    if total_findings > 0 and sys.stdin.isatty():
        console.print("\n[bold cyan]反馈引导[/bold cyan] — [yellow]r 5[/yellow]=仅驳回第5条(其余自动确认)  [green]c 3[/green]=确认第3条  [dim]done[/dim]=全部确认并结束")
        indexed = []
        for fp, _, f_list in all_reports:
            for f in f_list:
                indexed.append((fp, f))
        for i, (fp, f) in enumerate(indexed[:20]):
            sv = {"critical": "CRIT", "high": "HIGH", "medium": "MED", "low": "LOW"}.get(f.severity, "")
            fid = f.finding_id[:8] if f.finding_id else ""
            console.print(f"  {i+1:2d}. [{sv}] [{fid}] {Path(fp).name}: {f.title[:60]}")
        console.print("[dim]输入操作:[/dim] ", end="")
        sys.stdout.flush()

        rejected = set()
        confirmed = set()
        try:
            line = sys.stdin.readline().strip().lower()
            while line and line != "done":
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    action = parts[0]
                    num = int(parts[1]) - 1
                    if 0 <= num < len(indexed):
                        if action in ("r", "reject"):
                            rejected.add(num)
                            console.print(f"  [yellow]已驳回 #{num+1}[/yellow]")
                        elif action in ("c", "confirm"):
                            confirmed.add(num)
                            console.print(f"  [green]已确认 #{num+1}[/green]")
                console.print("[dim]下一个:[/dim] ", end="")
                sys.stdout.flush()
                line = sys.stdin.readline().strip().lower()
        except (EOFError, KeyboardInterrupt):
            pass

        # 提交反馈：驳回的 + 其余全部自动确认
        auto_confirmed = 0
        for i, (fp, f) in enumerate(indexed):
            if i in rejected:
                kb.submit_feedback(fp, f.finding_id, "reject")
            else:
                kb.submit_feedback(fp, f.finding_id, "confirm")
                if i not in confirmed:
                    auto_confirmed += 1
        # 追加反馈标记到报告文件
        if indexed and (rejected or confirmed or True):
            fb_lines = ["\n\n---\n## 反馈记录\n"]
            for i, (fp, f) in enumerate(indexed):
                tag = "[已驳回]" if i in rejected else "[已确认]"
                fb_lines.append(f"- {tag} {Path(fp).name}: {f.title[:60]}")
            with open(output, "a", encoding="utf-8") as rf:
                rf.write("\n".join(fb_lines))
        if rejected:
            console.print(f"[dim]已驳回 {len(rejected)} 条, 其余 {len(indexed) - len(rejected)} 条自动确认, 已写入报告[/dim]")
        else:
            console.print(f"[dim]全部 {len(indexed)} 条已确认, 已写入报告[/dim]")


def _print_terminal_summary(findings: list, elapsed: float, mode: str):
    """终端摘要输出"""
    table = Table(title=f"审查结果 ({elapsed:.1f}s)")
    table.add_column("严重程度", style="bold")
    table.add_column("标题")
    table.add_column("置信度")

    colors = {
        "critical": "red", "high": "orange3",
        "medium": "yellow", "low": "blue"
    }

    for f in findings:
        table.add_row(
            f"[{colors.get(f.severity, 'white')}]{f.severity}[/{colors.get(f.severity, 'white')}]",
            f.title[:60],
            f"{f.confidence:.0%}" if f.confidence else "-"
        )

    if findings:
        console.print(table)
    else:
        console.print("[green]✓ 未发现问题[/green]")
