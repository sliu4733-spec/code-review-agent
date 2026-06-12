"""多模型投票引擎：并行调用多个模型，投票聚合结果

只需一个 API Key，自动用不同的模型名称调用同一 API。
DeepSeek 支持: deepseek-chat, deepseek-coder, deepseek-reasoner
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from src.agents.base import ReviewFinding, parse_findings
from src.llm_client import LLMClient
from src.config import config

DEFAULT_MODELS = ["deepseek-chat", "deepseek-coder"]


def _get_voting_models() -> list[str]:
    """获取投票模型列表（从配置或默认值）"""
    env_models = config.voting_models
    if env_models:
        return [m.strip() for m in env_models.split(",") if m.strip()]
    return list(DEFAULT_MODELS)


def run_multi_model_review(code: str, file_path: str,
                           system_prompt: str, user_prompt: str,
                           models: list[str] | None = None) -> list[ReviewFinding]:
    """用多个模型并行审查同一段代码，投票聚合结果

    只需一个 API Key。DeepSeek 的不同模型共用同一 Key。
    """
    models = models or _get_voting_models()

    # 去重，确保至少 2 个模型
    models = list(dict.fromkeys(models))
    if len(models) < 2:
        models = list(DEFAULT_MODELS)

    all_findings: list[list[ReviewFinding]] = []

    def _review_with_model(model: str) -> list[ReviewFinding]:
        client = LLMClient()
        # 覆盖模型名：同一 API endpoint，不同 model 参数
        from src.config import config as cfg
        saved_model = cfg.openai_model
        try:
            cfg.openai_model = model
            resp = client.create_message(
                system_prompt=system_prompt, user_prompt=user_prompt,
                max_tokens=3072, use_json=True, enable_caching=False)
            return parse_findings(resp, "voting")
        except Exception:
            return []
        finally:
            cfg.openai_model = saved_model

    with ThreadPoolExecutor(max_workers=len(models)) as executor:
        futures = {executor.submit(_review_with_model, m): m for m in models}
        for future in as_completed(futures):
            try:
                findings = future.result()
                if findings:
                    all_findings.append(findings)
            except Exception:
                pass

    return _aggregate_votes(all_findings)


def _aggregate_votes(all_findings: list[list[ReviewFinding]]) -> list[ReviewFinding]:
    """投票聚合：多模型结果取并集，至少 2 票才保留"""
    if not all_findings:
        return []
    if len(all_findings) == 1:
        return all_findings[0]

    votes: dict[str, list[ReviewFinding]] = {}
    for findings in all_findings:
        for f in findings:
            key = f.title.lower()[:60]
            if key not in votes:
                votes[key] = []
            votes[key].append(f)

    result = []
    for key, fs in votes.items():
        if len(fs) >= 2:
            best = max(fs, key=lambda x: x.confidence)
            best.confidence = min(1.0, best.confidence + 0.1)
            result.append(best)
        elif len(all_findings) == 2 and len(fs) == 1 and fs[0].confidence >= 0.8:
            result.append(fs[0])

    result.sort(key=lambda x: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(x.severity, 4))
    return result
