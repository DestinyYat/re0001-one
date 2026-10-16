"""Case2 AC97 LangGraph runner. No external LLM required for baseline generation."""

from __future__ import annotations
import json, os, re, subprocess, time
from pathlib import Path
from typing import TypedDict
from langgraph.graph import StateGraph, END

PARTITIONS = [
    ("rx_fifo_partition", "u9 u10 u11", 2, "se_rx_fifo"),
    ("tx_fifo_partition", "u3 u4 u5 u6 u7 u8", 4, "se_tx_fifo"),
    ("rx_serdes_partition", "u1", 2, "se_serdes"),
    ("tx_serdes_partition", "u0", 1, "se_serdes"),
    ("reg_partition", "u13", 1, "se_ctrl"),
    ("wb_partition", "u12", 1, "se_ctrl"),
    (
        "ctrl_partition",
        "u2 u14 u15 u16 u17 u18 u19 u20 u21 u22 u23 u24 u25 u26",
        2,
        "se_ctrl",
    ),
]
ARTIFACTS = [
    "post_scan.v",
    "ac97.ctl",
    "ac97.def",
    "reports/pre_drc.rpt",
    "reports/ac97_insertion.rpt",
    "reports/ac97_chain.rpt",
    "reports/ac97_scan_cfg.rpt",
    "reports/ac97_scan_signal.rpt",
    "reports/ac97_scan_partition.rpt",
]


class State(TypedDict, total=False):
    input_dir: str
    output_dir: str
    max_attempts: int
    attempt: int
    deadline: float
    error: str
    validation: bool
    history: list[dict]
    dofile: str
    log_file: str
    returncode: int
    diagnostic: str
    missing: list[str]
    tool_seconds: float


def event(s: State, name: str, **kw) -> dict:
    return {
        "history": [
            *s.get("history", []),
            {"node": name, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), **kw},
        ]
    }


def initialize(s: State) -> dict:
    inp = Path(s["input_dir"])
    out = Path(s["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    needed = ["task_spec.md", "limitations.md", "lib/sky130.lib", "netlist/ac97_ctrl.v"]
    missing = [f for f in needed if not (inp / f).is_file()]
    err = ("Missing inputs: " + ", ".join(missing)) if missing else ""
    # Baseline is specifically for this published AC97 case; do not pretend generality.
    if not err and (
        "ac97_top" not in (inp / "task_spec.md").read_text(encoding="utf-8")
    ):
        err = "Expected AC97 case specification not found"
    return {
        "attempt": 0,
        "error": err,
        "validation": False,
        **event(s, "initialize", ok=not bool(err), missing=missing),
    }


def generate(s: State) -> dict:
    inp = Path(s["input_dir"]).resolve()
    out = Path(s["output_dir"]).resolve()
    # Explicit absolute paths: the script resides in output, not input.
    lines = [
        f"set out_res_dir {{{out}}}",
        f"set out_rpt_dir {{{out / 'reports'}}}",
        "file mkdir $out_rpt_dir",
        f"load_lib {{{inp / 'lib/sky130.lib'}}}",
        f"load_netlist {{{inp / 'netlist/ac97_ctrl.v'}}}",
        "present_design ac97_top",
        "set_scan_signal -type clock -port clk_i -off_state 0",
        "set_scan_signal -type clock -port bit_clk_pad_i -off_state 0",
        "set_scan_signal -type reset -port rst_i -off_state 1",
        'set_scan_cfg -add_lockup true -replace true -mix_edges false -si_port_format "scan_data_in_%d" -so_port_format "scan_data_out_%d"',
    ]
    for name, inst, _, _ in PARTITIONS:
        lines.append(f"add_scan_partition {name} -include {{{inst}}}")
    for name, _, count, enable in PARTITIONS:
        lines.extend(
            [
                f"set_current_scan_partition {name}",
                f"set_scan_cfg -chain_count {count} -max_length 300 -mix_clocks false",
                f"set_scan_signal -type scan_enable -port {enable}",
            ]
        )
    lines += [
        "examine_scan_drc -verbose",
        'rpt_scan_drc_violation > "$out_rpt_dir/pre_drc.rpt"',
        "examine_scan_chain",
        "insert_dft_logic -verbose",
        'rpt_insertion_info -file "$out_rpt_dir/ac97_insertion.rpt"',
        'rpt_scan_chain -file "$out_rpt_dir/ac97_chain.rpt"',
        'rpt_scan_cfg -file "$out_rpt_dir/ac97_scan_cfg.rpt"',
        'rpt_scan_signal -file "$out_rpt_dir/ac97_scan_signal.rpt"',
        'rpt_scan_partition -file "$out_rpt_dir/ac97_scan_partition.rpt"',
        'dump_netlist -file "$out_res_dir/post_scan.v"',
        'dump_ctl "$out_res_dir/ac97.ctl"',
        'dump_scan_def -file "$out_res_dir/ac97.def"',
        "exit",
    ]
    path = out / "generated.dofile"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "dofile": str(path),
        "error": "",
        **event(s, "generate", dofile=str(path), partitions=len(PARTITIONS)),
    }


def run_tool(s: State) -> dict:
    out = Path(s["output_dir"])
    attempt = s.get("attempt", 0) + 1
    remaining = s["deadline"] - time.monotonic()
    if remaining <= 5:
        return {
            "attempt": attempt,
            "returncode": 124,
            "error": "Global time budget exhausted",
            **event(s, "run_tool", attempt=attempt, skipped=True),
        }
    log = out / f"scan_run_{attempt}.log"
    started = time.monotonic()
    rc = 127
    # Discard stdin to ensure unattended execution; full stdout+stderr is persisted.
    with log.open("w", encoding="utf-8", errors="replace") as stream:
        try:
            result = subprocess.run(
                ["dftexp_scan", "-f", s["dofile"]],
                cwd=out,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=max(1, remaining - 5),
                check=False,
            )
            rc = result.returncode
        except subprocess.TimeoutExpired:
            rc = 124
            stream.write("\n[AGENT] Tool timed out\n")
        except OSError as exc:
            stream.write(f"\n[AGENT] Failed to launch tool: {exc}\n")
    return {
        "attempt": attempt,
        "returncode": rc,
        "log_file": str(log),
        "tool_seconds": time.monotonic() - started,
        "error": "" if rc == 0 else f"dftexp_scan returned {rc}",
        **event(s, "run_tool", attempt=attempt, returncode=rc, log=str(log)),
    }


def validate(s: State) -> dict:
    out = Path(s["output_dir"])
    missing = [
        x for x in ARTIFACTS if not (out / x).is_file() or (out / x).stat().st_size == 0
    ]
    log = Path(s.get("log_file", ""))
    content = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    # Some EDA tools return 0 after printing errors; never treat that as success.
    errors = [
        line.strip()
        for line in content.splitlines()
        if re.search(r"\[(?:ERROR|FATAL)\]", line, re.I)
    ]
    ok = s.get("returncode") == 0 and not missing and not errors
    error = (
        ""
        if ok
        else f"Validation failed: missing={missing}; error_lines={len(errors)}; rc={s.get('returncode')}"
    )
    return {
        "validation": ok,
        "missing": missing,
        "error": error,
        **event(s, "validate", ok=ok, missing=missing, errors=errors[-10:]),
    }


def diagnose(s: State) -> dict:
    log = Path(s.get("log_file", ""))
    content = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    codes = list(dict.fromkeys(re.findall(r"\b(?:SCAN|DFTDRC|CMD)-\d{4}\b", content)))
    root = Path(os.environ.get("SCAN_DOC_DIR", "/opt/dftexp_scan/share/md/log"))
    docs = []
    for code in codes[:12]:
        p = root / (code + ".md")
        if p.is_file():
            docs.append(
                {
                    "code": code,
                    "excerpt": p.read_text(encoding="utf-8", errors="replace")[:2500],
                }
            )
    diagnostic = json.dumps(
        {"codes": codes, "docs": docs, "log_tail": content[-5000:]},
        ensure_ascii=False,
        indent=2,
    )
    path = Path(s["output_dir"]) / f"diagnosis_{s.get('attempt', 0)}.json"
    path.write_text(diagnostic, encoding="utf-8")
    return {
        "diagnostic": str(path),
        **event(s, "diagnose", codes=codes, diagnosis=str(path)),
    }


def export(s: State) -> dict:
    out = Path(s["output_dir"])
    report = {
        "success": bool(s.get("validation")),
        "attempts": s.get("attempt", 0),
        "error": s.get("error", ""),
        "missing": s.get("missing", []),
        "dofile": s.get("dofile"),
        "log_file": s.get("log_file"),
        "diagnostic": s.get("diagnostic"),
        "history": s.get("history", []),
        "note": "Artifact checks are preliminary; DRC/chain semantic acceptance requires official evaluation.",
    }
    (out / "agent_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return event(s, "export", success=report["success"])


def route_init(s: State) -> str:
    return "export" if s.get("error") else "generate"


def route_after(s: State) -> str:
    return "export" if s.get("validation") else "diagnose"


def route_diagnose(s: State) -> str:
    # Repeating the same Dofile cannot fix a deterministic DFT error.
    # Explicitly stop until a genuine repair node is implemented.
    return "export"


def build_graph():
    g = StateGraph(State)
    for fn in [initialize, generate, run_tool, validate, diagnose, export]:
        g.add_node(fn.__name__, fn)
    g.set_entry_point("initialize")
    g.add_conditional_edges(
        "initialize", route_init, {"generate": "generate", "export": "export"}
    )
    g.add_edge("generate", "run_tool")
    g.add_edge("run_tool", "validate")
    g.add_conditional_edges(
        "validate", route_after, {"export": "export", "diagnose": "diagnose"}
    )
    g.add_conditional_edges("diagnose", route_diagnose, {"export": "export"})
    g.add_edge("export", END)
    return g.compile()


graph = build_graph()
