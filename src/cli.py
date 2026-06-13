import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-review-agent",
        description="自优化多智能体代码审查系统",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # review 子命令
    r = sub.add_parser("review", help="审查代码")
    r.add_argument("target", help="要审查的文件或目录路径")
    r.add_argument("--mode", choices=["single", "multi", "adaptive"], default="adaptive",
                   help="审查模式：single=单Agent, multi=全量多Agent, adaptive=自适应协作（默认）")
    r.add_argument("--output", "-o", help="输出报告路径（默认生成 review_<目标名>.md）")
    r.add_argument("--no-cache", action="store_true", help="跳过缓存，强制重新审查")
    r.add_argument("--stream", action="store_true", help="流式输出，实时显示审查进度")
    r.add_argument("--prefer", help="自然语言自定义审查偏好。例: '重点关注安全漏洞,忽略代码风格问题'")
    r.add_argument("--feedback", action="store_true", help="审查结束后进入确认/驳回反馈引导")
    r.add_argument("--no-feedback", action="store_true", help="跳过交互式反馈引导")

    # benchmark 子命令
    b = sub.add_parser("benchmark", help="运行 Benchmark 对比测试")
    b.add_argument("--category", choices=["all", "security", "performance", "maintainability", "mixed"],
                   default="all", help="测试类别（默认 all）")
    b.add_argument("--runs", type=int, default=1,
                   help="重复运行 benchmark 的次数，用于计算平均值和标准差（默认 1）")
    b.add_argument("--report-dir", default="reports/benchmarks",
                   help="benchmark 报告保存目录（默认 reports/benchmarks）")
    b.add_argument("--no-save", action="store_true",
                   help="只在终端显示 benchmark，不保存 .md/.json 报告")

    # feedback 子命令
    f = sub.add_parser("feedback", help="提交反馈以改进 Agent")
    f.add_argument("file", help="之前审查过的文件路径")
    f.add_argument("--confirm", help="确认某个发现正确（finding id）")
    f.add_argument("--reject", help="驳回某个发现（finding id）")
    f.add_argument("--note", help="附加备注")

    # stats 子命令
    sub.add_parser("stats", help="查看 Agent 准确率统计")

    # doctor 子命令
    sub.add_parser("doctor", help="检查运行环境、依赖、API Key 和本地工具")

    # seed knowledge 子命令
    sub.add_parser("seed-knowledge", help="初始化知识库中的预置审查规则")

    # rules 子命令
    sub.add_parser("rules", help="交互式管理审查规则")

    # web 子命令
    sub.add_parser("web", help="启动 Web UI（浏览器访问 http://127.0.0.1:8000）")

    # demo 子命令
    d = sub.add_parser("demo", help="一键运行环境检查、知识库初始化和 benchmark 演示")
    d.add_argument("--category", default="mixed",
                   choices=["all", "security", "performance", "maintainability", "mixed"],
                   help="Demo benchmark 类别（默认 mixed）")
    d.add_argument("--runs", type=int, default=3,
                   help="Demo benchmark 重复次数（默认 3）")
    d.add_argument("--review-target",
                   help="可选：额外生成某个文件/目录的 adaptive 审查报告，例如 src")
    d.add_argument("--report-dir", default="reports/demo",
                   help="Demo benchmark 报告保存目录（默认 reports/demo）")

    # consensus 子命令
    c = sub.add_parser("consensus", help="对多次审查报告做稳定性/共识分析")
    c.add_argument("reports", nargs="+", help="多份 Markdown 审查报告路径")
    c.add_argument("--output", "-o", default="reports/consensus.md",
                   help="共识分析 Markdown 输出路径（默认 reports/consensus.md）")
    c.add_argument("--json-output", help="可选：同时保存 JSON 结果")
    c.add_argument("--min-support", type=int, default=2,
                   help="进入 probable 的最低重复次数（默认 2）")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "review":
        from src.core.reviewer import run_review
        interactive_feedback = False if args.no_feedback else True if args.feedback else None
        run_review(args.target, mode=args.mode, output=args.output,
                   use_cache=not args.no_cache, stream=args.stream,
                   prefer=args.prefer, interactive_feedback=interactive_feedback)
    elif args.command == "benchmark":
        from src.benchmarks.runner import run_benchmark
        run_benchmark(
            args.category,
            runs=args.runs,
            save_report=not args.no_save,
            report_dir=args.report_dir,
        )
    elif args.command == "feedback":
        from src.core.knowledge import submit_feedback
        submit_feedback(args.file, confirm=args.confirm,
                        reject=args.reject, note=args.note)
    elif args.command == "stats":
        from src.core.knowledge import show_stats
        show_stats()
    elif args.command == "doctor":
        from src.core.doctor import run_doctor
        sys.exit(0 if run_doctor() else 1)
    elif args.command == "seed-knowledge":
        from src.core.knowledge import seed_knowledge
        seed_knowledge()
    elif args.command == "rules":
        from src.core.rules_config import interactive_rules
        interactive_rules()
    elif args.command == "web":
        from src.web import start
        start()
    elif args.command == "demo":
        from src.core.demo import run_demo
        run_demo(
            category=args.category,
            runs=args.runs,
            review_target=args.review_target,
            report_dir=args.report_dir,
        )
    elif args.command == "consensus":
        from src.core.consensus import run_consensus
        run_consensus(
            args.reports,
            output=args.output,
            json_output=args.json_output,
            min_support=args.min_support,
        )


if __name__ == "__main__":
    main()
