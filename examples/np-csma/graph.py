import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import sys
sys.path.append('.')
from colors import *

RESULTS_CSV = "examples/np-csma/results.csv"
df = pd.read_csv(RESULTS_CSV)

# ---------------------------------------------------------------------------
# Figure
# ---------------------------------------------------------------------------
fig, ax = plt.subplots(1, 1)

END = 7

ax.grid(zorder=0)
ax.set_xlim(0, END)
ax.set_xticks(np.arange(0, END, 0.5))
ax.set_xlabel("Offered load (G)")

ax.set_ylim(0, 1.0)
ax.set_ylabel("Throughput (S)")

ax.set_title("Non-persistent CSMA throughput")

# Normalised propagation delay (a ≈ 0 in this simulation)
a = 0.00004

# Dense G values for smooth theoretical curves
G_dense = np.linspace(0.01, END, 200)

# ---------- Reference curves ----------

# Pure ALOHA
S_aloha = G_dense * np.exp(-2 * G_dense)
ax.plot(G_dense, S_aloha, linestyle='--', linewidth=1,
        label='Pure ALOHA', zorder=1, color=BLUE_DARK)

# 1-persistent CSMA — Kleinrock-Tobagi (1975) closed-form
numer_1p = G_dense * (1 + G_dense + a*G_dense*(1 + G_dense + a*G_dense/2)) * np.exp(-G_dense*(1 + 2*a))
denom_1p = G_dense*(1 + 2*a) - (1 - np.exp(-a*G_dense)) + (1 + a*G_dense)*np.exp(-G_dense*(1 + a))
S_1p = numer_1p / denom_1p
ax.plot(G_dense, S_1p, color=GREEN_DARK, linestyle='--', linewidth=1,
        label='1-persistent CSMA', zorder=1)

# ---------- Non-persistent CSMA theoretical curve ----------

# Kleinrock-Tobagi (1975) closed-form
S_np = G_dense * np.exp(-a * G_dense) / (G_dense * (1 + 2 * a) + np.exp(-a * G_dense))
ax.plot(G_dense, S_np, color=ORANGE_DARK, linestyle='--', linewidth=1,
        label='Non-persistent CSMA', zorder=2)

# ---------- Simulated data points ----------

# Use target G (arrival rate) as x-axis — this matches the K-T model's G
ax.scatter(df['G'], df['S'], color=RED, marker='*',
           label='Simulated throughput', zorder=4)

ax.legend(loc='lower right')
fig.set_figwidth(7)
fig.tight_layout()
plt.savefig('examples/np-csma/np_csma_throughput.png', dpi=150, bbox_inches='tight')
plt.show()
