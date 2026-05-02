"""
Image overlay persistence and polygon extraction for ultragps_barrier.
"""

from __future__ import annotations

import math
import os
import tomllib

from .core import ImageOverlay, BarrierData


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_images(filepath: str) -> list[ImageOverlay]:
    """Load image overlays from a TOML file. Returns [] if file doesn't exist."""
    if not os.path.exists(filepath):
        return []

    with open(filepath, 'rb') as f:
        data = tomllib.load(f)

    images: list[ImageOverlay] = []
    for entry in data.get('image', []):
        try:
            overlay = ImageOverlay(
                name=entry['name'],
                filename=entry['filename'],
                center_x=float(entry['center_x']),
                center_y=float(entry['center_y']),
                width_cm=float(entry['width_cm']),
                height_cm=float(entry['height_cm']),
                rotation_deg=float(entry.get('rotation_deg', 0.0)),
            )
        except (KeyError, ValueError) as exc:
            print(f"Warning: skipping malformed image entry: {exc}")
            continue
        images.append(overlay)

    return images


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

def save_all(
    filepath: str,
    barriers: list[BarrierData],
    images: list[ImageOverlay],
) -> None:
    """Write barriers and image overlays to a single TOML file."""
    from .persistence import save_barriers as _save_barriers
    # Write barriers first (creates/overwrites the file)
    _save_barriers(filepath, barriers)

    # Append image sections
    with open(filepath, 'a') as f:
        for img in images:
            f.write("[[image]]\n")
            f.write(f'name = "{img.name}"\n')
            f.write(f'filename = "{img.filename}"\n')
            f.write(f'center_x = {float(img.center_x)}\n')
            f.write(f'center_y = {float(img.center_y)}\n')
            f.write(f'width_cm = {float(img.width_cm)}\n')
            f.write(f'height_cm = {float(img.height_cm)}\n')
            f.write(f'rotation_deg = {float(img.rotation_deg)}\n')
            f.write('\n')


# ---------------------------------------------------------------------------
# Polygon extraction
# ---------------------------------------------------------------------------

def polygon_from_image(
    overlay: ImageOverlay,
    resources_dir: str,
    alpha_threshold: int = 128,
    max_vertices: int = 80,
) -> list[tuple[float, float]]:
    """Extract an outline polygon from the alpha channel of an image overlay.

    Loads the image from *resources_dir*, traces the alpha-channel boundary
    using matplotlib contour, then converts the pixel-space path to arena
    coordinates (cm) accounting for the overlay's position, size, and rotation.

    Returns a list of (x, y) tuples in arena cm coordinates, or a fallback
    4-corner bounding box if PIL/alpha extraction fails or the image has no
    alpha channel.
    """
    filepath = os.path.join(resources_dir, overlay.filename)
    if not os.path.exists(filepath):
        return _bounding_box_polygon(overlay)

    try:
        from PIL import Image as _PILImage
        img = _PILImage.open(filepath)
    except Exception:
        return _bounding_box_polygon(overlay)

    # Images without an alpha channel get a bounding-box polygon
    if img.mode not in ('RGBA', 'LA', 'PA'):
        return _bounding_box_polygon(overlay)

    img = img.convert('RGBA')
    w_px, h_px = img.size

    import numpy as np
    # alpha_arr shape is (h, w); row=0 is the top of the image
    alpha_arr = np.array(img)[:, :, 3].astype(float)

    # Lightly blur to smooth staircase edges before contouring
    try:
        from scipy.ndimage import gaussian_filter
        alpha_arr = gaussian_filter(alpha_arr, sigma=1.5)
    except ImportError:
        pass  # scipy optional — skip smoothing

    # Trace the alpha boundary using matplotlib contour on an off-screen figure.
    # We import Figure directly so we never touch the active Qt backend.
    try:
        from matplotlib.figure import Figure
        fig = Figure()
        ax = fig.add_subplot(111)
        cs = ax.contour(alpha_arr, levels=[alpha_threshold - 0.5])

        # allsegs: list[level][segment] — each segment is an Nx2 ndarray of (col, row)
        best_seg: list = []
        for level_segs in cs.allsegs:
            for seg in level_segs:
                if len(seg) > len(best_seg):
                    best_seg = seg

        if len(best_seg) < 3:
            return _bounding_box_polygon(overlay)

        best_path = best_seg.tolist() if hasattr(best_seg, 'tolist') else list(best_seg)

        # Subsample evenly down to max_vertices
        if len(best_path) > max_vertices:
            step = len(best_path) / max_vertices
            best_path = [best_path[int(i * step)] for i in range(max_vertices)]

        return _pixels_to_arena(best_path, overlay, w_px, h_px)

    except Exception:
        return _bounding_box_polygon(overlay)


def _bounding_box_polygon(overlay: ImageOverlay) -> list[tuple[float, float]]:
    """Return the 4 rotated corners of the overlay bounding box."""
    hw = overlay.width_cm / 2.0
    hh = overlay.height_cm / 2.0
    corners_local = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    return _rotate_and_translate(corners_local, overlay)


def _pixels_to_arena(
    pixel_path: list,
    overlay: ImageOverlay,
    w_px: int,
    h_px: int,
) -> list[tuple[float, float]]:
    """Convert pixel-space coordinates to arena cm coordinates.

    The contour is in row/col (col=x, row=y) pixel space where (0,0) is
    top-left.  We map pixels to local overlay coordinates centred at origin,
    then rotate and translate to arena coordinates.
    """
    local: list[tuple[float, float]] = []
    for col, row in pixel_path:
        # Normalise to [-0.5, 0.5]
        nx = col / w_px - 0.5
        # Y is flipped: top of image → +y in local space
        ny = 0.5 - row / h_px
        lx = nx * overlay.width_cm
        ly = ny * overlay.height_cm
        local.append((lx, ly))

    return _rotate_and_translate(local, overlay)


def _rotate_and_translate(
    local: list[tuple[float, float]],
    overlay: ImageOverlay,
) -> list[tuple[float, float]]:
    """Apply overlay rotation (deg, CCW) and translate to arena centre."""
    rad = math.radians(overlay.rotation_deg)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    result = []
    for lx, ly in local:
        rx = lx * cos_r - ly * sin_r + overlay.center_x
        ry = lx * sin_r + ly * cos_r + overlay.center_y
        result.append((rx, ry))
    return result
