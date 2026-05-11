import json
import matplotlib.pyplot as plt
import numpy as np

with open("calibration.json") as f:
    cal = json.load(f)

def sorted_pairs(d):
    pairs = sorted((float(k), v) for k, v in d.items())
    return zip(*pairs)

pos_angles, pos_volts = sorted_pairs(cal["positive"])
neg_angles, neg_volts = sorted_pairs(cal["negative"])

pos_angles, pos_volts = list(pos_angles), list(pos_volts)
neg_angles, neg_volts = list(neg_angles), list(neg_volts)

fig, ax = plt.subplots(figsize=(9, 5))

ax.plot(pos_angles, pos_volts, "o-", color="#2196F3", label="Positive peak")
ax.plot(neg_angles, neg_volts, "s-", color="#F44336", label="Negative peak")

# linear fit lines
for angles, volts, color, label in [
    (pos_angles, pos_volts, "#2196F3", "Pos fit"),
    (neg_angles, neg_volts, "#F44336", "Neg fit"),
]:
    coeffs = np.polyfit(angles, volts, 1)
    x_fit = np.linspace(min(angles), max(angles), 200)
    ax.plot(x_fit, np.polyval(coeffs, x_fit), "--", color=color, alpha=0.4,
            label=f"{label}: {coeffs[0]:+.4f}°⁻¹ × angle + {coeffs[1]:.4f} V")

ax.set_xlabel("Pitch angle (°)")
ax.set_ylabel("Peak voltage (V)")
ax.set_title("Hall sensor calibration — voltage vs. pitch angle")
ax.legend()
ax.grid(True, alpha=0.3)
fig.tight_layout()
plt.savefig("calibration_plot.png", dpi=150)
print("Saved calibration_plot.png")
plt.show()
