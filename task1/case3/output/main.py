"""Task 1 V3 entrypoint."""

import argparse, json, time
from pathlib import Path
from test import graph


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", "-input", default="/case/input")
    p.add_argument("--output", "-output", default="/case/output")
    p.add_argument("--time-limit", type=float, default=150)
    p.add_argument("--max-attempts", type=int, default=2)
    a = p.parse_args()
    if a.time_limit <= 10 or a.max_attempts < 1:
        p.error("invalid time limit or attempts")
    out = Path(a.output)
    out.mkdir(parents=True, exist_ok=True)
    try:
        s = graph.invoke(
            {
                "input_dir": a.input,
                "output_dir": a.output,
                "deadline": time.monotonic() + a.time_limit,
                "max_attempts": a.max_attempts,
                "history": [],
            },
            config={"recursion_limit": 25},
        )
        ok = bool(s.get("validation"))
        print(
            json.dumps(
                {
                    "success": ok,
                    "attempts": s.get("attempt", 0),
                    "error": s.get("error", ""),
                    "report": str(out / "agent_report.json"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if ok else 1
    except Exception as e:
        (out / "agent_report.json").write_text(
            json.dumps(
                {"success": False, "error": repr(e)}, ensure_ascii=False, indent=2
            )
        )
        print(f"Agent exception: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
