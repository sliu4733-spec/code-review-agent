"""Web UI — FastAPI 后端 + 辩论过程展示 + 流式输出"""

import json
import re
import tempfile
import threading
import time
from pathlib import Path
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from src.agents.security import SecurityAgent
from src.agents.performance import PerformanceAgent
from src.agents.maintainability import MaintainabilityAgent
from src.agents.arbiter import ArbiterAgent
from src.agents.base import ReviewFinding
from src.core.debate import detect_conflicts, conduct_debate
from src.core.knowledge import KnowledgeBase
from src.core.reviewer import (
    build_project_context,
    cross_file_check,
    quality_filter,
    run_adaptive_review,
    run_multi_review,
    run_single_review,
)
from src.core.voting import run_multi_model_review
from src.core.diff_review import run_diff_review
from src.core.rules_config import RULES_FILE, load_rules, get_enabled_rules

app = FastAPI(title="Code Review Agent", version="3.2.0")
kb = KnowledgeBase()
kb_lock = threading.RLock()
REPORT_DIR = Path("data") / "reports"
SKIP_UPLOAD_DIRS = {"test_cases", "__pycache__", ".venv", "node_modules", ".git"}


def _safe_report_stem(value: str, fallback: str = "web-input") -> str:
    normalized = str(value or fallback).replace("\\", "/")
    stem = Path(normalized).stem or fallback
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._")
    return safe or fallback


def _safe_upload_relative_path(value: str) -> Path | None:
    normalized = str(value or "").replace("\\", "/").strip("/")
    if not normalized:
        return None
    raw_parts = [part for part in normalized.split("/") if part]
    if any(part in {"..", "."} for part in raw_parts):
        return None
    if any(":" in part for part in raw_parts):
        return None
    return Path(*raw_parts)


def _write_upload_file(temp_root: Path, display_name: str, code: str) -> str | None:
    relative = _safe_upload_relative_path(display_name)
    if relative is None:
        return None
    target = (temp_root / relative).resolve()
    try:
        target.relative_to(temp_root.resolve())
    except ValueError:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(code or "", encoding="utf-8", errors="ignore")
    return str(target)


def _save_report_file(report: str, file_path: str, mode: str,
                      prefix: str = "web_review") -> str:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    stem = _safe_report_stem(file_path)
    output = REPORT_DIR / f"{prefix}_{stem}_{mode}_{timestamp}.md"
    output.write_text(report or "", encoding="utf-8")
    return str(output)


def _knowledge_examples(code: str, file_path: str = "") -> str:
    with kb_lock:
        return kb.get_few_shot_examples(code, file_path=file_path)


def _store_review(file_path: str, code: str, findings: list[ReviewFinding]) -> None:
    if not findings:
        return
    with kb_lock:
        kb.store_review(file_path, code, findings)


def _submit_feedback(file_path: str, title: str, action: str) -> bool:
    with kb_lock:
        return kb.submit_feedback(file_path, title, action)


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    import logging
    logging.exception("Unhandled exception")
    return JSONResponse(
        {"error": "Internal server error", "findings": [], "count": 0,
         "severity_breakdown": {}, "report": "服务器内部错误，请稍后重试",
         "debate_transcripts": [], "agent_raw": {}, "timing": {"total_time": 0}},
        status_code=500)

HTML_TEMPLATE = (Path(__file__).parent / "templates" / "index.html").read_text(
    encoding="utf-8")


class ReviewRequest(BaseModel):
    code: str
    file_path: str = "web-input.py"
    mode: str = "multi"
    prefer: str | None = None


class ProjectFile(BaseModel):
    name: str
    code: str


class ProjectReviewRequest(BaseModel):
    files: list[ProjectFile]
    mode: str = "multi"


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_TEMPLATE


@app.post("/api/review")
async def review(req: ReviewRequest):
    if req.mode == "adaptive":
        with tempfile.TemporaryDirectory(prefix="code_review_input_") as temp_dir:
            temp_root = Path(temp_dir).resolve()
            evidence_path = _write_upload_file(temp_root, req.file_path, req.code)
            return _run_single_review(req.code, req.file_path, req.mode, evidence_path=evidence_path)
    return _run_single_review(req.code, req.file_path, req.mode)


@app.post("/api/review-project")
async def review_project(req: ProjectReviewRequest):
    t0 = time.time()
    if not req.files:
        return JSONResponse({"error": "请至少上传一个文件"}, status_code=400)

    files = []
    for f in req.files:
        relative = _safe_upload_relative_path(f.name)
        if relative is None:
            continue
        if set(relative.parts) & SKIP_UPLOAD_DIRS:
            continue
        files.append((f.name, f.code))
    if not files:
        return JSONResponse({"error": "所有文件均被过滤，请检查上传内容"}, status_code=400)
    project_context = build_project_context(files)

    # 上传项目先安全落盘到临时目录。报告继续使用原路径，静态工具使用真实磁盘路径。
    from concurrent.futures import ThreadPoolExecutor, as_completed
    file_reports = []
    with tempfile.TemporaryDirectory(prefix="code_review_upload_") as temp_dir:
        temp_root = Path(temp_dir).resolve()
        evidence_paths = {
            f_name: _write_upload_file(temp_root, f_name, f_code)
            for f_name, f_code in files
        }

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(
                    _run_single_review,
                    f_code,
                    f_name,
                    req.mode,
                    False,
                    project_context,
                    evidence_paths.get(f_name),
                ): f_name
                for f_name, f_code in files
            }
            for future in as_completed(futures):
                f_name = futures[future]
                result = future.result()
                result["file"] = f_name
                file_reports.append(result)

    # 按原始顺序排列
    name_order = {name: i for i, (name, _) in enumerate(files)}
    file_reports.sort(key=lambda r: name_order.get(r["file"], 99))

    all_findings = []
    all_debate = []
    for fr in file_reports:
        all_findings.extend(fr["findings"])
        all_debate.extend(fr.get("debate_transcripts", []))

    # 跨文件检查
    cross_findings = cross_file_check(files)
    cross_data = [f.to_dict() for f in cross_findings]

    total_time = time.time() - t0

    combined_report = f"# 项目审查报告\n\n**文件数**: {len(files)}\n**总问题**: {len(all_findings) + len(cross_data)}\n\n"
    for fr in file_reports:
        combined_report += f"\n---\n## {fr['file']}\n{fr['report']}\n"
    if cross_data:
        combined_report += f"\n---\n## 跨文件引用检查\n发现 {len(cross_data)} 个潜在问题\n"
        for item in cross_data:
            combined_report += (
                f"\n- **{str(item.get('severity', 'low')).upper()}** | "
                f"`{item.get('line_range', 'unknown')}` | "
                f"{item.get('category', 'maintainability')} | "
                f"{item.get('title', '')} | {item.get('description', '')}"
            )
    report_file = _save_report_file(combined_report, "project", req.mode, "web_project")

    return JSONResponse({
        "files": [name for name, _ in files],
        "uploaded_files": len(req.files),
        "reviewed_files": len(files),
        "total_findings": len(all_findings) + len(cross_data),
        "findings": all_findings,
        "cross_file_findings": cross_data,
        "cross_file_count": len(cross_data),
        "report": combined_report,
        "report_file": report_file,
        "file_reports": file_reports,
        "debate_transcripts": all_debate,
        "debate_count": len(all_debate),
        "project_context": project_context[:3000],
        "timing": {"total_time": round(total_time, 2)},
    })


@app.websocket("/ws/review")
async def ws_review(websocket: WebSocket):
    """流式审查：并行执行 Agent，实时推送进度"""
    await websocket.accept()
    try:
        data = await websocket.receive_json()
        code = data.get("code", "")
        file_path = data.get("file_path", "ws-input.py")
        mode = data.get("mode", "multi")

        await websocket.send_json({"type": "start"})
        few_shot = _knowledge_examples(code, file_path)

        # 并行执行 3 个 Agent，逐个推送进度
        from concurrent.futures import ThreadPoolExecutor
        import asyncio as aio
        agents = [
            (SecurityAgent(), "security"),
            (PerformanceAgent(), "performance"),
            (MaintainabilityAgent(), "maintainability"),
        ]
        results = {}
        loop = aio.get_running_loop()

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = []
            for agent, label in agents:
                await websocket.send_json({"type": "progress", "agent": label})
                fut = loop.run_in_executor(executor, agent.analyze, code, file_path, few_shot)
                futures.append((fut, label))

            for fut, label in futures:
                result = await fut
                results[label] = result
                await websocket.send_json(
                    {"type": "progress", "agent": label + "_done", "count": len(result)})

        sec = results.get("security", [])
        perf = results.get("performance", [])
        maint = results.get("maintainability", [])

        await websocket.send_json({"type": "progress", "stage": "debate"})
        if mode != "single":
            conflict_info = detect_conflicts(sec, perf, maint)
            transcripts, debate_context = conduct_debate(code, file_path, conflict_info)
            arbiter = ArbiterAgent()
            findings, _ = arbiter.arbitrate(code, file_path, debate_context)
        else:
            findings = sec

        findings = quality_filter(findings, file_path)

        await websocket.send_json({"type": "done", "count": len(findings),
                                   "findings": [f.to_dict() for f in findings]})
    except Exception as e:
        await websocket.send_json({"type": "error", "msg": str(e)})
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.post("/api/review-multi-model")
async def review_multi_model(req: ReviewRequest):
    """多模型投票审查"""
    t0 = time.time()
    security = SecurityAgent()
    sys_prompt = security.get_system_prompt()
    user_prompt = f"请审查以下代码:\n```\n{req.code}\n```\n以JSON格式输出审查结果。"

    findings = run_multi_model_review(req.code, req.file_path, sys_prompt, user_prompt)
    findings = quality_filter(findings, req.file_path)

    total_time = time.time() - t0
    _store_review(req.file_path, req.code, findings)
    report = ArbiterAgent().generate_report(findings, req.file_path, "", "multi")
    report_file = _save_report_file(report, req.file_path, "multi-model", "web_vote")

    return JSONResponse({
        "findings": [f.to_dict() for f in findings],
        "count": len(findings),
        "severity_breakdown": _count_severity(findings),
        "report": report,
        "report_file": report_file,
        "vote_mode": True,
        "timing": {"total_time": round(total_time, 2)},
    })


@app.post("/api/feedback")
async def submit_feedback_api(req: dict):
    """Web UI 提交反馈"""
    file_path = req.get("file_path", "")
    title = req.get("title", "")
    action = req.get("action", "")
    ok = _submit_feedback(file_path, title, action)
    return JSONResponse({"ok": ok, "title": title, "action": action})


@app.get("/api/rules")
async def get_rules():
    """返回当前审查规则配置"""
    rules = load_rules()
    result = {}
    for cat in ["security", "performance", "maintainability"]:
        result[cat] = get_enabled_rules(cat)
        # 补充未启用的规则
        all_cat = rules.get(cat, {}).get("rules", [])
        for r in all_cat:
            if not r.get("enabled", True):
                result[cat].append(r)
    return JSONResponse(result)


@app.post("/api/rules/update")
async def update_rules(req: dict):
    """更新规则启用状态"""
    import yaml
    cat = req.get("category")
    rule_id = req.get("rule_id")
    enabled = req.get("enabled", True)
    rules = load_rules()
    for r in rules.get(cat, {}).get("rules", []):
        if r.get("id") == rule_id:
            r["enabled"] = enabled
            break
    with open(RULES_FILE, "w", encoding="utf-8") as f:
        yaml.dump(rules, f, allow_unicode=True, default_flow_style=False)
    return JSONResponse({"ok": True})


@app.post("/api/review-diff")
async def review_diff(req: dict):
    """增量审查 (Git Diff)"""
    repo_path = req.get("repo_path", ".")
    base_branch = req.get("base_branch", "HEAD~1")
    diff_info = run_diff_review(repo_path, base_branch)
    files = diff_info.get("files", [])
    if not files:
        return JSONResponse({"files": [], "message": diff_info.get("message", "无变更")})

    findings = []
    for f_name, snippet, _ in files:
        result = _run_single_review(snippet, f_name, "multi", False)
        findings.extend(result.get("findings", []))

    report = ArbiterAgent().generate_report(
        [ReviewFinding(**f) if isinstance(f, dict) else f for f in findings[:10]],
        f"{repo_path} (diff vs {base_branch})", "", "multi")
    report_file = _save_report_file(report, f"{repo_path}_{base_branch}", "multi", "web_diff")

    return JSONResponse({
        "diff_info": diff_info,
        "findings": findings[:10],
        "count": len(findings),
        "report": report,
        "report_file": report_file,
    })


def _run_single_review(code: str, file_path: str, mode: str,
                       save_report: bool = True,
                       project_context: str = "",
                       evidence_path: str | None = None) -> dict:
    """执行单文件审查，返回完整结果字典。异常自动兜底。"""
    try:
        return _do_review(code, file_path, mode, save_report, project_context, evidence_path)
    except Exception as e:
        import logging
        logging.exception(f"Review failed for {file_path}")
        return {
            "findings": [], "count": 0,
            "severity_breakdown": {}, "report": f"审查异常: {str(e)[:100]}",
            "debate_context": "", "debate_transcripts": [],
            "agent_raw": {}, "timing": {"agent_time": 0, "total_time": 0},
        }


def _do_review(code: str, file_path: str, mode: str,
               save_report: bool = True,
               project_context: str = "",
               evidence_path: str | None = None) -> dict:
    t0 = time.time()
    mode = mode if mode in ("single", "multi", "adaptive") else "adaptive"

    if mode == "single":
        findings, elapsed = run_single_review(code, file_path, kb=None, cache=None,
                                              project_context=project_context)
        debate_text = ""
    elif mode == "multi":
        findings, debate_text, elapsed = run_multi_review(code, file_path, kb=None, cache=None,
                                                          project_context=project_context)
    else:
        findings, debate_text, elapsed = run_adaptive_review(
            code, file_path, kb=None, cache=None,
            project_context=project_context,
            evidence_path=evidence_path,
        )

    # 统一质量过滤（核心审查函数已经做过一次；这里作为 Web 入参兜底）
    findings = quality_filter(findings, file_path)

    total_time = time.time() - t0
    _store_review(file_path, code, findings)
    report = ArbiterAgent().generate_report(findings, file_path, debate_text, mode)
    report_file = _save_report_file(report, file_path, mode) if save_report else ""

    return {
        "findings": [f.to_dict() for f in findings],
        "count": len(findings),
        "severity_breakdown": _count_severity(findings),
        "report": report,
        "report_file": report_file,
        "debate_context": debate_text[:8000] if debate_text else "",
        "debate_transcripts": [],
        "agent_raw": {"security": [], "performance": [], "maintainability": []},
        "timing": {
            "agent_time": round(elapsed, 2),
            "total_time": round(total_time, 2),
            "debate_count": 0,
        },
    }


def _count_severity(findings: list) -> dict:
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for f in findings:
        if f.severity in counts:
            counts[f.severity] += 1
    return counts


def start():
    import uvicorn
    uvicorn.run("src.web:app", host="127.0.0.1", port=8000, reload=True)


if __name__ == "__main__":
    start()
