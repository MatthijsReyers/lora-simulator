
# LoRaWAN Authentication Examples

## ABP — Pre-Shared Key Activation

A single ABP-activated sensor sending periodic uplink temperature readings
to a gateway + network server. Demonstrates MIC verification, payload
encryption, application routing, downlink scheduling, and **MAC commands**
(LinkADRReq to change data rate, DevStatusReq for battery/SNR reporting).

```
$ pipenv run python examples/lorawan/abp.py
```

## OTAA — Over-The-Air Activation (planned)

Key exchange via JoinRequest/JoinAccept.

```
$ pipenv run python examples/lorawan/otaa.py
```

