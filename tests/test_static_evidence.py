from src.core.static_evidence import collect_static_evidence, summarize_static_evidence


def test_missing_file_is_safe():
    evidence = collect_static_evidence("does_not_exist.py")
    assert evidence.findings == []
    assert "file not found" in evidence.summaries[0]


def test_summary_handles_empty_evidence():
    evidence = collect_static_evidence("does_not_exist.py")
    summary = summarize_static_evidence(evidence)
    assert summary.startswith("Static evidence:")
