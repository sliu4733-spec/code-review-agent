"""基准测试指标计算：recall（检测率）、precision（准确率）、F1"""

from src.agents.base import ReviewFinding


def match_finding(finding: ReviewFinding, expected: dict) -> bool:
    """判断 Agent 发现是否匹配已知问题"""
    title_lower = finding.title.lower()
    description_lower = finding.description.lower()

    # 关键词匹配
    keyword_match = False
    for kw in expected["title_keywords"]:
        if kw.lower() in title_lower or kw.lower() in description_lower:
            keyword_match = True
            break

    if not keyword_match:
        return False

    # 类别匹配
    if finding.category != expected["category"]:
        return False

    # 严重程度检查
    severity_order = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
    min_sev = severity_order.get(expected["min_severity"], 0)
    actual_sev = severity_order.get(finding.severity, 0)
    return actual_sev >= min_sev


def calculate_metrics(findings: list[ReviewFinding],
                      expected_count: int,
                      expected_findings: list[dict]) -> dict:
    """计算 recall、precision、F1、遗漏和误报详情"""
    # 匹配结果
    matched_expected = set()
    matched_finding_indices = set()

    for ei, expected in enumerate(expected_findings):
        for fi, finding in enumerate(findings):
            if fi in matched_finding_indices:
                continue
            if match_finding(finding, expected):
                matched_expected.add(ei)
                matched_finding_indices.add(fi)
                break

    true_positives = len(matched_expected)
    false_negatives = expected_count - true_positives
    false_positives = len(findings) - true_positives

    recall = true_positives / expected_count if expected_count > 0 else 0
    precision = (true_positives / len(findings)
                 if len(findings) > 0 else 0)
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) > 0 else 0)

    # 未匹配的预期问题
    missed = [expected_findings[i] for i in range(len(expected_findings))
              if i not in matched_expected]

    return {
        "recall": recall,
        "precision": precision,
        "f1": f1,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "expected_count": expected_count,
        "found_count": len(findings),
        "missed": missed,
    }
