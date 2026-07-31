#!/usr/bin/env python3
"""
Logical Branch Flow Extractor — static "distributed tracing" for control flow.

Builds:
  1. Call graph among functions in the file
  2. Per-function nested branch spans (parent/child, depth, line range)
  3. Family grouping of similar predicates (incl. fillna / DF masks)

Usage:
  python analyze_branches.py [path/to/file.py] [--json out.json] [--summary]
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def snippet(src: str, node: ast.AST, max_len: int = 120) -> str:
    try:
        s = ast.get_source_segment(src, node) or ""
    except Exception:
        s = ""
    s = " ".join(s.split())
    return s[:max_len] + ("…" if len(s) > max_len else "")


def end_lineno(node: ast.AST) -> int:
    return int(getattr(node, "end_lineno", None) or getattr(node, "lineno", 0) or 0)


def classify(cond_text: str, kind: str) -> tuple[str, str]:
    t = cond_text.lower()

    if kind == "try":
        if "importerror" in t:
            return "import_fallback", "Try/except import fallback"
        return "exception_handling", "Try/except runtime recovery"
    if kind == "fillna":
        return "null_fill", "DataFrame null-fill (fillna)"
    if kind == "where_mask":
        if "rain-link" in t or "sensors" in t:
            return "sensor_filter", "Sensor type filter (rain-link)"
        if "is_active" in t or "is_archived" in t:
            return "entity_status", "Entity active / archived filter"
        if "field_id" in t or "exclude_field" in t:
            return "field_filter", "Field ID include/exclude filter"
        return "vector_where", "Vectorized boolean mask"

    if ".empty" in t or "num_rows" in t or "pipeline_results" in t:
        return "empty_guard", "Empty / missing data guard"
    if "reprocess" in t:
        return "reprocess_mode", "Reprocess mode branching"
    if "enable_async" in t or "async_optim" in t:
        return "async_feature_flag", "Async optimization feature flag"
    if "enable_calibrated" in t:
        return "calibrated_pipeline", "Calibrated pipeline feature flag"
    if "enable_prefect" in t or "enable_open_observe" in t or "openobserve" in t:
        return "observability_flag", "Logging / observability flag"
    if "isinstance" in t and "exception" in t:
        return "exception_result", "Exception vs success result"
    if "isinstance" in t:
        return "type_dispatch", "Type / shape dispatch"
    if "rain-link" in t or ("sensors" in t and "isin" in t):
        return "sensor_filter", "Sensor type filter (rain-link)"
    if "field_id" in t or "field_ids" in t or "exclude_field" in t:
        return "field_filter", "Field ID include/exclude filter"
    if "farm_id" in t or "farm_ids" in t or "account" in t:
        return "scope_filter", "Account / farm scope filter"
    if "is_archived" in t or "is_active" in t:
        return "entity_status", "Entity active / archived filter"
    if any(
        x in t
        for x in (
            "time_dimension",
            "time_dim",
            "start_date",
            "end_date",
            "start_time",
            "end_time",
            "clipped",
        )
    ):
        return "time_window", "Time window / time-dimension branch"
    if "installation_form" in t:
        return "missing_context", "Missing installation / context early-exit"
    if any(x in t for x in ("bronze", "silver", "not pool")):
        return "data_presence", "Data presence / empty result branch"
    if any(
        x in t
        for x in ("observation", "obs_ids", "reprocess_metric", "rt_obs", "rt_met")
    ):
        return "metric_selection", "Metric / observation selection mode"
    if "status" in t and "error" in t:
        return "error_status", "Error status early-exit"
    if kind == "comprehension_if":
        return "comprehension_filter", "Comprehension filter"
    if kind == "ternary":
        return "inline_ternary", "Inline ternary / defaulting"
    if any(x in t for x in ("hasattr", "is not none", "is none", " none")):
        return "null_presence", "None / presence check"
    return "other_guard", "Other guard / branch"


def analyze(path: Path) -> dict[str, Any]:
    src = path.read_text()
    tree = ast.parse(src)
    lines = src.splitlines()

    # ---- collect function defs ----
    functions: dict[str, dict[str, Any]] = {}

    class FuncCollector(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            functions[node.name] = {
                "name": node.name,
                "line": node.lineno,
                "endLine": end_lineno(node),
                "async": False,
            }
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            functions[node.name] = {
                "name": node.name,
                "line": node.lineno,
                "endLine": end_lineno(node),
                "async": True,
            }
            self.generic_visit(node)

    FuncCollector().visit(tree)

    # ---- call graph (caller → callee among local defs) ----
    call_edges: dict[tuple[str, str], int] = defaultdict(int)
    call_sites: list[dict[str, Any]] = []

    class CallVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = ["<module>"]

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.visit_FunctionDef(node)  # type: ignore[arg-type]

        def visit_Call(self, node: ast.Call) -> None:
            callee = None
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            elif isinstance(node.func, ast.Attribute):
                # process_field.submit(...) / process_field.with_options(...).submit
                if node.func.attr in {"submit", "map"}:
                    base = node.func.value
                    if isinstance(base, ast.Name):
                        callee = base.id
                    elif isinstance(base, ast.Call) and isinstance(base.func, ast.Attribute):
                        # x.with_options(...).submit
                        if isinstance(base.func.value, ast.Name):
                            callee = base.func.value.id
                else:
                    callee = node.func.attr
            if callee and callee in functions:
                caller = self.stack[-1]
                if caller != callee:
                    call_edges[(caller, callee)] += 1
                    call_sites.append(
                        {
                            "from": caller,
                            "to": callee,
                            "line": node.lineno,
                        }
                    )
            self.generic_visit(node)

    CallVisitor().visit(tree)

    # ---- nested branch spans per function ----
    spans: list[dict[str, Any]] = []
    span_seq = 0

    class SpanVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.fn_stack: list[str] = ["<module>"]
            self.parent_stack: list[str | None] = [None]
            self.depth = 0

        def _fn(self) -> str:
            return self.fn_stack[-1]

        def _parent(self) -> str | None:
            return self.parent_stack[-1]

        def _add(
            self,
            node: ast.AST,
            *,
            kind: str,
            condition: str,
            label: str,
            early_exit: bool = False,
            chain_id: str | None = None,
            enter_body: bool = False,
        ) -> str:
            nonlocal span_seq
            span_seq += 1
            sid = f"s{span_seq}"
            code = snippet(src, node, 140)
            fid, flabel = classify(f"{condition} {code}", kind)
            start = int(getattr(node, "lineno", 0) or 0)
            end = end_lineno(node)
            if end < start:
                end = start
            spans.append(
                {
                    "id": sid,
                    "parentId": self._parent(),
                    "depth": self.depth,
                    "seq": span_seq,
                    "function": self._fn(),
                    "line": start,
                    "endLine": end,
                    "kind": kind,
                    "familyId": fid,
                    "family": flabel,
                    "condition": condition,
                    "label": label,
                    "code": code,
                    "earlyExit": early_exit,
                    "chainId": chain_id,
                }
            )
            return sid

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.fn_stack.append(node.name)
            # function itself is a root "service" span for the waterfall
            self.generic_visit(node)
            self.fn_stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.visit_FunctionDef(node)  # type: ignore[arg-type]

        def visit_If(self, node: ast.If) -> None:
            chain_id = f"if@{node.lineno}"
            current: ast.stmt | ast.If = node
            branch_idx = 0
            while True:
                if isinstance(current, ast.If):
                    kind = "if" if branch_idx == 0 else "elif"
                    cond = snippet(src, current.test, 100)
                    has_return = any(isinstance(n, ast.Return) for n in current.body)
                    label = f"{kind} {cond}"
                    sid = self._add(
                        current,
                        kind=kind,
                        condition=cond,
                        label=label[:90],
                        early_exit=has_return,
                        chain_id=chain_id,
                    )
                    # visit body under this span
                    self.parent_stack.append(sid)
                    self.depth += 1
                    for child in current.body:
                        self.visit(child)
                    self.depth -= 1
                    self.parent_stack.pop()

                    if not current.orelse:
                        break
                    if len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
                        current = current.orelse[0]
                        branch_idx += 1
                        continue
                    # else block
                    else_node = current.orelse[0]
                    has_return = any(isinstance(n, ast.Return) for n in current.orelse)
                    # synthetic span covering else body range
                    else_start = else_node.lineno
                    else_end = end_lineno(current.orelse[-1])
                    sid = self._add(
                        else_node,
                        kind="else",
                        condition="else",
                        label="else",
                        early_exit=has_return,
                        chain_id=chain_id,
                    )
                    # fix endLine for else to cover full orelse
                    spans[-1]["endLine"] = max(spans[-1]["endLine"], else_end)
                    spans[-1]["line"] = else_start
                    self.parent_stack.append(sid)
                    self.depth += 1
                    for child in current.orelse:
                        self.visit(child)
                    self.depth -= 1
                    self.parent_stack.pop()
                    break
                break

        def visit_IfExp(self, node: ast.IfExp) -> None:
            cond = snippet(src, node.test, 80)
            self._add(
                node,
                kind="ternary",
                condition=cond,
                label=f"ternary {cond}"[:90],
            )
            self.generic_visit(node)

        def visit_comprehension(self, node: ast.comprehension) -> None:
            for cond in node.ifs:
                c = snippet(src, cond, 80)
                self._add(
                    cond,
                    kind="comprehension_if",
                    condition=c,
                    label=f"filter {c}"[:90],
                )
            self.generic_visit(node)

        def visit_Try(self, node: ast.Try) -> None:
            handlers = []
            for h in node.handlers:
                if h.type is None:
                    handlers.append("bare")
                else:
                    handlers.append(snippet(src, h.type, 40))
            cond = f"try/except ({', '.join(handlers)})"
            sid = self._add(
                node,
                kind="try",
                condition=cond,
                label=cond[:90],
            )
            self.parent_stack.append(sid)
            self.depth += 1
            for child in node.body:
                self.visit(child)
            for h in node.handlers:
                self.visit(h)
            for child in node.orelse:
                self.visit(child)
            for child in node.finalbody:
                self.visit(child)
            self.depth -= 1
            self.parent_stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            name = None
            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
            elif isinstance(node.func, ast.Name):
                name = node.func.id
            if name in {"fillna", "combine_first"}:
                self._add(
                    node,
                    kind="fillna",
                    condition=f"{name}(...)",
                    label=f"{name} null→default",
                )
            elif name in {"where", "mask", "clip"}:
                self._add(
                    node,
                    kind="where_mask",
                    condition=f"{name}(...)",
                    label=f"vector {name}",
                )
            self.generic_visit(node)

        def visit_Subscript(self, node: ast.Subscript) -> None:
            sl = node.slice
            if isinstance(sl, (ast.BoolOp, ast.UnaryOp, ast.Compare, ast.BinOp)):
                code = snippet(src, node, 120)
                if not code.startswith("kwargs["):
                    self._add(
                        node,
                        kind="where_mask",
                        condition="boolean mask",
                        label=f"mask {code}"[:90],
                    )
            self.generic_visit(node)

    SpanVisitor().visit(tree)

    # dedupe identical leaf spans at same line/kind/cond (masks + fillna overlap ok)
    seen: set[tuple] = set()
    uniq_spans: list[dict[str, Any]] = []
    id_remap: dict[str, str] = {}
    for s in spans:
        key = (s["line"], s["kind"], s["condition"][:60], s["function"], s["parentId"])
        if key in seen and s["kind"] in {"where_mask", "fillna", "ternary", "comprehension_if"}:
            continue
        seen.add(key)
        uniq_spans.append(s)

    # rebuild parent links validity
    valid_ids = {s["id"] for s in uniq_spans}
    for s in uniq_spans:
        if s["parentId"] not in valid_ids:
            s["parentId"] = None

    # per-function traces
    by_fn: dict[str, list] = defaultdict(list)
    for s in uniq_spans:
        by_fn[s["function"]].append(s)

    traces = []
    for fname, items in sorted(
        by_fn.items(),
        key=lambda x: (-len(x[1]), x[0]),
    ):
        items_sorted = sorted(items, key=lambda x: (x["line"], x["seq"]))
        # assign waterfall row index (DFS order by parent)
        children: dict[str | None, list] = defaultdict(list)
        for s in items_sorted:
            children[s["parentId"]].append(s)
        ordered: list[dict[str, Any]] = []

        def walk(pid: str | None) -> None:
            for child in children.get(pid, []):
                ordered.append(child)
                walk(child["id"])

        walk(None)
        # fallback any orphans
        seen_ids = {s["id"] for s in ordered}
        for s in items_sorted:
            if s["id"] not in seen_ids:
                ordered.append(s)

        for i, s in enumerate(ordered):
            s["row"] = i

        meta = functions.get(fname, {"line": items_sorted[0]["line"], "endLine": items_sorted[-1]["endLine"]})
        traces.append(
            {
                "function": fname,
                "line": meta.get("line", items_sorted[0]["line"]),
                "endLine": meta.get("endLine", items_sorted[-1]["endLine"]),
                "spanCount": len(ordered),
                "maxDepth": max((s["depth"] for s in ordered), default=0),
                "earlyExits": sum(1 for s in ordered if s["earlyExit"]),
                "spans": [
                    {
                        "id": s["id"],
                        "parentId": s["parentId"],
                        "depth": s["depth"],
                        "row": s["row"],
                        "line": s["line"],
                        "endLine": s["endLine"],
                        "kind": s["kind"],
                        "familyId": s["familyId"],
                        "family": s["family"],
                        "label": s["label"],
                        "condition": s["condition"],
                        "code": s["code"],
                        "earlyExit": s["earlyExit"],
                    }
                    for s in ordered
                ],
            }
        )

    # family rollup
    by_family: dict[str, list] = defaultdict(list)
    for s in uniq_spans:
        by_family[s["familyId"]].append(s)
    families = [
        {
            "id": fid,
            "label": items[0]["family"],
            "count": len(items),
            "functions": sorted({i["function"] for i in items}),
        }
        for fid, items in sorted(by_family.items(), key=lambda x: -len(x[1]))
    ]

    # ---- auto entrypoints & flow paths (no file-specific hardcoding) ----
    entrypoints: list[dict[str, Any]] = []

    class EntryVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            deco_names = []
            for d in node.decorator_list:
                if isinstance(d, ast.Name):
                    deco_names.append(d.id)
                elif isinstance(d, ast.Attribute):
                    deco_names.append(d.attr)
                elif isinstance(d, ast.Call):
                    if isinstance(d.func, ast.Name):
                        deco_names.append(d.func.id)
                    elif isinstance(d.func, ast.Attribute):
                        deco_names.append(d.func.attr)
            if any(n in {"flow", "task", "app"} or n.endswith("_flow") for n in deco_names):
                entrypoints.append(
                    {
                        "name": node.name,
                        "reason": f"decorator:{','.join(deco_names)}",
                        "line": node.lineno,
                    }
                )
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.visit_FunctionDef(node)  # type: ignore[arg-type]

        def visit_If(self, node: ast.If) -> None:
            # if __name__ == "__main__": ...
            test = node.test
            is_main = False
            if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name):
                if test.left.id == "__name__":
                    for comp in test.comparators:
                        if isinstance(comp, ast.Constant) and comp.value == "__main__":
                            is_main = True
            if is_main:
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        callee = None
                        if isinstance(child.func, ast.Name):
                            callee = child.func.id
                        elif isinstance(child.func, ast.Attribute):
                            # asyncio.run(foo(...)) → look into args
                            if child.func.attr == "run" and child.args:
                                arg0 = child.args[0]
                                if isinstance(arg0, ast.Call):
                                    if isinstance(arg0.func, ast.Name):
                                        callee = arg0.func.id
                            else:
                                callee = child.func.attr
                        if callee and callee in functions:
                            entrypoints.append(
                                {
                                    "name": callee,
                                    "reason": "__main__",
                                    "line": child.lineno,
                                }
                            )
            self.generic_visit(node)

    EntryVisitor().visit(tree)

    # also treat roots: defined functions that call others but are never called
    callees = {b for _, b in call_edges}
    callers_only = [
        name
        for name in functions
        if name not in callees
        and any(a == name for a, _ in call_edges)
        and name != "<module>"
    ]
    for name in callers_only:
        if not any(e["name"] == name for e in entrypoints):
            entrypoints.append(
                {"name": name, "reason": "call-graph-root", "line": functions[name]["line"]}
            )

    # dedupe entrypoints preserving order
    seen_ep: set[str] = set()
    uniq_eps: list[dict[str, Any]] = []
    for ep in entrypoints:
        if ep["name"] in seen_ep:
            continue
        seen_ep.add(ep["name"])
        uniq_eps.append(ep)
    entrypoints = uniq_eps

    # adjacency for path BFS
    adj: dict[str, list[str]] = defaultdict(list)
    for (a, b), _c in call_edges.items():
        if b not in adj[a]:
            adj[a].append(b)

    def longest_path(start: str, limit: int = 8) -> list[str]:
        """Greedy DFS preferring callees with more branch spans."""
        path = [start]
        seen_n = {start}
        cur = start
        while len(path) < limit:
            opts = [n for n in adj.get(cur, []) if n not in seen_n]
            if not opts:
                break
            opts.sort(key=lambda n: (-len(by_fn.get(n, [])), n))
            nxt = opts[0]
            path.append(nxt)
            seen_n.add(nxt)
            cur = nxt
        return path

    flow_paths: list[dict[str, Any]] = []
    for ep in entrypoints:
        steps = longest_path(ep["name"])
        # keep paths that actually have branching somewhere
        if sum(len(by_fn.get(s, [])) for s in steps) == 0 and len(steps) == 1:
            continue
        flow_paths.append(
            {
                "id": f"from_{ep['name']}",
                "label": f"From {ep['name']} ({ep['reason']})",
                "steps": steps,
                "entryReason": ep["reason"],
            }
        )

    # fallback: densest functions as solo paths
    if not flow_paths:
        densest = sorted(by_fn.items(), key=lambda x: -len(x[1]))[:5]
        for fname, items in densest:
            flow_paths.append(
                {
                    "id": f"fn_{fname}",
                    "label": f"Function · {fname}",
                    "steps": [fname],
                    "entryReason": "span-density",
                }
            )

    kind_counts: dict[str, int] = defaultdict(int)
    for s in uniq_spans:
        kind_counts[s["kind"]] += 1

    return {
        "kind": "file",
        "source": str(path),
        "fileName": path.name,
        "totalSpans": len(uniq_spans),
        "byKind": dict(kind_counts),
        "families": families,
        "entrypoints": entrypoints,
        "functions": [
            {
                "name": f["name"],
                "line": f["line"],
                "endLine": f["endLine"],
                "async": f["async"],
                "spanCount": len(by_fn.get(f["name"], [])),
            }
            for f in sorted(functions.values(), key=lambda x: x["line"])
        ],
        "callGraph": {
            "nodes": [
                {
                    "id": name,
                    "line": meta["line"],
                    "endLine": meta["endLine"],
                    "spanCount": len(by_fn.get(name, [])),
                }
                for name, meta in functions.items()
                if len(by_fn.get(name, [])) > 0
                or any(a == name or b == name for a, b in call_edges)
            ],
            "edges": [
                {
                    "from": a,
                    "to": b,
                    "count": c,
                    "line": next(
                        (
                            s["line"]
                            for s in call_sites
                            if s["from"] == a and s["to"] == b
                        ),
                        None,
                    ),
                }
                for (a, b), c in sorted(call_edges.items(), key=lambda x: -x[1])
            ],
        },
        "flowPaths": flow_paths,
        "traces": traces,
    }


SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    ".env",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    ".eggs",
    ".idea",
    ".cursor",
    "htmlcov",
}


def discover_python_files(root: Path, *, max_files: int = 2000) -> list[Path]:
    """Recursively find .py files under root, skipping junk directories."""
    root = root.resolve()
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith(".")
        )
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            if name.startswith("."):
                continue
            found.append(Path(dirpath) / name)
            if len(found) >= max_files:
                return found
    return found


def _pick_default_file(files: list[dict[str, Any]]) -> str | None:
    if not files:
        return None
    # Prefer names that look like entry modules, else densest by spans
    preferred_names = ("main.py", "app.py", "__main__.py", "cli.py", "server.py")
    for pref in preferred_names:
        hits = [f for f in files if f.get("error") is None and f["name"] == pref]
        if hits:
            hits.sort(key=lambda f: (-f.get("totalSpans", 0), f["relPath"]))
            return hits[0]["relPath"]
    ok = [f for f in files if f.get("error") is None]
    if not ok:
        return files[0]["relPath"]
    ok.sort(key=lambda f: (-f.get("totalSpans", 0), f["relPath"]))
    return ok[0]["relPath"]


def analyze_target(
    path: Path,
    *,
    file: str | None = None,
    max_files: int = 2000,
) -> dict[str, Any]:
    """
    Analyze a .py file or a directory of .py files.

    Directory mode returns a file index plus a full report for the active file
    (``file`` relative path, or auto-picked default).
    """
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(str(path))

    if path.is_file():
        if path.suffix != ".py":
            raise ValueError("path must be a .py file or a directory")
        report = analyze(path)
        report["files"] = [
            {
                "path": str(path),
                "relPath": path.name,
                "name": path.name,
                "totalSpans": report["totalSpans"],
                "families": len(report["families"]),
                "functions": len(report["functions"]),
                "entrypoints": len(report["entrypoints"]),
                "error": None,
            }
        ]
        report["root"] = str(path.parent)
        report["activeFile"] = path.name
        report["fileCount"] = 1
        report["scannedFiles"] = 1
        return report

    if not path.is_dir():
        raise ValueError("path must be a .py file or a directory")

    py_files = discover_python_files(path, max_files=max_files)
    index: list[dict[str, Any]] = []
    truncated = len(py_files) >= max_files

    for fp in py_files:
        rel = str(fp.relative_to(path))
        try:
            # Lightweight pass: full analyze for index metrics
            # (needed for spans; still AST-only and fast enough for typical packages)
            r = analyze(fp)
            index.append(
                {
                    "path": str(fp),
                    "relPath": rel,
                    "name": fp.name,
                    "totalSpans": r["totalSpans"],
                    "families": len(r["families"]),
                    "functions": len(r["functions"]),
                    "entrypoints": len(r["entrypoints"]),
                    "error": None,
                }
            )
        except SyntaxError as e:
            index.append(
                {
                    "path": str(fp),
                    "relPath": rel,
                    "name": fp.name,
                    "totalSpans": 0,
                    "families": 0,
                    "functions": 0,
                    "entrypoints": 0,
                    "error": f"syntax error: {e}",
                }
            )
        except Exception as e:
            index.append(
                {
                    "path": str(fp),
                    "relPath": rel,
                    "name": fp.name,
                    "totalSpans": 0,
                    "families": 0,
                    "functions": 0,
                    "entrypoints": 0,
                    "error": str(e),
                }
            )

    index.sort(key=lambda f: (-f.get("totalSpans", 0), f["relPath"]))

    active_rel = file
    if active_rel:
        # normalize
        active_rel = str(Path(active_rel))
        match = next((f for f in index if f["relPath"] == active_rel), None)
        if match is None:
            raise FileNotFoundError(
                f"file '{active_rel}' not found under directory {path}"
            )
    else:
        active_rel = _pick_default_file(index)

    if active_rel is None:
        return {
            "kind": "directory",
            "root": str(path),
            "source": str(path),
            "fileName": path.name,
            "activeFile": None,
            "fileCount": 0,
            "scannedFiles": 0,
            "truncated": truncated,
            "files": [],
            "totalSpans": 0,
            "byKind": {},
            "families": [],
            "entrypoints": [],
            "functions": [],
            "callGraph": {"nodes": [], "edges": []},
            "flowPaths": [],
            "traces": [],
        }

    active_abs = path / active_rel
    report = analyze(active_abs)
    report["kind"] = "directory"
    report["root"] = str(path)
    report["activeFile"] = active_rel
    report["files"] = index
    report["fileCount"] = len(index)
    report["scannedFiles"] = len(py_files)
    report["truncated"] = truncated
    report["workspaceSpans"] = sum(f.get("totalSpans", 0) for f in index)
    return report


def report_to_markdown(report: dict[str, Any], *, title: str | None = None) -> str:
    """
    LLM-oriented markdown of business/logical branches for one analyzed report.

    Grouped by family first (for clustering), then by function sequence order.
    """
    lines: list[str] = []
    src = report.get("activeFile") or report.get("fileName") or report.get("source")
    lines.append(f"# {title or 'Logical Branch Report'}")
    lines.append("")
    lines.append("## Meta")
    lines.append("")
    lines.append(f"- **source**: `{report.get('source')}`")
    if report.get("root"):
        lines.append(f"- **workspace_root**: `{report['root']}`")
    if report.get("activeFile"):
        lines.append(f"- **active_file**: `{report['activeFile']}`")
    lines.append(f"- **total_spans**: {report.get('totalSpans', 0)}")
    lines.append(f"- **families**: {len(report.get('families') or [])}")
    lines.append(f"- **entrypoints**: {len(report.get('entrypoints') or [])}")
    kind_bits = ", ".join(
        f"{k}={v}" for k, v in sorted((report.get("byKind") or {}).items())
    )
    if kind_bits:
        lines.append(f"- **by_kind**: {kind_bits}")
    lines.append("")
    lines.append(
        "> Use family groups to cluster similar business decisions; "
        "use per-function sequences for evaluation order."
    )
    lines.append("")

    # Entrypoints / flow paths
    if report.get("entrypoints"):
        lines.append("## Entrypoints")
        lines.append("")
        for ep in report["entrypoints"]:
            lines.append(
                f"- `{ep['name']}` — {ep.get('reason', '')} (L{ep.get('line', '?')})"
            )
        lines.append("")

    if report.get("flowPaths"):
        lines.append("## Orchestration paths")
        lines.append("")
        for p in report["flowPaths"]:
            steps = " → ".join(f"`{s}`" for s in p.get("steps") or [])
            lines.append(f"- **{p.get('label', p.get('id'))}**: {steps}")
        lines.append("")

    # Family grouping — primary LLM clustering surface
    lines.append("## Logical families (similar branches grouped)")
    lines.append("")
    by_family: dict[str, list] = defaultdict(list)
    for t in report.get("traces") or []:
        for s in t.get("spans") or []:
            by_family[s.get("familyId") or "other"].append({**s, "function": t["function"]})

    fam_meta = {f["id"]: f for f in (report.get("families") or [])}
    for fid, items in sorted(by_family.items(), key=lambda x: -len(x[1])):
        meta = fam_meta.get(fid, {})
        label = meta.get("label") or items[0].get("family") or fid
        lines.append(f"### {label} ({len(items)})")
        lines.append("")
        lines.append(f"- **family_id**: `{fid}`")
        funcs = sorted({i["function"] for i in items})
        lines.append(f"- **functions**: {', '.join(f'`{f}`' for f in funcs)}")
        lines.append("- **sites**:")
        for i in sorted(items, key=lambda x: (x.get("function", ""), x.get("line", 0))):
            exit_mark = " ↩" if i.get("earlyExit") else ""
            cond = (i.get("condition") or i.get("label") or "").replace("\n", " ")
            lines.append(
                f"  - `{i['function']}` L{i['line']}"
                f"{'' if i.get('endLine') == i.get('line') else '–' + str(i.get('endLine'))}"
                f" · `{i.get('kind')}`{exit_mark}: `{cond}`"
            )
        lines.append("")

    # Per-function ordered sequences
    lines.append("## Per-function evaluation order")
    lines.append("")
    for t in report.get("traces") or []:
        lines.append(
            f"### `{t['function']}` "
            f"(L{t['line']}–{t['endLine']}, {t['spanCount']} spans, depth≤{t['maxDepth']})"
        )
        lines.append("")
        for i, s in enumerate(t.get("spans") or [], 1):
            pad = "  " * int(s.get("depth") or 0)
            exit_mark = " ↩" if s.get("earlyExit") else ""
            cond = (s.get("condition") or s.get("label") or "").replace("\n", " ")
            lines.append(
                f"{i}. {pad}`{s.get('kind')}` L{s['line']} "
                f"[{s.get('familyId')}]{exit_mark}: `{cond}`"
            )
        lines.append("")

    # Call graph
    edges = (report.get("callGraph") or {}).get("edges") or []
    if edges:
        lines.append("## Call graph (local)")
        lines.append("")
        for e in edges[:40]:
            line_bit = f", L{e['line']}" if e.get("line") is not None else ""
            lines.append(
                f"- `{e['from']}` → `{e['to']}` (×{e.get('count', 1)}{line_bit})"
            )
        lines.append("")

    lines.append("## Prompt hint for LLM grouping")
    lines.append("")
    lines.append(
        "Cluster the **family** sections into higher-level business decisions "
        "(e.g. reprocess windowing, empty-data guards, sensor filters, feature flags). "
        "Prefer merging families that share the same business intent even if predicates differ slightly. "
        "Preserve evaluation order from the per-function sections when describing control flow."
    )
    lines.append("")
    return "\n".join(lines)


def workspace_markdown(
    root: Path,
    *,
    max_files: int = 2000,
    min_spans: int = 1,
) -> str:
    """Markdown report covering every .py file under root (dense files first)."""
    root = root.resolve()
    index_report = analyze_target(root, max_files=max_files)
    files = [
        f
        for f in (index_report.get("files") or [])
        if not f.get("error") and (f.get("totalSpans") or 0) >= min_spans
    ]
    parts: list[str] = [
        f"# Workspace Logical Branch Report",
        "",
        f"- **root**: `{root}`",
        f"- **files_scanned**: {index_report.get('scannedFiles', len(files))}",
        f"- **files_with_spans**: {len(files)}",
        f"- **workspace_spans**: {index_report.get('workspaceSpans', 0)}",
        "",
        "## File index (by span density)",
        "",
    ]
    for f in files:
        parts.append(f"- `{f['relPath']}` — {f['totalSpans']} spans")
    parts.append("")

    for f in files:
        try:
            r = analyze(Path(f["path"]))
            r["activeFile"] = f["relPath"]
            r["root"] = str(root)
            parts.append(
                report_to_markdown(
                    r,
                    title=f"File · {f['relPath']}",
                )
            )
            parts.append("")
            parts.append("---")
            parts.append("")
        except Exception as e:
            parts.append(f"## File · `{f['relPath']}`")
            parts.append("")
            parts.append(f"_Analyze failed: {e}_")
            parts.append("")
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        default=str(
            Path(__file__).resolve().parents[2]
            / "services"
            / "data-pipeline"
            / "src"
        ),
        help="Python file or directory to analyze",
    )
    parser.add_argument(
        "--file",
        help="When path is a directory, relative .py file to open as active report",
    )
    parser.add_argument("--json", dest="json_out")
    parser.add_argument(
        "--markdown",
        dest="markdown_out",
        help="Write LLM-oriented markdown report to this path",
    )
    parser.add_argument(
        "--workspace-markdown",
        action="store_true",
        help="With --markdown on a directory: include every file (not only active)",
    )
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--max-files", type=int, default=2000)
    args = parser.parse_args()

    path = Path(args.path).resolve()
    if not path.exists():
        print(f"Path not found: {path}", file=sys.stderr)
        return 1

    report = analyze_target(path, file=args.file, max_files=args.max_files)

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2))
        print(f"Wrote {report['totalSpans']} spans → {out}", file=sys.stderr)

    if args.markdown_out:
        out = Path(args.markdown_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if args.workspace_markdown and path.is_dir():
            md = workspace_markdown(path, max_files=args.max_files)
        else:
            md = report_to_markdown(report)
        out.write_text(md)
        print(f"Wrote markdown → {out}", file=sys.stderr)

    if args.summary or (not args.json_out and not args.markdown_out):
        print(f"kind: {report.get('kind')}")
        print(f"source: {report['source']}")
        if report.get("kind") == "directory":
            print(f"root: {report.get('root')}")
            print(f"files: {report.get('fileCount')} (active: {report.get('activeFile')})")
            print(f"workspace_spans: {report.get('workspaceSpans', report['totalSpans'])}")
        print(f"active_spans: {report['totalSpans']}")
        print(f"by_kind: {report['byKind']}")
        if report.get("files") and report.get("kind") == "directory":
            print("\nTop files by spans:")
            for f in report["files"][:15]:
                err = f"  ERR {f['error']}" if f.get("error") else ""
                print(f"  {f['totalSpans']:4d}  {f['relPath']}{err}")
        print("\nCall graph edges:")
        for e in report["callGraph"]["edges"][:20]:
            print(f"  {e['from']} → {e['to']}  (x{e['count']})  L{e['line']}")
        print("\nTraces (function waterfalls):")
        for t in report["traces"][:15]:
            print(
                f"  {t['spanCount']:3d} spans  depth≤{t['maxDepth']}  "
                f"{t['function']}  L{t['line']}-{t['endLine']}"
            )
        print("\nFlow paths:")
        for p in report["flowPaths"][:10]:
            print(f"  {p['label']}: {' → '.join(p['steps'])}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
