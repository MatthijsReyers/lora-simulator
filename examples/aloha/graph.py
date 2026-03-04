import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import sys
sys.path.append('.')
from colors import *

RESULTS_CSV = "examples/aloha/results.csv"
df = pd.read_csv(RESULTS_CSV)

fig, ax = plt.subplots(1,1)

ax.grid(zorder=0)
ax.set_xlim(0, 4)
ax.set_xticks(np.arange(0, 4.5, 0.5))
ax.set_xlabel("Offered load (G)")

ax.set_ylim(0, 0.2)
ax.set_ylabel("Throughput (S)")

ax.set_title("(Pure) ALOHA throughput")

ax.scatter(df["G"], df["S"], color=RED, marker='*',  label='Simulated throughput', zorder=2)

G = np.linspace(0, 4, 100)
S_ideal = G * np.exp(-2 * G)
ax.plot(G, S_ideal, linestyle='--', linewidth=1, label='Ideal ALOHA (S = G·e⁻²ᴳ)', zorder=3, color=BLUE_DARK)

fig.tight_layout()

ax.legend()

fig.set_figwidth(7, 9)

plt.show()
