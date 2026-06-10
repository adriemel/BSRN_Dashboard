#!/usr/bin/env python3
"""
SWD / SumSW versus time-of-day plots for one monthly BSRN .dat file.

Dependencies:
  - bsrn_qc.py in the same folder or otherwise importable
  - one monthly BSRN station-to-archive .dat file

Outputs:
  1. One large multi-panel PNG with one subplot per day
  2. One overlay PNG with all days in one axis

The script reuses:
  - parse_dat_file(...)
  - compute_solar_auxiliary(...)

from bsrn_qc.py.

Testability and QC envelopes:
  - SumSW > 50 W m-2
  - SZA < 75°:        0.92 <= SWD/SumSW <= 1.08
  - 75° <= SZA < 93°: 0.85 <= SWD/SumSW <= 1.15
  - SumSW <= 50 W m-2 or SZA >= 93°: no test, therefore not plotted

Untestable gaps are not bridged by the blue ratio line or fill.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from bsrn_qc import parse_dat_file, compute_solar_auxiliary


REQUIRED_COLUMNS = {"SWD", "SumSW", "SZA"}
ANCHOR_DATE = pd.Timestamp("2000-01-01")
DEFAULT_GAP_MINUTES = 2


def _prepare_ratio_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Return testable SWD/SumSW records with plotting helper columns."""
    missing = REQUIRED_COLUMNS.difference(df.columns)
    if missing:
        raise ValueError(
            "Cannot create SWD/SumSW time-of-day plots. "
            f"Missing required column(s): {', '.join(sorted(missing))}."
        )

    if df["SumSW"].isna().all():
        raise ValueError(
            "Cannot create SWD/SumSW time-of-day plots because SumSW is entirely missing. "
            "This usually means DIR and/or DIF are unavailable."
        )

    sub = df.copy()
    sub["ratio"] = sub["SWD"] / sub["SumSW"]

    testable = (
        sub["SWD"].notna()
        & sub["SumSW"].notna()
        & sub["SZA"].notna()
        & np.isfinite(sub["ratio"])
        & (sub["SumSW"] > 50)
        & (sub["SZA"] < 93)
    )

    sub = sub.loc[testable].copy()
    if sub.empty:
        raise ValueError(
            "No testable records found: all records have SumSW <= 50 W m-2, "
            "SZA >= 93°, or missing SWD/SumSW inputs."
        )

    sub["date"] = sub.index.date
    sub["time_of_day"] = ANCHOR_DATE + pd.to_timedelta(
        sub.index.hour * 3600
        + sub.index.minute * 60
        + sub.index.second,
        unit="s",
    )

    sub["lower_limit"] = np.where(sub["SZA"] < 75, 0.92, 0.85)
    sub["upper_limit"] = np.where(sub["SZA"] < 75, 1.08, 1.15)

    return sub


def _split_on_time_gaps(
    df: pd.DataFrame,
    *,
    time_col: str = "time_of_day",
    gap_minutes: int = DEFAULT_GAP_MINUTES,
) -> list[pd.DataFrame]:
    """Split testable data into contiguous sections."""
    if df.empty:
        return []

    ordered = df.sort_values(time_col).copy()
    gap = ordered[time_col].diff()
    new_segment = gap > pd.Timedelta(minutes=gap_minutes)
    segment_id = new_segment.cumsum()
    return [seg for _, seg in ordered.groupby(segment_id, sort=False)]


def _split_on_value_changes(
    df: pd.DataFrame,
    value_col: str,
    *,
    time_col: str = "time_of_day",
    gap_minutes: int = DEFAULT_GAP_MINUTES,
) -> list[pd.DataFrame]:
    """
    Split a plotted limit line on both time gaps and value-regime changes.

    This avoids:
      - red lines bridging untestable periods;
      - vertical connectors between the 8% and 15% regimes.
    """
    segments: list[pd.DataFrame] = []

    for time_segment in _split_on_time_gaps(
        df, time_col=time_col, gap_minutes=gap_minutes
    ):
        ordered = time_segment.sort_values(time_col).copy()
        regime_change = ordered[value_col].ne(ordered[value_col].shift())
        regime_id = regime_change.cumsum()
        segments.extend(
            seg for _, seg in ordered.groupby(regime_id, sort=False)
        )

    return segments


def _format_time_axis(
    ax: plt.Axes,
    xmin: pd.Timestamp,
    xmax: pd.Timestamp,
) -> None:
    ax.set_xlim(xmin, xmax)
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    ax.xaxis.set_minor_locator(mdates.HourLocator(interval=1))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.grid(True, which="major", linewidth=0.6, alpha=0.6)
    ax.grid(True, which="minor", axis="x", linewidth=0.3, alpha=0.25)


def _plot_ratio_segments(
    ax: plt.Axes,
    day_df: pd.DataFrame,
    *,
    line_width: float,
    line_alpha: float = 1.0,
    fill: bool = False,
    fill_alpha: float = 0.15,
    gap_minutes: int = DEFAULT_GAP_MINUTES,
) -> None:
    """Plot blue SWD/SumSW curve without bridging untestable gaps."""
    for segment in _split_on_time_gaps(day_df, gap_minutes=gap_minutes):
        ax.plot(
            segment["time_of_day"],
            segment["ratio"],
            color="blue",
            linewidth=line_width,
            alpha=line_alpha,
        )

        if fill:
            ax.fill_between(
                segment["time_of_day"],
                segment["ratio"].to_numpy(dtype=float),
                0,
                color="blue",
                alpha=fill_alpha,
            )


def _plot_limits_for_day(
    ax: plt.Axes,
    day_df: pd.DataFrame,
    *,
    linewidth: float = 1.0,
    gap_minutes: int = DEFAULT_GAP_MINUTES,
) -> None:
    """Plot red dotted QC envelopes without bridging gaps."""
    for seg in _split_on_value_changes(
        day_df,
        "upper_limit",
        gap_minutes=gap_minutes,
    ):
        ax.plot(
            seg["time_of_day"],
            seg["upper_limit"],
            linestyle=":",
            linewidth=linewidth,
            color="red",
        )

    for seg in _split_on_value_changes(
        day_df,
        "lower_limit",
        gap_minutes=gap_minutes,
    ):
        ax.plot(
            seg["time_of_day"],
            seg["lower_limit"],
            linestyle=":",
            linewidth=linewidth,
            color="red",
        )


def plot_daily_panel_grid(
    sub: pd.DataFrame,
    station_label: str,
    month_label: str,
    output_path: Path,
    *,
    ncols: int = 4,
    y_min: float | None = None,
    y_max: float | None = None,
    gap_minutes: int = DEFAULT_GAP_MINUTES,
    dpi: int = 180,
) -> None:
    """Create one large multi-panel image with one subplot per testable day."""
    grouped_days = list(sub.groupby("date", sort=True))
    n_days = len(grouped_days)
    nrows = math.ceil(n_days / ncols)

    fig, axes = plt.subplots(
        nrows=nrows,
        ncols=ncols,
        figsize=(4.6 * ncols, 3.5 * nrows),
        sharey=True,
        squeeze=False,
    )
    flat_axes = axes.ravel()

    global_xmin = sub["time_of_day"].min()
    global_xmax = sub["time_of_day"].max()

    if y_min is None:
        y_min = min(0.65, float(sub["ratio"].min()) - 0.03)
    if y_max is None:
        y_max = max(1.25, float(sub["ratio"].max()) + 0.03)

    for ax, (day, day_df) in zip(flat_axes, grouped_days):
        day_df = day_df.sort_values("time_of_day").copy()

        _plot_ratio_segments(
            ax,
            day_df,
            line_width=0.55,
            fill=True,
            gap_minutes=gap_minutes,
        )
        _plot_limits_for_day(
            ax,
            day_df,
            linewidth=1.0,
            gap_minutes=gap_minutes,
        )

        ax.set_title(
            pd.Timestamp(day).strftime("%Y-%m-%d"),
            fontsize=10,
            fontweight="bold",
        )
        ax.set_ylim(y_min, y_max)
        _format_time_axis(ax, global_xmin, global_xmax)

    for ax in flat_axes[n_days:]:
        ax.remove()

    fig.suptitle(
        f"SWD / SumSW vs. Time of Day — {station_label} {month_label}",
        fontsize=16,
        fontweight="bold",
        y=1.002,
    )
    fig.supxlabel("Time (UTC)", fontsize=12)
    fig.supylabel("SWD / SumSW", fontsize=12)
    fig.tight_layout()
    fig.savefig(output_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def plot_all_days_overlay(
    sub: pd.DataFrame,
    station_label: str,
    month_label: str,
    output_path: Path,
    *,
    y_min: float | None = None,
    y_max: float | None = None,
    gap_minutes: int = DEFAULT_GAP_MINUTES,
    dpi: int = 180,
) -> None:
    """Create one overlay plot with all testable days."""
    xmin = sub["time_of_day"].min()
    xmax = sub["time_of_day"].max()

    if y_min is None:
        y_min = min(0.65, float(sub["ratio"].min()) - 0.03)
    if y_max is None:
        y_max = max(1.25, float(sub["ratio"].max()) + 0.03)

    fig, ax = plt.subplots(figsize=(12, 6.5))

    for _, day_df in sub.groupby("date", sort=True):
        _plot_ratio_segments(
            ax,
            day_df,
            line_width=0.45,
            line_alpha=0.35,
            fill=False,
            gap_minutes=gap_minutes,
        )

    # Faint day-specific red envelopes prevent implying testability where no
    # given day has a valid comparison.
    for _, day_df in sub.groupby("date", sort=True):
        _plot_limits_for_day(
            ax,
            day_df,
            linewidth=0.7,
            gap_minutes=gap_minutes,
        )

    ax.set_title(
        f"SWD / SumSW vs. Time of Day — All Days Overlaid — "
        f"{station_label} {month_label}",
        fontsize=14,
        fontweight="bold",
    )
    ax.set_xlabel("Time (UTC)")
    ax.set_ylabel("SWD / SumSW")
    ax.set_ylim(y_min, y_max)
    _format_time_axis(ax, xmin, xmax)

    fig.tight_layout()
    fig.savefig(output_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def create_plots(
    dat_file: str | Path,
    output_dir: str | Path | None = None,
    *,
    ncols: int = 4,
    y_min: float | None = None,
    y_max: float | None = None,
    gap_minutes: int = DEFAULT_GAP_MINUTES,
    dpi: int = 180,
) -> dict[str, object]:
    """
    Parse one monthly BSRN .dat file and create:
      - one daily-panel grid image;
      - one all-days overlay image.

    Notebook use:
        from bsrn_swd_sumsw_time_of_day_corrected import create_plots
        result = create_plots("station_month.dat")
        result
    """
    dat_file = Path(dat_file)
    if not dat_file.exists():
        raise FileNotFoundError(f"Input .dat file not found: {dat_file}")

    df, metadata = parse_dat_file(dat_file)
    df = compute_solar_auxiliary(
        df,
        metadata["latitude"],
        metadata["longitude"],
    )
    sub = _prepare_ratio_dataframe(df)

    station_label = f"{metadata['station_code']}"
    month_label = f"{metadata['year']}-{metadata['month']:02d}"

    if output_dir is None:
        output_dir = dat_file.parent / f"{dat_file.stem}_SWD_SumSW_time_of_day"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    daily_grid_path = (
        output_dir
        / f"{dat_file.stem}_SWD_SumSW_time_of_day_daily_panels.png"
    )
    overlay_path = (
        output_dir
        / f"{dat_file.stem}_SWD_SumSW_time_of_day_all_days_overlay.png"
    )

    plot_daily_panel_grid(
        sub,
        station_label,
        month_label,
        daily_grid_path,
        ncols=ncols,
        y_min=y_min,
        y_max=y_max,
        gap_minutes=gap_minutes,
        dpi=dpi,
    )
    plot_all_days_overlay(
        sub,
        station_label,
        month_label,
        overlay_path,
        y_min=y_min,
        y_max=y_max,
        gap_minutes=gap_minutes,
        dpi=dpi,
    )

    return {
        "input_file": dat_file,
        "output_dir": output_dir,
        "daily_panel_grid": daily_grid_path,
        "all_days_overlay": overlay_path,
        "n_testable_records": int(len(sub)),
        "n_days_with_testable_records": int(sub["date"].nunique()),
        "gap_minutes": int(gap_minutes),
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create SWD/SumSW versus time-of-day plots for one monthly BSRN .dat file."
        )
    )
    parser.add_argument("dat_file", help="Path to one monthly BSRN .dat file.")
    parser.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Output directory. Default: next to the .dat file.",
    )
    parser.add_argument(
        "--ncols",
        type=int,
        default=4,
        help="Number of columns in the daily panel grid. Default: 4.",
    )
    parser.add_argument(
        "--gap-minutes",
        type=int,
        default=DEFAULT_GAP_MINUTES,
        help=(
            "Break plotted blue lines and red thresholds after gaps larger than "
            "this many minutes. Default: 2."
        ),
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=180,
        help="PNG resolution. Default: 180.",
    )
    parser.add_argument(
        "--y-min",
        type=float,
        default=None,
        help="Optional fixed y-axis lower limit.",
    )
    parser.add_argument(
        "--y-max",
        type=float,
        default=None,
        help="Optional fixed y-axis upper limit.",
    )
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    try:
        summary = create_plots(
            args.dat_file,
            args.output_dir,
            ncols=args.ncols,
            y_min=args.y_min,
            y_max=args.y_max,
            gap_minutes=args.gap_minutes,
            dpi=args.dpi,
        )
    except Exception as exc:
        parser.error(str(exc))
        return

    print(f"Input file: {summary['input_file']}")
    print(f"Testable records: {summary['n_testable_records']}")
    print(f"Days with testable records: {summary['n_days_with_testable_records']}")
    print(f"Daily panel grid: {summary['daily_panel_grid']}")
    print(f"All-days overlay: {summary['all_days_overlay']}")


if __name__ == "__main__":
    main()
