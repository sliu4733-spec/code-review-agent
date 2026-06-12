"""辩论引擎：冲突检测 + 多轮 LLM 对话辩论"""

import json
from dataclasses import dataclass
from src.agents.base import ReviewFinding
from src.llm_client import LLMClient

#这份代码的核心:冲突检测->agent进行辩论->Abiter进行裁决->构建markdown(生成完整的辩论记录和综合建议)
@dataclass
class DebateTranscript:#DebateTranscript是一个数据类,作用是把一场代码审查辩论的所有信息,结构化地存起来
    """辩论记录"""
    topic: str #辩论主题
    agent_a: str  # 发起方
    agent_b: str  # 对立方
    finding_a: ReviewFinding #发起方提出的问题
    finding_b: ReviewFinding | None  # 可能为空（unique finding 被质疑）
    round_1: str = ""   # Agent A 的论据
    round_2: str = ""   # Agent B 的反驳
    resolution: str = ""  # Arbiter 的裁决
    resolved: bool = False #表示这场辩论是否已经有了最终解决,默认是False,裁决完成后可以设为true


def detect_conflicts(security_findings: list[ReviewFinding],
                     performance_findings: list[ReviewFinding],
                     maintainability_findings: list[ReviewFinding]) -> dict:
    """初始冲突检测（仍用关键词启发式做第一轮快速筛选）"""
    all_findings = {
        "security": security_findings,
        "performance": performance_findings,
        "maintainability": maintainability_findings,
    } #收集所有模块的检测结果,后续的冲突检测都会基于这个统一的数据集进行

    overlaps = _find_overlaps(all_findings) #找出完全重叠的问题
    potential_conflicts = _find_potential_conflicts(all_findings) #找出潜在冲突的问题

    #收集重叠问题的唯一标识,用集合是为了自动去重,避免同一问题被多次标记
    overlapped_indices = set()
    for f1, f2, a1, a2 in overlaps:
        overlapped_indices.add((a1, id(f1)))
        overlapped_indices.add((a2, id(f2)))

    #生成每个模块独有的问题列表
    unique = {}
    for agent_name, findings in all_findings.items():
        unique[agent_name] = [
            f for f in findings if (agent_name, id(f)) not in overlapped_indices
        ]

    return {
        "overlaps": overlaps,
        "unique": unique,
        "potential_conflicts": potential_conflicts,
    }

#找出不同模块(agent)之间重复的问题
def _find_overlaps(all_findings: dict) -> list[tuple]:
    overlaps = []
    agents = list(all_findings.keys())
    #这里用了双重循环,避免同一模块内的问题对比;
    for i in range(len(agents)):
        for j in range(i + 1, len(agents)):
            for f1 in all_findings[agents[i]]:
                for f2 in all_findings[agents[j]]:
                    if _is_same_issue(f1, f2):
                        overlaps.append((f1, f2, agents[i], agents[j]))
    return overlaps


def _is_same_issue(f1: ReviewFinding, f2: ReviewFinding) -> bool:
    words1 = set(f1.title.lower().split())
    words2 = set(f2.title.lower().split())
    if not words1 or not words2:
        return False
    overlap = len(words1 & words2) / min(len(words1), len(words2))
    return overlap > 0.6
#关于上面两个函数的一些建议,后续再去考虑实现,目前只是先熟悉代码
#仅靠标题判断，准确性有限	可以加入文件路径、行号、规则 ID 等字段共同判断，减少误判
#对停用词（the/of/in 等）敏感	可以先过滤停用词，再计算相似度，提升匹配精度
#四重循环时间复杂度高	当模块和问题数量很多时，复杂度为 O (M²×N²)，可以用哈希表、预索引等方式优化

def _find_potential_conflicts(all_findings: dict) -> list[tuple]:
    """已弃用，保留兼容。使用 _find_all_conflicts"""
    return _find_all_conflicts(all_findings)


def _find_all_conflicts(all_findings: dict) -> list[tuple]:
    """检测所有 Agent 对之间的潜在冲突（3 对组合）"""
    conflicts = []

    # 定义每个 Agent 对的关键词
    pairs = [
        (("security", "performance"), (
            {"加密", "转义", "校验", "过滤", "脱敏", "hash", "验签", "沙箱"},
            {"缓存", "批量", "异步", "并行", "懒加载", "生成器", "预加载", "连接池"},
        )),
        (("security", "maintainability"), (
            {"加密", "校验", "脱敏", "沙箱", "日志审计", "权限检查", "多因素"},
            {"简洁", "易读", "依赖", "抽象", "少依赖", "单一职责", "可测试"},
        )),
        (("performance", "maintainability"), (
            {"缓存", "异步", "连接池", "预加载", "内联", "批量查询", "索引"},
            {"简洁", "单一职责", "可读", "分层", "松耦合", "无状态", "纯函数"},
        )),
    ]

    for (agent_a, agent_b), (keywords_a, keywords_b) in pairs:
        for fa in all_findings.get(agent_a, []):
            for fb in all_findings.get(agent_b, []):
                text_a = fa.title + fa.description
                text_b = fb.title + fb.description
                for ka in keywords_a:
                    if ka in text_a:
                        for kb in keywords_b:
                            if kb in text_b:
                                conflicts.append((
                                    fa, fb,
                                    f"[{agent_a}]建议涉及'{ka}'，可能与[{agent_b}]建议'{kb}'冲突"
                                ))
    return conflicts


def conduct_debate(code: str, file_path: str,
                   conflict_info: dict) -> tuple[list[DebateTranscript], str]:
    #这里输出是所有辩论记录的列表和综合的辩论上下文字符串
    """
    执行多轮 LLM 辩论。

    流程：
    1. 对每个 potential_conflict，两个 Agent 各发言一轮
    2. Arbiter 阅读辩论记录后做出裁决
    3. 返回所有辩论记录和综合辩论上下文
    """
    llm = LLMClient() #后面所有的AI对话都通过它来调用
    transcripts: list[DebateTranscript] = [] #列表里面的每个元素都是辩论记录对象

    # 处理潜在冲突：进行真正的 LLM 辩论
    for f1, f2, reason in conflict_info.get("potential_conflicts", []):
        # 从冲突原因中解析 Agent 名称
        if "[security]" in reason and "[performance]" in reason:
            agent_a, agent_b = "security", "performance"
        elif "[security]" in reason and "[maintainability]" in reason:
            agent_a, agent_b = "security", "maintainability"
        elif "[performance]" in reason and "[maintainability]" in reason:
            agent_a, agent_b = "performance", "maintainability"
        else:
            agent_a, agent_b = "security", "performance"

        transcript = _debate_conflict(
            llm, code, file_path, f1, f2, agent_a, agent_b, reason)
        transcripts.append(transcript)

    # 对 unique findings 中低置信度的发起质疑，根据结果过滤
    rejected_titles = set()
    for agent_name, findings in conflict_info.get("unique", {}).items():
        for f in findings:
            if f.confidence < 0.7:
                transcript = _challenge_finding(
                    llm, code, file_path, f, agent_name)
                transcripts.append(transcript)
                # 解析裁决结果，标记应撤销的发现
                try:
                    resolution = json.loads(transcript.resolution)
                    if not resolution.get("keep", True):
                        rejected_titles.add(f.title)
                except (json.JSONDecodeError, KeyError):
                    pass

    # 从 unique 列表中移除被裁决撤销的发现
    if rejected_titles:
        for agent_name in conflict_info["unique"]:
            conflict_info["unique"][agent_name] = [
                f for f in conflict_info["unique"][agent_name]
                if f.title not in rejected_titles
            ]

    debate_context = _build_enhanced_debate_prompt(conflict_info, transcripts)
    return transcripts, debate_context


def _debate_conflict(client, code, file_path, f1, f2,
                     agent_a, agent_b, reason) -> DebateTranscript:
    """两个 Agent 就一个冲突进行辩论"""

    #用来记录辩论的元数据和对话内容
    transcript = DebateTranscript(
        topic=reason,
        agent_a=agent_a,
        agent_b=agent_b,
        finding_a=f1,
        finding_b=f2,
    )

    system_a = _get_agent_defense_prompt(agent_a)
    system_b = _get_agent_defense_prompt(agent_b)

    # Round 1: Agent A 阐述立场
    prompt_a = f"""你的审查意见中包含了这个问题：
**{f1.title}**
{f1.description}
修复建议：{f1.fix_suggestion}

然而，{agent_b} Agent 提出了以下可能与你冲突的意见：
**{f2.title}**
{f2.description}

冲突点：{reason}

请用 2-3 句话为你的立场辩护，说明为什么你的建议不应因对手的意见而被削弱。
如果对手的意见有道理，你可以部分让步，但需要说明在什么条件下你的建议仍然优先。"""

    transcript.round_1 = client.create_message(
        system_prompt=system_a, user_prompt=prompt_a,
        max_tokens=300, enable_caching=False)

    # Round 2: Agent B 回应
    prompt_b = f"""{agent_a} Agent 对你的审查意见做出了以下回应：

> {transcript.round_1}

请用 2-3 句话回应。你可以：
- 坚持你的立场（如果你的建议确实不应被妥协）
- 接受对方的观点（如果你被说服了）
- 提出折中方案

请直接给出你的结论。"""

    transcript.round_2 = client.create_message(
        system_prompt=system_b, user_prompt=prompt_b,
        max_tokens=300, enable_caching=False)

    # Arbiter 裁决
    arbiter_prompt = f"""## 辩论主题
{reason}

## {agent_a} Agent 发现
**{f1.title}** [{f1.severity}]
{f1.description}
建议: {f1.fix_suggestion}

## {agent_b} Agent 发现
**{f2.title}** [{f2.severity}]
{f2.description}
建议: {f2.fix_suggestion}

## 辩论记录
**{agent_a}**: {transcript.round_1}
**{agent_b}**: {transcript.round_2}

## 请裁决
1. 这两个建议是否真的冲突？
2. 哪个 Agent 的建议应该优先？还是两者可以共存？
3. 给出最终的综合建议（2-3 句话）
4. 回答格式：{{"conflict": true/false, "winner": "security/performance/both", "final_advice": "..."}}"""

    transcript.resolution = client.create_message(
        system_prompt="你是技术主管，负责裁决代码审查分歧。请以 JSON 格式输出裁决。",
        user_prompt=arbiter_prompt, max_tokens=300, enable_caching=False)
    transcript.resolved = True

    return transcript


def _challenge_finding(client, code, file_path, finding,
                       agent_name) -> DebateTranscript:
    """对低置信度的独立发现发起质疑"""
    transcript = DebateTranscript(
        topic=f"质疑 {agent_name} Agent 的独立发现",
        agent_a="arbiter",
        agent_b=agent_name,
        finding_a=finding,
        finding_b=None,
    )

    challenge_prompt = f"""你之前作为 {agent_name} Agent 审查代码时提出了以下发现：

**{finding.title}** [置信度: {finding.confidence:.0%}]
{finding.description}

这个发现没有被其他 Agent 注意到，且你的置信度不高。请重新审视：

1. 这个发现是否可能是误报？
2. 如果是真实问题，为什么其他 Agent 没有发现它？
3. 你坚持这个发现吗？还是应该撤回？

请用 2-3 句话回答。"""

    system = _get_agent_defense_prompt(agent_name)
    transcript.round_2 = client.create_message(
        system_prompt=system, user_prompt=challenge_prompt,
        max_tokens=300, enable_caching=False)

    # Arbiter 裁决是否保留该发现
    arbiter_prompt = f"""## 质疑背景
{agent_name} Agent 提出了一个独立发现（其他 Agent 未发现），置信度为 {finding.confidence:.0%}。

**发现内容**: {finding.title}
**描述**: {finding.description}

## {agent_name} 的自我审查
{transcript.round_2}

## 请判断
这个发现应该保留在最终报告中吗？请以 JSON 格式回答：
{{"keep": true/false, "adjusted_confidence": 0.0-1.0, "reason": "..."}}"""

    transcript.resolution = client.create_message(
        system_prompt="你是技术主管。请以 JSON 格式输出判断。",
        user_prompt=arbiter_prompt, max_tokens=256, enable_caching=False)
    transcript.resolved = True

    return transcript


def _get_agent_defense_prompt(agent_name: str) -> str:
    """获取 Agent 在辩论中的角色 prompt"""
    prompts = {
        "security": "你是一位应用安全专家。在辩论中优先考虑安全性，但也要务实，不过度夸大风险。",
        "performance": "你是一位性能优化专家。在辩论中优先考虑系统性能，但也要承认安全不可妥协。",
        "maintainability": "你是一位代码质量专家。在辩论中优先考虑长期可维护性，但理解工期等现实约束。",
        "arbiter": "你是技术主管，中立公正，对所有团队成员的意见给予公平评估。",
    }
    return prompts.get(agent_name, "你是一位代码审查专家。")


def _build_enhanced_debate_prompt(conflict_info: dict,
                                  transcripts: list[DebateTranscript]) -> str:
    """构建增强版辩论摘要（含多轮辩论记录）"""
    lines = ["# 代码审查辩论摘要（含多轮对话）", ""]

    # 重叠发现
    if conflict_info["overlaps"]:
        lines.append("## 多方确认的问题（高可信度）")
        for f1, f2, a1, a2 in conflict_info["overlaps"]:
            lines.append(f"- [{a1}/{f1.category}] 和 [{a2}/{f2.category}] 都发现了: **{f1.title}**")
        lines.append("")

    # 互补发现
    lines.append("## 各 Agent 独立发现")
    for agent_name, findings in conflict_info["unique"].items():
        if findings:
            lines.append(f"### {agent_name}")
            for f in findings:
                lines.append(f"- [{f.severity}] [{f.category}] **{f.title}**: {f.description}")
    lines.append("")

    # 多轮辩论记录
    if transcripts:
        lines.append("## 多轮辩论记录")
        lines.append("")
        for i, t in enumerate(transcripts, 1):
            lines.append(f"### 辩论 {i}: {t.topic}")
            lines.append(f"**{t.agent_a}** 立场: {t.finding_a.title}")
            if t.finding_b:
                lines.append(f"**{t.agent_b}** 立场: {t.finding_b.title}")
            lines.append(f"**Round 1** ({t.agent_a}): {t.round_1}")
            lines.append(f"**Round 2** ({t.agent_b}): {t.round_2}")
            lines.append(f"**裁决**: {t.resolution}")
            lines.append("---")
            lines.append("")

    return "\n".join(lines)


# 保留旧接口兼容性 保留旧接口是为了不改坏老代码,不强制全系统同步升级,让新旧逻辑并行过渡
def build_debate_prompt(conflict_info: dict) -> str:
    """旧版兼容接口"""
    lines = ["# 代码审查辩论摘要", ""]

    if conflict_info["overlaps"]:
        lines.append("## 多方确认的问题（高可信度）")
        for f1, f2, a1, a2 in conflict_info["overlaps"]:
            lines.append(f"- [{a1}] 和 [{a2}] 都发现了: **{f1.title}** (category: {f1.category})")
            lines.append(f"  描述: {f1.description}")

    lines.append("")
    lines.append("## 各 Agent 独立发现")
    for agent_name, findings in conflict_info["unique"].items():
        if findings:
            lines.append(f"### {agent_name} 独有发现")
            for f in findings:
                lines.append(f"- [{f.severity}] [{f.category}] **{f.title}**: {f.description}")

    if conflict_info["potential_conflicts"]:
        lines.append("")
        lines.append("## 潜在冲突")
        for f1, f2, reason in conflict_info["potential_conflicts"]:
            lines.append(f"- **{f1.title}** vs **{f2.title}**")
            lines.append(f"  冲突原因: {reason}")

    return "\n".join(lines)
