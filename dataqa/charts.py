"""Pick a chart from the *plan*, not from the question text.

The plan already says what the answer's shape is:
    scalar / row listing      -> no chart (the number or the table is the answer)
    time bucket + aggregates  -> line  (change over time)
    one category + aggregates -> bar   (magnitude by identity)
    time bucket + one category-> one line per category
    two categories            -> grouped bar
Suppressed only when a chart is meaningless (single row, a listing, no numeric measure,
more than two group columns) — never for size. The table is always shown beneath.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from dataqa.engine import QueryResult
from dataqa.plan import agg_alias, group_alias

# Validated categorical palette (dataviz skill, light surface), fixed order. Past eight series
# Plotly's default cycle takes over — if the user asked for it, messy is theirs to see.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]


def colour(i: int) -> str | None:
    return SERIES[i] if i < len(SERIES) else None  # None -> Plotly default sequence

_LAYOUT = dict(
    template="plotly_white",
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    margin=dict(l=8, r=8, t=8, b=8),
    height=360,
    xaxis=dict(showgrid=False, zeroline=False),
    yaxis=dict(gridcolor="rgba(128,128,128,0.15)", zeroline=False),
    hovermode="x unified",
    font=dict(size=13),
)


def chart_for(r: QueryResult) -> go.Figure | None:
    return chart_with_reason(r)[0]


def chart_with_reason(r: QueryResult, force: bool = False) -> tuple[go.Figure | None, str]:
    """(figure or None, one-line reason). The reason is shown to the user either way — a
    silently missing chart reads as broken.

    force=True: the user asked for a chart. Draw whatever is drawable — a single bar, a listing's
    first numeric column, three group columns concatenated on the axis. Only an empty result
    gets no chart. Meaningless is theirs to see; missing is ours to explain."""
    plan, df = r.plan, r.rows
    if force:
        return _force(r)
    if r.scalar is not None:
        return None, "No chart: the answer is a single number."
    if plan.is_listing:
        return None, "No chart: the result is a list of rows, not a grouped measure."
    if df.empty:
        return None, "No chart: no rows to plot."
    if len(df) == 1:
        return None, "No chart: single row."
    groups = plan.group_by
    aggs = [agg_alias(a) for a in plan.aggregates]
    if not groups:
        return None, "No chart: nothing is grouped, so there is only one value per measure."
    if not aggs:
        return None, "No chart: no numeric measure to plot."
    numeric = [a for a in aggs if a in df.columns and pd.api.types.is_numeric_dtype(df[a])]
    if not numeric:
        return None, "No chart: no numeric measure to plot."

    time_groups = [g for g in groups if g.bucket != "none"]
    cat_groups = [g for g in groups if g.bucket == "none"]
    if len(groups) > 2:
        return None, "No chart: grouped by more than two columns — there's no axis for a third. Try grouping by one or two."
    if len(time_groups) > 1:
        return None, "No chart: the result is grouped by two date periods at once."

    fig = _build(r, time_groups, cat_groups, aggs)
    if time_groups:
        why = f"Line chart: one value per {time_groups[0].bucket}" + (f", one line per {cat_groups[0].column}" if cat_groups else "") + "."
    elif len(cat_groups) == 2:
        why = f"Grouped bar chart: {cat_groups[0].column} on the axis, one colour per {cat_groups[1].column}."
    else:
        why = f"Bar chart: one category ({cat_groups[0].column}) with a numeric measure."
    return fig, why


def _force(r: QueryResult) -> tuple[go.Figure | None, str]:
    plan, df = r.plan, r.rows
    if df.empty:
        return None, "No chart: no rows to plot."
    numeric = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])]
    if not numeric:
        return None, "No chart: nothing numeric in the result to plot."
    groups = plan.group_by
    aggs = [agg_alias(a) for a in plan.aggregates if agg_alias(a) in numeric]
    time_groups = [g for g in groups if g.bucket != "none"]
    cat_groups = [g for g in groups if g.bucket == "none"]
    if groups and aggs and len(groups) <= 2 and len(time_groups) <= 1:
        fig = _build(r, time_groups, cat_groups, aggs)
        kind = "Line chart" if time_groups else "Bar chart"
        return fig, f"{kind}, as asked" + (" — a single value." if len(df) == 1 else ".")
    # anything else: bars of the numeric column(s) against a label built from the non-numeric ones
    labels = [c for c in df.columns if c not in numeric]
    x = df[labels].astype(str).agg(" · ".join, axis=1) if labels else df.index.astype(str)
    ys = aggs or numeric[:1]
    fig = go.Figure()
    for i, y in enumerate(ys):
        fig.add_bar(x=x, y=df[y], name=y, marker_color=colour(i))
    fig.update_layout(**{**_LAYOUT, "hovermode": "x"}, bargap=0.35, showlegend=len(ys) > 1, yaxis_title=ys[0] if len(ys) == 1 else None)
    what = "a single value" if len(df) == 1 else f"{len(df):,} rows of {', '.join(ys)}"
    return fig, f"Bar chart, as asked — {what}" + (f", labelled by {', '.join(labels)}" if labels else "") + "."


def _build(r: QueryResult, time_groups, cat_groups, aggs) -> go.Figure:
    plan, df = r.plan, r.rows
    groups = plan.group_by
    fig = go.Figure()
    if time_groups:
        x = group_alias(time_groups[0])
        if cat_groups:  # one line per category, one measure
            cat = group_alias(cat_groups[0])
            cats = list(dict.fromkeys(df[cat].astype(str)))
            for i, c in enumerate(cats):
                sub = df[df[cat].astype(str) == c].sort_values(x)
                fig.add_scatter(x=sub[x], y=sub[aggs[0]], name=c, mode="lines+markers",
                                line=dict(color=colour(i), width=2), marker=dict(size=8))
            fig.update_layout(showlegend=True, yaxis_title=aggs[0])
        else:  # one line per measure
            d = df.sort_values(x)
            for i, a in enumerate(aggs):
                fig.add_scatter(x=d[x], y=d[a], name=a, mode="lines+markers",
                                line=dict(color=colour(i), width=2), marker=dict(size=8))
            fig.update_layout(showlegend=len(aggs) > 1, yaxis_title=aggs[0] if len(aggs) == 1 else None)
        fig.update_layout(**_LAYOUT, xaxis_title=None)
        return fig

    # categorical bars
    x = group_alias(cat_groups[0])
    if len(cat_groups) == 2:  # grouped bars: colour = second category
        color = group_alias(cat_groups[1])
        cats = list(dict.fromkeys(df[color].astype(str)))
        for i, c in enumerate(cats):
            sub = df[df[color].astype(str) == c]
            fig.add_bar(x=sub[x].astype(str), y=sub[aggs[0]], name=c, marker_color=colour(i))
        fig.update_layout(barmode="group", showlegend=True, yaxis_title=aggs[0])
    else:
        for i, a in enumerate(aggs):
            fig.add_bar(x=df[x].astype(str), y=df[a], name=a, marker_color=colour(i))
        fig.update_layout(barmode="group", showlegend=len(aggs) > 1, yaxis_title=aggs[0] if len(aggs) == 1 else None)
    fig.update_traces(marker_line_width=0)
    if len(fig.data) == 1:
        fig.update_traces(width=0.6)  # thin single-series bars
    layout = {**_LAYOUT, "hovermode": "x"}
    fig.update_layout(**layout, bargap=0.35)
    return fig
