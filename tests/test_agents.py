"""Agent 单元测试"""
import pytest
from src.agents.base import ReviewFinding, parse_findings
from src.agents.security import SecurityAgent
from src.agents.performance import PerformanceAgent
from src.agents.maintainability import MaintainabilityAgent


class TestReviewFinding:
    def test_to_dict(self):
        f = ReviewFinding(
            category="security", severity="high",
            title="SQL Injection", description="Found SQL injection",
            line_range="L10-L12", fix_suggestion="Use parameterized query",
            cwe_id="CWE-89", confidence=0.9
        )
        d = f.to_dict()
        assert d["category"] == "security"
        assert d["severity"] == "high"
        assert d["cwe_id"] == "CWE-89"
        assert d["confidence"] == 0.9


class TestParseFindings:
    def test_parse_valid_json(self):
        text = '{"findings": [{"category": "security", "severity": "high", "title": "Test", "description": "desc", "line_range": "L1", "fix_suggestion": "fix", "cwe_id": "", "confidence": 0.8}]}'
        findings = parse_findings(text, "security")
        assert len(findings) == 1
        assert findings[0].title == "Test"

    def test_parse_json_with_markdown_wrapper(self):
        text = '```json\n{"findings": [{"category": "security", "severity": "medium", "title": "XSS", "description": "xss found", "line_range": "L5", "fix_suggestion": "escape", "cwe_id": "CWE-79", "confidence": 0.7}]}\n```'
        findings = parse_findings(text, "security")
        assert len(findings) == 1
        assert findings[0].title == "XSS"

    def test_parse_empty_findings(self):
        text = '{"findings": []}'
        findings = parse_findings(text, "security")
        assert len(findings) == 0

    def test_parse_invalid_json(self):
        findings = parse_findings("not valid json", "security")
        assert len(findings) == 0

    def test_parse_list_format(self):
        text = '[{"category": "performance", "severity": "low", "title": "Slow", "description": "slow", "line_range": "L3", "fix_suggestion": "optimize", "cwe_id": "", "confidence": 0.5}]'
        findings = parse_findings(text, "performance")
        assert len(findings) == 1

    def test_general_agent_missing_category_is_inferred(self):
        text = '{"findings": [{"severity": "high", "title": "SQL injection in query", "description": "user input reaches execute", "line_range": "L10", "fix_suggestion": "Use parameters", "confidence": 0.8}]}'
        findings = parse_findings(text, "general")
        assert findings[0].category == "security"

    def test_chinese_category_is_normalized(self):
        text = '{"findings": [{"category": "性能问题", "severity": "medium", "title": "N+1 query", "description": "query is executed in a loop", "line_range": "L3", "fix_suggestion": "Batch load", "confidence": 0.7}]}'
        findings = parse_findings(text, "general")
        assert findings[0].category == "performance"


class TestAgentInit:
    def test_security_agent_init(self):
        agent = SecurityAgent()
        assert agent.name == "security"
        prompt = agent.get_system_prompt()
        assert "注入" in prompt or "Injection" in prompt

    def test_performance_agent_init(self):
        agent = PerformanceAgent()
        assert agent.name == "performance"
        prompt = agent.get_system_prompt()
        assert "N+1" in prompt or "O(n" in prompt

    def test_maintainability_agent_init(self):
        agent = MaintainabilityAgent()
        assert agent.name == "maintainability"
        prompt = agent.get_system_prompt()
        assert "SOLID" in prompt or "坏味道" in prompt
