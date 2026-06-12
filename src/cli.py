import argparse
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-review-agent",
        description="自优化多智能体代码审查系统",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # review 子命令
    r = sub.add_parser("review", help="审查代码")
    r.add_argument("target", help="要审查的文件或目录路径")
    r.add_argument("--mode", choices=["single", "multi"], default="multi",
                   help="审查模式：single=单Agent, multi=多Agent辩论（默认）")
    r.add_argument("--output", "-o", help="输出报告路径（默认终端输出）")
    r.add_argument("--no-cache", action="store_true", help="跳过缓存，强制重新审查")
    r.add_argument("--stream", action="store_true", help="流式输出，实时显示审查进度")
    r.add_argument("--prefer", help="自然语言自定义审查偏好。例: '重点关注安全漏洞,忽略代码风格问题'")

    # benchmark 子命令
    b = sub.add_parser("benchmark", help="运行 Benchmark 对比测试")
    b.add_argument("--category", choices=["all", "security", "performance", "maintainability"],
                   default="all", help="测试类别（默认 all）")

    # feedback 子命令
    f = sub.add_parser("feedback", help="提交反馈以改进 Agent")
    f.add_argument("file", help="之前审查过的文件路径")
    f.add_argument("--confirm", help="确认某个发现正确（finding id）")
    f.add_argument("--reject", help="驳回某个发现（finding id）")
    f.add_argument("--note", help="附加备注")

    # stats 子命令
    sub.add_parser("stats", help="查看 Agent 准确率统计")

    # rules 子命令
    sub.add_parser("rules", help="交互式管理审查规则")

    # web 子命令
    sub.add_parser("web", help="启动 Web UI（浏览器访问 http://127.0.0.1:8000）")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "review":
        from src.core.reviewer import run_review
        run_review(args.target, mode=args.mode, output=args.output,
                   use_cache=not args.no_cache, stream=args.stream,
                   prefer=args.prefer)
    elif args.command == "benchmark":
        from src.benchmarks.runner import run_benchmark
        run_benchmark(args.category)
    elif args.command == "feedback":
        from src.core.knowledge import submit_feedback
        submit_feedback(args.file, confirm=args.confirm,
                        reject=args.reject, note=args.note)
    elif args.command == "stats":
        from src.core.knowledge import show_stats
        show_stats()
    elif args.command == "rules":
        from src.core.rules_config import interactive_rules
        interactive_rules()
    elif args.command == "web":
        from src.web import start
        start()


if __name__ == "__main__":
    main()
