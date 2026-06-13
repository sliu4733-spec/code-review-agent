from src.agents.base import ReviewFinding
from src.core.knowledge import (
    SEED_REVIEW_RULES,
    _build_seed_rule_document,
    _detect_patterns,
    _extract_code_snippet,
    _extract_language,
    _extract_project_id,
)


def make_finding(category="security", title="SQL injection"):
    return ReviewFinding(
        category=category,
        severity="high",
        title=title,
        description="query uses string concatenation",
        line_range="L2",
        fix_suggestion="Use parameters",
        confidence=0.9,
    )


def test_extract_code_snippet_uses_line_range():
    code = "a = 1\nquery = 'SELECT * FROM users'\nprint(query)\n"
    snippet = _extract_code_snippet(code, "L2", context=1)
    assert "1: a = 1" in snippet
    assert "2: query" in snippet
    assert "3: print" in snippet


def test_detect_security_pattern():
    code = "cursor.execute('SELECT * FROM users WHERE name=' + name)"
    patterns = _detect_patterns(make_finding(), code, code)
    assert "sql-string-construction" in patterns


def test_detect_performance_pattern():
    finding = make_finding(category="performance", title="N+1 query")
    code = "for user in users:\n    db.query('SELECT * FROM orders')\n"
    patterns = _detect_patterns(finding, code, code)
    assert "loop-database-or-api-call" in patterns


def test_language_and_project_id_are_stable():
    assert _extract_language("src/app.py") == "python"
    assert _extract_language("src/App.tsx") == "typescript"
    assert _extract_project_id("src/app.py")


def test_seed_review_rules_cover_core_categories():
    categories = {rule["category"] for rule in SEED_REVIEW_RULES}
    patterns = {rule["pattern"] for rule in SEED_REVIEW_RULES}

    assert categories == {"security", "performance", "maintainability"}
    assert "sql-string-construction" in patterns
    assert "xss-html-sink" in patterns
    assert "loop-database-or-api-call" in patterns
    assert "broad-empty-error-handling" in patterns


def test_seed_rule_document_contains_detection_and_fix():
    document = _build_seed_rule_document(SEED_REVIEW_RULES[0])

    assert "Detection:" in document
    assert "Fix:" in document
    assert SEED_REVIEW_RULES[0]["pattern"] in document
