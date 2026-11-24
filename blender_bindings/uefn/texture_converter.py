"""
texture_converter.py — VTF to UEFN Texture Pipeline
===================================================

Converts Source 1 VTF textures to UEFN-compatible PNG/TGA with zero third-party
dependencies beyond NumPy and SourceIO's bundled pylib.

Handles:
- VTF decoding via SourceIO pylib.
- Normal map convention:
  * Standard Source 1 VTF normal maps are natively DirectX (Y- Down), matching Unreal Engine.
    Their Green channel is preserved directly (no inversion).
  * Source 1 $ssbump textures are transformed via Valve's bump basis matrix to tangent-space
    normals and flipped to DirectX (Y- Down) for Unreal Engine.
- sRGB vs Linear color space tagging.
- Power-of-two enforcement and resolution clamping (downscaling with warnings).
- Collision-free Unreal asset naming via relative path sanitization.
"""

from __future__ import annotations

from pathlib import Path
import struct
from typing import Any, Optional, Tuple

import numpy as np

try:
    from PIL import Image as _PILImage
    HAS_PIL = True
except ImportError:
    _PILImage = None
    HAS_PIL = False

from SourceIO.library.shared.content_manager import ContentManager
from SourceIO.library.source1.vtf import load_texture as vtf_load_texture
from SourceIO.library.utils.pylib.image import save_png as pylib_save_png
from SourceIO.library.utils.tiny_path import TinyPath
from SourceIO.logger import SourceLogMan
from .coordinates import sanitize_ue_name

log_manager = SourceLogMan()
logger = log_manager.get_logger("UEFN::TextureConverter")

DEFAULT_MAX_TEXTURE_SIZE = 2048

# Valve SSBump basis transpose matrix (Source SDK / ssbumpToNormal)
_SSBUMP_BASIS_T = np.array([
    [0.81649661064147949, -0.40824833512306213, -0.40824833512306213],
    [0.0,                  0.70710676908493042, -0.7071068286895752],
    [0.57735025882720947,  0.57735025882720947,  0.57735025882720947],
], dtype=np.float32)


def is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def next_lower_power_of_two(n: int) -> int:
    if n <= 1:
        return 1
    return 1 << (n.bit_length() - 1)


def convert_ssbump_pixels(rgba_float: np.ndarray) -> np.ndarray:
    """
    Convert a Source 1 $ssbump float RGBA array [0..1] into a DirectX (Y- Down) tangent-space normal map.
    """
    out = rgba_float.copy()
    dots = np.dot(out[:, :, :3], _SSBUMP_BASIS_T.T)
    dots = np.clip(dots * 0.5 + 0.5, 0.0, 1.0)
    # Bump basis yields OpenGL Y+ normal; invert Green for DirectX (Unreal Y-)
    dots[:, :, 1] = 1.0 - dots[:, :, 1]
    out[:, :, :3] = dots
    return out


def _resize_rgba_uint8(pixels: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
    """
    Resize an (H, W, 4) uint8 RGBA array using Pillow Lanczos if available,
    falling back to pure-NumPy bilinear resampling inside Blender.
    """
    if HAS_PIL:
        img = _PILImage.fromarray(pixels, mode="RGBA")
        img = img.resize((target_w, target_h), _PILImage.Resampling.LANCZOS)
        return np.asarray(img, dtype=np.uint8)

    src_h, src_w, _ = pixels.shape
    y_coords = np.linspace(0, src_h - 1, target_h)
    x_coords = np.linspace(0, src_w - 1, target_w)
    y0 = np.floor(y_coords).astype(np.int32)
    x0 = np.floor(x_coords).astype(np.int32)
    y1 = np.minimum(y0 + 1, src_h - 1)
    x1 = np.minimum(x0 + 1, src_w - 1)
    wy = (y_coords - y0)[:, None, None]
    wx = (x_coords - x0)[None, :, None]

    p00 = pixels[y0[:, None], x0[None, :]].astype(np.float32)
    p01 = pixels[y0[:, None], x1[None, :]].astype(np.float32)
    p10 = pixels[y1[:, None], x0[None, :]].astype(np.float32)
    p11 = pixels[y1[:, None], x1[None, :]].astype(np.float32)

    interp = (
        p00 * (1.0 - wx) * (1.0 - wy)
        + p01 * wx * (1.0 - wy)
        + p10 * (1.0 - wx) * wy
        + p11 * wx * wy
    )
    return np.clip(interp, 0, 255).astype(np.uint8)


def _save_tga_rgba(pixels: np.ndarray, out_file: Path) -> None:
    """Write uncompressed 32-bit top-down BGRA TGA file without external dependencies."""
    h, w, _ = pixels.shape
    # TGA header: 18 bytes, image type 2 (uncompressed true-color), 32-bit, top-down bit (0x20)
    header = struct.pack("<BBBHHBHHHHBB", 0, 0, 2, 0, 0, 0, 0, 0, w, h, 32, 0x28)
    bgra = pixels[:, :, [2, 1, 0, 3]]
    with open(out_file, "wb") as f:
        f.write(header)
        f.write(np.ascontiguousarray(bgra).tobytes())


class TextureConversionResult:
    def __init__(
        self,
        source_path: str,
        output_path: Path,
        texture_type: str,
        original_size: Tuple[int, int],
        final_size: Tuple[int, int],
        was_downscaled: bool = False,
        converted_from_ssbump: bool = False,
        is_srgb: bool = True,
        has_alpha: bool = False,
    ):
        self.source_path = source_path
        self.output_path = output_path
        self.texture_type = texture_type
        self.original_size = original_size
        self.final_size = final_size
        self.was_downscaled = was_downscaled
        self.converted_from_ssbump = converted_from_ssbump
        self.is_srgb = is_srgb
        self.has_alpha = has_alpha

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_name": self.output_path.stem,
            "source_path": self.source_path,
            "output_path": str(self.output_path.as_posix()),
            "texture_type": self.texture_type,
            "original_width": self.original_size[0],
            "original_height": self.original_size[1],
            "final_width": self.final_size[0],
            "final_height": self.final_size[1],
            "was_downscaled": self.was_downscaled,
            "converted_from_ssbump": self.converted_from_ssbump,
            "normal_convention": "DirectX_YMinus" if self.texture_type == "normal" else "N/A",
            "is_srgb": self.is_srgb,
            "has_alpha": self.has_alpha,
        }


class UEFNTextureConverter:
    """
    Manages VTF texture resolution and conversion for UEFN export.
    """

    def __init__(
        self,
        content_manager: ContentManager,
        output_dir: Path,
        max_size: int = DEFAULT_MAX_TEXTURE_SIZE,
        export_format: str = "PNG",
    ):
        self.content_manager = content_manager
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_size = max_size
        self.export_format = export_format.upper()
        self.converted_cache: dict[str, TextureConversionResult] = {}
        self.missing_textures: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def convert_texture(
        self,
        vtf_rel_path: str,
        texture_type: str = "diffuse",
        is_ssbump: bool = False,
    ) -> Optional[TextureConversionResult]:
        """
        Convert a VTF texture to a UEFN-ready PNG/TGA file.
        """
        clean_path = vtf_rel_path.strip().replace("\\", "/").lower().lstrip("/")
        if clean_path.endswith(".vmt"):
            clean_path = clean_path[:-4]
        if not clean_path.endswith(".vtf"):
            clean_path += ".vtf"
        if not clean_path.startswith("materials/"):
            clean_path = "materials/" + clean_path

        cache_key = f"{clean_path}:{texture_type}:ssbump={int(is_ssbump)}"
        if cache_key in self.converted_cache:
            return self.converted_cache[cache_key]

        file_buf = self.content_manager.find_file(TinyPath(clean_path))
        if not file_buf:
            msg = f"Texture file not found in search paths: {clean_path}"
            logger.warn(msg)
            if clean_path not in self.missing_textures:
                self.missing_textures.append(clean_path)
            self.warnings.append(msg)
            return None

        try:
            rgba_data, height, width = vtf_load_texture(file_buf)
        except Exception as e:
            err = f"Failed decoding VTF {clean_path}: {e}"
            logger.error(err)
            self.errors.append(err)
            return None

        if rgba_data is None or width == 0 or height == 0:
            err = f"VTF decoding yielded empty image for {clean_path}"
            logger.error(err)
            self.errors.append(err)
            return None

        rgba_arr = rgba_data.reshape(height, width, 4)

        is_srgb = True
        converted_ssbump = False
        if texture_type == "normal":
            is_srgb = False
            if is_ssbump:
                if rgba_arr.dtype == np.uint8:
                    rgba_arr = rgba_arr.astype(np.float32) / 255.0
                rgba_arr = convert_ssbump_pixels(rgba_arr)
                converted_ssbump = True
        elif texture_type in ("mask", "blend", "roughness", "metallic"):
            is_srgb = False

        if rgba_arr.dtype != np.uint8:
            pixels = np.clip(rgba_arr * 255.0, 0, 255).astype(np.uint8)
        else:
            pixels = rgba_arr

        original_size = (width, height)

        # Enforce size budget and Power-of-Two dimensions
        was_downscaled = False
        cur_w, cur_h = width, height
        target_w, target_h = cur_w, cur_h

        if target_w > self.max_size or target_h > self.max_size:
            scale_factor = min(self.max_size / target_w, self.max_size / target_h)
            target_w = max(1, int(target_w * scale_factor))
            target_h = max(1, int(target_h * scale_factor))
            was_downscaled = True

        if not is_power_of_two(target_w):
            target_w = next_lower_power_of_two(target_w)
            was_downscaled = True
        if not is_power_of_two(target_h):
            target_h = next_lower_power_of_two(target_h)
            was_downscaled = True

        if was_downscaled:
            warning_msg = (
                f"Texture '{clean_path}' ({cur_w}x{cur_h}) was downscaled to "
                f"({target_w}x{target_h}) to meet UEFN power-of-two / max-budget ({self.max_size})."
            )
            logger.info(warning_msg)
            self.warnings.append(warning_msg)
            pixels = _resize_rgba_uint8(pixels, target_w, target_h)

        has_alpha = bool(np.any(pixels[:, :, 3] < 255))

        type_suffix = {
            "diffuse": "D",
            "normal": "N",
            "mask": "M",
            "blend": "B",
        }.get(texture_type, "T")

        base_ue_name = sanitize_ue_name(clean_path, prefix="T_", max_length=56)
        ext = ".png" if self.export_format == "PNG" else ".tga"
        safe_name = f"{base_ue_name}_{type_suffix}{ext}"
        out_file = self.output_dir / safe_name

        try:
            if self.export_format == "TGA":
                _save_tga_rgba(pixels, out_file)
            else:
                pylib_save_png(np.ascontiguousarray(pixels).tobytes(), target_w, target_h, 4, str(out_file))
        except Exception as e:
            err = f"Failed saving converted texture to {out_file}: {e}"
            logger.error(err)
            self.errors.append(err)
            return None

        result = TextureConversionResult(
            source_path=clean_path,
            output_path=out_file,
            texture_type=texture_type,
            original_size=original_size,
            final_size=(target_w, target_h),
            was_downscaled=was_downscaled,
            converted_from_ssbump=converted_ssbump,
            is_srgb=is_srgb,
            has_alpha=has_alpha,
        )
        self.converted_cache[cache_key] = result
        return result
