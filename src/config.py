import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # LLM Provider: "anthropic" / "openai" / "ollama"
    provider: str = field(default_factory=lambda: os.getenv("PROVIDER", "anthropic"))

    # Ollama 配置
    ollama_base_url: str = field(default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"))
    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "qwen2.5:7b"))

    # Anthropic 配置
    api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    model: str = field(default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"))

    # OpenAI 兼容配置（DeepSeek / OpenAI / 其他）
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_base_url: str = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o"))

    # 多模型投票配置（逗号分隔，用同一 API Key）
    voting_models: str = field(default_factory=lambda: os.getenv("VOTING_MODELS", ""))

    # LLM 调用配置
    max_retries: int = 3  # LLM 调用失败最大重试次数

    # 缓存配置
    cache_ttl_hours: int = 24  # 审查缓存有效期（小时）

    # 知识库路径
    knowledge_db_path: str = "./data/knowledge_db"
    knowledge_max_age_days: int = 30  # 知识库记录最大保留天数

    @property
    def is_configured(self) -> bool:
        if self.provider == "ollama":
            return True  # Ollama 本地运行，无需 API Key
        if self.provider == "openai":
            return bool(self.openai_api_key)
        return bool(self.api_key)


config = Config()
