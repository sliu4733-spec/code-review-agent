"""多次审查报告的稳定性/共识分析工具。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev

from rich.console import Console
from rich.table import Table

console = Console()

SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
FINDING_RE = re.compile(
    r"^- \*\*(?P<severity>CRIT|HIGH|MED|LOW|INFO)?\*\*"
    r"\s+\|\s+`(?P<line>[^`]*)`\s+\|\s+(?P<category>[^|]+)"
    r"\|\s+(?P<source>[^|]+)\|\s+`?(?P<id>[^|`]*)`?\s+\|"
    r"\s+(?P<title>[^|]+)\|\s*(?P<description>.*)$"
)


@dataclass
class ReportFinding:
    run: int
    file: str
    severity: str
    line_range: str
    category: str
    source: str
    title: str
    description: str
    family: str


@dataclass
class ConsensusItem:
    key: str
    file: str
    category: str
    family: str
    severity: str
    runs: list[int]
    support: int
    support_ratio: float
    status: str
    example_title: str
    example_description: str


def analyze_reports(report_paths: list[str | Path], min_support: int = 2) -> dict:
    paths = [Path(p) for p in report_paths]
    parsed = []
    for index, path in enumerate(paths, start=1):
        parsed.extend(_parse_report(path, index))

    groups: dict[str, list[ReportFinding]] = {}
    for finding in parsed:
        groups.setdefault(_group_key(finding), []).append(finding)

    items = []
    total_runs = len(paths)
    for key, findings in groups.items():
        runs = sorted({f.run for f in findings})
        support = len(runs)
        if support < min_support:
            status = "volatile"
        elif support / total_runs >= 0.6:
            status = "stable"
        else:
            status = "probable"
        strongest = max(findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 0))
        items.append(ConsensusItem(
            key=key,
            file=strongest.file,
            category=strongest.category,
            family=strongest.family,
            severity=strongest.severity,
            runs=runs,
            support=support,
            support_ratio=support / total_runs if total_runs else 0,
            status=status,
            example_title=strongest.title,
            example_description=strongest.description,
        ))

    items.sort(key=lambda item: (
        {"stable": 0, "probable": 1, "volatile": 2}.get(item.status, 3),
        -item.support,
        -SEVERITY_ORDER.get(item.severity, 0),
        item.file,
    ))

    formal_counts = []
    severity_counts = []
    for path in paths:
        findings = _parse_report(path, len(formal_counts) + 1)
        formal_counts.append(len(findings))
        sev_count: dict[str, int] = {}
        for finding in findings:
            sev_count[finding.severity] = sev_count.get(finding.severity, 0) + 1
        severity_counts.append(sev_count)

    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "reports": [str(p) for p in paths],
        "runs": total_runs,
        "formal_count_mean": mean(formal_counts) if formal_counts else 0,
        "formal_count_std": pstdev(formal_counts) if len(formal_counts) > 1 else 0,
        "stable_count": sum(1 for item in items if item.status == "stable"),
        "probable_count": sum(1 for item in items if item.status == "probable"),
        "volatile_count": sum(1 for item in items if item.status == "volatile"),
        "items": [asdict(item) for item in items],
        "severity_counts": severity_counts,
    }


def run_consensus(report_paths: list[str], output: str = "reports/consensus.md",
                  json_output: str | None = None, min_support: int = 2) -> dict:
    result = analyze_reports(report_paths, min_support=min_support)
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(format_consensus_markdown(result), encoding="utf-8")

    if json_output:
        json_path = Path(json_output)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_consensus_summary(result, output_path)
    return result


def format_consensus_markdown(result: dict) -> str:
    lines = [
        "# Review Consensus Report",
        "",
        f"- Runs: `{result['runs']}`",
        f"- Formal finding count avg/std: `{result['formal_count_mean']:.2f}` / `{result['formal_count_std']:.2f}`",
        f"- Stable / Probable / Volatile: `{result['stable_count']}` / `{result['probable_count']}` / `{result['volatile_count']}`",
        f"- Created at: `{result['created_at']}`",
        "",
        "## Stable Findings",
        "",
    ]
    lines.extend(_format_items_table(result["items"], "stable"))
    lines.extend(["", "## Probable Findings", ""])
    lines.extend(_format_items_table(result["items"], "probable"))
    lines.extend(["", "## Volatile Findings", ""])
    lines.extend(_format_items_table(result["items"], "volatile", limit=30))
    lines.extend([
        "",
        "## Interpretation",
        "",
        "- Stable: appears in at least 60% of runs; prioritize these for fixes.",
        "- Probable: appears more than once but below stable threshold; inspect before fixing.",
        "- Volatile: appears once or below the chosen support threshold; treat as low-confidence LLM noise unless static evidence confirms it.",
    ])
    return "\n".join(lines)


def _format_items_table(items: list[dict], status: str, limit: int | None = None) -> list[str]:
    selected = [item for item in items if item["status"] == status]
    if limit:
        selected = selected[:limit]
    if not selected:
        return ["_None_"]
    lines = [
        "| Support | Severity | File | Category | Family | Example |",
        "|---:|---|---|---|---|---|",
    ]
    for item in selected:
        runs = ",".join(map(str, item["runs"]))
        title = str(item["example_title"]).replace("|", "/")
        lines.append(
            f"| {item['support']}/{item['support_ratio']:.0%} ({runs}) | "
            f"{item['severity']} | `{item['file']}` | {item['category']} | "
            f"{item['family']} | {title} |"
        )
    return lines


def _print_consensus_summary(result: dict, output_path: Path) -> None:
    table = Table(title="Review Consensus Summary")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Runs", str(result["runs"]))
    table.add_row("Finding avg", f"{result['formal_count_mean']:.2f}")
    table.add_row("Finding std", f"{result['formal_count_std']:.2f}")
    table.add_row("Stable", str(result["stable_count"]))
    table.add_row("Probable", str(result["probable_count"]))
    table.add_row("Volatile", str(result["volatile_count"]))
    console.print(table)
    console.print(f"[green]Consensus report saved:[/green] {output_path}")


def _parse_report(path: Path, run: int) -> list[ReportFinding]:
    findings = []
    current_file = ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        header = re.match(r"^##\s+(?!跨文件引用检查)(.+)$", line)
        if header:
            value = header.group(1).strip()
            if "/" in value or "\\" in value:
                current_file = value
            continue
        match = FINDING_RE.match(line)
        if not match:
            continue
        severity = _normalize_severity(match.group("severity"))
        title = match.group("title").strip()
        description = match.group("description").strip()
        family = _issue_family(f"{title} {description}")
        findings.append(ReportFinding(
            run=run,
            file=current_file or "unknown",
            severity=severity,
            line_range=match.group("line").strip(),
            category=match.group("category").strip(),
            source=match.group("source").strip(),
            title=title,
            description=description,
            family=family,
        ))
    return findings


def _normalize_severity(value: str | None) -> str:
    return {
        "CRIT": "critical",
        "HIGH": "high",
        "MED": "medium",
        "LOW": "low",
        "INFO": "info",
    }.get(value or "", "low")


def _group_key(finding: ReportFinding) -> str:
    line_bucket = _line_bucket(finding.line_range)
    return "|".join([finding.file, finding.category, finding.family, line_bucket])


def _line_bucket(line_range: str) -> str:
    match = re.search(r"L?(\d+)", line_range or "")
    if not match:
        return "unknown"
    line = int(match.group(1))
    return str((line // 10) * 10)


def _issue_family(text: str) -> str:
    normalized = text.lower()
    checks = {
        "cache-key-collision": ["缓存键", "cache key", "system_prompt[:200]", "user_prompt[:200]"],
        "ground-truth-duplication": ["expected_findings", "ground truth", "预期结果", "重复的expected"],
        "truncation-hallucination": [
            "truncated file", "truncated source", "source is truncated",
            "文件截断", "代码截断", "源文件截断", "incomplete code",
            "incomplete function", "incomplete file", "函数不完整", "文件不完整",
            "代码不完整", "unterminated", "三引号", "docstring",
        ],
        "static-evidence-missing": ["static tools skipped", "file not found"],
        "broad-error-handling": ["bare except", "空的 except", "静默忽略", "异常处理过于宽泛", "broad exception"],
        "sql-injection": ["sql", "注入", "injection"],
        "xss": ["xss", "innerhtml", "跨站"],
        "serial-io": ["sequential", "串行", "promise.all", "n+1"],
        "hardcoded-secret": ["hardcoded", "secret", "api key", "密钥", "token"],
        "long-or-complex": ["职责", "复杂", "过长", "large", "complex"],
    }
    for family, needles in checks.items():
        if any(needle in normalized for needle in needles):
            return family
    words = re.findall(r"[a-zA-Z_]{3,}|[\u4e00-\u9fff]{2,}", normalized)
    return "-".join(words[:4]) if words else "other"
