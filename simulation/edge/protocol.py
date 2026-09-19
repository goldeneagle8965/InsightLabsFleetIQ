"""Wire vocabulary for the edge fleet.

Implements ``docs/edge/03_protocol.md`` and ``docs/edge/04_message_schema.md``.

Two families travel on the same medium:

CTRL  host <-> agent   HELLO, HELLO_WORLD, OBS, ACT, ACT_RESULT, HEARTBEAT,
                       SHUTDOWN, and the CHUNK envelope used to split a large
                       HELLO_WORLD without relying on IP fragmentation.
PEER  agent <-> agent  the nine types already defined in ``simulation/peer.py``,
                       reused byte for byte so ``Robot._ingest`` needs no change.

Nothing in here knows how to plan, bid or yield. It is a codec and a vocabulary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from simulation.peer import (
    MSG_BID,
    MSG_CLAIM,
    MSG_OBSTACLE,
    MSG_PEER_STATE,
    MSG_RELEASE,
    MSG_TASK_ANNOUNCE,
    MSG_YIELD_ACK,
    MSG_YIELD_NACK,
    MSG_YIELD_QUERY,
)

POS = tuple[int, int]

PROTOCOL_VERSION = 1
#: Hard ceiling for one datagram. Above this the message is split into CHUNKs so
#: no send ever depends on IP fragmentation (doc 03 §3 rule 6).
MAX_DATAGRAM = 1200

HOST_ID = "HOST"
WMS_ID = "WMS"

# --- CTRL ---------------------------------------------------------------
CTRL_HELLO = "HELLO"
CTRL_HELLO_WORLD = "HELLO_WORLD"
CTRL_OBS = "OBS"
CTRL_ACT = "ACT"
CTRL_ACT_RESULT = "ACT_RESULT"
CTRL_HEARTBEAT = "HEARTBEAT"
CTRL_SHUTDOWN = "SHUTDOWN"
CTRL_CHUNK = "CHUNK"

# --- PEER (re-exported so callers import from one place) ----------------
PEER_STATE = MSG_PEER_STATE
TASK_ANNOUNCE = MSG_TASK_ANNOUNCE
BID = MSG_BID
CLAIM = MSG_CLAIM
RELEASE = MSG_RELEASE
OBSTACLE = MSG_OBSTACLE
YIELD_QUERY = MSG_YIELD_QUERY
YIELD_ACK = MSG_YIELD_ACK
YIELD_NACK = MSG_YIELD_NACK

CTRL_TYPES = frozenset(
    {
        CTRL_HELLO,
        CTRL_HELLO_WORLD,
        CTRL_OBS,
        CTRL_ACT,
        CTRL_ACT_RESULT,
        CTRL_HEARTBEAT,
        CTRL_SHUTDOWN,
        CTRL_CHUNK,
    }
)
PEER_TYPES = frozenset(
    {
        PEER_STATE,
        TASK_ANNOUNCE,
        BID,
        CLAIM,
        RELEASE,
        OBSTACLE,
        YIELD_QUERY,
        YIELD_ACK,
        YIELD_NACK,
    }
)
KNOWN_TYPES = CTRL_TYPES | PEER_TYPES

#: Types only the world/host side is allowed to originate. Used by the tests and
#: by the agent's source filter; it is an addressing rule, not a scheduler.
HOST_ORIGINATED = frozenset({CTRL_HELLO_WORLD, CTRL_OBS, CTRL_ACT_RESULT, CTRL_SHUTDOWN})


class ProtocolError(ValueError):
    """Malformed, mis-versioned or truncated message."""


class MessageTooLarge(ProtocolError):
    """A message exceeded ``MAX_DATAGRAM`` and was not chunked."""


# --- codec --------------------------------------------------------------


def head(
    msg_type: str,
    from_id: str,
    tick: int,
    to_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a message with the mandatory common header (doc 04, top table)."""
    msg: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": str(msg_type),
        "from_id": str(from_id),
        "tick": int(tick),
    }
    if to_id is not None:
        msg["to_id"] = str(to_id)
    msg.update(extra)
    return msg


def encode(msg: dict[str, Any], *, strict: bool = True) -> bytes:
    """Encode one message to one datagram-sized payload.

    ``strict`` raises :class:`MessageTooLarge` instead of silently producing a
    payload the transport would have to fragment.
    """
    raw = json.dumps(msg, separators=(",", ":"), ensure_ascii=False, sort_keys=False).encode("utf-8")
    if strict and len(raw) > MAX_DATAGRAM:
        raise MessageTooLarge(f"{len(raw)} bytes > MAX_DATAGRAM={MAX_DATAGRAM} (type={msg.get('type')})")
    return raw


def decode(data: bytes | bytearray | str) -> dict[str, Any]:
    """Decode and validate one datagram. Raises :class:`ProtocolError`."""
    if isinstance(data, (bytes, bytearray)):
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"not UTF-8: {exc}") from exc
    elif isinstance(data, str):
        text = data
    else:
        raise ProtocolError(f"cannot decode {type(data).__name__}")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"malformed JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("a message must be a JSON object")
    version = obj.get("v")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version {version!r}")
    if not obj.get("type"):
        raise ProtocolError("missing 'type'")
    return obj


def decode_safe(data: bytes | bytearray | str) -> dict[str, Any] | None:
    """Decode, returning ``None`` instead of raising.

    The socket loops use this so a malformed datagram is counted and dropped
    rather than killing a robot mid-run (doc 03 §7).
    """
    try:
        return decode(data)
    except ProtocolError:
        return None


# --- geometry digest ----------------------------------------------------


def world_digest(
    width: int,
    height: int,
    walls: Any,
    turnouts: Any,
    length: int = 12,
) -> str:
    """Short stable hash of the static geometry.

    The agent compares this against the digest in every ``OBS``; a difference
    means the floor plan changed and a fresh ``HELLO_WORLD`` is required.
    """
    payload = json.dumps(
        [
            int(width),
            int(height),
            sorted([int(x), int(y)] for x, y in walls),
            sorted([int(x), int(y)] for x, y in turnouts),
        ],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[: int(length)]


# --- chunking -----------------------------------------------------------

#: Placeholder used only while measuring how many characters fit per chunk. It
#: is wider than any plausible real part counter, so a real envelope is always
#: smaller than the probe and therefore always fits.
_PROBE_COUNTER = 99999


def chunk_message(msg: dict[str, Any], max_datagram: int = MAX_DATAGRAM) -> list[bytes]:
    """Encode ``msg`` into one or more datagram payloads.

    A message that fits is returned as a single payload, byte-identical to
    :func:`encode`. A larger one is split into ``CHUNK`` envelopes carrying
    ``part``/``parts`` and a SHA-256 of the whole body, so a receiver can detect
    a lost or reordered piece instead of planning on a half map.
    """
    body = encode(msg, strict=False)
    if len(body) <= max_datagram:
        return [body]

    text = body.decode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    base: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": CTRL_CHUNK,
        "from_id": str(msg.get("from_id", HOST_ID)),
        "tick": int(msg.get("tick", 0)),
        "of": str(msg.get("type", "")),
        "sha256": digest,
    }
    probe_base = dict(base, part=_PROBE_COUNTER, parts=_PROBE_COUNTER)

    slices: list[str] = []
    cursor = 0
    while cursor < len(text):
        lo, hi, best = 1, len(text) - cursor, 1
        while lo <= hi:
            mid = (lo + hi) // 2
            probe = dict(probe_base, payload=text[cursor : cursor + mid])
            if len(encode(probe, strict=False)) <= max_datagram:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        slices.append(text[cursor : cursor + best])
        cursor += best

    if len(slices) == 1:  # pragma: no cover - defensive, probe is conservative
        return [body]

    parts = len(slices)
    out: list[bytes] = []
    for index, piece in enumerate(slices, start=1):
        env = dict(base, part=index, parts=parts, payload=piece)
        payload = encode(env, strict=False)
        if len(payload) > max_datagram:  # pragma: no cover - probe guarantees fit
            raise MessageTooLarge(f"chunk {index}/{parts} is {len(payload)} bytes")
        out.append(payload)
    return out


class ChunkAssembler:
    """Reassembles ``CHUNK`` envelopes into the original message dict."""

    def __init__(self) -> None:
        self._pending: dict[tuple[str, int, str], dict[str, Any]] = {}
        self.lost = 0

    @property
    def pending(self) -> int:
        return len(self._pending)

    def reset(self) -> None:
        self._pending.clear()
        self.lost = 0

    def feed(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Return a complete message, or ``None`` while pieces are missing.

        A non-chunk message is returned unchanged, so callers can pipe every
        inbound datagram through here and never special-case the path.
        """
        if msg.get("type") != CTRL_CHUNK:
            return msg
        key = (str(msg.get("from_id")), int(msg.get("tick", 0)), str(msg.get("of", "")))
        state = self._pending.get(key)
        parts = int(msg.get("parts", 0))
        if parts <= 0:
            raise ProtocolError("CHUNK without a positive 'parts'")
        if state is None:
            state = {
                "parts": parts,
                "sha256": msg.get("sha256"),
                "of": str(msg.get("of", "")),
                "pieces": {},
            }
            self._pending[key] = state
        elif state["parts"] != parts:
            del self._pending[key]
            raise ProtocolError("CHUNK reassembly disagrees about 'parts'")
        state["pieces"][int(msg.get("part", 0))] = str(msg.get("payload", ""))
        if len(state["pieces"]) < parts:
            return None

        del self._pending[key]
        missing = [i for i in range(1, parts + 1) if i not in state["pieces"]]
        if missing:
            self.lost += 1
            return None
        raw = "".join(state["pieces"][i] for i in range(1, parts + 1)).encode("utf-8")
        expected = state.get("sha256")
        if expected and hashlib.sha256(raw).hexdigest() != expected:
            self.lost += 1
            raise ProtocolError("CHUNK reassembly checksum mismatch")
        return decode(raw)


# --- optional message authentication ------------------------------------

#: Designed and tested, deliberately OFF by default: the demo runs on a closed
#: LAN where the cost is a shared secret on every node (doc 03 §7, doc 04 §4).
def sign(msg: dict[str, Any], secret: bytes | str) -> dict[str, Any]:
    """Return a copy of ``msg`` with an HMAC-SHA256 ``sig`` field."""
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    body = {k: v for k, v in msg.items() if k != "sig"}
    blob = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signed = dict(msg)
    signed["sig"] = hmac.new(secret, blob, hashlib.sha256).hexdigest()
    return signed


def verify(msg: dict[str, Any], secret: bytes | str) -> bool:
    """Constant-time check of the ``sig`` field produced by :func:`sign`."""
    supplied = msg.get("sig")
    if not isinstance(supplied, str):
        return False
    expected = sign(msg, secret).get("sig")
    return bool(expected) and hmac.compare_digest(str(expected), supplied)
