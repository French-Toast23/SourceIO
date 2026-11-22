"""
material_mapper.py — VMT to UEFN Material Mapping Pipeline
==========================================================

Analyzes Source 1 VMT material definitions and generates:
- UEFN Material Instance (MI) parameter JSON specifications
- Texture dependency lists with semantic roles (diffuse, normal, mask, blend)
- Blend mode mappings (Opaque, Masked, Translucent)
- Shading model assignments (DefaultLit, Subsurface, Water/Translucent, Decal)
- Documented fallbacks for unsupported or proprietary Source shaders
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from SourceIO.library.shared.content_manager import ContentManager
from SourceIO.library.source1.vmt import VMT
from SourceIO.library.utils.tiny_path import TinyPath
from SourceIO.logger import SourceLogMan
from .texture_converter import UEFNTextureConverter

log_manager = SourceLogMan()
logger = log_manager.get_logger("UEFN::MaterialMapper")


class UEFNMaterialSpec:
    """
    Representation of a material ready to be instantiated as a MaterialInstance in UEFN.
    """

    def __init__(
        self,
        name: str,
        shader: str,
        master_material: str,
        blend_mode: str = "Opaque",
        shading_model: str = "DefaultLit",
        two_sided: bool = False,
    ):
        self.name = name
        self.shader = shader
        self.master_material = master_material
        self.blend_mode = blend_mode
        self.shading_model = shading_model
        self.two_sided = two_sided
        self.textures: dict[str, str] = {}  # ParameterName -> Converted Relative Texture Path
        self.scalars: dict[str, float] = {}
        self.vectors: dict[str, list[float]] = {}
        self.warnings: list[str] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "shader": self.shader,
            "master_material": self.master_material,
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
        self.fallback_materials: list[str] = []

    def get_or_map_material(self, material_path: str) -> Optional[UEFNMaterialSpec]:
        clean_path = material_path.strip().replace("\\", "/").lower()
        clean_path = clean_path.lstrip("/").rstrip("/")
        if not clean_path.endswith(".vmt"):
            clean_path += ".vmt"
        if not clean_path.startswith("materials/"):
            clean_path = "materials/" + clean_path

        if clean_path in self.material_cache:
            return self.material_cache[clean_path]

        mat_stem = Path(clean_path).stem
        safe_name = f"MI_{mat_stem}".replace(" ", "_")

        file_buf = self.content_manager.find_file(TinyPath(clean_path))
        if not file_buf:
            msg = f"VMT file not found: {clean_path}"
            logger.warning(msg)
            self.missing_materials.append(clean_path)
            # Create a basic missing placeholder material spec
            fallback_spec = UEFNMaterialSpec(
                name=safe_name,
                shader="MissingFallback",
                master_material="/Game/TF2_Map/Materials/M_Master_Fallback",
                blend_mode="Opaque",
            )
            fallback_spec.vectors["BaseColorTint"] = [0.8, 0.0, 0.8, 1.0]  # Magenta missing-texture color
            fallback_spec.warnings.append(msg)
            self.material_cache[clean_path] = fallback_spec
            return fallback_spec

        try:
            vmt = VMT(file_buf, clean_path, self.content_manager)
        except Exception as e:
            err = f"Failed to parse VMT {clean_path}: {e}"
            logger.error(err)
            self.fallback_materials.append(clean_path)
            spec = UEFNMaterialSpec(
                name=safe_name,
                shader="ParseErrorFallback",
                master_material="/Game/TF2_Map/Materials/M_Master_Fallback",
            )
            spec.vectors["BaseColorTint"] = [1.0, 0.5, 0.0, 1.0]
            spec.warnings.append(err)
            self.material_cache[clean_path] = spec
            return spec

        shader_name = vmt.shader.lower() if vmt.shader else "lightmappedgeneric"
        spec = self._build_spec_from_vmt(safe_name, shader_name, vmt, clean_path)
        self.material_cache[clean_path] = spec

        # Save spec JSON to Materials/ folder
        spec_path = self.output_dir / f"{safe_name}.json"
        with open(spec_path, "w", encoding="utf-8") as f:
            json.dump(spec.to_dict(), f, indent=2)

        return spec

    def _build_spec_from_vmt(
        self,
        name: str,
        shader: str,
        vmt: VMT,
        vmt_path: str,
    ) -> UEFNMaterialSpec:
        translucent = bool(vmt.get_int("$translucent", 0))
        alphatest = bool(vmt.get_int("$alphatest", 0))
        nocull = bool(vmt.get_int("$nocull", 0))

        blend_mode = "Opaque"
        if translucent:
            blend_mode = "Translucent"
        elif alphatest:
            blend_mode = "Masked"

        shading_model = "DefaultLit"

        # Determine Master Material based on shader family
        if "worldvertextransition" in shader:
            master = "/Game/TF2_Map/Materials/M_Master_Displacement_2Way"
        elif "water" in shader:
            master = "/Game/TF2_Map/Materials/M_Master_Water"
            blend_mode = "Translucent"
        elif "unlit" in shader:
            master = "/Game/TF2_Map/Materials/M_Master_Unlit"
            shading_model = "Unlit"
        elif "decal" in shader:
            master = "/Game/TF2_Map/Materials/M_Master_Decal"
            shading_model = "DeferredDecal"
        elif "vertexlitgeneric" in shader:
            master = (
                "/Game/TF2_Map/Materials/M_Master_Prop_Masked"
                if blend_mode == "Masked"
                else (
                    "/Game/TF2_Map/Materials/M_Master_Prop_Translucent"
                    if blend_mode == "Translucent"
                    else "/Game/TF2_Map/Materials/M_Master_Prop"
                )
            )
        else:
            master = (
                "/Game/TF2_Map/Materials/M_Master_World_Masked"
                if blend_mode == "Masked"
                else (
                    "/Game/TF2_Map/Materials/M_Master_World_Translucent"
                    if blend_mode == "Translucent"
                    else "/Game/TF2_Map/Materials/M_Master_World"
                )
            )

        spec = UEFNMaterialSpec(
            name=name,
            shader=shader,
            master_material=master,
            blend_mode=blend_mode,
            shading_model=shading_model,
            two_sided=nocull,
        )

        # 1. Base Diffuse Texture ($basetexture)
        base_tex = vmt.get_param("$basetexture")
        if base_tex:
            conv = self.texture_converter.convert_texture(str(base_tex), "diffuse")
            if conv:
                spec.textures["BaseColorTexture"] = conv.output_path.name
            else:
                spec.warnings.append(f"Failed converting $basetexture: {base_tex}")

        # 2. Second Diffuse for Displacements ($basetexture2)
        base_tex2 = vmt.get_param("$basetexture2")
        if base_tex2:
            conv2 = self.texture_converter.convert_texture(str(base_tex2), "diffuse")
            if conv2:
                spec.textures["BaseColorTexture2"] = conv2.output_path.name

        # 3. Normal / Bump Map ($bumpmap)
        bump_tex = vmt.get_param("$bumpmap")
        if bump_tex:
            conv_bump = self.texture_converter.convert_texture(str(bump_tex), "normal")
            if conv_bump:
                spec.textures["NormalTexture"] = conv_bump.output_path.name

        # 4. Second Normal Map ($bumpmap2)
        bump_tex2 = vmt.get_param("$bumpmap2")
        if bump_tex2:
            conv_bump2 = self.texture_converter.convert_texture(str(bump_tex2), "normal")
            if conv_bump2:
                spec.textures["NormalTexture2"] = conv_bump2.output_path.name

        # 5. Blend Modulation Texture ($blendmodulatetexture)
        blend_tex = vmt.get_param("$blendmodulatetexture")
        if blend_tex:
            conv_blend = self.texture_converter.convert_texture(str(blend_tex), "blend")
            if conv_blend:
                spec.textures["BlendModulateTexture"] = conv_blend.output_path.name

        # 6. Self-Illumination ($selfillum)
        if vmt.get_int("$selfillum", 0):
            spec.scalars["EmissiveStrength"] = 1.0
            selfillum_mask = vmt.get_param("$selfillummask")
            if selfillum_mask:
                conv_mask = self.texture_converter.convert_texture(str(selfillum_mask), "mask")
                if conv_mask:
                    spec.textures["EmissiveMask"] = conv_mask.output_path.name

        # 7. Phong / Specular Settings
        if vmt.get_int("$phong", 0):
            phong_exponent = float(vmt.get_param("$phongexponent", 20.0))
            # Convert Source phong exponent (1..100) to Unreal Roughness (0.0..1.0)
            roughness = max(0.05, min(1.0, 1.0 - (phong_exponent / 150.0)))
            spec.scalars["Roughness"] = round(roughness, 3)
            spec.scalars["Specular"] = 0.5
        else:
            spec.scalars["Roughness"] = 0.85
            spec.scalars["Specular"] = 0.2

        # 8. Water Specifics
        if "water" in shader:
            normal_map = vmt.get_param("$normalmap")
            if normal_map:
                conv_water_n = self.texture_converter.convert_texture(str(normal_map), "normal")
                if conv_water_n:
                    spec.textures["WaterNormalTexture"] = conv_water_n.output_path.name
            spec.scalars["WaterRefraction"] = 1.333
            spec.warnings.append(
                "Water shader converted to UEFN Translucent approximation. "
                "For interactive gameplay swimming and buoyancy, place a Fortnite Water Body / Water Zone device."
            )

        # 9. Unsupported Shader Handling
        unsupported_shaders = {"refract", "shatteredglass", "eyerefract", "teeth"}
        if shader in unsupported_shaders:
            spec.master_material = "/Game/TF2_Map/Materials/M_Master_Fallback"
            msg = f"Specialized Source shader '{shader}' in {vmt_path} mapped to standard lit fallback."
            spec.warnings.append(msg)
            self.fallback_materials.append(vmt_path)

        return spec
