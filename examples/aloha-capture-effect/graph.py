import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
import sys
sys.path.append('.')
from colors import *

RESULTS_CSV = "examples/aloha-capture-effect/results.csv"
df = pd.read_csv(RESULTS_CSV)

fig, ax = plt.subplots(1, 1)

ax.grid(zorder=0)
ax.set_xlim(0, 4)
ax.set_xticks(np.arange(0, 4.5, 0.5))
ax.set_xlabel("Offered load (G)")

ax.set_ylim(0, 0.5)
ax.set_ylabel("Throughput (S)")

ax.set_title("ALOHA throughput with capture effect")

# Number of nodes (must match simulation)
N = 10

# Chasserat et al. (2022) capture coefficients (SF7, 125 kHz, co-located)
C1, C2, C3 = 0.88, 0.42, 0.23


def chasserat_throughput(G_vals: np.ndarray, n: int) -> np.ndarray:
    """Chasserat et al. (2022) throughput model for Pure ALOHA with capture effect.

    T = sum(P_i * C_i) for i=1..3, where P_i are occurrence probabilities
    and C_i are experimentally measured capture success coefficients.
    """
    S_out = np.zeros_like(G_vals)
    for idx, G in enumerate(G_vals):
        # Per-device transmission probability per ToA
        lam = G / n
        p = 1 - np.exp(-lam)
        if p <= 0 or p >= 1:
            S_out[idx] = 0
            continue
        q = 1 - p

        P1 = n * p * q ** (2 * (n - 1))
        P2 = n * (n - 1) * p**2 * (q ** (2 * (n - 2)) / 2 + q ** (2 * n - 3))
        P3 = n * (n - 1) * p**3 * q ** (2 * (n - 2)) / 2 * (2 * n - 3)

        S_out[idx] = P1 * C1 + P2 * C2 + P3 * C3
    return S_out


# Simulated data points
ax.scatter(df["G"], df["S"], color=RED, marker='*', label='Simulated (capture effect)', zorder=2)

# Ideal ALOHA (without capture effect) for reference
G = np.linspace(0.01, 4, 200)
S_ideal = G * np.exp(-2 * G)
ax.plot(G, S_ideal, linestyle='--', linewidth=1, label='Ideal ALOHA (S = G·e⁻²ᴳ)', zorder=3, color=BLUE_DARK)

# Chasserat et al. (2022) model
S_chasserat = chasserat_throughput(G, N)
ax.plot(G, S_chasserat, linestyle='-.', linewidth=1, label='Chasserat et al. (2022)', zorder=3, color=GREEN_DARK)

ax.legend()

fig.tight_layout()
fig.set_figwidth(7, 9)

plt.show()
