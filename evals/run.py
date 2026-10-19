"""Run golden questions against the real LLM.

    python -m evals.run evals/retail_small.yaml

Each question has either `expected_sql` (reference answer computed independently, by hand-written SQL on the
same loaded data) or `expect: refuse` (the data cannot support it; refusing is the correct answer).
A question passes when every reference value appears in the actual result (so extra columns/rows and aliases are fine).
"""

import argparse
import sys
import tempfile
from pathlib import Path

import duckdb
import yaml

from app.config import get_settings
from app.engine import ask
from app.ingest import ingest_csv
from app.llm import get_llm
from app.profiler import jsonable


def flatten(rows) -> list:
    return [jsonable(v) for row in rows for v in row]


def contains_all(expected: list, actual: list, rel_tol: float = 0.01) -> bool:
    """Every expected value must appear in `actual` (numbers within relative tolerance, strings case-insensitive)."""
    pool = list(actual)
    for want in expected:
        for i, got in enumerate(pool):
            if _same(want, got, rel_tol):
                pool.pop(i)
                break
        else:
            return False
    return True


def _same(want, got, rel_tol: float) -> bool:
    if isinstance(want, (int, float)) and isinstance(got, (int, float)) and not isinstance(want, bool):
        return abs(want - got) <= max(0.01, abs(want) * rel_tol)
    return str(want).strip().lower() == str(got).strip().lower()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("suite")
    parser.add_argument("--only", help="substring filter on question text")
    args = parser.parse_args()

    suite = yaml.safe_load(Path(args.suite).read_text())
    csv_path = (Path(args.suite).parent / suite["csv"]).resolve()
    settings = get_settings()
    llm = get_llm(settings)

    work = Path(tempfile.mkdtemp(prefix="eval_"))
    profile = ingest_csv(csv_path, work)
    db = work / "data.duckdb"
    print(f"Loaded {csv_path.name}: {profile['row_count']} rows, {len(profile['columns'])} columns\n")

    ref = duckdb.connect(str(db), read_only=True)
    results = []
    for case in suite["questions"]:
        q = case["q"]
        if args.only and args.only.lower() not in q.lower():
            continue
        out = ask(q, db, profile, llm, settings)
        if case.get("expect") == "refuse":
            ok, note = out["status"] == "refused", f"{out['status']}"
        elif out["status"] != "succeeded":
            ok, note = False, f"refused: {out.get('reason', '')[:90]}"
        else:
            expected = flatten(ref.execute(case["expected_sql"]).fetchall())
            ok = contains_all(expected, flatten(out["rows"]), case.get("tolerance", 0.01))
            note = "match" if ok else f"MISMATCH\n      sql: {out['sql']}\n      got: {out['rows'][:3]}\n      want: {expected[:6]}"
        tokens = out["usage"]["input_tokens"] + out["usage"]["output_tokens"]
        results.append((ok, case.get("expect") == "refuse", tokens))
        print(f"[{'PASS' if ok else 'FAIL'}] {q}\n      {note}")

    passed = sum(r[0] for r in results)
    refuse_cases = [r for r in results if r[1]]
    print(f"\n{passed}/{len(results)} passed; correct refusals {sum(r[0] for r in refuse_cases)}/{len(refuse_cases)}; "
          f"tokens {sum(r[2] for r in results)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
