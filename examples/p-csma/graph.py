import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import sys
sys.path.append('.')
from colors import *

RESULTS_CSV = "examples/p-csma/results.csv"
df = pd.read_csv(RESULTS_CSV)

fig, ax = plt.subplots(1, 1)

END = 7

ax.grid(zorder=0)
ax.set_xlim(0, END)
ax.set_xticks(np.arange(0, END, 0.5))
ax.set_xlabel("Offered load (G)")

ax.set_ylim(0)
# ax.set_ylim(0, 1.0)
ax.set_ylabel("Throughput (S)")

ax.set_title("p-CSMA throughput")

# Plot simulated data points, colored by persistence probability p
if 'p' in df.columns:
    unique_p = df['p'].unique()
    colors = plt.cm.viridis(np.linspace(0, 1, len(unique_p)))
    for i, p_val in enumerate(sorted(unique_p)):
        subset = df[df['p'] == p_val]
        ax.scatter(subset["G"], subset["S"], color=colors[i], marker='o', 
                   label=f'p = {p_val:.2f}', zorder=2)
else:
    ax.scatter(df["G"], df["S"], color=RED, marker='*', label='Simulated throughput', zorder=2)

# Theoretical curves for comparison
G = np.linspace(0.01, END, 100)

# Pure ALOHA for reference
S_aloha = G * np.exp(-2 * G)
ax.plot(G, S_aloha, linestyle=':', linewidth=1, label='Pure ALOHA', zorder=3, color=BLUE_DARK)

# Slotted ALOHA for reference
S_slotted = G * np.exp(-G)
ax.plot(G, S_slotted, linestyle='--', linewidth=1, label='Slotted ALOHA', zorder=3, color=RED)

# 1-persistent CSMA theoretical max (approaches 1 for low G with perfect sensing)
# For non-persistent CSMA: S = G * e^(-aG) / (G(1 + 2a) + e^(-aG))
# where a is the normalized propagation delay (a -> 0 for ideal CSMA)
a = 0.01  # Small propagation delay
S_csma = G * np.exp(-a * G) / (G * (1 + 2 * a) + np.exp(-a * G))
ax.plot(G, S_csma, linestyle='-', linewidth=1, label='1-persistent CSMA (ideal)', zorder=3, color='green')

fig.tight_layout()

ax.legend(loc='upper right')

fig.set_figwidth(7, 9)

plt.show()
