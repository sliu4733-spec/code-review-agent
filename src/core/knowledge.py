"""知识库：ChromaDB 存储历史审查案例，支持自学习"""

import json
import hashlib
import re
import time
from pathlib import Path
from chromadb import PersistentClient
from chromadb.utils import embedding_functions
from src.agents.base import ReviewFinding
from src.config import config


def _extract_key_sections(code: str, max_len: int = 8000) -> str:
    """提取代码的关键部分：保留前 N 字符，同时尽量在函数/类边界处截断"""
    if len(code) <= max_len:
        return code

    # 优先保留前面的代码（通常包含 import、类定义、核心逻辑）
    # 同时在 max_len 附近找一个合理的截断点
    truncated = code[:max_len]
    # 找到最后一个完整函数/类结尾
    last_def = max(
        truncated.rfind("\ndef "), truncated.rfind("\nclass "),
        truncated.rfind("\nasync def "),
    )
    if last_def > max_len // 2:
        truncated = code[:last_def]

    # 附加文件末尾的关键信息（装饰器、配置等）
    ending = code[-1000:]
    return truncated + "\n# ... (truncated) ...\n" + ending


def _extract_function_names(code: str) -> str:
    """提取函数/类名列表作为辅助检索特征"""
    names = re.findall(r'(?:def|class|async def)\s+(\w+)', code)
    return " ".join(names[:50])


class KnowledgeBase:
    """管理审查历史的知识库"""

    def cleanup_old_records(self, max_age_days: int = 30):
        """清理超过 N 天且无反馈的旧记录"""
        import time as _time
        try:
            results = self.reviews.get()
            if not results["ids"]:
                return 0
            cutoff = _time.time() - max_age_days * 86400
            to_delete = []
            for i, meta in enumerate(results["metadatas"]):
                feedback = meta.get("feedback", "{}")
                ts = meta.get("timestamp", 0)
                if ts < cutoff and (feedback == "{}" or feedback == ""):
                    to_delete.append(results["ids"][i])
            if to_delete:
                self.reviews.delete(ids=to_delete)
            return len(to_delete)
        except Exception:
            return 0

    def __init__(self, db_path: str | None = None):
        path = db_path or config.knowledge_db_path
        Path(path).mkdir(parents=True, exist_ok=True)
        self.client = PersistentClient(path=path)
        self.ef = embedding_functions.DefaultEmbeddingFunction()
        self._init_collections()

    def _init_collections(self):
        try:
            self.reviews = self.client.get_collection(
                name="code_reviews",
                embedding_function=self.ef,
            )
        except Exception:
            self.reviews = self.client.create_collection(
                name="code_reviews",
                embedding_function=self.ef,
                metadata={"description": "代码审查历史记录"},
            )
        try:
            self.accuracy = self.client.get_collection(
                name="accuracy_tracker",
                embedding_function=self.ef,
            )
        except Exception:
            self.accuracy = self.client.create_collection(
                name="accuracy_tracker",
                embedding_function=self.ef,
                metadata={"description": "Agent 准确率追踪"},
            )

    def store_review(self, file_path: str, code: str,
                     findings: list[ReviewFinding]):
        """存储一次审查记录（含关键片段提取）"""
        doc_id = hashlib.sha256(
            f"{file_path}:{time.time()}".encode()
        ).hexdigest()[:16]

        key_code = _extract_key_sections(code)
        func_names = _extract_function_names(code)

        metadata = {
            "file_path": file_path,
            "timestamp": time.time(),
            "finding_count": len(findings),
            "findings_json": json.dumps([f.to_dict() for f in findings],
                                        ensure_ascii=False),
            "func_names": func_names,
            "feedback": "{}",
        }

        self.reviews.upsert(
            ids=[doc_id],
            documents=[key_code],
            metadatas=[metadata],
        )

    def get_few_shot_examples(self, code: str, top_k: int = 3) -> str:
        """检索相似案例，反馈质量高（confirmed多）的优先"""
        try:
            total = self.reviews.count()
            fetch_n = min(top_k * 2, total)
            if fetch_n == 0:
                return ""
            results = self.reviews.query(
                query_texts=[code[:4000]],
                n_results=fetch_n,
            )
        except Exception:
            return ""

        if not results or not results["documents"] or not results["documents"][0]:
            return ""

        # 按反馈质量排序：confirmed 多的排前面
        scored = []
        for i, doc in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][i]
            findings = json.loads(meta.get("findings_json", "[]"))
            if not findings:
                continue
            feedback = json.loads(meta.get("feedback", "{}"))
            confirmed = sum(1 for v in feedback.values()
                            if isinstance(v, dict) and v.get("action") == "confirm")
            rejected = sum(1 for v in feedback.values()
                           if isinstance(v, dict) and v.get("action") == "reject")
            score = confirmed - rejected
            scored.append((score, i, meta, findings))

        scored.sort(key=lambda x: x[0], reverse=True)

        examples = []
        for score, idx, meta, findings in scored[:top_k]:
            fb_tag = f" [确认{score}]" if score > 0 else ""
            examples.append(f"### 历史案例（相似代码审查参考）{fb_tag}")
            examples.append(f"文件: {meta.get('file_path', 'unknown')}")
            for f in findings[:3]:
                examples.append(
                    f"- [{f['severity']}] {f['title']}: {f['description'][:100]}"
                )
            examples.append("")

        if not examples:
            return ""

        return (
            "## 参考：以下是过往相似代码的审查发现，请借鉴这些经验\n\n"
            + "\n".join(examples)
            + "\n请运用这些历史经验来审查当前代码。\n"
        )

    def get_dynamic_prompt_hints(self) -> str:
        """分析反馈历史，生成动态 Prompt 调优建议"""
        try:
            results = self.reviews.get()
        except Exception:
            return ""

        if not results.get("ids"):
            return ""

        # 收集所有反馈，分析常见误报和遗漏模式
        confirmed_patterns = {}
        rejected_patterns = {}
        for meta in results["metadatas"]:
            findings = json.loads(meta.get("findings_json", "[]"))
            feedback = json.loads(meta.get("feedback", "{}"))
            for f in findings:
                title = f.get("title", "")
                fb = feedback.get(title, {})
                if fb.get("action") == "confirm":
                    for word in title.lower().split():
                        if len(word) > 2:
                            confirmed_patterns[word] = confirmed_patterns.get(word, 0) + 1
                elif fb.get("action") == "reject":
                    for word in title.lower().split():
                        if len(word) > 2:
                            rejected_patterns[word] = rejected_patterns.get(word, 0) + 1

        if not confirmed_patterns and not rejected_patterns:
            return ""

        hints = [
            "## 基于反馈的动态审查提示\n",
            "**重要**: 只报告代码中确实存在的问题。如果在代码中找不到某类问题的证据，不要报告。\n",
        ]
        if confirmed_patterns:
            top_confirmed = sorted(confirmed_patterns.items(), key=lambda x: -x[1])[:5]
            hints.append("**经常被确认的真实问题类型**（请重点检查）:")
            for word, count in top_confirmed:
                hints.append(f"- 涉及 `{word}` 的问题 (确认 {count} 次)")
        if rejected_patterns:
            top_rejected = sorted(rejected_patterns.items(), key=lambda x: -x[1])[:5]
            hints.append("\n**经常被驳回的误报类型**（请谨慎判断，降低置信度）:")
            for word, count in top_rejected:
                hints.append(f"- 涉及 `{word}` 的警告 (驳回 {count} 次, 可能是误报)")

        return "\n".join(hints) + "\n"

    def deduplicate_by_similarity(self, findings: list, threshold: float = 0.85):
        """用向量相似度对发现列表去重"""
        if len(findings) <= 1:
            return findings
        try:
            texts = [f.title for f in findings]
            ids = [f"dedup_{i}" for i in range(len(texts))]
            temp_coll = self.client.get_or_create_collection(
                name="_dedup_temp", embedding_function=self.ef)
            temp_coll.upsert(ids=ids, documents=texts)

            keep_indices = set(range(len(texts)))
            for i in range(len(texts)):
                if i not in keep_indices:
                    continue
                results = temp_coll.query(query_texts=[texts[i]], n_results=min(5, len(texts)))
                if results and results["distances"] and results["distances"][0]:
                    for j_idx, dist in enumerate(results["distances"][0]):
                        if dist < (1 - threshold) and j_idx > 0:
                            similar_id = results["ids"][0][j_idx]
                            sim_idx = int(similar_id.replace("dedup_", ""))
                            if sim_idx != i and sim_idx in keep_indices:
                                keep_indices.discard(sim_idx)
            temp_coll.delete(ids=ids)
            return [findings[i] for i in sorted(keep_indices)]
        except Exception:
            return findings

    def cleanup_old_records(self, days: int | None = None):
        """删除超过指定天数的旧审查记录"""
        max_age_days = days or config.knowledge_max_age_days
        cutoff = time.time() - max_age_days * 86400
        deleted = 0
        try:
            results = self.reviews.get()
            for i, meta in enumerate(results.get("metadatas", [])):
                if meta.get("timestamp", 0) < cutoff:
                    rid = results["ids"][i]
                    self.reviews.delete(ids=[rid])
                    deleted += 1
        except Exception:
            pass
        return deleted

    def submit_feedback(self, file_path: str, finding_ref: str,
                        action: str, note: str = ""):
        """提交人工反馈：confirm/reject。
        finding_ref 可以是 finding_id（精确匹配）或 title（模糊匹配）"""
        file_path = str(Path(file_path))
        try:
            results = self.reviews.get()
        except Exception:
            return False

        if not results["ids"]:
            return False

        target_name = Path(file_path).name
        any_matched = False
        for i, meta in enumerate(results["metadatas"]):
            stored_path = meta.get("file_path", "")
            if target_name not in stored_path:
                continue
            any_matched = True
            existing = meta.get("feedback", "{}")
            feedback = json.loads(existing) if isinstance(existing, str) else existing

            # 优先按 finding_id 匹配，失败则按 title 模糊匹配
            findings = json.loads(meta.get("findings_json", "[]"))
            matched_finding = None
            for f in findings:
                fid = f.get("finding_id", "")
                if fid and fid == finding_ref:
                    matched_finding = f
                    break
            if not matched_finding:
                for f in findings:
                    if finding_ref.lower() in f.get("title", "").lower():
                        matched_finding = f
                        break

            key = matched_finding.get("finding_id") if matched_finding else finding_ref
            feedback[key] = {
                "action": action,
                "title": matched_finding.get("title", "") if matched_finding else "",
                "note": note,
                "timestamp": time.time(),
            }
            self.reviews.update(
                ids=[results["ids"][i]],
                metadatas=[{**meta, "feedback": json.dumps(feedback, ensure_ascii=False)}],
            )
        return any_matched

    def get_accuracy_stats(self) -> dict:
        """获取各 Agent 准确率统计"""
        try:
            results = self.reviews.get()
        except Exception:
            return {"total_reviews": 0, "agent_stats": {}}

        if not results["ids"]:
            return {"total_reviews": 0, "agent_stats": {}}

        total_reviews = len(results["ids"])
        agent_stats = {"security": {"confirmed": 0, "rejected": 0, "total": 0},
                       "performance": {"confirmed": 0, "rejected": 0, "total": 0},
                       "maintainability": {"confirmed": 0, "rejected": 0, "total": 0}}

        for meta in results["metadatas"]:
            findings = json.loads(meta.get("findings_json", "[]"))
            feedback_raw = meta.get("feedback", "{}")
            feedback = json.loads(feedback_raw) if isinstance(feedback_raw, str) else feedback_raw

            for f in findings:
                cat = f.get("category", "")
                if cat in agent_stats:
                    agent_stats[cat]["total"] += 1
                    fb = feedback.get(f.get("title", ""), {})
                    if fb.get("action") == "confirm":
                        agent_stats[cat]["confirmed"] += 1
                    elif fb.get("action") == "reject":
                        agent_stats[cat]["rejected"] += 1

        for cat in agent_stats:
            total = agent_stats[cat]["total"]
            confirmed = agent_stats[cat]["confirmed"]
            rated = confirmed + agent_stats[cat]["rejected"]
            agent_stats[cat]["accuracy"] = confirmed / rated if rated > 0 else 0.0

        return {"total_reviews": total_reviews, "agent_stats": agent_stats}


def submit_feedback(file_path: str, confirm: str | None = None,
                    reject: str | None = None, note: str = ""):
    from rich.console import Console
    console = Console()
    kb = KnowledgeBase()
    if confirm:
        ok = kb.submit_feedback(file_path, confirm, "confirm", note)
        if ok:
            console.print(f"[green]已确认: {confirm}[/green]")
        else:
            console.print(f"[red]未匹配到: {confirm} (文件不在知识库中)[/red]")
    if reject:
        ok = kb.submit_feedback(file_path, reject, "reject", note)
        if ok:
            console.print(f"[yellow]已驳回: {reject}[/yellow]")
        else:
            console.print(f"[red]未匹配到: {reject} (文件不在知识库中)[/red]")
    if not confirm and not reject:
        console.print("[red]请指定 --confirm 或 --reject[/red]")


def show_stats():
    from rich.console import Console
    from rich.table import Table
    console = Console()
    kb = KnowledgeBase()
    stats = kb.get_accuracy_stats()
    console.print(f"\n[bold]总审查次数: {stats['total_reviews']}[/bold]\n")
    table = Table(title="Agent 准确率统计")
    table.add_column("Agent")
    table.add_column("确认")
    table.add_column("驳回")
    table.add_column("准确率")
    for agent_name, s in stats["agent_stats"].items():
        accuracy = s.get("accuracy", 0)
        color = "green" if accuracy > 0.7 else "yellow" if accuracy > 0.4 else "red"
        table.add_row(agent_name, str(s["confirmed"]), str(s["rejected"]),
                      f"[{color}]{accuracy:.0%}[/{color}]")
    console.print(table)
