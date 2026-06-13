"""增量审查：仅审查 Git diff 变更部分"""

import re
import subprocess
from pathlib import Path


def _validate_repo_path(repo_path: str) -> bool:
    """校验仓库路径合法性，防止路径遍历和非法字符注入"""
    p = Path(repo_path).resolve()
    if not p.exists() or not p.is_dir():
        return False
    if not (p / ".git").exists():
        return False
    if re.search(r'[;&|`$]', repo_path):
        return False
    return True


def _validate_branch(branch: str) -> bool:
    """校验分支名合法性"""
    return bool(re.match(r'^[a-zA-Z0-9._\-~/]+$', branch))


def _validate_diff_file(repo_path: str, file_path: str) -> bool:
    """Ensure diff file path is a relative path inside the repository."""
    if not file_path or Path(file_path).is_absolute():
        return False
    if re.search(r'[;&|`$]', file_path):
        return False
    repo = Path(repo_path).resolve()
    target = (repo / file_path).resolve()
    try:
        target.relative_to(repo)
    except ValueError:
        return False
    return True


def get_diff_files(repo_path: str, base_branch: str = "HEAD~1") -> list[str]:
    """获取相对于 base_branch 的变更文件列表"""
    if not _validate_repo_path(repo_path) or not _validate_branch(base_branch):
        return []
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(repo_path).resolve()), "diff", "--name-only", base_branch],
            capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return []
        return [f for f in result.stdout.strip().split("\n") if f]
    except Exception:
        return []


def get_all_file_diffs(repo_path: str, base_branch: str = "HEAD~1") -> dict[str, str]:
    """一次性获取全部 diff，并按文件拆分，避免为每个文件重复启动 git。"""
    if not _validate_repo_path(repo_path) or not _validate_branch(base_branch):
        return {}
    try:
        repo = str(Path(repo_path).resolve())
        result = subprocess.run(
            ["git", "-C", repo, "diff", base_branch, "--"],
            capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            return {}
        return _split_diff_by_file(result.stdout)
    except Exception:
        return {}


def _split_diff_by_file(diff_text: str) -> dict[str, str]:
    file_diffs: dict[str, list[str]] = {}
    current_file = ""

    for line in diff_text.splitlines():
        match = re.match(r"diff --git a/(.+?) b/(.+)", line)
        if match:
            current_file = match.group(2)
            file_diffs[current_file] = [line]
            continue
        if current_file:
            file_diffs[current_file].append(line)

    return {file_path: "\n".join(lines) for file_path, lines in file_diffs.items()}


def get_file_diff(repo_path: str, file_path: str,
                  base_branch: str = "HEAD~1") -> str:
    """获取单个文件相对于 base_branch 的 diff"""
    if (
        not _validate_repo_path(repo_path)
        or not _validate_branch(base_branch)
        or not _validate_diff_file(repo_path, file_path)
    ):
        return ""
    try:
        repo = str(Path(repo_path).resolve())
        result = subprocess.run(
            ["git", "-C", repo, "diff", base_branch, "--", file_path],
            capture_output=True, text=True, timeout=10)
        return result.stdout.strip()
    except Exception:
        return ""


def extract_changed_lines(diff_text: str, original_code: str) -> str:
    """从 diff 和原始代码中提取变更相关的代码片段"""
    if not diff_text:
        return original_code[:5000]

    # 解析 diff 中 + 开头的行号
    changed_lines = set()
    for line in diff_text.split("\n"):
        if line.startswith("@@"):
            parts = line.split(" ")
            for p in parts:
                if p.startswith("+") and "," in p:
                    try:
                        start = int(p[1:].split(",")[0])
                        count = int(p[1:].split(",")[1])
                        for i in range(start, start + count):
                            changed_lines.add(i)
                    except (ValueError, IndexError):
                        pass

    if not changed_lines:
        return original_code[:5000]

    # 提取变更行及上下文（前后各 3 行）
    code_lines = original_code.split("\n")
    extracted = []
    included = set()
    for ln in sorted(changed_lines):
        for ctx in range(max(0, ln - 4), min(len(code_lines), ln + 3)):
            if ctx not in included:
                included.add(ctx)
                extracted.append(f"L{ctx+1}: {code_lines[ctx]}")

    return "\n".join(extracted)[:8000]


def run_diff_review(repo_path: str, base_branch: str = "HEAD~1") -> dict:
    """执行增量审查"""
    changed = get_diff_files(repo_path, base_branch)
    if not changed:
        return {"files": [], "message": "无变更文件"}

    supported_exts = {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go"}
    file_diffs = get_all_file_diffs(repo_path, base_branch)
    review_files = []
    for f in changed:
        ext = Path(f).suffix.lower()
        if ext in supported_exts:
            full_path = str(Path(repo_path) / f)
            try:
                original = Path(full_path).read_text(encoding="utf-8", errors="ignore")
                diff = file_diffs.get(f) or get_file_diff(repo_path, f, base_branch)
                snippet = extract_changed_lines(diff, original)
                review_files.append((f, snippet, original[:500]))
            except Exception:
                pass

    return {
        "files": review_files,
        "total_changed": len(changed),
        "reviewable": len(review_files),
        "base_branch": base_branch,
    }
