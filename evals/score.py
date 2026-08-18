"""Turn a raw bake-off run into the comparison table.

    .venv/bin/python -m evals.score                      # newest run
    .venv/bin/python -m evals.score evals/results/raw_*.json

Reports rates, not averages of averages, and prints a per-case grid showing how
many reps each model passed. The grid is the point: a model that passes a case
2 times in 3 is a different proposition from one that passes it 3 in 3, and an
aggregate percentage hides exactly that. Run-to-run variance is this repo's
recorded failure mode (docs/model-constraints.md), so it gets its own view.
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"

CHAT_CHECKS = [
    ("called_expected", "right tool called"),
    ("args_ok", "arguments right"),
    ("final_ok", "answer used the result"),
    ("no_fabrication_ok", "nothing fabricated"),
    ("no_tool_expected_ok", "no tool when none needed"),
    ("no_repeat_confirm_ok", "no repeated confirmation"),
]

TASK_CHECKS = [
    ("non_empty", "answer not empty"),
    ("parsed_ok", "output parsed"),
    ("complete", "all items returned"),
]


def latest_raw() -> Path:
    files = sorted(RESULTS_DIR.glob("raw_*.json"))
    if not files:
        sys.exit(f"No raw results in {RESULTS_DIR}. Run evals.run_eval first.")
    return files[-1]


def rate(records: list[dict], key: str) -> tuple[int, int]:
    """(passed, applicable). A check that's None for a case doesn't count
    against a model — it means the case never asked."""
    vals = [r["score"].get(key) for r in records]
    applicable = [v for v in vals if v is not None]
    return sum(1 for v in applicable if v), len(applicable)


def _pct(passed: int, total: int) -> str:
    return "  —  " if not total else f"{100 * passed / total:3.0f}% ({passed}/{total})"


def summarize(records: list[dict]) -> dict:
    """{model: {path: {...}}} of rates and timings."""
    out: dict = {}
    for model in dict.fromkeys(r["model"] for r in records):
        out[model] = {}
        for path, checks in (("chat", CHAT_CHECKS), ("tasks", TASK_CHECKS)):
            rows = [r for r in records if r["model"] == model and r["path"] == path]
            if not rows:
                continue
            times = [r["elapsed_s"] for r in rows]
            out[model][path] = {
                "runs": len(rows),
                "checks": {key: rate(rows, key) for key, _ in checks},
                "errors": sum(1 for r in rows if r.get("outcome") in ("error", "unavailable")),
                "median_s": round(statistics.median(times), 1),
                "max_s": round(max(times), 1),
                "eval_tokens": sum(r.get("eval_tokens") or 0 for r in rows),
                "warned": sum(1 for r in rows if r.get("loop_warnings")),
            }
    return out


def print_summary(summary: dict) -> None:
    models = list(summary)
    width = max((len(m) for m in models), default=10)
    for path, checks in (("chat", CHAT_CHECKS), ("tasks", TASK_CHECKS)):
        rows = {m: s[path] for m, s in summary.items() if path in s}
        if not rows:
            continue
        title = "CHAT — tool calling" if path == "chat" else "SCHEDULED TASKS — templates"
        print(f"\n{title}")
        header = " " * (width + 2) + "".join(f"{label:>26}" for _, label in checks)
        print(header)
        for model, s in rows.items():
            cells = "".join(f"{_pct(*s['checks'][key]):>26}" for key, _ in checks)
            print(f"{model:<{width}}  {cells}")
        print()
        print(" " * (width + 2) + f"{'runs':>8}{'median':>10}{'slowest':>10}"
              f"{'errors':>9}{'loop warnings':>16}")
        for model, s in rows.items():
            print(f"{model:<{width}}  {s['runs']:>8}{s['median_s']:>9}s"
                  f"{s['max_s']:>9}s{s['errors']:>9}{s['warned']:>16}")


def print_grid(records: list[dict]) -> None:
    """Per-case reps passed, e.g. 3/3 or 1/3. A case is 'passed' when no check
    it declared came back False."""
    models = list(dict.fromkeys(r["model"] for r in records))
    cases = list(dict.fromkeys(r["case"] for r in records))
    width = max(len(c) for c in cases)
    print("\nPER-CASE — reps passed")
    print(" " * (width + 2) + "".join(f"{m:>20}" for m in models))
    for case in cases:
        cells = []
        for model in models:
            rows = [r for r in records if r["case"] == case and r["model"] == model]
            passed = sum(1 for r in rows
                         if not any(v is False for v in r["score"].values()))
            cells.append(f"{passed}/{len(rows)}" if rows else "—")
        flag = "  <-- inconsistent" if any(
            0 < int(c.split("/")[0]) < int(c.split("/")[1]) for c in cells if "/" in c
        ) else ""
        print(f"{case:<{width}}  " + "".join(f"{c:>20}" for c in cells) + flag)


def print_failures(records: list[dict], limit: int) -> None:
    """The raw failures, because a rate can't say whether a loss is fixable by
    a prompt tweak or is the model's shape."""
    bad = [r for r in records if any(v is False for v in r["score"].values())
           or r.get("outcome") in ("error", "unavailable")]
    if not bad:
        print("\nNo failures.")
        return
    print(f"\nFAILURES ({len(bad)}; showing up to {limit})")
    for r in bad[:limit]:
        failed = [k for k, v in r["score"].items() if v is False]
        print(f"\n  {r['model']} / {r['case']} rep{r['rep']} — {', '.join(failed) or r.get('outcome')}")
        if r.get("error"):
            print(f"    error: {r['error']}")
        if r["path"] == "chat":
            print(f"    called: {r['score'].get('tools_called') or '(nothing)'}")
            if r["score"].get("args_failed_keys"):
                print(f"    bad args: {r['score']['args_failed_keys']}")
            if r["score"].get("fabricated"):
                print(f"    fabricated: {r['score']['fabricated']}")
            if r.get("final"):
                print(f"    answer: {r['final'][:300]}")
        else:
            print(f"    got {r['score'].get('result_count')} of "
                  f"{r['score'].get('expect_count')} in {r['score']['raw_chars']} chars")
            if r["score"].get("parse_error"):
                print(f"    parse: {r['score']['parse_error'][:200]}")
            if r.get("raw"):
                print(f"    raw: {r['raw'][:300]}")
        for warning in r.get("loop_warnings") or []:
            print(f"    loop: {warning[:200]}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", nargs="?", type=Path, help="Raw JSON. Default: newest.")
    parser.add_argument("--failures", type=int, default=25,
                        help="How many failures to print in full.")
    args = parser.parse_args(argv)

    path = args.raw or latest_raw()
    records = json.loads(path.read_text())
    print(f"{path}  —  {len(records)} run(s)")
    print_summary(summarize(records))
    print_grid(records)
    print_failures(records, args.failures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
