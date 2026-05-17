import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import sys
sys.path.append('.')
from colors import *

RESULTS_CSV = "examples/p-csma/results.csv"
df = pd.read_csv(RESULTS_CSV)


# ---------------------------------------------------------------------------
# Monte-Carlo p-persistent CSMA throughput (a = 0, with carryover)
# ---------------------------------------------------------------------------
def p_csma_throughput_mc(G_array, p, num_cycles=20_000, seed=42):
    """
    Estimate the steady-state throughput of p-persistent CSMA at a = 0
    using a fast cycle-level Monte Carlo with correct carryover tracking.

    Each cycle:
      1. B = carryover + Poisson(G) new arrivals.
      2. If B = 0: channel idle until next lone arrival → success.
      3. If B >= 1: contention (zero-time slots until someone transmits).
         - K = 1: success.   K >= 2: collision.
         Carryover = B - K.
    """
    rng = np.random.default_rng(seed)
    S = np.empty(len(G_array), dtype=float)

    for idx, G in enumerate(G_array):
        if G < 1e-10:
            S[idx] = 0.0
            continue

        successes = 0
        total_time = 0.0
        carry = 0

        for _ in range(num_cycles):
            b = carry + rng.poisson(G)

            if b == 0:
                total_time += rng.exponential(1.0 / G) + 1.0
                successes += 1
                carry = 0
                continue

            if b == 1:
                tx = 1
            elif p >= 1.0:
                tx = b
            else:
                tx = 0
                while tx == 0:
                    tx = rng.binomial(b, p)

            if tx == 1:
                successes += 1

            carry = b - tx
            total_time += 1.0

        S[idx] = successes / total_time if total_time > 0 else 0.0

    return S


# ---------------------------------------------------------------------------
# Figure
# ---------------------------------------------------------------------------
fig, ax = plt.subplots(1, 1)

END = 8

ax.grid(zorder=0)
ax.set_xlim(0, END)
ax.set_xticks(np.arange(0, END, 0.5))
ax.set_xlabel("Offered load (G)")

ax.set_ylim(0, 1.0)
ax.set_ylabel("Throughput (S)")

ax.set_title("p-CSMA throughput")

# Normalised propagation delay for the K-T closed-form curves.
# The simulation uses SLOT_TIME = 2 µs, PACKET_TIME ≈ 46 ms → a ≈ 4.3e-5.
a = 0.00004

# Dense G values for smooth theoretical curves
G_dense = np.linspace(0.01, END, 100)

# ---------- Reference curves (ALOHA) ----------

S_aloha = G_dense * np.exp(-2 * G_dense)
ax.plot(G_dense, S_aloha, linestyle=':', linewidth=1,
        label='Pure ALOHA', zorder=1, color=BLUE_DARK)

S_slotted = G_dense * np.exp(-G_dense)
ax.plot(G_dense, S_slotted, linestyle='--', linewidth=1,
        label='Slotted ALOHA', zorder=1, color=RED)

# ---------- Theoretical CSMA curves ----------

# 1-persistent CSMA — Kleinrock-Tobagi (1975) closed-form
numer_1p = G_dense * (1 + G_dense + a*G_dense*(1 + G_dense + a*G_dense/2)) * np.exp(-G_dense*(1 + 2*a))
denom_1p = G_dense*(1 + 2*a) - (1 - np.exp(-a*G_dense)) + (1 + a*G_dense)*np.exp(-G_dense*(1 + a))
S_1p = numer_1p / denom_1p

# Non-persistent CSMA — Kleinrock-Tobagi (1975) closed-form
S_np = G_dense * np.exp(-a * G_dense) / (G_dense * (1 + 2 * a) + np.exp(-a * G_dense))

# p-persistent CSMA — Monte-Carlo cycle model (a = 0, with carryover)
print("Computing p-persistent theoretical curves (Monte Carlo) …")
S_p001 = p_csma_throughput_mc(G_dense, p=0.01)
S_p01  = p_csma_throughput_mc(G_dense, p=0.1)
S_p03  = p_csma_throughput_mc(G_dense, p=0.3)
S_p05  = p_csma_throughput_mc(G_dense, p=0.5)
print("Done.")

# Color palette shared between theoretical curves and simulation dots
p_vals = [0.1, 0.3, 0.5, 1.0]
cmap_colors = plt.cm.viridis(np.linspace(0.15, 0.85, len(p_vals)))
p_colors = dict(zip(p_vals, cmap_colors))

# Plot theoretical curves
ax.plot(G_dense, S_np,   color='gray', linestyle='-.', linewidth=1,
        label='Non-persistent CSMA', zorder=2)
ax.plot(G_dense, S_p001, color='tab:cyan', linestyle='-', linewidth=1.5,
        label='p = 0.01', zorder=2)
ax.plot(G_dense, S_p01,  color=p_colors[0.1], linestyle='-', linewidth=1.5,
        label='p = 0.1', zorder=2)
ax.plot(G_dense, S_p03,  color=p_colors[0.3], linestyle='-', linewidth=1.5,
        label='p = 0.3', zorder=2)
ax.plot(G_dense, S_p05,  color=p_colors[0.5], linestyle='-', linewidth=1.5,
        label='p = 0.5', zorder=2)
ax.plot(G_dense, S_1p,   color=p_colors[1.0], linestyle='-', linewidth=1.5,
        label='p = 1.0 (1-persistent)', zorder=2)

# ---------- Simulated data points ----------

if 'p' in df.columns:
    for p_val in sorted(df['p'].unique()):
        subset = df[df['p'] == p_val].sort_values('G_measured')
        color = p_colors.get(p_val, 'black')
        ax.scatter(subset['G_measured'], subset['S'], color=color, marker='o',
                   s=30, zorder=4, edgecolors='white', linewidths=0.4,
                   label=f'p = {p_val:.1f} (sim)')
else:
    ax.scatter(df['G_measured'], df['S'], color=RED, marker='*',
               label='Simulated', zorder=4)

ax.legend(loc='upper right', fontsize='small')
fig.set_figwidth(7)
fig.tight_layout()
plt.show()
