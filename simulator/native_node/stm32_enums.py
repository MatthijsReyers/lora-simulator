from zmq import IntEnum

class HAL_StatusTypeDef(IntEnum):
    HAL_OK = 0
    HAL_ERROR = 1
    HAL_BUSY = 2
    HAL_TIMEOUT = 3

