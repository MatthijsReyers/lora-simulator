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

# Figure 2 at 0.05 km steps from 0 to 1 km: (distance km, EC normalised) and (distance km, DT
# hours). None where a curve leaves the plotted range.
#
# All six curves were extracted pixel by pixel from a 600 dpi render of the figure (axis frame
# = [0, 1] km x [0, 60] / [0, 150] h, checked against the grid lines; median row of the pixels
# of the curve's MATLAB colour per column), gaps between dashes interpolated, the few points
# where a marker of another curve was picked up replaced by the running maximum (every curve is
# monotone in distance). Reading precision is about 0.2 units of EC and 0.5 h of DT (the line
# width). Exceptions read by eye: the proposed scheme's delivery time below 0.35 km, which is
# hidden under the other curves, and its 1 km value, where the line is clipped by the frame
# (Table I gives 54.5 h).
FIG2_DISTANCES = [0.05 * i for i in range(21)]
FIG2_EC: dict[str, list[float | None]] = {
    "proposed": [5.2, 5.2, 5.2, 5.2, 5.4, 5.4, 6.1, 6.3, 6.8, 8.3, 9.5, 11.3, 13.8, 16.1, 19.1, 21.9, 25.7, 28.9, 33.0, 36.5, 39.5],
    "fsf10": [9.3, 9.3, 9.3, 9.4, 9.4, 9.5, 9.6, 9.7, 9.8, 10.1, 10.5, 10.9, 11.5, 13.4, 15.3, 17.2, 18.1, 22.6, 30.5, 43.9, None],
    "fsf11": [16.2, 16.3, 16.3, 16.3, 16.3, 16.4, 16.5, 16.6, 16.8, 17.0, 17.3, 17.7, 18.1, 18.7, 19.5, 20.5, 21.3, 24.0, 25.5, 29.9, 34.6],
    "fsf12": [27.1, 27.1, 27.1, 27.1, 27.3, 27.3, 27.5, 27.6, 27.8, 28.0, 28.3, 28.6, 29.1, 29.6, 30.1, 30.9, 31.9, 33.2, 34.2, 36.3, 38.2],
    "gb_e": [4.3, 4.3, 4.3, 4.6, 4.6, 4.6, 4.8, 5.3, 6.1, 7.0, 8.2, 9.2, 10.9, 12.2, 13.4, 15.6, 18.5, 23.3, 26.1, 29.4, 34.0],
    "gb_l": [4.3, 4.3, 4.3, 4.3, 4.4, 4.5, 5.4, 5.6, 7.2, 7.6, 10.2, 10.7, 11.6, 12.6, 13.8, 20.4, 21.8, 32.7, 34.0, 36.2, 37.9],
}
FIG2_DT: dict[str, list[float | None]] = {
    "proposed": [0.6, 0.7, 0.8, 1.0, 1.1, 1.5, 2.0, 3.1, 3.9, 6.2, 7.5, 10.1, 13.3, 16.7, 19.7, 24.5, 27.7, 34.1, 39.7, 46.3, 54.5],
    "fsf10": [3.2, 3.2, 3.2, 3.2, 3.4, 3.6, 4.3, 4.3, 4.7, 5.4, 6.4, 7.7, 9.5, 11.7, 15.9, 20.8, 31.2, 42.0, 65.8, 102.9, 144.5],
    "fsf11": [6.7, 6.7, 6.8, 6.8, 7.1, 7.3, 7.5, 8.2, 8.2, 9.1, 10.1, 11.1, 12.6, 14.4, 16.8, 19.9, 23.5, 29.9, 36.9, 47.5, 61.2],
    "fsf12": [12.5, 12.5, 12.5, 12.5, 12.8, 13.0, 13.3, 13.6, 14.1, 14.8, 15.3, 16.5, 17.7, 19.1, 20.8, 22.9, 25.6, 28.6, 31.9, 37.5, 43.1],
    "gb_e": [0.6, 0.6, 0.6, 0.8, 1.0, 1.5, 2.2, 5.5, 9.6, 13.0, 21.2, 24.2, 29.4, 43.5, 47.4, 52.7, 61.2, 76.3, 112.9, 123.1, 137.5],
    "gb_l": [0.6, 0.6, 0.6, 0.8, 1.0, 1.5, 3.6, 4.4, 7.9, 9.2, 15.7, 16.9, 19.9, 22.4, 26.1, 47.4, 52.6, 84.1, 87.9, 93.6, 98.9],
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
