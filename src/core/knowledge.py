"""Review knowledge base.

The knowledge base is a memory layer for code review:
- raw findings: every LLM/static-tool finding with source, evidence, snippet,
  feedback, and routing patterns.
- distilled rules: feedback-backed or evidence-backed patterns used as prompt
  hints.
- project profiles: lightweight JSON summaries of project risk distribution.

ChromaDB is imported lazily so utility helpers can be tested without loading
the full runtime stack.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.agents.base import ReviewFinding


VALID_CATEGORIES = {"security", "performance", "maintainability"}
SEVERITY_WEIGHT = {"critical": 0.35, "high": 0.25, "medium": 0.12, "low": 0.04, "info": 0.0}
SOURCE_WEIGHT = {"semgrep": 0.18, "bandit": 0.16, "ruff": 0.10, "llm": 0.0}


SEED_REVIEW_RULES: list[dict[str, Any]] = [
    {
        "category": "security",
        "pattern": "sql-string-construction",
        "severity": "high",
        "title": "SQL string construction reaching query execution",
        "detection": "Look for user-controlled values interpolated into SQL strings passed to execute/query/raw APIs.",
        "fix": "Use parameterized queries or ORM query builders.",
        "sinks": ["execute", "query", "raw"],
        "sources": ["request", "args", "params", "payload"],
    },
    {
        "category": "security",
        "pattern": "xss-html-sink",
        "severity": "high",
        "title": "Untrusted HTML rendered through an unsafe sink",
        "detection": "Look for unescaped input passed to innerHTML, dangerouslySetInnerHTML, document.write, or render_template_string.",
        "fix": "Escape or sanitize untrusted HTML and prefer safe templating APIs.",
        "sinks": ["innerHTML", "dangerouslySetInnerHTML", "render_template_string"],
        "sources": ["request", "props", "state", "user input"],
    },
    {
        "category": "security",
        "pattern": "shell-command-execution",
        "severity": "high",
        "title": "User input reaches shell command execution",
        "detection": "Look for os.system, subprocess shell mode, child_process, exec, or eval with external input.",
        "fix": "Avoid shell=True, pass arguments as arrays, validate allowlisted commands, or remove command execution.",
        "sinks": ["os.system", "subprocess", "child_process", "exec"],
        "sources": ["request", "payload", "filename", "command"],
    },
    {
        "category": "security",
        "pattern": "path-file-access",
        "severity": "medium",
        "title": "User-controlled path reaches file access",
        "detection": "Look for user-controlled path or filename values passed to open/read/write APIs.",
        "fix": "Normalize paths, enforce a safe base directory, and reject traversal sequences.",
        "sinks": ["open", "readFile", "writeFile"],
        "sources": ["path", "filename", "output_path", "export_name"],
    },
    {
        "category": "security",
        "pattern": "unsafe-deserialization",
        "severity": "high",
        "title": "Unsafe deserialization of untrusted data",
        "detection": "Look for pickle, yaml.load, ObjectInputStream, or eval-style APIs on untrusted input.",
        "fix": "Use safe serializers, signed payloads, safe loaders, or strict schemas.",
        "sinks": ["pickle.loads", "yaml.load", "ObjectInputStream"],
        "sources": ["payload", "request", "file upload"],
    },
    {
        "category": "security",
        "pattern": "hardcoded-secret",
        "severity": "high",
        "title": "Hardcoded credential or secret",
        "detection": "Look for API keys, passwords, tokens, JWT secrets, or credentials in source code.",
        "fix": "Move secrets to a secret manager or environment variables and rotate exposed values.",
        "sinks": ["API_KEY", "SECRET", "PASSWORD", "TOKEN"],
        "sources": ["literal"],
    },
    {
        "category": "security",
        "pattern": "weak-crypto-random",
        "severity": "medium",
        "title": "Weak crypto or random source for security-sensitive data",
        "detection": "Look for MD5/SHA1 for security or Math.random/random/math/rand for tokens.",
        "fix": "Use modern password hashing, SHA-256/HMAC where appropriate, and cryptographic randomness.",
        "sinks": ["md5", "sha1", "Math.random", "random"],
        "sources": ["token", "password", "signature"],
    },
    {
        "category": "performance",
        "pattern": "loop-database-or-api-call",
        "severity": "high",
        "title": "Loop performs database or API calls",
        "detection": "Look for database queries, HTTP requests, or awaits inside loops.",
        "fix": "Batch requests, prefetch related data, use joins, or parallelize independent calls.",
        "sinks": ["execute", "query", "fetch", "axios", "requests"],
        "sources": ["for", "while"],
    },
    {
        "category": "performance",
        "pattern": "nested-loop",
        "severity": "medium",
        "title": "Nested loops create quadratic behavior",
        "detection": "Look for nested loops comparing or searching collections.",
        "fix": "Use maps, sets, indexes, or precomputed lookup tables.",
        "sinks": ["nested loop"],
        "sources": ["collection"],
    },
    {
        "category": "performance",
        "pattern": "serial-async",
        "severity": "medium",
        "title": "Independent async work is executed serially",
        "detection": "Look for repeated await/fetch calls in a loop without Promise.all or batching.",
        "fix": "Use Promise.all, bounded concurrency, or backend batch APIs.",
        "sinks": ["await", "fetch"],
        "sources": ["for", "ids"],
    },
    {
        "category": "performance",
        "pattern": "bulk-file-read",
        "severity": "medium",
        "title": "Large data is loaded into memory at once",
        "detection": "Look for readlines, readAll, readFileSync, or unbounded full dataset loading.",
        "fix": "Stream data, paginate queries, or process chunks.",
        "sinks": ["readlines", "readAll", "readFileSync"],
        "sources": ["file", "dataset"],
    },
    {
        "category": "maintainability",
        "pattern": "weak-typing",
        "severity": "medium",
        "title": "Weak or overly broad types reduce maintainability",
        "detection": "Look for excessive any, dict, object, or unstructured data where a schema is known.",
        "fix": "Introduce typed models, interfaces, or explicit schemas.",
        "sinks": ["any", "dict", "object"],
        "sources": ["props", "state", "payload"],
    },
    {
        "category": "maintainability",
        "pattern": "broad-empty-error-handling",
        "severity": "medium",
        "title": "Broad or empty exception handling hides failures",
        "detection": "Look for except/pass, broad Exception catches, or empty catch blocks.",
        "fix": "Catch specific exceptions, log context, and surface actionable failures.",
        "sinks": ["except Exception", "except:", "catch"],
        "sources": ["error handling"],
    },
    {
        "category": "maintainability",
        "pattern": "large-complex-block",
        "severity": "medium",
        "title": "Large function/component mixes multiple responsibilities",
        "detection": "Look for long functions, god classes, oversized React components, or deep branching.",
        "fix": "Split responsibilities into smaller functions, components, or services.",
        "sinks": ["long function", "god class", "large component"],
        "sources": ["component", "class", "function"],
    },
]


@dataclass
class MemoryCandidate:
    score: float
    meta: dict[str, Any]
    document: str


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _stable_hash(text: str, length: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:length]


def _extract_key_sections(code: str, max_len: int = 8000) -> str:
    if len(code) <= max_len:
        return code
    truncated = code[:max_len]
    last_boundary = max(
        truncated.rfind("\ndef "),
        truncated.rfind("\nclass "),
        truncated.rfind("\nasync def "),
        truncated.rfind("\nfunction "),
    )
    if last_boundary > max_len // 2:
        truncated = code[:last_boundary]
    return truncated + "\n# ... (truncated) ...\n" + code[-1200:]


def _extract_function_names(code: str) -> str:
    names = re.findall(r"(?:def|class|async def|function)\s+([A-Za-z_]\w*)", code)
    names.extend(re.findall(r"(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\(", code))
    return " ".join(names[:60])


def _extract_project_id(file_path: str) -> str:
    path = Path(file_path)
    try:
        resolved = path.resolve()
        cwd = Path.cwd().resolve()
        if resolved == cwd or cwd in resolved.parents:
            return cwd.name
    except OSError:
        pass

    probe = path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
    start = probe if probe.is_dir() else probe.parent
    for parent in [start, *start.parents]:
        if any((parent / marker).exists() for marker in [".git", "pyproject.toml", "package.json", "requirements.txt"]):
            return parent.name
    if path.parent.name:
        return path.parent.name
    return "unknown"


def _extract_language(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".java": "java",
        ".go": "go",
    }.get(ext, "unknown")


def _parse_line_range(line_range: str) -> tuple[int, int] | None:
    if not line_range:
        return None
    try:
        cleaned = line_range.replace("L", "").replace(" ", "")
        parts = cleaned.split("-")
        start = int(parts[0])
        end = int(parts[1]) if len(parts) > 1 else start
        if start <= 0 or end <= 0:
            return None
        return min(start, end), max(start, end)
    except (ValueError, IndexError):
        return None


def _extract_code_snippet(code: str, line_range: str, context: int = 3, max_chars: int = 1200) -> str:
    parsed = _parse_line_range(line_range)
    if not parsed:
        return _extract_key_sections(code, max_len=max_chars)

    lines = code.splitlines()
    if not lines:
        return ""
    start, end = parsed
    lo = max(1, start - context)
    hi = min(len(lines), end + context)
    snippet = "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(lo, hi + 1))
    if len(snippet) > max_chars:
        return snippet[:max_chars] + "\n# ... (snippet truncated) ..."
    return snippet


def _detect_patterns(finding: ReviewFinding, code_snippet: str, full_code: str = "") -> list[str]:
    text = f"{finding.title}\n{finding.description}\n{code_snippet}\n{full_code[:1500]}".lower()
    patterns: list[tuple[str, list[str]]] = [
        ("sql-string-construction", ["select ", "insert ", "update ", "delete ", "execute(", "query(", "%", "f\""]),
        ("shell-command-execution", ["os.system", "subprocess", "child_process", "exec("]),
        ("xss-html-sink", ["innerhtml", "dangerouslysetinnerhtml", "document.write", "render_template_string"]),
        ("path-file-access", ["open(", "readfile", "fileinputstream", "ioutil.readfile"]),
        ("unsafe-deserialization", ["pickle.load", "pickle.loads", "yaml.load", "objectinputstream", "eval("]),
        ("weak-crypto-random", ["md5", "sha1", "math.random", "math/rand", "random."]),
        ("hardcoded-secret", ["api_key", "secret", "password", "token"]),
        ("loop-database-or-api-call", ["for ", "while ", "execute(", "query(", "fetch(", "axios.", "requests."]),
        ("nested-loop", ["for ", "while "]),
        ("bulk-file-read", ["readlines(", "readall(", "readfilesync"]),
        ("serial-async", ["await ", "promise.all"]),
        ("weak-typing", ["any", "dict", "object"]),
        ("broad-empty-error-handling", ["except:", "except exception", "catch (e)", "catch(e)", "pass"]),
        ("large-complex-block", ["god", "responsibility", "long function", "deep nesting"]),
    ]

    detected = []
    for name, needles in patterns:
        hits = sum(1 for needle in needles if needle in text)
        if name == "loop-database-or-api-call":
            if ("for " in text or "while " in text) and any(n in text for n in ["execute(", "query(", "fetch(", "axios.", "requests."]):
                detected.append(name)
            continue
        if name == "nested-loop":
            if len(re.findall(r"\b(for|while)\b", code_snippet.lower())) >= 2:
                detected.append(name)
            continue
        if name == "serial-async":
            if text.count("await ") >= 2 and "promise.all" not in text:
                detected.append(name)
            continue
        if hits >= 1:
            detected.append(name)

    if not detected:
        detected.append(f"{finding.category}-general")
    return sorted(set(detected))[:8]


def _normalize_source(finding: ReviewFinding) -> str:
    source = getattr(finding, "source", "llm") or "llm"
    return str(source).lower()


def _normalize_evidence(finding: ReviewFinding) -> list[str]:
    evidence = getattr(finding, "evidence", []) or []
    if isinstance(evidence, str):
        evidence = [evidence]
    return [str(item) for item in evidence if item]


def _normalize_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _build_seed_rule_document(rule: dict[str, Any]) -> str:
    return "\n".join([
        f"Seed review rule: {rule['title']}",
        f"Category: {rule['category']}",
        f"Severity: {rule['severity']}",
        f"Pattern: {rule['pattern']}",
        f"Detection: {rule['detection']}",
        f"Fix: {rule['fix']}",
        f"Sinks: {', '.join(rule.get('sinks', []))}",
        f"Sources: {', '.join(rule.get('sources', []))}",
    ])


class KnowledgeBase:
    """Chroma-backed review memory with feedback-aware retrieval."""

    DISTILL_THRESHOLD = 80
    PATTERN_DISTILL_MIN = 4
    FEEDBACK_MIN = 3
    FEEDBACK_CONFIDENCE_THRESHOLD = 0.75
    WEIGHT_DECAY_RATE = 0.01
    WEIGHT_MIN = 0.2

    def __init__(self, db_path: str | None = None):
        from chromadb import PersistentClient
        from chromadb.utils import embedding_functions
        from src.config import config

        path = db_path or config.knowledge_db_path
        Path(path).mkdir(parents=True, exist_ok=True)
        self.client = PersistentClient(path=path)
        self.ef = embedding_functions.DefaultEmbeddingFunction()
        self._pp_dir = Path(path).parent / "project_profiles"
        self._pp_dir.mkdir(parents=True, exist_ok=True)
        self._init_collections()

    def _init_collections(self):
        collections = {
            "raw_findings": "Layer 1: raw review findings with feedback and snippets",
            "distilled_rules": "Layer 2: feedback/evidence-backed review rules",
            "accuracy_tracker": "Agent/tool accuracy statistics",
        }
        for name, desc in collections.items():
            try:
                coll = self.client.get_collection(name=name, embedding_function=self.ef)
            except Exception:
                coll = self.client.create_collection(
                    name=name,
                    embedding_function=self.ef,
                    metadata={"description": desc},
                )
            setattr(self, name, coll)

    def store_review(self, file_path: str, code: str, findings: list[ReviewFinding]):
        """Store review findings as searchable memory records."""
        if not findings:
            return

        language = _extract_language(file_path)
        project_id = _extract_project_id(file_path)
        now = time.time()
        parent_id = _stable_hash(f"{project_id}:{file_path}:{now}", 16)

        ids, docs, metas = [], [], []
        for finding in findings:
            finding_id = finding.finding_id or _stable_hash(
                f"{file_path}:{finding.line_range}:{finding.category}:{finding.title}"
            )
            source = _normalize_source(finding)
            evidence = _normalize_evidence(finding)
            snippet = _extract_code_snippet(code, finding.line_range)
            patterns = _detect_patterns(finding, snippet, code)
            confidence = _normalize_confidence(finding.confidence)

            record_id = _stable_hash(f"{finding_id}:{source}:{finding.title}:{now}", 20)
            ids.append(record_id)
            docs.append(self._build_memory_document(finding, snippet, patterns, source, evidence))
            metas.append({
                "schema_version": 2,
                "finding_id": finding_id,
                "file_path": str(file_path),
                "project_id": project_id,
                "language": language,
                "category": finding.category if finding.category in VALID_CATEGORIES else "maintainability",
                "severity": finding.severity,
                "line_range": finding.line_range,
                "title": finding.title[:300],
                "source": source,
                "evidence": _json_dumps(evidence),
                "patterns": _json_dumps(patterns),
                "confidence": confidence,
                "verdict": "unknown",
                "action": "",
                "weight": 1.0,
                "fix_available": bool(finding.fix_suggestion),
                "timestamp": now,
                "parent_id": parent_id,
                "snippet_hash": _stable_hash(snippet),
            })

        self.raw_findings.upsert(ids=ids, documents=docs, metadatas=metas)
        self._update_project_profile(project_id, language, findings, now, code)

        try:
            if self.raw_findings.count() >= self.DISTILL_THRESHOLD:
                self._distill()
        except Exception:
            pass

    def _build_memory_document(self, finding: ReviewFinding, snippet: str,
                               patterns: list[str], source: str,
                               evidence: list[str]) -> str:
        return "\n".join([
            f"Title: {finding.title}",
            f"Category: {finding.category}",
            f"Severity: {finding.severity}",
            f"Source: {source}",
            f"Evidence: {', '.join(evidence) if evidence else 'none'}",
            f"Patterns: {', '.join(patterns)}",
            f"Description: {finding.description}",
            f"Fix: {finding.fix_suggestion}",
            "Code snippet:",
            snippet,
        ])

    def _distill(self):
        """Distill observed memory into pattern-level rules."""
        try:
            results = self.raw_findings.get()
        except Exception:
            return
        if not results.get("ids"):
            return

        groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        for meta in results.get("metadatas", []):
            language = meta.get("language", "unknown")
            category = meta.get("category", "maintainability")
            source = meta.get("source", "llm")
            patterns = _json_loads(meta.get("patterns"), [f"{category}-general"])
            verdict = meta.get("verdict") or meta.get("action") or "unknown"
            for pattern in patterns:
                key = (language, category, pattern)
                stats = groups.setdefault(key, {
                    "total": 0,
                    "confirmed": 0,
                    "rejected": 0,
                    "static": 0,
                    "sources": {},
                    "examples": [],
                })
                stats["total"] += 1
                if verdict == "confirm" or verdict == "confirmed":
                    stats["confirmed"] += 1
                elif verdict == "reject" or verdict == "rejected":
                    stats["rejected"] += 1
                if source != "llm":
                    stats["static"] += 1
                stats["sources"][source] = stats["sources"].get(source, 0) + 1
                if len(stats["examples"]) < 3:
                    stats["examples"].append(meta.get("title", ""))

        now = time.time()
        ids, docs, metas = [], [], []
        for (language, category, pattern), stats in groups.items():
            total = stats["total"]
            votes = stats["confirmed"] + stats["rejected"]
            if total < self.PATTERN_DISTILL_MIN and votes < self.FEEDBACK_MIN:
                continue

            rule_type = "observed"
            confidence = min(0.65, 0.35 + total * 0.04)
            if votes >= self.FEEDBACK_MIN:
                confirm_rate = stats["confirmed"] / votes
                reject_rate = stats["rejected"] / votes
                if confirm_rate >= self.FEEDBACK_CONFIDENCE_THRESHOLD:
                    rule_type = "positive"
                    confidence = confirm_rate
                elif reject_rate >= self.FEEDBACK_CONFIDENCE_THRESHOLD:
                    rule_type = "negative"
                    confidence = reject_rate
            elif stats["static"] > 0:
                rule_type = "tool_evidence"
                confidence = min(0.78, 0.5 + stats["static"] * 0.06)

            rule_id = _stable_hash(f"{language}:{category}:{pattern}:{rule_type}", 18)
            ids.append(rule_id)
            docs.append(self._format_rule_document(language, category, pattern, rule_type, confidence, stats))
            metas.append({
                "schema_version": 2,
                "language": language,
                "category": category,
                "pattern": pattern,
                "type": rule_type,
                "confidence": float(confidence),
                "sample_count": int(total),
                "feedback_count": int(votes),
                "confirmed": int(stats["confirmed"]),
                "rejected": int(stats["rejected"]),
                "static_count": int(stats["static"]),
                "sources": _json_dumps(stats["sources"]),
                "timestamp": now,
            })

        if ids:
            self.distilled_rules.upsert(ids=ids, documents=docs, metadatas=metas)

    def _format_rule_document(self, language: str, category: str, pattern: str,
                              rule_type: str, confidence: float,
                              stats: dict[str, Any]) -> str:
        examples = "; ".join(x for x in stats.get("examples", []) if x)
        return (
            f"{rule_type} review rule for {language}/{category}: pattern={pattern}, "
            f"confidence={confidence:.0%}, samples={stats['total']}, "
            f"confirmed={stats['confirmed']}, rejected={stats['rejected']}. "
            f"Examples: {examples}"
        )

    def seed_knowledge(self) -> int:
        """Initialize distilled rules with curated seed review knowledge."""
        now = time.time()
        ids, docs, metas = [], [], []
        for rule in SEED_REVIEW_RULES:
            rule_id = _stable_hash(f"seed:{rule['category']}:{rule['pattern']}", 18)
            ids.append(rule_id)
            docs.append(_build_seed_rule_document(rule))
            metas.append({
                "schema_version": 2,
                "language": "unknown",
                "category": rule["category"],
                "pattern": rule["pattern"],
                "type": "seed",
                "confidence": 0.9,
                "sample_count": 0,
                "feedback_count": 0,
                "confirmed": 0,
                "rejected": 0,
                "static_count": 0,
                "sources": _json_dumps({"seed": 1}),
                "severity": rule["severity"],
                "title": rule["title"],
                "sinks": _json_dumps(rule.get("sinks", [])),
                "source_hints": _json_dumps(rule.get("sources", [])),
                "timestamp": now,
            })

        self.distilled_rules.upsert(ids=ids, documents=docs, metadatas=metas)
        return len(ids)

    def _update_project_profile(self, project_id: str, language: str,
                                findings: list[ReviewFinding], timestamp: float,
                                code: str = ""):
        pp_file = self._pp_dir / f"{project_id}.json"
        profile: dict[str, Any] = {}
        if pp_file.exists():
            try:
                profile = json.loads(pp_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                profile = {}

        profile["project_id"] = project_id
        profile["last_updated"] = timestamp
        profile["total_findings"] = profile.get("total_findings", 0) + len(findings)
        profile["function_names"] = sorted(set(
            profile.get("function_names", []) + _extract_function_names(code).split()
        ))[:80]

        language_counts = profile.setdefault("languages", {})
        language_counts[language] = language_counts.get(language, 0) + len(findings)

        category_counts = profile.setdefault("categories", {})
        severity_counts = profile.setdefault("severities", {})
        source_counts = profile.setdefault("sources", {})
        pattern_counts = profile.setdefault("patterns", {})

        for finding in findings:
            category_counts[finding.category] = category_counts.get(finding.category, 0) + 1
            severity_counts[finding.severity] = severity_counts.get(finding.severity, 0) + 1
            source = _normalize_source(finding)
            source_counts[source] = source_counts.get(source, 0) + 1
            snippet = _extract_code_snippet(code, finding.line_range)
            for pattern in _detect_patterns(finding, snippet, code):
                pattern_counts[pattern] = pattern_counts.get(pattern, 0) + 1

        pp_file.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_project_profile(self, project_id: str) -> dict:
        pp_file = self._pp_dir / f"{project_id}.json"
        if not pp_file.exists():
            return {}
        try:
            return json.loads(pp_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _decay_weight(self, weight: float, timestamp: float) -> float:
        days = max(0.0, (time.time() - float(timestamp or 0)) / 86400)
        return float(weight or 1.0) * math.exp(-self.WEIGHT_DECAY_RATE * days)

    def cleanup_raw(self):
        try:
            results = self.raw_findings.get()
        except Exception:
            return 0

        to_delete = []
        for idx, meta in enumerate(results.get("metadatas", [])):
            decayed = self._decay_weight(meta.get("weight", 1.0), meta.get("timestamp", 0))
            if decayed < self.WEIGHT_MIN:
                to_delete.append(results["ids"][idx])
        if to_delete:
            self.raw_findings.delete(ids=to_delete)
        return len(to_delete)

    def get_few_shot_examples(self, code: str, top_k: int = 3,
                              file_path: str | None = None,
                              category: str | None = None) -> str:
        """Retrieve feedback-aware examples for prompt augmentation."""
        language = _extract_language(file_path or "")
        project_id = _extract_project_id(file_path or "unknown.py") if file_path else ""
        query_text = "\n".join([
            _extract_key_sections(code, max_len=4000),
            "Symbols: " + _extract_function_names(code),
        ])

        lines: list[str] = []
        profile_lines = self._build_profile_prompt(project_id)
        if profile_lines:
            lines.extend(profile_lines)

        rule_lines = self._build_rule_prompt(language=language, category=category)
        if rule_lines:
            lines.extend(rule_lines)

        candidates = self._retrieve_memory_candidates(
            query_text=query_text,
            top_k=top_k,
            language=language,
            project_id=project_id,
            category=category,
        )
        if candidates:
            lines.append("## Similar Confirmed Review Memories")
            for candidate in candidates[:top_k]:
                meta = candidate.meta
                patterns = ", ".join(_json_loads(meta.get("patterns"), []))
                source = meta.get("source", "llm")
                lines.append(
                    f"- [{meta.get('severity', '?')}] [{meta.get('category', '?')}] "
                    f"{meta.get('title', '?')} | source={source} | "
                    f"patterns={patterns or 'n/a'} | score={candidate.score:.2f}"
                )
            lines.append("")

        return "\n".join(lines)

    def _build_profile_prompt(self, project_id: str) -> list[str]:
        if not project_id:
            return []
        profile = self.get_project_profile(project_id)
        if not profile:
            return []

        lines = ["## Project Review Profile"]
        categories = profile.get("categories", {})
        severities = profile.get("severities", {})
        sources = profile.get("sources", {})
        patterns = profile.get("patterns", {})
        if categories:
            lines.append(f"- Historical categories: {categories}")
        if severities:
            lines.append(f"- Historical severity distribution: {severities}")
        if sources:
            lines.append(f"- Evidence sources: {sources}")
        if patterns:
            top_patterns = sorted(patterns.items(), key=lambda x: -x[1])[:6]
            lines.append(f"- Frequent risk patterns: {dict(top_patterns)}")
        lines.append("")
        return lines

    def _build_rule_prompt(self, language: str = "unknown",
                           category: str | None = None) -> list[str]:
        try:
            rules = self.distilled_rules.get()
        except Exception:
            return []
        if not rules.get("ids"):
            return []

        relevant = []
        for meta in rules.get("metadatas", []):
            if language != "unknown" and meta.get("language") not in (language, "unknown"):
                continue
            if category and meta.get("category") != category:
                continue
            relevant.append(meta)

        if not relevant:
            return []

        relevant.sort(key=lambda m: (m.get("type") != "positive", -float(m.get("confidence", 0))))
        lines = ["## Distilled Review Rules"]
        for meta in relevant[:8]:
            direction = {
                "seed": "seed rule",
                "positive": "prioritize",
                "negative": "be cautious",
                "tool_evidence": "verify tool-backed pattern",
                "observed": "observed pattern",
            }.get(meta.get("type"), "observed pattern")
            lines.append(
                f"- {direction}: [{meta.get('category')}] {meta.get('pattern')} "
                f"(confidence={float(meta.get('confidence', 0)):.0%}, n={meta.get('sample_count')})"
            )
        lines.append("")
        return lines

    def _retrieve_memory_candidates(self, query_text: str, top_k: int,
                                    language: str, project_id: str,
                                    category: str | None) -> list[MemoryCandidate]:
        try:
            total = self.raw_findings.count()
            if total == 0:
                return []
            fetch_n = min(max(top_k * 10, 12), total)
            results = self.raw_findings.query(
                query_texts=[query_text],
                n_results=fetch_n,
                include=["documents", "metadatas", "distances"],
            )
        except Exception:
            return []

        documents = (results.get("documents") or [[]])[0]
        metadatas = (results.get("metadatas") or [[]])[0]
        distances = (results.get("distances") or [[]])[0] if results.get("distances") else [None] * len(documents)

        candidates = []
        for doc, meta, distance in zip(documents, metadatas, distances):
            if language != "unknown" and meta.get("language") not in (language, "unknown"):
                continue
            if category and meta.get("category") != category:
                continue
            score = self._score_memory(meta, distance)
            if project_id and meta.get("project_id") == project_id:
                score += 0.08
            if score <= 0:
                continue
            candidates.append(MemoryCandidate(score=score, meta=meta, document=doc))

        candidates.sort(key=lambda item: -item.score)
        return candidates

    def _score_memory(self, meta: dict[str, Any], distance: float | None) -> float:
        similarity = 0.5 if distance is None else 1.0 / (1.0 + max(0.0, float(distance)))
        decayed = self._decay_weight(meta.get("weight", 1.0), meta.get("timestamp", time.time()))
        if decayed < self.WEIGHT_MIN:
            return 0.0

        verdict = meta.get("verdict") or meta.get("action") or "unknown"
        verdict_bonus = 0.35 if verdict in ("confirm", "confirmed") else -0.45 if verdict in ("reject", "rejected") else 0.0
        source_bonus = SOURCE_WEIGHT.get(meta.get("source", "llm"), 0.0)
        severity_bonus = SEVERITY_WEIGHT.get(meta.get("severity", "low"), 0.0)
        confidence_bonus = _normalize_confidence(meta.get("confidence", 0.0)) * 0.12
        return similarity + decayed * 0.35 + verdict_bonus + source_bonus + severity_bonus + confidence_bonus

    def get_dynamic_prompt_hints(self, file_path: str | None = None,
                                 category: str | None = None) -> str:
        language = _extract_language(file_path or "")
        lines = self._build_rule_prompt(language=language, category=category)
        if not lines:
            return ""
        return "\n".join(["## Feedback-Aware Review Guidance", *lines])

    def submit_feedback(self, file_path: str, finding_ref: str,
                        action: str, note: str = "",
                        distill: bool = True):
        """Attach user feedback to matching raw findings."""
        action = "confirm" if action in ("confirm", "confirmed", "c") else "reject"
        matched = self._submit_to_collection("raw_findings", file_path, finding_ref, action, note)
        if not matched:
            matched = self._submit_feedback_legacy(file_path, finding_ref, action, note)
        if matched and distill:
            self._distill()
        return matched

    def distill_feedback(self):
        """Refresh distilled rules after a batch of feedback updates."""
        self._distill()

    def _submit_to_collection(self, coll_name: str, file_path: str,
                              finding_ref: str, action: str, note: str = "") -> bool:
        try:
            coll = getattr(self, coll_name, None)
            if coll is None:
                coll = self.client.get_collection(name=coll_name, embedding_function=self.ef)
            results = coll.get()
        except Exception:
            return False
        if not results.get("ids"):
            return False

        target_name = Path(file_path).name
        matched = False
        for idx, meta in enumerate(results.get("metadatas", [])):
            stored_path = meta.get("file_path", "")
            fid = str(meta.get("finding_id", ""))
            title = str(meta.get("title", ""))
            path_match = target_name in stored_path or target_name == Path(stored_path).name
            ref_match = finding_ref == fid or finding_ref in fid or finding_ref.lower() in title.lower()
            if not (path_match or ref_match):
                continue
            if not ref_match:
                continue

            matched = True
            meta["action"] = action
            meta["verdict"] = "confirmed" if action == "confirm" else "rejected"
            meta["feedback_note"] = note
            meta["reviewed_at"] = time.time()
            meta["weight"] = 1.2 if action == "confirm" else 0.7
            coll.update(ids=[results["ids"][idx]], metadatas=[meta])

        return matched

    def _submit_feedback_legacy(self, file_path: str, finding_ref: str,
                                action: str, note: str = "") -> bool:
        target_name = Path(file_path).name
        try:
            old = self.client.get_collection(name="code_reviews", embedding_function=self.ef)
            results = old.get()
        except Exception:
            return False
        if not results.get("ids"):
            return False

        matched = False
        for idx, meta in enumerate(results.get("metadatas", [])):
            stored_path = meta.get("file_path", "")
            if target_name not in stored_path:
                continue
            matched = True
            feedback = _json_loads(meta.get("feedback"), {})
            feedback[finding_ref] = {"action": action, "note": note, "timestamp": time.time()}
            old.update(ids=[results["ids"][idx]], metadatas=[{**meta, "feedback": _json_dumps(feedback)}])
        return matched

    def get_accuracy_stats(self) -> dict:
        try:
            results = self.raw_findings.get()
        except Exception:
            return {"total_reviews": 0, "agent_stats": {}, "source_stats": {}}
        if not results.get("ids"):
            return {"total_reviews": 0, "agent_stats": {}, "source_stats": {}}

        agent_stats = {cat: {"confirmed": 0, "rejected": 0, "unknown": 0} for cat in VALID_CATEGORIES}
        source_stats: dict[str, dict[str, int]] = {}
        for meta in results.get("metadatas", []):
            category = meta.get("category", "maintainability")
            source = meta.get("source", "llm")
            verdict = meta.get("verdict") or meta.get("action") or "unknown"
            bucket = "confirmed" if verdict in ("confirm", "confirmed") else "rejected" if verdict in ("reject", "rejected") else "unknown"
            if category in agent_stats:
                agent_stats[category][bucket] += 1
            source_stats.setdefault(source, {"confirmed": 0, "rejected": 0, "unknown": 0})[bucket] += 1

        for stats in [*agent_stats.values(), *source_stats.values()]:
            confirmed = stats["confirmed"]
            rejected = stats["rejected"]
            stats["accuracy"] = confirmed / (confirmed + rejected) if confirmed + rejected else 0.0

        return {
            "total_reviews": len(results["ids"]),
            "agent_stats": agent_stats,
            "source_stats": source_stats,
        }

    def cleanup_old_records(self, days: int | None = None):
        return self.cleanup_raw()


def submit_feedback(file_path: str, confirm: str | None = None,
                    reject: str | None = None, note: str = ""):
    from rich.console import Console

    console = Console()
    kb = KnowledgeBase()
    if confirm:
        ok = kb.submit_feedback(file_path, confirm, "confirm", note)
        console.print(f"[green]{'confirmed' if ok else 'not matched'}: {confirm}[/green]")
    if reject:
        ok = kb.submit_feedback(file_path, reject, "reject", note)
        console.print(f"[yellow]{'rejected' if ok else 'not matched'}: {reject}[/yellow]")


def seed_knowledge():
    from rich.console import Console

    console = Console()
    kb = KnowledgeBase()
    count = kb.seed_knowledge()
    console.print(f"[green]Seed knowledge initialized: {count} review rules[/green]")
    console.print("[dim]Stored in distilled_rules; rerunning this command updates existing seed rules instead of duplicating them.[/dim]")


def show_stats():
    from rich.console import Console
    from rich.table import Table

    console = Console()
    kb = KnowledgeBase()
    stats = kb.get_accuracy_stats()
    console.print(f"\n[bold]Total memory records: {stats['total_reviews']}[/bold]\n")

    table = Table(title="Review Memory Accuracy")
    table.add_column("Bucket")
    table.add_column("Confirmed")
    table.add_column("Rejected")
    table.add_column("Unknown")
    table.add_column("Accuracy")

    for name, bucket in stats.get("agent_stats", {}).items():
        table.add_row(
            name,
            str(bucket["confirmed"]),
            str(bucket["rejected"]),
            str(bucket["unknown"]),
            f"{bucket.get('accuracy', 0):.0%}",
        )
    for name, bucket in stats.get("source_stats", {}).items():
        table.add_row(
            f"source:{name}",
            str(bucket["confirmed"]),
            str(bucket["rejected"]),
            str(bucket["unknown"]),
            f"{bucket.get('accuracy', 0):.0%}",
        )
    console.print(table)
