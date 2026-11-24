"""
material_mapper.py — VMT to UEFN Material Mapping Pipeline
==========================================================

Analyzes Source 1 VMT material definitions and generates:
- UEFN Material Instance (MI) parameter JSON specifications
- Texture dependency lists with semantic roles (diffuse, normal, mask, blend)
- Blend mode mappings (Opaque, Masked, Translucent)
- Shading model assignments (DefaultLit, Unlit, DeferredDecal, Water)
- Documented fallbacks for unsupported or proprietary Source shaders
- Materials/materials_index.json for UEFN Toolbelt batch creation
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from SourceIO.library.shared.content_manager import ContentManager
from SourceIO.library.source1.vmt import VMT
from SourceIO.library.utils.tiny_path import TinyPath
from SourceIO.logger import SourceLogMan
from .coordinates import material_ue_name, normalize_source_material_path
from .texture_converter import UEFNTextureConverter

log_manager = SourceLogMan()
logger = log_manager.get_logger("UEFN::MaterialMapper")

_STRIP_PATCH_COORDS_RE = re.compile(r"_-?\d+_-?\d+_-?\d+.*$")


def _parse_vmt_color(val: Any) -> Optional[list[float]]:
    """Parse VMT color string like '{255 128 0}' or '[1.0 0.5 0.0]' into [r, g, b, 1.0]."""
    if val is None:
        return None
    if isinstance(val, (list, tuple)) and len(val) >= 3:
        nums = [float(x) for x in val[:3]]
        if any(x > 1.0 for x in nums):
            nums = [x / 255.0 for x in nums]
        return [round(nums[0], 4), round(nums[1], 4), round(nums[2], 4), 1.0]
    s = str(val).strip()
    is_byte = s.startswith("{")
    s = s.strip("{}[]() ")
    parts = [p for p in s.replace(",", " ").split() if p]
    if len(parts) >= 3:
        try:
            nums = [float(parts[0]), float(parts[1]), float(parts[2])]
            if is_byte or any(x > 1.0 for x in nums):
                nums = [x / 255.0 for x in nums]
            return [round(nums[0], 4), round(nums[1], 4), round(nums[2], 4), 1.0]
        except ValueError:
            return None
    return None


class UEFNMaterialSpec:
    """
    Representation of a material ready to be instantiated as a MaterialInstance in UEFN.
    """

    def __init__(
        self,
        name: str,
        source_path: str,
        shader: str,
        master_material_name: str,
        blend_mode: str = "Opaque",
        shading_model: str = "DefaultLit",
        two_sided: bool = False,
    ):
        self.name = name
        self.source_path = source_path
        self.shader = shader
        self.master_material_name = master_material_name
        self.blend_mode = blend_mode
        self.shading_model = shading_model
        self.two_sided = two_sided
        self.textures: dict[str, str] = {}  # ParameterName -> Texture Asset Name (stem)
        self.scalars: dict[str, float] = {}
        self.vectors: dict[str, list[float]] = {}
        self.warnings: list[str] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_path": self.source_path,
            "shader": self.shader,
            "master_material_name": self.master_material_name,
            "blend_mode": self.blend_mode,
            "shading_model": self.shading_model,
            "two_sided": self.two_sided,
            "textures": self.textures,
            "scalars": self.scalars,
            "vectors": self.vectors,
            "warnings": self.warnings,
        }


class UEFNMaterialMapper:
    """
    Parses VMT files and translates them into UEFN MaterialInstance specs.
    """

    def __init__(
        self,
        content_manager: ContentManager,
        texture_converter: UEFNTextureConverter,
        output_dir: Path,
    ):
        self.content_manager = content_manager
        self.texture_converter = texture_converter
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.material_cache: dict[str, UEFNMaterialSpec] = {}
        self.missing_materials: list[str] = []
        self.fallback_materials: list[dict[str, str]] = []

    @staticmethod
    def normalize_vmt_path(material_path: str) -> str:
        return normalize_source_material_path(material_path)

    def get_or_map_material(self, material_path: str) -> Optional[UEFNMaterialSpec]:
        clean_path = self.normalize_vmt_path(material_path)

        if clean_path in self.material_cache:
            return self.material_cache[clean_path]

        safe_name = material_ue_name(clean_path)

        file_buf = self.content_manager.find_file(TinyPath(clean_path))
        if not file_buf:
            # Retry stripping BSP cubemap patch suffix (_X_Y_Z)
            no_patch = _STRIP_PATCH_COORDS_RE.sub("", clean_path[:-4]) + ".vmt"
            if no_patch != clean_path:
                file_buf = self.content_manager.find_file(TinyPath(no_patch))

        if not file_buf:
            msg = f"VMT file not found: {clean_path}"
            logger.warn(msg)
            if clean_path not in self.missing_materials:
                self.missing_materials.append(clean_path)
            fallback_spec = UEFNMaterialSpec(
                name=safe_name,
                source_path=clean_path,
                shader="MissingFallback",
                master_material_name="M_Master_Fallback",
                blend_mode="Opaque",
            )
            fallback_spec.vectors["BaseColorTint"] = [0.8, 0.0, 0.8, 1.0]
            fallback_spec.warnings.append(msg)
            self.material_cache[clean_path] = fallback_spec
            self._write_spec_file(fallback_spec)
            return fallback_spec

        try:
            vmt = VMT(file_buf, clean_path, self.content_manager)
        except Exception as e:
            err = f"Failed to parse VMT {clean_path}: {e}"
            logger.error(err)
            self.fallback_materials.append({
                "asset": clean_path,
                "fallback": "M_Master_Fallback",
                "reason": err,
            })
            spec = UEFNMaterialSpec(
                name=safe_name,
                source_path=clean_path,
                shader="ParseErrorFallback",
                master_material_name="M_Master_Fallback",
            )
            spec.vectors["BaseColorTint"] = [1.0, 0.5, 0.0, 1.0]
            spec.warnings.append(err)
            self.material_cache[clean_path] = spec
            self._write_spec_file(spec)
            return spec

        shader_name = vmt.shader.lower() if vmt.shader else "lightmappedgeneric"
        spec = self._build_spec_from_vmt(safe_name, shader_name, vmt, clean_path)
        self.material_cache[clean_path] = spec
        self._write_spec_file(spec)
        return spec

    def _write_spec_file(self, spec: UEFNMaterialSpec) -> None:
        spec_path = self.output_dir / f"{spec.name}.json"
        with open(spec_path, "w", encoding="utf-8") as f:
            json.dump(spec.to_dict(), f, indent=2)

    def write_materials_index(self) -> Path:
        """
        Write Materials/materials_index.json containing all mapped MaterialInstance specs
        and converted texture metadata (sRGB, compression) for UEFN batch import.
        """
        index_path = self.output_dir / "materials_index.json"
        data = {
            "materials": {spec.name: spec.to_dict() for spec in self.material_cache.values()},
            "textures": {
                res.output_path.stem: res.to_dict()
                for res in self.texture_converter.converted_cache.values()
            },
        }
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return index_path

    def _build_spec_from_vmt(
        self,
        name: str,
        shader: str,
        vmt: VMT,
        vmt_path: str,
    ) -> UEFNMaterialSpec:
        translucent = bool(vmt.get_int("$translucent", 0))
        additive = bool(vmt.get_int("$additive", 0))
        alphatest = bool(vmt.get_int("$alphatest", 0))
        nocull = bool(vmt.get_int("$nocull", 0))
        is_decal = bool(vmt.get_int("$decal", 0)) or "decal" in shader

        blend_mode = "Opaque"
        if translucent or additive:
            blend_mode = "Translucent"
        elif alphatest:
            blend_mode = "Masked"

        shading_model = "DefaultLit"

        if "worldvertextransition" in shader:
            master = "M_Master_Displacement_2Way"
        elif "water" in shader:
            master = "M_Master_Water"
            blend_mode = "Translucent"
        elif "unlit" in shader:
            master = "M_Master_Unlit_Translucent" if blend_mode == "Translucent" else "M_Master_Unlit"
            shading_model = "Unlit"
        elif is_decal:
            master = "M_Master_Decal"
            blend_mode = "Translucent"
            shading_model = "DeferredDecal"
        elif "vertexlitgeneric" in shader:
            master = (
                "M_Master_Prop_Masked"
                if blend_mode == "Masked"
                else (
                    "M_Master_Prop_Translucent"
                    if blend_mode == "Translucent"
                    else "M_Master_Prop"
                )
            )
        else:
            master = (
                "M_Master_World_Masked"
                if blend_mode == "Masked"
                else (
                    "M_Master_World_Translucent"
                    if blend_mode == "Translucent"
                    else "M_Master_World"
                )
            )

        spec = UEFNMaterialSpec(
            name=name,
            source_path=vmt_path,
            shader=shader,
            master_material_name=master,
            blend_mode=blend_mode,
            shading_model=shading_model,
            two_sided=nocull,
        )

        # Opacity / alpha scalar
        alpha_param = vmt.get("$alpha")
        if alpha_param is not None:
            try:
                spec.scalars["Opacity"] = float(alpha_param)
            except (TypeError, ValueError):
                pass

        if alphatest:
            spec.scalars["AlphaTestReference"] = float(vmt.get_float("$alphatestreference", 0.5))

        # Tint / color multiplier
        color_vec = _parse_vmt_color(vmt.get("$color2") or vmt.get("$color"))
        if color_vec:
            spec.vectors["BaseColorTint"] = color_vec

        # 1. Base Diffuse Texture ($basetexture)
        base_tex = vmt.get("$basetexture")
        if base_tex:
            conv = self.texture_converter.convert_texture(str(base_tex), "diffuse")
            if conv:
                spec.textures["BaseColorTexture"] = conv.output_path.stem
            else:
                spec.warnings.append(f"Failed converting $basetexture: {base_tex}")

        # 2. Second Diffuse for Displacements ($basetexture2)
        base_tex2 = vmt.get("$basetexture2")
        if base_tex2:
            conv2 = self.texture_converter.convert_texture(str(base_tex2), "diffuse")
            if conv2:
                spec.textures["BaseColorTexture2"] = conv2.output_path.stem

        # 3. Normal / Bump Map ($bumpmap) — check $ssbump
        is_ssbump = bool(vmt.get_int("$ssbump", 0))
        bump_tex = vmt.get("$bumpmap")
        if bump_tex:
            conv_bump = self.texture_converter.convert_texture(str(bump_tex), "normal", is_ssbump=is_ssbump)
            if conv_bump:
                spec.textures["NormalTexture"] = conv_bump.output_path.stem

        # 4. Second Normal Map ($bumpmap2)
        bump_tex2 = vmt.get("$bumpmap2")
        if bump_tex2:
            conv_bump2 = self.texture_converter.convert_texture(str(bump_tex2), "normal", is_ssbump=is_ssbump)
            if conv_bump2:
                spec.textures["NormalTexture2"] = conv_bump2.output_path.stem

        # 5. Blend Modulation Texture ($blendmodulatetexture)
        blend_tex = vmt.get("$blendmodulatetexture")
        if blend_tex:
            conv_blend = self.texture_converter.convert_texture(str(blend_tex), "blend")
            if conv_blend:
                spec.textures["BlendModulateTexture"] = conv_blend.output_path.stem

        # 6. Self-Illumination ($selfillum)
        if vmt.get_int("$selfillum", 0):
            spec.scalars["EmissiveStrength"] = 1.0
            illum_tint = _parse_vmt_color(vmt.get("$selfillumtint"))
            if illum_tint:
                spec.vectors["EmissiveTint"] = illum_tint
            selfillum_mask = vmt.get("$selfillummask")
            if selfillum_mask:
                conv_mask = self.texture_converter.convert_texture(str(selfillum_mask), "mask")
                if conv_mask:
                    spec.textures["EmissiveMask"] = conv_mask.output_path.stem

        # 7. Phong / Specular / Envmap Settings
        if vmt.get_int("$phong", 0):
            phong_exponent = float(vmt.get_float("$phongexponent", 20.0))
            roughness = max(0.05, min(1.0, 1.0 - (phong_exponent / 150.0)))
            spec.scalars["Roughness"] = round(roughness, 3)
            spec.scalars["Specular"] = min(1.0, float(vmt.get_float("$phongboost", 1.0)) * 0.5)
        else:
            spec.scalars["Roughness"] = 0.85
            spec.scalars["Specular"] = 0.2

        envmap_mask = vmt.get("$envmapmask")
        if envmap_mask:
            conv_env = self.texture_converter.convert_texture(str(envmap_mask), "mask")
            if conv_env:
                spec.textures["SpecularMask"] = conv_env.output_path.stem

        # 8. Water Specifics
        if "water" in shader:
            normal_map = vmt.get("$normalmap")
            if normal_map:
                conv_water_n = self.texture_converter.convert_texture(str(normal_map), "normal")
                if conv_water_n:
                    spec.textures["WaterNormalTexture"] = conv_water_n.output_path.stem
            fog_col = _parse_vmt_color(vmt.get("$fogcolor"))
            if fog_col:
                spec.vectors["WaterFogColor"] = fog_col
            spec.scalars["WaterRefraction"] = 1.333
            warn_msg = (
                "Water shader mapped to UEFN Translucent approximation. "
                "Place a Fortnite Water Body / Water Zone device for swimming & buoyancy."
            )
            spec.warnings.append(warn_msg)
            self.fallback_materials.append({
                "asset": vmt_path,
                "fallback": "M_Master_Water",
                "reason": warn_msg,
            })

        # 9. Unsupported / Refractive Shader Handling
        unsupported_shaders = {"refract", "shatteredglass", "eyerefract", "teeth"}
        if shader in unsupported_shaders:
            spec.master_material_name = "M_Master_World_Translucent" if "glass" in shader or "refract" in shader else "M_Master_Fallback"
            msg = f"Specialized Source shader '{shader}' in {vmt_path} mapped to {spec.master_material_name} approximation."
            spec.warnings.append(msg)
            self.fallback_materials.append({
                "asset": vmt_path,
                "fallback": spec.master_material_name,
                "reason": msg,
            })

        return spec
