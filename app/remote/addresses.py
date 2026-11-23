"""Which local addresses the listener binds and which peers it accepts.

PROTOCOL.md section 3: one socket per local address that is private or
link-local (``10/8``, ``172.16/12``, ``192.168/16``, ``169.254/16``,
``fc00::/7``, ``fe80::/10``), never a wildcard or a public address, and a
peer outside those ranges is dropped before the TLS handshake. Loopback
(``127.0.0.1`` only) is bound and accepted only in loopback test mode, where
the QR also lists ``10.0.2.2`` (the Android emulator's name for this PC).

The classification is plain ``ipaddress`` and Qt-free; only
:func:`bind_addresses` asks Qt for the interface list.
"""

from __future__ import annotations

import ipaddress
import sys
from collections.abc import Iterable, Sequence

LOOPBACK = "127.0.0.1"
EMULATOR_HOST = "10.0.2.2"
#: Fewer than the protocol's 8: the phone tries them in order, and a short
#: list keeps the QR code small enough to scan off a screen.
MAX_ADVERTISED = 4

_ALLOWED_V4 = tuple(
    ipaddress.IPv4Network(net)
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
)
_UNIQUE_LOCAL = ipaddress.IPv6Network("fc00::/7")
_LINK_LOCAL_V6 = ipaddress.IPv6Network("fe80::/10")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def parse(address: object) -> tuple[IPAddress, str] | None:
    """``(ip, zone)`` for an address string, or None if it is not one.

    Accepts a zone id (``fe80::1%eth0``) and brackets. An IPv4-mapped IPv6
    address is returned as its IPv4 address, so it is judged by the IPv4 rules.
    """
    if not isinstance(address, str):
        return None
    text = address.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    text, _, zone = text.partition("%")
    try:
        ip: IPAddress = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped, ""
    return ip, zone


def _is_private_or_link_local(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _ALLOWED_V4)
    return ip in _UNIQUE_LOCAL or ip in _LINK_LOCAL_V6


def is_client_target(address: object) -> bool:
    """True for an address a link may connect to when the host itself supplied
    it (``authOk.addresses``): an IP literal (at most 64 characters, no zone id) that is
    private or link-local. Host names, public addresses and loopback are refused, so a host cannot
    steer this PC to somewhere else on the network or the internet."""
    if not isinstance(address, str) or len(address) > 64 or "%" in address:
        return False  # no zone id, nothing long: it came from another machine
    parsed = parse(address)
    if parsed is None:
        return False
    ip = parsed[0]
    return not ip.is_loopback and _is_private_or_link_local(ip)


def is_bindable(address: object, test_mode: bool = False) -> bool:
    """True if the listener may open a socket on this local address."""
    parsed = parse(address)
    if parsed is None:
        return False
    ip, zone = parsed
    if test_mode and str(ip) == LOOPBACK:
        return True
    if not _is_private_or_link_local(ip):
        return False
    # A link-local IPv6 address cannot be bound without its interface.
    return not (isinstance(ip, ipaddress.IPv6Address) and ip in _LINK_LOCAL_V6 and not zone)


def is_allowed_peer(address: object, test_mode: bool = False) -> bool:
    """True if a connection from this remote address may be served."""
    parsed = parse(address)
    if parsed is None:
        return False
    ip = parsed[0]
    if str(ip) == LOOPBACK:
        return test_mode
    return _is_private_or_link_local(ip)


def _rank(address: str) -> int:
    parsed = parse(address)
    if parsed is None:
        return 3
    ip = parsed[0]
    if isinstance(ip, ipaddress.IPv4Address):
        return 0
    return 2 if ip in _LINK_LOCAL_V6 else 1


def select_bind_addresses(candidates: Iterable[str], test_mode: bool = False) -> list[str]:
    """The addresses to listen on, out of ``candidates``: IPv4 first,
    link-local IPv6 last, no duplicates, loopback only in test mode."""
    chosen: list[str] = []
    for candidate in candidates:
        if is_bindable(candidate, test_mode) and candidate not in chosen:
            chosen.append(candidate)
    chosen.sort(key=_rank)  # stable
    if test_mode:
        # First, so that a test's port 0 is picked on the loopback socket.
        chosen = [LOOPBACK, *(a for a in chosen if a != LOOPBACK)]
    return chosen


#: Interface names of adapters a phone on the Wi-Fi can never reach: virtual
#: machines, WSL, containers and overlay VPNs.
_VIRTUAL_NAME_HINTS = (
    "vethernet",
    "virtualbox",
    "vbox",
    "vmware",
    "vmnet",
    "hyper-v",
    "default switch",
    "wsl",
    "docker",
    "tailscale",
    "zerotier",
    "hamachi",
    "wireguard",
    "bluetooth",
    "npcap",
)
#: MAC prefixes of virtual adapters (VirtualBox, Hyper-V, VMware, Parallels, Docker).
_VIRTUAL_MAC_PREFIXES = (
    "0A:00:27",
    "08:00:27",
    "00:15:5D",
    "00:50:56",
    "00:0C:29",
    "00:05:69",
    "00:1C:42",
    "02:42:",
)


def is_virtual_interface(name: str, human_name: str, mac: str) -> bool:
    """True for an adapter that belongs to a virtual machine, WSL, a container or a VPN."""
    text = f"{name} {human_name}".lower()
    if any(hint in text for hint in _VIRTUAL_NAME_HINTS):
        return True
    return mac.upper().startswith(_VIRTUAL_MAC_PREFIXES)


if sys.platform == "win32":

    def routed_interface_index() -> int | None:
        """Index of the adapter Windows sends internet traffic through (the
        default route): the network the router, and so the phone, is on.
        Asked of the routing table (``GetBestInterface``); nothing is sent and
        no socket is opened. None without a default route."""
        import ctypes
        from ctypes import wintypes

        index = wintypes.DWORD(0)
        destination = int.from_bytes(ipaddress.IPv4Address("192.0.2.1").packed, "little")
        try:
            result = ctypes.windll.iphlpapi.GetBestInterface(
                wintypes.DWORD(destination), ctypes.byref(index)
            )
        except (OSError, AttributeError):
            return None
        return int(index.value) if result == 0 and index.value else None

else:

    def routed_interface_index() -> int | None:
        """Only Windows is asked; elsewhere adapters keep the order Qt gives."""
        return None


def order_candidates(entries: Iterable[tuple[str, bool]], routed: str | None) -> list[str]:
    """``(address, virtual)`` pairs in the order a phone should try them: the
    routed address first, then real adapters, virtual adapters last."""
    listed = list(entries)

    def key(item: tuple[int, tuple[str, bool]]) -> tuple[int, int]:
        index, (address, virtual) = item
        return (0 if routed is not None and address == routed else 1 + int(virtual), index)

    return [address for _, (address, _) in sorted(enumerate(listed), key=key)]


def bind_addresses(test_mode: bool = False) -> list[str]:
    """This PC's private and link-local addresses, read from Qt just now, the
    reachable ones first (:func:`order_candidates`).

    Link-local IPv6 entries carry their scope id (``fe80::1%ethernet_3``).
    Interfaces that are down are skipped.
    """
    from PySide6.QtNetwork import QNetworkInterface

    flags = QNetworkInterface.InterfaceFlag
    routed_index = routed_interface_index()
    routed: str | None = None
    entries: list[tuple[str, bool]] = []
    for interface in QNetworkInterface.allInterfaces():
        state = interface.flags()
        if not (state & flags.IsUp) or not (state & flags.IsRunning):
            continue
        virtual = is_virtual_interface(
            interface.name(), interface.humanReadableName(), interface.hardwareAddress()
        )
        found = [entry.ip().toString() for entry in interface.addressEntries()]
        if routed is None and routed_index is not None and interface.index() == routed_index:
            routed = next((a for a in found if "." in a and ":" not in a), None)
        entries.extend((address, virtual) for address in found)
    return select_bind_addresses(order_candidates(entries, routed), test_mode)


def advertised(bound: Sequence[str], test_mode: bool = False) -> list[str]:
    """What the QR and ``authOk.addresses`` list for these bound addresses:
    no zone id, IPv6 in brackets, IPv4 first, link-local IPv6 last, at most
    :data:`MAX_ADVERTISED`.

    Loopback is never listed (a phone cannot use it); in test mode the
    emulator's ``10.0.2.2`` is listed first instead.
    """
    listed: list[str] = [EMULATOR_HOST] if test_mode else []
    for address in sorted(bound, key=_rank):
        parsed = parse(address)
        if parsed is None or str(parsed[0]) == LOOPBACK:
            continue
        ip = parsed[0]
        text = str(ip) if isinstance(ip, ipaddress.IPv4Address) else f"[{ip}]"
        if text not in listed:
            listed.append(text)
    return listed[:MAX_ADVERTISED]
