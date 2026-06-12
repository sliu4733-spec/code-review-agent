"""三层知识库：原始记录层 + 规则沉淀层 + 团队画像层，含权重衰减 + 蒸馏机制"""

import json
import hashlib
import math
import re
import time
from pathlib import Path
from chromadb import PersistentClient
from chromadb.utils import embedding_functions
from src.agents.base import ReviewFinding
from src.config import config


def _extract_key_sections(code: str, max_len: int = 8000) -> str:
    if len(code) <= max_len:
        return code
    truncated = code[:max_len]
    last_def = max(
        truncated.rfind("\ndef "), truncated.rfind("\nclass "),
        truncated.rfind("\nasync def "),
    )
    if last_def > max_len // 2:
        truncated = code[:last_def]
    return truncated + "\n# ... (truncated) ...\n" + code[-1000:]


def _extract_function_names(code: str) -> str:
    names = re.findall(r'(?:def|class|async def)\s+(\w+)', code)
    return " ".join(names[:50])


def _extract_project_id(file_path: str) -> str:
    """从文件路径提取项目ID（取倒数第2-3级目录名）"""
    parts = Path(file_path).parts
    if len(parts) >= 3:
        return parts[-3] if parts[-1].endswith(('.py', '.js', '.ts', '.java', '.go')) else parts[-2]
    return "unknown"


def _extract_language(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    return {".py": "python", ".js": "javascript", ".ts": "typescript",
            ".tsx": "typescript", ".java": "java", ".go": "go"}.get(ext, "unknown")


class KnowledgeBase:
    """三层知识库：raw_findings → distilled_rules → project_profiles"""

    # 蒸馏触发阈值
    DISTILL_THRESHOLD = 800       # 原始记录超过此数触发蒸馏
    CATEGORY_DISTILL_MIN = 30     # 单类别+语言组合超过此数触发蒸馏
    WEIGHT_DECAY_RATE = 0.01      # 权重衰减系数（每天）
    WEIGHT_MIN = 0.3              # 低于此权重自动清理
    RULE_CONFIDENCE_THRESHOLD = 0.8  # 规则置信度阈值

    def __init__(self, db_path: str | None = None):
        path = db_path or config.knowledge_db_path
        Path(path).mkdir(parents=True, exist_ok=True)
        self.client = PersistentClient(path=path)
        self.ef = embedding_functions.DefaultEmbeddingFunction()
        self._pp_dir = Path(path).parent / "project_profiles"
        self._pp_dir.mkdir(parents=True, exist_ok=True)
        self._init_collections()

    # ============================================================
    # 初始化
    # ============================================================
    def _init_collections(self):
        for name, desc in [
            ("raw_findings",   "Layer1: 原始审查发现（权重衰减）"),
            ("distilled_rules","Layer2: 蒸馏规则（高置信度）"),
            ("accuracy_tracker","Agent 准确率追踪"),
        ]:
            try:
                setattr(self, name, self.client.get_collection(name=name, embedding_function=self.ef))
            except Exception:
                setattr(self, name, self.client.create_collection(name=name, embedding_function=self.ef, metadata={"description": desc}))

    # ============================================================
    # Layer 1: 原始记录层 (finding级别存储)
    # ============================================================
    def store_review(self, file_path: str, code: str, findings: list[ReviewFinding]):
        """存储审查结果：每个 finding 单独一条记录"""
        if not findings:
            return

        lang = _extract_language(file_path)
        pid = _extract_project_id(file_path)
        now = time.time()

        ids, docs, metas = [], [], []
        for f in findings:
            fid = f.finding_id or hashlib.sha256(
                f"{file_path}:{f.line_range}:{f.category}:{now}".encode()
            ).hexdigest()[:12]
            ids.append(fid + hashlib.sha256(f"{now}:{f.title}".encode()).hexdigest()[:4])
            docs.append(f"{f.title}\n{f.description}\n{f.fix_suggestion}")
            metas.append({
                "finding_id": fid,
                "file_path": file_path,
                "language": lang,
                "category": f.category,
                "severity": f.severity,
                "line_range": f.line_range,
                "title": f.title,
                "weight": 1.0,
                "action": "",          # confirm/reject，初始为空
                "fix_available": bool(f.fix_suggestion),
                "timestamp": now,
                "project_id": pid,
                "parent_id": hashlib.sha256(f"{file_path}:{now}".encode()).hexdigest()[:16],
            })

        self.raw_findings.upsert(ids=ids, documents=docs, metadatas=metas)

        # 更新项目画像
        self._update_project_profile(pid, lang, findings, now)

        # 检查是否需要蒸馏
        count = self.raw_findings.count()
        if count >= self.DISTILL_THRESHOLD:
            self._distill()

    # ============================================================
    # Layer 2: 规则沉淀层 (蒸馏)
    # ============================================================
    def _distill(self):
        """从原始层蒸馏规则：高频确认 → 正向规则，高频驳回 → 负向规则"""
        try:
            results = self.raw_findings.get()
        except Exception:
            return

        if not results["ids"]:
            return

        # 按 language + category 分组
        groups = {}
        for meta in results["metadatas"]:
            key = f"{meta.get('language', 'unknown')}:{meta.get('category', 'unknown')}"
            if key not in groups:
                groups[key] = {"accept": 0, "reject": 0, "total": 0}
            groups[key]["total"] += 1
            action = meta.get("action", "")
            if action == "confirm":
                groups[key]["accept"] += 1
            elif action == "reject":
                groups[key]["reject"] += 1

        # 为每个组合生成蒸馏规则
        now = time.time()
        for key, stats in groups.items():
            if stats["total"] < self.CATEGORY_DISTILL_MIN:
                continue
            lang, cat = key.split(":", 1)
            accept_rate = stats["accept"] / stats["total"] if stats["total"] > 0 else 0
            reject_rate = stats["reject"] / stats["total"] if stats["total"] > 0 else 0

            rule_id = f"distilled_{key.replace(':', '_')}"

            if accept_rate >= self.RULE_CONFIDENCE_THRESHOLD and stats["accept"] > 5:
                # 正向规则
                self.distilled_rules.upsert(
                    ids=[rule_id],
                    documents=[f"正向规则: {lang} {cat}问题确认率高({accept_rate:.0%})，应重点检查"],
                    metadatas=[{
                        "language": lang, "category": cat,
                        "type": "positive", "confidence": accept_rate,
                        "sample_count": stats["accept"],
                        "timestamp": now,
                    }],
                )
            elif reject_rate >= self.RULE_CONFIDENCE_THRESHOLD and stats["reject"] > 5:
                # 负向规则
                self.distilled_rules.upsert(
                    ids=[rule_id],
                    documents=[f"负向规则: {lang} {cat}问题驳回率高({reject_rate:.0%})，应谨慎判断"],
                    metadatas=[{
                        "language": lang, "category": cat,
                        "type": "negative", "confidence": reject_rate,
                        "sample_count": stats["reject"],
                        "timestamp": now,
                    }],
                )

    # ============================================================
    # Layer 3: 团队画像层 (JSON文件)
    # ============================================================
    def _update_project_profile(self, project_id: str, language: str,
                                 findings: list[ReviewFinding], timestamp: float):
        pp_file = self._pp_dir / f"{project_id}.json"
        profile = {}
        if pp_file.exists():
            profile = json.loads(pp_file.read_text(encoding="utf-8"))

        # 语言分布
        langs = profile.setdefault("languages", {})
        langs[language] = langs.get(language, 0) + len(findings)

        # 类别分布
        cats = profile.setdefault("categories", {})
        for f in findings:
            cats[f.category] = cats.get(f.category, 0) + 1

        # 严重度分布
        sevs = profile.setdefault("severities", {})
        for f in findings:
            sevs[f.severity] = sevs.get(f.severity, 0) + 1

        profile["last_updated"] = timestamp
        profile["total_findings"] = profile.get("total_findings", 0) + len(findings)

        pp_file.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_project_profile(self, project_id: str) -> dict:
        pp_file = self._pp_dir / f"{project_id}.json"
        if pp_file.exists():
            return json.loads(pp_file.read_text(encoding="utf-8"))
        return {}

    # ============================================================
    # 权重衰减
    # ============================================================
    def _decay_weight(self, weight: float, timestamp: float) -> float:
        """指数衰减: w' = w * e^(-k * days)"""
        days = max(0, (time.time() - timestamp) / 86400)
        return weight * math.exp(-self.WEIGHT_DECAY_RATE * days)

    def cleanup_raw(self):
        """清理权重低于阈值的原始记录"""
        try:
            results = self.raw_findings.get()
        except Exception:
            return 0

        to_delete = []
        for i, meta in enumerate(results["metadatas"]):
            w = meta.get("weight", 1.0)
            ts = meta.get("timestamp", 0)
            decayed = self._decay_weight(w, ts)
            if decayed < self.WEIGHT_MIN:
                to_delete.append(results["ids"][i])

        if to_delete:
            self.raw_findings.delete(ids=to_delete)
        return len(to_delete)

    # ============================================================
    # 三层混合检索
    # ============================================================
    def get_few_shot_examples(self, code: str, top_k: int = 3) -> str:
        """三层检索：画像 → 规则 → 原始记录（带权重衰减）"""
        lines = []

        # 第一层：项目画像（从代码推断项目ID）
        pid = _extract_project_id("unknown.py")  # 无法推断时不使用
        if pid != "unknown":
            profile = self.get_project_profile(pid)
            if profile:
                cats = profile.get("categories", {})
                lang_dist = profile.get("languages", {})
                if cats or lang_dist:
                    lines.append("## 项目画像")
                    if lang_dist:
                        lines.append(f"语言分布: {lang_dist}")
                    if cats:
                        lines.append(f"问题分布: {cats}")
                    lines.append("请根据项目特征调整审查重点。\n")

        # 第二层：蒸馏规则
        try:
            rules = self.distilled_rules.get()
            if rules["ids"]:
                pos_rules, neg_rules = [], []
                for meta in rules["metadatas"]:
                    if meta.get("type") == "positive":
                        pos_rules.append(
                            f"- [{meta['category']}] 确认率 {meta['confidence']:.0%} (n={meta['sample_count']})")
                    else:
                        neg_rules.append(
                            f"- [{meta['category']}] 驳回率 {meta['confidence']:.0%} (n={meta['sample_count']})")
                if pos_rules:
                    lines.append("## 高置信规则（重点检查）\n" + "\n".join(pos_rules))
                if neg_rules:
                    lines.append("\n## 负向规则（谨慎判断）\n" + "\n".join(neg_rules))
                if pos_rules or neg_rules:
                    lines.append("")
        except Exception:
            pass

        # 第三层：原始记录（带权重衰减检索）
        try:
            total = self.raw_findings.count()
            if total == 0:
                return "\n".join(lines) if lines else ""

            fetch_n = min(top_k * 3, total)
            results = self.raw_findings.query(query_texts=[code[:4000]], n_results=fetch_n)
        except Exception:
            return "\n".join(lines) if lines else ""

        if not results or not results["documents"] or not results["documents"][0]:
            return "\n".join(lines) if lines else ""

        # 按衰减后权重排序
        scored = []
        for i, doc in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][i]
            w = meta.get("weight", 1.0)
            ts = meta.get("timestamp", time.time())
            decayed = self._decay_weight(w, ts)
            if decayed < self.WEIGHT_MIN:
                continue
            action_bonus = 0.2 if meta.get("action") == "confirm" else (-0.2 if meta.get("action") == "reject" else 0)
            final_score = decayed + action_bonus
            scored.append((final_score, meta))

        scored.sort(key=lambda x: -x[0])

        if scored:
            lines.append("## 历史参考案例")
            for score, meta in scored[:top_k]:
                lines.append(
                    f"- [{meta.get('severity','?')}] [{meta.get('category','?')}] "
                    f"{meta.get('title','?')} | 权重={score:.2f}"
                )
            lines.append("")

        return "\n".join(lines) if lines else ""

    def get_dynamic_prompt_hints(self) -> str:
        """分析规则层生成动态提示"""
        hints = ["## 基于反馈的动态审查提示\n"]
        hints.append("**重要**: 只报告代码中确实存在的问题。\n")

        try:
            rules = self.distilled_rules.get()
            if rules["ids"]:
                pos = [m for m in rules["metadatas"] if m.get("type") == "positive"]
                neg = [m for m in rules["metadatas"] if m.get("type") == "negative"]
                if pos:
                    hints.append("**正向规则（请重点检查）**:")
                    for p in pos:
                        hints.append(f"- [{p['category']}] 确认率 {p['confidence']:.0%}")
                if neg:
                    hints.append("\n**负向规则（请谨慎，降低置信度）**:")
                    for n in neg:
                        hints.append(f"- [{n['category']}] 驳回率 {n['confidence']:.0%}")
        except Exception:
            pass

        return "\n".join(hints) + "\n" if len(hints) > 2 else ""

    # ============================================================
    # 反馈（写入原始层 + 更新权重）
    # ============================================================
    def submit_feedback(self, file_path: str, finding_ref: str,
                        action: str, note: str = ""):
        """反馈写入。先搜 raw_findings，再搜旧 code_reviews"""
        file_path = str(Path(file_path))
        target_name = Path(file_path).name

        # 1. 尝试新集合 raw_findings
        any_matched = self._submit_to_collection(
            "raw_findings", target_name, finding_ref, action, note)

        # 2. 如果没匹配到，尝试旧集合 code_reviews
        if not any_matched:
            any_matched = self._submit_feedback_legacy(file_path, finding_ref, action, note)

        return any_matched

    def _submit_to_collection(self, coll_name: str, target_name: str,
                               finding_ref: str, action: str, note: str = "") -> bool:
        """在指定 collection 中匹配并写入反馈"""
        try:
            coll = getattr(self, coll_name, None)
            if coll is None:
                coll = self.client.get_collection(name=coll_name, embedding_function=self.ef)
            results = coll.get()
        except Exception:
            return False

        if not results["ids"]:
            return False

        any_matched = False
        for i, meta in enumerate(results["metadatas"]):
            stored_path = meta.get("file_path", "")
            # 文件名匹配（去掉扩展名也试试）
            stored_name = Path(stored_path).stem if stored_path else ""
            if target_name not in stored_path and target_name != stored_name and finding_ref not in str(meta.get("finding_id", "")):
                continue

            # 匹配：finding_id 精确 OR title 包含
            fid_match = str(meta.get("finding_id", "")) == finding_ref
            title_match = finding_ref.lower() in meta.get("title", "").lower()
            if fid_match or title_match:
                any_matched = True
                meta["action"] = action
                meta["weight"] = 1.0
                meta["feedback_note"] = note
                coll.update(ids=[results["ids"][i]], metadatas=[meta])

        return any_matched

    def _submit_feedback_legacy(self, file_path, finding_ref, action, note=""):
        """兼容旧版 code_reviews collection 的反馈"""
        target_name = Path(file_path).name
        any_matched = False
        try:
            old = self.client.get_collection(name="code_reviews", embedding_function=self.ef)
        except Exception:
            return False
        try:
            results = old.get()
        except Exception:
            return False
        for i, meta in enumerate(results["metadatas"]):
            stored_path = meta.get("file_path", "")
            if target_name not in stored_path:
                continue
            any_matched = True
            existing = meta.get("feedback", "{}")
            feedback = json.loads(existing) if isinstance(existing, str) else existing
            findings = json.loads(meta.get("findings_json", "[]"))
            for f in findings:
                fid = f.get("finding_id", "")
                if fid and fid == finding_ref:
                    feedback[fid] = {"action": action, "title": f.get("title", ""), "timestamp": time.time()}
                    break
            else:
                feedback[finding_ref] = {"action": action, "title": finding_ref, "timestamp": time.time()}
            old.update(ids=[results["ids"][i]],
                       metadatas=[{**meta, "feedback": json.dumps(feedback, ensure_ascii=False)}])
        return any_matched

    # ============================================================
    # 统计
    # ============================================================
    def get_accuracy_stats(self) -> dict:
        try:
            results = self.raw_findings.get()
        except Exception:
            return {"total_reviews": 0, "agent_stats": {}}

        if not results["ids"]:
            return {"total_reviews": 0, "agent_stats": {}}

        agent_stats = {"security": {"confirmed": 0, "rejected": 0},
                       "performance": {"confirmed": 0, "rejected": 0},
                       "maintainability": {"confirmed": 0, "rejected": 0}}

        for meta in results["metadatas"]:
            cat = meta.get("category", "")
            if cat not in agent_stats:
                continue
            action = meta.get("action", "")
            if action == "confirm":
                agent_stats[cat]["confirmed"] += 1
            elif action == "reject":
                agent_stats[cat]["rejected"] += 1

        for cat in agent_stats:
            c, r = agent_stats[cat]["confirmed"], agent_stats[cat]["rejected"]
            agent_stats[cat]["accuracy"] = c / (c + r) if (c + r) > 0 else 0.0

        return {"total_reviews": len(results["ids"]), "agent_stats": agent_stats}

    def cleanup_old_records(self, days: int | None = None):
        """触发权重衰减清理"""
        return self.cleanup_raw()


# ============================================================
# CLI 命令（兼容旧接口）
# ============================================================
def submit_feedback(file_path: str, confirm: str | None = None,
                    reject: str | None = None, note: str = ""):
    from rich.console import Console
    console = Console()
    kb = KnowledgeBase()
    if confirm:
        ok = kb.submit_feedback(file_path, confirm, "confirm", note)
        tag = "已确认" if ok else "未匹配(已确认)"
        console.print(f"[green]{tag}: {confirm}[/green]")
    if reject:
        ok = kb.submit_feedback(file_path, reject, "reject", note)
        tag = "已驳回" if ok else "未匹配(已驳回)"
        console.print(f"[yellow]{tag}: {reject}[/yellow]")


def show_stats():
    from rich.console import Console
    from rich.table import Table
    console = Console()
    kb = KnowledgeBase()
    stats = kb.get_accuracy_stats()
    console.print(f"\n[bold]总记录数: {stats['total_reviews']}[/bold]\n")
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
