"""
End node of the *only broadcast* method (section 3.3 of the paper).

The node behaves exactly like in the broadcast + unicast method: it stores every fragment of
the announced binary it hears and reports its bitmap on request. The difference between the two
methods lies entirely in what the gateway does with that bitmap.
"""
from broadcast_unicast.node import BroadcastNode


class BroadcastOnlyNode(BroadcastNode):
    pass
