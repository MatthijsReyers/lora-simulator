from __future__ import annotations
from abc import ABC, abstractmethod


class Application(ABC):
    """
        Base class for LoRaWAN application handlers.

        Subclass this and register it with a LoRaWanDevice or NetworkServer to handle
        payloads on a specific FPort.
    """

    @abstractmethod
    def port(self) -> int:
        """The FPort this application handles (1-223)."""
        pass

    @abstractmethod
    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        """
            Called on the network server side when an uplink payload arrives on this port.

            Args:
                dev_addr: The device address that sent the uplink.
                payload: The decrypted application payload.
        """
        pass

    async def on_downlink(self, payload: bytes) -> None:
        """
            Called on the device side when a downlink payload arrives on this port.

            Args:
                payload: The decrypted downlink application payload.
        """
        pass

    async def get_downlink(self, dev_addr: int) -> bytes | None:
        """
            Called by the network server to check if this application has a pending downlink for
            the given device.

            Returns None if no downlink is pending.
        """
        return None
