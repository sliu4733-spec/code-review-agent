from src.core.consensus import analyze_reports


def test_consensus_groups_repeated_findings(tmp_path):
    report_1 = tmp_path / "r1.md"
    report_2 = tmp_path / "r2.md"
    content = """# 项目审查报告

## code-review-agent/src/app.py
- **HIGH** | `L10-L12` | security | llm | `a` | SQL注入漏洞 | 拼接 SQL
"""
    report_1.write_text(content, encoding="utf-8")
    report_2.write_text(content.replace("`a`", "`b`"), encoding="utf-8")

    result = analyze_reports([report_1, report_2])

    assert result["stable_count"] == 1
    assert result["items"][0]["support"] == 2
    assert result["items"][0]["status"] == "stable"
