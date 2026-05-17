import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import sys
sys.path.append('.')
from colors import *

RESULTS_CSV = "examples/p-csma/results.csv"
df = pd.read_csv(RESULTS_CSV)


# ---------------------------------------------------------------------------
# Numerical p-persistent CSMA throughput (Poisson contention model)
# ---------------------------------------------------------------------------
def p_csma_throughput(G_array, p, a, max_slots=5000):
    """
    Compute the throughput of p-persistent CSMA numerically.

    Model: after each busy period (duration 1+a), a backlog of B_0 = G*(1+a)
    stations enters the contention window.  New stations arrive at rate
    beta = G*a per mini-slot.  In each idle slot j, the backlog evolves as:

        B_1 = B_0 + beta
        B_{j+1} = B_j * (1-p) + beta

    and the Poisson attempt rate is lambda_j = B_j * p.

    A "cycle" = contention window (idle slots) + one busy period (1+a).
    Throughput  S = P(success per cycle) / E[cycle time].
    """
    S = np.empty_like(G_array, dtype=float)

    for idx, G in enumerate(G_array):
        beta = G * a                # new arrivals per mini-slot
        B0 = G * (1.0 + a)         # initial backlog from previous busy period
        B1 = B0 + beta              # total ready at start of slot 1

        if p >= 1.0:
            # 1-persistent: all backlog transmits in slot 1.
            # Thereafter only new arrivals (beta) per slot.
            def lam(j):
                return B1 if j == 1 else beta
        else:
            steady = beta / p       # steady-state backlog B_∞
            diff = B1 - steady      # transient component
            def lam(j):
                # B_j = steady + diff * (1-p)^{j-1}
                # attempt rate = B_j * p
                B_j = steady + diff * (1.0 - p) ** (j - 1)
                return max(B_j * p, 0.0)

        p_success = 0.0
        e_idle_slots = 0.0
        log_cum_idle = 0.0

        for j in range(1, max_slots + 1):
            lam_j = lam(j)

            cum_idle = np.exp(log_cum_idle)
            if cum_idle < 1e-18:
                break

            p_success += cum_idle * lam_j * np.exp(-lam_j)
            p_first_tx_j = cum_idle * (1.0 - np.exp(-lam_j))
            e_idle_slots += (j - 1) * p_first_tx_j

            log_cum_idle -= lam_j

        e_cycle = e_idle_slots * a + (1.0 + a)
        S[idx] = p_success / e_cycle if e_cycle > 0 else 0.0

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

# Normalized propagation delay — the simulation uses SLOT_TIME = 1 µs with
# PACKET_TIME ≈ 46 ms, giving a ≈ 2.2e-5.  For the theoretical curves we use
# a value very close to zero so they match the idealised Kleinrock-Tobagi model.
a = 0.00001

# Dense G values for smooth theoretical curves
G_dense = np.linspace(0.01, END, 300)

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

# p-persistent CSMA — numerical contention model
S_p001 = p_csma_throughput(G_dense, p=0.01, a=a)
S_p01 = p_csma_throughput(G_dense, p=0.1, a=a)
S_p03 = p_csma_throughput(G_dense, p=0.3, a=a)
S_p05 = p_csma_throughput(G_dense, p=0.5, a=a)

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
