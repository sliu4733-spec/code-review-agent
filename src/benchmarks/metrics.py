"""Benchmark metrics with score-based structured matching."""

from __future__ import annotations

from typing import Any

from src.agents.base import ReviewFinding


SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
MATCH_THRESHOLD = 60


def _text(finding: ReviewFinding) -> str:
    evidence = getattr(finding, "evidence", []) or []
    if isinstance(evidence, str):
        evidence = [evidence]
    return " ".join([
        finding.title or "",
        finding.description or "",
        finding.fix_suggestion or "",
        finding.cwe_id or "",
        " ".join(str(item) for item in evidence),
    ]).lower()


def _parse_range(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    try:
        cleaned = value.replace("L", "").replace(" ", "")
        parts = cleaned.split("-")
        start = int(parts[0])
        end = int(parts[1]) if len(parts) > 1 else start
        if start <= 0 or end <= 0:
            return None
        return min(start, end), max(start, end)
    except (ValueError, IndexError):
        return None


def _ranges_overlap(a: str | None, b: str | None, tolerance: int = 3) -> bool:
    left = _parse_range(a)
    right = _parse_range(b)
    if not left or not right:
        return False
    return max(left[0], right[0]) <= min(left[1], right[1]) + tolerance


def _keyword_hits(text: str, keywords: list[str]) -> int:
    return sum(1 for kw in keywords if kw and kw.lower() in text)


def _infer_patterns(text: str) -> set[str]:
    patterns = set()
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
    for name, needles in checks.items():
        if any(needle in text for needle in needles):
            patterns.add(name)
    return patterns


def score_finding_match(finding: ReviewFinding, expected: dict[str, Any]) -> dict[str, Any]:
    """Return a structured match score and explanation."""
    text = _text(finding)
    score = 0
    reasons: list[str] = []

    if finding.category == expected.get("category"):
        score += 25
        reasons.append("category")
    else:
        return {"matched": False, "score": 0, "reasons": ["category-mismatch"]}

    min_sev = SEVERITY_ORDER.get(expected.get("min_severity", "info"), 0)
    actual_sev = SEVERITY_ORDER.get(finding.severity, 0)
    if actual_sev >= min_sev:
        score += 10
        reasons.append("severity")

    title_keywords = expected.get("title_keywords", [])
    keyword_hits = _keyword_hits(text, title_keywords)
    if keyword_hits:
        score += min(15, 6 + keyword_hits * 3)
        reasons.append(f"keywords:{keyword_hits}")

    desc_keywords = expected.get("description_keywords", [])
    desc_hits = _keyword_hits(text, desc_keywords)
    if desc_hits:
        score += min(10, desc_hits * 4)
        reasons.append(f"description:{desc_hits}")

    expected_patterns = set(expected.get("patterns", []))
    if expected_patterns:
        finding_patterns = set(getattr(finding, "patterns", []) or []) | _infer_patterns(text)
        overlap = expected_patterns & finding_patterns
        if overlap:
            score += 25
            reasons.append("pattern:" + ",".join(sorted(overlap)))

    if expected.get("issue_type"):
        issue_terms = str(expected["issue_type"]).replace("-", " ").split()
        if all(term.lower() in text for term in issue_terms[:2]):
            score += 12
            reasons.append("issue_type")

    if _ranges_overlap(finding.line_range, expected.get("line_range")):
        score += 20
        reasons.append("line")

    for key, label in [("sink", "sink"), ("source", "source")]:
        value = expected.get(key)
        if value and str(value).lower() in text:
            score += 8
            reasons.append(label)

    # Legacy expected records only have keyword/category/severity. Keep them usable.
    if not any(expected.get(key) for key in ["issue_type", "line_range", "patterns", "sink", "source"]):
        threshold = 42
    else:
        threshold = MATCH_THRESHOLD

    return {
        "matched": score >= threshold,
        "score": score,
        "threshold": threshold,
        "reasons": reasons,
    }


def match_finding(finding: ReviewFinding, expected: dict) -> bool:
    return bool(score_finding_match(finding, expected)["matched"])


def calculate_metrics(findings: list[ReviewFinding],
                      expected_count: int,
                      expected_findings: list[dict]) -> dict:
    """Calculate recall, precision, F1, and detailed TP/FP/FN diagnostics."""
    matched_expected: set[int] = set()
    matched_finding_indices: set[int] = set()
    matches = []

    pairs = []
    for ei, expected in enumerate(expected_findings):
        for fi, finding in enumerate(findings):
            if fi in matched_finding_indices:
                continue
            result = score_finding_match(finding, expected)
            if result["matched"]:
                pairs.append((result["score"], ei, fi, result))

    # Greedy high-score matching: each expected and finding can match once.
    for _, ei, fi, result in sorted(pairs, key=lambda item: -item[0]):
        if ei in matched_expected or fi in matched_finding_indices:
            continue
        matched_expected.add(ei)
        matched_finding_indices.add(fi)
        expected = expected_findings[ei]
        finding = findings[fi]
        matches.append({
            "expected_id": expected.get("id", f"expected-{ei + 1}"),
            "expected_title": expected.get("issue_type") or expected.get("title_keywords", [""])[0],
            "finding_title": finding.title,
            "score": result["score"],
            "reasons": result["reasons"],
            "finding_index": fi,
        })

    true_positives = len(matched_expected)
    false_negatives = expected_count - true_positives
    false_positives = len(findings) - true_positives

    recall = true_positives / expected_count if expected_count > 0 else 0
    precision = true_positives / len(findings) if findings else 0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) > 0 else 0)

    missed = [
        {**expected_findings[i], "expected_id": expected_findings[i].get("id", f"expected-{i + 1}")}
        for i in range(len(expected_findings))
        if i not in matched_expected
    ]
    false_positive_details = [
        {
            "finding_index": i,
            "title": finding.title,
            "category": finding.category,
            "severity": finding.severity,
            "line_range": finding.line_range,
        }
        for i, finding in enumerate(findings)
        if i not in matched_finding_indices
    ]

    return {
        "recall": recall,
        "precision": precision,
        "f1": f1,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "expected_count": expected_count,
        "found_count": len(findings),
        "matched": matches,
        "missed": missed,
        "false_positive_details": false_positive_details,
    }
