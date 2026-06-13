from src.benchmarks.runner import _aggregate_runs


def test_aggregate_runs_returns_mean_and_std():
    runs = [
        {
            "single": {"recall": 0.5, "precision": 0.8, "f1": 0.6},
            "multi": {"recall": 0.7, "precision": 0.6, "f1": 0.65},
            "adaptive": {"recall": 0.6, "precision": 0.9, "f1": 0.72},
        },
        {
            "single": {"recall": 0.7, "precision": 0.6, "f1": 0.64},
            "multi": {"recall": 0.9, "precision": 0.8, "f1": 0.85},
            "adaptive": {"recall": 0.8, "precision": 0.7, "f1": 0.75},
        },
    ]

    summary = _aggregate_runs(runs)

    assert summary["single"]["recall"]["mean"] == 0.6
    assert round(summary["single"]["recall"]["std"], 2) == 0.1
    assert summary["adaptive"]["precision"]["mean"] == 0.8
