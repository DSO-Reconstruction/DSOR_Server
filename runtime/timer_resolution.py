"""Windows timer resolution for the process's timed waits.

Windows rounds every timed kernel wait up to the system timer period, 15.625
ms by default. ``Event.wait`` and ``Condition.wait`` use such waits, so the
25 Hz monster loop, the deadline scheduler and the reliable repair loop all
fired up to a period late: tick spacing reached p95 50 ms instead of 40 ms,
and every monster sample that came late showed as a pause on the client.
``time.sleep`` uses a high-resolution timer since Python 3.11 and is not
affected. ``timeBeginPeriod`` applies to the calling process only.
"""

from __future__ import annotations

import atexit
import ctypes
import os
import sys


_PROCESS_POWER_THROTTLING = 4
_POWER_THROTTLING_CURRENT_VERSION = 1
_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4

_requested_period_ms = 0


class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_ulong),
        ("ControlMask", ctypes.c_ulong),
        ("StateMask", ctypes.c_ulong),
    ]


def current_timer_resolution_ms() -> float | None:
    """Return the system timer period now in force, in milliseconds."""

    if sys.platform != "win32":
        return None
    try:
        minimum, maximum, current = (ctypes.c_ulong() for _ in range(3))
        status = ctypes.WinDLL("ntdll").NtQueryTimerResolution(
            ctypes.byref(minimum), ctypes.byref(maximum), ctypes.byref(current)
        )
    except (AttributeError, OSError):
        return None
    if status != 0:
        return None
    return current.value / 10_000.0


def _honor_timer_resolution_when_hidden() -> None:
    # Windows 11 may ignore a raised resolution for a process it considers
    # invisible. Opting out keeps the request in force for a console server.
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        state = _PowerThrottlingState(
            _POWER_THROTTLING_CURRENT_VERSION,
            _POWER_THROTTLING_IGNORE_TIMER_RESOLUTION,
            0,
        )
        kernel32.SetProcessInformation(
            ctypes.c_void_p(kernel32.GetCurrentProcess()),
            _PROCESS_POWER_THROTTLING,
            ctypes.byref(state),
            ctypes.sizeof(state),
        )
    except (AttributeError, OSError):
        pass


def raise_timer_resolution() -> float | None:
    """Request a 1 ms timer period for this process (Windows only).

    ``DSO_WINDOWS_TIMER_RESOLUTION_MS`` sets the period; 0 leaves the system
    default. Returns the period in force afterwards, or None when unchanged.
    """

    global _requested_period_ms
    if sys.platform != "win32" or _requested_period_ms:
        return None
    try:
        period = int(os.getenv("DSO_WINDOWS_TIMER_RESOLUTION_MS", "1"))
    except ValueError:
        period = 1
    if period <= 0:
        return None
    before = current_timer_resolution_ms()
    try:
        winmm = ctypes.WinDLL("winmm")
        if winmm.timeBeginPeriod(period) != 0:
            print(f"[Startup] timeBeginPeriod({period}) was refused")
            return None
    except (AttributeError, OSError) as exc:
        print(f"[Startup] could not raise the Windows timer resolution: {exc}")
        return None
    _requested_period_ms = period
    atexit.register(winmm.timeEndPeriod, period)
    _honor_timer_resolution_when_hidden()
    after = current_timer_resolution_ms()

    def shown(value):
        return "unknown" if value is None else f"{value:.3f} ms"

    print(f"[Startup] Windows timer resolution {shown(before)} -> {shown(after)}")
    return after
