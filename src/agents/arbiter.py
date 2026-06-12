"""仲裁 Agent：分析 Agent 辩论信息，生成综合审查报告"""

from pathlib import Path
from src.llm_client import LLMClient
from src.agents.base import ReviewFinding, parse_findings


class ArbiterAgent:
    """裁决冲突，生成审查报告"""

    def __init__(self):
        self.llm = LLMClient()
        self.name = "arbiter"

    def adjudicate(self, code: str, file_path: str,
                   conflict_groups: list[list]) -> list[dict]:
        """
        输入：冲突组（每组2-3个互相冲突的 findings）
        输出：裁决指令列表 [{action, finding_id, ...}]
        LLM 只做判断，不修改原始数据
        """
        prompt = self._build_adjudication_prompt(code, file_path, conflict_groups)

        response_text = self.llm.create_message(
            system_prompt=self._get_adjudication_system_prompt(),
            user_prompt=prompt,
            max_tokens=1024,
        )

        import json
        try:
            instructions = json.loads(response_text.strip().strip("`").strip("json").strip())
            if isinstance(instructions, dict):
                instructions = instructions.get("instructions", [instructions])
            if not isinstance(instructions, list):
                return []
            # 校验 finding_id 合法性
            valid_ids = {f.finding_id for group in conflict_groups for f in group}
            valid = []
            for inst in instructions:
                fid = inst.get("finding_id", "")
                if fid in valid_ids:
                    valid.append(inst)
                if inst.get("action") == "merge":
                    if inst.get("primary_id", "") in valid_ids and inst.get("supplement_id", "") in valid_ids:
                        valid.append(inst)
            return valid
        except (json.JSONDecodeError, ValueError):
            return []

    def _build_adjudication_prompt(self, code: str, file_path: str,
                                    conflict_groups: list[list]) -> str:
        groups_text = []
        for gi, group in enumerate(conflict_groups):
            groups_text.append(f"\n### 冲突组 {gi+1}")
            for f in group:
                groups_text.append(
                    f"- id={f.finding_id} | [{f.severity}] [{f.category}] {f.title} | {f.line_range}\n"
                    f"  {f.description}"
                )

        return f"""以下代码审查中发现了冲突，请裁决每个冲突组。

## 代码文件: {file_path}

```
{code[:3000]}
```

{"".join(groups_text)}

## 裁决要求

对每个冲突组，输出一个裁决指令。指令类型：
- keep: 保留该finding，丢弃同组其他
- merge: 合并两个finding（指定primary和supplement）
- discard: 丢弃该finding

返回严格的JSON数组，每个元素必须包含 action 和 finding_id：
[{{"action": "keep", "finding_id": "xxx"}},
 {{"action": "merge", "primary_id": "xxx", "supplement_id": "yyy"}},
 {{"action": "discard", "finding_id": "zzz"}}]"""

    def _get_adjudication_system_prompt(self) -> str:
        return """你是资深技术主管。裁决代码审查冲突。

原则：
1. 安全问题 > 性能问题 > 可维护性问题
2. 如果两个finding描述不同代码行，可能不是真正冲突，都保留(keep)
3. 如果两个finding描述同一行但参考不同，merge合并
4. 只输出JSON数组，不要输出其他内容"""

    def arbitrate(self, code: str, file_path: str,
                  debate_context: str) -> tuple[list, str]:
        """兼容旧接口：走完整辩论流程（已不再使用）"""
        return self.adjudicate(code, file_path, []), ""

    def _build_prompt(self, code: str, file_path: str, debate_context: str) -> str:
        return f"""请对以下代码审查结果进行综合裁决。

## 代码文件: {file_path}

```
{code}
```

{debate_context}

请综合分析各方意见，输出最终的审查发现列表。
如果某个发现被多方确认，提高其置信度。
如果存在冲突，按"安全 > 性能 > 可维护性"原则裁决。
请以 JSON 格式输出：{{"findings": [...]}}

如果综合分析后确认没有实质性问题，返回 {{"findings": []}}"""

    def generate_report(self, final_findings: list[ReviewFinding],
                        file_path: str, debate_summary: str,
                        mode: str = "multi") -> str:
        """生成一行一发现的简洁报告，无代码块，PyCharm不报红"""
        now = __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fname = Path(file_path).name
        sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        sorted_f = sorted(final_findings, key=lambda f: sev_order.get(f.severity, 4))

        if not sorted_f:
            return ""

        lines = [f"## {fname}", ""]
        sev_label = {"critical": "CRIT", "high": "HIGH", "medium": "MED", "low": "LOW"}

        for f in sorted_f:
            sv = sev_label.get(f.severity, "INFO")
            cwe = f" [{f.cwe_id}]" if f.cwe_id else ""
            fid = f.finding_id if f.finding_id else ""
            fix = ""
            if f.fix_suggestion and f.severity in ("critical", "high"):
                fix_code = f.fix_suggestion.strip().split("\n")[0]
                if len(fix_code) > 120:
                    fix_code = fix_code[:117] + "..."
                fix = f" | fix: {fix_code}"
            lines.append(f"- **{sv}** | `{f.line_range}` | {f.category} | `{fid}` | {f.title}{cwe} | {f.description}{fix}")

        lines.append("")
        return "\n".join(lines)

    def _get_system_prompt(self) -> str:
        return """你是一位资深技术主管 (Tech Lead)，负责对代码审查结果做最终裁决。

## 你的任务

1. **合并重复发现**: 如果多个 Agent 发现了相同问题，保留置信度更高的描述
2. **裁决冲突**: 当安全建议与性能建议冲突时，做出权衡判断
3. **评估置信度**: 结合多方意见，调整每个发现的置信度
4. **排序优先级**: 按严重程度排序最终问题列表
5. **生成报告**: 提供 Markdown 格式的综合审查报告

## 裁决原则

- 安全问题优先级 > 性能问题 > 可维护性问题
- 如果安全 Agent 和性能 Agent 的建议冲突，优先满足安全要求
- 对于被多个 Agent 确认的问题，大幅提升其置信度
- 对于仅被单个 Agent 发现且其他 Agent 明显忽略的问题，适当降低置信度

## 输出格式

请以 JSON 格式输出最终的问题列表。每个问题需包含上述所有字段。

**CRITICAL**：category 字段必须严格使用以下三个值之一（不能使用中文或其他名称）：
- "security" — 安全问题
- "performance" — 性能问题
- "maintainability" — 可维护性问题

如果综合分析后确认没有实质性问题，返回空的 findings 数组。"""
