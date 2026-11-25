"""
export_pipeline.py — Master Orchestration Pipeline for UEFN Map Export
======================================================================

Orchestrates complete Team Fortress 2 (Source 1) map export to UEFN:
1. Auto-detects Steam / TF2 installation if not specified.
2. Mounts BSP embedded PakFile (Lump 40) with priority BEFORE TF2 VPK search paths.
3. Uses existing imported Blender scene objects OR imports the BSP cleanly inside Blender.
4. Exports spatially chunked world geometry and displacement meshes (tool faces culled).
5. Exports visible brush entities (func_door, func_brush, etc.) and extracts trigger bounds.
6. Exports BSP overlays (LUMP_OVERLAYS) and infodecal placements.
7. Deduplicates and exports every unique MDL prop (static, dynamic, physics) at local origin
   with UCX_ convex collision hulls and per-skin material overrides.
8. Detects sky_camera and tags/transforms 3D skybox entities.
9. Converts VTFs (DirectX normal preservation, SSBump conversion, POT clamping) and maps VMTs.
10. Extracts soundscripts, soundscapes, and ambient audio to 16-bit PCM WAV.
11. Writes map_manifest.json, Materials/materials_index.json, export_report.json, and export_report.md.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Optional

import numpy as np

try:
    import bpy
    from mathutils import Vector
    IN_BLENDER = True
except ImportError:
    bpy = None
    Vector = None
    IN_BLENDER = False

from SourceIO.library.shared.content_manager import ContentManager
from SourceIO.library.source1.bsp.bsp_file import open_bsp, VBSPFile
from SourceIO.library.source1.bsp.datatypes.static_prop_lump import StaticPropLump
from SourceIO.library.source1.bsp.lumps import (
    EntityLump,
    GameLump,
    OverlayLump,
    PakLump,
    StringsLump,
    TextureDataLump,
    TextureInfoLump,
)
from SourceIO.library.utils import FileBuffer, kv1
from SourceIO.library.utils.math_utilities import SOURCE1_HAMMER_UNIT_TO_METERS
from SourceIO.library.utils.tiny_path import TinyPath
from SourceIO.logger import SourceLogMan
from .audio_exporter import UEFNAudioExporter
from .coordinates import (
    SOURCE1_HU_TO_CM,
    blender_euler_to_unreal_rotation,
    blender_to_unreal_position,
    material_ue_name,
    parse_vector_string,
    sanitize_ue_name,
    skybox_to_world_transform,
    source_to_unreal_position,
    source_to_unreal_rotation,
    source_to_unreal_scale,
)
from .manifest_builder import PlacedActorRecord, UEFNManifestBuilder
from .material_mapper import UEFNMaterialMapper
from .mesh_exporter import ExportedMeshInfo, UEFNMeshExporter, get_material_source_path, is_tool_material
from .texture_converter import UEFNTextureConverter

log_manager = SourceLogMan()
logger = log_manager.get_logger("UEFN::ExportPipeline")

_STRIP_PATCH_RE = re.compile(r"_-?\d+_-?\d+_-?\d+.*$")

GAMEPLAY_ENTITY_CLASSES = {
    "info_player_teamspawn",
    "info_player_tfteamspawn",
    "team_control_point",
    "team_control_point_master",
    "trigger_capture_area",
    "item_teamflag",
    "func_capturezone",
    "func_flagdetectionzone",
    "func_respawnflag",
    "func_regenerate",
    "func_respawnroom",
    "func_respawnroomvisualizer",
    "func_nobuild",
    "func_door",
    "func_door_rotating",
    "func_button",
    "trigger_multiple",
    "trigger_once",
    "trigger_push",
    "trigger_teleport",
    "info_teleport_destination",
    "item_healthkit_small",
    "item_healthkit_medium",
    "item_healthkit_full",
    "item_ammopack_small",
    "item_ammopack_medium",
    "item_ammopack_full",
    "sky_camera",
    "water_lod_control",
    "light_environment",
}

PROP_ENTITY_CLASSES = {
    "prop_dynamic",
    "prop_dynamic_override",
    "prop_physics",
    "prop_physics_override",
    "prop_physics_multiplayer",
    "prop_ragdoll",
}

# Trigger/logic brush classes whose geometry is invisible in game (we record their bounds instead of FBX)
INVISIBLE_BRUSH_ENTITIES = {
    "trigger_multiple",
    "trigger_once",
    "trigger_push",
    "trigger_teleport",
    "trigger_capture_area",
    "trigger_soundscape",
    "func_respawnroom",
    "func_capturezone",
    "func_nobuild",
    "func_areaportal",
    "func_occluder",
    "func_flagdetectionzone",
    "func_respawnflag",
}


def detect_tf2_game_dir() -> Optional[Path]:
    """
    Locate Team Fortress 2's 'tf' directory via Windows Registry and Steam's libraryfolders.vdf.
    """
    steam_paths: list[Path] = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            val, _ = winreg.QueryValueEx(key, "SteamPath")
            if val:
                steam_paths.append(Path(val))
    except Exception:
        pass

    steam_paths.extend([
        Path(r"C:\Program Files (x86)\Steam"),
        Path(r"C:\Program Files\Steam"),
    ])

    lib_roots: list[Path] = []
    for sp in steam_paths:
        vdf_path = sp / "steamapps" / "libraryfolders.vdf"
        if vdf_path.exists():
            try:
                text = vdf_path.read_text(encoding="utf-8", errors="ignore")
                root = kv1.loads(text, str(vdf_path))
                _, block = root.top()
                if isinstance(block, kv1.KV1Block):
                    for _, entry in block.blocks():
                        lib_p = entry.get_string("path", "")
                        apps = entry.get("apps", None)
                        if lib_p:
                            p = Path(lib_p)
                            # Prioritize libraries explicitly listing AppID 440 (TF2)
                            if isinstance(apps, kv1.KV1Block) and "440" in apps:
                                lib_roots.insert(0, p)
                            else:
                                lib_roots.append(p)
            except Exception:
                pass
        if sp.exists() and sp not in lib_roots:
            lib_roots.append(sp)

    for lib in lib_roots:
        tf_dir = lib / "steamapps" / "common" / "Team Fortress 2" / "tf"
        if (tf_dir / "gameinfo.txt").exists():
            return tf_dir
    return None


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
        cm_per_hu: float = SOURCE1_HU_TO_CM,
        scale_multiplier: float = 1.0,
        use_active_blender_scene: bool = True,
    ):
        self.bsp_path = Path(bsp_path)
        self.tf2_game_dir = Path(tf2_game_dir) if tf2_game_dir else detect_tf2_game_dir()
        self.export_root_dir = Path(export_root_dir)
        self.generate_collision = generate_collision
        self.max_texture_size = max_texture_size
        self.world_chunk_size = world_chunk_size
        self.export_audio = export_audio
        self.cm_per_hu = cm_per_hu
        self.scale_multiplier = scale_multiplier
        self.use_active_blender_scene = use_active_blender_scene


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
        self.blender_scale = SOURCE1_HAMMER_UNIT_TO_METERS

        self.manifest_builder = UEFNManifestBuilder(
            self.map_name,
            self.export_root,
            cm_per_hu=self.options.cm_per_hu,
            scale_multiplier=self.options.scale_multiplier,
        )
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
            blender_scale=self.blender_scale,
            scale_multiplier=self.options.scale_multiplier,
        )
        self.audio_exporter = UEFNAudioExporter(
            self.content_manager,
            self.audio_dir,
            cm_per_hu=self.options.cm_per_hu,
            scale_multiplier=self.options.scale_multiplier,
        )

        self._seen_materials: set[str] = set()
        self._mdl_skin_cache: dict[str, list[list[str]]] = {}  # model_path -> skin_group UE material names
        self._missing_models: set[str] = set()
        self._sky_camera_origin_hu: Optional[list[float]] = None
        self._sky_camera_scale: float = 16.0

    def _on_material_seen(self, mat_path: str) -> None:
        if not mat_path or is_tool_material(mat_path):
            return
        clean = mat_path.strip().replace("\\", "/").lower().lstrip("/")
        if clean not in self._seen_materials:
            self._seen_materials.add(clean)
            self.material_mapper.get_or_map_material(clean)

    def _mount_content(self, bsp: Optional[VBSPFile]) -> None:
        """
        Mount BSP PakFile first, then TF2 VPKs & directories so map-packed content overrides base game files.
        """
        self.content_manager.clean()
        if self.options.tf2_game_dir and self.options.tf2_game_dir.exists():
            logger.info(f"Scanning TF2 directory: {self.options.tf2_game_dir}")
            self.content_manager.scan_for_content(TinyPath(self.options.tf2_game_dir))

        if bsp is not None:
            pak_lump: Optional[PakLump] = bsp.get_lump("LUMP_PAK")
            if pak_lump:
                logger.info("Mounted BSP embedded PakFile (Lump 40) with priority 0")
                self.content_manager.add_child(pak_lump)
                # Ensure PakFile is checked BEFORE game VPKs
                other_children = [c for c in self.content_manager.children if c != pak_lump]
                self.content_manager.priority_list = [pak_lump, *other_children]

    def execute(self) -> dict[str, Any]:
        logger.info(f"Starting UEFN export pipeline for {self.map_name}...")

        # 1. Open BSP file
        bsp_tiny = TinyPath(self.options.bsp_path)
        if not self.options.bsp_path.exists():
            err = f"BSP file does not exist: {self.options.bsp_path}"
            logger.error(err)
            self.manifest_builder.record_hard_error(err)
            self.manifest_builder.write_reports()
            return {"status": "error", "error": err}

        file_buf = FileBuffer(bsp_tiny)
        bsp = open_bsp(bsp_tiny, file_buf, content_manager=self.content_manager)
        if not bsp:
            err = f"Failed to open BSP file: {self.options.bsp_path}"
            logger.error(err)
            self.manifest_builder.record_hard_error(err)
            self.manifest_builder.write_reports()
            return {"status": "error", "error": err}

        # 2. Mount PakFile + TF2 VPKs
        self._mount_content(bsp)

        # 3. Ensure Blender scene is populated if running inside Blender
        map_collection = None
        if IN_BLENDER:
            map_collection = self._ensure_blender_scene_loaded(bsp, bsp_tiny, file_buf)

        # 4. Audio Manifest & Soundscripts
        if self.options.export_audio:
            logger.info("Parsing TF2 soundscripts & soundscapes...")
            self.audio_exporter.load_sound_manifest()

        # 5. Extract sky_camera first so world/props can check 3D skybox membership
        entity_lump: Optional[EntityLump] = bsp.get_lump("LUMP_ENTITIES")
        if entity_lump and entity_lump.entities:
            self._detect_sky_camera(entity_lump.entities)

        # 6. Export World Geometry & Displacements (from Blender scene)
        if IN_BLENDER and map_collection is not None:
            self._export_world_and_displacements(map_collection)
            self._export_brush_entities(map_collection)
        elif not IN_BLENDER:
            self.manifest_builder.record_budget_warning(
                "Pipeline executed outside Blender: FBX meshes (world chunks, displacements, props) "
                "were NOT exported; run via Blender operator or 'blender --background' for full FBX output."
            )

        # 7. Export BSP Overlays (LUMP_OVERLAYS) & Decals
        self._export_overlays(bsp)

        # 8. Process Entities (Gameplay, Audio, Dynamic/Physics Props, Decals)
        if entity_lump and entity_lump.entities:
            self._process_entities(entity_lump.entities)

        # 9. Process Static Props (Lump 35) — deduplicate MDLs and export FBX
        game_lump: Optional[GameLump] = bsp.get_lump("LUMP_GAME_LUMP")
        if game_lump:
            static_prop_lump: Optional[StaticPropLump] = game_lump.game_lumps.get("sprp")
            if static_prop_lump:
                self._process_static_props(static_prop_lump)

        # 10. Export Materials Index
        materials_index_path = self.material_mapper.write_materials_index()

        # 11. Aggregate warnings, fallbacks, missing assets, and errors
        self._collect_audit_diagnostics()

        # 12. Write Audio Mapping Document
        if self.options.export_audio:
            audio_doc_path = self.export_root / "docs" / "AUDIO_MAPPING.md"
            audio_doc_path.parent.mkdir(parents=True, exist_ok=True)
            self.audio_exporter.generate_mapping_markdown(audio_doc_path)

        # 13. Write Manifest & Reports
        manifest_path = self.manifest_builder.write_manifest()
        report_json, report_md = self.manifest_builder.write_reports()

        try:
            file_buf.close()
        except Exception:
            pass

        logger.info(f"Pipeline complete! Manifest: {manifest_path}, Report: {report_md}")

        return {
            "status": "ok",
            "manifest_path": str(manifest_path),
            "materials_index_path": str(materials_index_path),
            "report_md_path": str(report_md),
            "report_json_path": str(report_json),
            "total_actors": len(self.manifest_builder.placed_actors),
            "exported_meshes": len(self.mesh_exporter.exported_meshes),
            "exported_textures": len(self.texture_converter.converted_cache),
            "exported_materials": len(self.material_mapper.material_cache),
            "exported_audio": len(self.audio_exporter.converted_audio),
            "gameplay_entities": len(self.manifest_builder.gameplay_entities),
            "missing_assets_count": len(self.manifest_builder.missing_assets),
        }

    def _ensure_blender_scene_loaded(self, bsp: VBSPFile, bsp_tiny: TinyPath, file_buf: FileBuffer) -> Optional[Any]:
        """
        Return the Blender collection for the map; if not already imported in Blender,
        run SourceIO's BSP importer with fast settings (geometry + entities, materials handled directly).
        """
        existing = bpy.data.collections.get(self.options.bsp_path.name)
        if existing is not None and self.options.use_active_blender_scene:
            logger.info(f"Using existing imported Blender collection '{existing.name}'.")
            return existing

        if not hasattr(bpy.context.scene, "sourceio_props"):
            from SourceIO.blender_bindings.bindings import register as register_sourceio
            register_sourceio()

        from SourceIO.blender_bindings.operators.import_settings_base import Source1BSPSettings
        from SourceIO.blender_bindings.source1.bsp.import_bsp import (
            import_disp,
            import_entities,
            import_static_props,
        )

        # Build lightweight settings namespace compatible with import_bsp
        class _HeadlessBSPSettings:
            scale = self.blender_scale
            light_scale = 1.0
            use_bvlg = False
            import_textures = False  # Textures & materials are converted directly via UEFNTextureConverter
            import_cubemaps = False
            load_props = True
            load_lights = True
            load_decals = True
            load_static_props = True
            load_triggers = True
            load_info = True
            load_logic = False
            load_ropes = False

        settings = _HeadlessBSPSettings()
        master_collection = bpy.data.collections.new(self.options.bsp_path.name)
        bpy.context.scene.collection.children.link(master_collection)

        logger.info(f"Importing BSP geometry into Blender for UEFN export: {bsp_tiny}")
        import_entities(bsp, self.content_manager, settings, master_collection, logger)
        import_static_props(bsp, settings, master_collection, logger)
        import_disp(bsp, settings, master_collection, logger)
        return master_collection

    def _detect_sky_camera(self, entities: list[dict[str, Any]]) -> None:
        for ent in entities:
            if ent.get("classname", "").lower() == "sky_camera":
                origin = parse_vector_string(ent.get("origin", "0 0 0"))
                try:
                    sky_scale = float(ent.get("scale", 16.0))
                except (TypeError, ValueError):
                    sky_scale = 16.0
                self._sky_camera_origin_hu = origin
                self._sky_camera_scale = sky_scale
                self.manifest_builder.sky_camera_info = {
                    "origin_hu": origin,
                    "unreal_location": source_to_unreal_position(
                        origin, self.options.cm_per_hu, self.options.scale_multiplier
                    ),
                    "sky_scale": sky_scale,
                    "fog_color": ent.get("fogcolor", ""),
                    "fog_start": ent.get("fogstart", ""),
                    "fog_end": ent.get("fogend", ""),
                    "note": (
                        "3D skybox meshes/props are tagged is_skybox=true. To display around the "
                        "level at 1:1 scale, scale actors by sky_scale and offset from sky_camera origin."
                    ),
                }
                break

    def _is_point_in_skybox(self, origin_hu: list[float]) -> bool:
        """Heuristic: sky_camera sits in an isolated skybox room far from playable world center."""
        if self._sky_camera_origin_hu is None:
            return False
        dx = origin_hu[0] - self._sky_camera_origin_hu[0]
        dy = origin_hu[1] - self._sky_camera_origin_hu[1]
        dz = origin_hu[2] - self._sky_camera_origin_hu[2]
        dist_to_sky = (dx * dx + dy * dy + dz * dz) ** 0.5
        dist_to_world_origin = (origin_hu[0] ** 2 + origin_hu[1] ** 2 + origin_hu[2] ** 2) ** 0.5
        # Skybox room is typically a small sealed box (< 1800 HU radius) centered on sky_camera
        return dist_to_sky < 1800.0 and dist_to_sky < dist_to_world_origin

    def _skybox_pivot_and_location(self) -> Optional[tuple[list[float], list[float]]]:
        """Return (pivot_offset_bu, unreal_location_cm) when a 3D sky_camera is present."""
        if self._sky_camera_origin_hu is None:
            return None
        pivot_bu = [float(c) * self.blender_scale for c in self._sky_camera_origin_hu]
        loc_cm = source_to_unreal_position(
            self._sky_camera_origin_hu,
            cm_per_hu=self.options.cm_per_hu,
            scale_multiplier=self.options.scale_multiplier,
        )
        return pivot_bu, loc_cm

    def _export_world_and_displacements(self, map_collection: Any) -> None:
        sky_info = self._skybox_pivot_and_location()
        playable_filter = (lambda pt: not self._is_point_in_skybox(pt)) if sky_info else None

        world_objs = [
            o for o in map_collection.objects
            if o.type == "MESH" and "world_geometry" in o.name.lower()
        ]
        if world_objs:
            self.manifest_builder.expected_counts["world_chunks"] += len(world_objs)
            chunks = self.mesh_exporter.export_spatial_world_chunks(
                world_objs,
                prefix="world_chunk",
                on_material_seen=self._on_material_seen,
                face_filter=playable_filter,
            )
            for chunk in chunks:
                self.manifest_builder.exported_counts["world_chunks"] += 1
                self.manifest_builder.add_actor(PlacedActorRecord(
                    actor_name=chunk.mesh_name,
                    asset_reference=chunk.fbx_path.name,
                    location=[0.0, 0.0, 0.0],
                    rotation=[0.0, 0.0, 0.0],
                    scale=[1.0, 1.0, 1.0],
                    entity_class="worldspawn",
                    material_overrides=chunk.materials,
                    source_entity_data={"triangle_count": chunk.triangle_count, "chunk": True},
                ))
            if sky_info:
                sky_pivot_bu, sky_loc_cm = sky_info
                sky_chunks = self.mesh_exporter.export_spatial_world_chunks(
                    world_objs,
                    prefix="skybox_world_chunk",
                    on_material_seen=self._on_material_seen,
                    face_filter=self._is_point_in_skybox,
                    pivot_offset_bu=sky_pivot_bu,
                )
                for chunk in sky_chunks:
                    self.manifest_builder.exported_counts["world_chunks"] += 1
                    self.manifest_builder.add_actor(PlacedActorRecord(
                        actor_name=chunk.mesh_name,
                        asset_reference=chunk.fbx_path.name,
                        location=list(sky_loc_cm),
                        rotation=[0.0, 0.0, 0.0],
                        scale=[1.0, 1.0, 1.0],
                        entity_class="worldspawn",
                        material_overrides=chunk.materials,
                        source_entity_data={"triangle_count": chunk.triangle_count, "chunk": True},
                        is_skybox=True,
                    ))

        # Displacements
        disp_col = map_collection.children.get("displacements")
        if disp_col:
            disp_objs = [o for o in disp_col.objects if o.type == "MESH"]
            self.manifest_builder.expected_counts["displacements"] += len(disp_objs)
            if disp_objs:
                disp_chunks = self.mesh_exporter.export_spatial_world_chunks(
                    disp_objs,
                    prefix="disp_chunk",
                    on_material_seen=self._on_material_seen,
                    face_filter=playable_filter,
                )
                for chunk in disp_chunks:
                    self.manifest_builder.exported_counts["displacements"] += 1
                    self.manifest_builder.add_actor(PlacedActorRecord(
                        actor_name=chunk.mesh_name,
                        asset_reference=chunk.fbx_path.name,
                        location=[0.0, 0.0, 0.0],
                        rotation=[0.0, 0.0, 0.0],
                        scale=[1.0, 1.0, 1.0],
                        entity_class="displacement",
                        material_overrides=chunk.materials,
                        source_entity_data={"triangle_count": chunk.triangle_count, "displacement": True},
                    ))
                if sky_info:
                    sky_pivot_bu, sky_loc_cm = sky_info
                    sky_disp_chunks = self.mesh_exporter.export_spatial_world_chunks(
                        disp_objs,
                        prefix="skybox_disp_chunk",
                        on_material_seen=self._on_material_seen,
                        face_filter=self._is_point_in_skybox,
                        pivot_offset_bu=sky_pivot_bu,
                    )
                    for chunk in sky_disp_chunks:
                        self.manifest_builder.exported_counts["displacements"] += 1
                        self.manifest_builder.add_actor(PlacedActorRecord(
                            actor_name=chunk.mesh_name,
                            asset_reference=chunk.fbx_path.name,
                            location=list(sky_loc_cm),
                            rotation=[0.0, 0.0, 0.0],
                            scale=[1.0, 1.0, 1.0],
                            entity_class="displacement",
                            material_overrides=chunk.materials,
                            source_entity_data={"triangle_count": chunk.triangle_count, "displacement": True},
                            is_skybox=True,
                        ))

    def _export_brush_entities(self, map_collection: Any) -> None:
        """
        Walk 'brushes' and 'triggers' collections in the imported BSP hierarchy.
        Exports visible brush models to FBX and records invisible triggers' bounding boxes.
        """
        for sub_name in ("brushes", "triggers"):
            sub_col = map_collection.children.get(sub_name)
            if not sub_col:
                continue
            for type_col in list(sub_col.children) + [sub_col]:
                for obj in type_col.objects:
                    if obj.type != "MESH":
                        continue
                    ent_data = dict(obj.get("entity_data", {}).get("entity", {}))
                    classname = ent_data.get("classname", type_col.name).lower()
                    targetname = ent_data.get("targetname") or obj.name

                    # Compute world-space bounding box in Unreal cm
                    bbox_corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
                    ue_corners = [
                        blender_to_unreal_position(
                            [c.x, c.y, c.z],
                            blender_scale=self.blender_scale,
                            cm_per_hu=self.options.cm_per_hu,
                            scale_multiplier=self.options.scale_multiplier,
                        )
                        for c in bbox_corners
                    ]
                    xs = [c[0] for c in ue_corners]
                    ys = [c[1] for c in ue_corners]
                    zs = [c[2] for c in ue_corners]
                    bounds_cm = {
                        "center": [
                            round((min(xs) + max(xs)) * 0.5, 2),
                            round((min(ys) + max(ys)) * 0.5, 2),
                            round((min(zs) + max(zs)) * 0.5, 2),
                        ],
                        "extent": [
                            round((max(xs) - min(xs)) * 0.5, 2),
                            round((max(ys) - min(ys)) * 0.5, 2),
                            round((max(zs) - min(zs)) * 0.5, 2),
                        ],
                    }

                    # If this is an invisible trigger/logic volume, record bounds into gameplay_entities
                    if classname in INVISIBLE_BRUSH_ENTITIES:
                        self.manifest_builder.expected_counts["gameplay_entities"] += 1
                        self.manifest_builder.gameplay_entities.append({
                            "classname": classname,
                            "targetname": targetname,
                            "location": bounds_cm["center"],
                            "rotation": [0.0, 0.0, 0.0],
                            "bounds_cm": bounds_cm,
                            "keyvalues": ent_data,
                        })
                        self.manifest_builder.exported_counts["gameplay_entities"] += 1
                        self.manifest_builder.add_actor(PlacedActorRecord(
                            actor_name=f"TF2_{sanitize_ue_name(targetname)}_{ent_data.get('hammerid', 'vol')}",
                            asset_reference=None,
                            location=bounds_cm["center"],
                            rotation=[0.0, 0.0, 0.0],
                            scale=[1.0, 1.0, 1.0],
                            entity_class=classname,
                            source_entity_data=ent_data,
                            is_gameplay_entity=True,
                            bounds_cm=bounds_cm,
                        ))
                        continue

                    self.manifest_builder.expected_counts["brush_entities"] += 1
                    mesh_info = self.mesh_exporter.export_brush_entity(
                        f"{classname}_{ent_data.get('hammerid', obj.name)}",
                        obj,
                        on_material_seen=self._on_material_seen,
                    )
                    if mesh_info is None:
                        # All faces were tool faces (nodraw/clip); skip visible mesh cleanly
                        self.manifest_builder.exported_counts["brush_entities"] += 1
                        continue

                    ue_loc = blender_to_unreal_position(
                        [obj.location.x, obj.location.y, obj.location.z],
                        blender_scale=self.blender_scale,
                        cm_per_hu=self.options.cm_per_hu,
                        scale_multiplier=self.options.scale_multiplier,
                    )
                    ue_rot = blender_euler_to_unreal_rotation(obj.rotation_euler)
                    factor = self.options.cm_per_hu * self.options.scale_multiplier
                    center_cm = bounds_cm["center"]
                    center_hu = [center_cm[0] / factor, -center_cm[1] / factor, center_cm[2] / factor]
                    is_sky_brush = self._is_point_in_skybox(center_hu)

                    self.manifest_builder.exported_counts["brush_entities"] += 1
                    self.manifest_builder.add_actor(PlacedActorRecord(
                        actor_name=f"Brush_{mesh_info.mesh_name}_{ent_data.get('hammerid', '0')}",
                        asset_reference=mesh_info.fbx_path.name,
                        location=ue_loc,
                        rotation=ue_rot,
                        scale=[1.0, 1.0, 1.0],
                        entity_class=classname,
                        material_overrides=mesh_info.materials,
                        source_entity_data=ent_data,
                        is_gameplay_entity=(classname in GAMEPLAY_ENTITY_CLASSES),
                        is_skybox=is_sky_brush,
                        bounds_cm=bounds_cm,
                    ))

    def _export_overlays(self, bsp: VBSPFile) -> None:
        """
        Extract LUMP_OVERLAYS from BSP, resolve their VMTs, and export a combined overlay quad mesh
        if running inside Blender.
        """
        overlay_lump: Optional[OverlayLump] = bsp.get_lump("LUMP_OVERLAYS")
        tex_info_lump: Optional[TextureInfoLump] = bsp.get_lump("LUMP_TEXINFO")
        tex_data_lump: Optional[TextureDataLump] = bsp.get_lump("LUMP_TEXDATA")
        strings_lump: Optional[StringsLump] = bsp.get_lump("LUMP_TEXDATA_STRING_TABLE")

        if not overlay_lump or not overlay_lump.overlays:
            return
        if not tex_info_lump or not tex_data_lump or not strings_lump:
            return

        self.manifest_builder.expected_counts["overlays_decals"] += len(overlay_lump.overlays)

        if not IN_BLENDER:
            # Map overlay materials even outside Blender
            for ov in overlay_lump.overlays:
                if 0 <= ov.tex_info < len(tex_info_lump.texture_info):
                    ti = tex_info_lump.texture_info[ov.tex_info]
                    td = tex_data_lump.texture_data[ti.texture_data_id]
                    mat_name = strings_lump.strings[td.name_id] or ""
                    self._on_material_seen(mat_name)
            return

        # Build Blender quad mesh per material or unified overlay mesh slightly offset along normal
        verts: list[tuple[float, float, float]] = []
        faces: list[tuple[int, int, int, int]] = []
        uvs: list[tuple[float, float]] = []
        mat_indices: list[int] = []
        mat_map: dict[str, int] = {}
        temp_mats: list[Any] = []

        for ov in overlay_lump.overlays:
            if not (0 <= ov.tex_info < len(tex_info_lump.texture_info)):
                continue
            ti = tex_info_lump.texture_info[ov.tex_info]
            td = tex_data_lump.texture_data[ti.texture_data_id]
            mat_name = (strings_lump.strings[td.name_id] or "").lstrip("/\\")
            if not mat_name or is_tool_material(mat_name):
                continue

            self._on_material_seen(mat_name)
            if mat_name not in mat_map:
                mat_map[mat_name] = len(temp_mats)
                bmat = bpy.data.materials.get(f"__ov_{len(temp_mats)}") or bpy.data.materials.new(f"__ov_{len(temp_mats)}")
                bmat["full_path"] = mat_name
                temp_mats.append(bmat)

            dst_pos, dst_uv = ov.plane
            # Offset by 0.25 HU along normal to avoid z-fighting against brush faces
            offset = np.asarray(ov.normal, dtype=np.float32) * 0.25
            base_idx = len(verts)
            for p_idx in range(4):
                pos_m = (dst_pos[p_idx] + offset) * self.blender_scale
                verts.append((float(pos_m[0]), float(pos_m[1]), float(pos_m[2])))
                uvs.append((float(dst_uv[p_idx][0]), 1.0 - float(dst_uv[p_idx][1])))
            faces.append((base_idx, base_idx + 1, base_idx + 2, base_idx + 3))
            mat_indices.append(mat_map[mat_name])

        if not faces:
            return

        ov_mesh = bpy.data.meshes.new(f"{self.map_name}_overlays_MESH")
        ov_mesh.from_pydata(verts, [], faces)
        for m in temp_mats:
            ov_mesh.materials.append(m)
        ov_mesh.polygons.foreach_set("material_index", mat_indices)
        uv_layer = ov_mesh.uv_layers.new()
        loop_uvs = []
        for f_idx in range(len(faces)):
            for corner in faces[f_idx]:
                loop_uvs.extend(uvs[corner])
        uv_layer.data.foreach_set("uv", loop_uvs)

        ov_obj = bpy.data.objects.new(f"{self.map_name}_overlays", ov_mesh)
        bpy.context.scene.collection.objects.link(ov_obj)

        sky_info = self._skybox_pivot_and_location()
        playable_filter = (lambda pt: not self._is_point_in_skybox(pt)) if sky_info else None

        chunks = self.mesh_exporter.export_spatial_world_chunks(
            [ov_obj],
            prefix="overlays_chunk",
            on_material_seen=self._on_material_seen,
            face_filter=playable_filter,
        )
        sky_chunks = []
        if sky_info:
            sky_pivot_bu, sky_loc_cm = sky_info
            sky_chunks = self.mesh_exporter.export_spatial_world_chunks(
                [ov_obj],
                prefix="skybox_overlays_chunk",
                on_material_seen=self._on_material_seen,
                face_filter=self._is_point_in_skybox,
                pivot_offset_bu=sky_pivot_bu,
            )
        bpy.data.objects.remove(ov_obj, do_unlink=True)
        bpy.data.meshes.remove(ov_mesh)

        for chunk in chunks:
            self.manifest_builder.add_actor(PlacedActorRecord(
                actor_name=chunk.mesh_name,
                asset_reference=chunk.fbx_path.name,
                location=[0.0, 0.0, 0.0],
                rotation=[0.0, 0.0, 0.0],
                scale=[1.0, 1.0, 1.0],
                entity_class="info_overlay",
                material_overrides=chunk.materials,
                source_entity_data={"overlays": True},
            ))
        if sky_info:
            _, sky_loc_cm = sky_info
            for chunk in sky_chunks:
                self.manifest_builder.add_actor(PlacedActorRecord(
                    actor_name=chunk.mesh_name,
                    asset_reference=chunk.fbx_path.name,
                    location=list(sky_loc_cm),
                    rotation=[0.0, 0.0, 0.0],
                    scale=[1.0, 1.0, 1.0],
                    entity_class="info_overlay",
                    material_overrides=chunk.materials,
                    source_entity_data={"overlays": True},
                    is_skybox=True,
                ))
        self.manifest_builder.exported_counts["overlays_decals"] += len(overlay_lump.overlays)

    def _ensure_mdl_exported(self, model_path: str, bodygroup_val: int = 0) -> tuple[Optional[str], list[list[str]]]:
        """
        Ensure a unique MDL model has its FBX exported and all skin materials mapped.
        Returns (fbx_filename, list_of_skin_ue_material_lists).
        """
        clean_model = model_path.strip().replace("\\", "/").lower()
        if not clean_model:
            return None, []

        cache_key = f"{clean_model}:bg={bodygroup_val}"
        safe_mesh_name = sanitize_ue_name(clean_model, prefix="SM_")

        if cache_key in self._mdl_skin_cache:
            return f"{safe_mesh_name}.fbx", self._mdl_skin_cache[cache_key]

        if clean_model in self._missing_models:
            return None, []

        mdl_buf = self.content_manager.find_file(TinyPath(clean_model))
        if not mdl_buf:
            if clean_model not in self._missing_models:
                self._missing_models.add(clean_model)
                self.manifest_builder.record_missing_asset("Model", clean_model, "MDL file not found in PakFile or VPKs")
            return None, []

        # Resolve skin groups directly from MDL / SourceIO
        skin_groups_ue: list[list[str]] = []
        if not IN_BLENDER:
            return f"{safe_mesh_name}.fbx", skin_groups_ue

        from SourceIO.blender_bindings.models import import_model
        from SourceIO.blender_bindings.models.materials import get_model_material_names
        from SourceIO.blender_bindings.operators.import_settings_base import ModelOptions

        options = ModelOptions.default()
        options.scale = self.blender_scale
        options.import_textures = False
        options.import_physics = False
        options.create_flex_drivers = False
        options.load_refpose = False
        options.bodygroup_grouping = False
        options.import_animations = False
        options.bodygroup_vis_switches = False

        try:
            steam_id = self.content_manager.get_steamid_from_asset(TinyPath(clean_model))
            container = import_model(TinyPath(clean_model), mdl_buf, self.content_manager, options, steam_id)
        except Exception as e:
            self.manifest_builder.record_missing_asset("Model", clean_model, f"MDL load error: {e}")
            self._missing_models.add(clean_model)
            return None, []

        if container is None or not container.objects:
            self.manifest_builder.record_missing_asset("Model", clean_model, "MDL yielded empty ModelContainer")
            self._missing_models.add(clean_model)
            return None, []

        # Map all skins from mdl.skin_groups if available
        if getattr(container, "mdl", None) is not None and hasattr(container.mdl, "skin_groups"):
            try:
                full_mat_names = get_model_material_names(self.content_manager, container.mdl)
                for group in container.mdl.skin_groups:
                    skin_ue: list[str] = []
                    for mat_entry in group:
                        mat_full = full_mat_names.get(mat_entry.name, mat_entry.name)
                        self._on_material_seen(mat_full)
                        skin_ue.append(material_ue_name(mat_full))
                    skin_groups_ue.append(skin_ue)
            except Exception:
                pass

        # Export unique prop FBX
        mesh_info = self.mesh_exporter.export_unique_prop(
            clean_model,
            container.objects,
            on_material_seen=self._on_material_seen,
        )

        # Clean up temporary imported Blender objects/meshes/armatures
        for obj in list(container.objects) + list(getattr(container, "physics_objects", [])):
            if obj and obj.name in bpy.data.objects:
                mesh_data = obj.data if obj.type == "MESH" else None
                bpy.data.objects.remove(obj, do_unlink=True)
                if mesh_data and mesh_data.users == 0:
                    bpy.data.meshes.remove(mesh_data)
        if getattr(container, "armature", None) is not None:
            arm = container.armature
            if arm.name in bpy.data.objects:
                arm_data = arm.data
                bpy.data.objects.remove(arm, do_unlink=True)
                if arm_data and arm_data.users == 0:
                    bpy.data.armatures.remove(arm_data)

        if mesh_info is None:
            return None, skin_groups_ue

        self._mdl_skin_cache[cache_key] = skin_groups_ue
        self.manifest_builder.exported_counts["unique_models"] = len(self._mdl_skin_cache)
        return mesh_info.fbx_path.name, skin_groups_ue

    def _process_entities(self, entities: list[dict[str, Any]]) -> None:
        logger.info(f"Processing {len(entities)} map entities...")

        for n, ent in enumerate(entities):
            classname = ent.get("classname", "").lower()
            targetname = ent.get("targetname") or f"{classname}_{ent.get('hammerid', n)}"

            # 1. Gameplay & Logic Entities (avoid double-recording invisible brush triggers handled above)
            if classname in GAMEPLAY_ENTITY_CLASSES and classname not in INVISIBLE_BRUSH_ENTITIES:
                self.manifest_builder.expected_counts["gameplay_entities"] += 1
                origin_raw = parse_vector_string(ent.get("origin", "0 0 0"))
                angles_raw = parse_vector_string(ent.get("angles", "0 0 0"))

                ue_loc = source_to_unreal_position(
                    origin_raw, cm_per_hu=self.options.cm_per_hu, scale_multiplier=self.options.scale_multiplier
                )
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

                self.manifest_builder.add_actor(PlacedActorRecord(
                    actor_name=f"TF2_{sanitize_ue_name(targetname)}_{n}",
                    asset_reference=None,
                    location=ue_loc,
                    rotation=ue_rot,
                    scale=[1.0, 1.0, 1.0],
                    entity_class=classname,
                    source_entity_data=ent,
                    is_gameplay_entity=True,
                ))

            # 2. Door Sound Extraction (non-blocking)
            if classname in ("func_door", "func_door_rotating") and self.options.export_audio:
                for snd_key in ("noise1", "noise2", "startclosesound", "closesound", "locked_sound", "unlocked_sound"):
                    snd_val = ent.get(snd_key, "")
                    if snd_val:
                        self.audio_exporter.extract_and_convert_sound(snd_val)

            # 3. Ambient Audio & Soundscape Entities
            elif classname in ("ambient_generic", "env_soundscape"):
                self.manifest_builder.expected_counts["audio_entities"] += 1
                audio_spec = self.audio_exporter.process_ambient_entity(ent)
                self.manifest_builder.exported_counts["audio_entities"] += 1

                self.manifest_builder.add_actor(PlacedActorRecord(
                    actor_name=f"Audio_{sanitize_ue_name(audio_spec['entity_name'])}_{n}",
                    asset_reference=audio_spec["converted_asset"],
                    location=audio_spec["location"],
                    rotation=[0.0, 0.0, 0.0],
                    scale=[1.0, 1.0, 1.0],
                    entity_class=classname,
                    source_entity_data=ent,
                    audio_spec=audio_spec,
                ))

            # 4. Infodecal Entities
            elif classname == "infodecal":
                self.manifest_builder.expected_counts["overlays_decals"] += 1
                tex_mat = ent.get("texture", "")
                if tex_mat:
                    self._on_material_seen(tex_mat)
                    ue_mat = material_ue_name(tex_mat)
                else:
                    ue_mat = None
                origin_raw = parse_vector_string(ent.get("origin", "0 0 0"))
                ue_loc = source_to_unreal_position(
                    origin_raw, cm_per_hu=self.options.cm_per_hu, scale_multiplier=self.options.scale_multiplier
                )
                self.manifest_builder.add_actor(PlacedActorRecord(
                    actor_name=f"Decal_{ent.get('hammerid', n)}",
                    asset_reference=None,
                    location=ue_loc,
                    rotation=[0.0, 0.0, 0.0],
                    scale=[1.0, 1.0, 1.0],
                    entity_class="infodecal",
                    material_overrides=[ue_mat] if ue_mat else [],
                    source_entity_data=ent,
                ))
                self.manifest_builder.exported_counts["overlays_decals"] += 1

            # 5. Dynamic / Physics Props
            elif classname in PROP_ENTITY_CLASSES:
                self.manifest_builder.expected_counts["dynamic_props"] += 1
                model_path = ent.get("model", "")
                if not model_path:
                    continue

                try:
                    skin_idx = int(ent.get("skin", 0))
                except (TypeError, ValueError):
                    skin_idx = 0
                try:
                    bodygroup = int(ent.get("body", 0))
                except (TypeError, ValueError):
                    bodygroup = 0

                asset_ref, skin_groups = self._ensure_mdl_exported(model_path, bodygroup_val=bodygroup)
                if asset_ref is None:
                    continue

                origin_raw = parse_vector_string(ent.get("origin", "0 0 0"))
                angles_raw = parse_vector_string(ent.get("angles", "0 0 0"))
                try:
                    scale_raw = float(ent.get("modelscale", 1.0))
                except (TypeError, ValueError):
                    scale_raw = 1.0

                is_sky = self._is_point_in_skybox(origin_raw)
                ue_loc = source_to_unreal_position(
                    origin_raw, cm_per_hu=self.options.cm_per_hu, scale_multiplier=self.options.scale_multiplier
                )
                ue_rot = source_to_unreal_rotation(angles_raw)
                ue_scale = source_to_unreal_scale(scale_raw)

                mat_overrides = skin_groups[skin_idx] if 0 <= skin_idx < len(skin_groups) else (skin_groups[0] if skin_groups else [])
                tint = None
                rc = ent.get("rendercolor")
                if rc:
                    rgb = parse_vector_string(rc, (255.0, 255.0, 255.0))
                    tint = [round(c / 255.0, 3) for c in rgb[:3]] + [1.0]

                self.manifest_builder.add_actor(PlacedActorRecord(
                    actor_name=f"DynProp_{sanitize_ue_name(model_path)}_{n}",
                    asset_reference=asset_ref,
                    location=ue_loc,
                    rotation=ue_rot,
                    scale=ue_scale,
                    entity_class=classname,
                    material_overrides=mat_overrides,
                    source_entity_data={**ent, "skin": skin_idx, "bodygroup": bodygroup},
                    tint_color=tint,
                    is_skybox=is_sky,
                ))
                self.manifest_builder.exported_counts["dynamic_props"] += 1

    def _process_static_props(self, static_prop_lump: StaticPropLump) -> None:
        total_props = len(static_prop_lump.static_props)
        unique_models = len(static_prop_lump.model_names)
        logger.info(f"Processing {total_props} static props ({unique_models} unique models)...")

        self.manifest_builder.expected_counts["static_props"] += total_props
        self.manifest_builder.expected_counts["unique_models"] = max(
            self.manifest_builder.expected_counts["unique_models"], unique_models
        )

        # Deduplicate and export each unique model ONCE
        exported_model_map: dict[int, tuple[Optional[str], list[list[str]]]] = {}
        for m_idx, model_name in enumerate(static_prop_lump.model_names):
            exported_model_map[m_idx] = self._ensure_mdl_exported(model_name)

        for n, prop in enumerate(static_prop_lump.static_props):
            model_name = static_prop_lump.model_names[prop.prop_type]
            asset_ref, skin_groups = exported_model_map.get(prop.prop_type, (None, []))
            if asset_ref is None:
                continue

            is_sky = self._is_point_in_skybox(prop.origin)
            ue_loc = source_to_unreal_position(
                prop.origin, cm_per_hu=self.options.cm_per_hu, scale_multiplier=self.options.scale_multiplier
            )
            ue_rot = source_to_unreal_rotation(prop.rotation)
            ue_scale = source_to_unreal_scale(prop.scaling)

            skin_idx = int(getattr(prop, "skin", 0))
            mat_overrides = skin_groups[skin_idx] if 0 <= skin_idx < len(skin_groups) else (skin_groups[0] if skin_groups else [])

            tint = None
            if prop.diffuse_modulation:
                tint = [round(c / 255.0, 3) for c in prop.diffuse_modulation[:3]] + [1.0]

            fade_info = {
                "fade_min_dist": round(prop.fade_min_dist * self.options.cm_per_hu * self.options.scale_multiplier, 2)
                if hasattr(prop, "fade_min_dist") else 0.0,
                "fade_max_dist": round(prop.fade_max_dist * self.options.cm_per_hu * self.options.scale_multiplier, 2)
                if hasattr(prop, "fade_max_dist") else 0.0,
            }

            self.manifest_builder.add_actor(PlacedActorRecord(
                actor_name=f"StaticProp_{sanitize_ue_name(model_name)}_{n}",
                asset_reference=asset_ref,
                location=ue_loc,
                rotation=ue_rot,
                scale=ue_scale,
                entity_class="prop_static",
                material_overrides=mat_overrides,
                source_entity_data={
                    "model": model_name,
                    "skin": skin_idx,
                    "solid": prop.solid if hasattr(prop, "solid") else 6,
                },
                tint_color=tint,
                fade_distances=fade_info,
                is_skybox=is_sky,
            ))
            self.manifest_builder.exported_counts["static_props"] += 1

    def _collect_audit_diagnostics(self) -> None:
        for w in self.texture_converter.warnings:
            self.manifest_builder.record_budget_warning(w)
        for w in self.mesh_exporter.warnings:
            self.manifest_builder.record_budget_warning(w)
        for w in self.audio_exporter.warnings:
            self.manifest_builder.record_budget_warning(w)
        for e in self.texture_converter.errors:
            self.manifest_builder.record_hard_error(e)
        for e in self.mesh_exporter.errors:
            self.manifest_builder.record_hard_error(e)
        for e in self.audio_exporter.errors:
            self.manifest_builder.record_hard_error(e)

        for miss_mat in self.material_mapper.missing_materials:
            self.manifest_builder.record_missing_asset("Material", miss_mat, "VMT file not found")
        for miss_tex in self.texture_converter.missing_textures:
            self.manifest_builder.record_missing_asset("Texture", miss_tex, "VTF file not found")
        for miss_snd in self.audio_exporter.missing_audio:
            self.manifest_builder.record_missing_asset("Audio", miss_snd, "Sound wave not found in PakFile/VPKs")
        for fb in self.material_mapper.fallback_materials:
            self.manifest_builder.record_fallback(
                "Material", fb["asset"], fb["fallback"], fb["reason"]
            )

        self.manifest_builder.non_map_assets_summary["audio_converted"] = len(self.audio_exporter.converted_audio)
        self.manifest_builder.non_map_assets_summary["audio_missing"] = len(self.audio_exporter.missing_audio)
