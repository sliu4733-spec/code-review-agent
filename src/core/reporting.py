"""Report normalization helpers.

The LLM and static tools may produce mixed English/Chinese findings. This layer
keeps technical identifiers intact while making report titles/descriptions more
consistent for Chinese readers.
"""

from __future__ import annotations

import re


SEVERITY_ALIASES = {
    "critical": "critical",
    "crit": "critical",
    "严重": "critical",
    "high": "high",
    "高危": "high",
    "medium": "medium",
    "med": "medium",
    "中等": "medium",
    "low": "low",
    "建议": "low",
    "info": "info",
    "informational": "info",
}


def normalize_severity(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    return SEVERITY_ALIASES.get(raw, "low")


def severity_label(value: str | None) -> str:
    return {
        "critical": "CRIT",
        "high": "HIGH",
        "medium": "MED",
        "low": "LOW",
        "info": "INFO",
    }.get(normalize_severity(value), "LOW")


def normalize_finding_for_report(finding):
    """Normalize one ReviewFinding in-place and return it."""
    finding.severity = normalize_severity(getattr(finding, "severity", "low"))
    finding.title = _normalize_title(str(getattr(finding, "title", "") or ""))
    finding.description = _normalize_description(
        str(getattr(finding, "description", "") or ""),
        finding.title,
    )
    return finding


def normalize_findings_for_report(findings: list) -> list:
    return [normalize_finding_for_report(finding) for finding in findings]


def _normalize_title(title: str) -> str:
    text = title.strip()
    if not text:
        return text

    ruff = _translate_ruff_title(text)
    if ruff:
        return ruff

    bandit = _translate_bandit_title(text)
    if bandit:
        return bandit

    translated = text
    phrase_map = {
        "Missing type annotations": "缺少类型注解",
        "Weak typing": "弱类型使用",
        "Weak typing due to": "弱类型使用：",
        "Broad catch of Exception": "异常捕获过宽",
        "Broad exception handling": "异常处理过宽",
        "Mixed responsibilities": "职责混杂",
        "Command injection via": "可能导致命令注入：",
        "Duplicate import": "重复导入",
        "Hardcoded import": "硬编码导入",
        "Hardcoded tool names and paths": "硬编码工具名称和路径",
        "Overlarge function": "函数过长",
        "Potential repeated": "可能重复调用",
        "Local variable": "局部变量",
        "Do not use bare": "不要使用裸",
    }
    for english, chinese in phrase_map.items():
        translated = translated.replace(english, chinese)

    translated = re.sub(r"\s+", " ", translated).strip()
    return translated


def _normalize_description(description: str, title: str) -> str:
    text = description.strip()
    if not text:
        return text

    # Static tool descriptions are often duplicated in English. The title is
    # already normalized, so a concise Chinese description reads better.
    if title.startswith("Ruff F401"):
        return "存在未使用的导入，建议删除以减少噪声。"
    if title.startswith("Ruff E722"):
        return "存在裸 except，建议捕获具体异常类型。"
    if title.startswith("Ruff F841"):
        return "局部变量已赋值但未使用，建议删除或补充实际使用逻辑。"
    if title.startswith("Ruff F541"):
        return "f-string 中没有占位符，建议改为普通字符串。"
    if title.startswith("Bandit B110"):
        return "捕获异常后直接忽略，可能隐藏真实错误。"
    if title.startswith("Bandit B404"):
        return "使用 subprocess 模块时需要确认输入来源和执行边界。"
    if title.startswith("Bandit B607"):
        return "启动外部进程时使用了部分路径，建议使用可验证的完整路径。"

    replacements = {
        "lacks a type annotation": "缺少类型注解",
        "missing type annotations": "缺少类型注解",
        "making it harder to maintain and refactor": "会增加后续维护和重构难度",
        "catches all exceptions": "捕获了所有异常",
        "potentially masking bugs": "可能掩盖真实错误",
        "harder to extend and test": "更难扩展和测试",
        "does not validate": "没有进行校验",
        "allowing any arbitrary string": "可能接受任意字符串",
    }
    normalized = text
    for english, chinese in replacements.items():
        normalized = normalized.replace(english, chinese)
        normalized = normalized.replace(english.capitalize(), chinese)
    return normalized


def _translate_ruff_title(title: str) -> str | None:
    match = re.match(r"Ruff\s+([A-Z]\d+):\s*(.*)", title)
    if not match:
        return None
    code, message = match.groups()
    if code == "F401":
        imported = _between_backticks(message) or message.replace(" imported but unused", "")
        return f"Ruff {code}: 未使用的导入 {imported}".strip()
    if code == "E722":
        return "Ruff E722: 不要使用裸 `except`"
    if code == "F841":
        variable = _between_backticks(message) or "局部变量"
        return f"Ruff F841: 局部变量 {variable} 已赋值但未使用"
    if code == "F541":
        return "Ruff F541: f-string 没有占位符"
    return f"Ruff {code}: {message}"


def _translate_bandit_title(title: str) -> str | None:
    match = re.match(r"Bandit\s+(B\d+):\s*(.*)", title)
    if not match:
        return None
    code, message = match.groups()
    mapping = {
        "B110": "捕获异常后直接忽略",
        "B404": "使用 subprocess 模块需确认安全边界",
        "B607": "使用部分路径启动外部进程",
    }
    translated = mapping.get(code)
    if translated:
        return f"Bandit {code}: {translated}"
    return f"Bandit {code}: {message}"


def _between_backticks(text: str) -> str:
    match = re.search(r"`([^`]+)`", text)
    return f"`{match.group(1)}`" if match else ""
