#!/usr/bin/env python3
"""
compare_plot.py — Multi-CSV comparison plotter.

Load any number of CSVs (theoretical performance + test-stand recordings),
add optional derived columns, then interactively build overlay plots by
picking X and Y columns from any file.

Usage
-----
    python compare_plot.py --files FILE1.csv FILE2.csv ...
        [--labels "Theory" "Test run 1"]
        [--smooth none|bin|sigma|lowess]

Typical workflow
----------------
    # 1. Generate theoretical CSV from blade design
    python ../Fusion\ Scripts/blade_evaluator.py objects/prop_X/ExportedParameters.csv --vinf 0

    # 2. Compare with a test-stand recording
    python compare_plot.py \\
        --files "../Fusion Scripts/objects/prop_X/prop_X_theoretical.csv" \\
                "recordings/recording_20260505_154110.csv" \\
        --labels "Theory (V_inf=0)" "Bench test"

Derived column examples (enter at the prompt)
----------------------------------------------
    1.T_N = 1.weight_g * 9.81 / 1000      # grams → Newtons
    1.P_W = 1.voltage_v * 1.current_a     # electrical power
    0.RPM_krpm = 0.RPM / 1000             # RPM → kRPM

Row filter examples (pandas query syntax)
------------------------------------------
    0: V_inf_ms == 0        # hover-only rows from theoretical file
    1: throttle > 200       # exclude low-throttle noise
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

# ── smoothing (shared with plot_xy.py) ───────────────────────────────────────

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


# ── interactive helpers ───────────────────────────────────────────────────────

def _ask(prompt, options):
    opts = "/".join(options)
    while True:
        val = input(f"{prompt} [{opts}]: ").strip().lower()
        if val in options:
            return val
        print(f"  Please enter one of: {opts}")


def print_columns(dfs, labels):
    """Print a numbered index of all columns across all loaded files."""
    print()
    idx = 0
    for fi, (df, label) in enumerate(zip(dfs, labels)):
        print(f"  File {fi}: {label}  ({len(df)} rows)")
        for col in df.columns:
            print(f"    [{idx:3d}]  {col}")
            idx += 1
    print()


def pick_column(dfs, labels, prompt):
    """Let the user pick one column by global number or 'fi.colname'."""
    choices = []
    for fi, df in enumerate(dfs):
        for col in df.columns:
            choices.append((fi, col))

    print(f"  {prompt}")
    while True:
        raw = input("  Enter number or fi.colname  (e.g. 0.RPM): ").strip()
        # Numeric index
        try:
            n = int(raw)
            if 0 <= n < len(choices):
                return choices[n]
        except ValueError:
            pass
        # fi.colname syntax
        m = re.match(r"^(\d+)\.(.+)$", raw)
        if m:
            fi, col = int(m.group(1)), m.group(2)
            if 0 <= fi < len(dfs) and col in dfs[fi].columns:
                return fi, col
        print(f"  Invalid — enter a number 0–{len(choices)-1} or 'fi.colname'.")


def pick_column_from_file(df, label, prompt):
    """Pick one column from a single DataFrame."""
    cols = list(df.columns)
    print(f"\n  {prompt}  (file: {label})")
    for i, c in enumerate(cols):
        print(f"    [{i:3d}]  {c}")
    while True:
        raw = input("  Enter number or column name: ").strip()
        try:
            n = int(raw)
            if 0 <= n < len(cols):
                return cols[n]
        except ValueError:
            pass
        if raw in cols:
            return raw
        print(f"  Invalid — enter 0–{len(cols)-1} or a column name.")


# ── derived columns ───────────────────────────────────────────────────────────

def add_derived_columns(dfs, labels):
    """Prompt user to add computed columns to any loaded DataFrame.

    Syntax:  fi.new_col = expression
    In the expression, reference columns as  fi.col_name.
    Standard shortcuts are pre-expanded before eval:
        weight_g → N:   fi.T_N = fi.weight_g * 9.81 / 1000
        electrical P:   fi.P_W = fi.voltage_v * fi.current_a
    """
    print("\nDerived columns  (press Enter to skip)")
    print("  Syntax:  fi.new_col = expression  referencing fi.existing_col")
    print("  Example: 1.T_N = 1.weight_g * 9.81 / 1000")
    print("  Example: 1.P_W = 1.voltage_v * 1.current_a")

    while True:
        raw = input("  > ").strip()
        if not raw:
            break

        m = re.match(r"^(\d+)\.(\w+)\s*=\s*(.+)$", raw)
        if not m:
            print("  Bad syntax — use  fi.new_col = expression")
            continue

        fi, new_col, expr = int(m.group(1)), m.group(2), m.group(3)
        if fi >= len(dfs):
            print(f"  File index {fi} out of range (have {len(dfs)} files).")
            continue

        df = dfs[fi]

        # Replace every fi.colname token with a safe df["colname"] reference.
        # Tokens from OTHER files are left untouched (will fail cleanly in eval).
        def _replace(match):
            ref_fi = int(match.group(1))
            ref_col = match.group(2)
            if ref_fi == fi and ref_col in df.columns:
                return f'_df["{ref_col}"]'
            elif ref_fi != fi:
                # cross-file references: substitute the other df's series
                if ref_fi < len(dfs) and ref_col in dfs[ref_fi].columns:
                    return f'_dfs[{ref_fi}]["{ref_col}"]'
            return match.group(0)

        safe_expr = re.sub(r"(\d+)\.(\w+)", _replace, expr)

        try:
            result = eval(safe_expr, {"_df": df, "_dfs": dfs, "np": np})
            df[new_col] = result
            print(f"  ✓ Added  {fi}.{new_col}  to  '{labels[fi]}'")
        except Exception as e:
            print(f"  Error: {e}")


# ── row filters ───────────────────────────────────────────────────────────────

def apply_row_filters(dfs, labels):
    """Optionally filter rows of any file using pandas query syntax."""
    print("\nRow filters  (press Enter to skip)")
    print("  Syntax:  [fi:] query_expression  (fi defaults to 0)")
    print("  Example: V_inf_ms == 0        ← applies to file 0")
    print("  Example: 1: throttle > 200    ← applies to file 1")

    while True:
        raw = input("  > ").strip()
        if not raw:
            break
        m = re.match(r"^(\d+)\s*:\s*(.+)$", raw)
        if m:
            fi = int(m.group(1))
            query = m.group(2).strip()
        else:
            fi = 0
            query = raw
        if fi >= len(dfs):
            print(f"  File index {fi} out of range (have {len(dfs)} files).")
            continue
        # Fix single = → == (but leave ==, !=, <=, >= untouched)
        fixed = re.sub(r"(?<![=!<>])=(?!=)", "==", query)
        if fixed != query:
            print(f"  (auto-fixed '=' → '=='  →  {fixed})")
            query = fixed
        before = len(dfs[fi])
        try:
            dfs[fi] = dfs[fi].query(query).reset_index(drop=True)
            print(f"  ✓ File {fi} ({labels[fi]}): {before} → {len(dfs[fi])} rows  (filter: {query})")
        except Exception as e:
            print(f"  Error: {e}")


# ── plot session ──────────────────────────────────────────────────────────────

def _draw_series_on_ax(ax, series_list, dfs, method, force_color=None):
    """Plot all series onto ax. Returns (x_labels, y_labels) sets.

    force_color: when set, overrides the color cycle for all series in this call.
    """
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
    """Interactive axis-limit adjustment shared between single and faceted plots."""
    print("\nAdjust axis limits (applies to all subplots):")
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


def run_plot_session(dfs, labels, default_smooth):
    """Collect series interactively and draw one comparison plot."""

    print_columns(dfs, labels)

    # ── collect series ────────────────────────────────────────────────────────
    series_list = []
    print("Add series to plot. Each series picks its X and Y from one file.")
    while True:
        if series_list:
            print(f"\n  Current series ({len(series_list)}):")
            for i, s in enumerate(series_list):
                print(f"    [{i}]  {labels[s['fi']]}.{s['x_col']}  →  {s['y_col']}  [{s['label']}]")

        if _ask("\nAdd a series?", ["y", "n"]) == "n":
            break

        print("\n  Available files:")
        for fi, lbl in enumerate(labels):
            print(f"    [{fi}]  {lbl}  ({len(dfs[fi])} rows)")
        while True:
            try:
                fi = int(input("  File index: ").strip())
                if 0 <= fi < len(dfs):
                    break
            except ValueError:
                pass
            print(f"  Enter 0–{len(dfs)-1}.")

        x_col = pick_column_from_file(dfs[fi], labels[fi], "X column:")
        y_col = pick_column_from_file(dfs[fi], labels[fi], "Y column:")

        default_label = f"{labels[fi]}: {y_col}"
        raw_label = input(f"  Series label [{default_label}]: ").strip()
        series_list.append({
            "fi": fi, "x_col": x_col, "y_col": y_col,
            "label": raw_label if raw_label else default_label,
        })
        print("  ✓ Series added.")

    if not series_list:
        print("  No series selected — skipping plot.")
        return

    # ── smoothing ─────────────────────────────────────────────────────────────
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

    # ── grouping ──────────────────────────────────────────────────────────────
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
                                          "Column to group by (e.g. V_inf_ms or J):")

        unique_vals = sorted(dfs[facet_fi][facet_col].dropna().unique())
        n = len(unique_vals)

        if group_mode == "colors":
            # ── one colored line per group on the same plot ───────────────────
            fig, ax = plt.subplots(figsize=(11, 6))
            group_colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]
            x_labels = set()
            y_labels = set()

            for vi, val in enumerate(unique_vals):
                dfs_f = []
                for fi, df in enumerate(dfs):
                    if fi == facet_fi and facet_col in df.columns:
                        dfs_f.append(df[df[facet_col] == val].reset_index(drop=True))
                    else:
                        dfs_f.append(df)

                val_str = f"{val:.4g}" if isinstance(val, float) else str(val)
                series_f = [
                    {**s, "label": f"{s['label']}  {facet_col}={val_str}"}
                    for s in series_list
                ]
                color = group_colors[vi % len(group_colors)]
                xl, yl = _draw_series_on_ax(ax, series_f, dfs_f, method,
                                            force_color=color)
                x_labels |= xl
                y_labels |= yl

            ax.set_xlabel(" / ".join(sorted(x_labels)))
            ax.set_ylabel(" / ".join(sorted(y_labels)))
            title = f"{', '.join(s['y_col'] for s in series_list)}  vs  {' / '.join(sorted(x_labels))}"
            if method != "none":
                title += f"  [{method}]"
            ax.set_title(title)
            ax.legend()
            ax.grid(True, alpha=0.3)
            fig.tight_layout()

            plt.show(block=False)
            plt.pause(0.1)
            _axis_limit_loop(fig, [ax])

        else:
            # ── one subplot per group ─────────────────────────────────────────
            ncols = min(n, 4)
            nrows = int(np.ceil(n / ncols))

            fig, axes = plt.subplots(nrows, ncols,
                                     figsize=(5 * ncols, 4 * nrows),
                                     sharey=True, sharex=True,
                                     squeeze=False)
            axes_flat = axes.flatten()
            x_labels  = set()
            y_labels  = set()

            for vi, val in enumerate(unique_vals):
                ax = axes_flat[vi]

                dfs_f = []
                for fi, df in enumerate(dfs):
                    if fi == facet_fi and facet_col in df.columns:
                        dfs_f.append(df[df[facet_col] == val].reset_index(drop=True))
                    else:
                        dfs_f.append(df)

                xl, yl = _draw_series_on_ax(ax, series_list, dfs_f, method)
                x_labels |= xl
                y_labels |= yl

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
            fig.suptitle(suptitle, fontsize=10)
            fig.tight_layout()

            plt.show(block=False)
            plt.pause(0.1)
            _axis_limit_loop(fig, [ax for ax in axes_flat if ax.get_visible()])

    else:
        # ── single overlay plot ───────────────────────────────────────────────
        fig, ax = plt.subplots(figsize=(11, 6))

        x_labels, y_labels = _draw_series_on_ax(ax, series_list, dfs, method)

        ax.set_xlabel(" / ".join(sorted(x_labels)))
        ax.set_ylabel(" / ".join(sorted(y_labels)))
        title_y = ", ".join(s["y_col"] for s in series_list)
        title   = f"{title_y}  vs  {' / '.join(sorted(x_labels))}"
        if method != "none":
            title += f"  [{method}]"
        ax.set_title(title)
        ax.legend()
        ax.grid(True, alpha=0.3)
        fig.tight_layout()

        plt.show(block=False)
        plt.pause(0.1)
        _axis_limit_loop(fig, [ax])

    # ── save ──────────────────────────────────────────────────────────────────
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
    parser.add_argument("--files", nargs="+", required=True,
                        help="CSV files to load (theoretical and/or recordings)")
    parser.add_argument("--labels", nargs="+", default=None,
                        help="Display labels for each file (default: filename stem)")
    parser.add_argument("--smooth", choices=["none", "bin", "sigma", "lowess"],
                        default=None,
                        help="Pre-select smoothing method (default: ask interactively)")
    args = parser.parse_args()

    dfs = []
    for f in args.files:
        p = Path(f)
        if not p.exists():
            print(f"Error: file not found: {f}", file=sys.stderr)
            sys.exit(1)
        df = pd.read_csv(p)
        dfs.append(df)

    if args.labels:
        labels = list(args.labels)
        while len(labels) < len(dfs):
            labels.append(Path(args.files[len(labels)]).stem)
    else:
        labels = [Path(f).stem for f in args.files]

    print("\nLoaded files:")
    for fi, (df, lbl) in enumerate(zip(dfs, labels)):
        print(f"  [{fi}]  {lbl}  —  {len(df)} rows × {len(df.columns)} cols")

    # Optional: add derived columns and row filters
    add_derived_columns(dfs, labels)
    apply_row_filters(dfs, labels)

    # Plot loop
    while True:
        run_plot_session(dfs, labels, args.smooth)
        if _ask("\nMake another plot?", ["y", "n"]) == "n":
            break


if __name__ == "__main__":
    main()
