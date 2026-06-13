"""Environment diagnostics for local setup and demos."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table


REQUIRED_MODULES = {
    "openai": "openai",
    "anthropic": "anthropic",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "rich": "rich",
    "dotenv": "python-dotenv",
    "yaml": "pyyaml",
    "jieba": "jieba",
    "chromadb": "chromadb",
    "bandit": "bandit",
    "ruff": "ruff",
}


def _module_available(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _tool_detail(tool: str) -> tuple[bool, str]:
    found = shutil.which(tool)
    if found:
        return True, found
    if _module_available(tool):
        return True, f"{sys.executable} -m {tool}"
    return False, "command not found"


def _add_row(rows: list[tuple[str, str, str]], name: str,
             ok: bool, detail: str) -> bool:
    rows.append(("OK" if ok else "FAIL", name, detail))
    return ok


def run_doctor() -> bool:
    """Print a setup report. Returns True when required checks pass."""
    from src.config import config

    rows: list[tuple[str, str, str]] = []
    ok = True

    ok &= _add_row(
        rows,
        "Python",
        sys.version_info >= (3, 10),
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )

    missing = []
    for module, package in REQUIRED_MODULES.items():
        available = _module_available(module)
        if not available:
            missing.append(package)
        ok &= _add_row(
            rows,
            f"Package: {package}",
            available,
            "installed" if available else f"missing; run pip install {package}",
        )

    provider = config.provider
    if provider == "openai":
        provider_ok = bool(config.openai_api_key)
        detail = "OPENAI_API_KEY set" if provider_ok else "OPENAI_API_KEY missing"
    elif provider == "anthropic":
        provider_ok = bool(config.api_key)
        detail = "ANTHROPIC_API_KEY set" if provider_ok else "ANTHROPIC_API_KEY missing"
    elif provider == "ollama":
        provider_ok = True
        detail = "no API key required; ensure Ollama service is running"
    else:
        provider_ok = False
        detail = f"unknown provider: {provider}"
    ok &= _add_row(rows, "LLM provider", provider_ok, f"{provider}: {detail}")

    data_dir = Path("data")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        writable = True
    except OSError as exc:
        writable = False
        detail = str(exc)
    else:
        detail = str(data_dir.resolve())
    ok &= _add_row(rows, "Data directory", writable, detail)

    try:
        from src.core.knowledge import KnowledgeBase

        KnowledgeBase()
        kb_ok = True
        kb_detail = "ChromaDB initialized"
    except Exception as exc:
        kb_ok = False
        kb_detail = f"{type(exc).__name__}: {exc}"
    ok &= _add_row(rows, "Knowledge DB", kb_ok, kb_detail)

    for tool, required in [("git", False), ("bandit", True), ("ruff", True), ("semgrep", False)]:
        found, detail = _tool_detail(tool)
        tool_ok = found or not required
        if not found and not required:
            detail = "optional; skipped when absent"
        ok &= _add_row(rows, f"Tool: {tool}", tool_ok, detail)

    console = Console()
    table = Table(title="Code Review Agent Doctor")
    table.add_column("Status", style="bold")
    table.add_column("Check")
    table.add_column("Detail")
    for status, name, detail in rows:
        style = "green" if status == "OK" else "red"
        table.add_row(f"[{style}]{status}[/{style}]", name, detail)
    console.print(table)

    if missing:
        console.print(f"[yellow]Missing packages:[/yellow] {', '.join(sorted(set(missing)))}")
    console.print("[green]Environment looks ready.[/green]" if ok else "[red]Environment needs attention.[/red]")
    return ok
