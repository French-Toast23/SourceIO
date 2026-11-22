"""
coordinates.py — Coordinate and Transform Conversion for Source 1 to UEFN
========================================================================

Handles scale, axis, and handedness conversions between Source 1 (Hammer),
Blender, and Unreal Engine / UEFN.

Coordinate Systems:
- Source Engine (Hammer):
    Right-handed: X = East (Forward), Y = North (Left), Z = Up.
    Units: 1 Hammer Unit = 1 inch = 2.54 cm.
    Angles: (Pitch, Yaw, Roll) where Pitch is rotation around Y,
            Yaw is rotation around Z (positive turns left),
            Roll is rotation around X.

- Unreal Engine (UEFN):
    Left-handed: X = Forward, Y = Right, Z = Up.
    Units: 1 Unreal Unit = 1 centimeter (cm).
    Angles: (Pitch, Yaw, Roll) in degrees.

- Blender:
    Right-handed: X = Right, Y = Forward, Z = Up.
"""

from __future__ import annotations

import math
from typing import Sequence, Tuple, Union

INCHES_TO_CM = 2.54
CM_TO_INCHES = 1.0 / 2.54


def source_to_unreal_position(pos: Sequence[float], scale_multiplier: float = 1.0) -> list[float]:
    """
    Convert a 3D position vector from Source Engine (Hammer) to Unreal Engine (cm).

    Source: (X=Forward, Y=Left, Z=Up) in inches.
    Unreal: (X=Forward, Y=Right, Z=Up) in centimeters.
    """
    if len(pos) < 3:
        raise ValueError(f"Position vector must have at least 3 components, got {pos}")
    x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
    factor = INCHES_TO_CM * scale_multiplier
    return [
        round(x * factor, 4),
        round(-y * factor, 4),  # Invert Y to convert from right-handed to left-handed
        round(z * factor, 4),
    ]


def unreal_to_source_position(pos: Sequence[float], scale_multiplier: float = 1.0) -> list[float]:
    """
    Convert an Unreal position back to Source Hammer coordinates (inverse of source_to_unreal_position).
    """
    if len(pos) < 3:
        raise ValueError(f"Position vector must have at least 3 components, got {pos}")
    x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
    factor = CM_TO_INCHES / scale_multiplier
    return [
        round(x * factor, 4),
        round(-y * factor, 4),
        round(z * factor, 4),
    ]


def source_to_unreal_rotation(angles: Sequence[float]) -> list[float]:
    """
    Convert Source Engine Euler angles (Pitch, Yaw, Roll) to Unreal Rotator (Pitch, Yaw, Roll).

    In Source:
      Pitch: rotation around Y (nose down is positive).
      Yaw: rotation around Z (heading, CCW / left is positive).
      Roll: rotation around X (bank, CW is positive).
    In Unreal:
      Pitch: rotation around Y.
      Yaw: rotation around Z (heading, CW / right is positive).
      Roll: rotation around X.
    """
    if len(angles) < 3:
        raise ValueError(f"Angle vector must have at least 3 components, got {angles}")
    pitch, yaw, roll = float(angles[0]), float(angles[1]), float(angles[2])

    unreal_pitch = _normalize_angle(pitch)
    unreal_yaw = _normalize_angle(-yaw)
    unreal_roll = _normalize_angle(-roll)

    return [round(unreal_pitch, 4), round(unreal_yaw, 4), round(unreal_roll, 4)]


def unreal_to_source_rotation(angles: Sequence[float]) -> list[float]:
    """
    Convert Unreal Rotator (Pitch, Yaw, Roll) back to Source angles.
    """
    if len(angles) < 3:
        raise ValueError(f"Angle vector must have at least 3 components, got {angles}")
    pitch, yaw, roll = float(angles[0]), float(angles[1]), float(angles[2])

    src_pitch = _normalize_angle(pitch)
    src_yaw = _normalize_angle(-yaw)
    src_roll = _normalize_angle(-roll)

    return [round(src_pitch, 4), round(src_yaw, 4), round(src_roll, 4)]


def source_to_unreal_scale(scale: Union[float, Sequence[float]]) -> list[float]:
    """
    Convert scale factors from Source to Unreal.
    Scale magnitudes are identical across coordinate conventions.
    """
    if isinstance(scale, (int, float)):
        s = float(scale)
        return [s, s, s]
    if len(scale) >= 3:
        return [float(scale[0]), float(scale[1]), float(scale[2])]
    if len(scale) == 1:
        s = float(scale[0])
        return [s, s, s]
    return [1.0, 1.0, 1.0]


def parse_vector_string(val: str, default: Tuple[float, float, float] = (0.0, 0.0, 0.0)) -> list[float]:
    """
    Parse a whitespace-delimited vector string (e.g., '120 -500 64').
    """
    if not val or not isinstance(val, str):
        return list(default)
    tokens = [t for t in val.strip().split() if t]
    if len(tokens) >= 3:
        try:
            return [float(tokens[0]), float(tokens[1]), float(tokens[2])]
        except ValueError:
            return list(default)
    return list(default)


def _normalize_angle(deg: float) -> float:
    """Normalize degree angle into (-180, 180] range."""
    ang = deg % 360.0
    if ang > 180.0:
        ang -= 360.0
    elif ang <= -180.0:
        ang += 360.0
    return ang
