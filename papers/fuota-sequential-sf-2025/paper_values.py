"""
The paper's published numbers, for the comparison tables and the overlays in the plots.
Tables I-III are copied exactly; the Figure 2 and 3 curves are read off the published plots
(by eye, from a 300 dpi render) and are accurate to roughly half a grid line.
"""

# Table I: battery lifetime (years) and delivery time (hours) at the edge (d0 = R0, uplink
# SF 12) and at a quarter of the radius (d0 = R0/4, uplink SF 7).
TABLE_I = {
    "edge": {"proposed": (1.42, 54.5), "fsf11": (1.47, 61.9), "gb_e": (1.47, 137.7)},
    "quarter": {"proposed": (1.82, 1.44), "fsf11": (1.66, 7.18), "gb_e": (1.82, 1.41)},
}

# Table II: normalised energy (without the control overhead, see README) and delivery time
# (hours), averaged over the node distances.
TABLE_II = {
    "proposed": (11.6, 15.3),
    "fsf10": (13.4, 24.2),
    "fsf11": (16.3, 17.0),
    "fsf12": (26.5, 19.5),
    "gb_e": (8.7, 36.4),
    "gb_l": (10.7, 28.3),
}

# Table III: the same two averages for other interferer densities λ_I (devices per m²).
TABLE_III = {
    0.0005: {"proposed": (13.69, 18.85), "fsf11": (18.02, 21.78), "gb_l": (12.78, 32.17)},
    0.001: {"proposed": (14.50, 20.04), "fsf11": (18.45, 22.57), "gb_l": (12.9, 34.21)},
    0.002: {"proposed": (16.35, 22.83), "fsf11": (19.33, 24.24), "gb_l": (13.51, 36.02)},
}

# Battery lifetime model of equation (21) and Section VII.
BATTERY_CAPACITY_MAH = 1200
UPDATES_PER_MONTH = 1
UPLINK_PAYLOAD = 50          # bytes
UPLINK_INTERVAL_H = 0.5
CURRENT_TX_MA = 83
CURRENT_RX_MA = 38
CURRENT_SLEEP_MA = 0.045

# Figure 2, read at 0.05 km steps from 0 to 1 km: (distance km, EC normalised) and (distance
# km, DT hours). None where a curve leaves the plotted range.
FIG2_DISTANCES = [0.05 * i for i in range(21)]
FIG2_EC: dict[str, list[float | None]] = {
    "proposed": [4.5, 4.5, 4.5, 4.5, 4.6, 4.8, 5.2, 6.0, 7.0, 8.5, 10.0, 12.0, 14.0, 16.5, 19.0, 22.5, 26.5, 30.0, 33.0, 36.0, 39.5],
    "fsf10": [9.3, 9.3, 9.3, 9.3, 9.4, 9.5, 9.6, 9.8, 10.0, 10.3, 10.8, 11.5, 12.5, 14.0, 16.0, 19.0, 24.0, 30.0, 40.0, 55.0, None],
    "fsf11": [16.0, 16.0, 16.0, 16.1, 16.2, 16.3, 16.4, 16.6, 16.8, 17.0, 17.2, 17.5, 18.0, 18.5, 19.3, 20.0, 21.5, 23.0, 26.0, 29.0, 34.0],
    "fsf12": [27.0, 27.0, 27.0, 27.1, 27.2, 27.4, 27.6, 27.8, 28.0, 28.3, 28.6, 29.0, 29.3, 29.7, 30.2, 30.8, 31.5, 32.5, 34.0, 36.0, 39.0],
    "gb_e": [4.5, 4.5, 4.5, 4.5, 4.5, 4.6, 4.8, 5.3, 6.0, 7.0, 8.0, 9.3, 11.0, 12.5, 14.0, 15.5, 18.0, 23.0, 26.0, 29.0, 34.0],
    "gb_l": [4.5, 4.5, 4.5, 4.5, 4.5, 4.6, 5.0, 5.8, 7.0, 8.0, 10.0, 11.0, 12.0, 12.5, 19.0, 19.0, 19.0, 33.0, 33.0, 33.0, 39.5],
}
FIG2_DT = {
    "proposed": [0.6, 0.7, 0.8, 1.0, 1.1, 1.5, 2.0, 2.8, 4.0, 5.5, 7.5, 9.5, 12.0, 14.5, 18.0, 22.0, 28.0, 33.0, 40.0, 47.0, 54.5],
    "fsf10": [3.5, 3.5, 3.5, 3.5, 3.6, 3.7, 3.8, 4.0, 4.5, 5.0, 6.0, 7.5, 9.0, 11.0, 15.0, 21.0, 32.0, 50.0, 75.0, 110.0, 145.0],
    "fsf11": [7.2, 7.2, 7.2, 7.2, 7.3, 7.4, 7.6, 7.9, 8.3, 9.0, 10.0, 11.0, 12.5, 14.5, 17.0, 20.0, 25.0, 30.0, 38.0, 47.0, 61.9],
    "fsf12": [12.5, 12.5, 12.6, 12.7, 12.8, 13.0, 13.2, 13.5, 13.9, 14.5, 15.3, 16.5, 17.5, 19.0, 21.0, 23.5, 26.5, 30.0, 34.0, 39.0, 44.0],
    "gb_e": [0.7, 0.7, 0.8, 0.9, 1.0, 1.2, 1.5, 2.5, 7.0, 10.0, 22.0, 25.0, 30.0, 44.0, 48.0, 53.0, 62.0, 75.0, 113.0, 123.0, 137.7],
    "gb_l": [0.7, 0.7, 0.8, 0.9, 1.0, 1.2, 1.5, 2.5, 4.0, 9.5, 13.0, 14.0, 17.0, 24.0, 48.0, 51.0, 53.0, 85.0, 87.0, 95.0, 99.0],
}

# Figure 3: averages over node distances as a function of w (M = 12), read at a few w values.
FIG3_W = [25, 100, 200, 300, 400, 600, 800, 1000]
FIG3_EC = {
    7: [22.0, 15.5, 13.0, 12.0, 11.5, 10.9, 10.7, 10.9],
    8: [22.5, 15.8, 13.2, 12.1, 11.5, 10.9, 10.7, 10.8],
    9: [23.5, 16.8, 13.8, 12.5, 11.8, 11.1, 10.8, 10.8],
    10: [24.5, 18.0, 15.0, 13.5, 12.8, 12.1, 11.8, 11.6],
    11: [25.5, 22.0, 19.0, 18.0, 17.5, 17.0, 16.8, 16.6],
}
FIG3_DT = {
    7: [17.5, 14.6, 14.7, 15.4, 16.2, 17.8, 19.5, 21.8],
    8: [18.0, 14.6, 14.5, 14.8, 15.3, 16.5, 18.0, 20.3],
    9: [18.1, 15.3, 14.3, 14.4, 14.8, 15.7, 16.8, 18.1],
    10: [18.6, 16.5, 14.6, 14.3, 14.4, 14.9, 15.4, 15.9],
    11: [19.2, 18.3, 17.0, 16.2, 16.0, 16.1, 16.3, 16.5],
}
