"""
coordinates.py — Coordinate, Scale, Naming, and Transform Utilities for UEFN
===========================================================================

Handles scale, axis, handedness conversions, and Unreal asset naming between
Source 1 (Hammer), Blender, and Unreal Engine / UEFN.

Coordinate Systems:
- Source Engine (Hammer):
    Right-handed: X = Forward (East), Y = Left (North), Z = Up.
    Units: 16 Hammer units = 1 foot = 30.48 cm -> 1 HU = 1.905 cm (0.01905 m).
    Angles: (Pitch, Yaw, Roll) in degrees.
      Pitch: rotation around Y (nose down is positive).
      Yaw:   rotation around Z (counter-clockwise / left is positive).
      Roll:  rotation around X (clockwise is positive).

- Blender (after SourceIO import at `blender_scale` m/HU):
    Right-handed: X = Source X * scale, Y = Source Y * scale, Z = Source Z * scale.
    Euler 'XYZ': (Roll_X, Pitch_Y, Yaw_Z) in radians.

- Unreal Engine (UEFN):
    Left-handed: X = Forward, Y = Right, Z = Up.
    Units: 1 Unreal Unit = 1 centimeter (cm).
    Rotator: (Pitch, Yaw, Roll) in degrees.
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Sequence, Tuple, Union

from SourceIO.library.utils.math_utilities import SOURCE1_HAMMER_UNIT_TO_METERS

SOURCE1_HU_TO_CM = SOURCE1_HAMMER_UNIT_TO_METERS * 100.0  # 1.905 cm per Hammer Unit
INCHES_TO_CM = 2.54
CM_TO_INCHES = 1.0 / 2.54

_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9_]+")


def sanitize_ue_name(raw_path: str, prefix: str = "", max_length: int = 60) -> str:
    """
    Build a collision-free, UEFN-safe asset name (only [A-Za-z0-9_]).

    Preserves relative directory components (e.g. 'models/props_2fort/crate01.mdl'
    -> 'SM_props_2fort_crate01') so identically named files in different folders
    never overwrite each other. Truncates long names deterministically with a short
    MD5 hash suffix.
    """
    clean = raw_path.strip().replace("\\", "/").lower().lstrip("/")
    # Strip extension and common root prefixes to keep names readable
    p = Path(clean)
    if p.suffix:
        clean = str(p.with_suffix("").as_posix())
    for root_prefix in ("materials/", "models/", "sound/"):
        if clean.startswith(root_prefix):
            clean = clean[len(root_prefix):]
            break

    sanitized = _NON_ALNUM_RE.sub("_", clean).strip("_")
    if not sanitized:
        sanitized = "asset"

    full_name = f"{prefix}{sanitized}"
    if len(full_name) > max_length:
        digest = hashlib.md5(clean.encode("utf-8")).hexdigest()[:6]
        keep_len = max(8, max_length - len(digest) - 1)
        full_name = f"{full_name[:keep_len]}_{digest}"
    return full_name


def normalize_source_material_path(material_path: str) -> str:
    """Normalize a Source VMT path so all paths start with 'materials/' and end with '.vmt'."""
    clean = material_path.strip().replace("\\", "/").lower().lstrip("/").rstrip("/")
    if not clean.endswith(".vmt"):
        clean += ".vmt"
    if not clean.startswith("materials/"):
        clean = "materials/" + clean
    return clean


def material_ue_name(material_path: str) -> str:
    """Compute the canonical UEFN MaterialInstance name (MI_*) from any Source material path."""
    return sanitize_ue_name(normalize_source_material_path(material_path), prefix="MI_")


def source_to_unreal_position(
    pos: Sequence[float],
    cm_per_hu: float = SOURCE1_HU_TO_CM,
    scale_multiplier: float = 1.0,
) -> list[float]:
    """
    Convert a 3D position vector from Source Engine (Hammer units) to Unreal Engine (cm).

    Source: (X=Forward, Y=Left, Z=Up) in Hammer units.
    Unreal: (X=Forward, Y=Right, Z=Up) in centimeters.
    """
    if len(pos) < 3:
        raise ValueError(f"Position vector must have at least 3 components, got {pos}")
    x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
    factor = cm_per_hu * scale_multiplier
    return [
        round(x * factor, 4),
        round(-y * factor, 4),  # Invert Y for right-handed -> left-handed conversion
        round(z * factor, 4),
    ]


def unreal_to_source_position(
    pos: Sequence[float],
    cm_per_hu: float = SOURCE1_HU_TO_CM,
    scale_multiplier: float = 1.0,
) -> list[float]:
    """
    Convert an Unreal position (cm) back to Source Hammer coordinates.
    """
    if len(pos) < 3:
        raise ValueError(f"Position vector must have at least 3 components, got {pos}")
    x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
    factor = 1.0 / (cm_per_hu * scale_multiplier)
    return [
        round(x * factor, 4),
        round(-y * factor, 4),
        round(z * factor, 4),
    ]


def blender_to_unreal_position(
    pos: Sequence[float],
    blender_scale: float = SOURCE1_HAMMER_UNIT_TO_METERS,
    cm_per_hu: float = SOURCE1_HU_TO_CM,
    scale_multiplier: float = 1.0,
) -> list[float]:
    """
    Convert a Blender scene position (scaled by `blender_scale` m/HU) to Unreal cm.
    """
    if len(pos) < 3:
        raise ValueError(f"Position vector must have at least 3 components, got {pos}")
    hu_pos = [float(pos[0]) / blender_scale, float(pos[1]) / blender_scale, float(pos[2]) / blender_scale]
    return source_to_unreal_position(hu_pos, cm_per_hu=cm_per_hu, scale_multiplier=scale_multiplier)


def source_to_unreal_rotation(angles: Sequence[float]) -> list[float]:
    """
    Convert Source Engine Euler angles (Pitch, Yaw, Roll) in degrees
    to Unreal Rotator (Pitch, Yaw, Roll) in degrees.

    Source applies R_z(yaw) * R_y(pitch) * R_x(roll) in right-handed space.
    Reflecting Y -> -Y negates Yaw (around Z) and Roll (around X), while preserving Pitch (around Y).
    """
    if len(angles) < 3:
        raise ValueError(f"Angle vector must have at least 3 components, got {angles}")
    pitch, yaw, roll = float(angles[0]), float(angles[1]), float(angles[2])
    return [
        round(_normalize_angle(pitch), 4),
        round(_normalize_angle(-yaw), 4),
        round(_normalize_angle(-roll), 4),
    ]


def unreal_to_source_rotation(angles: Sequence[float]) -> list[float]:
    """
    Convert Unreal Rotator (Pitch, Yaw, Roll) back to Source angles.
    """
    if len(angles) < 3:
        raise ValueError(f"Angle vector must have at least 3 components, got {angles}")
    pitch, yaw, roll = float(angles[0]), float(angles[1]), float(angles[2])
    return [
        round(_normalize_angle(pitch), 4),
        round(_normalize_angle(-yaw), 4),
        round(_normalize_angle(-roll), 4),
    ]


def blender_euler_to_unreal_rotation(euler_rad: Sequence[float]) -> list[float]:
    """
    Convert a Blender 'XYZ' Euler (Roll_X, Pitch_Y, Yaw_Z in radians) imported by SourceIO
    into an Unreal Rotator (Pitch, Yaw, Roll in degrees).
    """
    if len(euler_rad) < 3:
        raise ValueError(f"Euler vector must have at least 3 components, got {euler_rad}")
    roll_deg = math.degrees(float(euler_rad[0]))
    pitch_deg = math.degrees(float(euler_rad[1]))
    yaw_deg = math.degrees(float(euler_rad[2]))
    return source_to_unreal_rotation([pitch_deg, yaw_deg, roll_deg])


def source_to_unreal_scale(scale: Union[float, Sequence[float]]) -> list[float]:
    """
    Convert uniform or per-axis scale factors from Source to Unreal.
    """
    if isinstance(scale, (int, float)):
        s = float(scale)
        return [round(s, 5), round(s, 5), round(s, 5)]
    if len(scale) >= 3:
        return [round(float(scale[0]), 5), round(float(scale[1]), 5), round(float(scale[2]), 5)]
    if len(scale) == 1:
        s = float(scale[0])
        return [round(s, 5), round(s, 5), round(s, 5)]
    return [1.0, 1.0, 1.0]


def skybox_to_world_transform(
    pos_hu: Sequence[float],
    sky_origin_hu: Sequence[float],
    sky_scale: float = 16.0,
    cm_per_hu: float = SOURCE1_HU_TO_CM,
    scale_multiplier: float = 1.0,
) -> list[float]:
    """
    Transform a 3D skybox point (relative to `sky_camera`) into full-scale Unreal world coordinates (cm).
    """
    world_hu = [
        (float(pos_hu[0]) - float(sky_origin_hu[0])) * float(sky_scale),
        (float(pos_hu[1]) - float(sky_origin_hu[1])) * float(sky_scale),
        (float(pos_hu[2]) - float(sky_origin_hu[2])) * float(sky_scale),
    ]
    return source_to_unreal_position(world_hu, cm_per_hu=cm_per_hu, scale_multiplier=scale_multiplier)


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
