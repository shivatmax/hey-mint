"""Audio devices and who is using them, straight from CoreAudio (ctypes).

* devices()        - every microphone and speaker, with a stable UID, name,
                     transport (built-in, Bluetooth, USB...) and which way it goes.
* default_device() - the system's current default input or output.
* device_id(uid)   - the live AudioObjectID for a saved UID (IDs change across
                     reboots and replugging; UIDs do not).
* mic_users()      - other apps capturing audio right now (macOS 14+ process
                     objects): the call or meeting Mint should step aside for.
* unduck(device)   - lift the flat -15 dB duck macOS applies to everything else
                     on the output while a voice-processing unit runs.

Reading per-process state works; listeners on it do not fire (Apple forums,
macOS 15-26), so mic_users() is polled.
"""

from __future__ import annotations

import ctypes
import logging
import os
import struct

log = logging.getLogger("mint.voice.devices")

_ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
_cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")


def _fourcc(code: str) -> int:
    return struct.unpack(">I", code.encode("ascii"))[0]


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


_ca.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32,
                                               ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
_ca.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32,
                                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
_ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
_ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
_cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
_cf.CFStringGetCString.restype = ctypes.c_bool
_cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
_cf.CFStringCreateWithCString.restype = ctypes.c_void_p
_cf.CFRelease.argtypes = [ctypes.c_void_p]

SYSTEM = 1
GLOBAL, INPUT, OUTPUT = _fourcc("glob"), _fourcc("inpt"), _fourcc("outp")
_UTF8 = 0x08000100
TRANSPORTS = {"bltn": "built-in", "blue": "Bluetooth", "bltl": "Bluetooth", "usb ": "USB", "hdmi": "HDMI",
              "dprt": "DisplayPort", "airp": "AirPlay", "virt": "virtual", "grup": "aggregate",
              "thun": "Thunderbolt", "pci ": "PCI", "cont": "Continuity"}


def _size(obj: int, selector: str, scope: int = GLOBAL, qualifier: bytes | None = None) -> int:
    address = _Address(_fourcc(selector), scope, 0)
    size = ctypes.c_uint32(0)
    q = ctypes.c_char_p(qualifier) if qualifier else None
    status = _ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(address), len(qualifier or b""), q, ctypes.byref(size))
    return size.value if status == 0 else 0


def _get(obj: int, selector: str, ctype, scope: int = GLOBAL, qualifier=None, count: int = 1):
    address = _Address(_fourcc(selector), scope, 0)
    buffer = (ctype * max(1, count))()
    size = ctypes.c_uint32(ctypes.sizeof(buffer))
    q_ptr, q_size = (ctypes.byref(qualifier), ctypes.sizeof(qualifier)) if qualifier is not None else (None, 0)
    status = _ca.AudioObjectGetPropertyData(obj, ctypes.byref(address), q_size, q_ptr, ctypes.byref(size), buffer)
    if status != 0:
        return None
    n = size.value // ctypes.sizeof(ctype)
    return list(buffer[:n])


def _cfstring(obj: int, selector: str, scope: int = GLOBAL) -> str:
    value = _get(obj, selector, ctypes.c_void_p, scope)
    if not value or not value[0]:
        return ""
    text = ctypes.create_string_buffer(512)
    ok = _cf.CFStringGetCString(value[0], text, 512, _UTF8)
    _cf.CFRelease(value[0])
    return text.value.decode("utf-8", "replace") if ok else ""


def _uint(obj: int, selector: str, scope: int = GLOBAL) -> int | None:
    value = _get(obj, selector, ctypes.c_uint32, scope)
    return value[0] if value else None


def _channels(device: int, scope: int) -> int:
    """Streams in a direction - a cheap 'does it have inputs/outputs' test."""
    return _size(device, "stm#", scope) // 4


def devices() -> list[dict]:
    """[{id, uid, name, transport, inputs, outputs}] for every real device."""
    count = _size(SYSTEM, "dev#") // 4
    ids = _get(SYSTEM, "dev#", ctypes.c_uint32, count=count) or []
    found = []
    for device in ids:
        transport = _uint(device, "tran")
        kind = struct.pack(">I", transport).decode("ascii", "replace") if transport else ""
        found.append({
            "id": device, "uid": _cfstring(device, "uid "), "name": _cfstring(device, "lnam"),
            "transport": TRANSPORTS.get(kind, kind.strip() or "unknown"),
            "inputs": _channels(device, INPUT), "outputs": _channels(device, OUTPUT),
        })
    return found


def default_device(output: bool) -> int | None:
    return _uint(SYSTEM, "dOut" if output else "dIn ")


def device_by_uid(uid: str) -> dict | None:
    return next((d for d in devices() if d["uid"] == uid), None)


def describe(device: int | None) -> dict | None:
    return next((d for d in devices() if d["id"] == device), None) if device else None


def is_private_listening(device: dict | None) -> bool:
    """Headphones, AirPods, a headset: nothing Mint says reaches the mic, so
    echo cancellation (and its ducking of other apps) is not needed."""
    if not device:
        return False
    name = device["name"].lower()
    return device["transport"] == "Bluetooth" or any(
        word in name for word in ("headphone", "airpods", "headset", "earpods", "buds"))


def mic_users(exclude_pids: set[int] | None = None) -> list[dict]:
    """Other processes capturing audio now: [{pid, bundle, name}]."""
    exclude = set(exclude_pids or ()) | {os.getpid()}
    count = _size(SYSTEM, "prs#") // 4
    processes = _get(SYSTEM, "prs#", ctypes.c_uint32, count=count) or []
    users = []
    for process in processes:
        if not _uint(process, "piri"):          # kAudioProcessPropertyIsRunningInput
            continue
        pid = _get(process, "ppid", ctypes.c_int32)
        pid = pid[0] if pid else -1
        if pid in exclude:
            continue
        bundle = _cfstring(process, "pbid")
        users.append({"pid": pid, "bundle": bundle, "name": _app_name(pid, bundle)})
    return users


def _app_name(pid: int, bundle: str) -> str:
    try:
        import AppKit
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None and app.localizedName():
            return str(app.localizedName())
    except Exception:
        pass
    return bundle.split(".")[-1] if bundle else f"process {pid}"


# --- ducking -------------------------------------------------------------------------

_ca.AudioDeviceDuck.argtypes = [ctypes.c_uint32, ctypes.c_float, ctypes.c_void_p, ctypes.c_float]
_ca.AudioDeviceDuck.restype = ctypes.c_int32


def unduck(device: int | None = None) -> bool:
    """Undo, for this process only, the output duck macOS applies to other
    apps while a voice-processing unit runs (not in the public headers; the
    same call MrAutoDuck uses). Harmless if it is not ducked."""
    device = device or default_device(output=True)
    if not device:
        return False
    try:
        return _ca.AudioDeviceDuck(device, ctypes.c_float(1.0), None, ctypes.c_float(0.0)) == 0
    except Exception as error:
        log.debug("unduck failed: %s", error)
        return False
