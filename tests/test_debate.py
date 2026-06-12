"""辩论引擎单元测试"""
import pytest
from src.agents.base import ReviewFinding
from src.core.debate import detect_conflicts, _is_same_issue, build_debate_prompt


def make_finding(title: str, description: str = "test",
                 category: str = "security",
                 severity: str = "medium") -> ReviewFinding:
    return ReviewFinding(
        category=category, severity=severity,
        title=title, description=description,
        line_range="L1", fix_suggestion="fix",
        confidence=0.8
    )


class TestIsSameIssue:
    def test_same_title(self):
        f1 = make_finding("SQL injection vulnerability")
        f2 = make_finding("SQL injection found in code")
        assert _is_same_issue(f1, f2)

    def test_different_titles(self):
        f1 = make_finding("SQL injection")
        f2 = make_finding("N+1 query problem")
        assert not _is_same_issue(f1, f2)

    def test_similar_titles(self):
        f1 = make_finding("Missing input validation for user data")
        f2 = make_finding("Input validation missing")
        assert _is_same_issue(f1, f2)


class TestDetectConflicts:
    def test_overlap_detection(self):
        sec = [make_finding("SQL injection in query", category="security")]
        perf = []
        maint = [make_finding("SQL injection vulnerability", category="maintainability")]
        result = detect_conflicts(sec, perf, maint)
        assert len(result["overlaps"]) >= 1

    def test_unique_findings(self):
        sec = [make_finding("Security issue A")]
        perf = [make_finding("Performance issue B")]
        maint = [make_finding("Maintainability issue C")]
        result = detect_conflicts(sec, perf, maint)
        assert len(result["unique"]["security"]) == 1
        assert len(result["unique"]["performance"]) == 1
        assert len(result["unique"]["maintainability"]) == 1

    def test_empty_all(self):
        result = detect_conflicts([], [], [])
        assert result["overlaps"] == []
        assert result["potential_conflicts"] == []


class TestBuildDebatePrompt:
    def test_build_with_overlaps(self):
        f1 = make_finding("SQL Injection", category="security")
        f2 = make_finding("SQL Injection", category="maintainability")
        info = {
            "overlaps": [(f1, f2, "security", "maintainability")],
            "unique": {"security": [], "performance": [], "maintainability": []},
            "potential_conflicts": [],
        }
        prompt = build_debate_prompt(info)
        assert "多方确认" in prompt
        assert "SQL Injection" in prompt
