from src.agents.base import ReviewFinding
from src.benchmarks.metrics import calculate_metrics, score_finding_match


def make_finding(**overrides):
    values = {
        "category": "security",
        "severity": "high",
        "title": "SQL injection in query",
        "description": "User input reaches execute through string interpolation",
        "line_range": "L18-L22",
        "fix_suggestion": "Use parameterized queries",
        "confidence": 0.9,
    }
    values.update(overrides)
    return ReviewFinding(**values)


def test_structured_match_scores_line_and_pattern():
    finding = make_finding()
    expected = {
        "id": "E1",
        "category": "security",
        "issue_type": "sql-injection",
        "line_range": "L18-L22",
        "patterns": ["sql-string-construction"],
        "title_keywords": ["SQL", "injection"],
        "min_severity": "high",
    }
    result = score_finding_match(finding, expected)
    assert result["matched"]
    assert result["score"] >= 60


def test_category_mismatch_does_not_match():
    finding = make_finding(category="performance")
    expected = {
        "category": "security",
        "title_keywords": ["SQL"],
        "min_severity": "high",
    }
    assert not score_finding_match(finding, expected)["matched"]


def test_metrics_returns_false_positive_details():
    findings = [
        make_finding(),
        make_finding(title="Generic style suggestion", category="maintainability", severity="low", line_range="L2"),
    ]
    expected = [{
        "id": "E1",
        "category": "security",
        "issue_type": "sql-injection",
        "line_range": "L18-L22",
        "patterns": ["sql-string-construction"],
        "title_keywords": ["SQL", "injection"],
        "min_severity": "high",
    }]
    metrics = calculate_metrics(findings, 1, expected)
    assert metrics["true_positives"] == 1
    assert metrics["false_positives"] == 1
    assert metrics["false_positive_details"]
