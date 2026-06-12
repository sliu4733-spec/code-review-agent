"""审查缓存：基于代码哈希的内存 + 磁盘双层缓存"""

import json
import time
import hashlib
from pathlib import Path
from src.agents.base import ReviewFinding
from src.config import config

CACHE_DIR = Path("./data/review_cache")


class ReviewCache:
    """审查结果缓存，内存 + JSON 文件双层"""

    def __init__(self):
        self._memory: dict[str, tuple[float, list[dict]]] = {}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._load_from_disk()

    def _cache_key(self, code: str, mode: str, context: str = "") -> str:
        raw = f"{code}||{mode}||{context}" if context else f"{code}||{mode}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def get(self, code: str, file_path: str, mode: str,
            context: str = "") -> list[ReviewFinding] | None:
        """查缓存，命中返回 findings 列表，未命中返回 None"""
        key = self._cache_key(code, mode, context)

        # 先查内存
        if key in self._memory:
            ts, data = self._memory[key]
            if time.time() - ts < config.cache_ttl_hours * 3600:
                return [self._dict_to_finding(d) for d in data]
            del self._memory[key]

        # 再查磁盘
        disk_file = CACHE_DIR / f"{key}.json"
        if disk_file.exists():
            try:
                cached = json.loads(disk_file.read_text("utf-8"))
                if time.time() - cached["ts"] < config.cache_ttl_hours * 3600:
                    self._memory[key] = (cached["ts"], cached["findings"])
                    return [self._dict_to_finding(d) for d in cached["findings"]]
                disk_file.unlink()
            except (json.JSONDecodeError, KeyError):
                disk_file.unlink(missing_ok=True)

        return None

    def set(self, code: str, file_path: str, mode: str,
            findings: list[ReviewFinding], context: str = ""):
        """存入缓存"""
        key = self._cache_key(code, mode, context)
        data = [f.to_dict() for f in findings]
        self._memory[key] = (time.time(), data)

        # 写磁盘
        disk_file = CACHE_DIR / f"{key}.json"
        disk_file.write_text(json.dumps({
            "ts": time.time(),
            "findings": data,
        }, ensure_ascii=False), encoding="utf-8")

    def clear(self):
        """清空所有缓存"""
        self._memory.clear()
        for f in CACHE_DIR.glob("*.json"):
            f.unlink()

    def _load_from_disk(self):
        """启动时从磁盘加载缓存到内存"""
        for disk_file in CACHE_DIR.glob("*.json"):
            try:
                cached = json.loads(disk_file.read_text("utf-8"))
                if time.time() - cached["ts"] < config.cache_ttl_hours * 3600:
                    key = disk_file.stem
                    self._memory[key] = (cached["ts"], cached["findings"])
                else:
                    disk_file.unlink()
            except (json.JSONDecodeError, KeyError):
                disk_file.unlink(missing_ok=True)

    @staticmethod
    def _dict_to_finding(d: dict) -> ReviewFinding:
        return ReviewFinding(
            category=d.get("category", ""),
            severity=d.get("severity", "medium"),
            title=d.get("title", ""),
            description=d.get("description", ""),
            line_range=d.get("line_range", "unknown"),
            fix_suggestion=d.get("fix_suggestion", ""),
            cwe_id=d.get("cwe_id", ""),
            confidence=d.get("confidence", 0.0),
        )
