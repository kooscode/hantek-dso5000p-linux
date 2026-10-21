"""USB bulk transport for the DSO5000P "device" port (VID 049f, PID 505a).

The scope runs the Linux g_serial gadget in vendor mode: one interface with a
bulk OUT endpoint 0x02 and a bulk IN endpoint 0x81. On the scope side the data
appears on /dev/ttyGS0 and is parsed by dso.exe.

The firmware reads the tty one byte at a time and treats a short pause as the
end of a request, so every request must go out as a single bulk write.
"""

from __future__ import annotations

import time

import usb.core
import usb.util

VID = 0x049F
PID = 0x505A
EP_OUT = 0x02
EP_IN = 0x81


class TransportError(Exception):
    pass


def _port_path(dev) -> str:
    """Physical port path like '1-5.2.4.3' (bus-port.port...). Unlike the
    device address it stays the same when the scope is replugged."""
    ports = getattr(dev, "port_numbers", None) or ()
    return f"{dev.bus}-" + ".".join(str(p) for p in ports) if ports else f"{dev.bus}-?"


def _describe(dev) -> str:
    try:
        product = dev.product or ""
    except (usb.core.USBError, ValueError, NotImplementedError):
        product = ""  # strings need device access; not essential
    return product


def list_devices(with_product: bool = True) -> list[dict]:
    """All connected DSO5000P scopes: dicts with port, bus, address, product.
    with_product=False skips reading the product string (no device access)."""
    out = []
    for dev in usb.core.find(find_all=True, idVendor=VID, idProduct=PID) or []:
        out.append({"port": _port_path(dev), "bus": dev.bus, "address": dev.address,
                    "product": _describe(dev) if with_product else ""})
    return sorted(out, key=lambda d: d["port"])


def find_device(spec: str | None = None):
    """Select a scope.

    spec: None / "" / "auto" = the first scope found; a port path such as
    "1-5.2.4.3" (as shown by list_devices / `hantek-dso devices`); or
    "bus:address" such as "1:57".
    """
    devs = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID) or [])
    if not devs:
        raise TransportError("DSO5000P (049f:505a) not found on USB")
    if not spec or spec == "auto":
        return devs[0]
    for dev in devs:
        if _port_path(dev) == spec or f"{dev.bus}:{dev.address}" == spec:
            return dev
    found = ", ".join(_port_path(d) for d in devs)
    raise TransportError(f"no DSO5000P at '{spec}' (found: {found})")


class UsbTransport:
    def __init__(self, device: str | None = None, bus: int | None = None,
                 address: int | None = None):
        if bus is not None and address is not None:
            device = f"{bus}:{address}"
        dev = find_device(device)
        self.dev = dev
        self.port = _port_path(dev)
        try:
            if dev.is_kernel_driver_active(0):
                # Host kernel binds cdc_subset (usbnet) to this ID; take it back.
                dev.detach_kernel_driver(0)
        except usb.core.USBError as e:
            raise TransportError(
                f"cannot detach kernel driver: {e}. Install udev/99-hantek-dso5000p.rules"
            ) from e
        except NotImplementedError:
            pass
        # Only configure when needed: SET_CONFIGURATION resets the scope's
        # gadget, which drops whatever request is sent right after.
        try:
            cfg = dev.get_active_configuration()
        except usb.core.USBError:
            cfg = None
        if cfg is None:
            dev.set_configuration()
        try:
            usb.util.claim_interface(dev, 0)
        except usb.core.USBError as e:
            if getattr(e, "errno", None) == 16:  # EBUSY
                raise TransportError("the scope is in use by another program (e.g. hantek-dso-gui); "
                                     "only one connection at a time") from e
            raise TransportError(f"cannot claim interface: {e} (permissions? see udev rule)") from e

    def write(self, data: bytes, timeout_ms: int = 2000) -> None:
        n = self.dev.write(EP_OUT, data, timeout_ms)
        if n != len(data):
            raise TransportError(f"short write {n}/{len(data)}")

    def read(self, size: int = 65536, timeout_ms: int = 500) -> bytes:
        try:
            return bytes(self.dev.read(EP_IN, size, timeout_ms))
        except usb.core.USBTimeoutError:
            return b""
        except usb.core.USBError as e:
            if getattr(e, "errno", None) in (110, None) and "timed out" in str(e).lower():
                return b""
            raise

    def drain(self, quiet_ms: int = 100) -> int:
        """Discard pending input until the line is quiet."""
        total = 0
        while True:
            d = self.read(65536, quiet_ms)
            if not d:
                return total
            total += len(d)

    def close(self) -> None:
        try:
            usb.util.release_interface(self.dev, 0)
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(self.dev)


def pause(ms: float) -> None:
    time.sleep(ms / 1000.0)
