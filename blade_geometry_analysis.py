"""
Blade geometry feasibility — required twist vs. RPM and freestream velocity.

For each (RPM, V_inf) operating point the local inflow angle phi at every
radial station is:

    phi(r) = atan2(V_inf, Omega * r)          [rad]

For a "good" angle of attack alpha_design the required blade pitch is:

    beta(r) = phi(r) + alpha_design

The script plots
  1.  Required pitch at the reference radius (contour over RPM × V_inf)
  2.  Advance ratio J contours
  3.  Full twist distribution along the blade for selected operating points
  4.  Inflow angle distribution for the same points
  5.  Tip Mach number  M_tip = sqrt((Ω·R)² + V∞²) / a  (full-width bottom panel)
"""

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

# =============================================================================
# Configuration — edit these to match your propeller / test conditions
# =============================================================================
D          = 0.3         # Propeller diameter [m]  (≈ 10 inch)
R          = D / 2          # Tip radius [m]
alpha_opt  = 5.0            # Design angle of attack [°] — "good" AoA target
r_ref      = 0.75           # Reference radial station as fraction of R
a_sound    = 343.0          # Speed of sound [m/s]  (ISA sea level)

rpm_min, rpm_max = 1_000, 30_000
v_min,   v_max   =     1,     70    # [m/s]

N_rpm = 200   # grid resolution
N_v   = 200
N_r   = 100   # radial stations for twist distribution plots

# Operating points to highlight (RPM, V_inf [m/s])
op_points = [
    (2_000,  5.0),
    (4_000, 10.0),
    (7_000, 15.0),
    (10_000, 22.0),
]

# =============================================================================
# Grid computation
# =============================================================================
rpm_arr = np.linspace(rpm_min, rpm_max, N_rpm)
v_arr   = np.linspace(v_min,   v_max,   N_v)
RPM, V  = np.meshgrid(rpm_arr, v_arr)

n_rps = RPM / 60.0
Omega  = 2.0 * np.pi * n_rps

# Inflow angle and required pitch at reference radius
phi_ref  = np.degrees(np.arctan2(V, Omega * (r_ref * R)))
beta_ref = phi_ref + alpha_opt

# Advance ratio
J = V / (n_rps * D)

# Tip Mach number — total relative velocity at blade tip
V_tip = np.sqrt((Omega * R) ** 2 + V ** 2)
M_tip = V_tip / a_sound

# Radial stations (avoid hub singularity)
r_norm = np.linspace(0.15, 1.0, N_r)

# Pre-compute twist distributions for the selected operating points
op_data = []
for rpm_op, v_op in op_points:
    omega_op  = rpm_op * 2.0 * np.pi / 60.0
    J_op      = v_op / (rpm_op / 60.0 * D)
    phi_dist  = np.degrees(np.arctan2(v_op, omega_op * r_norm * R))
    beta_dist = phi_dist + alpha_opt
    op_data.append((rpm_op, v_op, J_op, phi_dist, beta_dist))

# =============================================================================
# Plotting
# =============================================================================
COLORS = plt.cm.Set1(np.linspace(0, 0.8, len(op_points)))

fig = plt.figure(figsize=(16, 17))
fig.suptitle(
    f"Blade Geometry Feasibility  |  D = {D*100:.1f} cm  |"
    f"  α_design = {alpha_opt}°  |  ref station = {r_ref*100:.0f}% R",
    fontsize=13, fontweight="bold",
)
gs = gridspec.GridSpec(3, 2, figure=fig, hspace=0.42, wspace=0.35)

# ── Panel 1 : Required pitch at reference radius ──────────────────────────────
ax1 = fig.add_subplot(gs[0, 0])
cf1 = ax1.contourf(rpm_arr, v_arr, beta_ref, levels=30, cmap="plasma")
cb1 = plt.colorbar(cf1, ax=ax1)
cb1.set_label(f"β at {r_ref*100:.0f}% R  [°]", fontsize=9)
# iso-lines every 5°
beta_lines = np.arange(np.floor(beta_ref.min()), np.ceil(beta_ref.max()) + 1, 5)
cs1 = ax1.contour(rpm_arr, v_arr, beta_ref, levels=beta_lines,
                   colors="white", linewidths=0.6, alpha=0.7)
ax1.clabel(cs1, fmt="%d°", fontsize=7, inline=True)
for i, (rpm_op, v_op, J_op, *_) in enumerate(op_data):
    ax1.scatter(rpm_op, v_op, color=COLORS[i], s=90, zorder=6,
                edgecolors="k", linewidths=0.9)
    ax1.annotate(f"OP{i+1}", (rpm_op, v_op),
                 textcoords="offset points", xytext=(6, 4),
                 fontsize=7.5, color=COLORS[i], fontweight="bold")
ax1.set_xlabel("RPM")
ax1.set_ylabel("V∞  [m/s]")
ax1.set_title(f"Required blade pitch  β  at {r_ref*100:.0f}% R")
ax1.grid(True, alpha=0.15)

# ── Panel 2 : Advance ratio J ─────────────────────────────────────────────────
ax2 = fig.add_subplot(gs[0, 1])
cf2 = ax2.contourf(rpm_arr, v_arr, J, levels=30, cmap="viridis")
cb2 = plt.colorbar(cf2, ax=ax2)
cb2.set_label("Advance ratio  J = V∞ / (n·D)  [–]", fontsize=9)
J_iso = [0.1, 0.2, 0.3, 0.5, 0.7, 1.0, 1.5, 2.0, 3.0]
cs2 = ax2.contour(rpm_arr, v_arr, J, levels=J_iso,
                   colors="white", linewidths=0.7, alpha=0.8)
ax2.clabel(cs2, fmt="J=%.1f", fontsize=7.5, inline=True)
for i, (rpm_op, v_op, J_op, *_) in enumerate(op_data):
    ax2.scatter(rpm_op, v_op, color=COLORS[i], s=90, zorder=6,
                edgecolors="k", linewidths=0.9)
    ax2.annotate(f"OP{i+1}", (rpm_op, v_op),
                 textcoords="offset points", xytext=(6, 4),
                 fontsize=7.5, color=COLORS[i], fontweight="bold")
ax2.set_xlabel("RPM")
ax2.set_ylabel("V∞  [m/s]")
ax2.set_title("Advance ratio  J")
ax2.grid(True, alpha=0.15)

# ── Panel 3 : Twist (pitch) distribution along the blade ─────────────────────
ax3 = fig.add_subplot(gs[1, 0])
for i, (rpm_op, v_op, J_op, phi_dist, beta_dist) in enumerate(op_data):
    ax3.plot(r_norm, beta_dist, color=COLORS[i], linewidth=2.2,
             label=f"OP{i+1}: {rpm_op} RPM, {v_op} m/s  (J={J_op:.2f})")
ax3.axvline(r_ref, color="gray", linestyle="--", linewidth=1.2, alpha=0.7,
            label=f"{r_ref*100:.0f}% R reference")
ax3.set_xlabel("r / R  [–]")
ax3.set_ylabel("Blade pitch angle  β  [°]")
ax3.set_title(f"Required twist distribution  (α_design = {alpha_opt}°)")
ax3.legend(fontsize=7.5, loc="upper right")
ax3.grid(True, alpha=0.25)
ax3.set_xlim(r_norm[0], 1.0)

# ── Panel 4 : Inflow angle distribution ──────────────────────────────────────
ax4 = fig.add_subplot(gs[1, 1])
for i, (rpm_op, v_op, J_op, phi_dist, beta_dist) in enumerate(op_data):
    ax4.plot(r_norm, phi_dist, color=COLORS[i], linewidth=2.2,
             label=f"OP{i+1}: {rpm_op} RPM, {v_op} m/s  (J={J_op:.2f})")
ax4.axvline(r_ref, color="gray", linestyle="--", linewidth=1.2, alpha=0.7)
ax4.set_xlabel("r / R  [–]")
ax4.set_ylabel("Inflow angle  φ  [°]")
ax4.set_title("Inflow angle  φ = atan(V∞ / Ω·r)")
ax4.legend(fontsize=7.5, loc="upper right")
ax4.grid(True, alpha=0.25)
ax4.set_xlim(r_norm[0], 1.0)
ax4.annotate(
    f"β(r) = φ(r) + α_design\nα_design = {alpha_opt}°",
    xy=(0.97, 0.96), xycoords="axes fraction",
    ha="right", va="top", fontsize=8.5,
    bbox=dict(boxstyle="round,pad=0.35", facecolor="lightyellow",
               edgecolor="gray", alpha=0.9),
)

# ── Panel 5 : Tip Mach number (full-width bottom row) ────────────────────────
ax5 = fig.add_subplot(gs[2, :])
cf5 = ax5.contourf(rpm_arr, v_arr, M_tip, levels=30, cmap="RdYlGn_r")
cb5 = plt.colorbar(cf5, ax=ax5)
cb5.set_label("Tip Mach  M_tip  [–]", fontsize=9)
# Highlight aerodynamic thresholds
M_thresholds = {0.5: ("cyan",   "M=0.5  (onset of wave drag)"),
                0.7: ("yellow", "M=0.7"),
                0.85:("orange", "M=0.85 (drag-divergence region)"),
                1.0: ("red",    "M=1.0  (sonic tip)")}
for M_val, (col, lbl) in M_thresholds.items():
    cs5 = ax5.contour(rpm_arr, v_arr, M_tip, levels=[M_val],
                      colors=[col], linewidths=1.6)
    ax5.clabel(cs5, fmt=lbl, fontsize=7.5, inline=True)
for i, (rpm_op, v_op, J_op, *_) in enumerate(op_data):
    ax5.scatter(rpm_op, v_op, color=COLORS[i], s=90, zorder=6,
                edgecolors="k", linewidths=0.9)
    ax5.annotate(f"OP{i+1}", (rpm_op, v_op),
                 textcoords="offset points", xytext=(6, 4),
                 fontsize=7.5, color=COLORS[i], fontweight="bold")
ax5.set_xlabel("RPM")
ax5.set_ylabel("V∞  [m/s]")
ax5.set_title(
    f"Tip Mach number  M_tip = √((Ω·R)² + V∞²) / a     "
    f"[a = {a_sound} m/s,  R = {R*100:.1f} cm]"
)
ax5.grid(True, alpha=0.15)

plt.savefig("blade_geometry_analysis.png", dpi=150, bbox_inches="tight")
plt.show()
print("Saved → blade_geometry_analysis.png")
