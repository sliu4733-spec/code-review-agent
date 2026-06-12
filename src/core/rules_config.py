"""加载用户自定义审查规则配置"""
import yaml
from pathlib import Path

RULES_FILE = Path(__file__).parent.parent.parent / "config" / "rules.yaml"


def load_rules() -> dict:
    """加载规则配置，文件不存在则返回默认值"""
    if RULES_FILE.exists():
        with open(RULES_FILE, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def save_rules(rules: dict):
    """保存规则到文件"""
    RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RULES_FILE, "w", encoding="utf-8") as f:
        yaml.dump(rules, f, allow_unicode=True, default_flow_style=False)


def get_strictness() -> str:
    rules = load_rules()
    return rules.get("global", {}).get("strictness", "medium")


def get_confidence_threshold() -> float:
    rules = load_rules()
    return rules.get("global", {}).get("min_confidence", 0.4)


def get_enabled_rules(category: str) -> list[dict]:
    """获取某类别下所有启用的规则"""
    rules = load_rules()
    cat_rules = rules.get(category, {}).get("rules", [])
    return [r for r in cat_rules if r.get("enabled", True)]


def get_all_rules_flat() -> list[dict]:
    """获取所有规则（含启用/禁用状态），用于交互式管理"""
    rules = load_rules()
    result = []
    for cat in ["security", "performance", "maintainability"]:
        for r in rules.get(cat, {}).get("rules", []):
            result.append({
                "id": r.get("id"),
                "name": r.get("name"),
                "category": cat,
                "severity": r.get("severity", "medium"),
                "enabled": r.get("enabled", True),
            })
    return result


def toggle_rule(category: str, rule_id: str) -> dict | None:
    """切换规则启用状态，返回更新后的规则"""
    rules = load_rules()
    for r in rules.get(category, {}).get("rules", []):
        if r.get("id") == rule_id:
            r["enabled"] = not r.get("enabled", True)
            save_rules(rules)
            return r
    return None


def build_rules_prompt(category: str) -> str:
    """将启用规则转为 Agent prompt 片段"""
    enabled = get_enabled_rules(category)
    if not enabled:
        return "请检查代码中的常见问题。"

    lines = ["请重点检查以下问题类型："]
    for r in enabled:
        sev = r.get("severity", "medium")
        lines.append(f"- [{sev}] {r['name']}")
    return "\n".join(lines)


def interactive_rules():
    """CLI 交互式规则管理"""
    from rich.console import Console
    from rich.table import Table
    console = Console()

    all_rules = get_all_rules_flat()
    cat_names = {"security": "安全", "performance": "性能", "maintainability": "可维护"}

    while True:
        table = Table(title="审查规则管理 (输入序号切换启用/禁用, q=退出)")
        table.add_column("#")
        table.add_column("类别")
        table.add_column("规则名称")
        table.add_column("严重度")
        table.add_column("状态")

        for i, r in enumerate(all_rules):
            status = "[green]启用[/green]" if r["enabled"] else "[dim]禁用[/dim]"
            table.add_row(str(i + 1), cat_names.get(r["category"], r["category"]),
                         r["name"], r["severity"], status)

        console.print(table)
        console.print("\n[dim]输入序号切换状态, 输入 'all-on' 全部启用, 输入 'q' 退出:[/dim] ", end="")

        cmd = input().strip().lower()
        if cmd == "q":
            break
        elif cmd == "all-on":
            rules = load_rules()
            for cat in ["security", "performance", "maintainability"]:
                for r in rules.get(cat, {}).get("rules", []):
                    r["enabled"] = True
            save_rules(rules)
            all_rules = get_all_rules_flat()
            console.print("[green]已全部启用[/green]\n")
        elif cmd.isdigit():
            idx = int(cmd) - 1
            if 0 <= idx < len(all_rules):
                r = all_rules[idx]
                updated = toggle_rule(r["category"], r["id"])
                if updated:
                    all_rules = get_all_rules_flat()
                    state = "启用" if updated["enabled"] else "禁用"
                    console.print(f"[green]{r['name']} → {state}[/green]\n")
