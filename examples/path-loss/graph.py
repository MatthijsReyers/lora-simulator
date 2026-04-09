import pandas as pd
import matplotlib.pyplot as plt
import sys

sys.path.append('.')

from simulator.lora.enums.bandwidth import Bandwidth
from simulator.path_loss.log_distance_path_loss import log_distance_path_loss

data = pd.read_csv("path_loss_data.csv")

data.set_index('distance', inplace=True)

func = log_distance_path_loss(exponent=3, sigma=0)

data['path_loss'] = data.index.map(lambda d: func(
    distance=d, 
    frequency=Bandwidth.KHz125.to_hz(), 
))

data['log_loss'] = 8 - data['path_loss']

fig, ax = plt.subplots(1,1)


data['rssi'].plot(ax=ax, style='.', label='Simulated packet RSSI')
data['log_loss'].plot(ax=ax, label='Ideal log-distance Path Loss Model (no shadowing)')

ax.grid()
ax.legend()

ax.set_title("Path Loss Simulation")

plt.show()
