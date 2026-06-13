import json
import hashlib
from abc import ABC, abstractmethod


VALID_CATEGORIES = {"security", "performance", "maintainability"}


def _normalize_category(item: dict, default_category: str) -> str:
    raw = str(item.get("category") or "").strip().lower()
    text = " ".join(str(item.get(k, "")) for k in [
        "title", "description", "fix_suggestion", "cwe_id"
    ]).lower()

    aliases = {
        "sec": "security",
        "security": "security",
        "安全": "security",
        "安全问题": "security",
        "安全漏洞": "security",
        "perf": "performance",
        "performance": "performance",
        "性能": "performance",
        "性能问题": "performance",
        "maintainability": "maintainability",
        "maint": "maintainability",
        "quality": "maintainability",
        "可维护": "maintainability",
        "可维护性": "maintainability",
        "代码质量": "maintainability",
    }
    if raw in aliases:
        return aliases[raw]
    if raw in VALID_CATEGORIES:
        return raw
    if default_category in VALID_CATEGORIES:
        return default_category

    # General-agent fallback: infer a valid benchmark category from the finding text.
    if any(needle in text for needle in [
        "sql", "injection", "xss", "csrf", "ssrf", "command", "shell",
        "path traversal", "deserialize", "deserialization", "pickle",
        "yaml.load", "secret", "password", "token", "cwe", "md5", "sha1",
    ]):
        return "security"
    if any(needle in text for needle in [
        "n+1", "performance", "slow", "loop", "o(n", "readlines",
        "memory", "await", "promise.all", "query in loop", "bulk",
    ]):
        return "performance"
    return "maintainability"


class ReviewFinding: #这个类的作用就是获取LLM返回的文本->提取/解析成合法JSON->程序能用的结构化对象
    """单个审查发现"""

    def __init__(self, category: str, severity: str, title: str,
                 description: str, line_range: str, fix_suggestion: str,
                 cwe_id: str = "", confidence: float = 0.0): #创建这个类的实例的时候会自动调用
        #下面这段代码的作用:把传入的参数值绑定到当前对象self的属性上,
        # 执行完这段代码这个对象就有了这些属性，后续就可以直接访问这些属性
        self.category = category #漏洞/问题的类别 例如“安全漏洞","代码规范","性能问题"
        self.severity = severity  # 严重程度 critical / high / medium / low / info
        self.title = title #漏洞的简短标题
        self.description = description #漏洞的详细描述
        self.line_range = line_range #漏洞在代码中的位置
        self.fix_suggestion = fix_suggestion #修复建议
        self.cwe_id = cwe_id #对应的CWE漏洞编号
        self.confidence = confidence #检测置信度
        self.finding_id = ""  # 稳定ID，由 set_id() 注入

    def to_dict(self) -> dict:
        return {
            "finding_id": self.finding_id,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "description": self.description,
            "line_range": self.line_range,
            "fix_suggestion": self.fix_suggestion,
            "cwe_id": self.cwe_id,
            "confidence": self.confidence,
        }

    def set_id(self, file_path: str):
        """基于 file_path + line_range + category 生成稳定 ID"""
        raw = f"{file_path}:{self.line_range}:{self.category}".encode()
        self.finding_id = hashlib.sha256(raw).hexdigest()[:12]
        return self.finding_id


def parse_findings(response_text: str, default_category: str) -> list[ReviewFinding]:
    """解析 LLM 响应中的 JSON 审查结果
    Args:
        response_text: LLM返回的原始文本
        default_category: 解析中没有指定类别时的默认值
    Returns:
        ReviewFinding对象列表
    """
    findings = []
    # 移除 markdown 代码块标记
    cleaned = response_text.strip()#把LLM响应前后的多余空白清理掉
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # 去掉开头 ```json 和结尾 ```
        if len(lines) > 2:
            cleaned = "\n".join(lines[1:-1])
        cleaned = cleaned.strip()

    try:
        # 尝试直接解析完整 JSON
        data = json.loads(cleaned)#把清洗后的字符串解析成Python对象
        items = data if isinstance(data, list) else data.get("findings", [])
        if items:
            for item in items:
                cat = _normalize_category(item, default_category)
                findings.append(ReviewFinding(
                    category=cat,
                    severity=item.get("severity", "medium"),
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    line_range=item.get("line_range", "unknown"),
                    fix_suggestion=item.get("fix_suggestion", ""),
                    cwe_id=item.get("cwe_id", ""),
                    confidence=item.get("confidence", 0.0),
                ))
        return findings
    except json.JSONDecodeError:
        pass

    try:
        start = cleaned.find("[")
        if start == -1:
            start = cleaned.find("{")
        end = cleaned.rfind("]")
        if end == -1 or cleaned.rfind("}") > end:
            end = cleaned.rfind("}")
        if start != -1 and end != -1:
            data = json.loads(cleaned[start:end + 1])
            items = data if isinstance(data, list) else data.get("findings", [])
            for item in items:
                cat = _normalize_category(item, default_category)
                findings.append(ReviewFinding(
                    category=cat,
                    severity=item.get("severity", "medium"),
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    line_range=item.get("line_range", "unknown"),
                    fix_suggestion=item.get("fix_suggestion", ""),
                    cwe_id=item.get("cwe_id", ""),
                    confidence=item.get("confidence", 0.0),
                ))
    except (json.JSONDecodeError, KeyError):
        pass
    return findings


class BaseAgent(ABC):
    """Agent 基类"""

    def __init__(self, name: str, role_prompt: str):
        self.name = name
        self.role_prompt = role_prompt
        from src.llm_client import LLMClient
        self.llm = LLMClient()

    def _load_prompt_from_file(self) -> str | None:
        """尝试从 prompts/{name}.md 加载模板化提示词"""
        from pathlib import Path
        prompt_file = Path(__file__).parent.parent.parent / "prompts" / f"{self.name}.md"
        if prompt_file.exists():
            return prompt_file.read_text(encoding="utf-8")
        return None

    @abstractmethod
    def get_system_prompt(self) -> str:
        ...

    def analyze(self, code: str, file_path: str = "unknown",
                few_shot_examples: str = "", prefer: str | None = None) -> list[ReviewFinding]:
        system_prompt = self._load_prompt_from_file() or self.get_system_prompt()
        # 注入用户自定义规则
        from src.core.rules_config import build_rules_prompt
        rules_prompt = build_rules_prompt(self.name)
        if rules_prompt:
            system_prompt = system_prompt + "\n\n" + rules_prompt
        # 注入用户自然语言偏好
        if prefer:
            system_prompt = system_prompt + f"\n\n## 用户审查偏好\n{prefer}\n请严格按照上述偏好调整审查重点。"
        user_prompt = self._build_user_prompt(code, file_path, few_shot_examples)

        response_text = self.llm.create_message(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=1500,  # 降低以加速响应，审查结论通常 500-1000 tokens
            use_json=True,
        ) #调用LLM向大模型发送请求,传入提示词和最大token数
        return parse_findings(response_text, self.name)

    def analyze_stream(self, code: str, file_path: str = "unknown",
                       few_shot_examples: str = "",
                       on_token=None, prefer: str | None = None) -> list[ReviewFinding]:
        """流式审查：逐 token 回调 on_token(token)，同时返回最终结果"""
        system_prompt = self.get_system_prompt()
        if prefer:
            system_prompt = system_prompt + f"\n\n## 用户审查偏好\n{prefer}\n请严格按照上述偏好调整审查重点。"
        user_prompt = self._build_user_prompt(code, file_path, few_shot_examples)

        full_text = ""
        for token, accumulated in self.llm.create_message_stream(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=3072,
        ):
            full_text = accumulated
            if on_token:
                on_token(token)
        return parse_findings(full_text, self.name)

    def _build_user_prompt(self, code: str, file_path: str,
                           few_shot_examples: str) -> str:
        parts = [
            f"请审查以下代码文件：{file_path}",
            "",
            "```",
            code,
            "```",
        ]
        if few_shot_examples:
            parts.insert(0, few_shot_examples)

        parts.append(
            "请用JSON输出审查结果。\n"
            "要求：\n"
            "1. description: 用1句话说明问题\n"
            "2. fix_suggestion: 必须写出完整修复代码，不能为空\n"
            "3. category 只能且必须是 \"security\" / \"performance\" / \"maintainability\" 之一，禁止使用其他值\n"
            "4. 如果输入说明当前代码是片段、分块或项目上下文摘要，不要报告“文件截断、docstring未闭合、函数不完整、代码不完整”等仅由上下文边界造成的问题\n"
            "5. 示例: {\"fix_suggestion\": \"# 修复: 使用参数化查询\\ncursor.execute('SELECT * FROM users WHERE name=?', (username,))\"}\n"
            "格式: "
            '{"findings": [{"category": "仅限security/performance/maintainability", "severity": "critical|high|medium|low|info", '
            '"title": "简短标题", "description": "1句话描述", "line_range": "L1-L3", '
            '"fix_suggestion": "修复代码", "cwe_id": "CWE-xxx或空", "confidence": 0.0-1.0}]}'
            "\n如果没问题返回 {\"findings\": []}"
        )
        return "\n".join(parts)
    #这样的设计实现了开闭原则 有Agent的基类相当于给所有的Agent定好了框架
    # 之后每个具体的Agent只需要继承这个基类Agent然后专注于自己的实现逻辑即可
    #这个框架就是 子类继承BaseAgent->传入待审查代码,文件路径->构建提示词->调用LLM->解析结果->返回结果
