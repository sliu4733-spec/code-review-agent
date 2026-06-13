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
from src.core.debate import detect_conflicts, conduct_debate
from src.core.knowledge import KnowledgeBase
from src.core.cache import ReviewCache
from src.core.router import build_review_plan, format_plan_summary
from src.core.static_evidence import collect_static_evidence, summarize_static_evidence
from src.core.reporting import normalize_findings_for_report, severity_label

console = Console()

# 支持的文件扩展名
PYTHON_EXTS = {".py"}
JS_EXTS = {".js", ".ts", ".jsx", ".tsx"}
JAVA_EXTS = {".java"}
GO_EXTS = {".go"}
SUPPORTED_EXTS = PYTHON_EXTS | JS_EXTS | JAVA_EXTS | GO_EXTS

TEST_PATH_PARTS = {"tests", "test", "__tests__", "spec"}


def _is_test_fixture_path(file_path: str) -> bool:
    normalized = str(file_path or "").replace("\\", "/").lower()
    parts = {p for p in normalized.split("/") if p}
    name = Path(normalized).name
    return bool(parts & TEST_PATH_PARTS) or name.startswith("test_") or name.endswith("_test.py")


def _review_scope_note(file_path: str, *, is_chunk: bool = False) -> str:
    notes = [
        "## 审查上下文约束",
        "- 只报告当前可见代码中有直接证据的问题。",
        "- 不要因为输入是片段、分块、项目上下文摘要或缺少文件结尾，就报告“文件截断、docstring 未闭合、函数不完整、代码不完整”等问题。",
    ]
    if is_chunk:
        notes.append("- 当前输入是大文件分块，请按片段内真实代码问题审查，不要把分块边界当成缺陷。")
    if _is_test_fixture_path(file_path):
        notes.append(
            "- 当前文件位于 tests/test/spec 路径。SQL/XSS/危险 API 片段可能是测试夹具或故意构造的样例，"
            "不要按生产漏洞上报；只有测试代码本身会在运行时造成真实风险时才报告，并降级说明为测试夹具风险。"
        )
    return "\n".join(notes)


def _compose_review_input(code: str, file_path: str, project_context: str = "",
                          *, is_chunk: bool = False) -> str:
    note = _review_scope_note(file_path, is_chunk=is_chunk)
    if project_context:
        return f"{note}\n\n{project_context}\n\n---\n当前审查文件: {Path(file_path).name}\n---\n\n{code}"
    return f"{note}\n\n{code}"


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


def _quality_filter(findings: list, file_path: str = "") -> list:
    """统一的发现质量过滤：去重 + 去噪 + 排序。保证每次审查报告简洁准确。"""
    if not findings:
        return []

    def _raw_text(f):
        evidence = getattr(f, "evidence", []) or []
        if isinstance(evidence, str):
            evidence = [evidence]
        return " ".join([
            f.title or "",
            f.description or "",
            f.fix_suggestion or "",
            " ".join(map(str, evidence)),
        ]).lower()

    def _is_self_contradictory(f) -> bool:
        text = _raw_text(f)
        title = (f.title or "").lower()
        secret_like = any(term in title for term in ["secret", "api key", "apikey", "token", "密钥"])
        no_secret = any(phrase in text for phrase in [
            "no actual secret", "no real secret", "not a secret",
            "no secret", "no fix needed", "没有实际密钥", "不是密钥",
        ])
        if secret_like and no_secret:
            return True
        if secret_like and ("'ollama'" in text or "空字符串" in text):
            return True
        return False

    def _is_truncation_hallucination(f) -> bool:
        if getattr(f, "source", "llm") != "llm":
            return False
        text = _raw_text(f)
        truncation_terms = [
            "truncated file", "truncated source", "source is truncated",
            "cut off", "ends abruptly", "incomplete code", "incomplete function",
            "incomplete file", "unterminated", "docstring", "triple-quoted",
            "文件截断", "代码截断", "源文件截断", "函数不完整", "文件不完整", "代码不完整",
            "三引号", "未闭合", "文档字符串未闭合",
        ]
        return any(term in text for term in truncation_terms)

    def _mark_test_fixture_context(f) -> None:
        if not _is_test_fixture_path(file_path):
            return
        if getattr(f, "source", "llm") != "llm":
            return
        text = _raw_text(f)
        fixture_terms = [
            "sql", "xss", "innerhtml", "fetch", "dangerouslysetinnerhtml",
            "injection", "注入", "跨站", "漏洞", "n+1", "promise.all",
        ]
        if f.category not in ("security", "performance") or not any(term in text for term in fixture_terms):
            return
        if f.severity in ("critical", "high", "medium"):
            f.severity = "low"
        f.confidence = min(f.confidence or 0.0, 0.6)
        if "测试夹具" not in (f.title or ""):
            f.title = f"测试夹具示例风险: {f.title}"
        if "测试文件" not in (f.description or ""):
            f.description = (
                "该发现位于测试文件，默认按测试夹具或故意构造的样例代码处理；"
                f"原说明: {f.description}"
            )

    def _downgrade_overstated_maintainability(f) -> None:
        if getattr(f, "source", "llm") != "llm" or f.category != "maintainability":
            return
        title = (f.title or "").lower()
        if "__init__.py" in title and ("空" in title or "empty" in title):
            f.severity = "low"
            return
        if f.severity not in ("critical", "high"):
            return
        text = _raw_text(f)
        bug_terms = ["未定义", "nameerror", "崩溃", "crash", "runtime", "运行时"]
        if any(term in text for term in bug_terms):
            return
        soft_terms = ["职责", "复杂", "重复", "硬编码", "过长", "双重导入", "too long", "duplicated", "complex"]
        if any(term in text for term in soft_terms):
            f.severity = "medium"

    # ---- 1. 剔除低质量发现 ----
    filtered = []
    for f in findings:
        # 跳过空标题
        if not f.title or not f.title.strip():
            continue
        if _is_self_contradictory(f):
            continue
        if _is_truncation_hallucination(f):
            continue
        _mark_test_fixture_context(f)
        _downgrade_overstated_maintainability(f)
        # 跳过无行号的（质量太低）
        if f.line_range == "unknown" or not f.line_range:
            continue
        source = getattr(f, "source", "llm")
        # 跳过低置信度且非严重的（静态工具证据更稳定，适当放宽）
        if source == "llm" and f.confidence < 0.55 and f.severity not in ("critical", "high"):
            continue
        if source != "llm" and f.confidence < 0.45 and f.severity not in ("critical", "high"):
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

    def _finding_text(f):
        evidence = getattr(f, "evidence", []) or []
        if isinstance(evidence, str):
            evidence = [evidence]
        return " ".join([f.title or "", f.description or "", f.fix_suggestion or "", " ".join(map(str, evidence))]).lower()

    def _patterns(f):
        text = _finding_text(f)
        checks = {
            "sql-string-construction": ["sql", "injection", "select", "execute", "query"],
            "xss-html-sink": ["xss", "innerhtml", "dangerouslysetinnerhtml", "render_template_string", "html"],
            "hardcoded-secret": ["hardcoded", "secret", "password", "token", "api_key"],
            "shell-command-execution": ["command injection", "os.system", "subprocess", "shell"],
            "path-file-access": ["path traversal", "file", "open", "readfile"],
            "unsafe-deserialization": ["pickle", "deserialization", "yaml.load"],
            "weak-crypto-random": ["math.random", "random", "md5", "sha1"],
            "loop-database-or-api-call": ["n+1", "loop", "query", "fetch", "await"],
            "nested-loop": ["o(n", "nested", "loop"],
            "bulk-file-read": ["memory", "readlines", "stream", "readall"],
            "serial-async": ["serial", "await", "promise.all"],
            "weak-typing": ["any", "type"],
            "broad-empty-error-handling": ["empty", "catch", "except", "error handling"],
            "large-complex-block": ["god", "component", "responsibility", "long"],
        }
        found = []
        for name, needles in checks.items():
            if any(needle in text for needle in needles):
                found.append(name)
        return found or [f.category]

    def _rank(f):
        sev_score = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}.get(f.severity, 1)
        source_bonus = {"semgrep": 0.8, "bandit": 0.7, "ruff": 0.45, "llm": 0}.get(getattr(f, "source", "llm"), 0)
        evidence_bonus = 0.25 if getattr(f, "evidence", []) else 0
        confidence = f.confidence or 0
        line_bonus = 0.15 if _lines(f) != (0, 0) else -0.4
        fix_bonus = 0.15 if f.fix_suggestion else -0.1
        return sev_score + confidence + source_bonus + evidence_bonus + line_bonus + fix_bonus

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
    # ---- 3. 模式级去重：同类、同模式、相邻行只保留更可信的一条 ----
    compact = []
    used = set()
    for i, f1 in enumerate(result):
        if i in used:
            continue
        best = f1
        p1 = set(_patterns(f1))
        s1, _ = _lines(f1)
        for j, f2 in enumerate(result):
            if j <= i or j in used:
                continue
            if f1.category != f2.category:
                continue
            p2 = set(_patterns(f2))
            s2, _ = _lines(f2)
            same_pattern = bool(p1 & p2)
            line_near = s1 > 0 and s2 > 0 and abs(s1 - s2) <= 8
            if same_pattern and (line_near or _title_words(f1) & _title_words(f2)):
                used.add(j)
                if _rank(f2) > _rank(best):
                    best = f2
        used.add(i)
        compact.append(best)

    # ---- 4. 每类 top-k，防止某个专家刷屏拉低 precision ----
    caps = {"security": 6, "performance": 5, "maintainability": 5}
    by_cat = {}
    for f in compact:
        by_cat.setdefault(f.category, []).append(f)

    final = []
    for cat, items in by_cat.items():
        items.sort(key=_rank, reverse=True)
        final.extend(items[:caps.get(cat, 4)])

    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    final.sort(key=lambda f: (sev_order.get(f.severity, 4), -_rank(f)))
    return normalize_findings_for_report(final)


def _cross_file_check(files: list[tuple[str, str]], use_llm: bool = False) -> list:
    """跨文件引用检查：静态分析，跳过标准库和第三方包"""
    from src.agents.base import ReviewFinding
    # 标准库和常见第三方包，无需检查。stdlib_module_names 覆盖 __future__、statistics 等。
    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    stdlib.update({
        "os", "sys", "re", "json", "time", "datetime", "pathlib", "abc",
        "concurrent", "threading", "typing", "hashlib", "dataclasses",
        "functools", "collections", "itertools", "subprocess", "asyncio",
        "math", "random", "copy", "logging", "io", "textwrap", "enum",
        "inspect", "importlib", "unittest", "argparse",
    })
    third_party = {
        "pytest", "dotenv", "openai", "anthropic", "chromadb", "rich",
        "fastapi", "uvicorn", "pydantic", "flask", "werkzeug", "pymysql",
        "yaml", "jieba", "bandit", "ruff",
    }

    def _module_names_for_path(file_path: str) -> set[str]:
        normalized = str(file_path).replace("\\", "/")
        if not normalized.endswith(".py"):
            return set()
        parts = [p for p in normalized.split("/") if p]
        if not parts:
            return set()

        if parts[-1] == "__init__.py":
            module_parts = parts[:-1]
        else:
            module_parts = parts[:-1] + [parts[-1][:-3]]

        candidates = set()
        for start in range(len(module_parts)):
            candidate = ".".join(module_parts[start:])
            if candidate:
                candidates.add(candidate)
        return candidates

    available_modules = set()
    for fp, _ in files:
        available_modules.update(_module_names_for_path(fp))

    def _module_is_external(module: str) -> bool:
        root = module.split(".")[0]
        return root in stdlib or root in third_party

    def _module_is_available(module: str) -> bool:
        if module in available_modules:
            return True
        return any(m == module or m.startswith(module + ".") for m in available_modules)

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
                if _module_is_external(module) or _module_is_available(module):
                    continue
                imported = [x.strip() for x in m.group(2).split(",")]
                for name in imported:
                    clean_name = name.split(" as ")[0].strip()
                    if clean_name != "*" and clean_name not in all_defined:
                        findings.append(ReviewFinding(
                            category="maintainability", severity="low",
                            title=f"未解析的导入: {imp}",
                            description=f"'{module}' 未在项目中找到",
                            line_range="unknown",
                            fix_suggestion="",
                            confidence=0.3,))

    return findings


def build_project_context(files: list[tuple[str, str]]) -> str:
    """Public wrapper for Web/UI callers."""
    return _build_project_context(files)


def cross_file_check(files: list[tuple[str, str]], use_llm: bool = False) -> list:
    """Public wrapper for cross-file import validation."""
    return _cross_file_check(files, use_llm=use_llm)


def quality_filter(findings: list, file_path: str = "") -> list:
    """Public wrapper for report finding cleanup."""
    return _quality_filter(findings, file_path)


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
    hints = kb.get_dynamic_prompt_hints(file_path=file_path) if kb else ""
    few_shot = (hints + "\n" + kb.get_few_shot_examples(code, file_path=file_path)) if kb else ""
    enhanced = _compose_review_input(code, file_path, project_context)
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


def _agent_task(agent, code, file_path, few_shot, label, project_context="", stream=False,
                token_callback=None, prefer=None, is_chunk=False):
    """单个 Agent 的审查任务（供线程池调用）"""
    enhanced_code = _compose_review_input(code, file_path, project_context, is_chunk=is_chunk)
    if stream and token_callback:
        findings = agent.analyze_stream(enhanced_code, file_path, few_shot, on_token=token_callback, prefer=prefer)
    else:
        findings = agent.analyze(enhanced_code, file_path, few_shot, prefer=prefer)
    return label, findings


def _agent_for_label(label: str):
    agents = {
        "security": SecurityAgent,
        "performance": PerformanceAgent,
        "maintainability": MaintainabilityAgent,
    }
    return agents[label]()


def _resolve_findings(code: str, file_path: str, findings: list) -> tuple[list, str]:
    raw_findings = _inject_finding_ids(findings, file_path)
    raw_findings = _quality_filter(raw_findings, file_path)
    clean_findings, conflict_groups = _group_by_line_conflict(raw_findings)

    if not conflict_groups:
        return clean_findings, ""

    arbiter = ArbiterAgent()
    instructions = arbiter.adjudicate(code, file_path, conflict_groups)
    resolved = _apply_adjudication(conflict_groups, instructions)
    return clean_findings + resolved, f"Conflict adjudication: {len(conflict_groups)} groups -> kept {len(resolved)} findings"


def _finding_tokens(f) -> set[str]:
    import re

    text = " ".join([f.title or "", f.description or "", f.fix_suggestion or ""]).lower()
    return {token for token in re.split(r"[^a-z0-9_]+", text) if len(token) > 2}


def _is_same_or_near_issue(left, right) -> bool:
    if left.category != right.category:
        return False

    l_start, l_end = _parse_line_range(left)
    r_start, r_end = _parse_line_range(right)
    line_overlap = (
        l_start > 0
        and r_start > 0
        and max(l_start, r_start) <= min(l_end, r_end) + 8
    )
    if line_overlap:
        return True

    left_tokens = _finding_tokens(left)
    right_tokens = _finding_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens)) >= 0.45


def _select_specialist_supplements(base_findings: list, specialist_findings: list,
                                   file_path: str) -> list:
    """Keep general findings as baseline; add only conservative specialist supplements."""
    if not specialist_findings:
        return []

    base_categories = {f.category for f in base_findings}
    candidates = _inject_finding_ids(specialist_findings, file_path)
    candidates = _quality_filter(candidates, file_path)

    selected = []
    per_category = {}
    for finding in candidates:
        if any(_is_same_or_near_issue(finding, existing) for existing in base_findings + selected):
            continue

        severity_ok = finding.severity in ("critical", "high")
        confidence = finding.confidence or 0.0

        fills_missing_category = finding.category not in base_categories and confidence >= 0.82
        very_strong_same_category = finding.severity == "critical" and confidence >= 0.92
        if not severity_ok or not (fills_missing_category or very_strong_same_category):
            continue

        if per_category.get(finding.category, 0) >= 1:
            continue

        selected.append(finding)
        per_category[finding.category] = per_category.get(finding.category, 0) + 1

    return selected


def _resolve_adaptive_findings(code: str, file_path: str, general_findings: list,
                               specialist_findings: list, static_findings: list) -> tuple[list, str]:
    base_findings, base_summary = _resolve_findings(code, file_path, general_findings + static_findings)
    supplements = _select_specialist_supplements(base_findings, specialist_findings, file_path)

    if not supplements:
        return base_findings, base_summary + "\nExpert supplements: kept 0"

    final_findings = base_findings + supplements
    final_findings = _inject_finding_ids(final_findings, file_path)
    return final_findings, base_summary + f"\nExpert supplements: kept {len(supplements)}"


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

    hints = kb.get_dynamic_prompt_hints(file_path=file_path) if kb else ""
    few_shot = (hints + "\n" + kb.get_few_shot_examples(code, file_path=file_path)) if kb else ""
    all_sec, all_perf, all_maint = [], [], []
    all_sec_chunks, all_perf_chunks, all_maint_chunks = [], [], []
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
                executor.submit(
                    _agent_task,
                    agent,
                    chunk,
                    file_path,
                    few_shot,
                    label,
                    project_context,
                    False,
                    None,
                    prefer,
                    use_chunking,
                ): label
                for agent, label in agents
            }
            for future in as_completed(futures):
                label, findings = future.result()
                results[label] = findings
                if stream:
                    console.print(f"  [cyan]{label}[/cyan] 完成 → {len(findings)} 个发现")

        total_elapsed += time.time() - start
        sec_findings = results.get("security", [])
        perf_findings = results.get("performance", [])
        maint_findings = results.get("maintainability", [])
        all_sec.extend(sec_findings)
        all_perf.extend(perf_findings)
        all_maint.extend(maint_findings)
        all_sec_chunks.append(sec_findings)
        all_perf_chunks.append(perf_findings)
        all_maint_chunks.append(maint_findings)
        chunk_offsets.append(offset)

    if use_chunking:
        all_sec = _merge_chunk_findings(all_sec_chunks, chunk_offsets)
        all_perf = _merge_chunk_findings(all_perf_chunks, chunk_offsets)
        all_maint = _merge_chunk_findings(all_maint_chunks, chunk_offsets)

    # ── 三步冲突处理：ID注入 → 分组 → 裁决(仅冲突部分) → 透传(无冲突部分) ──
    raw_findings = all_sec + all_perf + all_maint
    raw_findings = _inject_finding_ids(raw_findings, file_path)
    raw_findings = _quality_filter(raw_findings, file_path)

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


def run_adaptive_review(code: str, file_path: str,
                        kb: KnowledgeBase | None = None,
                        cache: ReviewCache | None = None,
                        project_context: str = "",
                        stream: bool = False,
                        prefer: str | None = None,
                        evidence_path: str | None = None) -> tuple[list, str, float]:
    """Adaptive mode: run a general reviewer, then add routed specialists."""
    plan = build_review_plan(code, file_path)
    cache_key = f"adaptive:general+{','.join(plan.agents) or 'none'}"
    if cache:
        cached = cache.get(code, file_path, cache_key, project_context)
        if cached:
            console.print("  [dim]adaptive cache hit[/dim]")
            return cached, format_plan_summary(plan), 0.0

    static_evidence = collect_static_evidence(evidence_path or file_path)
    static_summary = summarize_static_evidence(static_evidence)

    if plan.fallback_to_single:
        llm_findings, elapsed = run_single_review(
            code, file_path, kb, None, project_context, prefer)
        findings, resolution_summary = _resolve_findings(
            code, file_path, llm_findings + static_evidence.findings)
        debate_summary = format_plan_summary(plan) + "\n" + static_summary
        if resolution_summary:
            debate_summary = debate_summary + "\n" + resolution_summary
    else:
        hints = kb.get_dynamic_prompt_hints(file_path=file_path) if kb else ""
        few_shot = (hints + "\n" + kb.get_few_shot_examples(code, file_path=file_path)) if kb else ""
        start = time.time()
        results = {}
        max_workers = max(1, len(plan.agents) + 1)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    run_single_review,
                    code,
                    file_path,
                    kb,
                    None,
                    project_context,
                    prefer,
                ): "general",
            }
            for label in plan.agents:
                futures[executor.submit(
                    _agent_task,
                    _agent_for_label(label),
                    code,
                    file_path,
                    few_shot,
                    label,
                    project_context,
                    False,
                    None,
                    prefer,
                )] = label
            for future in as_completed(futures):
                label = futures[future]
                if label == "general":
                    findings, _ = future.result()
                else:
                    _, findings = future.result()
                results[label] = findings
                if stream:
                    console.print(f"  [cyan]{label}[/cyan] adaptive done -> {len(findings)} findings")

        elapsed = time.time() - start
        specialist_findings = []
        for label in plan.agents:
            specialist_findings.extend(results.get(label, []))
        findings, resolution_summary = _resolve_adaptive_findings(
            code,
            file_path,
            results.get("general", []),
            specialist_findings,
            static_evidence.findings,
        )
        debate_summary = "General-first adaptive review.\n" + format_plan_summary(plan) + "\n" + static_summary
        if resolution_summary:
            debate_summary = debate_summary + "\n" + resolution_summary

    if cache:
        cache.set(code, file_path, cache_key, findings, project_context)
    return findings, debate_summary, elapsed


def run_review(target: str, mode: str = "multi", output: str | None = None,
               use_cache: bool = True, stream: bool = False,
               prefer: str | None = None,
               interactive_feedback: bool | None = None):
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

    mode_label = {
        "single": "单Agent",
        "multi": "多Agent (3并行)",
        "adaptive": "adaptive (通用 reviewer + 按需专家)",
    }.get(mode, mode)
    console.print(f"[bold]审查: {target}[/bold] → 报告: {output}")
    console.print(f"模式: {mode_label}  |  "
                  f"文件数: {len(files)}  |  {'流式输出' if stream else '静默等待...'}")
    console.print("-" * 50)

    kb = KnowledgeBase()
    cache = ReviewCache() if use_cache else None

    all_reports = []
    for file_index, (file_path, code) in enumerate(files, start=1):
        console.print(f"[{file_index}/{len(files)}] {Path(file_path).name} ", end="", highlight=False)

        if mode == "single":
            findings, elapsed = run_single_review(
                code, file_path, kb, cache, project_context, prefer)
            debate_summary = ""
        elif mode == "adaptive":
            findings, debate_summary, elapsed = run_adaptive_review(
                code, file_path, kb, cache, project_context, stream=stream, prefer=prefer)
        else:
            findings, debate_summary, elapsed = run_multi_review(
                code, file_path, kb, cache, project_context, stream=stream, prefer=prefer)

        # 统一质量过滤：去重 + 去噪
        findings = _quality_filter(findings, file_path)
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
        parts = [f"{c}={sev[c]}" for c in ["critical", "high", "medium", "low", "info"] if c in sev]
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
    indexed = []
    for fp, _, f_list in all_reports:
        for f in f_list:
            indexed.append((fp, f))

    wants_feedback = sys.stdin.isatty() if interactive_feedback is None else interactive_feedback
    can_prompt = total_findings > 0 and wants_feedback and sys.stdin.isatty()
    if can_prompt:
        console.print(
            "\n[bold cyan]反馈引导[/bold cyan] — "
            "[yellow]r5[/yellow]=驳回第5条  "
            "[green]c3[/green]=确认第3条  "
            "[green]done[/green]=其余全部确认并结束  "
            "[dim]skip[/dim]=只提交已标记反馈"
        )
        for i, (fp, f) in enumerate(indexed):
            sv = severity_label(f.severity)
            fid = f.finding_id[:8] if f.finding_id else ""
            console.print(f"  {i+1:2d}. [{sv}] [{fid}] {Path(fp).name}: {f.title[:60]}")
        console.print("[dim]输入操作:[/dim] ", end="")
        sys.stdout.flush()

        rejected = set()
        confirmed = set()
        confirm_remaining = True
        try:
            line = sys.stdin.readline().strip().lower()
            while line:
                if line in ("done", "all", "confirm-all", "confirm_all", "ca"):
                    confirm_remaining = True
                    break
                if line in ("skip", "partial", "marked-only", "marked_only"):
                    confirm_remaining = False
                    break

                matches = re.findall(r"(r|reject|c|confirm)\s*#?\s*(\d+)", line)
                if matches:
                    for action, number in matches:
                        num = int(number) - 1
                        if 0 <= num < len(indexed):
                            if action in ("r", "reject"):
                                rejected.add(num)
                                confirmed.discard(num)
                                console.print(f"  [yellow]已驳回 #{num+1}[/yellow]")
                            elif action in ("c", "confirm"):
                                if num not in rejected:
                                    confirmed.add(num)
                                    console.print(f"  [green]已确认 #{num+1}[/green]")
                                else:
                                    console.print(f"  [yellow]#{num+1} 已驳回，如需确认请重新运行 feedback 命令[/yellow]")
                        else:
                            console.print(f"  [yellow]编号超出范围: {num+1}[/yellow]")
                else:
                    console.print("  [yellow]未识别，请输入 r1 / r 1 / c1 / c 1 / r1 r2 / done / skip[/yellow]")
                console.print("[dim]下一个:[/dim] ", end="")
                sys.stdout.flush()
                line = sys.stdin.readline().strip().lower()
        except (EOFError, KeyboardInterrupt):
            confirm_remaining = False

        if confirm_remaining:
            confirmed.update(i for i in range(len(indexed)) if i not in rejected)
            console.print(f"  [green]已标记确认其余 {len(confirmed)} 条[/green]")

        # 提交反馈：默认采用人工 review 语义，done 表示驳回项之外均确认。
        # 使用 skip 可只提交用户明确标记的条目。
        feedback_items = sorted(rejected | confirmed)
        if feedback_items:
            console.print(f"[dim]正在批量写入 {len(feedback_items)} 条反馈，最后统一刷新知识库规则...[/dim]")
        for offset, i in enumerate(feedback_items, start=1):
            fp, f = indexed[i]
            if i in rejected:
                kb.submit_feedback(fp, f.finding_id, "reject", distill=False)
            elif i in confirmed:
                kb.submit_feedback(fp, f.finding_id, "confirm", distill=False)
            if offset % 20 == 0 or offset == len(feedback_items):
                console.print(f"[dim]反馈写入进度: {offset}/{len(feedback_items)}[/dim]")
        if feedback_items:
            kb.distill_feedback()

        # 追加反馈标记到报告文件
        if feedback_items:
            fb_lines = ["\n\n---\n## 反馈记录\n"]
            for i in feedback_items:
                fp, f = indexed[i]
                tag = "[已驳回]" if i in rejected else "[已确认]"
                fb_lines.append(f"- {tag} {Path(fp).name}: {f.title[:60]}")
            with open(output, "a", encoding="utf-8") as rf:
                rf.write("\n".join(fb_lines))
        if feedback_items:
            unknown_count = len(indexed) - len(feedback_items)
            console.print(f"[dim]已提交 {len(feedback_items)} 条反馈：确认 {len(confirmed)} 条，驳回 {len(rejected)} 条，未处理 {unknown_count} 条[/dim]")
        else:
            console.print("[dim]未提交反馈；所有条目保持 unknown[/dim]")
    elif total_findings > 0 and indexed:
        example_fp, example_f = indexed[0]
        example_id = example_f.finding_id[:8] if example_f.finding_id else example_f.title[:30]
        if interactive_feedback:
            console.print("[yellow]当前运行环境无法读取交互输入，因此没有进入确认/驳回。请在可输入的终端中运行，或使用 feedback 命令提交。[/yellow]")
        elif interactive_feedback is None:
            console.print("[dim]反馈学习：benchmark 不会弹确认/驳回；真实 review 可在这里交互反馈。若想显式开启，可加 --feedback。[/dim]")
        console.print(
            f"[dim]示例: python -m src feedback \"{example_fp}\" --confirm {example_id}  "
            f"或 --reject {example_id}[/dim]"
        )


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
