"""AI workflows: a graph of nodes that moves rows from sources through LLMs to datasets, with guardrails.

Rows flow between nodes as Polars frames with two hidden columns:
  _row_id  - stable row number from the input, used to join LLM results back
  _review  - "; "-joined reasons a row was flagged for review ("" when clean)
Rejected rows leave the flow and are collected (with the node and reason) into the run's review file.

Node types
  input_rows      rows from a source or a published mapping dataset (columns, filter, limit)
  input_form      one row built from the run input (form in the studio, or the API payload)
  transform       add columns with formulas and/or filter rows (the mapping formula language)
  sql             a read-only DuckDB SELECT over the incoming rows (table "rows"): aggregate first, then ask
  llm             system + user prompt per row or per batch; text or JSON output; guardrails
  router          splits rows by a formula into "true" and "false" branches
  output_dataset  saves rows as a DataBridge source you can map, publish and serve as an endpoint

The same executor does a dry run (no model calls) for validation and cost estimates.
"""

import json
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import duckdb
import polars as pl
import sqlglot
from sqlalchemy import func, select

from databridge.ai import guardrails as gr
from databridge.ai.templating import PromptTemplateError, render, variables_in
from databridge.config import settings
from databridge.core.auth import can
from databridge.core.db import session_scope
from databridge.core.models import (Mapping, NodeRun, SourceObject, User, Workflow, WorkflowRun, utcnow)
from databridge.engine.formula import FormulaError, compile_formula, referenced_columns
from databridge.ingest.profiling import profile_fields
from databridge.services import llm
from databridge.services import mappings as map_svc
from databridge.services import prompts as prompt_svc
from databridge.services import sources as src_svc
from databridge.services.users import audit

HIDDEN = ("_row_id", "_review")

NODE_TYPES: dict[str, dict[str, Any]] = {
    "input_rows": {"label": "Dataset rows", "group": "Inputs", "ports": ["out"], "inputs": 0,
                   "help": "Rows from a source or a published mapping"},
    "input_form": {"label": "Run input", "group": "Inputs", "ports": ["out"], "inputs": 0,
                   "help": "One row from the run form or the API payload"},
    "transform": {"label": "Transform", "group": "Logic", "ports": ["out"], "inputs": 1,
                  "help": "Add columns with formulas, filter rows"},
    "sql": {"label": "SQL (aggregate)", "group": "Logic", "ports": ["out"], "inputs": 1,
            "help": "Read-only DuckDB query over the incoming rows"},
    "router": {"label": "Router", "group": "Logic", "ports": ["true", "false"], "inputs": 1,
               "help": "Split rows by a condition"},
    "llm": {"label": "LLM", "group": "AI", "ports": ["out"], "inputs": 1,
            "help": "Prompt a model per row or per batch, with guardrails"},
    "output_dataset": {"label": "Output dataset", "group": "Outputs", "ports": [], "inputs": 1,
                       "help": "Save rows as a source you can map and serve"},
}

DEFAULT_LIMITS = {"max_rows": 1000, "max_tokens_per_run": 200_000, "confirm_over_tokens": 20_000,
                  "monthly_tokens": 0}

DEFAULT_GUARDRAILS = {"pii_mode": "auto", "injection": "flag", "on_invalid": "flag", "rules": [], "grounding": [],
                      "banned": [], "max_output_chars": 0, "max_prompt_tokens": 4000, "pii_leak": True}


def default_config(node_type: str) -> dict[str, Any]:
    return {
        "input_rows": {"source_id": None, "mapping_id": None, "columns": [], "filter": "", "limit": 0},
        "input_form": {"fields": [{"name": "question", "default": ""}]},
        "transform": {"columns": [], "filter": ""},
        "sql": {"query": "SELECT * FROM rows"},
        "router": {"condition": ""},
        "llm": {"model": "", "prompt": {"source": "inline", "name": "", "version": None, "system": "",
                                        "user": "{{ data }}"},
                "mode": "per_row", "batch_size": 20, "columns": [],
                "output": {"kind": "text", "column": "ai_answer", "fields": []},
                "temperature": None, "max_tokens": 512, "concurrency": 4, "retries": 1,
                "guardrails": dict(DEFAULT_GUARDRAILS)},
        "output_dataset": {"name": "", "include_flagged": True, "columns": []},
    }[node_type]


class WorkflowError(Exception):
    pass


class NeedsConfirmation(WorkflowError):
    def __init__(self, estimate: dict[str, Any]):
        super().__init__(f"This run may use about {estimate['tokens_expected']:,} tokens. Confirm to run it.")
        self.estimate = estimate


# ================================================================== CRUD


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80] or "workflow"


def list_workflows() -> list[Workflow]:
    with session_scope() as s:
        return list(s.scalars(select(Workflow).order_by(Workflow.name)))


def get_workflow(workflow_id: int) -> Workflow:
    with session_scope() as s:
        wf = s.get(Workflow, workflow_id)
        if not wf:
            raise WorkflowError("Workflow not found")
        return wf


def get_by_slug(slug: str) -> Workflow | None:
    with session_scope() as s:
        return s.scalars(select(Workflow).where(Workflow.slug == slug)).first()


def new_node(node_type: str, x: float = 40, y: float = 40, label: str = "", node_id: str | None = None,
             **config) -> dict[str, Any]:
    cfg = default_config(node_type)
    for k, v in config.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k] = {**cfg[k], **v}
        else:
            cfg[k] = v
    return {"id": node_id or f"{node_type[:3]}{int(time.time() * 1000) % 10_000_000}", "type": node_type,
            "label": label or NODE_TYPES[node_type]["label"], "x": x, "y": y, "config": cfg}


def template_spec(template: str) -> dict[str, Any]:
    """Starting graphs offered in the New workflow dialog."""
    if template == "classify":
        return {"nodes": [
            new_node("input_rows", 16, 120, "Rows", "in"),
            new_node("llm", 216, 120, "Classify", "classify",
                     prompt={"system": "You classify customer records. Be concise and use only the data given.",
                             "user": "Classify this record.\n\n{{ data }}"},
                     output={"kind": "json", "column": "", "fields": [
                         {"name": "category", "type": "enum", "enum": ["billing", "shipping", "product", "other"],
                          "required": True},
                         {"name": "urgency", "type": "integer", "required": True, "description": "1 (low) to 5"},
                         {"name": "reason", "type": "string", "required": False}]},
                     guardrails={"rules": [{"formula": "AND([urgency] >= 1, [urgency] <= 5)",
                                            "message": "urgency must be 1 to 5"}]}),
            new_node("router", 416, 120, "Urgent?", "route", condition="[urgency] >= 4"),
            new_node("output_dataset", 616, 30, "Urgent items", "urgent", name="Urgent items"),
            new_node("output_dataset", 616, 230, "Other items", "other", name="Other items"),
        ], "edges": [{"from": "in", "to": "classify", "port": "out"}, {"from": "classify", "to": "route", "port": "out"},
                     {"from": "route", "to": "urgent", "port": "true"}, {"from": "route", "to": "other", "port": "false"}],
            "limits": dict(DEFAULT_LIMITS)}
    if template == "summarize":
        return {"nodes": [
            new_node("input_rows", 16, 120, "Rows", "in"),
            new_node("sql", 216, 120, "Totals", "agg", query="SELECT * FROM rows LIMIT 200"),
            new_node("llm", 416, 120, "Summarize", "sum", mode="batch", batch_size=200,
                     prompt={"system": "You are a data analyst. Summarize for a business reader in 5 bullet points. "
                                       "Use only the numbers given.",
                             "user": "Summarize this table:\n\n{{ data }}"},
                     output={"kind": "text", "column": "summary", "fields": []}),
            new_node("output_dataset", 616, 120, "Summary", "out", name="Summary"),
        ], "edges": [{"from": "in", "to": "agg", "port": "out"}, {"from": "agg", "to": "sum", "port": "out"},
                     {"from": "sum", "to": "out", "port": "out"}], "limits": dict(DEFAULT_LIMITS)}
    if template == "ask":
        return {"nodes": [
            new_node("input_form", 16, 120, "Question", "in"),
            new_node("llm", 216, 120, "Answer", "answer",
                     prompt={"system": "Answer briefly and factually.", "user": "{{ data }}"},
                     output={"kind": "text", "column": "answer", "fields": []}),
            new_node("output_dataset", 416, 120, "Answers", "out", name="Answers"),
        ], "edges": [{"from": "in", "to": "answer", "port": "out"}, {"from": "answer", "to": "out", "port": "out"}],
            "limits": dict(DEFAULT_LIMITS)}
    return {"nodes": [], "edges": [], "limits": dict(DEFAULT_LIMITS)}


def create_workflow(name: str, actor: str, template: str = "blank", description: str = "") -> Workflow:
    name = (name or "").strip()
    if not name:
        raise WorkflowError("Give the workflow a name")
    with session_scope() as s:
        if s.scalars(select(Workflow).where(Workflow.name == name)).first():
            raise WorkflowError(f"A workflow named {name!r} already exists")
        slug, n = slugify(name), 1
        while s.scalars(select(Workflow).where(Workflow.slug == slug)).first():
            n += 1
            slug = f"{slugify(name)}-{n}"
        wf = Workflow(name=name, slug=slug, description=description, spec=template_spec(template), owner=actor,
                      updated_by=actor)
        s.add(wf)
        s.flush()
        wid = wf.id
    audit(actor, "ai.workflow.create", name)
    return get_workflow(wid)


def save_spec(workflow_id: int, spec: dict[str, Any], actor: str, name: str | None = None,
              description: str | None = None) -> Workflow:
    with session_scope() as s:
        wf = s.get(Workflow, workflow_id)
        if not wf:
            raise WorkflowError("Workflow not found")
        wf.spec = json.loads(json.dumps(spec))  # detach from the caller's objects
        if name:
            wf.name = name.strip()
        if description is not None:
            wf.description = description
        wf.has_draft_changes, wf.updated_by = True, actor
    return get_workflow(workflow_id)


def publish(workflow_id: int, user: User) -> Workflow:
    wf = get_workflow(workflow_id)
    problems = validate(wf.spec, user)
    if problems:
        raise WorkflowError("Fix these before publishing: " + "; ".join(problems))
    with session_scope() as s:
        w = s.get(Workflow, workflow_id)
        w.published_spec = json.loads(json.dumps(w.spec))
        w.published_version += 1
        w.has_draft_changes = False
        version = w.published_version
    audit(user.username, "ai.workflow.publish", wf.name, f"v{version}")
    return get_workflow(workflow_id)


def delete_workflow(workflow_id: int, actor: str) -> None:
    with session_scope() as s:
        wf = s.get(Workflow, workflow_id)
        if not wf:
            return
        name = wf.name
        run_ids = list(s.scalars(select(WorkflowRun.id).where(WorkflowRun.workflow_id == workflow_id)))
        for nr in s.scalars(select(NodeRun).where(NodeRun.run_id.in_(run_ids))) if run_ids else []:
            s.delete(nr)
        for r in s.scalars(select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)):
            s.delete(r)
        s.delete(wf)
    audit(actor, "ai.workflow.delete", name)


def runs_for(workflow_id: int | None = None, limit: int = 50) -> list[WorkflowRun]:
    with session_scope() as s:
        q = select(WorkflowRun).order_by(WorkflowRun.id.desc()).limit(limit)
        if workflow_id:
            q = q.where(WorkflowRun.workflow_id == workflow_id)
        return list(s.scalars(q))


def get_run(run_id: int) -> tuple[WorkflowRun, list[NodeRun]]:
    with session_scope() as s:
        run = s.get(WorkflowRun, run_id)
        if not run:
            raise WorkflowError("Run not found")
        nodes = list(s.scalars(select(NodeRun).where(NodeRun.run_id == run_id).order_by(NodeRun.id)))
        return run, nodes


def run_frames(run: WorkflowRun) -> tuple[pl.DataFrame | None, pl.DataFrame | None]:
    out = pl.read_parquet(run.output_path) if run.output_path else None
    review = pl.read_parquet(run.review_path) if run.review_path else None
    return out, review


# ================================================================== graph helpers


def _nodes(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["id"]: n for n in spec.get("nodes", [])}


def _incoming(spec: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    return [e for e in spec.get("edges", []) if e["to"] == node_id]


def topo_order(spec: dict[str, Any]) -> list[str]:
    nodes = _nodes(spec)
    indeg = {nid: 0 for nid in nodes}
    for e in spec.get("edges", []):
        if e["to"] in indeg and e["from"] in nodes:
            indeg[e["to"]] += 1
    order, ready = [], [nid for nid, d in indeg.items() if d == 0]
    while ready:
        nid = ready.pop(0)
        order.append(nid)
        for e in spec.get("edges", []):
            if e["from"] == nid and e["to"] in indeg:
                indeg[e["to"]] -= 1
                if indeg[e["to"]] == 0:
                    ready.append(e["to"])
    if len(order) != len(nodes):
        raise WorkflowError("The workflow has a loop; connections must flow one way")
    return order


def _llm_template(cfg: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """(system, user, prompt info) for an LLM node, from inline text or the prompt library."""
    p = cfg.get("prompt") or {}
    if p.get("source") == "library":
        try:
            snap = prompt_svc.published(p.get("name") or "", p.get("version") or None)
        except prompt_svc.PromptError as e:
            raise WorkflowError(str(e)) from e
        return snap.get("system", ""), snap.get("user", ""), {"name": p.get("name"), "version": snap["version"],
                                                              "hash": snap["content_hash"][:12]}
    return p.get("system", ""), p.get("user", ""), {"name": "inline"}


def _network(user: User, ref: str) -> str:
    try:
        return llm._resolve(ref, user)[3]
    except llm.AIError:
        return "external"


def validate(spec: dict[str, Any], user: User) -> list[str]:
    """Structural and policy checks that don't need data (the dry run adds the data checks)."""
    problems = []
    nodes = _nodes(spec)
    if not nodes:
        return ["Add at least one input and one output node"]
    try:
        topo_order(spec)
    except WorkflowError as e:
        problems.append(str(e))
    models = {m.ref for m in llm.available_models(user)}
    for nid, n in nodes.items():
        t, cfg, label = n["type"], n.get("config", {}), n.get("label") or nid
        meta = NODE_TYPES.get(t)
        if not meta:
            problems.append(f"{label}: unknown node type {t}")
            continue
        incoming = _incoming(spec, nid)
        if meta["inputs"] and not incoming:
            problems.append(f"{label}: connect an input to it")
        if t == "input_rows" and not (cfg.get("source_id") or cfg.get("mapping_id")):
            problems.append(f"{label}: choose a source or dataset")
        if t == "router" and not cfg.get("condition"):
            problems.append(f"{label}: enter a condition")
        if t == "output_dataset" and not (cfg.get("name") or "").strip():
            problems.append(f"{label}: name the output dataset")
        if t == "llm":
            ref = cfg.get("model") or ""
            if not ref:
                problems.append(f"{label}: choose a model")
            elif ref not in models:
                problems.append(f"{label}: you can't use model {ref} (check Models)")
            try:
                system, user_t, _ = _llm_template(cfg)
                variables_in(system, user_t)
                leaked = set(variables_in(system)) & {"data", "row", "rows", "rows_table", "input"}
                if leaked:
                    problems.append(f"{label}: data variables ({', '.join(sorted(leaked))}) belong in the user "
                                    "prompt, never in the system prompt")
            except (WorkflowError, PromptTemplateError) as e:
                problems.append(f"{label}: {e}")
            out = cfg.get("output") or {}
            if out.get("kind") == "json" and not out.get("fields"):
                problems.append(f"{label}: add at least one output field")
            if ref and (cfg.get("guardrails") or {}).get("pii_mode") == "off" and _network(user, ref) == "external":
                problems.append(f"{label}: sending PII as is is only allowed to internal models")
    return problems


# ================================================================== execution


@dataclass
class Ctx:
    user: User
    run_input: dict[str, Any]
    dry: bool = False
    row_limit: int = 0
    keep_samples: bool = False
    limits: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    masker: gr.Masker = field(default_factory=gr.Masker)
    events: Counter = field(default_factory=Counter)
    rejects: list[pl.DataFrame] = field(default_factory=list)
    flagged: list[pl.DataFrame] = field(default_factory=list)  # snapshots of flagged rows, for the review table
    classification: dict[str, str] = field(default_factory=dict)
    tokens: int = 0
    calls: int = 0
    estimate: dict[str, Any] = field(default_factory=lambda: {"calls": 0, "prompt_tokens": 0, "max_output_tokens": 0,
                                                              "nodes": {}, "egress": {}, "warnings": []})
    outputs: list[tuple[str, pl.DataFrame]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    run_id: int | None = None
    source_ids: list[int] = field(default_factory=list)


@dataclass
class NodeStat:
    rows_in: int = 0
    rows_out: int = 0
    flagged: int = 0
    rejected: int = 0
    calls: int = 0
    tokens: int = 0
    events: Counter = field(default_factory=Counter)
    samples: list[dict[str, Any]] = field(default_factory=list)


def _with_hidden(df: pl.DataFrame) -> pl.DataFrame:
    if "_row_id" not in df.columns:
        df = df.with_row_index("_row_id")
        df = df.with_columns(pl.col("_row_id").cast(pl.Int64))
    if "_review" not in df.columns:
        df = df.with_columns(pl.lit("").alias("_review"))
    return df


def _visible(df: pl.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in HIDDEN]


def _classify(ctx: Ctx, column: str, level: str | None) -> None:
    """Records a column's level, keeping the most restrictive one seen (sources, renames and derived columns)."""
    if not level:
        return
    current = ctx.classification.get(column)
    if current is None or gr.LEVEL_RANK[level] > gr.LEVEL_RANK[current]:
        ctx.classification[column] = level


def _level_of(ctx: Ctx, columns) -> str | None:
    return gr.max_level(ctx.classification.get(c) for c in columns)


def _reject(ctx: Ctx, df: pl.DataFrame, node: dict[str, Any], reasons: dict[int, str], stat: NodeStat) -> pl.DataFrame:
    """Moves rows (by _row_id) out of the flow into the run's rejects."""
    if not reasons:
        return df
    ids = list(reasons)
    rej = df.filter(pl.col("_row_id").is_in(ids)).with_columns(
        pl.col("_row_id").replace_strict(reasons, default="", return_dtype=pl.String).alias("_reason"),
        pl.lit(node.get("label") or node["id"]).alias("_node"))
    with ctx.lock:
        ctx.rejects.append(rej)
    stat.rejected += rej.height
    return df.filter(~pl.col("_row_id").is_in(ids))


def _flag(ctx: Ctx, df: pl.DataFrame, node: dict[str, Any], reasons: dict[int, str], stat: NodeStat) -> pl.DataFrame:
    """Adds a review note to rows (they stay in the flow) and keeps a snapshot for the run's review table."""
    reasons = {rid: why for rid, why in reasons.items() if why}
    if not reasons:
        return df
    note = pl.col("_row_id").replace_strict(reasons, default="", return_dtype=pl.String)
    df = df.with_columns(pl.when(note != "").then(
        pl.when(pl.col("_review") == "").then(note).otherwise(pl.col("_review") + "; " + note))
        .otherwise(pl.col("_review")).alias("_review"))
    hit = df.filter(pl.col("_row_id").is_in(list(reasons)))
    stat.flagged += hit.height
    if hit.height:
        with ctx.lock:
            ctx.flagged.append(hit.with_columns(
                pl.col("_row_id").replace_strict(reasons, default="", return_dtype=pl.String).alias("_reason"),
                pl.lit(node.get("label") or node["id"]).alias("_node")))
    return df


def _apply_failures(ctx: Ctx, df: pl.DataFrame, node: dict[str, Any], failures: dict[int, str], action: str,
                    stat: NodeStat) -> pl.DataFrame:
    if not failures:
        return df
    if action == "fail":
        rid, reason = next(iter(failures.items()))
        raise gr.GuardrailError(f"{node.get('label')}: row {rid}: {reason}")
    if action == "reject":
        return _reject(ctx, df, node, failures, stat)
    return _flag(ctx, df, node, failures, stat)


# ------------------------------------------------------------------ inputs and logic


def _run_input_rows(ctx: Ctx, node: dict[str, Any], stat: NodeStat) -> pl.DataFrame:
    cfg = node["config"]
    if cfg.get("mapping_id"):
        m = map_svc.get_mapping(int(cfg["mapping_id"]))
        ds = map_svc.dataset_for(m.id)
        if not ds:
            raise WorkflowError(f"{node['label']}: mapping {m.name} has no published dataset")
        df = pl.read_parquet(ds.parquet_path)
        src_levels = src_svc.get_source(m.source_id).classification or {}
        # dataset columns are target fields: each gets the highest level of the source columns its rule reads
        spec = map_svc.spec_of(m, published=True)
        classification = {}
        for rule in spec.rules:
            level = gr.max_level(src_levels.get(c) for c in referenced_columns(rule.get("formula") or ""))
            if level and rule.get("target"):
                classification[rule["target"]] = level
        for col, level in src_levels.items():  # same-named passthrough columns
            classification.setdefault(col, level)
    else:
        src = src_svc.get_source(int(cfg["source_id"]))
        df = src_svc.load_snapshot(src.id)
        classification = src.classification or {}
    for col, level in classification.items():
        if col in df.columns:
            _classify(ctx, col, level)
    if cfg.get("columns"):
        missing = [c for c in cfg["columns"] if c not in df.columns]
        if missing:
            raise WorkflowError(f"{node['label']}: columns not in the data: {', '.join(missing)}")
        df = df.select(cfg["columns"])
    if (cfg.get("filter") or "").strip():
        df = df.filter(_formula(cfg["filter"], df, node).fill_null(False))
    if cfg.get("limit"):
        df = df.head(int(cfg["limit"]))
    if ctx.row_limit:
        df = df.head(ctx.row_limit)
    max_rows = int(ctx.limits.get("max_rows") or 0)
    if max_rows and df.height > max_rows:
        raise gr.GuardrailError(f"{node['label']}: {df.height:,} rows is over the workflow limit of {max_rows:,}. "
                                "Add a filter or limit, or raise the limit in the workflow settings.")
    return _with_hidden(df)


def _run_input_form(ctx: Ctx, node: dict[str, Any], stat: NodeStat) -> pl.DataFrame:
    fields = node["config"].get("fields") or []
    row = {f["name"]: ctx.run_input.get(f["name"], f.get("default", "")) for f in fields if f.get("name")}
    for k, v in ctx.run_input.items():
        row.setdefault(k, v)
    row = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in row.items()}
    return _with_hidden(pl.DataFrame([row]) if row else pl.DataFrame({"input": [""]}))


def _formula(text: str, df: pl.DataFrame, node: dict[str, Any]) -> pl.Expr:
    try:
        return compile_formula(text, set(_visible(df)))
    except FormulaError as e:
        raise WorkflowError(f"{node.get('label')}: formula {text!r}: {e}") from e


def _run_transform(ctx: Ctx, node: dict[str, Any], df: pl.DataFrame, stat: NodeStat) -> pl.DataFrame:
    cfg = node["config"]
    for c in cfg.get("columns") or []:
        if c.get("name") and c.get("formula"):
            df = df.with_columns(_formula(c["formula"], df, node).alias(c["name"]))
            _classify(ctx, c["name"], _level_of(ctx, referenced_columns(c["formula"])))
    if (cfg.get("filter") or "").strip():
        df = df.filter(_formula(cfg["filter"], df, node).fill_null(False))
    return df


def _check_select(sql: str) -> None:
    try:
        stmts = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise WorkflowError(f"SQL does not parse: {e}") from e
    if len(stmts) != 1 or not isinstance(stmts[0], (sqlglot.exp.Select, sqlglot.exp.Union)):
        raise WorkflowError("Only one SELECT query is allowed")
    bad = [getattr(sqlglot.exp, n) for n in ("Insert", "Update", "Delete", "Drop", "Create", "Command", "Copy",
                                            "Pragma", "Attach", "Set") if hasattr(sqlglot.exp, n)]
    if any(stmts[0].find(b) for b in bad):
        raise WorkflowError("Only read-only SELECT queries are allowed")
    for t in stmts[0].find_all(sqlglot.exp.Table):
        if t.name.lower() != "rows" and not isinstance(t.parent, sqlglot.exp.CTE):
            ctes = {c.alias_or_name.lower() for c in stmts[0].find_all(sqlglot.exp.CTE)}
            if t.name.lower() not in ctes:
                raise WorkflowError(f"Query the incoming rows as table 'rows' (found {t.name!r})")
    for fn in stmts[0].find_all(sqlglot.exp.Anonymous):
        if fn.name.lower().startswith(("read_", "glob")):
            raise WorkflowError("File functions are not allowed in workflow SQL")


def _sql_lineage(sql: str, input_cols: set[str]) -> tuple[dict[str, set[str]] | None, set[str]]:
    """Which input columns feed each output column, failing closed.

    Returns (lineage, fallback). lineage is None when the query can't be analysed reliably (subqueries, CTEs,
    unions, positional #n, COLUMNS(...), table aliases with column lists): every output column then counts as
    derived from *every* input column. Within a simple SELECT, any reference that isn't an input column name
    (a table alias used as a value, a struct, a renamed column) also counts as reading every input column.
    """
    tree = sqlglot.parse_one(sql, read="duckdb")
    exp = sqlglot.exp
    everything = set(input_cols)
    risky = [getattr(exp, n) for n in ("PositionalColumn", "Columns", "Subquery", "CTE", "Union", "Lateral",
                                       "Unnest", "Pivot") if hasattr(exp, n)]
    if (not isinstance(tree, exp.Select) or any(tree.find(r) for r in risky)
            or any(ta.args.get("columns") for ta in tree.find_all(exp.TableAlias))):
        return None, everything

    def refs(e) -> set[str]:
        names = {c.name for c in e.find_all(exp.Column)}
        if names - input_cols:  # alias-as-value, struct access, renamed columns: can't tell, assume everything
            return everything
        if any(not isinstance(st.parent, exp.Count) for st in e.find_all(exp.Star)):
            return everything
        return names

    out: dict[str, set[str]] = {}
    for e in tree.expressions:
        if isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)):
            if e.args.get("except") or e.args.get("replace") or e.args.get("rename"):
                return None, everything
            for c in input_cols:
                out[c] = {c}
            continue
        out[e.alias_or_name] = refs(e)
    return out, everything


def _run_sql(ctx: Ctx, node: dict[str, Any], df: pl.DataFrame, stat: NodeStat) -> pl.DataFrame:
    sql = node["config"].get("query") or "SELECT * FROM rows"
    _check_select(sql)
    cols = _visible(df)
    con = duckdb.connect()
    timer = threading.Timer(float(settings.ai_request_timeout or 120), con.interrupt)
    try:
        con.execute("SET enable_external_access = false")
        con.execute("SET threads = 2")
        con.execute("SET memory_limit = '1GB'")
        con.register("rows", df.select(cols + ["_review"]).to_arrow())
        timer.start()
        out = pl.from_arrow(con.execute(sql).arrow())
    except duckdb.Error as e:
        raise WorkflowError(f"{node['label']}: {e}") from e
    finally:
        timer.cancel()
        con.close()
    if isinstance(out, pl.Series):
        out = out.to_frame()
    max_rows = int(ctx.limits.get("max_rows") or 0)
    if max_rows and out.height > max_rows:
        raise gr.GuardrailError(f"{node['label']}: the query returned {out.height:,} rows, over the workflow limit "
                                f"of {max_rows:,}")
    lineage, all_refs = _sql_lineage(sql, set(cols))
    for col in out.columns:
        if col in HIDDEN:
            continue
        sources = lineage.get(col, all_refs) if lineage is not None else all_refs  # unknown -> everything
        _classify(ctx, col, _level_of(ctx, sources))
    if "_review" in out.columns:
        out = out.with_columns(pl.col("_review").cast(pl.String).fill_null(""))
    return _with_hidden(out.drop("_row_id") if "_row_id" in out.columns else out)


def _run_router(ctx: Ctx, node: dict[str, Any], df: pl.DataFrame, stat: NodeStat) -> dict[str, pl.DataFrame]:
    cond = _formula(node["config"].get("condition") or "TRUE", df, node).fill_null(False)
    mask = df.select(cond.alias("_m"))["_m"]
    return {"true": df.filter(mask), "false": df.filter(~mask)}


# ------------------------------------------------------------------ LLM node


def _row_text(values: dict[str, Any]) -> str:
    return "\n".join(f"{k}: {'' if v is None else v}" for k, v in values.items())


def _table_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    cols = list(rows[0])
    lines = [" | ".join(cols), " | ".join("---" for _ in cols)]
    for r in rows:
        lines.append(" | ".join("" if r.get(c) is None else str(r.get(c)).replace("|", "/").replace("\n", " ")
                                for c in cols))
    return "\n".join(lines)


class _Prepared:
    __slots__ = ("row_ids", "values", "system", "user", "problems", "flags", "originals")

    def __init__(self):
        self.row_ids: list[int] = []
        self.values: list[dict[str, Any]] = []
        self.originals: list[dict[str, Any]] = []
        self.system = self.user = ""
        self.problems: list[str] = []  # reject/fail before calling
        self.flags: list[str] = []


DATA_VARS = {"data", "row", "rows", "rows_table"}


def _run_llm(ctx: Ctx, node: dict[str, Any], df: pl.DataFrame, stat: NodeStat) -> pl.DataFrame:
    cfg = node["config"]
    label = node.get("label") or node["id"]
    g = {**DEFAULT_GUARDRAILS, **(cfg.get("guardrails") or {})}
    ref = cfg.get("model") or ""
    if not ref:
        raise WorkflowError(f"{label}: choose a model")
    network = _network(ctx.user, ref)
    system_t, user_t, prompt_info = _llm_template(cfg)
    try:
        system_vars, user_vars = set(variables_in(system_t)), set(variables_in(user_t))
    except PromptTemplateError as e:
        raise WorkflowError(f"{label}: prompt template: {e}") from e
    if system_vars & (DATA_VARS | {"input"}):
        raise WorkflowError(f"{label}: data variables ({', '.join(sorted(system_vars & (DATA_VARS | {'input'})))}) "
                            "belong in the user prompt, never in the system prompt")
    if not user_vars & DATA_VARS:
        user_t = (user_t + "\n\n{{ data }}").strip()
    out_cfg = cfg.get("output") or {"kind": "text", "column": "ai_answer"}
    fields = out_cfg.get("fields") or []
    json_out = out_cfg.get("kind") == "json"
    if json_out:
        system_t = (system_t + "\n\n" + gr.schema_instructions(fields)).strip()
    system_t = (system_t + "\n\n" + gr.DATA_NOTICE).strip()
    batch = cfg.get("mode") == "batch"
    batch_size = max(1, int(cfg.get("batch_size") or 20))
    max_out = int(cfg.get("max_tokens") or 512)
    attempts = 1 + min(max(int(cfg.get("retries") or 0), 0), 5)
    out_cols = [f["name"] for f in fields] if json_out else [out_cfg.get("column") or "ai_answer"]

    # ---- egress: which columns may be sent, which must be masked
    cols = [c for c in (cfg.get("columns") or _visible(df)) if c in df.columns]
    missing = [c for c in cfg.get("columns") or [] if c not in df.columns]
    if missing:
        raise WorkflowError(f"{label}: columns not in the incoming rows: {', '.join(missing)}")
    decision = gr.egress_decision(cols, ctx.classification, network, g["pii_mode"])
    ctx.estimate["egress"][label] = {"network": network, "send": decision.send, "mask": decision.mask,
                                     "blocked": decision.blocked}
    if decision.blocked:
        raise gr.GuardrailError(f"{label}: {', '.join(decision.blocked)} is classified confidential and can't be "
                                f"sent to an external model. Remove the column(s) or use an internal model.")
    if g["pii_mode"] == "off" and network == "external":
        raise gr.GuardrailError(f"{label}: sending PII as is is only allowed to internal models")
    mask_text = g["pii_mode"] in ("mask", "pseudonymize") or (g["pii_mode"] == "auto" and network == "external")

    inserted: dict[int, set[str]] = {}  # row id -> mask tokens the masker put into that row

    def screen(values: dict[str, Any], classified: bool, rid: int = -1) -> tuple[dict[str, Any], list[str], list[str]]:
        """Masks and checks one set of values. Returns (values to send, problems, flags)."""
        sent, problems, flags = {}, [], []
        for c, v in values.items():
            if classified and c in decision.mask:
                sent[c] = ctx.masker.mask_whole(v, c)
                stat.events["pii masked"] += 1 if v not in (None, "") else 0
            elif mask_text and isinstance(v, str):
                sent[c], n = ctx.masker.mask(v)
                stat.events["pii masked"] += n
            else:
                sent[c] = v
            if isinstance(sent[c], str) and sent[c] != v:
                before = set(gr.Masker.TOKEN.findall(v if isinstance(v, str) else ""))
                inserted.setdefault(rid, set()).update(set(gr.Masker.TOKEN.findall(sent[c])) - before)
        if g["pii_mode"] == "block" and any(gr.find_pii(v) for v in values.values() if isinstance(v, str)):
            problems.append("contains personal data (PII mode: block)")
            stat.events["pii blocked"] += 1
        hits = sorted({h for v in sent.values() if isinstance(v, str) for h in gr.detect_injection(v)})
        if hits and g["injection"] != "off":
            stat.events["injection detected"] += 1
            msg = f"possible prompt injection ({', '.join(hits)})"
            (problems if g["injection"] in ("reject", "fail") else flags).append(msg)
        return sent, problems, flags

    # ---- run input ({{ input.x }}) goes through the same checks; a problem there affects every call
    safe_input, input_flags = ctx.run_input, []
    if "input" in user_vars and ctx.run_input:
        flat = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in ctx.run_input.items()}
        safe_input, problems, input_flags = screen(flat, classified=False)
        if problems:
            raise gr.GuardrailError(f"{label}: run input {problems[0]}")

    # ---- screen every row first: rows with problems never reach the model
    records = df.select(["_row_id", *cols]).to_dicts()
    screened: list[tuple[int, dict[str, Any], list[str]]] = []  # (row id, sent values, flags)
    pre: dict[int, str] = {}
    for rec in records:
        rid = rec.pop("_row_id")
        sent, problems, flags = screen(rec, classified=True, rid=rid)
        if problems:
            if g["injection"] == "fail" and any("injection" in p for p in problems):
                raise gr.GuardrailError(f"{label}: row {rid + 1}: {problems[0]}")
            pre[rid] = "; ".join(problems)
        else:
            screened.append((rid, sent, flags + input_flags))

    groups = ([screened[i:i + batch_size] for i in range(0, len(screened), batch_size)] if batch
              else [[item] for item in screened])
    prepared: list[_Prepared] = []
    for group in groups:
        p = _Prepared()
        p.row_ids = [rid for rid, _, _ in group]
        p.values = [sent for _, sent, _ in group]
        p.flags = sorted({f for _, _, fl in group for f in fl})
        values = {"input": safe_input, "row": p.values[0] if not batch else {},
                  "rows": p.values, "rows_table": _table_text(p.values),
                  "data": gr.wrap_data(_table_text(p.values) if batch else _row_text(p.values[0]))}
        try:
            p.system, p.user = render(system_t, values), render(user_t, values)
        except PromptTemplateError as e:
            raise WorkflowError(f"{label}: prompt template: {e}") from e
        tokens = gr.estimate_tokens(p.system + p.user)
        if g.get("max_prompt_tokens") and tokens > int(g["max_prompt_tokens"]):
            stat.events["prompt too long"] += 1
            for rid in p.row_ids:
                pre[rid] = f"prompt is about {tokens:,} tokens (limit {int(g['max_prompt_tokens']):,})"
            continue
        prepared.append(p)
    df = _reject(ctx, df, node, pre, stat)  # never sent

    # ---- estimate
    est = ctx.estimate
    node_prompt_tokens = sum(gr.estimate_tokens(p.system + p.user) for p in prepared)
    est["calls"] += len(prepared)
    est["prompt_tokens"] += node_prompt_tokens
    est["max_output_tokens"] += len(prepared) * max_out
    est["nodes"][label] = {"calls": len(prepared), "prompt_tokens": node_prompt_tokens,
                           "max_output_tokens": len(prepared) * max_out, "network": network, "prompt": prompt_info}

    def batch_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
        schema = {"_row_id": pl.Int64, "batch": pl.Int64, "rows": pl.Int64, **{c: pl.String for c in out_cols},
                  "_review": pl.String}
        return pl.DataFrame(rows, schema=schema, strict=False) if rows else pl.DataFrame(schema=schema)

    if ctx.dry:
        if batch:
            return batch_frame([{"_row_id": i, "batch": i + 1, "rows": len(p.row_ids), "_review": "; ".join(p.flags)}
                                for i, p in enumerate(prepared)])
        df = df.with_columns([pl.lit(None, dtype=pl.String).alias(c) for c in out_cols if c not in df.columns])
        return _flag(ctx, df, node, {rid: "; ".join(p.flags) for p in prepared for rid in p.row_ids}, stat)

    limit = int(ctx.limits.get("max_tokens_per_run") or 0)
    if limit and ctx.tokens + node_prompt_tokens > limit:
        raise gr.GuardrailError(f"{label}: the run would exceed its token limit ({limit:,})")

    results: dict[int, dict[str, Any]] = {}

    def call(i: int, p: _Prepared) -> None:
        if limit and ctx.tokens >= limit:
            with ctx.lock:
                results[i] = {"problems": [f"run token limit ({limit:,}) reached"], "values": {}}
            return
        user_msg, last = p.user, None
        for attempt in range(attempts):
            try:
                res = llm.chat(ctx.user, ref, user_msg, p.system, purpose="workflow",
                               temperature=cfg.get("temperature"), max_tokens=max_out, json_mode=json_out,
                               prompt_template=prompt_info.get("name") or "")
            except llm.AIError as e:
                with ctx.lock:
                    results[i] = {"problems": [f"model error: {e}"], "values": {}, "error": True}
                return
            if json_out:
                chk = gr.check_json_output(res.text, fields)
                values, problems = chk.values, list(chk.problems)
            else:
                values, problems = {out_cols[0]: res.text.strip()}, []
            problems += gr.check_text(res.text, p.user, g.get("banned") or [], int(g.get("max_output_chars") or 0),
                                      bool(g.get("pii_leak", True)))
            last = {"values": values, "problems": problems, "reply": res.text}
            with ctx.lock:
                ctx.calls += 1
                ctx.tokens += res.prompt_tokens + res.completion_tokens
                stat.calls += 1
                stat.tokens += res.prompt_tokens + res.completion_tokens
                if problems and attempt < attempts - 1:
                    stat.events["retried"] += 1
            if not problems or attempt == attempts - 1:
                break
            user_msg = (p.user + "\n\nYour previous reply was rejected: " + "; ".join(problems) +
                        ". Reply again, following the instructions exactly.")
        if g["pii_mode"] == "pseudonymize":  # only tokens this call was given can be restored
            allowed = set().union(*(inserted.get(rid, set()) for rid in p.row_ids), inserted.get(-1, set()))
            last["values"] = {k: ctx.masker.unmask(v, allowed) if isinstance(v, str) else v
                              for k, v in last["values"].items()}
        with ctx.lock:
            results[i] = last
            if ctx.keep_samples and len(stat.samples) < 3:
                stat.samples.append({"rows": p.row_ids[:5], "system": p.system[:3000], "user": user_msg[:4000],
                                     "reply": (last.get("reply") or "")[:4000], "problems": last["problems"]})

    with ThreadPoolExecutor(max_workers=max(1, min(int(cfg.get("concurrency") or 4), 16))) as pool:
        list(pool.map(lambda a: call(*a), list(enumerate(prepared))))

    errors = [r for r in results.values() if r.get("error")]
    if prepared and len(errors) == len(prepared):
        raise WorkflowError(f"{label}: every model call failed: {errors[0]['problems'][0]}")

    column_values = {r["column"]: {str(v).strip().lower() for v in df[r["column"]].to_list() if v is not None}
                     for r in g.get("grounding") or [] if r.get("source") == "column" and r.get("column") in df.columns}
    originals = {rec["_row_id"]: rec for rec in df.select(["_row_id", *[c for c in cols if c in df.columns]]).to_dicts()}

    def problems_for(i: int, p: _Prepared) -> tuple[dict[str, Any], list[str]]:
        r = results.get(i, {"values": {}, "problems": ["no reply"]})
        problems = list(r.get("problems") or [])
        if g.get("grounding") and r.get("values") and not batch:
            problems += gr.check_grounding(r["values"], g["grounding"], originals.get(p.row_ids[0], {}), column_values)
        if problems:
            stat.events["output invalid"] += 1
        return r.get("values") or {}, problems

    if batch:
        rows, failures = [], {}
        for i, p in enumerate(prepared):
            values, problems = problems_for(i, p)
            rows.append({"_row_id": i, "batch": i + 1, "rows": len(p.row_ids), "_review": "",
                         **{c: values.get(c) for c in out_cols}})
            if problems:
                failures[i] = "; ".join(problems)
        result = batch_frame(rows)
        result = _flag(ctx, result, node, {i: "; ".join(p.flags) for i, p in enumerate(prepared)}, stat)
        result = _apply_failures(ctx, result, node, failures, g["on_invalid"], stat)
        return _apply_rules(ctx, node, result, g, stat)

    rows, failures, flags = [], {}, {}
    for i, p in enumerate(prepared):
        values, problems = problems_for(i, p)
        rid = p.row_ids[0]
        rows.append({"_row_id": rid, **{c: values.get(c) for c in out_cols}})
        if problems:
            failures[rid] = "; ".join(problems)
        if p.flags:
            flags[rid] = "; ".join(p.flags)
    schema = {"_row_id": pl.Int64, **{c: pl.String for c in out_cols}}
    res_df = pl.DataFrame(rows, strict=False, infer_schema_length=None) if rows else pl.DataFrame(schema=schema)
    res_df = res_df.with_columns(pl.col("_row_id").cast(pl.Int64))
    df = df.drop([c for c in out_cols if c in df.columns]).join(res_df, on="_row_id", how="left")
    df = _flag(ctx, df, node, flags, stat)
    df = _apply_failures(ctx, df, node, failures, g["on_invalid"], stat)
    return _apply_rules(ctx, node, df, g, stat)


def _apply_rules(ctx: Ctx, node: dict[str, Any], df: pl.DataFrame, g: dict[str, Any], stat: NodeStat) -> pl.DataFrame:
    """Formula rules on the node's output (e.g. AND([urgency] >= 1, [urgency] <= 5))."""
    failures: dict[int, str] = {}
    for rule in g.get("rules") or []:
        text = (rule.get("formula") or "").strip()
        if not text or df.is_empty():
            continue
        ok = df.select(_formula(text, df, node).fill_null(False).alias("_ok"), "_row_id")
        for rid in ok.filter(~pl.col("_ok"))["_row_id"].to_list():
            failures.setdefault(rid, rule.get("message") or f"rule failed: {text}")
    if failures:
        stat.events["rule failed"] += len(failures)
    return _apply_failures(ctx, df, node, failures, g["on_invalid"], stat)


# ------------------------------------------------------------------ outputs


def _clean_output(df: pl.DataFrame, include_flagged: bool, columns: list[str]) -> pl.DataFrame:
    if not include_flagged:
        df = df.filter(pl.col("_review") == "")
    keep = [c for c in (columns or _visible(df)) if c in df.columns]
    out = df.select(keep)
    if include_flagged and (df["_review"] != "").any():
        out = out.with_columns(df["_review"].alias("ai_review"))
    return out


def _save_output_source(ctx: Ctx, node: dict[str, Any], df: pl.DataFrame, workflow: Workflow) -> int:
    name = node["config"]["name"].strip()
    with session_scope() as s:
        src = s.scalars(select(SourceObject).where(SourceObject.name == name)).first()
        if src and src.kind != "workflow":
            raise WorkflowError(f"Output name {name!r} is already used by a source that isn't a workflow output")
        if not src:
            src = SourceObject(name=name, kind="workflow", object_ref={"workflow_id": workflow.id, "node": node["id"]},
                               fields=[])
            s.add(src)
            s.flush()
        src.classification = {c: lvl for c, lvl in ctx.classification.items() if c in df.columns} or None
        src_id = src.id
    src_svc._write_snapshot(src_svc.get_source(src_id), df, profile_fields(df), f"workflow run {ctx.run_id}", None,
                            [f"Written by workflow {workflow.name}, run {ctx.run_id}"])
    return src_id


# ------------------------------------------------------------------ driver


def _execute(spec: dict[str, Any], ctx: Ctx, workflow: Workflow | None, on_node=None) -> dict[str, NodeStat]:
    nodes = _nodes(spec)
    produced: dict[tuple[str, str], pl.DataFrame] = {}
    stats: dict[str, NodeStat] = {}
    for nid in topo_order(spec):
        node = nodes[nid]
        t = node["type"]
        stat = stats[nid] = NodeStat()
        started = time.perf_counter()
        incoming = [produced[(e["from"], e.get("port") or "out")] for e in _incoming(spec, nid)
                    if (e["from"], e.get("port") or "out") in produced]
        if NODE_TYPES[t]["inputs"] and not incoming:
            if on_node:
                on_node(node, stat, 0, "skipped")
            continue
        df_in = incoming[0] if len(incoming) == 1 else None
        if len(incoming) > 1:  # merged branches: give every row a fresh id so results can't be joined to the wrong row
            df_in = pl.concat(incoming, how="diagonal_relaxed").drop("_row_id").with_row_index("_row_id")
            df_in = df_in.with_columns(pl.col("_row_id").cast(pl.Int64))
        stat.rows_in = df_in.height if df_in is not None else 0
        error = ""
        try:
            if t == "input_rows":
                out = {"out": _run_input_rows(ctx, node, stat)}
            elif t == "input_form":
                out = {"out": _run_input_form(ctx, node, stat)}
            elif t == "transform":
                out = {"out": _run_transform(ctx, node, df_in, stat)}
            elif t == "sql":
                out = {"out": _run_sql(ctx, node, df_in, stat)}
            elif t == "router":
                out = _run_router(ctx, node, df_in, stat)
            elif t == "llm":
                out = {"out": _run_llm(ctx, node, df_in, stat)}
            elif t == "output_dataset":
                cleaned = _clean_output(df_in, node["config"].get("include_flagged", True),
                                        node["config"].get("columns") or [])
                ctx.outputs.append((node["config"].get("name") or node["label"], cleaned))
                if not ctx.dry and workflow is not None:
                    ctx.source_ids.append(_save_output_source(ctx, node, cleaned, workflow))
                out = {}
            else:
                raise WorkflowError(f"Unknown node type {t}")
        except Exception as e:
            error = str(e)
            if on_node:
                on_node(node, stat, int((time.perf_counter() - started) * 1000), "failed", error)
            raise
        for port, frame in out.items():
            produced[(nid, port)] = frame
        stat.rows_out = sum(f.height for f in out.values()) if out else stat.rows_in
        for k, v in stat.events.items():
            ctx.events[k] += v
        if on_node:
            on_node(node, stat, int((time.perf_counter() - started) * 1000),
                    "warning" if stat.flagged or stat.rejected else "ok")
    return stats


def _owner_user(user: User | None, workflow: Workflow) -> User:
    if user is not None:
        return user
    with session_scope() as s:
        u = s.scalars(select(User).where(User.username == workflow.owner)).first()
        if not u or not u.active:
            raise WorkflowError("The workflow owner's account is not active")
        return u


def tokens_this_month(workflow_id: int) -> int:
    start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with session_scope() as s:
        return int(s.scalar(select(func.coalesce(func.sum(WorkflowRun.tokens), 0))
                            .where(WorkflowRun.workflow_id == workflow_id, WorkflowRun.started_at >= start)) or 0)


def estimate(workflow_id: int, user: User, run_input: dict[str, Any] | None = None, use_draft: bool = True,
             row_limit: int = 0) -> dict[str, Any]:
    """Dry run: executes inputs and logic, renders every prompt, applies input guardrails; no model calls."""
    wf = get_workflow(workflow_id)
    spec = wf.spec if use_draft or not wf.published_spec else wf.published_spec
    problems = validate(spec, user)
    if problems:
        raise WorkflowError("; ".join(problems))
    limits = {**DEFAULT_LIMITS, **(spec.get("limits") or {})}
    ctx = Ctx(user=user, run_input=run_input or {}, dry=True, row_limit=row_limit, limits=limits)
    _execute(spec, ctx, wf)
    e = ctx.estimate
    expected = e["prompt_tokens"] + int(e["max_output_tokens"] * 0.4)
    e.update(tokens_expected=expected, tokens_max=e["prompt_tokens"] + e["max_output_tokens"],
             guardrail_events=dict(ctx.events), rows_rejected_before_calls=sum(r.height for r in ctx.rejects),
             outputs={name: df.height for name, df in ctx.outputs},
             needs_confirmation=bool(limits.get("confirm_over_tokens")) and expected > int(limits["confirm_over_tokens"]))
    if limits.get("max_tokens_per_run") and e["prompt_tokens"] > int(limits["max_tokens_per_run"]):
        e["warnings"].append(f"Prompts alone are about {e['prompt_tokens']:,} tokens, over the run limit of "
                             f"{int(limits['max_tokens_per_run']):,}")
    monthly = int(limits.get("monthly_tokens") or 0)
    if monthly:
        used = tokens_this_month(workflow_id)
        e["monthly"] = {"used": used, "limit": monthly}
        if used + expected > monthly:
            e["warnings"].append(f"This month: {used:,} of {monthly:,} tokens used; this run may go over")
    return e


def run(workflow_id: int, user: User | None = None, run_input: dict[str, Any] | None = None, *,
        trigger: str = "manual", use_draft: bool = False, row_limit: int = 0, confirm: bool = False) -> WorkflowRun:
    """Runs a workflow (published version unless use_draft). Returns the finished WorkflowRun."""
    wf = get_workflow(workflow_id)
    if not use_draft and not wf.published_spec:
        raise WorkflowError("Publish the workflow first, or run the draft")
    spec = wf.spec if use_draft else wf.published_spec
    user = _owner_user(user, wf)
    if not can(user.role, "use_ai"):
        raise WorkflowError("Your role does not allow AI workflows")
    limits = {**DEFAULT_LIMITS, **(spec.get("limits") or {})}

    def blocked(reason: str) -> WorkflowRun:
        """Guardrails that stop a run before any model call still leave a record in the run history."""
        with session_scope() as s:
            r = WorkflowRun(workflow_id=wf.id, version=0 if use_draft else wf.published_version, trigger=trigger,
                            started_by=user.username, input=run_input or {}, status="blocked", error=reason,
                            finished_at=utcnow())
            s.add(r)
            s.flush()
            rid = r.id
        audit(user.username, "ai.workflow.blocked", wf.name, reason[:300])
        return get_run(rid)[0]

    try:
        est = estimate(workflow_id, user, run_input, use_draft=use_draft, row_limit=row_limit)
    except gr.GuardrailError as e:
        return blocked(str(e))
    monthly = int(limits.get("monthly_tokens") or 0)
    if monthly and tokens_this_month(workflow_id) >= monthly:
        return blocked(f"This workflow has used its monthly budget of {monthly:,} tokens")
    if est["needs_confirmation"] and not confirm:
        raise NeedsConfirmation(est)

    with session_scope() as s:
        r = WorkflowRun(workflow_id=wf.id, version=0 if use_draft else wf.published_version, trigger=trigger,
                        started_by=user.username, input=run_input or {}, estimate=_jsonable(est))
        s.add(r)
        s.flush()
        run_id = r.id
    ctx = Ctx(user=user, run_input=run_input or {}, row_limit=row_limit, limits=limits, run_id=run_id,
              keep_samples=trigger == "test" or settings.ai_log_content)

    def on_node(node, stat: NodeStat, ms: int, status: str, error: str = "") -> None:
        with session_scope() as s:
            s.add(NodeRun(run_id=run_id, node_id=node["id"], node_type=node["type"], label=node.get("label", ""),
                          status=status, rows_in=stat.rows_in, rows_out=stat.rows_out, flagged=stat.flagged,
                          rejected=stat.rejected, calls=stat.calls, tokens=stat.tokens, ms=ms,
                          events=dict(stat.events), samples=stat.samples, error=error))

    status, error = "ok", ""
    try:
        stats = _execute(spec, ctx, wf, on_node)
    except gr.GuardrailError as e:
        status, error, stats = "blocked", str(e), {}
    except Exception as e:  # noqa: BLE001 - recorded on the run
        status, error, stats = "failed", str(e) if isinstance(e, WorkflowError) else f"{type(e).__name__}: {e}", {}

    folder = settings.data_dir / "ai_runs" / str(run_id)
    folder.mkdir(parents=True, exist_ok=True)
    output_path = review_path = None
    if ctx.outputs:
        final = pl.concat([df.with_columns(pl.lit(name).alias("_output")) if len(ctx.outputs) > 1 else df
                           for name, df in ctx.outputs], how="diagonal_relaxed")
        output_path = str(folder / "output.parquet")
        final.write_parquet(output_path)
    flagged_frames = [f.drop("_row_id").rename({"_review": "ai_review"}).with_columns(pl.lit("flagged").alias("_status"))
                      for f in ctx.flagged]
    reject_frames = [r.drop("_row_id").rename({"_review": "ai_review"}).with_columns(pl.lit("rejected").alias("_status"))
                     for r in ctx.rejects]
    review = [f for f in flagged_frames + reject_frames if f.height]
    if review:
        review_df = pl.concat([f.with_columns(pl.all().cast(pl.String)) for f in review], how="diagonal_relaxed")
        first = [c for c in ("_status", "_node", "_reason", "ai_review") if c in review_df.columns]
        review_df = review_df.select(first + [c for c in review_df.columns if c not in first])
        review_path = str(folder / "review.parquet")
        review_df.write_parquet(review_path)
    flagged = sum(f.height for f in flagged_frames)
    rejected = sum(f.height for f in reject_frames)
    if status == "ok" and (flagged or rejected):
        status = "warning"
    rows_in = sum(st.rows_out for nid, st in stats.items() if _nodes(spec)[nid]["type"].startswith("input"))
    with session_scope() as s:
        r = s.get(WorkflowRun, run_id)
        r.status, r.error, r.finished_at = status, error, utcnow()
        r.rows_in, r.rows_out = rows_in, sum(df.height for _, df in ctx.outputs)
        r.rows_flagged, r.rows_rejected = flagged, rejected
        r.tokens, r.calls, r.guardrail_events = ctx.tokens, ctx.calls, dict(ctx.events)
        r.output_path, r.review_path = output_path, review_path
        r.output_source_ids = ctx.source_ids
    audit(user.username, "ai.workflow.run", wf.name, f"run {run_id} {status} ({trigger})")
    return get_run(run_id)[0]


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))


def api_result(run_obj: WorkflowRun, max_rows: int = 500) -> dict[str, Any]:
    out, review = run_frames(run_obj)
    return {"run_id": run_obj.id, "status": run_obj.status, "error": run_obj.error or None,
            "rows_in": run_obj.rows_in, "rows_out": run_obj.rows_out, "rows_flagged": run_obj.rows_flagged,
            "rows_rejected": run_obj.rows_rejected, "tokens": run_obj.tokens, "calls": run_obj.calls,
            "guardrail_events": run_obj.guardrail_events,
            "rows": map_svc.as_rows(out, max_rows) if out is not None else [],
            "review": map_svc.as_rows(review, 100) if review is not None else []}
