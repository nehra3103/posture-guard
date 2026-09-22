"""Detect whether you're probably on a call: is any app using a microphone right now?

Uses CoreAudio's "device is running somewhere" flag on every input device, so it covers Zoom, Meet in
a browser, FaceTime, Teams, Discord and so on, with no per-app detection and no microphone permission.
"""

import ctypes
import ctypes.util
import struct


def _fourcc(code):
    return struct.unpack(">I", code.encode())[0]


SYSTEM_OBJECT = 1
DEVICES = _fourcc("dev#")
STREAMS = _fourcc("stm#")
RUNNING_SOMEWHERE = _fourcc("gone")
SCOPE_GLOBAL = _fourcc("glob")
SCOPE_INPUT = _fourcc("inpt")
SCOPE_OUTPUT = _fourcc("outp")
ELEMENT_MAIN = 0


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


try:
    _ca = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreAudio"))
    _ca.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32,
                                                   ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    _ca.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32,
                                               ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
except (OSError, TypeError):
    _ca = None


def _size(obj, selector, scope):
    addr = _Address(selector, scope, ELEMENT_MAIN)
    size = ctypes.c_uint32(0)
    if _ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size)) != 0:
        return 0
    return size.value


def _devices():
    n = _size(SYSTEM_OBJECT, DEVICES, SCOPE_GLOBAL) // 4
    if not n:
        return []
    ids = (ctypes.c_uint32 * n)()
    size = ctypes.c_uint32(n * 4)
    addr = _Address(DEVICES, SCOPE_GLOBAL, ELEMENT_MAIN)
    if _ca.AudioObjectGetPropertyData(SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size), ids) != 0:
        return []
    return list(ids)


def _running(device):
    value = ctypes.c_uint32(0)
    size = ctypes.c_uint32(4)
    addr = _Address(RUNNING_SOMEWHERE, SCOPE_GLOBAL, ELEMENT_MAIN)
    if _ca.AudioObjectGetPropertyData(device, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(value)) != 0:
        return False
    return bool(value.value)


def devices_in_use(scope=SCOPE_INPUT):
    """IDs of audio devices with streams in `scope` that some app is currently using."""
    if _ca is None:
        return []
    return [d for d in _devices() if _size(d, STREAMS, scope) > 0 and _running(d)]


def mic_in_use():
    return bool(devices_in_use(SCOPE_INPUT))


if __name__ == "__main__":
    print("Microphone in use:", mic_in_use())
