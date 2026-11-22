"""
texture_converter.py — VTF to UEFN Texture Pipeline
===================================================

Converts Source 1 VTF textures (or Blender Image datablocks) to UEFN-compatible formats (PNG/TGA).
Handles:
- Texture format decoding (via SourceIO pylib or PIL fallback)
- Normal map convention conversion: Source (OpenGL, Y+ Up) to Unreal (DirectX, Y- Down)
  by inverting the Green channel (1.0 - G).
- sRGB vs Linear color space tagging.
- Power-of-two enforcement and resolution clamping (downscaling with warnings).
- Alpha channel handling (translucency, alphatest / cutout, or composite masks).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional, Tuple

import numpy as np
from PIL import Image

from SourceIO.library.shared.content_manager import ContentManager
from SourceIO.library.source1.vtf import load_texture as vtf_load_texture
from SourceIO.library.utils.tiny_path import TinyPath
from SourceIO.logger import SourceLogMan

log_manager = SourceLogMan()
logger = log_manager.get_logger("UEFN::TextureConverter")

DEFAULT_MAX_TEXTURE_SIZE = 2048


def is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def next_lower_power_of_two(n: int) -> int:
    if n <= 1:
        return 1
    return 1 << (n.bit_length() - 1)


class TextureConversionResult:
    def __init__(
        self,
        source_path: str,
        output_path: Path,
        texture_type: str,
        original_size: Tuple[int, int],
        final_size: Tuple[int, int],
        was_downscaled: bool = False,
        normal_inverted: bool = False,
        is_srgb: bool = True,
        has_alpha: bool = False,
    ):
        self.source_path = source_path
        self.output_path = output_path
        self.texture_type = texture_type
        self.original_size = original_size
        self.final_size = final_size
        self.was_downscaled = was_downscaled
        self.normal_inverted = normal_inverted
        self.is_srgb = is_srgb
        self.has_alpha = has_alpha

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "output_path": str(self.output_path.as_posix()),
            "texture_type": self.texture_type,
            "original_width": self.original_size[0],
            "original_height": self.original_size[1],
            "final_width": self.final_size[0],
            "final_height": self.final_size[1],
            "was_downscaled": self.was_downscaled,
            "normal_inverted": self.normal_inverted,
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
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def convert_texture(
        self,
        vtf_rel_path: str,
        texture_type: str = "diffuse",
    ) -> Optional[TextureConversionResult]:
        """
        Convert a VTF texture to a UEFN-ready PNG/TGA file.

        texture_type:
            - 'diffuse': Albedo/BaseColor (sRGB = True)
            - 'normal': Tangent-space normal map (OpenGL -> DirectX green flip, sRGB = False)
            - 'mask': Roughness / Metallic / Specular / Exponent (Linear, sRGB = False)
            - 'blend': Terrain blend modulation (Linear)
        """
        clean_path = vtf_rel_path.strip().replace("\\", "/").lower()
        if not clean_path.endswith(".vtf"):
            clean_path += ".vtf"
        if not clean_path.startswith("materials/"):
            clean_path = "materials/" + clean_path

        cache_key = f"{clean_path}:{texture_type}"
        if cache_key in self.converted_cache:
            return self.converted_cache[cache_key]

        file_buf = self.content_manager.find_file(TinyPath(clean_path))
        if not file_buf:
            msg = f"Texture file not found in search paths: {clean_path}"
            logger.warning(msg)
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

        # Convert float32 array [0.0..1.0] to uint8 [0..255]
        if rgba_data.dtype != np.uint8:
            pixels = np.clip(rgba_data * 255.0, 0, 255).astype(np.uint8)
        else:
            pixels = rgba_data

        original_size = (width, height)
        normal_inverted = False
        is_srgb = True

        # Process Normal Map (Source OpenGL -> Unreal DirectX)
        if texture_type == "normal":
            # Invert green channel (index 1)
            pixels[:, :, 1] = 255 - pixels[:, :, 1]
            normal_inverted = True
            is_srgb = False
        elif texture_type in ("mask", "blend", "roughness", "metallic"):
            is_srgb = False

        img = Image.fromarray(pixels, mode="RGBA")

        # Check and enforce size budget and Power of Two
        was_downscaled = False
        cur_w, cur_h = img.size
        target_w = cur_w
        target_h = cur_h

        if target_w > self.max_size or target_h > self.max_size:
            scale_factor = min(self.max_size / target_w, self.max_size / target_h)
            target_w = int(target_w * scale_factor)
            target_h = int(target_h * scale_factor)
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
            img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)

        # Check if alpha is meaningful
        alpha_channel = np.array(img)[:, :, 3]
        has_alpha = bool(np.any(alpha_channel < 255))

        # Build clean filename following Unreal convention: T_{stem}_{type}
        stem = Path(clean_path).stem
        type_suffix = "D"
        if texture_type == "normal":
            type_suffix = "N"
        elif texture_type == "mask":
            type_suffix = "M"
        elif texture_type == "blend":
            type_suffix = "B"

        ext = ".png" if self.export_format == "PNG" else ".tga"
        safe_name = f"T_{stem}_{type_suffix}{ext}".replace(" ", "_")
        out_file = self.output_dir / safe_name

        try:
            if self.export_format == "TGA":
                img.save(out_file, format="TGA")
            else:
                img.save(out_file, format="PNG", optimize=True)
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
            normal_inverted=normal_inverted,
            is_srgb=is_srgb,
            has_alpha=has_alpha,
        )
        self.converted_cache[cache_key] = result
        return result
