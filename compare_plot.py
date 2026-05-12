#!/usr/bin/env python3
"""
compare_plot.py — Multi-CSV comparison plotter.

Interactive series-building workflow
--------------------------------------
  For each series you want on the plot:
    1.  "Add a series?" → y
    2.  Enter the CSV file path
        - New file → asked for label, optional row filter, optional derived columns
        - Already loaded path → reused as-is (no re-setup)
    3.  Pick X column
    4.  Pick Y column
    5.  Enter a series label (or accept default)
    6.  Repeat from 1 until you enter  n
  Then choose smoothing + grouping and the plot appears.

Optional pre-loading
----------------------
    python compare_plot.py --files FILE1.csv FILE2.csv [--labels "A" "B"]

Derived column syntax  (at the  derive>  prompt)
-------------------------------------------------
    fi.new_col = expression   (fi = file index shown in brackets)
    Example: 1.T_N = 1.weight_g * 9.81 / 1000
    Example: 1.P_W = 1.voltage_v * 1.current_a
    Cross-file: 0.RPM_k = 0.RPM / 1000

Row filter syntax  (at the  filter>  prompt)
----------------------------------------------
    pandas query string for this file's columns, e.g.:
        throttle > 200
        time_s > 5 and time_s < 60
"""

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import zscore


# ── smoothing ────────────────────────────────────────────────────────────────

def apply_filter(x, y, method):
    if method == "none":
        return x, y

    df_tmp = pd.DataFrame({"x": x, "y": y}).dropna()
    x, y = df_tmp["x"], df_tmp["y"]
    if len(x) == 0:
        return x, y

    if method == "bin":
        n_bins = 80
        bins = np.linspace(x.min(), x.max(), n_bins + 1)
        idx_arr = np.digitize(x, bins) - 1
        bx, by = [], []
        for b in range(n_bins):
            mask = idx_arr == b
            if mask.sum() > 0:
                bx.append(x[mask].median())
                by.append(y[mask].median())
        return pd.Series(bx), pd.Series(by)

    if method == "sigma":
        xv, yv = x.values.copy(), y.values.copy()
        for _ in range(3):
            if len(xv) < 4:
                break
            coeffs = np.polyfit(xv, yv, 2)
            residuals = yv - np.polyval(coeffs, xv)
            keep = np.abs(zscore(residuals)) < 2.5
            xv, yv = xv[keep], yv[keep]
        return pd.Series(xv), pd.Series(yv)

    if method == "lowess":
        from statsmodels.nonparametric.smoothers_lowess import lowess
        order = np.argsort(x.values)
        xs, ys = x.values[order], y.values[order]
        smoothed = lowess(ys, xs, frac=0.15, return_sorted=True)
        return pd.Series(smoothed[:, 0]), pd.Series(smoothed[:, 1])

    return x, y


# ── prompts ───────────────────────────────────────────────────────────────────

def _ask(prompt, options):
    opts = "/".join(options)
    while True:
        val = input(f"{prompt} [{opts}]: ").strip().lower()
        if val in options:
            return val
        print(f"  Please enter one of: {opts}")


def pick_column_from_file(df, label, prompt):
    cols = list(df.columns)
    print(f"\n  {prompt}  (file: {label})")
    for i, c in enumerate(cols):
        print(f"    [{i:3d}]  {c}")
    while True:
        raw = input("  Number or column name: ").strip()
        try:
            n = int(raw)
            if 0 <= n < len(cols):
                return cols[n]
        except ValueError:
            pass
        if raw in cols:
            return raw
        print(f"  Invalid — enter 0–{len(cols)-1} or a column name.")


# ── per-file interactive setup ────────────────────────────────────────────────

def _filter_file(dfs, fi, labels):
    """Optionally apply a row filter to dfs[fi] in-place."""
    if _ask(f"  Row filter for '{labels[fi]}'?", ["y", "n"]) == "n":
        return
    print("  Pandas query expression for this file's columns.")
    print("  Examples:  throttle > 200   /   time_s > 5 and time_s < 60")
    print("  Press Enter to skip.")
    while True:
        raw = input("  filter> ").strip()
        if not raw:
            return
        fixed = re.sub(r"(?<![=!<>])=(?!=)", "==", raw)
        if fixed != raw:
            print(f"  (auto-fixed '=' → '=='  →  {fixed})")
            raw = fixed
        before = len(dfs[fi])
        try:
            dfs[fi] = dfs[fi].query(raw).reset_index(drop=True)
            print(f"  ✓ {before} → {len(dfs[fi])} rows  (filter: {raw})")
            return
        except Exception as e:
            print(f"  Error: {e}")


def _derived_for_file(dfs, fi, labels):
    """Optionally add derived columns to dfs[fi] in-place."""
    if _ask(f"  Add derived column(s) to '{labels[fi]}'?", ["y", "n"]) == "n":
        return
    print("  Syntax:  fi.new_col = expression  referencing fi.existing_col")
    print(f"  This file is [{fi}].  Example: {fi}.T_N = {fi}.weight_g * 9.81 / 1000")
    print("  Press Enter to finish.")
    df = dfs[fi]
    while True:
        raw = input("  derive> ").strip()
        if not raw:
            return
        m = re.match(r"^(\d+)\.(\w+)\s*=\s*(.+)$", raw)
        if not m:
            print(f"  Bad syntax — use  {fi}.new_col = expression")
            continue
        tgt_fi, new_col, expr = int(m.group(1)), m.group(2), m.group(3)
        if tgt_fi != fi:
            print(f"  Derived column target must be [{fi}] (the file just loaded).")
            continue

        def _replace(match):
            ref_fi, ref_col = int(match.group(1)), match.group(2)
            if ref_fi == fi and ref_col in df.columns:
                return f'_df["{ref_col}"]'
            if ref_fi < len(dfs) and ref_col in dfs[ref_fi].columns:
                return f'_dfs[{ref_fi}]["{ref_col}"]'
            return match.group(0)

        safe_expr = re.sub(r"(\d+)\.(\w+)", _replace, expr)
        try:
            result = eval(safe_expr, {"_df": df, "_dfs": dfs, "np": np})
            df[new_col] = result
            sample = df[new_col].dropna().head(3).tolist()
            print(f"  ✓ Added [{fi}].{new_col}  (sample: {sample})")
        except Exception as e:
            print(f"  Error: {e}")


def _load_and_setup(path_str, dfs, labels, paths):
    """Load a CSV (or reuse if already loaded). Returns fi, or None on error."""
    p = Path(path_str.strip())
    resolved = str(p.resolve())

    if resolved in paths:
        fi = paths.index(resolved)
        print(f"  Reusing already-loaded [{fi}]: {labels[fi]}  ({len(dfs[fi])} rows)")
        return fi

    if not p.exists():
        print(f"  File not found: {p}")
        return None

    df = pd.read_csv(p)
    fi = len(dfs)
    default_lbl = p.stem
    raw_lbl = input(f"  Label [{default_lbl}]: ").strip()
    lbl = raw_lbl if raw_lbl else default_lbl

    dfs.append(df)
    labels.append(lbl)
    paths.append(resolved)
    print(f"  Loaded [{fi}]: {lbl}  ({len(df)} rows × {len(df.columns)} cols)")

    _filter_file(dfs, fi, labels)
    _derived_for_file(dfs, fi, labels)
    return fi


# ── series collection ─────────────────────────────────────────────────────────

def collect_series(dfs, labels, paths):
    """Interactive loop: add series one by one until the user says n."""
    series_list = []
    while True:
        if series_list:
            print(f"\n  Series so far ({len(series_list)}):")
            for i, s in enumerate(series_list):
                print(f"    [{i}]  {labels[s['fi']]}: {s['x_col']}  →  {s['y_col']}  [{s['label']}]")

        if _ask("\nAdd a series?", ["y", "n"]) == "n":
            break

        path_str = input("  File path: ").strip()
        if not path_str:
            continue

        fi = _load_and_setup(path_str, dfs, labels, paths)
        if fi is None:
            continue

        x_col = pick_column_from_file(dfs[fi], labels[fi], "X column:")
        y_col = pick_column_from_file(dfs[fi], labels[fi], "Y column:")
        default_lbl = f"{labels[fi]}: {y_col}"
        raw_lbl = input(f"  Series label [{default_lbl}]: ").strip()
        series_list.append({
            "fi": fi, "x_col": x_col, "y_col": y_col,
            "label": raw_lbl if raw_lbl else default_lbl,
        })
        print("  ✓ Series added.")

    return series_list


# ── plot drawing helpers ──────────────────────────────────────────────────────

def _draw_series_on_ax(ax, series_list, dfs, method, force_color=None):
    colors   = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    x_labels = set()
    y_labels = set()

    for ci, s in enumerate(series_list):
        df    = dfs[s["fi"]]
        x_raw = df[s["x_col"]].dropna()
        y_raw = df[s["y_col"]]

        aligned = pd.DataFrame({"x": x_raw, "y": y_raw}).dropna()
        xv, yv  = aligned["x"], aligned["y"]
        if len(xv) == 0:
            continue

        x_filt, y_filt = apply_filter(xv, yv, method)
        color = force_color if force_color is not None else colors[ci % len(colors)]

        if method == "none":
            ax.plot(x_filt, y_filt, marker="o", markersize=2, linewidth=1.2,
                    color=color, label=s["label"])
        else:
            ax.scatter(xv, yv, s=5, alpha=0.18, color=color)
            ax.plot(x_filt, y_filt, linewidth=2.0, color=color, label=s["label"])

        x_labels.add(s["x_col"])
        y_labels.add(s["y_col"])

    return x_labels, y_labels


def _axis_limit_loop(fig, axes_flat):
    print("\nAdjust axis limits:")
    print("  x 0 3000   — set X range")
    print("  y 0 5      — set Y range")
    print("  x auto / y auto  — reset")
    print("  Enter      — done")
    while True:
        raw = input("  > ").strip().lower()
        if not raw:
            break
        parts = raw.split()
        if len(parts) >= 1 and parts[0] in ("x", "y"):
            axis = parts[0]
            if len(parts) == 3:
                try:
                    lo, hi = float(parts[1]), float(parts[2])
                    for ax in axes_flat:
                        if axis == "x":
                            ax.set_xlim(lo, hi)
                        else:
                            ax.set_ylim(lo, hi)
                    fig.canvas.draw_idle()
                    plt.pause(0.05)
                    continue
                except ValueError:
                    pass
            elif len(parts) == 2 and parts[1] == "auto":
                for ax in axes_flat:
                    ax.relim()
                    if axis == "x":
                        ax.autoscale_view(scalex=True,  scaley=False)
                    else:
                        ax.autoscale_view(scalex=False, scaley=True)
                fig.canvas.draw_idle()
                plt.pause(0.05)
                continue
        print("  Usage:  x 0 3000  /  y 0 5  /  x auto  /  y auto  /  Enter to finish")


# ── plot session ──────────────────────────────────────────────────────────────

def run_plot_session(series_list, dfs, labels, default_smooth):
    """Draw one comparison plot from a pre-built series_list."""
    if not series_list:
        print("  No series — nothing to plot.")
        return

    # ── smoothing ─────────────────────────────────────────────────────────
    if default_smooth is not None:
        method = default_smooth
        print(f"\nSmoothing: {method}  (set via --smooth)")
    else:
        print("\nOutlier / smoothing method:")
        print("  none    raw data")
        print("  bin     bin-median  (good for RPM hysteresis loops)")
        print("  sigma   sigma-clipping on a quadratic fit")
        print("  lowess  LOWESS smooth curve")
        method = _ask("Method", ["none", "bin", "sigma", "lowess"])

    # ── grouping ──────────────────────────────────────────────────────────
    print("\nGroup series by a column value?")
    print("  colors    — one colored line per group on the same plot")
    print("  subplots  — one separate subplot per group")
    print("  n         — no grouping")
    group_mode = _ask("Group mode", ["colors", "subplots", "n"])

    if group_mode in ("colors", "subplots"):
        print("\n  Which file contains the grouping column?")
        for fi, lbl in enumerate(labels):
            print(f"    [{fi}]  {lbl}")
        while True:
            try:
                facet_fi = int(input("  File index: ").strip())
                if 0 <= facet_fi < len(dfs):
                    break
            except ValueError:
                pass
            print(f"  Enter 0–{len(dfs)-1}.")

        facet_col = pick_column_from_file(dfs[facet_fi], labels[facet_fi],
                                          "Column to group by:")
        unique_vals = sorted(dfs[facet_fi][facet_col].dropna().unique())
        n = len(unique_vals)

        if group_mode == "colors":
            fig, ax = plt.subplots(figsize=(11, 6))
            group_colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]
            x_labels, y_labels = set(), set()
            for vi, val in enumerate(unique_vals):
                dfs_f = [
                    df[df[facet_col] == val].reset_index(drop=True)
                    if fi == facet_fi and facet_col in df.columns else df
                    for fi, df in enumerate(dfs)
                ]
                val_str = f"{val:.4g}" if isinstance(val, float) else str(val)
                series_f = [{**s, "label": f"{s['label']}  {facet_col}={val_str}"}
                            for s in series_list]
                xl, yl = _draw_series_on_ax(ax, series_f, dfs_f, method,
                                            force_color=group_colors[vi % len(group_colors)])
                x_labels |= xl; y_labels |= yl

            ax.set_xlabel(" / ".join(sorted(x_labels)))
            ax.set_ylabel(" / ".join(sorted(y_labels)))
            title = f"{', '.join(s['y_col'] for s in series_list)}  vs  {' / '.join(sorted(x_labels))}"
            if method != "none":
                title += f"  [{method}]"
            ax.set_title(title); ax.legend(); ax.grid(True, alpha=0.3)
            fig.tight_layout()
            plt.show(block=False); plt.pause(0.1)
            _axis_limit_loop(fig, [ax])

        else:  # subplots
            ncols = min(n, 4)
            nrows = int(np.ceil(n / ncols))
            fig, axes = plt.subplots(nrows, ncols, figsize=(5 * ncols, 4 * nrows),
                                     sharey=True, sharex=True, squeeze=False)
            axes_flat = axes.flatten()
            x_labels, y_labels = set(), set()
            for vi, val in enumerate(unique_vals):
                ax = axes_flat[vi]
                dfs_f = [
                    df[df[facet_col] == val].reset_index(drop=True)
                    if fi == facet_fi and facet_col in df.columns else df
                    for fi, df in enumerate(dfs)
                ]
                xl, yl = _draw_series_on_ax(ax, series_list, dfs_f, method)
                x_labels |= xl; y_labels |= yl
                val_str = f"{val:.4g}" if isinstance(val, float) else str(val)
                ax.set_title(f"{facet_col} = {val_str}", fontsize=9)
                ax.grid(True, alpha=0.3)
            for vi in range(n, len(axes_flat)):
                axes_flat[vi].set_visible(False)
            x_lbl = " / ".join(sorted(x_labels))
            y_lbl = " / ".join(sorted(y_labels))
            for ax in axes[:, 0]:
                ax.set_ylabel(y_lbl)
            for ax in axes[-1, :]:
                ax.set_xlabel(x_lbl)
            handles, leg_labels = axes_flat[0].get_legend_handles_labels()
            if handles:
                fig.legend(handles, leg_labels, loc="upper right",
                           bbox_to_anchor=(1.0, 1.0), fontsize=8)
            suptitle = f"{', '.join(s['y_col'] for s in series_list)}  vs  {x_lbl}"
            if method != "none":
                suptitle += f"  [{method}]"
            fig.suptitle(suptitle, fontsize=10); fig.tight_layout()
            plt.show(block=False); plt.pause(0.1)
            _axis_limit_loop(fig, [ax for ax in axes_flat if ax.get_visible()])

    else:  # single overlay
        fig, ax = plt.subplots(figsize=(11, 6))
        x_labels, y_labels = _draw_series_on_ax(ax, series_list, dfs, method)
        ax.set_xlabel(" / ".join(sorted(x_labels)))
        ax.set_ylabel(" / ".join(sorted(y_labels)))
        title_y = ", ".join(s["y_col"] for s in series_list)
        title   = f"{title_y}  vs  {' / '.join(sorted(x_labels))}"
        if method != "none":
            title += f"  [{method}]"
        ax.set_title(title); ax.legend(); ax.grid(True, alpha=0.3)
        fig.tight_layout()
        plt.show(block=False); plt.pause(0.1)
        _axis_limit_loop(fig, [ax])

    # ── save ──────────────────────────────────────────────────────────────
    if _ask("\nSave plot?", ["y", "n"]) == "y":
        ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
        y_str = "_".join(s["y_col"] for s in series_list)
        x_str = "_".join(sorted(x_labels))
        default_out = Path(__file__).parent / f"compare_{x_str}_vs_{y_str}_{ts}.png"
        raw = input(f"  Save path [{default_out}]: ").strip()
        out  = Path(raw) if raw else default_out
        out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out, dpi=150, bbox_inches="tight")
        print(f"  Saved → {out}")

    plt.show()


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Multi-CSV comparison plotter for propeller theory vs. test data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--files", nargs="*", default=[],
                        help="CSV files to pre-load (optional; files can also be added interactively)")
    parser.add_argument("--labels", nargs="+", default=None,
                        help="Display labels for pre-loaded files (default: filename stem)")
    parser.add_argument("--smooth", choices=["none", "bin", "sigma", "lowess"],
                        default=None,
                        help="Pre-select smoothing method (default: ask interactively)")
    args = parser.parse_args()

    dfs:    list[pd.DataFrame] = []
    labels: list[str]          = []
    paths:  list[str]          = []   # resolved absolute paths, parallel to dfs

    # Pre-load --files if supplied
    if args.files:
        for i, f in enumerate(args.files):
            p = Path(f)
            if not p.exists():
                print(f"Error: file not found: {f}", file=sys.stderr)
                sys.exit(1)
            df  = pd.read_csv(p)
            lbl = (args.labels[i]
                   if args.labels and i < len(args.labels)
                   else p.stem)
            dfs.append(df)
            labels.append(lbl)
            paths.append(str(p.resolve()))

        print("\nPre-loaded files:")
        for fi, (df, lbl) in enumerate(zip(dfs, labels)):
            print(f"  [{fi}]  {lbl}  —  {len(df)} rows × {len(df.columns)} cols")

    # Main plot loop
    while True:
        series_list = collect_series(dfs, labels, paths)
        if series_list:
            run_plot_session(series_list, dfs, labels, args.smooth)
        if _ask("\nMake another plot?", ["y", "n"]) == "n":
            break


if __name__ == "__main__":
    main()
