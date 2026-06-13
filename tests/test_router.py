from src.core.router import build_review_plan


def test_security_signal_routes_security_agent():
    code = """
def get_user(cursor, name):
    return cursor.execute("SELECT * FROM users WHERE name = '%s'" % name)
"""
    plan = build_review_plan(code, "app.py")
    assert "security" in plan.agents
    assert plan.scores["security"] >= 0.55
    assert not plan.fallback_to_single


def test_mixed_signals_route_multiple_agents():
    code = """
async function load(ids) {
  for (const id of ids) {
    document.body.innerHTML = await fetch('/user/' + id)
  }
}
"""
    plan = build_review_plan(code, "app.ts")
    assert "security" in plan.agents
    assert "performance" in plan.agents
    assert plan.scores["security"] >= 0.55
    assert plan.scores["performance"] >= 0.55


def test_simple_code_falls_back_to_single():
    code = """
def add(a, b):
    return a + b
"""
    plan = build_review_plan(code, "math.py")
    assert plan.fallback_to_single
    assert plan.agents == []


def test_weak_signal_does_not_trigger_specialist():
    code = """
def read_config(path):
    return open(path).read()
"""
    plan = build_review_plan(code, "config.py")
    assert "security" not in plan.agents
    assert plan.scores["security"] < 0.55
