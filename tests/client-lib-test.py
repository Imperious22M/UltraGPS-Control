"""
client-lib-test.py
==================
Offline integration test for the ultragps_client library.

Verifies the public API, enum values, command table, default state,
and graceful error handling — all without requiring a running server.

Usage
-----
    python tests/client-lib-test.py

All tests pass with exit code 0. Any failure prints the failing assertion
and exits with code 1.
"""

import sys
import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from ultragps_client import UltraGPSClient, CommandType, CommMode
from ultragps_client.client import _COMMANDS

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        print(f"  PASS  {label}")
        _passed += 1
    else:
        msg = f"  FAIL  {label}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        _failed += 1


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


# ---------------------------------------------------------------------------
# 1. Imports
# ---------------------------------------------------------------------------
section("1. Imports")
check("UltraGPSClient importable", UltraGPSClient is not None)
check("CommandType importable", CommandType is not None)
check("CommMode importable", CommMode is not None)

# ---------------------------------------------------------------------------
# 2. CommandType enum values
# ---------------------------------------------------------------------------
section("2. CommandType enum")
check("PULSE value", CommandType.PULSE.value == "pulse")
check("SIMULATE value", CommandType.SIMULATE.value == "simulate")
check("CONTINUOUS value", CommandType.CONTINUOUS.value == "continuous")
check("Exactly 3 members", len(CommandType) == 3)

# ---------------------------------------------------------------------------
# 3. CommMode enum values
# ---------------------------------------------------------------------------
section("3. CommMode enum")
check("NORMAL value", CommMode.NORMAL.value == "normal")
check("CONTINUOUS value", CommMode.CONTINUOUS.value == "continuous")
check("Exactly 2 members", len(CommMode) == 2)

# ---------------------------------------------------------------------------
# 4. Private command table (_COMMANDS)
# ---------------------------------------------------------------------------
section("4. Command table (_COMMANDS)")
check("PULSE wire format", _COMMANDS[CommandType.PULSE] == "P\n")
check("SIMULATE wire format", _COMMANDS[CommandType.SIMULATE] == "S\n")
check("CONTINUOUS wire format", _COMMANDS[CommandType.CONTINUOUS] == "C\n")
check("Exactly 3 entries", len(_COMMANDS) == 3)

# ---------------------------------------------------------------------------
# 5. Default state (no connection)
# ---------------------------------------------------------------------------
section("5. Default state")
client = UltraGPSClient()
check("mode is NORMAL", client.mode == CommMode.NORMAL)
check("get_latest_reading() is None in NORMAL mode", client.get_latest_reading() is None)
check("not running", not client._running)

# ---------------------------------------------------------------------------
# 6. Idempotent disconnect (before connect)
# ---------------------------------------------------------------------------
section("6. Idempotent disconnect")
raised = False
try:
    client.disconnect()
    client.disconnect()
except Exception as exc:
    raised = True
    check("disconnect() before connect() is safe", False, str(exc))
if not raised:
    check("disconnect() before connect() is safe", True)
    check("double disconnect() is safe", True)

# ---------------------------------------------------------------------------
# 7. Constructor parameters
# ---------------------------------------------------------------------------
section("7. Constructor parameters")
c = UltraGPSClient(host="192.168.1.50", tcp_port=9100, udp_port=9101, timeout=5.0)
check("custom host stored", c._host == "192.168.1.50")
check("custom tcp_port stored", c._tcp_port == 9100)
check("custom udp_port stored", c._udp_port == 9101)
check("custom timeout stored", c._timeout == 5.0)
c.disconnect()

# ---------------------------------------------------------------------------
# 8. Connect failure (no server running)
# ---------------------------------------------------------------------------
section("8. Connect failure (no server)")
c2 = UltraGPSClient(host="127.0.0.1", tcp_port=19999)
caught_connection_error = False
try:
    c2.connect()
except ConnectionError:
    caught_connection_error = True
except Exception as exc:
    check("connect() raises ConnectionError when server absent", False, f"Got {type(exc).__name__}: {exc}")
check("connect() raises ConnectionError when server absent", caught_connection_error)

# ---------------------------------------------------------------------------
# 9. Private members not in top-level package namespace
# ---------------------------------------------------------------------------
section("9. Public API encapsulation")
import ultragps_client as pkg
check("_COMMANDS not in top-level namespace", not hasattr(pkg, "_COMMANDS"))
check("_RESPONSE_PREFIX_NORMAL not in top-level namespace", not hasattr(pkg, "_RESPONSE_PREFIX_NORMAL"))
check("UltraGPSClient in top-level namespace", hasattr(pkg, "UltraGPSClient"))
check("CommandType in top-level namespace", hasattr(pkg, "CommandType"))
check("CommMode in top-level namespace", hasattr(pkg, "CommMode"))

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'='*40}")
print(f"Results: {_passed} passed, {_failed} failed")
print(f"{'='*40}")

if _failed > 0:
    sys.exit(1)
