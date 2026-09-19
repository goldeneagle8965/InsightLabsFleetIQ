"""Edge deployment package for SIH26123.

The existing simulation stays the reference implementation: `simulation/engine.py`
and `simulation/robot.py` are not edited by anything in here. This package only
*re-hosts* the same robot decision stack so it can run on separate machines.

What lives where
----------------
- ``protocol``  : wire vocabulary + JSON codec + chunking. Reuses the nine peer
                  message types from ``simulation/peer.py`` verbatim.
- ``worldview`` : ``RemoteWorldView`` (the wire form) and ``LocalWorldView``
                  (a live ``Warehouse`` in the same process) - the same seven-member
                  slice a robot may look at in either home.
- ``radio``     : ``SharedMedium`` / ``LoopbackRadio`` (in-process parity) and
                  ``UdpRadio`` (real agent-to-agent unicast).
- ``agent``     : ``EdgeAgent`` - one ``Robot`` + a remote world view + a radio.
- ``host``      : ``EdgeHost`` - physics + observation/action transport only.

Authority boundary
------------------
``EdgeHost`` may move robots (``Warehouse.apply_moves``) and publish observations.
It never plans a path, never picks an auction winner and never resolves a
conflict; those stay inside ``Robot``. ``tests/test_edge_no_central_scheduler.py``
enforces that with an AST guard plus a behavioural test.

Explicit non-goals (documented, not implemented): no SLAM, no motor control, no
ROS. The edge agents are the *decision* stack on edge hardware; the drive layer
is out of scope for this problem statement.
"""

from __future__ import annotations

from simulation.edge.agent import EdgeAgent
from simulation.edge.host import EdgeHost
from simulation.edge.protocol import (
    MAX_DATAGRAM,
    PROTOCOL_VERSION,
    ChunkAssembler,
    MessageTooLarge,
    ProtocolError,
    decode,
    encode,
)
from simulation.edge.radio import LoopbackRadio, SharedMedium, UdpRadio
from simulation.edge.worldview import LocalWorldView, RemoteWorldView

__all__ = [
    "MAX_DATAGRAM",
    "PROTOCOL_VERSION",
    "ChunkAssembler",
    "EdgeAgent",
    "EdgeHost",
    "LocalWorldView",
    "LoopbackRadio",
    "MessageTooLarge",
    "ProtocolError",
    "RemoteWorldView",
    "SharedMedium",
    "UdpRadio",
    "decode",
    "encode",
]
