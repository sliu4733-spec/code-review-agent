"""Optional static-analysis evidence collectors.

These integrations are deliberately best-effort. If Bandit, Ruff, or Semgrep
is not installed, review continues normally and the report records that the
tool was skipped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import importlib.util
from dataclasses import dataclass
from pathlib import Path

from src.agents.base import ReviewFinding


@dataclass
class StaticEvidence:
    findings: list[ReviewFinding]
    summaries: list[str]


def collect_static_evidence(file_path: str, timeout: int = 30) -> StaticEvidence:
    path = Path(file_path)
    summaries: list[str] = []
    findings: list[ReviewFinding] = []

    if not path.exists() or not path.is_file():
        return StaticEvidence([], [f"static tools skipped: file not found ({file_path})"])

    if path.suffix == ".py":
        bandit_findings, bandit_summary = _run_bandit(path, timeout)
        ruff_findings, ruff_summary = _run_ruff(path, timeout)
        findings.extend(bandit_findings)
        findings.extend(ruff_findings)
        summaries.extend([bandit_summary, ruff_summary])
    else:
        summaries.append("bandit skipped: Python files only")
        summaries.append("ruff skipped: Python files only")

    semgrep_findings, semgrep_summary = _run_semgrep(path, timeout)
    findings.extend(semgrep_findings)
    summaries.append(semgrep_summary)

    return StaticEvidence(findings, summaries)


def summarize_static_evidence(evidence: StaticEvidence) -> str:
    if not evidence.findings:
        return "Static evidence: " + "; ".join(evidence.summaries)

    by_source: dict[str, int] = {}
    for finding in evidence.findings:
        source = getattr(finding, "source", "static")
        by_source[source] = by_source.get(source, 0) + 1

    counts = ", ".join(f"{source}={count}" for source, count in sorted(by_source.items()))
    return f"Static evidence: {counts}. " + "; ".join(evidence.summaries)


def _run_bandit(path: Path, timeout: int) -> tuple[list[ReviewFinding], str]:
    command = _tool_command("bandit")
    if not command:
        return [], "bandit skipped: command not found"

    result = _run_command([*command, "-q", "-f", "json", str(path)], timeout)
    if result is None:
        return [], "bandit failed: timeout or execution error"

    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return [], "bandit failed: invalid JSON output"

    findings = []
    for item in data.get("results", []):
        severity = _map_bandit_severity(item.get("issue_severity", "LOW"))
        line = item.get("line_number", 1)
        test_id = item.get("test_id", "")
        title = item.get("test_name") or item.get("issue_text", "Bandit finding")
        finding = ReviewFinding(
            category="security",
            severity=severity,
            title=f"Bandit {test_id}: {title}".strip(),
            description=item.get("issue_text", ""),
            line_range=f"L{line}",
            fix_suggestion="Review Bandit finding and apply the recommended secure pattern.",
            cwe_id=_bandit_cwe(item),
            confidence=0.85,
        )
        _attach_source(finding, "bandit", [test_id] if test_id else [])
        findings.append(finding)

    return findings, f"bandit completed: {len(findings)} findings"


def _run_ruff(path: Path, timeout: int) -> tuple[list[ReviewFinding], str]:
    command = _tool_command("ruff")
    if not command:
        return [], "ruff skipped: command not found"

    result = _run_command([*command, "check", "--output-format", "json", str(path)], timeout)
    if result is None:
        return [], "ruff failed: timeout or execution error"

    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return [], "ruff failed: invalid JSON output"

    findings = []
    for item in data:
        code = item.get("code", "RUFF")
        location = item.get("location") or {}
        line = location.get("row", 1)
        finding = ReviewFinding(
            category="maintainability",
            severity="low",
            title=f"Ruff {code}: {item.get('message', 'lint finding')}",
            description=item.get("message", ""),
            line_range=f"L{line}",
            fix_suggestion=_ruff_fix_text(item),
            confidence=0.8,
        )
        _attach_source(finding, "ruff", [code])
        findings.append(finding)

    return findings, f"ruff completed: {len(findings)} findings"


def _run_semgrep(path: Path, timeout: int) -> tuple[list[ReviewFinding], str]:
    config = os.environ.get("SEMGREP_CONFIG")
    if not shutil.which("semgrep"):
        return [], "semgrep skipped: command not found"
    if not config:
        return [], "semgrep skipped: set SEMGREP_CONFIG to a local ruleset"

    result = _run_command(["semgrep", "--quiet", "--json", "--config", config, str(path)], timeout)
    if result is None:
        return [], "semgrep failed: timeout or execution error"

    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return [], "semgrep failed: invalid JSON output"

    findings = []
    for item in data.get("results", []):
        extra = item.get("extra") or {}
        line = (item.get("start") or {}).get("line", 1)
        finding = ReviewFinding(
            category=_semgrep_category(extra),
            severity=_map_semgrep_severity(extra.get("severity", "WARNING")),
            title=f"Semgrep {item.get('check_id', '')}: {extra.get('message', 'rule match')}".strip(),
            description=extra.get("message", ""),
            line_range=f"L{line}",
            fix_suggestion="Review Semgrep rule match and apply the project-approved remediation.",
            confidence=0.82,
        )
        _attach_source(finding, "semgrep", [item.get("check_id", "")])
        findings.append(finding)

    return findings, f"semgrep completed: {len(findings)} findings"


def _run_command(args: list[str], timeout: int) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _tool_command(name: str) -> list[str] | None:
    executable = shutil.which(name)
    if executable:
        return [executable]
    if importlib.util.find_spec(name):
        return [sys.executable, "-m", name]
    return None


def _attach_source(finding: ReviewFinding, source: str, evidence: list[str]) -> None:
    finding.source = source
    finding.evidence = [item for item in evidence if item]


def _map_bandit_severity(value: str) -> str:
    return {
        "HIGH": "high",
        "MEDIUM": "medium",
        "LOW": "low",
    }.get(value.upper(), "low")


def _map_semgrep_severity(value: str) -> str:
    return {
        "ERROR": "high",
        "WARNING": "medium",
        "INFO": "low",
    }.get(value.upper(), "medium")


def _semgrep_category(extra: dict) -> str:
    metadata = extra.get("metadata") or {}
    category = str(metadata.get("category", "")).lower()
    if "security" in category or metadata.get("cwe"):
        return "security"
    if "performance" in category:
        return "performance"
    return "maintainability"


def _bandit_cwe(item: dict) -> str:
    cwe = item.get("issue_cwe")
    if isinstance(cwe, dict):
        cwe_id = cwe.get("id")
        return f"CWE-{cwe_id}" if cwe_id else ""
    return ""


def _ruff_fix_text(item: dict) -> str:
    fix = item.get("fix")
    if isinstance(fix, dict) and fix.get("applicability"):
        return f"Apply Ruff fix when safe ({fix['applicability']})."
    return "Follow Ruff lint guidance or configure the rule if it is intentionally ignored."
