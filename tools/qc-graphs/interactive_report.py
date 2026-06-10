#!/usr/bin/env python3
"""
BSRN QC — Interactive HTML Report Generator
=============================================
Generates a fully self-contained interactive HTML report using Plotly.
Works on JupyterHub, local Jupyter, and anywhere else — no server required.

Called automatically by run_notebook() / run_notebook_batch() in bsrn_qc.py.
Can also be imported and called directly:

    from interactive_report import generate_interactive_report, show_notebook_link
    path = generate_interactive_report(df, metadata, qc_summary, output_path, lr4000)
    show_notebook_link(path)
"""

from pathlib import Path
import datetime as dt
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.io as pio

# ── Shared colour scheme (matches bsrn_qc.py tab10 palette) ─────────────────
TAB10 = ["#1f77b4","#ff7f0e","#2ca02c","#d62728",
         "#9467bd","#8c564b","#e377c2","#7f7f7f","#bcbd22","#17becf"]
PARAM_COLOR = {
    "DIF": TAB10[0], "DIR": TAB10[1], "SWD": TAB10[2],
    "LWD": TAB10[3], "SWU": TAB10[4], "LWU": TAB10[5],
    "T2":  TAB10[6], "P":   TAB10[7], "RH":  TAB10[8],
}
BASE_LAYOUT = dict(
    template="plotly_white",
    margin=dict(l=60, r=20, t=45, b=40),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    font=dict(family="Inter, Arial, sans-serif", size=12),
)

# QC flag definitions: (bit mask, colour, short label, long label, bit value)
# The bit value is the real flag integer; fig_radiation_with_flags maps these
# to evenly-spaced display positions 1–6 via _BIT_TO_Y so axis labels don't overlap.
_FLAG_DEFS = [
    (1 << 0, "darkviolet", "PP−",  "< Phys. Possible",  1),
    (1 << 1, "darkviolet", "PP+",  "> Phys. Possible",  2),
    (1 << 2, "darkorange", "ER−",  "< Extremely Rare",  4),
    (1 << 3, "darkorange", "ER+",  "> Extremely Rare",  8),
    (1 << 4, "steelblue",  "Cmp−", "< Comparison",     16),
    (1 << 5, "tomato",     "Cmp+", "> Comparison",     32),
]
_QC_MAP = {
    "SWD": "SWDQc", "DIF": "DIFQc", "DIR": "DIRQc",
    "LWD": "LWDQc", "SWU": "SWUQc", "LWU": "LWUQc",
}


# =============================================================================
# CHART BUILDERS  (each returns a go.Figure)
# =============================================================================

def fig_world_map(metadata):
    """Small world map with a star marker at the station position."""
    lat = metadata.get("latitude")
    lon = metadata.get("longitude")
    if lat is None or lon is None:
        return None
    try:
        if np.isnan(lat) or np.isnan(lon):
            return None
    except (TypeError, ValueError):
        return None

    station = metadata.get("station_name", "Station")
    code    = metadata.get("station_code", "")
    elv     = metadata.get("elevation")
    elv_str = f"{elv} m" if elv is not None else "—"

    hover = (f"<b>{station} ({code})</b><br>"
             f"Lat: {lat:.3f}°<br>Lon: {lon:.3f}°<br>"
             f"Elevation: {elv_str}<extra></extra>")

    fig = go.Figure(go.Scattergeo(
        lat=[lat], lon=[lon],
        mode="markers+text",
        marker=dict(size=14, color="crimson", symbol="star",
                    line=dict(color="darkred", width=1.5)),
        text=[f"  {code}"],
        textposition="middle right",
        textfont=dict(size=11, color="crimson",
                      family="Inter, Arial, sans-serif"),
        hovertemplate=hover,
        showlegend=False,
    ))

    fig.update_geos(
        projection_type="natural earth",
        showland=True,        landcolor="#dbe9d8",
        showocean=True,       oceancolor="#b8d9ea",
        showlakes=True,       lakecolor="#b8d9ea",
        showcoastlines=True,  coastlinecolor="#888", coastlinewidth=0.6,
        showcountries=True,   countrycolor="#bbb",   countrywidth=0.5,
        showframe=False,
        bgcolor="white",
    )
    fig.update_layout(
        height=230,
        margin=dict(l=0, r=0, t=30, b=0),
        paper_bgcolor="white",
        title=dict(
            text=f"📍 {station} ({code})  ·  {lat:.3f}°N, {lon:.3f}°E  ·  {elv_str}",
            font=dict(size=11, family="Inter, Arial, sans-serif"),
            x=0.5, xanchor="center",
        ),
    )
    return fig


def fig_radiation_with_flags(df, metadata):
    """
    Interleaved radiation time series + QC flag panels, one pair per component.
    Single-column, full-width layout — x-axes are shared so zoom is synchronised.
    Layout (per component):
      • Row A  — full-width time series
      • Row B  — full-width QC flags  (y = 0–32, one level per flag bit)
    """
    rad_cols = ["DIF", "DIR", "SWD", "LWD"]
    if metadata.get("has_upward"):
        rad_cols += ["SWU", "LWU"]
    available = [c for c in rad_cols if c in df.columns and df[c].notna().any()]
    if not available:
        return None

    n = len(available)

    # One title per subplot row: timeseries title then flags title, per parameter
    subplot_titles = []
    for col in available:
        subplot_titles.extend([f"{col}  —  time series", f"{col}  —  QC flags"])

    # Row heights: data rows taller, flag rows shorter but clearly readable
    # 200 px for time series, 120 px for flags  →  ratio 5:3
    row_heights = [h for _ in available for h in [5, 3]]
    total_height = n * (200 + 120)

    fig = make_subplots(
        rows=2 * n, cols=1,
        shared_xaxes=True,          # link all date axes — zoom one, zoom all
        vertical_spacing=0.03,
        row_heights=row_heights,
        subplot_titles=subplot_titles,
    )

    legend_flags_shown = set()

    for i, col in enumerate(available, start=1):
        ts_row   = 2 * i - 1
        flag_row = 2 * i
        color    = PARAM_COLOR.get(col, TAB10[0])
        # Resample to the native 1-min grid so missing timestamps become explicit
        # NaN rows — Plotly then breaks the line at every gap automatically.
        data     = df[col].resample("1min").first()

        # ── Time series ──────────────────────────────────────────────────────
        fig.add_trace(go.Scattergl(
            x=data.index, y=data.values, mode="lines",
            line=dict(color=color, width=0.7),
            name=col, legendgroup=col, showlegend=False,
            hovertemplate=(f"<b>%{{x|%Y-%m-%d %H:%M}}</b>"
                           f"<br>{col}: %{{y:.2f}} W/m²<extra></extra>"),
        ), row=ts_row, col=1)
        fig.update_yaxes(title_text="W/m²", row=ts_row, col=1)

        # ── QC flags ─────────────────────────────────────────────────────────
        # Flags are plotted at evenly-spaced y-positions 1–6 so tick labels
        # never overlap, while the labels themselves show the true bit values.
        # Mapping: bit-value → display y-position
        _BIT_TO_Y = {1: 1, 2: 2, 4: 3, 8: 4, 16: 5, 32: 6}

        qc_col = _QC_MAP.get(col)
        if qc_col and qc_col in df.columns:
            vals = df[qc_col].fillna(0).values.astype(int)
            for bit, fc, short, long_label, y_val in _FLAG_DEFS:
                mask = (vals & bit).astype(bool)
                if not mask.any():
                    continue
                # Show this flag type in the legend on its first appearance
                show_in_legend = short not in legend_flags_shown
                if show_in_legend:
                    legend_flags_shown.add(short)
                y_display = float(_BIT_TO_Y[y_val])
                fig.add_trace(go.Scattergl(
                    x=df.index[mask],
                    y=np.full(mask.sum(), y_display),
                    mode="markers",
                    marker=dict(color=fc, size=5, opacity=1.0,
                                symbol="line-ns-open",
                                line=dict(width=1.5, color=fc)),
                    name=long_label, legendgroup=short,
                    showlegend=show_in_legend,
                    hovertemplate=(
                        f"<b>%{{x|%Y-%m-%d %H:%M}}</b>"
                        f"<br>{col} → {long_label}<extra></extra>"),
                ), row=flag_row, col=1)

        # Invisible baseline trace so Plotly always draws the axes and gridlines
        # even when no flags fired for this parameter (without it the subplot
        # appears as a blank white box).
        fig.add_trace(go.Scattergl(
            x=[data.index[0], data.index[-1]],
            y=[0, 0],
            mode="lines",
            line=dict(color="rgba(0,0,0,0)", width=0),
            showlegend=False, hoverinfo="skip",
        ), row=flag_row, col=1)

        # y-axis: 4 evenly-spaced labels (0, 8, 16, 32) mapped to positions
        # 0, 2, 4, 6 in the 0–7 range → ~34 px apart at 120 px row height
        fig.update_yaxes(
            range=[0, 7],
            tickvals=[0, 2, 4, 6],
            ticktext=["0", "8", "16", "32"],
            tickfont=dict(size=9),
            gridcolor="#e0e0e0",
            row=flag_row, col=1,
        )

    # Build layout without the keys we override explicitly below
    layout = {k: v for k, v in BASE_LAYOUT.items() if k not in ("legend", "margin")}
    fig.update_layout(
        **layout,
        height=total_height,
        showlegend=True,
        hovermode="closest",
        title_text=(f"{metadata['station_name']} — "
                    "Radiation Components & QC Flags"),
        # y=1.05 lifts the legend clear of the first subplot title annotation
        legend=dict(
            orientation="h", yanchor="bottom", y=1.05,
            xanchor="right", x=1, font=dict(size=11),
        ),
        # Increased top margin to accommodate the raised legend
        margin={**BASE_LAYOUT["margin"], "t": 110},
    )
    return fig


def fig_meteorology(df, metadata):
    meteo = [("T2","Air Temperature","°C"),
             ("P", "Station Pressure","hPa"),
             ("RH","Relative Humidity","%")]
    available = [(k, l, u) for k, l, u in meteo
                 if k in df.columns and df[k].notna().any()]
    if not available:
        return None

    fig = make_subplots(rows=len(available), cols=1, shared_xaxes=True,
                        vertical_spacing=0.05,
                        subplot_titles=[l for _, l, _ in available])
    for i, (col, label, unit) in enumerate(available, start=1):
        data = df[col].dropna()
        fig.add_trace(go.Scattergl(
            x=data.index, y=data.values, mode="lines",
            line=dict(color=PARAM_COLOR.get(col, TAB10[i-1]), width=0.9),
            name=f"{col} ({unit})",
            hovertemplate=f"<b>%{{x|%Y-%m-%d %H:%M}}</b><br>{col}: %{{y:.2f}} {unit}<extra></extra>",
        ), row=i, col=1)
        fig.update_yaxes(title_text=unit, row=i, col=1)

    fig.update_layout(**BASE_LAYOUT, height=240 * len(available), showlegend=False,
                      hovermode="x unified",
                      title_text=f"Meteorology — {metadata['station_name']}")
    return fig


def fig_missing_data(df, metadata):
    """Missing-data bar charts — height reduced ~20 % vs. original."""
    check_cols = [c for c in ["SWD","DIF","DIR","LWD"] if c in df.columns]
    if not check_cols:
        return None

    hourly = df[check_cols].isna().astype(float).resample("1h").sum()
    fig = make_subplots(rows=len(check_cols), cols=1, shared_xaxes=True,
                        vertical_spacing=0.05,
                        subplot_titles=check_cols)
    for i, col in enumerate(check_cols, start=1):
        fig.add_trace(go.Bar(
            x=hourly.index, y=hourly[col].values,
            marker_color=PARAM_COLOR.get(col, TAB10[i-1]), opacity=0.85,
            name=col,
            hovertemplate=f"<b>%{{x|%Y-%m-%d %H:00}}</b><br>{col} missing: %{{y:.0f}} min/hour<extra></extra>",
        ), row=i, col=1)
        fig.add_hline(y=60, line_color="red", line_dash="dash",
                      line_width=1, opacity=0.3, row=i, col=1)
        fig.update_yaxes(title_text="min/hr", range=[0, 65], row=i, col=1)

    # 168 px per row ≈ 20 % smaller than the original 210 px
    fig.update_layout(**BASE_LAYOUT, height=168 * len(check_cols), showlegend=False,
                      hovermode="x unified",
                      title_text="Missing Data per Hour")
    return fig


def fig_long_dutton(df, metadata):
    if "SZA" not in df.columns:
        return None
    ld_params = [("SWD",1.5,1.2,100,1.2,1.2,50),
                 ("DIF",0.95,1.2,50,0.75,1.2,30),
                 ("DIR",1.0,0,0,0.95,0.2,10)]
    available = [(p,*r) for p,*r in ld_params if p in df.columns and df[p].notna().any()]
    if not available:
        return None

    sub = df[df["SZA"] < 90].copy()
    fig = make_subplots(rows=1, cols=len(available),
                        subplot_titles=[p for p,*_ in available],
                        horizontal_spacing=0.08)
    for j, (param, ppl_f, ppl_e, ppl_o, erl_f, erl_e, erl_o) in enumerate(available, start=1):
        if param == "DIR":
            ppl_upper = sub["Sa"]
        else:
            ppl_upper = sub["Sa"] * ppl_f * np.power(sub["Mu0"], ppl_e) + ppl_o
        erl_upper = sub["Sa"] * erl_f * np.power(sub["Mu0"], erl_e) + erl_o
        sort_idx  = sub["SZA"].argsort()

        fig.add_trace(go.Scattergl(
            x=sub["SZA"], y=sub[param], mode="markers",
            marker=dict(color="black", size=2, opacity=0.15),
            name="Measurement", legendgroup="meas", showlegend=(j==1),
            text=sub.index.strftime("%Y-%m-%d %H:%M"),
            hovertemplate=(f"<b>%{{text}}</b><br>SZA: %{{x:.1f}}°<br>"
                           f"{param}: %{{y:.2f}} W/m²<extra></extra>"),
        ), row=1, col=j)
        fig.add_trace(go.Scattergl(
            x=sub["SZA"].values[sort_idx], y=ppl_upper.values[sort_idx],
            mode="markers", marker=dict(color="green", size=2, opacity=0.4),
            name="Phys. possible", legendgroup="ppl", showlegend=(j==1),
            hovertemplate="SZA: %{x:.1f}°<br>PPL: %{y:.1f} W/m²<extra></extra>",
        ), row=1, col=j)
        fig.add_trace(go.Scattergl(
            x=sub["SZA"].values[sort_idx], y=erl_upper.values[sort_idx],
            mode="markers", marker=dict(color="red", size=2, opacity=0.4),
            name="Extremely rare", legendgroup="erl", showlegend=(j==1),
            hovertemplate="SZA: %{x:.1f}°<br>ERL: %{y:.1f} W/m²<extra></extra>",
        ), row=1, col=j)
        fig.update_xaxes(title_text="SZA [°]", row=1, col=j)
        fig.update_yaxes(title_text="W/m²",    row=1, col=j)

    fig.update_layout(**BASE_LAYOUT, height=420, hovermode="closest",
                      title_text="Long & Dutton QC Limits")
    return fig


def fig_dif_swd_ratio(df, metadata):
    if not {"DIF","SWD","SZA"}.issubset(df.columns):
        return None
    sub = df[(df["SWD"] > 50) & (df["SZA"] < 93)].copy()
    if len(sub) == 0:
        return None
    sub["ratio"] = sub["DIF"] / sub["SWD"]

    fig = go.Figure()
    fig.add_trace(go.Scattergl(
        x=sub["SZA"], y=sub["ratio"], mode="markers",
        marker=dict(color="black", size=2, opacity=0.35), name="DIF/SWD",
        text=sub.index.strftime("%Y-%m-%d %H:%M"),
        hovertemplate="<b>%{text}</b><br>SZA: %{x:.1f}°<br>DIF/SWD: %{y:.4f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=[0,75,75,93], y=[1.05,1.05,1.10,1.10], mode="lines",
        line=dict(color="red", width=2), name="QC threshold", hoverinfo="skip",
    ))
    fig.update_layout(**BASE_LAYOUT, height=380, hovermode="closest",
                      xaxis_title="SZA [°]", yaxis_title="DIF / SWD",
                      xaxis_range=[20,93], yaxis_range=[0,1.4],
                      title_text="Diffuse Ratio (DIF/SWD) vs SZA")
    return fig


def fig_swd_sumsw_ratio(df, metadata):
    if not {"SWD","SumSW","SZA"}.issubset(df.columns):
        return None
    sub = df[(df["SZA"] < 93) & (df["SumSW"] > 50)].copy()
    if len(sub) == 0:
        return None
    sub["ratio"] = sub["SWD"] / sub["SumSW"]

    fig = go.Figure()
    fig.add_trace(go.Scattergl(
        x=sub["SZA"], y=sub["ratio"], mode="markers",
        marker=dict(color="black", size=2, opacity=0.35), name="SWD/SumSW",
        text=sub.index.strftime("%Y-%m-%d %H:%M"),
        hovertemplate="<b>%{text}</b><br>SZA: %{x:.1f}°<br>SWD/SumSW: %{y:.4f}<extra></extra>",
    ))
    for ys in [[1.08,1.08,1.15,1.15],[0.92,0.92,0.85,0.85]]:
        fig.add_trace(go.Scatter(
            x=[0,75,75,93], y=ys, mode="lines",
            line=dict(color="red", width=2), showlegend=False, hoverinfo="skip",
        ))
    fig.update_layout(**BASE_LAYOUT, height=380, hovermode="closest",
                      xaxis_title="SZA [°]", yaxis_title="SWD / SumSW",
                      xaxis_range=[20,93], yaxis_range=[0.5,1.5],
                      title_text="SWD / SumSW Ratio vs SZA")
    return fig


def fig_swd_vs_sumsw(df, metadata):
    if not {"SWD","SumSW"}.issubset(df.columns):
        return None
    sub = df[df["SWD"].notna() & df["SumSW"].notna()]
    if len(sub) == 0:
        return None
    maxval = max(sub["SWD"].max(), sub["SumSW"].max()) * 1.05

    fig = go.Figure()
    fig.add_trace(go.Scattergl(
        x=sub["SWD"], y=sub["SumSW"], mode="markers",
        marker=dict(color="steelblue", size=3, opacity=0.2), name="Data",
        text=sub.index.strftime("%Y-%m-%d %H:%M"),
        hovertemplate=("<b>%{text}</b><br>SWD: %{x:.2f} W/m²"
                       "<br>SumSW: %{y:.2f} W/m²<extra></extra>"),
    ))
    fig.add_trace(go.Scatter(
        x=[0, maxval], y=[0, maxval], mode="lines",
        line=dict(color="red", width=1.5, dash="dash"),
        name="1:1 line", hoverinfo="skip",
    ))
    fig.update_layout(**BASE_LAYOUT, height=460, hovermode="closest",
                      xaxis_title="SWD [W/m²]", yaxis_title="SumSW [W/m²]",
                      title_text="SWD vs SumSW  (= DIF + DIR·cos(SZA))")
    return fig


def fig_swd_minus_sumsw(df, metadata):
    if not {"SWD","SumSW"}.issubset(df.columns):
        return None
    diff = (df["SWD"] - df["SumSW"]).dropna()
    if len(diff) == 0:
        return None

    fig = go.Figure()
    fig.add_trace(go.Scattergl(
        x=diff.index, y=diff.values, mode="lines",
        line=dict(color="steelblue", width=0.6), name="SWD − SumSW",
        hovertemplate="<b>%{x|%Y-%m-%d %H:%M}</b><br>Diff: %{y:.2f} W/m²<extra></extra>",
    ))
    fig.add_hline(y=0, line_color="red", line_width=1.2)
    fig.update_layout(**BASE_LAYOUT, height=300,
                      hovermode="x unified",
                      xaxis_title="Date", yaxis_title="W/m²",
                      title_text="SWD − SumSW Difference Over Time")
    return fig


# ── Kept for backward compatibility (no longer called by generate_interactive_report) ──

def fig_radiation_timeseries(df, metadata):
    """Legacy: standalone radiation time series. Use fig_radiation_with_flags() instead."""
    rad_cols = ["DIF", "DIR", "SWD", "LWD"]
    if metadata.get("has_upward"):
        rad_cols += ["SWU", "LWU"]
    available = [c for c in rad_cols if c in df.columns and df[c].notna().any()]
    if not available:
        return None

    fig = make_subplots(
        rows=len(available), cols=2,
        shared_xaxes=False,
        column_widths=[0.78, 0.22],
        vertical_spacing=0.04,
        subplot_titles=[v for c in available
                        for v in (f"{c}  —  time series", f"{c}  —  histogram")],
    )
    for i, col in enumerate(available, start=1):
        data = df[col].dropna()
        color = PARAM_COLOR.get(col, TAB10[0])
        fig.add_trace(go.Scattergl(
            x=data.index, y=data.values, mode="lines",
            line=dict(color=color, width=0.7), name=col, legendgroup=col,
            hovertemplate=f"<b>%{{x|%Y-%m-%d %H:%M}}</b><br>{col}: %{{y:.2f}} W/m²<extra></extra>",
        ), row=i, col=1)
        fig.update_yaxes(title_text="W/m²", row=i, col=1)
        fig.add_trace(go.Histogram(
            y=data.values, nbinsy=60, marker_color=color, opacity=0.8,
            name=col, showlegend=False, legendgroup=col,
            hovertemplate=f"{col}: %{{y:.1f}} W/m²<br>Count: %{{x}}<extra></extra>",
        ), row=i, col=2)
        fig.update_xaxes(title_text="Count", row=i, col=2)

    fig.update_layout(**BASE_LAYOUT, height=240 * len(available), showlegend=False,
                      hovermode="closest",
                      title_text=f"{metadata['station_name']} — Radiation Components")
    return fig


def fig_qc_flags(df, metadata):
    """Legacy: standalone QC flags. Use fig_radiation_with_flags() instead."""
    qc_cols = [c for c in ["SWDQc","DIFQc","DIRQc","LWDQc","SWUQc","LWUQc"] if c in df.columns]
    if not qc_cols:
        return None

    fig = make_subplots(rows=len(qc_cols), cols=1, shared_xaxes=True,
                        vertical_spacing=0.04,
                        subplot_titles=[c.replace("Qc","") for c in qc_cols])
    for i, col in enumerate(qc_cols, start=1):
        param = col.replace("Qc", "")
        vals  = df[col].values.astype(int)
        for bit, fc, short, long_label, y_val in _FLAG_DEFS:
            mask = (vals & bit).astype(bool)
            if not mask.any():
                continue
            fig.add_trace(go.Scattergl(
                x=df.index[mask], y=np.full(mask.sum(), float(y_val)),
                mode="markers",
                marker=dict(color=fc, size=3, opacity=0.7, symbol="line-ns"),
                name=long_label, legendgroup=short, showlegend=(i == 1),
                hovertemplate=(f"<b>%{{x|%Y-%m-%d %H:%M}}</b><br>"
                               f"{param} → {long_label}<extra></extra>"),
            ), row=i, col=1)
        fig.update_yaxes(range=[0, 36],
                         tickvals=[1, 2, 4, 8, 16, 32],
                         ticktext=["PP−","PP+","ER−","ER+","Cmp−","Cmp+"],
                         tickfont=dict(size=9),
                         row=i, col=1)

    fig.update_layout(**BASE_LAYOUT, height=160 * len(qc_cols),
                      hovermode="closest",
                      title_text="QC Flags — hover for exact timestamp & flag type")
    return fig


def fig_qc_summary_bars(qc_summary):
    """Legacy: QC summary bar chart. Replaced by table-only display in report."""
    flag_cols = [c for c in qc_summary.columns
                 if c not in ["Parameter","Total records","All OK (0)"]]
    melted = qc_summary.melt(id_vars="Parameter", value_vars=flag_cols,
                              var_name="Flag type", value_name="Count")
    melted = melted[melted["Count"] > 0]
    if melted.empty:
        return None

    fig = go.Figure()
    for ftype in melted["Flag type"].unique():
        sub = melted[melted["Flag type"] == ftype]
        fig.add_trace(go.Bar(
            x=sub["Parameter"], y=sub["Count"], name=ftype,
            hovertemplate="<b>%{x}</b><br>" + ftype + ": %{y:,} records<extra></extra>",
        ))
    fig.update_layout(**BASE_LAYOUT, barmode="group", height=340,
                      hovermode="x",
                      xaxis_title="Parameter", yaxis_title="Records flagged",
                      title_text="QC Flag Counts per Parameter")
    return fig


def fig_lr4000(lr4000):
    """Legacy: LR4000 bar chart. Replaced by table-only display in report."""
    if not lr4000:
        return None

    eq_labels, mean_vals, std_vals, max_vals = [], [], [], []
    eq_labels.append("Simple eq.")
    mean_vals.append(abs(lr4000["simple_mean_diff"]))
    std_vals.append(lr4000["simple_std_diff"])
    max_vals.append(abs(lr4000["simple_max_diff"]))
    if lr4000.get("has_full"):
        eq_labels.append("Full eq.")
        mean_vals.append(abs(lr4000["full_mean_diff"]))
        std_vals.append(lr4000["full_std_diff"])
        max_vals.append(abs(lr4000["full_max_diff"]))

    colors_mean = ["#4a90d9","#d94a4a"][:len(eq_labels)]
    colors_std  = ["#7ab8e8","#e87a7a"][:len(eq_labels)]
    colors_max  = ["#1a5f99","#991a1a"][:len(eq_labels)]

    fig = go.Figure()
    fig.add_trace(go.Bar(name="|Mean Δ|", x=eq_labels, y=mean_vals,
                         marker_color=colors_mean,
                         hovertemplate="%{x}<br>|Mean Δ|: %{y:.4f} W/m²<extra></extra>"))
    fig.add_trace(go.Bar(name="Std Δ",   x=eq_labels, y=std_vals,
                         marker_color=colors_std,
                         hovertemplate="%{x}<br>Std Δ: %{y:.4f} W/m²<extra></extra>"))
    fig.add_trace(go.Bar(name="|Max Δ|", x=eq_labels, y=max_vals,
                         marker_color=colors_max,
                         hovertemplate="%{x}<br>|Max Δ|: %{y:.4f} W/m²<extra></extra>"))
    fig.update_layout(**BASE_LAYOUT, barmode="group", height=320, hovermode="x",
                      yaxis_title="Δ LWD [W/m²]",
                      title_text="LR4000: |Measured − Recalculated| LWD Statistics")
    return fig


# =============================================================================
# HTML ASSEMBLY
# =============================================================================

_SECTION_STYLE = (
    "margin: 2rem 0 0.5rem; padding-bottom: 0.3rem; "
    "border-bottom: 2px solid #dee2e6; font-size: 1.1rem; font-weight: 700;"
)


def _fig_to_div(fig, div_id):
    """Convert a Plotly figure to an HTML div string (no Plotly.js included)."""
    if fig is None:
        return ""
    return pio.to_html(fig, include_plotlyjs=False, full_html=False, div_id=div_id)


def _qc_table_html(qc_summary):
    """Render the QC summary as a colour-coded HTML table."""
    flag_cols = [c for c in qc_summary.columns
                 if c not in ["Parameter","Total records","All OK (0)"]]

    def cell_style(col, val):
        if col not in flag_cols or not isinstance(val, (int, float, np.integer)):
            return ""
        if val == 0:
            return ' style="color:#198754;font-weight:600"'
        elif val < 10:
            return ' style="color:#fd7e14;font-weight:600"'
        else:
            return ' style="color:#dc3545;font-weight:600"'

    headers = "".join(f"<th>{c}</th>" for c in qc_summary.columns)
    rows_html = ""
    for _, row in qc_summary.iterrows():
        cells = "".join(
            f'<td{cell_style(col, row[col])}>'
            f'{row[col]:,}' if isinstance(row[col], (int, float, np.integer))
            else f'<td>{row[col]}'
            + '</td>'
            for col in qc_summary.columns
        )
        rows_html += f"<tr>{cells}</tr>\n"

    return f"""
    <table style="border-collapse:collapse; width:100%; font-size:0.9rem;">
      <thead style="background:#f1f3f5;">
        <tr>{headers}</tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>"""


def _minima_table_html(df):
    """
    Render the Parameter Minima table — mirrors the 'Parameter Minima' section
    in the normal QC report generated by bsrn_qc.py.
    """
    numeric_cols = ["SWD", "DIF", "DIR", "LWD", "SWU", "LWU",
                    "T2", "RH", "P", "SZA", "SumSW"]
    minima = {}
    for col in numeric_cols:
        if col in df.columns and df[col].notna().any():
            minima[col] = float(df[col].min())

    if not minima:
        return ""

    rows_html = "".join(
        f"<tr>"
        f"<td style='padding:5px 12px; text-align:left'><strong>{param}</strong></td>"
        f"<td style='padding:5px 12px; text-align:right'>{val:.1f}</td>"
        f"</tr>\n"
        for param, val in minima.items()
    )
    return f"""
    <table style="border-collapse:collapse; font-size:0.9rem; width:auto;">
      <thead style="background:#f1f3f5;">
        <tr>
          <th style="padding:5px 12px; text-align:left;">Parameter</th>
          <th style="padding:5px 12px; text-align:right;">Minimum Value</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>"""


def _lr4000_stats_table(lr4000):
    rows = [
        ("Equation",       "Simple",
         "Full" if lr4000.get("has_full") else "—"),
        ("Records",        f"{lr4000['n_lr4000']:,}",
         f"{lr4000.get('n_full','—'):,}" if lr4000.get("has_full") else "—"),
        ("Mean Δ [W/m²]",  f"{lr4000['simple_mean_diff']:.4f}",
         f"{lr4000.get('full_mean_diff',0):.4f}" if lr4000.get("has_full") else "—"),
        ("Std Δ [W/m²]",   f"{lr4000['simple_std_diff']:.4f}",
         f"{lr4000.get('full_std_diff',0):.4f}" if lr4000.get("has_full") else "—"),
        ("|Max Δ| [W/m²]", f"{abs(lr4000['simple_max_diff']):.4f}",
         f"{abs(lr4000.get('full_max_diff',0)):.4f}" if lr4000.get("has_full") else "—"),
        ("Max Δ at",       lr4000["simple_max_diff_time"],
         lr4000.get("full_max_diff_time","—") if lr4000.get("has_full") else "—"),
    ]
    rows_html = "".join(
        f"<tr><th style='text-align:left;background:#f1f3f5;padding:4px 8px'>{r}</th>"
        f"<td style='padding:4px 8px'>{s}</td>"
        f"<td style='padding:4px 8px'>{f}</td></tr>"
        for r, s, f in rows
    )
    return f"""
    <table style="border-collapse:collapse; font-size:0.9rem;">
      <thead style="background:#e9ecef;">
        <tr><th style="padding:4px 8px">Metric</th>
            <th style="padding:4px 8px">Simple equation</th>
            <th style="padding:4px 8px">Full equation</th></tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>"""


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def generate_interactive_report(df, metadata, qc_summary, output_path,
                                 lr4000_report=None):
    """
    Generate a fully self-contained interactive HTML report.

    Parameters
    ----------
    df            : pandas DataFrame from the QC pipeline
    metadata      : dict from parse_dat_file()
    qc_summary    : DataFrame from summarize_qc_flags()
    output_path   : Path or str — where to write the HTML file
    lr4000_report : dict from check_lr4000() (or None)

    Returns
    -------
    Path to the written HTML file.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    station    = metadata["station_name"]
    code       = metadata["station_code"]
    period     = f"{metadata['year']}-{metadata['month']:02d}"
    total_recs = int(qc_summary["Total records"].max()) if len(qc_summary) else 0
    total_flags= int(qc_summary["Any flag"].sum())
    flag_pct   = 100 * total_flags / max(total_recs, 1)
    now        = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Build all figures ────────────────────────────────────────────────────
    figures = {
        "worldmap":   fig_world_map(metadata),
        "rad_flags":  fig_radiation_with_flags(df, metadata),
        "meteo":      fig_meteorology(df, metadata),
        "missing":    fig_missing_data(df, metadata),
        "longdutton": fig_long_dutton(df, metadata),
        "difswd":     fig_dif_swd_ratio(df, metadata),
        "swdsumsw":   fig_swd_sumsw_ratio(df, metadata),
        "scatter":    fig_swd_vs_sumsw(df, metadata),
        "diff":       fig_swd_minus_sumsw(df, metadata),
    }

    divs = {k: _fig_to_div(v, f"fig_{k}") for k, v in figures.items()}

    # ── Parameter Minima table ────────────────────────────────────────────────
    minima_html = _minima_table_html(df)

    # ── Metadata cards ───────────────────────────────────────────────────────
    elv = metadata.get("elevation")
    elv_str = f"{elv} m" if elv is not None else "—"
    cards = [
        ("Latitude",      f"{metadata['latitude']:.3f} °"),
        ("Longitude",     f"{metadata['longitude']:.3f} °"),
        ("Elevation",     elv_str),
        ("Records",       f"{total_recs:,}"),
        ("Flagged",       f"{total_flags:,}  ({flag_pct:.1f} %)"),
        ("Generated",     now),
    ]
    cards_html = "".join(
        f'<div style="background:#f8f9fa;border:1px solid #e9ecef;border-radius:8px;'
        f'padding:12px 16px;min-width:130px">'
        f'<div style="font-size:0.75rem;color:#6c757d;text-transform:uppercase;'
        f'letter-spacing:.05em">{label}</div>'
        f'<div style="font-size:1.1rem;font-weight:700;margin-top:2px">{val}</div>'
        f'</div>'
        for label, val in cards
    )

    # ── World map inline (right of cards) ────────────────────────────────────
    worldmap_html = ""
    if divs["worldmap"]:
        worldmap_html = (
            f'<div style="max-width:420px;flex:0 0 420px">'
            f'{divs["worldmap"]}'
            f'</div>'
        )

    # ── LR4000 section (table only) ──────────────────────────────────────────
    lr4000_html = ""
    if lr4000_report:
        lr4000_html = f"""
        <h2 id="lr4000" style="{_SECTION_STYLE}">🔬 LR4000 Pyrgeometer Check</h2>
        {_lr4000_stats_table(lr4000_report)}
        """

    # ── Full page ────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BSRN QC — {code} {period}</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      font-family: Inter, Arial, sans-serif;
      font-size: 14px; color: #212529;
      background: #fff; margin: 0; padding: 0;
    }}
    nav {{
      position: sticky; top: 0; z-index: 100;
      background: #1a1a2e; color: #fff;
      display: flex; gap: 0; align-items: center;
      padding: 0 1rem; height: 44px;
      box-shadow: 0 2px 6px rgba(0,0,0,.25);
    }}
    nav span {{ font-weight: 700; font-size: 1rem; margin-right: 1.5rem; }}
    nav a {{
      color: #adb5bd; text-decoration: none;
      padding: 0 10px; height: 44px; line-height: 44px;
      font-size: 0.82rem; white-space: nowrap;
    }}
    nav a:hover {{ color: #fff; background: rgba(255,255,255,.08); }}
    main {{ max-width: 1400px; margin: 0 auto; padding: 1.5rem 2rem 3rem; }}
    h1 {{ font-size: 1.6rem; margin: 0 0 1rem; }}
    .header-row {{
      display: flex; flex-wrap: wrap; gap: 1.5rem;
      align-items: flex-start; margin-bottom: 1.5rem;
    }}
    .cards {{ display: flex; flex-wrap: wrap; gap: 10px; flex: 1 1 auto; }}
    table th, table td {{ padding: 5px 10px; border: 1px solid #dee2e6; }}
    table thead {{ position: sticky; top: 44px; }}
  </style>
</head>
<body>
<nav>
  <span>☀️ BSRN QC</span>
  <a href="#radiation">Radiation & Flags</a>
  <a href="#meteo">Meteorology</a>
  <a href="#scatter">Scatter / Ratios</a>
  <a href="#missing">Missing Data</a>
  <a href="#qcsummary">QC Summary</a>
  {"<a href='#minima'>Minima</a>" if minima_html else ""}
  {"<a href='#lr4000'>LR4000</a>" if lr4000_report else ""}
</nav>
<main>
  <h1>☀️ {station} &nbsp;({code}) &nbsp;·&nbsp; {period}</h1>

  <!-- Header: metadata cards + world map side by side -->
  <div class="header-row">
    <div class="cards">{cards_html}</div>
    {worldmap_html}
  </div>

  <h2 id="radiation" style="{_SECTION_STYLE}">📈 Radiation Components &amp; QC Flags</h2>
  {divs["rad_flags"]}

  <h2 id="meteo" style="{_SECTION_STYLE}">🌡️ Meteorology</h2>
  {divs["meteo"]}

  <h2 id="scatter" style="{_SECTION_STYLE}">🔵 Scatter &amp; Ratios</h2>
  {divs["longdutton"]}
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:1rem">
    <div>{divs["difswd"]}</div>
    <div>{divs["swdsumsw"]}</div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:1rem">
    <div>{divs["scatter"]}</div>
    <div>{divs["diff"]}</div>
  </div>

  <h2 id="missing" style="{_SECTION_STYLE}">❌ Missing Data</h2>
  {divs["missing"]}

  <h2 id="qcsummary" style="{_SECTION_STYLE}">📋 QC Summary</h2>
  {_qc_table_html(qc_summary)}

  {'<h2 id="minima" style="' + _SECTION_STYLE + '">📉 Parameter Minima</h2>' + minima_html if minima_html else ""}

  {lr4000_html}

  <hr style="margin-top:3rem;border-color:#dee2e6">
  <p style="color:#6c757d;font-size:0.8rem">
    BSRN QC Dashboard · {station} ({code}) · {period} · Generated {now}
  </p>
</main>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")
    return output_path


# =============================================================================
# JUPYTER DISPLAY HELPER
# =============================================================================

def show_notebook_link(report_path):
    """
    Display a styled clickable link to the interactive HTML report
    inside a Jupyter notebook cell output.  Works on JupyterHub and
    local Jupyter — uses IPython.display.FileLink which routes through
    the notebook server, so no localhost port is needed.
    """
    report_path = Path(report_path)
    try:
        from IPython.display import display, FileLink, HTML

        # FileLink creates a /files/... URL that JupyterHub serves directly
        display(HTML(f"""
        <div style="margin:14px 0;padding:14px 20px;background:#f0f7ff;
                    border-left:4px solid #1a73e8;border-radius:6px;
                    font-family:sans-serif;">
          <div style="font-size:1.05em;font-weight:700;margin-bottom:6px">
            ☀️ Interactive QC Report ready
          </div>
          <div style="margin-bottom:8px">
        """))
        display(FileLink(str(report_path), result_html_prefix="📂 &nbsp;"))
        display(HTML("""
          </div>
          <div style="font-size:0.82em;color:#555">
            Click the link above to open the interactive report in a new tab.
            All charts support hover, zoom, and pan — no server required.
          </div>
        </div>"""))
    except ImportError:
        print(f"\n  ✅  Interactive report saved → {report_path}")
