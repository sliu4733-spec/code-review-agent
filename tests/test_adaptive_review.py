from types import SimpleNamespace

from src.agents.base import ReviewFinding
from src.core import reviewer


def make_finding(title: str, category: str, line_range: str = "L3-L4",
                 severity: str = "high", confidence: float = 0.9) -> ReviewFinding:
    return ReviewFinding(
        category=category,
        severity=severity,
        title=title,
        description=title,
        line_range=line_range,
        fix_suggestion="Fix the issue.",
        confidence=confidence,
    )


def test_adaptive_review_runs_general_plus_routed_specialists(monkeypatch):
    calls = []

    def fake_single_review(*args, **kwargs):
        calls.append("general")
        return [make_finding("general sql finding", "security", "L3-L4")], 0.1

    def fake_agent_task(agent, code, file_path, few_shot, label, *args, **kwargs):
        calls.append(label)
        line = "L10-L11" if label == "performance" else "L20-L21"
        return label, [make_finding(f"{label} specialist finding", label, line)]

    monkeypatch.setattr(reviewer, "run_single_review", fake_single_review)
    monkeypatch.setattr(reviewer, "_agent_task", fake_agent_task)
    monkeypatch.setattr(reviewer, "_agent_for_label", lambda label: object())
    monkeypatch.setattr(
        reviewer,
        "collect_static_evidence",
        lambda file_path: SimpleNamespace(findings=[], summaries=[]),
    )
    monkeypatch.setattr(reviewer, "summarize_static_evidence", lambda evidence: "Static evidence: none")

    code = """
async function load(ids) {
  for (const id of ids) {
    document.body.innerHTML = await fetch('/user/' + id)
  }
}
"""
    findings, summary, _ = reviewer.run_adaptive_review(code, "app.tsx", kb=None, cache=None)

    assert "general" in calls
    assert "security" in calls
    assert "performance" in calls
    assert any(f.title == "general sql finding" for f in findings)
    assert any(f.title == "performance specialist finding" for f in findings)
    assert not any(f.title == "security specialist finding" for f in findings)
    assert "General-first adaptive review" in summary


def test_specialist_supplements_do_not_duplicate_general_baseline():
    general = [make_finding("SQL injection in query", "security", "L3-L4")]
    specialist = [
        make_finding("SQL injection from specialist", "security", "L4-L5", confidence=0.99),
        make_finding("N+1 query from specialist", "performance", "L20-L21", confidence=0.9),
    ]

    selected = reviewer._select_specialist_supplements(general, specialist, "app.py")

    assert [f.category for f in selected] == ["performance"]


def test_adaptive_review_uses_evidence_path_for_static_tools(monkeypatch):
    captured = {}

    def fake_static(path):
        captured["path"] = path
        return SimpleNamespace(findings=[], summaries=["static ok"])

    monkeypatch.setattr(reviewer, "collect_static_evidence", fake_static)
    monkeypatch.setattr(reviewer, "summarize_static_evidence", lambda evidence: "Static evidence: ok")
    monkeypatch.setattr(reviewer, "run_single_review", lambda *args, **kwargs: ([], 0.1))

    findings, summary, _ = reviewer.run_adaptive_review(
        "print('ok')",
        "uploaded/app.py",
        evidence_path=r"C:\tmp\app.py",
    )

    assert findings == []
    assert captured["path"] == r"C:\tmp\app.py"
    assert "Static evidence: ok" in summary


def test_quality_filter_drops_truncation_hallucination():
    finding = make_finding(
        "Incomplete code due to truncated file",
        "maintainability",
        severity="high",
        confidence=0.95,
    )

    assert reviewer.quality_filter([finding], "src/app.py") == []


def test_quality_filter_marks_test_fixture_risk():
    finding = make_finding(
        "XSS via innerHTML",
        "security",
        line_range="L8",
        severity="critical",
        confidence=0.95,
    )

    filtered = reviewer.quality_filter([finding], "tests/test_router.py")

    assert filtered
    assert filtered[0].severity == "low"
    assert "测试夹具" in filtered[0].title
