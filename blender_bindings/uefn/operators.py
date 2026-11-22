"""
operators.py — Blender Operators and UI Panels for UEFN Export
==============================================================

Provides:
- SOURCEIO_OT_ExportUEFN: Operator to export the current imported TF2 scene or BSP to UEFN.
- SOURCEIO_PT_UEFNExport: UI Panel in the 3D View sidebar under 'SourceIO' tab.
- Integration hook into Blender's File > Export menu.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    import bpy
    from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
    from bpy.types import Operator, Panel
    IN_BLENDER = True
except ImportError:
    bpy = None
    BoolProperty = EnumProperty = FloatProperty = StringProperty = lambda *a, **kw: None
    Operator = object
    Panel = object
    IN_BLENDER = False

from SourceIO.logger import SourceLogMan
from .export_pipeline import UEFNExportOptions, UEFNExportPipeline

log_manager = SourceLogMan()
logger = log_manager.get_logger("UEFN::BlenderOperator")


class SOURCEIO_OT_ExportUEFN(Operator):
    """Export the active TF2 map scene to Unreal Editor for Fortnite (UEFN)"""

    bl_idname = "sourceio.export_uefn"
    bl_label = "Export Map for UEFN"
    bl_options = {"REGISTER", "UNDO"}

    directory: StringProperty(
        name="Export Directory",
        description="Target folder where Meshes, Textures, Materials, Audio, and map_manifest.json will be saved",
        subtype="DIR_PATH",
    )

    tf2_path: StringProperty(
        name="TF2 Mod Directory (tf)",
        description="Path to Team Fortress 2 'tf' folder containing gameinfo.txt and VPKs",
        subtype="DIR_PATH",
        default=r"H:\SteamLibrary\steamapps\common\Team Fortress 2\tf",
    )

    generate_collision: BoolProperty(
        name="Generate UCX Collision",
        description="Generate simplified convex collision hulls for all static meshes",
        default=True,
    )

    max_texture_size: EnumProperty(
        name="Max Texture Size",
        description="Maximum texture dimension enforced for UEFN budgets (auto-downscaled)",
        items=[
            ("1024", "1024 x 1024", "Optimized memory tier"),
            ("2048", "2048 x 2048 (Recommended)", "Default UEFN budget"),
            ("4096", "4096 x 4096", "High-res desktop tier"),
        ],
        default="2048",
    )

    export_audio: BoolProperty(
        name="Export Audio & Soundscripts",
        description="Extract and convert map soundscapes and ambient audio to 16-bit PCM WAV",
        default=True,
    )

    world_chunk_size: FloatProperty(
        name="World Chunk Size",
        description="Size in Hammer units for spatial grid chunking of static world geometry",
        default=2048.0,
        min=512.0,
        max=8192.0,
    )

    def invoke(self, context, event):
        if not self.directory:
            # Default export directory adjacent to active blend or in user's UEFN project folder
            blend_dir = Path(bpy.data.filepath).parent if bpy.data.filepath else Path.home()
            self.directory = str(blend_dir / "UEFN_Export")
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if not self.directory:
            self.report({"ERROR"}, "Please select a target export directory.")
            return {"CANCELLED"}

        out_dir = Path(self.directory)
        out_dir.mkdir(parents=True, exist_ok=True)

        tf2_dir = Path(self.tf2_path) if self.tf2_path else None

        # Look for active map name or BSP file in scene properties
        map_name = "ctf_2fort"
        for col in bpy.data.collections:
            if col.name.endswith(".bsp"):
                map_name = col.name[:-4]
                break

        # Check if BSP file is available
        bsp_path = None
        if tf2_dir and (tf2_dir / "maps" / f"{map_name}.bsp").exists():
            bsp_path = tf2_dir / "maps" / f"{map_name}.bsp"
        else:
            # Check default test map
            fallback_bsp = Path(r"H:\SteamLibrary\steamapps\common\Team Fortress 2\tf\maps\ctf_2fort.bsp")
            if fallback_bsp.exists():
                bsp_path = fallback_bsp
                map_name = "ctf_2fort"

        if not bsp_path:
            self.report({"ERROR"}, f"Could not locate BSP map file for {map_name}.")
            return {"CANCELLED"}

        options = UEFNExportOptions(
            bsp_path=bsp_path,
            tf2_game_dir=tf2_dir,
            export_root_dir=out_dir,
            generate_collision=self.generate_collision,
            max_texture_size=int(self.max_texture_size),
            world_chunk_size=self.world_chunk_size,
            export_audio=self.export_audio,
        )

        pipeline = UEFNExportPipeline(options)
        result = pipeline.execute()

        if result.get("status") == "ok":
            self.report(
                {"INFO"},
                f"Exported {result['total_actors']} actors to UEFN! Manifest: {result['manifest_path']}",
            )
            return {"FINISHED"}
        else:
            self.report({"ERROR"}, f"Export failed: {result.get('error')}")
            return {"CANCELLED"}


class SOURCEIO_PT_UEFNExport(Panel):
    """UI Panel for UEFN Exporter in 3D View sidebar"""

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SourceIO"
    bl_label = "UEFN Map Exporter"
    bl_idname = "SOURCEIO_PT_UEFNExport"

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.label(text="Target: Unreal Editor for Fortnite (UEFN)", icon="WORLD")
        col.separator()

        box = layout.box()
        box.label(text="Export Pipeline", icon="EXPORT")
        box.operator(SOURCEIO_OT_ExportUEFN.bl_idname, text="Export Map for UEFN...", icon="FILE_FOLDER")


def menu_export_uefn(self, context):
    self.layout.operator(SOURCEIO_OT_ExportUEFN.bl_idname, text="UEFN Fortnite Map (.json + .fbx)")


classes = [
    SOURCEIO_OT_ExportUEFN,
    SOURCEIO_PT_UEFNExport,
]


def register():
    if not IN_BLENDER:
        return
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_export.append(menu_export_uefn)


def unregister():
    if not IN_BLENDER:
        return
    bpy.types.TOPBAR_MT_file_export.remove(menu_export_uefn)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
