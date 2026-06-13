"""统一 LLM 客户端：支持 Anthropic 和 OpenAI 兼容 API，含重试和缓存"""

import hashlib
import json
import threading
import time
from src.config import config


class LLMError(Exception):
    """LLM 调用异常"""
    pass


class LLMClient:
    """对 Anthropic/OpenAI 两种 API 的薄封装，含重试 + 缓存 + 连接复用"""

    def __init__(self):
        self._client = None
        self.provider = config.provider
        self.max_retries = config.max_retries
        self._cache = {}  # 简单内存缓存: key → (timestamp, response)
        self._cache_lock = threading.RLock()

    @property
    def client(self):
        """复用 HTTP 连接池：首次创建，后续直接返回已缓存实例"""
        if self._client is None:
            if self.provider in ("openai", "ollama"):
                from openai import OpenAI
                base_url = config.ollama_base_url if self.provider == "ollama" else config.openai_base_url
                api_key = "ollama" if self.provider == "ollama" else config.openai_api_key
                self._client = OpenAI(api_key=api_key, base_url=base_url)
            else:
                from anthropic import Anthropic
                self._client = Anthropic(api_key=config.api_key)
        return self._client

    def create_message(self, system_prompt: str, user_prompt: str,
                       max_tokens: int = 4096, enable_caching: bool = True,
                       use_json: bool = False,
                       model: str | None = None) -> str:
        """统一的 LLM 调用接口，含内存缓存 + 自动重试"""
        cache_key = self._make_cache_key(system_prompt, user_prompt, max_tokens, use_json, model)
        if enable_caching:
            with self._cache_lock:
                cached = self._cache.get(cache_key)
            if cached:
                ts, resp = cached
                if time.time() - ts < 3600:  # 1小时TTL
                    return resp

        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    wait = 2 ** (attempt - 1)
                    time.sleep(wait)  # 在线程池中运行，不会阻塞事件循环
                result = self._do_call(system_prompt, user_prompt,
                                       max_tokens, enable_caching, use_json, model)
                if enable_caching:
                    with self._cache_lock:
                        self._cache[cache_key] = (time.time(), result)
                return result
            except Exception as e:
                last_error = e
                if not self._should_retry(e):
                    break

        raise LLMError(
            f"LLM 调用失败（重试 {self.max_retries} 次后仍失败）: {last_error}"
        )

    def create_message_stream(self, system_prompt: str, user_prompt: str,
                              max_tokens: int = 4096):
        """流式 LLM 调用，逐 token  yield，用于实时展示进度"""
        if self.provider in ("openai", "ollama"):
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_prompt})
            stream = self.client.chat.completions.create(
                model=config.ollama_model if self.provider == "ollama" else config.openai_model,
                max_tokens=max_tokens,
                messages=messages,
                stream=True,
            )
            full_text = ""
            for chunk in stream:
                if chunk.choices[0].delta.content:
                    token = chunk.choices[0].delta.content
                    full_text += token
                    yield token, full_text
        else:
            # Anthropic 流式
            system_kwargs = [{"type": "text", "text": system_prompt}]
            with self.client.messages.stream(
                model=config.model,
                max_tokens=max_tokens,
                system=system_kwargs,
                messages=[{"role": "user", "content": user_prompt}],
            ) as stream:
                full_text = ""
                for event in stream:
                    if event.type == "content_block_delta":
                        token = event.delta.text
                        full_text += token
                        yield token, full_text

    def _do_call(self, system_prompt: str, user_prompt: str,
                 max_tokens: int, enable_caching: bool,
                 use_json: bool, model: str | None = None) -> str:
        if self.provider in ("openai", "ollama"):
            selected_model = model or (config.ollama_model if self.provider == "ollama" else config.openai_model)
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_prompt})
            kwargs = dict(model=selected_model, max_tokens=max_tokens, messages=messages)
            if use_json:
                kwargs["response_format"] = {"type": "json_object"}
            response = self.client.chat.completions.create(**kwargs)
            return response.choices[0].message.content.strip()
        else:
            selected_model = model or config.model
            system_kwargs = [{"type": "text", "text": system_prompt}]
            if enable_caching:
                system_kwargs[0]["cache_control"] = {"type": "ephemeral"}
            response = self.client.messages.create(
                model=selected_model,
                max_tokens=max_tokens,
                system=system_kwargs,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return response.content[0].text.strip()

    def _make_cache_key(self, system_prompt: str, user_prompt: str,
                        max_tokens: int, use_json: bool,
                        model: str | None) -> str:
        payload = {
            "provider": self.provider,
            "model": model or "",
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "max_tokens": max_tokens,
            "use_json": use_json,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _should_retry(self, error: Exception) -> bool:
        """判断错误是否值得重试"""
        msg = str(error).lower()
        # 网络/超时/服务端错误 → 重试
        retry_keywords = [
            "timeout", "connection", "rate_limit", "server error",
            "service unavailable", "too many requests", "internal server",
            "503", "502", "504", "429", "overloaded",
        ]
        for kw in retry_keywords:
            if kw in msg:
                return True
        # 401/403 等鉴权错误 → 不重试
        no_retry_keywords = ["401", "403", "invalid api key", "permission"]
        for kw in no_retry_keywords:
            if kw in msg:
                return False
        # 未知错误 → 保守重试一次
        return True
