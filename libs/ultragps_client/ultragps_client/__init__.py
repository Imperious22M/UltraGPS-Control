"""
ultragps_client
===============
Standalone TCP/UDP client library for the UltraGPS ground station server
(UltraGpsXY): one-shot and continuous measurements, base-station settings
(trigger rate, sampling window, burst delay, baud rate) and connection state.
"""

from .client import (
    DEFAULT_BURST_DELAY_US,
    DEFAULT_SAMPLING_TIME_MS,
    DEFAULT_TRIGGER_RATE_HZ,
    SAMPLING_TIME_MAX_MS,
    SAMPLING_TIME_MIN_MS,
    TRIGGER_RATE_MAX_HZ,
    TRIGGER_RATE_MIN_HZ,
    CommandType,
    CommMode,
    DelaySearchResult,
    GroundStationBusy,
    GroundStationError,
    GroundStationUnavailable,
    SamplingTime,
    TriggerRate,
    UltraGPSClient,
    max_sampling_time_ms,
    trigger_top,
)

__all__ = [
    "UltraGPSClient",
    "CommandType",
    "CommMode",
    "GroundStationError",
    "GroundStationUnavailable",
    "GroundStationBusy",
    "DelaySearchResult",
    "SamplingTime",
    "TriggerRate",
    "TRIGGER_RATE_MIN_HZ",
    "TRIGGER_RATE_MAX_HZ",
    "DEFAULT_TRIGGER_RATE_HZ",
    "SAMPLING_TIME_MIN_MS",
    "SAMPLING_TIME_MAX_MS",
    "DEFAULT_SAMPLING_TIME_MS",
    "DEFAULT_BURST_DELAY_US",
    "trigger_top",
    "max_sampling_time_ms",
]
