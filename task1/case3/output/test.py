"""Task 1 V3: LLM-authored Dofile, EDA execution and bounded repair."""

from __future__ import annotations
import json, os, re, subprocess, time
from pathlib import Path
from typing import TypedDict
from langgraph.graph import StateGraph, END
from openai import OpenAI

ARTIFACTS = [
    "e902_aes_scan.v",
    "e902_aes_scan.ctl",
    "e902_aes_scan.def",
    "reports/e902_scan_cfg.rpt",
    "reports/e902_wrapper_cfg.rpt",
    "reports/e902_scan_signal.rpt",
    "reports/e902_scan_segment.rpt",
    "reports/e902_chain.rpt",
    "reports/e902_chaincell.rpt",
]


class State(TypedDict, total=False):
    input_dir: str
    output_dir: str
    deadline: float
    max_attempts: int
    attempt: int
    history: list[dict]
    error: str
    validation: bool
    dofile: str
    log_file: str
    returncode: int
    missing: list[str]
    diagnostic: str
    llm_calls: int


def ev(s, name, **kw):
    return {
        "history": [
            *s.get("history", []),
            {"node": name, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), **kw},
        ]
    }


def read(p, n=18000):
    return p.read_text(encoding="utf-8", errors="replace")[:n]


def initialize(s):
    inp = Path(s["input_dir"])
    out = Path(s["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    required = ["task_spec.md", "limitations.md"]
    missing = [x for x in required if not (inp / x).is_file()]
    missing += ["netlist/*.v"] if not list((inp / "netlist").glob("*.v")) else []
    missing += ["lib/*.lib"] if not list((inp / "lib").glob("*.lib")) else []
    if not os.getenv("LLM_API_KEY"):
        missing.append("LLM_API_KEY environment variable")
    return {
        "attempt": 0,
        "llm_calls": 0,
        "validation": False,
        "error": ("Missing: " + ", ".join(missing)) if missing else "",
        **ev(s, "initialize", missing=missing),
    }


def compile_config(inp, out, cfg):
    """Compile a constrained LLM plan; no arbitrary Tcl from model."""

    def atom(v):
        if not isinstance(v, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_./*\[\]-]*", v
        ):
            raise ValueError(f"Unsafe Tcl identifier: {v!r}")
        return v

    def number(v, low=0, high=100000):
        if type(v) is not int or not low <= v <= high:
            raise ValueError(f"Invalid integer: {v!r}")
        return v

    libs = sorted((inp / "lib").glob("*.lib"))
    nets = sorted((inp / "netlist").glob("*.v"))
    ctls = sorted((inp / "ctl").glob("*.ctl"))
    if len(nets) != 1:
        raise ValueError("Expected one netlist; add selection support for multiple")
    lines = [
        f"set out_res_dir {{{out}}}",
        f"set out_rpt_dir {{{out / 'reports'}}}",
        "file mkdir $out_rpt_dir",
        "load_lib [list " + " ".join("{" + str(x) + "}" for x in libs) + "]",
        f"load_netlist {{{nets[0]}}}",
    ]
    for entry in cfg.get("ctl_modules", []):
        module = atom(entry["module"])
        filename = entry["file"]
        matching = [x for x in ctls if x.name == filename]
        if len(matching) != 1:
            raise ValueError(f"CTL not found: {filename}")
        lines.append(f"load_ctl {{{matching[0]}}} -module {module}")
    lines.append("present_design " + atom(cfg["top_module"]))
    for c in cfg["clocks"]:
        lines.append(
            f"set_scan_signal -type clock -port {atom(c['port'])} -off_state {number(c.get('off_state', 0), 0, 1)}"
        )
    for c in cfg.get("resets", []):
        lines.append(
            f"set_scan_signal -type reset -port {atom(c['port'])} -off_state {number(c['off_state'], 0, 1)}"
        )
    for c in cfg["scan_enables"]:
        lines.append(
            f"set_scan_signal -type scan_enable -port {atom(c['port'])} -off_state {number(c['off_state'], 0, 1)}"
        )
    for rule in cfg.get("scan_elements", []):
        lines.append(
            f"set_scan_element {'true' if rule['enabled'] is True else 'false'} {atom(rule['path'])}"
        )
    scan = cfg["scan"]
    chain = number(scan["chain_count"], 1)
    maxlen = number(scan["max_length"], 1)
    mix = "true" if scan.get("mix_clocks") is True else "false"
    if cfg.get("wrapper"):
        w = cfg["wrapper"]
        lines.append("set_wrapper_cfg enable -style " + atom(w["style"]))
    lines.append(
        f"set_scan_cfg -chain_count {chain} -mix_clocks {mix} -max_length {maxlen}"
    )
    if cfg.get("wrapper"):
        lines.append(
            f"set_wrapper_cfg -chain_count {number(w['chain_count'], 1)} -max_length {number(w['max_length'], 1)}"
        )
    lines += ["examine_scan_drc -verbose", "examine_scan_chain", "insert_dft_logic"]
    reports = {
        "scan_cfg": "rpt_scan_cfg",
        "wrapper_cfg": "rpt_wrapper_cfg",
        "scan_signal": "rpt_scan_signal",
        "scan_segment": "rpt_scan_segment",
        "chain": "rpt_scan_chain",
        "chaincell": "rpt_scan_chain_cell",
    }
    for key, cmd in reports.items():
        lines.append(f'{cmd} -file "$out_rpt_dir/e902_{key}.rpt"')
    lines += [
        'dump_netlist -file "$out_res_dir/e902_aes_scan.v"',
        'dump_ctl "$out_res_dir/e902_aes_scan.ctl"',
        'dump_scan_def -file "$out_res_dir/e902_aes_scan.def"',
        "exit",
    ]
    return "\n".join(lines) + "\n"


def llm_json(s, stage, task, fields, context, extra=""):
    """One bounded model call, with persisted metadata even on empty content."""
    out = Path(s["output_dir"]).resolve()
    remaining = s["deadline"] - time.monotonic()
    if remaining < 24:
        raise TimeoutError(f"Insufficient budget before {stage}: {remaining:.1f}s")
    timeout = min(38, max(8, remaining - 18))
    client = OpenAI(
        api_key=os.environ["LLM_API_KEY"],
        base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
        timeout=timeout,
        max_retries=0,
    )
    # The design stage succeeded previously. Reuse its validated per-case output.
    # To force regeneration, delete config_design.json before executing main.py.
    if stage == "design":
        cached_file = out / "config_design.json"
        if cached_file.is_file():
            cached = json.loads(cached_file.read_text(encoding="utf-8"))
            if (isinstance(cached, dict) and cached.get("top_module")
                    and isinstance(cached.get("clocks"), list) and cached["clocks"]
                    and isinstance(cached.get("scan_enables"), list)
                    and cached["scan_enables"]):
                print("[LLM CACHE] stage=design reused config_design.json", flush=True)
                return cached, {"stage": "design", "cached": True}
    prompt = (
        f"TASK: {task}\nReturn ONE compact JSON object with exactly these fields: "
        + json.dumps(fields, ensure_ascii=False)
        + "\n"
        + "Extract only explicitly supported information. Do not invent ports, modules or paths. "
        + "Use [] for missing arrays, null for unknown scalar values. No explanations.\n"
        + "SOURCE TEXT:\n"
        + context
        + "\n"
        + extra
    )
    started = time.monotonic()
    (out / f"prompt_{stage}.txt").write_text(prompt, encoding="utf-8")
    print(
        f"[LLM START] stage={stage} task={task[:60]!r} "
        f"remaining={remaining:.1f}s timeout={timeout:.1f}s prompt_chars={len(prompt)}",
        flush=True,
    )

    try:
        response = client.chat.completions.create(
            model=(os.getenv("LLM_EXTRACT_MODEL", "deepseek-flash") if stage in ("scan", "blackbox") else os.getenv("LLM_MODEL", "deepseek-v4-pro")),
            messages=[
                {
                    "role": "system",
                    "content": "Extract EDA task parameters accurately. Respond with JSON only.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=2000,
        )
    except Exception as e:
        (out / f"llm_{stage}_error.txt").write_text(
            f"{type(e).__name__}: {e}\n", encoding="utf-8"
        )
        print(f"[LLM ERROR] stage={stage} {type(e).__name__}: {e}", flush=True)
        raise

    choice = response.choices[0]
    raw = choice.message.content or ""
    usage = response.usage
    details = getattr(usage, "completion_tokens_details", None)
    elapsed = round(time.monotonic() - started, 2)
    meta = {
        "stage": stage,
        "elapsed_seconds": elapsed,
        "finish_reason": choice.finish_reason,
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "reasoning_tokens": getattr(details, "reasoning_tokens", None),
        "content_chars": len(raw),
    }
    print("[LLM]", json.dumps(meta, ensure_ascii=False), flush=True)
    (out / f"llm_{stage}.txt").write_text(raw, encoding="utf-8")
    (out / f"llm_{stage}_usage.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    if not raw.strip():
        raise ValueError(f"{stage}: empty model content; {meta}")
    if choice.finish_reason == "length":
        raise ValueError(f"{stage}: truncated model output; {meta}")
    cleaned = re.sub(r"^\s*```(?:json)?\s*", "", raw, flags=re.I)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned).strip()
    cfg = json.loads(cleaned)
    if not isinstance(cfg, dict):
        raise ValueError(f"{stage}: expected JSON object")
    (out / f"config_{stage}.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return cfg, meta


def select_scan_context(spec):
    """Use only scan/wrapper requirements, not unrelated design or deliverables."""
    import re
    terms = re.compile(
        r"扫描链|扫描通道|链长度|链长|链数量|链数|扫描配置|混合时钟|跨时钟|"
        r"wrapper|unwrapper|scan.chain|chain.count|chain.length|max.length|mix.clock|"
        r"专用包装|包装链|包装扫描|包装器",
        re.I,
    )
    lines = spec.splitlines()
    selected = set()
    for i, line in enumerate(lines):
        if terms.search(line):
            selected.add(i)
            if i + 1 < len(lines) and not lines[i + 1].lstrip().startswith('#'):
                selected.add(i + 1)
    text = "\n".join(lines[i] for i in sorted(selected)).strip()
    return text[:2200] if text else spec[:2200]


def _required_int(text, name, patterns):
    """Extract an unambiguous integer, with no case-specific fallback."""
    values = set()
    for pattern in patterns:
        values.update(int(n) for n in re.findall(pattern, text, re.I))
    if len(values) != 1:
        raise ValueError(f"Cannot reliably extract {name}: candidates={sorted(values)}")
    return values.pop()


def extract_scan_from_spec(spec):
    """Turn explicit scan requirements into config, never guess missing values."""
    wrapper_count = _required_int(spec, 'wrapper chain count', [
        r'(?<!un)\bwrapper\s*chains?\b\s*(?:数目|数量|count)[^\n0-9]{0,18}(\d+)\s*(?:条|chains?)',
        r'(?:包装链|wrapper链)[^\n0-9]{0,25}(\d+)\s*条',
    ])
    unwrapper_count = _required_int(spec, 'unwrapper chain count', [
        r'\bunwrapper\s*chains?\b\s*(?:数目|数量|count)[^\n0-9]{0,18}(\d+)\s*(?:条|chains?)',
        r'(?:非包装链|unwrapper链)[^\n0-9]{0,25}(\d+)\s*条',
    ])
    max_length = _required_int(spec, 'maximum chain length', [
        r'最大链长度\s*(?:为|是|=|:|：)?\s*(\d+)',
        r'\bmax[_ -]?length\s*(?:为|=|:|：)?\s*(\d+)',
    ])
    style_match = re.search(r'(?i)wrapper[^\n]{0,85}\bdedicated\b', spec)
    if not style_match:
        raise ValueError('Cannot verify dedicated wrapper mode from task_spec.md')
    allows_mix = bool(re.search(
        r'时钟域不同[^\n]{0,100}允许混合|允许混合[^\n]{0,100}时钟域|'
        r'mix[_ -]?clocks\s*(?:=|:)?\s*true', spec, re.I))
    forbids_mix = bool(re.search(r'不允许混合[^\n]{0,80}时钟|mix[_ -]?clocks\s*(?:=|:)?\s*false', spec, re.I))
    if allows_mix == forbids_mix:
        raise ValueError('Missing or conflicting mix-clocks requirement')
    config = {
        'scan': {
            'chain_count': wrapper_count + unwrapper_count,
            'max_length': max_length,
            'mix_clocks': allows_mix,
        },
        'wrapper': {
            'style': 'dedicated',
            'chain_count': wrapper_count,
            'max_length': max_length,
        },
    }
    return config


def extract_blackbox_from_spec(spec, ctl_files):
    """Extract explicit CTL module association and scan-element overrides."""
    associations = []
    for filename in ctl_files:
        # The common naming convention is accepted only if explicitly
        # supported by text referencing this exact module identifier.
        module = Path(filename).stem
        if not re.search(r'(?<![\w])' + re.escape(module) + r'(?![\w])', spec):
            raise ValueError(f'CTL module {module!r} not stated in task specification')
        associations.append({'file': filename, 'module': module})

    restrictions = [line for line in spec.splitlines()
                    if 'Hinstance' in line or 'set_scan_element' in line]
    scan_elements = []
    for line in restrictions:
        # Task specifies hierarchical path and exception in code ticks.
        paths = re.findall(r'`([A-Za-z_][A-Za-z0-9_./*\[\]-]*)`', line)
        paths = [p for p in paths if '/' in p]
        if not paths:
            continue
        parents = [p for p in paths if not p.endswith('*')]
        exceptions = [p for p in paths if p.endswith('*')]
        for parent in parents:
            if any(e.startswith(parent + '/') for e in exceptions):
                scan_elements.append({'path': parent, 'enabled': False})
                scan_elements += [{'path': e, 'enabled': True}
                                  for e in exceptions if e.startswith(parent + '/')]

    if re.search(r'Hinstance[^\n]{0,300}(?:不允许|除)', spec, re.I) and not scan_elements:
        raise ValueError('Hinstance restriction found but scan-element paths not parsed')
    # Avoid emitting duplicates if the task describes constraints twice.
    unique = []
    for entry in scan_elements:
        if entry not in unique:
            unique.append(entry)
    return {'ctl_modules': associations, 'scan_elements': unique}


def assemble_config(s, extra=""):
    inp = Path(s['input_dir']).resolve()
    out = Path(s['output_dir']).resolve()
    spec = read(inp / 'task_spec.md', 18000)
    def section(words):
        lines = spec.splitlines()
        headings = [i for i, x in enumerate(lines) if x.lstrip().startswith('#')]
        blocks = []
        for j, i in enumerate(headings):
            end = headings[j + 1] if j + 1 < len(headings) else len(lines)
            block = '\n'.join(lines[i:end])
            if any(w.lower() in block.split('\n', 1)[0].lower() for w in words):
                blocks.append(block)
        return '\n'.join(blocks)

    manifest = {
        'lib': [p.name for p in sorted((inp / 'lib').glob('*.lib'))],
        'netlist': [p.name for p in sorted((inp / 'netlist').glob('*.v'))],
        'ctl': [p.name for p in sorted((inp / 'ctl').glob('*.ctl'))],
    }
    design_text = section(['基本信息', '时钟', 'DFT 控制', '任务要求', '设计名称', '设计']) or spec
    design, dm = llm_json(
        s, 'design',
        'Extract top module, clock ports, reset ports, scan enable ports.',
        {'top_module': 'string or null', 'clocks': [{'port': 'string', 'off_state': 0}],
         'resets': [{'port': 'string', 'off_state': 0}],
         'scan_enables': [{'port': 'string', 'off_state': 0}]},
        design_text + '\nAvailable files: ' + json.dumps(manifest, ensure_ascii=False),
    )

    scan = extract_scan_from_spec(spec)
    (out / 'config_scan.json').write_text(
        json.dumps(scan, ensure_ascii=False, indent=2), encoding='utf-8')
    sm = {'stage': 'scan', 'method': 'local_rules', 'reasoning_tokens': 0}
    print('[LOCAL EXTRACT] scan:', json.dumps(scan, ensure_ascii=False), flush=True)

    box = extract_blackbox_from_spec(spec, manifest['ctl'])
    (out / 'config_blackbox.json').write_text(
        json.dumps(box, ensure_ascii=False, indent=2), encoding='utf-8')
    bm = {'stage': 'blackbox', 'method': 'local_rules', 'reasoning_tokens': 0}
    print('[LOCAL EXTRACT] blackbox:', json.dumps(box, ensure_ascii=False), flush=True)

    cfg = {**design, **scan, **box}
    for key in ('clocks', 'scan_enables', 'ctl_modules', 'scan_elements'):
        if not isinstance(cfg.get(key), list):
            raise ValueError(f'Missing or invalid {key}')
    if not cfg.get('top_module') or not cfg['clocks'] or not cfg['scan_enables']:
        raise ValueError('Missing top module, clock, or scan enable')
    if not isinstance(cfg.get('scan'), dict):
        raise ValueError('Missing scan configuration')

    idx = s.get('attempt', 0) + 1
    (out / f'config_attempt_{idx}.json').write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    code = compile_config(inp, out, cfg)
    path = out / 'generated.dofile'
    path.write_text(code, encoding='utf-8')
    (out / f'generated_attempt_{idx}.dofile').write_text(code, encoding='utf-8')
    return {
        'dofile': str(path), 'error': '',
        'llm_calls': s.get('llm_calls', 0) + (0 if dm.get('cached') else 1),
        **ev(s, 'llm_generate', stages=[dm, sm, bm],
             config=str(out / f'config_attempt_{idx}.json')),
    }

def generate(s):
    try:
        return assemble_config(s)
    except Exception as e:
        return {
            "error": "LLM generation failed: " + str(e),
            **ev(s, "llm_generate", error=str(e)),
        }


def run_tool(s):
    out = Path(s["output_dir"])
    attempt = s.get("attempt", 0) + 1
    remaining = s["deadline"] - time.monotonic()
    if remaining < 8:
        return {
            "attempt": attempt,
            "returncode": 124,
            "error": "Insufficient tool budget",
            **ev(s, "run_tool", skipped=True),
        }
    log = out / f"scan_run_{attempt}.log"
    rc = 127
    # Remove previous outputs to avoid falsely validating stale artifacts.
    for rel in ARTIFACTS:
        p = out / rel
        if p.is_file():
            p.unlink()
    with log.open("w", encoding="utf-8") as f:
        try:
            cp = subprocess.run(
                ["dftexp_scan", "-f", s["dofile"]],
                cwd=out,
                stdin=subprocess.DEVNULL,
                stdout=f,
                stderr=subprocess.STDOUT,
                timeout=max(1, remaining - 6),
                check=False,
            )
            rc = cp.returncode
        except subprocess.TimeoutExpired:
            f.write("\n[AGENT] tool timed out\n")
            rc = 124
        except OSError as e:
            f.write("\n[AGENT] " + str(e) + "\n")
    return {
        "attempt": attempt,
        "returncode": rc,
        "log_file": str(log),
        "error": "" if rc == 0 else f"Tool returned {rc}",
        **ev(s, "run_tool", attempt=attempt, rc=rc),
    }


def validate(s):
    out = Path(s["output_dir"])
    missing = [
        x for x in ARTIFACTS if not (out / x).is_file() or (out / x).stat().st_size == 0
    ]
    log = Path(s.get("log_file", "/dev/null"))
    content = read(log, 1000000) if log.is_file() else ""
    errors = [
        x.strip()
        for x in content.splitlines()
        if re.search(r"\[(?:ERROR|FATAL)\]", x, re.I)
    ]
    ok = s.get("returncode") == 0 and not missing and not errors
    return {
        "validation": ok,
        "missing": missing,
        "error": ""
        if ok
        else f"rc={s.get('returncode')}, missing={missing}, errors={errors[-5:]}",
        **ev(s, "validate", ok=ok, missing=missing, errors=errors[-8:]),
    }


def diagnose(s):
    out = Path(s["output_dir"])
    log = Path(s.get("log_file", "/dev/null"))
    content = read(log, 1000000) if log.is_file() else ""
    codes = list(dict.fromkeys(re.findall(r"\b(?:SCAN|DFTDRC|CMD)-\d{4}\b", content)))
    root = Path("/opt/dftexp_scan/share/md/log")
    docs = {
        c: read(root / (c + ".md"), 1800)
        for c in codes[:8]
        if (root / (c + ".md")).is_file()
    }
    diagnosis = {
        "codes": codes,
        "docs": docs,
        "log_tail": content[-9000:],
        "error": s.get("error", ""),
    }
    path = out / f"diagnosis_{s.get('attempt', 0)}.json"
    path.write_text(json.dumps(diagnosis, ensure_ascii=False, indent=2))
    return {"diagnostic": str(path), **ev(s, "diagnose", codes=codes)}


def repair(s):
    """Repair by asking for a concise JSON patch, rather than re-running all three extraction stages."""
    try:
        out = Path(s["output_dir"])
        inp = Path(s["input_dir"]).resolve()
        idx = s.get("attempt", 0)
        cfg = json.loads(read(out / f"config_attempt_{idx}.json", 30000))
        diag = read(Path(s["diagnostic"]), 12000)
        patch, meta = llm_json(
            s,
            f"repair_{idx}",
            "Return only changed configuration fields needed to fix this DFTEXP failure. Preserve unrelated fields. Nested objects may be complete replacements.",
            {
                "patch": {
                    "scan": {"chain_count": 70, "max_length": 100, "mix_clocks": True}
                }
            },
            "CURRENT CONFIG:\n"
            + json.dumps(cfg, ensure_ascii=False)
            + "\nDIAGNOSTICS:\n"
            + diag,
        )
        changes = patch.get("patch")
        if not isinstance(changes, dict) or not changes:
            raise ValueError("Empty repair patch")
        for k, v in changes.items():
            if k not in cfg:
                raise ValueError(f"Unknown repair key: {k}")
            cfg[k] = v
        next_idx = idx + 1
        (out / f"config_attempt_{next_idx}.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        code = compile_config(inp, out, cfg)
        path = out / "generated.dofile"
        path.write_text(code, encoding="utf-8")
        (out / f"generated_attempt_{next_idx}.dofile").write_text(
            code, encoding="utf-8"
        )
        return {
            "dofile": str(path),
            "error": "",
            "llm_calls": s.get("llm_calls", 0) + 1,
            **ev(s, "llm_repair", changes=list(changes), usage=meta),
        }
    except Exception as e:
        return {
            "error": "LLM repair failed: " + str(e),
            **ev(s, "llm_repair", error=str(e)),
        }


def export(s):
    out = Path(s["output_dir"])
    report = {
        k: s.get(k)
        for k in [
            "validation",
            "attempt",
            "llm_calls",
            "error",
            "missing",
            "dofile",
            "log_file",
            "diagnostic",
            "history",
        ]
    }
    report["success"] = bool(s.get("validation"))
    report["note"] = (
        "File/exit-code checks only; official scan semantics and chain constraints require separate evaluation."
    )
    (out / "agent_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return ev(s, "export", success=report["success"])


def route_init(s):
    return "export" if s.get("error") else "generate"


def route_gen(s):
    return "export" if s.get("error") else "run_tool"


def route_valid(s):
    return "export" if s.get("validation") else "diagnose"


def route_diag(s):
    return (
        "repair"
        if s.get("attempt", 0) < s.get("max_attempts", 2)
        and s["deadline"] - time.monotonic() > 25
        else "export"
    )


def build_graph():
    g = StateGraph(State)
    for fn in [initialize, generate, run_tool, validate, diagnose, repair, export]:
        g.add_node(fn.__name__, fn)
    g.set_entry_point("initialize")
    g.add_conditional_edges(
        "initialize", route_init, {"export": "export", "generate": "generate"}
    )
    g.add_conditional_edges(
        "generate", route_gen, {"export": "export", "run_tool": "run_tool"}
    )
    g.add_edge("run_tool", "validate")
    g.add_conditional_edges(
        "validate", route_valid, {"export": "export", "diagnose": "diagnose"}
    )
    g.add_conditional_edges(
        "diagnose", route_diag, {"repair": "repair", "export": "export"}
    )
    g.add_conditional_edges(
        "repair", route_gen, {"export": "export", "run_tool": "run_tool"}
    )
    g.add_edge("export", END)
    return g.compile()


graph = build_graph()
