import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import sys
sys.path.append('.')
from colors import *

RESULTS_CSV = "examples/p-csma/results.csv"
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

ax.set_title("p-CSMA throughput")

G_dense = np.linspace(0.01, END, 100)

S_aloha = G_dense * np.exp(-2 * G_dense)
ax.plot(G_dense, S_aloha, linestyle=':', linewidth=1,
        label='Pure ALOHA', zorder=1, color=BLUE_DARK)

# 1-persistent CSMA — Kleinrock-Tobagi (1975) closed-form
a = 0.000001
numer_1p = G_dense * (1 + G_dense + a*G_dense*(1 + G_dense + a*G_dense/2)) * np.exp(-G_dense*(1 + 2*a))
denom_1p = G_dense*(1 + 2*a) - (1 - np.exp(-a*G_dense)) + (1 + a*G_dense)*np.exp(-G_dense*(1 + a))
S_1p = numer_1p / denom_1p
ax.plot(G_dense, S_1p, color=GREEN, linestyle='-', linewidth=1.5,
        label='p = 1.0 (1-persistent)', zorder=2)

# ---------- Simulated data points ----------

ax.scatter(df['G'], df['S'], color=RED, marker='*', label='Simulated', zorder=4)

ax.legend(loc='upper right', fontsize='small')
fig.set_figwidth(7)
fig.tight_layout()
plt.show()
