"""One-command demo workflow for interviews and reproducible showcases."""

from __future__ import annotations

from rich.console import Console


def run_demo(category: str = "mixed", runs: int = 3,
             review_target: str | None = None,
             report_dir: str = "reports/demo") -> None:
    console = Console()

    console.rule("[bold cyan]Demo step 1/4: environment doctor[/bold cyan]")
    from src.core.doctor import run_doctor

    ready = run_doctor()
    if not ready:
        console.print("[yellow]Doctor reported issues. Demo continues, but LLM calls may fail.[/yellow]")

    console.rule("[bold cyan]Demo step 2/4: seed knowledge[/bold cyan]")
    from src.core.knowledge import seed_knowledge

    seed_knowledge()

    console.rule("[bold cyan]Demo step 3/4: benchmark[/bold cyan]")
    from src.benchmarks.runner import run_benchmark

    run_benchmark(category=category, runs=runs, save_report=True, report_dir=report_dir)

    console.rule("[bold cyan]Demo step 4/4: review report[/bold cyan]")
    if review_target:
        from src.core.reviewer import run_review

        run_review(
            review_target,
            mode="adaptive",
            output=None,
            use_cache=False,
            stream=False,
            interactive_feedback=False,
        )
    else:
        console.print("[dim]Skipped full review. Pass --review-target src to generate a review report.[/dim]")

    console.print("[green]Demo workflow complete.[/green]")
