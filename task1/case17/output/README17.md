# Case2 AC97 LangGraph agent v2

This is a **case-specific deterministic baseline**, not a general natural-language Dofile generator or LLM repair agent. It does not read `golden.dofile`; the seven AC97 partitions and scan configuration are encoded from the provided task specification. Verify competition rules before using a golden reference in development.

## Environment
Python 3.12, `langgraph`, `dftexp_scan` in PATH, and `/case/input` with task_spec.md, limitations.md, lib/sky130.lib, netlist/ac97_ctrl.v.

## Run
From the directory containing main.py and test.py, with your Python virtual environment active:

```bash
python -m py_compile main.py test.py
python main.py --input /case/input --output /case/output --time-limit 150
```

It generates `generated.dofile`, runs `dftexp_scan -f` noninteractively, stores the full log in `scan_run_1.log`, extracts documented error codes into `diagnosis_1.json` if needed, and writes `agent_report.json`.

**Caveats:** no automatic Dofile repair or LLM call; no semantic DRC/chain-count evaluation; global time budget is applied to the tool call but not a hard watchdog for Python filesystem operations. `--max-attempts` is reserved and currently does not trigger retries. Do not run simultaneously with another agent in the same output directory. Existing named artifacts may be overwritten by the tool. Use a fresh output directory when possible.
