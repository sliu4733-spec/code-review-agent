"""Scoring router for adaptive code review.

The router is cheap and deterministic. It assigns a score to each specialist
domain, then invokes only the agents whose score crosses the trigger threshold.
Weak signals are kept as context but do not automatically create extra LLM
calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


AGENT_ORDER = ("security", "performance", "maintainability")
TRIGGER_THRESHOLD = 0.55
WEAK_SIGNAL_THRESHOLD = 0.35


@dataclass
class ReviewPlan:
    """Execution plan for adaptive review."""

    agents: list[str]
    reasons: dict[str, list[str]] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    weak_signals: dict[str, list[str]] = field(default_factory=dict)
    complexity_score: int = 0
    fallback_to_single: bool = False

    @property
    def mode_label(self) -> str:
        if self.fallback_to_single:
            return "adaptive(general)"
        return "adaptive(general+" + ",".join(self.agents) + ")"


def build_review_plan(code: str, file_path: str = "") -> ReviewPlan:
    """Score code risk and decide which specialist agents should review it."""

    text = code.lower()
    lines = code.splitlines()
    scores: dict[str, float] = {name: 0.0 for name in AGENT_ORDER}
    reasons: dict[str, list[str]] = {name: [] for name in AGENT_ORDER}

    _score_security(text, scores, reasons)
    _score_performance(text, lines, scores, reasons)
    _score_maintainability(text, lines, scores, reasons)

    complexity_score = _complexity_score(lines)
    if complexity_score >= 3:
        _add(scores, reasons, "maintainability", 0.22, "complex file shape")

    scores = {name: min(1.0, round(value, 2)) for name, value in scores.items()}
    agents = [name for name in AGENT_ORDER if scores[name] >= TRIGGER_THRESHOLD]
    weak_signals = {
        name: reasons[name]
        for name in AGENT_ORDER
        if name not in agents and scores[name] >= WEAK_SIGNAL_THRESHOLD
    }

    if not agents:
        return ReviewPlan(
            agents=[],
            reasons={},
            scores=scores,
            weak_signals=weak_signals,
            complexity_score=complexity_score,
            fallback_to_single=True,
        )

    return ReviewPlan(
        agents=agents,
        reasons={name: reasons[name] for name in agents},
        scores=scores,
        weak_signals=weak_signals,
        complexity_score=complexity_score,
    )


def format_plan_summary(plan: ReviewPlan) -> str:
    """Human-readable plan summary for reports and terminal output."""

    score_text = ", ".join(f"{name}={plan.scores.get(name, 0):.2f}" for name in AGENT_ORDER)
    if plan.fallback_to_single:
        if plan.weak_signals:
            weak = "; ".join(
                f"{name}: {', '.join(reasons[:2])}"
                for name, reasons in plan.weak_signals.items()
            )
            return f"Router selected general-only review. Scores: {score_text}. Weak signals: {weak}"
        return f"Router selected general-only review. Scores: {score_text}."

    parts = []
    for agent in plan.agents:
        reason_text = ", ".join(plan.reasons.get(agent, [])[:3])
        parts.append(f"{agent}={plan.scores.get(agent, 0):.2f} ({reason_text})")
    if plan.weak_signals:
        weak = "; ".join(
            f"{name}={plan.scores.get(name, 0):.2f} ({', '.join(reasons[:2])})"
            for name, reasons in plan.weak_signals.items()
        )
        parts.append(f"weak: {weak}")
    return "Router selected general reviewer plus specialists: " + "; ".join(parts)


def _add(scores: dict[str, float], reasons: dict[str, list[str]],
         agent: str, weight: float, reason: str) -> None:
    scores[agent] += weight
    if reason not in reasons[agent]:
        reasons[agent].append(reason)


def _score_security(text: str, scores: dict[str, float], reasons: dict[str, list[str]]) -> None:
    if any(sql in text for sql in ["select ", "insert ", "update ", "delete "]):
        if any(sink in text for sink in ["execute(", "query(", "raw("]):
            _add(scores, reasons, "security", 0.58, "sql construction with execution sink")
        else:
            _add(scores, reasons, "security", 0.20, "sql construction")
    if any(needle in text for needle in ["os.system", "subprocess", "child_process", "exec("]):
        _add(scores, reasons, "security", 0.56, "shell execution")
    if any(needle in text for needle in ["innerhtml", "dangerouslysetinnerhtml", "document.write", "render_template_string"]):
        _add(scores, reasons, "security", 0.56, "xss sink")
    if any(needle in text for needle in ["pickle.loads", "pickle.load", "yaml.load", "objectinputstream", "eval("]):
        _add(scores, reasons, "security", 0.56, "unsafe deserialization/eval")
    if any(needle in text for needle in ["open(", "readfile", "fileinputstream", "os.open", "ioutil.readfile"]):
        if any(user in text for user in ["request.", "args", "params", "payload", "path", "filename", "output_path", "export_name"]):
            _add(scores, reasons, "security", 0.28, "path/file input")
        else:
            _add(scores, reasons, "security", 0.12, "file access")
    if any(needle in text for needle in ["md5", "sha1", "math.random", "math/rand"]):
        _add(scores, reasons, "security", 0.24, "weak crypto/random")
    secret_hits = len(re.findall(r"\b(api_key|secret|password|token|jwt)\b", text))
    if secret_hits >= 2:
        _add(scores, reasons, "security", 0.26, "secret handling")
    elif secret_hits == 1:
        _add(scores, reasons, "security", 0.12, "possible secret handling")


def _score_performance(text: str, lines: list[str],
                       scores: dict[str, float], reasons: dict[str, list[str]]) -> None:
    loop_with_io = (
        re.search(r"for\s+.+\n(?:.|\n){0,600}?(execute|query|fetch|requests\.|axios\.|await\s)", text)
        or ("for " in text and any(needle in text for needle in ["fetch(", "axios.", "execute(", "query(", "await "]))
    )
    if loop_with_io:
        _add(scores, reasons, "performance", 0.56, "loop with database/api call")
    if _max_loop_depth(lines) >= 2:
        _add(scores, reasons, "performance", 0.28, "nested loop")
    if any(needle in text for needle in ["readlines(", "readall(", "readfilesync", "findall("]):
        _add(scores, reasons, "performance", 0.24, "bulk data loading")
    if text.count("await ") >= 2 and "promise.all" not in text:
        _add(scores, reasons, "performance", 0.32, "serial async calls")
    if any(needle in text for needle in ["cache", "memo", "lru_cache"]):
        _add(scores, reasons, "performance", 0.10, "cache-sensitive code")


def _score_maintainability(text: str, lines: list[str],
                           scores: dict[str, float], reasons: dict[str, list[str]]) -> None:
    if len(lines) >= 120:
        _add(scores, reasons, "maintainability", 0.25, "large file")
    if _longest_block(lines) >= 50:
        _add(scores, reasons, "maintainability", 0.28, "long function or class")
    if _max_branch_depth(lines) >= 3:
        _add(scores, reasons, "maintainability", 0.25, "deep branching")
    weak_type_hits = len(re.findall(r"\b(any|dict|object)\b", text))
    if weak_type_hits >= 6:
        _add(scores, reasons, "maintainability", 0.36, "weak typing / broad objects")
    elif weak_type_hits >= 3:
        _add(scores, reasons, "maintainability", 0.22, "weak typing / broad objects")
    if any(needle in text for needle in ["except:", "except exception", "catch (e)", "catch(e)"]):
        _add(scores, reasons, "maintainability", 0.30, "broad error handling")
    if re.search(r"catch\s*\([^)]*\)\s*\{\s*\}", text):
        _add(scores, reasons, "maintainability", 0.34, "empty catch block")
    if "pass" in text and "except" in text:
        _add(scores, reasons, "maintainability", 0.30, "swallowed exception")


def _complexity_score(lines: list[str]) -> int:
    score = 0
    if len(lines) >= 120:
        score += 1
    if _longest_block(lines) >= 50:
        score += 1
    if _max_loop_depth(lines) >= 2:
        score += 1
    if _max_branch_depth(lines) >= 3:
        score += 1
    return score


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _max_loop_depth(lines: list[str]) -> int:
    stack: list[int] = []
    max_depth = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        indent = _indent_width(line)
        stack = [level for level in stack if level < indent]
        if re.match(r"(for|while)\b", stripped):
            stack.append(indent)
            max_depth = max(max_depth, len(stack))
    return max_depth


def _max_branch_depth(lines: list[str]) -> int:
    stack: list[int] = []
    max_depth = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        indent = _indent_width(line)
        stack = [level for level in stack if level < indent]
        if re.match(r"(if|elif|else|try|except|catch|switch)\b", stripped):
            stack.append(indent)
            max_depth = max(max_depth, len(stack))
    return max_depth


def _longest_block(lines: list[str]) -> int:
    starts = []
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if re.match(r"(def|class|function)\b", stripped) or "=>" in stripped:
            starts.append(idx)

    if not starts:
        return len(lines)

    starts.append(len(lines) + 1)
    return max(starts[i + 1] - starts[i] for i in range(len(starts) - 1))
