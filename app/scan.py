"""Scan definitions (YAML), universes, and scan evaluation: results, funnel, history."""

from __future__ import annotations

import math
import re
import time
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import builder, universes
from .config import SCANS_DIR
from .datastore import DataStore, MarketData
from .expr import Context, ExprError, evaluate, explain, k_and, k_at_least, k_not

# --------------------------------------------------------------------------- models

_MATCH_RE = re.compile(r"^(all|any\s*[:_ ]?\s*(\d+))$", re.IGNORECASE)


class FilterNode(BaseModel):
    expr: str | None = None
    # Point-and-click token tree from the UI. Turned into `expr`; never saved to YAML.
    tree: dict | None = Field(default=None, exclude=True)
    note: str | None = None
    enabled: bool = True
    group: "GroupNode | None" = None

    @model_validator(mode="after")
    def _one_kind(self):
        if self.tree is not None and self.expr is None:
            self.expr = builder.condition_expr(self.tree)
        if (self.expr is None) == (self.group is None):
            raise ValueError("Each filter needs either a condition or a group")
        return self


class GroupNode(BaseModel):
    match: str = "all"
    negate: bool = False
    filters: list[FilterNode] = Field(default_factory=list)

    @field_validator("match")
    @classmethod
    def _match(cls, v: str) -> str:
        m = _MATCH_RE.match(v.strip())
        if not m:
            raise ValueError("match must be 'all' or 'any N', for example 'any 2'")
        return "all" if m.group(1).lower() == "all" else f"any {int(m.group(2))}"

    def required(self) -> int | None:
        return None if self.match == "all" else int(self.match.split()[1])


class ColumnSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    expr: str | None = None
    tree: dict | None = Field(default=None, exclude=True)
    label: str | None = Field(default=None, alias="as")
    format: Literal["number", "percent", "integer"] = "number"
    decimals: int | None = None
    color: bool = False  # green when positive, red when negative

    @model_validator(mode="after")
    def _expr_from_tree(self):
        if self.tree is not None and self.expr is None:
            self.expr = builder.operand_expr(self.tree)
        if not self.expr:
            raise ValueError("Each column needs a value to show")
        return self


class SortSpec(BaseModel):
    by: str | None = None
    desc: bool = True


def _default_columns() -> list[ColumnSpec]:
    return [
        ColumnSpec(expr="close", label="Close", format="number", decimals=2),
        ColumnSpec(expr="pct_change", label="% Chg", format="percent", decimals=2, color=True),
        ColumnSpec(expr="volume", label="Volume", format="integer"),
    ]


class ScanDef(BaseModel):
    name: str = "Untitled scan"
    description: str = ""
    universe: str = "nifty500"
    group: GroupNode = Field(default_factory=GroupNode)
    columns: list[ColumnSpec] = Field(default_factory=_default_columns)
    sort: SortSpec = Field(default_factory=lambda: SortSpec(by="% Chg", desc=True))


FilterNode.model_rebuild()


class ScanError(ValueError):
    """A problem in a scan, optionally pointing at a filter (path of indexes) or column."""

    def __init__(self, message: str, path: list[int] | None = None, column: int | None = None):
        super().__init__(message)
        self.message = message
        self.path = path
        self.column = column


# --------------------------------------------------------------------------- scan files

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _scan_path(scan_id: str) -> Path:
    if not _ID_RE.match(scan_id):
        raise ScanError("Scan id may only contain lowercase letters, digits, '-' and '_'")
    return SCANS_DIR / f"{scan_id}.yaml"


def load_scan(scan_id: str) -> ScanDef:
    path = _scan_path(scan_id)
    if not path.exists():
        raise FileNotFoundError(f"Scan '{scan_id}' not found")
    return ScanDef.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def list_scans() -> list[dict]:
    out = []
    for path in sorted(SCANS_DIR.glob("*.yaml")):
        try:
            scan = ScanDef.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
            out.append({"id": path.stem, "name": scan.name, "description": scan.description,
                        "universe": scan.universe})
        except Exception as exc:  # show broken files instead of hiding them
            out.append({"id": path.stem, "name": path.stem, "description": f"Invalid scan file: {exc}",
                        "universe": None, "error": True})
    return out


def with_trees(scan: ScanDef) -> dict:
    """Scan as a dict, with a point-and-click token tree next to every filter and column."""
    data = scan.model_dump(by_alias=True)

    def annotate(group: dict) -> None:
        for f in group["filters"]:
            if f.get("group"):
                annotate(f["group"])
            else:
                f["tree"] = builder.condition_tree(f["expr"])

    annotate(data["group"])
    for c in data["columns"]:
        c["tree"] = builder.operand_tree(c["expr"])
    return data


def save_scan(scan_id: str, scan: ScanDef) -> None:
    path = _scan_path(scan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = scan.model_dump(by_alias=True, exclude_none=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=False), encoding="utf-8")


# --------------------------------------------------------------------------- universes


def resolve_universe(universe_id: str, data: MarketData) -> dict:
    """Return the universe's symbols that exist in Historify and the ones that do not."""
    if universe_id == universes.ALL_UNIVERSE:
        return {"id": universe_id, "name": "All Historify symbols", "symbols": None, "missing": [],
                "size": len(data.symbols)}
    if universe_id not in universes.INDEX_FILES:
        raise ScanError(f"Unknown universe '{universe_id}'")
    try:
        members = [c.symbol for c in universes.load(universe_id)]
    except FileNotFoundError as exc:
        raise ScanError(str(exc)) from None
    available = set(data.symbols)
    return {
        "id": universe_id,
        "name": universes.INDEX_FILES[universe_id][0],
        "symbols": [s for s in members if s in available],
        "missing": [s for s in members if s not in available],
        "size": len(members),
    }


def _company_info() -> dict[str, tuple[str, str]]:
    for uid in ("nifty500", "nifty200", "nifty100", "nifty50"):
        if universes.local_path(uid).exists():
            return {c.symbol: (c.company, c.industry) for c in universes.load(uid)}
    return {}


# --------------------------------------------------------------------------- evaluation


def _eval_filter(node: FilterNode, ctx: Context, path: list[int]) -> pd.DataFrame | None:
    if not node.enabled:
        return None
    if node.group is not None:
        return _eval_group(node.group, ctx, path)
    try:
        val = evaluate(node.expr, ctx)
    except ExprError as exc:
        raise ScanError(str(exc), path=path) from None
    if val.kind != "bool":
        raise ScanError(
            f"'{node.expr}' is a value, not a condition. Compare it, for example '{node.expr} > 0'", path=path
        )
    return ctx.as_frame(val)


def _combine(group: GroupNode, parts: list[pd.DataFrame]) -> pd.DataFrame:
    need = group.required()
    if need is None:
        out = parts[0]
        for p in parts[1:]:
            out = k_and(out, p)
    else:
        if need > len(parts):
            raise ScanError(f"'any {need}' needs at least {need} enabled filters, the group has {len(parts)}")
        out = k_at_least(parts, need)
    return k_not(out) if group.negate else out


def _eval_group(group: GroupNode, ctx: Context, path: list[int]) -> pd.DataFrame | None:
    parts = [p for i, f in enumerate(group.filters) if (p := _eval_filter(f, ctx, path + [i])) is not None]
    return _combine(group, parts) if parts else None


def resolve_as_of(dates: pd.DatetimeIndex, as_of: str | None) -> pd.Timestamp:
    if not as_of:
        return dates[-1]
    try:
        target = pd.Timestamp(as_of)
    except ValueError:
        raise ScanError(f"Invalid date '{as_of}', use YYYY-MM-DD") from None
    pos = dates.searchsorted(target, side="right") - 1
    if pos < 0:
        raise ScanError(f"No data on or before {as_of}; data starts {dates[0].date()}")
    return dates[pos]


def _clean(v):
    if v is None:
        return None
    if isinstance(v, (float, np.floating)):
        return None if math.isnan(v) else float(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    return v


def _explain_group(group: GroupNode, depth: int = 0) -> list[str]:
    verb = "fails" if group.negate else "passes"
    what = "all" if group.match == "all" else f"at least {group.required()}"
    lines = [("  " * depth) + f"Stock {verb} {what} of the following:"]
    n = 0
    for f in group.filters:
        if not f.enabled:
            continue
        n += 1
        pad = "  " * (depth + 1)
        if f.group is not None:
            lines.append(f"{pad}{n}. a sub-group:")
            lines.extend(_explain_group(f.group, depth + 2))
        else:
            try:
                lines.append(f"{pad}{n}. {explain(f.expr)}")
            except ExprError as exc:
                lines.append(f"{pad}{n}. (invalid: {exc})")
    if n == 0:
        lines.append(("  " * (depth + 1)) + "(no enabled filters: every stock passes)")
    return lines


def explain_scan(scan: ScanDef) -> str:
    return "\n".join(_explain_group(scan.group))


def _prepare(scan: ScanDef, store: DataStore, universe_override: str | None):
    data_all = store.data
    universe = resolve_universe(universe_override or scan.universe, data_all)
    data = store.for_symbols(universe["id"], universe["symbols"])
    if not data.symbols:
        raise ScanError(f"None of the {universe['name']} symbols are in Historify")
    ctx = Context(data)
    match = _eval_group(scan.group, ctx, [])
    active = data.frames["close"].notna()
    if match is None:
        match = active.astype(float)
    return data, ctx, universe, match, active


def run_scan(scan: ScanDef, store: DataStore, as_of: str | None = None, universe: str | None = None) -> dict:
    started = time.perf_counter()
    data, ctx, uni, match, active = _prepare(scan, store, universe)
    day = resolve_as_of(data.dates, as_of)

    active_row = active.loc[day]
    match_row = match.loc[day]
    matched = list(match_row[(match_row == 1) & active_row].index)
    insufficient = int((match_row.isna() & active_row).sum())
    n_active = int(active_row.sum())

    # Debug funnel over the top-level filters, like Chartink's debug badges.
    funnel = []
    cumulative = None
    cumulative_ok = scan.group.match == "all" and not scan.group.negate
    for i, f in enumerate(scan.group.filters):
        entry = {"index": i, "enabled": f.enabled, "expr": f.expr, "note": f.note,
                 "is_group": f.group is not None}
        frame = _eval_filter(f, ctx, [i])
        if frame is not None:
            passed = (frame.loc[day] == 1) & active_row
            entry["pass"] = int(passed.sum())
            entry["pass_pct"] = round(100 * entry["pass"] / n_active, 2) if n_active else 0.0
            if cumulative_ok:
                cumulative = passed if cumulative is None else cumulative & passed
                entry["cumulative"] = int(cumulative.sum())
                entry["cumulative_pct"] = round(100 * entry["cumulative"] / n_active, 2) if n_active else 0.0
        funnel.append(entry)

    # Result columns.
    labels, seen = [], set()
    for j, col in enumerate(scan.columns):
        label = col.label or col.expr
        while label in seen:
            label += "_"
        seen.add(label)
        labels.append(label)
    values: dict[str, pd.Series] = {}
    for j, col in enumerate(scan.columns):
        try:
            val = evaluate(col.expr, ctx)
        except ExprError as exc:
            raise ScanError(f"Column '{labels[j]}': {exc}", column=j) from None
        values[labels[j]] = ctx.as_frame(val).loc[day]

    info = _company_info()
    rows = []
    for sym in matched:
        company, industry = info.get(sym, ("", ""))
        rows.append({"symbol": sym, "company": company, "industry": industry,
                     "values": {lab: _clean(values[lab].get(sym)) for lab in labels}})

    sort_by = scan.sort.by if scan.sort.by in labels or scan.sort.by == "symbol" else None
    if sort_by == "symbol":
        rows.sort(key=lambda r: r["symbol"], reverse=scan.sort.desc)
    elif sort_by:
        rows.sort(key=lambda r: (r["values"][sort_by] is None,
                                 -(r["values"][sort_by] or 0) if scan.sort.desc else (r["values"][sort_by] or 0)))

    return {
        "scan": scan.name,
        "as_of": day.date().isoformat(),
        "requested_as_of": as_of,
        "universe": {k: uni[k] for k in ("id", "name", "size", "missing")},
        "universe_in_data": len(data.symbols),
        "active": n_active,
        "matched": len(rows),
        "insufficient_history": insufficient,
        "columns": [{"label": labels[j], "format": c.format, "decimals": c.decimals, "color": c.color}
                    for j, c in enumerate(scan.columns)],
        "rows": rows,
        "funnel": funnel,
        "explain": explain_scan(scan),
        "elapsed_ms": round(1000 * (time.perf_counter() - started), 1),
    }


PERIODS = {"3m": 3, "6m": 6, "9m": 9, "1y": 12, "2y": 24, "4y": 48, "6y": 72, "9y": 108}


def scan_history(scan: ScanDef, store: DataStore, period: str = "9m", universe: str | None = None,
                 as_of: str | None = None) -> dict:
    if period not in PERIODS:
        raise ScanError(f"Period must be one of {', '.join(PERIODS)}")
    started = time.perf_counter()
    data, _, uni, match, active = _prepare(scan, store, universe)
    end = resolve_as_of(data.dates, as_of)
    start = end - pd.DateOffset(months=PERIODS[period])
    window = (match.loc[start:end] == 1) & active.loc[start:end]

    per_day = window.sum(axis=1)
    per_stock = window.sum(axis=0)
    per_stock = per_stock[per_stock > 0].sort_values(ascending=False)
    last_hit = {s: window.index[window[s].to_numpy()][-1].date().isoformat() for s in per_stock.index}

    info = _company_info()
    industries: dict[str, int] = {}
    for sym, n in per_stock.items():
        ind = info.get(sym, ("", ""))[1] or "Unknown"
        industries[ind] = industries.get(ind, 0) + int(n)

    return {
        "scan": scan.name,
        "period": period,
        "start": window.index[0].date().isoformat() if len(window.index) else None,
        "end": end.date().isoformat(),
        "universe": {k: uni[k] for k in ("id", "name", "size", "missing")},
        "trading_days": int(len(per_day)),
        "days_with_signals": int((per_day > 0).sum()),
        "total_signals": int(per_day.sum()),
        "distinct_stocks": int(len(per_stock)),
        "max_per_day": int(per_day.max()) if len(per_day) else 0,
        "days": [{"date": d.date().isoformat(), "count": int(c)} for d, c in per_day.items()],
        "stocks": [{"symbol": s, "count": int(n), "last": last_hit[s], "industry": info.get(s, ("", ""))[1]}
                   for s, n in per_stock.items()],
        "industries": sorted(({"industry": k, "count": v} for k, v in industries.items()),
                             key=lambda x: -x["count"]),
        "elapsed_ms": round(1000 * (time.perf_counter() - started), 1),
    }
