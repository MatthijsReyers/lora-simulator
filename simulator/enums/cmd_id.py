from enum import IntEnum

class CmdId(IntEnum):
    PING = 1
    TX_CONFIG = 2
    RX_CONFIG = 3
    TX_DATA = 4
    GET_RADIO_STATE = 5

    ACK = 20
    RX_DATA = 21
    TX_FINISHED = 22
    RADIO_STATE = 23

    NACK = 30

    def get_constructor(self):
        # There imports are nested inside the method to prevent a circular import error.
        from lora_modem.commands.ack import AckCmd
        from lora_modem.commands.nack import NackCmd
        from lora_modem.commands.ping import PingCmd
        from lora_modem.commands.tx_config import TxConfigCmd
        from lora_modem.commands.rx_config import RxConfigCmd
        from lora_modem.commands.rx_data import RxDataCmd
        from lora_modem.commands.tx_data import TxDataCmd
        from lora_modem.commands.tx_finished import TxFinishedCmd
        from lora_modem.commands.radio_state import RadioStateCmd
        from lora_modem.commands.get_radio_state import GetRadioStateCmd

        match self:
            case CmdId.PING:
                return PingCmd
            case CmdId.TX_CONFIG:
                return TxConfigCmd
            case CmdId.RX_CONFIG:
                return RxConfigCmd
            case CmdId.ACK: 
                return AckCmd
            case CmdId.RX_DATA:
                return RxDataCmd
            case CmdId.TX_DATA:
                return TxDataCmd
            case CmdId.TX_FINISHED: 
                return TxFinishedCmd
            case CmdId.NACK:
                return NackCmd
            case CmdId.RADIO_STATE:
                return RadioStateCmd
            case CmdId.GET_RADIO_STATE:
                return GetRadioStateCmd
            case _:
                raise Exception(f'Missing constructor for packet ID: {self}')
