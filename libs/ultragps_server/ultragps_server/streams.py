from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ultragps_barrier import BarrierEvent
    from .server import UltraGPSServer


def send_position(server: UltraGPSServer, port_name: str, x: float, y: float,
                  source: str = 'lm') -> None:
    """Send a position message on the named port."""
    server.send_message(port_name, f"N: POS {source} {x:.3f} {y:.3f}")


def send_nmea(server: UltraGPSServer, port_name: str, lat: float = 0.0,
              lon: float = 0.0, num_satellites: int = 6,
              altitude: float = 0.0) -> None:
    """Build and send a GPGGA NMEA sentence on the named port."""
    utc = datetime.now(timezone.utc)
    time_str = utc.strftime('%H%M%S.') + f"{utc.microsecond // 10000:02d}"

    lat_str, lat_hemi = _dd_to_lat(lat)
    lon_str, lon_hemi = _dd_to_lon(lon)

    body = (
        f"GPGGA,{time_str},"
        f"{lat_str},{lat_hemi},"
        f"{lon_str},{lon_hemi},"
        f"1,{num_satellites:02d},1.0,{altitude:.1f},M,,M,,"
    )
    checksum = _nmea_checksum(body)
    sentence = f"$GPGGA,{time_str},{lat_str},{lat_hemi},{lon_str},{lon_hemi},1,{num_satellites:02d},1.0,{altitude:.1f},M,,M,,*{checksum}"
    server.send_message(port_name, f"N: NMEA {sentence}")


def send_barrier_event(server: UltraGPSServer, port_name_tcp: str | None,
                       port_name_udp: str | None, event: BarrierEvent) -> None:
    """Send a barrier event message on the named TCP and/or UDP ports."""
    message = (
        f"N: BARRIER {event.barrier_name} {event.event_type.value} "
        f"{event.position[0]:.3f} {event.position[1]:.3f} {event.source}"
    )
    if port_name_tcp is not None:
        server.send_message(port_name_tcp, message)
    if port_name_udp is not None:
        server.send_message(port_name_udp, message)


# ------------------------------------------------------------------
# NMEA helpers
# ------------------------------------------------------------------

def _dd_to_lat(dd: float) -> tuple[str, str]:
    hemi = 'N' if dd >= 0 else 'S'
    dd = abs(dd)
    d = int(dd)
    m = (dd - d) * 60
    return f"{d:02d}{m:08.5f}", hemi


def _dd_to_lon(dd: float) -> tuple[str, str]:
    hemi = 'E' if dd >= 0 else 'W'
    dd = abs(dd)
    d = int(dd)
    m = (dd - d) * 60
    return f"{d:03d}{m:08.5f}", hemi


def _nmea_checksum(body: str) -> str:
    """XOR checksum of all characters between $ and * (i.e., the body)."""
    cs = 0
    for c in body:
        cs ^= ord(c)
    return f"{cs:02X}"
