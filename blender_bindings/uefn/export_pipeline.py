"""
export_pipeline.py — Master Orchestration Pipeline for UEFN Map Export
======================================================================

Orchestrates:
1. ContentManager setup (BSP pakfile + TF2 VPK search paths).
2. Mesh extraction, chunking, and FBX export with collision hulls (UCX_).
3. Texture decoding and conversion (sRGB vs Linear, normal map green inversion, power-of-two budgets).
4. VMT to UEFN MaterialInstance mapping.
5. Audio event resolution and 16-bit PCM WAV conversion.
6. Manifest assembly (map_manifest.json) and validation reporting (export_report.md/.json).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from SourceIO.library.shared.content_manager import ContentManager
from SourceIO.library.source1.bsp.bsp_file import open_bsp, VBSPFile
from SourceIO.library.source1.bsp.datatypes.static_prop_lump import StaticPropLump
from SourceIO.library.source1.bsp.lumps import (
    EntityLump,
    GameLump,
    PakLump,
)
from SourceIO.library.utils import FileBuffer
from SourceIO.library.utils.tiny_path import TinyPath
from SourceIO.logger import SourceLogMan
from .audio_exporter import UEFNAudioExporter
from .coordinates import (
    parse_vector_string,
    source_to_unreal_position,
    source_to_unreal_rotation,
    source_to_unreal_scale,
)
from .manifest_builder import PlacedActorRecord, UEFNManifestBuilder
from .material_mapper import UEFNMaterialMapper
from .mesh_exporter import UEFNMeshExporter
from .texture_converter import UEFNTextureConverter

log_manager = SourceLogMan()
logger = log_manager.get_logger("UEFN::ExportPipeline")

GAMEPLAY_ENTITY_CLASSES = {
    "info_player_teamspawn",
    "info_player_tfteamspawn",
    "team_control_point",
    "trigger_capture_area",
    "item_teamflag",
    "func_capturezone",
    "func_regenerate",
    "func_respawnroom",
    "func_respawnroomvisualizer",
    "func_nobuild",
    "func_door",
    "func_door_rotating",
    "func_button",
    "trigger_multiple",
    "trigger_once",
    "sky_camera",
    "water_lod_control",
    "light_environment",
}


class UEFNExportOptions:
    def __init__(
        self,
        bsp_path: Path,
        tf2_game_dir: Optional[Path],
        export_root_dir: Path,
        generate_collision: bool = True,
        max_texture_size: int = 2048,
        world_chunk_size: float = 2048.0,
        export_audio: bool = True,
    ):
        self.bsp_path = Path(bsp_path)
        self.tf2_game_dir = Path(tf2_game_dir) if tf2_game_dir else None
        self.export_root_dir = Path(export_root_dir)
        self.generate_collision = generate_collision
        self.max_texture_size = max_texture_size
        self.world_chunk_size = world_chunk_size
        self.export_audio = export_audio


class UEFNExportPipeline:
    """
    Executes the complete Source 1 to UEFN export pipeline.
    """

    def __init__(self, options: UEFNExportOptions):
        self.options = options
        self.export_root = self.options.export_root_dir
        self.meshes_dir = self.export_root / "Meshes"
        self.textures_dir = self.export_root / "Textures"
        self.materials_dir = self.export_root / "Materials"
        self.audio_dir = self.export_root / "Audio"

        for d in (self.meshes_dir, self.textures_dir, self.materials_dir, self.audio_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.content_manager = ContentManager()
        self.map_name = self.options.bsp_path.stem
        self.manifest_builder = UEFNManifestBuilder(self.map_name, self.export_root)
        self.texture_converter = UEFNTextureConverter(
            self.content_manager,
            self.textures_dir,
            max_size=self.options.max_texture_size,
        )
        self.material_mapper = UEFNMaterialMapper(
            self.content_manager,
            self.texture_converter,
            self.materials_dir,
        )
        self.mesh_exporter = UEFNMeshExporter(
            self.meshes_dir,
            generate_collision=self.options.generate_collision,
            world_chunk_size=self.options.world_chunk_size,
        )
        self.audio_exporter = UEFNAudioExporter(
            self.content_manager,
            self.audio_dir,
        )

    def execute(self) -> dict[str, Any]:
        logger.info(f"Starting UEFN export pipeline for {self.map_name}...")

        # 1. Mount TF2 Game Files (VPKs & directories)
        if self.options.tf2_game_dir and self.options.tf2_game_dir.exists():
            logger.info(f"Scanning TF2 directory: {self.options.tf2_game_dir}")
            self.content_manager.scan_for_content(TinyPath(self.options.tf2_game_dir))

        # 2. Open BSP file
        bsp_tiny = TinyPath(self.options.bsp_path)
        file_buf = FileBuffer(bsp_tiny)
        bsp = open_bsp(bsp_tiny, file_buf, content_manager=self.content_manager)
        if not bsp:
            err = f"Failed to open BSP file: {self.options.bsp_path}"
            logger.error(err)
            self.manifest_builder.record_hard_error(err)
            self.manifest_builder.write_reports()
            return {"status": "error", "error": err}

        # 3. Mount embedded PakFile (Lump 40)
        pak_lump: Optional[PakLump] = bsp.get_lump("LUMP_PAK")
        if pak_lump:
            logger.info("Mounted BSP embedded PakFile (Lump 40)")
            self.content_manager.add_child(pak_lump)

        # 4. Process Audio Scripts & Map Audio Entities
        if self.options.export_audio:
            logger.info("Parsing TF2 audio manifest and sound scripts...")
            self.audio_exporter.load_sound_manifest()

        # 5. Process BSP Entities (Lump 0)
        entity_lump: Optional[EntityLump] = bsp.get_lump("LUMP_ENTITIES")
        if entity_lump and entity_lump.entities:
            self._process_entities(entity_lump.entities)

        # 6. Process Static Props (Lump 35)
        game_lump: Optional[GameLump] = bsp.get_lump("LUMP_GAME_LUMP")
        if game_lump:
            static_prop_lump: Optional[StaticPropLump] = game_lump.game_lumps.get("sprp")
            if static_prop_lump:
                self._process_static_props(static_prop_lump)

        # 7. Collect reports and warnings from sub-exporters
        for w in self.texture_converter.warnings:
            self.manifest_builder.record_budget_warning(w)
        for w in self.mesh_exporter.warnings:
            self.manifest_builder.record_budget_warning(w)
        for e in self.texture_converter.errors:
            self.manifest_builder.record_hard_error(e)
        for e in self.mesh_exporter.errors:
            self.manifest_builder.record_hard_error(e)
        for miss_mat in self.material_mapper.missing_materials:
            self.manifest_builder.record_missing_asset("Material", miss_mat, "VMT file not found")
        for fb_mat in self.material_mapper.fallback_materials:
            self.manifest_builder.record_fallback("Material", fb_mat, "M_Master_Fallback", "Unsupported shader")

        # 8. Write Audio Mapping Document
        if self.options.export_audio:
            audio_doc_path = self.export_root / "docs" / "AUDIO_MAPPING.md"
            audio_doc_path.parent.mkdir(parents=True, exist_ok=True)
            self.audio_exporter.generate_mapping_markdown(audio_doc_path)

        # 9. Write Manifest and Final Reports
        manifest_path = self.manifest_builder.write_manifest()
        report_json, report_md = self.manifest_builder.write_reports()

        logger.info(f"Pipeline complete! Manifest: {manifest_path}, Report: {report_md}")

        return {
            "status": "ok",
            "manifest_path": str(manifest_path),
            "report_md_path": str(report_md),
            "report_json_path": str(report_json),
            "total_actors": len(self.manifest_builder.placed_actors),
            "gameplay_entities": len(self.manifest_builder.gameplay_entities),
            "missing_assets_count": len(self.manifest_builder.missing_assets),
        }

    def _process_entities(self, entities: list[dict[str, Any]]) -> None:
        logger.info(f"Processing {len(entities)} map entities...")

        for n, ent in enumerate(entities):
            classname = ent.get("classname", "").lower()
            targetname = ent.get("targetname", f"{classname}_{n}")

            # 1. Gameplay Entities
            if classname in GAMEPLAY_ENTITY_CLASSES:
                self.manifest_builder.expected_counts["gameplay_entities"] += 1
                origin_raw = parse_vector_string(ent.get("origin", "0 0 0"))
                angles_raw = parse_vector_string(ent.get("angles", "0 0 0"))

                ue_loc = source_to_unreal_position(origin_raw)
                ue_rot = source_to_unreal_rotation(angles_raw)

                gp_record = {
                    "classname": classname,
                    "targetname": targetname,
                    "location": ue_loc,
                    "rotation": ue_rot,
                    "keyvalues": ent,
                }
                self.manifest_builder.gameplay_entities.append(gp_record)
                self.manifest_builder.exported_counts["gameplay_entities"] += 1

                # Record in placed actors as a logical marker actor
                actor_rec = PlacedActorRecord(
                    actor_name=f"TF2_{targetname}",
                    asset_reference=None,
                    location=ue_loc,
                    rotation=ue_rot,
                    scale=[1.0, 1.0, 1.0],
                    entity_class=classname,
                    source_entity_data=ent,
                    is_gameplay_entity=True,
                )
                self.manifest_builder.add_actor(actor_rec)

            # 2. Ambient Audio Entities
            elif classname == "ambient_generic":
                self.manifest_builder.expected_counts["audio_entities"] += 1
                audio_spec = self.audio_exporter.process_ambient_entity(ent)
                self.manifest_builder.exported_counts["audio_entities"] += 1

                actor_rec = PlacedActorRecord(
                    actor_name=f"Audio_{audio_spec['entity_name']}",
                    asset_reference=audio_spec["converted_asset"],
                    location=audio_spec["location"],
                    rotation=[0.0, 0.0, 0.0],
                    scale=[1.0, 1.0, 1.0],
                    entity_class="ambient_generic",
                    source_entity_data=ent,
                    is_gameplay_entity=False,
                )
                self.manifest_builder.add_actor(actor_rec)

            # 3. Dynamic / Physics Props
            elif classname in ("prop_dynamic", "prop_physics", "prop_physics_override", "prop_dynamic_override"):
                self.manifest_builder.expected_counts["static_props"] += 1
                model_path = ent.get("model", "")
                if not model_path:
                    continue

                clean_model = model_path.replace("\\", "/").lower()
                clean_stem = Path(clean_model).stem
                asset_ref = f"SM_{clean_stem}.fbx"

                origin_raw = parse_vector_string(ent.get("origin", "0 0 0"))
                angles_raw = parse_vector_string(ent.get("angles", "0 0 0"))
                scale_raw = float(ent.get("modelscale", 1.0))

                ue_loc = source_to_unreal_position(origin_raw)
                ue_rot = source_to_unreal_rotation(angles_raw)
                ue_scale = source_to_unreal_scale(scale_raw)

                # Skin assignment
                skin = ent.get("skin", "0")

                actor_rec = PlacedActorRecord(
                    actor_name=f"Prop_{clean_stem}_{n}",
                    asset_reference=asset_ref,
                    location=ue_loc,
                    rotation=ue_rot,
                    scale=ue_scale,
                    entity_class=classname,
                    source_entity_data=ent,
                    is_gameplay_entity=False,
                )
                self.manifest_builder.add_actor(actor_rec)
                self.manifest_builder.exported_counts["static_props"] += 1

    def _process_static_props(self, static_prop_lump: StaticPropLump) -> None:
        total_props = len(static_prop_lump.static_props)
        unique_models = len(static_prop_lump.model_names)
        logger.info(f"Processing {total_props} static props ({unique_models} unique models)...")

        self.manifest_builder.expected_counts["static_props"] += total_props

        for n, prop in enumerate(static_prop_lump.static_props):
            model_name = static_prop_lump.model_names[prop.prop_type]
            clean_stem = Path(model_name).stem.replace(" ", "_").lower()
            asset_ref = f"SM_{clean_stem}.fbx"

            # Coordinate conversion
            ue_loc = source_to_unreal_position(prop.origin)
            ue_rot = source_to_unreal_rotation(prop.rotation)
            ue_scale = source_to_unreal_scale(prop.scaling)

            tint = None
            if prop.diffuse_modulation:
                tint = [round(c / 255.0, 3) for c in prop.diffuse_modulation[:3]]
                tint.append(1.0)

            fade_info = {
                "fade_min_dist": round(prop.fade_min_dist * 2.54, 2) if hasattr(prop, "fade_min_dist") else 0.0,
                "fade_max_dist": round(prop.fade_max_dist * 2.54, 2) if hasattr(prop, "fade_max_dist") else 0.0,
            }

            actor_rec = PlacedActorRecord(
                actor_name=f"StaticProp_{clean_stem}_{n}",
                asset_reference=asset_ref,
                location=ue_loc,
                rotation=ue_rot,
                scale=ue_scale,
                entity_class="prop_static",
                source_entity_data={
                    "model": model_name,
                    "skin": prop.skin,
                    "solid": prop.solid if hasattr(prop, "solid") else 6,
                },
                tint_color=tint,
                fade_distances=fade_info,
                is_gameplay_entity=False,
            )
            self.manifest_builder.add_actor(actor_rec)
            self.manifest_builder.exported_counts["static_props"] += 1
