"""
ATIP Phase 1 baseline extractor (read-only).

Facts only, taken from the code and the database rather than from a description
of them: module inventory, DB schema with row counts and date spans, API routes,
scheduled jobs, implemented scores/indicators, engine constants, config keys and
tests. Writes JSON + Markdown so later phases can be diffed against it.

    python baseline.py <repo_root> <out_dir>
"""
import ast, io, json, re, sqlite3, subprocess, sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "docs" / "baseline"
OUT.mkdir(parents=True, exist_ok=True)

SKIP = {"__pycache__", ".git", "atip_data", ".pytest_cache", ".github"}


def py_files():
    return [p for p in sorted(ROOT.rglob("*.py"))
            if not any(part in SKIP for part in p.parts)]


def rel(p):
    return str(p.relative_to(ROOT)).replace("\\", "/")


def read(p):
    return io.open(p, encoding="utf-8", errors="replace").read()


# -- modules, functions, classes -------------------------------------------
ALL_PY = py_files()
modules = []
for p in ALL_PY:
    src = read(p)
    entry = {"file": rel(p), "lines": src.count("\n") + 1}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        entry["doc"] = "(unparsed)"
        modules.append(entry)
        continue
    entry["doc"] = (ast.get_docstring(tree) or "").strip().split("\n")[0][:150]
    entry["functions"] = [n.name for n in tree.body
                          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    entry["classes"] = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    modules.append(entry)

# -- database --------------------------------------------------------------
db_path = ROOT / "atip_data" / "atip.db"
tables = []
if db_path.exists():
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    names = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for t in names:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            n = None
        span = None
        for c in ("date", "signal_date", "run_date", "timestamp", "created_at"):
            if c in cols:
                try:
                    lo, hi = con.execute(f"SELECT MIN({c}), MAX({c}) FROM {t}").fetchone()
                    if lo is not None:
                        span = {"column": c, "min": str(lo)[:19], "max": str(hi)[:19]}
                except Exception:
                    pass
                break
        tables.append({"table": t, "rows": n, "n_columns": len(cols),
                       "columns": cols, "span": span})
    con.close()


# -- greps ----------------------------------------------------------------
def grep(pattern, group=1):
    out, rx = [], re.compile(pattern)
    for p in ALL_PY:
        for i, line in enumerate(read(p).splitlines(), 1):
            m = rx.search(line)
            if m:
                out.append({"file": rel(p), "line": i, "match": m.group(group).strip()})
    return out


routes = grep(r'@app\.(?:get|post|put|delete)\(\s*[\'"]([^\'"]+)')
jobs = grep(r'(schedule\.every\(\)[^#]*)')
job_fns = sorted({m["match"] for m in grep(r'schedule\.every\(\)[^#]*?\.do\((\w+)')})
cfg_keys = sorted({m["match"] for m in grep(r'cfg\.get\(\s*[\'"](\w+)[\'"]')})

engine = ROOT / "scores" / "engine.py"
score_fns, engine_consts = [], []
if engine.exists():
    src = read(engine)
    score_fns = re.findall(r'^def (\w+)', src, re.M)
    engine_consts = re.findall(r'^([A-Z][A-Z0-9_]{2,})\s*=\s*(.+)$', src, re.M)

tech = ROOT / "data" / "technical.py"
indicator_cols = []
if tech.exists():
    src = read(tech)
    # the columns technical_indicators is actually written with
    m = re.search(r'INSERT (?:OR REPLACE )?INTO technical_indicators\s*\(([^)]+)\)', src, re.I)
    if m:
        indicator_cols = [c.strip() for c in m.group(1).split(",")]
    else:
        indicator_cols = sorted(set(re.findall(r'df\[[\'"](\w+)[\'"]\]\s*=', src)))

tests = []
tdir = ROOT / "tests"
if tdir.exists():
    for p in sorted(tdir.glob("test_*.py")):
        src = read(p)
        tests.append({"file": rel(p),
                      "tests": len(re.findall(r'^def test_', src, re.M)),
                      "parametrized": len(re.findall(r'@pytest\.mark\.parametrize', src))})


def sh(*cmd):
    try:
        return subprocess.run(list(cmd), cwd=str(ROOT), capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except Exception as e:
        return f"({e})"


deps = {}
for name in ("requirements.txt", "pyproject.toml", "pytest.ini"):
    f = ROOT / name
    if f.exists():
        deps[name] = read(f)[:1500]

baseline = {
    "root": str(ROOT),
    "git_head": sh("git", "log", "-1", "--format=%H %s"),
    "git_branch": sh("git", "rev-parse", "--abbrev-ref", "HEAD"),
    "totals": {
        "py_files": len(modules),
        "py_lines": sum(m["lines"] for m in modules),
        "db_tables": len(tables),
        "db_rows": sum((t["rows"] or 0) for t in tables),
        "api_routes": len(routes),
        "scheduled_jobs": len(jobs),
        "tests": sum(t["tests"] for t in tests),
    },
    "modules": modules,
    "database": tables,
    "api_routes": routes,
    "scheduled_jobs": jobs,
    "job_functions": job_fns,
    "score_functions": score_fns,
    "engine_constants": engine_consts,
    "technical_indicator_columns": indicator_cols,
    "config_keys_read": cfg_keys,
    "tests": tests,
    "dependency_files": deps,
}
(OUT / "baseline.json").write_text(json.dumps(baseline, indent=1), encoding="utf-8")

L = []
A = L.append
t = baseline["totals"]
A("# ATIP baseline — Phase 1\n")
A(f"Extracted from `{ROOT}` at `{baseline['git_head']}` "
  f"(branch `{baseline['git_branch']}`).")
A("Every figure below is read out of the code or the database, so later phases "
  "can be diffed against it rather than described.\n")
A("| Python files | Python lines | DB tables | DB rows | API routes | Scheduled jobs | Tests |")
A("|---|---|---|---|---|---|---|")
A(f"| {t['py_files']} | {t['py_lines']:,} | {t['db_tables']} | {t['db_rows']:,} | "
  f"{t['api_routes']} | {t['scheduled_jobs']} | {t['tests']} |\n")

A("## Modules\n")
A("| File | Lines | Purpose |")
A("|---|---|---|")
for m in sorted(modules, key=lambda m: -m["lines"]):
    A(f"| `{m['file']}` | {m['lines']} | {m.get('doc', '')} |")

A("\n## Database\n")
A("| Table | Rows | Cols | Span |")
A("|---|---|---|---|")
for tb in tables:
    span = f"{tb['span']['min']} → {tb['span']['max']}" if tb["span"] else ""
    rows = f"{tb['rows']:,}" if tb["rows"] is not None else "?"
    A(f"| `{tb['table']}` | {rows} | {tb['n_columns']} | {span} |")

A("\n## API routes\n")
for r in routes:
    A(f"- `{r['match']}` — `{r['file']}:{r['line']}`")

A("\n## Scheduled jobs\n")
for j in jobs:
    A(f"- `{j['match'][:110]}` — `{j['file']}:{j['line']}`")

A("\n## Scoring engine\n")
A("Functions: " + ", ".join(f"`{f}`" for f in score_fns))
A("\nConstants:\n")
for k, v in engine_consts:
    A(f"- `{k}` = `{v[:110]}`")

A("\n## technical_indicators columns written\n")
A(", ".join(f"`{c}`" for c in indicator_cols) or "(not detected)")

A("\n\n## Tests\n")
A("| File | Tests | Parametrized blocks |")
A("|---|---|---|")
for te in tests:
    A(f"| `{te['file']}` | {te['tests']} | {te['parametrized']} |")
A(f"\n**Total: {t['tests']} test functions.**")

(OUT / "BASELINE.md").write_text("\n".join(L) + "\n", encoding="utf-8")
print("wrote", OUT / "baseline.json", "and", OUT / "BASELINE.md")
print(json.dumps(baseline["totals"], indent=1))
