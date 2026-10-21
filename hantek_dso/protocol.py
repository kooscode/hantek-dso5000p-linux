"""Packet framing for the Hantek DSO5000P USB protocol.

Recovered from the scope firmware (dso.exe, module ``opt/pc_uart.c``) and the
Windows "Scope" application.

Frame layout (both directions)::

    +--------+--------+--------+-------+------------------+----------+
    | marker | len lo | len hi |  cmd  |  payload ...     | checksum |
    +--------+--------+--------+-------+------------------+----------+
       0        1        2        3       4 .. len+1         len+2

* ``marker`` is ``'S'`` (0x53) for the normal protocol and ``'C'`` (0x43) for
  the factory/debug protocol.
* ``len`` (little endian) counts everything after the length field, i.e.
  ``cmd + payload + checksum``. Total frame size is ``len + 3``.
* ``checksum`` is the 8-bit sum of bytes ``0 .. len+1``. The firmware also
  accepts the literal byte ``0x66`` ('f') as a wildcard checksum.
* Replies use ``cmd | 0x80``.
"""

from __future__ import annotations

from dataclasses import dataclass

MARK_NORMAL = 0x53  # 'S'
MARK_DEBUG = 0x43  # 'C'
WILDCARD_SUM = 0x66


class ProtocolError(Exception):
    pass


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def build(cmd: int, payload: bytes = b"", marker: int = MARK_NORMAL) -> bytes:
    length = len(payload) + 2  # cmd + payload + checksum
    if length > 0xFFFF:
        raise ValueError("payload too long")
    head = bytes([marker, length & 0xFF, length >> 8, cmd & 0xFF]) + bytes(payload)
    return head + bytes([checksum(head)])


@dataclass
class Packet:
    marker: int
    cmd: int
    payload: bytes

    @property
    def is_reply(self) -> bool:
        return bool(self.cmd & 0x80)

    @property
    def request_cmd(self) -> int:
        return self.cmd & 0x7F

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        p = self.payload[:24].hex(" ")
        more = "..." if len(self.payload) > 24 else ""
        return f"Packet({chr(self.marker)} cmd=0x{self.cmd:02x} len={len(self.payload)} [{p}{more}])"


class Deframer:
    """Incremental parser: feed raw bytes, pull complete packets.

    Bytes that do not start a valid frame are skipped one at a time so that the
    stream can resynchronise after noise.
    """

    def __init__(self, verify: bool = True):
        self.buf = bytearray()
        self.verify = verify
        self.dropped = 0

    def feed(self, data: bytes) -> list[Packet]:
        self.buf += data
        out: list[Packet] = []
        while True:
            pkt = self._next()
            if pkt is None:
                break
            out.append(pkt)
        return out

    def _next(self) -> Packet | None:
        b = self.buf
        while b and b[0] not in (MARK_NORMAL, MARK_DEBUG):
            del b[0]
            self.dropped += 1
        if len(b) < 4:
            return None
        length = b[1] | (b[2] << 8)
        if length < 2:
            del b[0]
            self.dropped += 1
            return self._next()
        total = length + 3
        if len(b) < total:
            return None
        frame = bytes(b[:total])
        if self.verify and frame[-1] != checksum(frame[:-1]) and frame[-1] != WILDCARD_SUM:
            del b[0]
            self.dropped += 1
            return self._next()
        del b[:total]
        return Packet(frame[0], frame[3], frame[4:-1])


# --- Normal ('S') command numbers ------------------------------------------------
CMD_ECHO = 0x00            # payload echoed back verbatim
CMD_READ_SETTINGS = 0x01   # -> 0x81 + packed settings (see settings.py)
CMD_READ_WAVE = 0x02       # 00: lock panel, 01 <src>: read waveform
CMD_READ_FILE = 0x10       # 00 <path>: read a file from scope filesystem
CMD_WRITE_SETTINGS = 0x11  # <packed settings>: apply full settings block
CMD_LOCK = 0x12            # 00 <0/1>: remote mode,  01 <0/1>: lock panel
CMD_KEY = 0x13             # <keycode>: press a front-panel key
CMD_SET_X = 0x14           # 7 bytes: u16 + 5x u8 (internal struct at 0x602c4)
CMD_SCREENSHOT = 0x20      # -> 0xA0 chunks of the 800x480 RGB565 framebuffer
CMD_GET_X = 0x21           # read back the 0x14 struct
CMD_MEASURE = 0x22         # <item> <src> <mode>: text of a measurement value

# Reply sub-codes for chunked transfers (0x82, 0x90, 0xA0)
SUB_START = 0x00
SUB_DATA = 0x01
SUB_END = 0x02
SUB_ERROR = 0x03

# Waveform source selectors for CMD_READ_WAVE / 01
SRC_CH1 = 0
SRC_CH2 = 1
SRC_CH1_OR_CH2 = 2
SRC_MATH = 3
SRC_ALL = 4
