"""CLI for Case2 AC97 Scan Insertion baseline."""

import argparse, json, time
from test import graph


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", "-input", default="/case/input")
    parser.add_argument("--output", "-output", default="/case/output")
    parser.add_argument("--time-limit", type=float, default=150)
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=1,
        help="Reserved for future LLM repair; baseline runs once",
    )
    args = parser.parse_args()
    if args.time_limit <= 5:
        parser.error("time limit must exceed 5 seconds")
    state = graph.invoke(
        {
            "input_dir": args.input,
            "output_dir": args.output,
            "deadline": time.monotonic() + args.time_limit,
            "max_attempts": args.max_attempts,
            "history": [],
        },
        config={"recursion_limit": 30},
    )
    print(
        json.dumps(
            {
                "success": bool(state.get("validation")),
                "attempts": state.get("attempt", 0),
                "error": state.get("error", ""),
                "report": args.output + "/agent_report.json",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if state.get("validation") else 1


if __name__ == "__main__":
    raise SystemExit(main())
