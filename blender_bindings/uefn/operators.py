"""
operators.py — Blender Operators and UI Panels for UEFN Export
==============================================================

Provides:
- SOURCEIO_OT_ExportUEFN: Operator to export the current imported TF2 scene or BSP to UEFN.
- SOURCEIO_PT_UEFNExport: UI Panel in the 3D View sidebar under 'SourceIO' tab.
- Integration hook into Blender's File > Export menu.
"""

from __future__ import annotations

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
from .export_pipeline import UEFNExportOptions, UEFNExportPipeline, detect_tf2_game_dir

log_manager = SourceLogMan()
logger = log_manager.get_logger("UEFN::BlenderOperator")


def _default_tf2_dir() -> str:
    found = detect_tf2_game_dir()
    return str(found) if found else ""


class SOURCEIO_OT_ExportUEFN(Operator):
    """Export the active imported TF2 map scene (or BSP) to Unreal Editor for Fortnite (UEFN)"""

    bl_idname = "sourceio.export_uefn"
    bl_label = "Export Map for UEFN"
    bl_options = {"REGISTER", "UNDO"}

    directory: StringProperty(
        name="Export Directory",
        description="Target folder where Meshes, Textures, Materials, Audio, and map_manifest.json will be saved",
        subtype="DIR_PATH",
    )

    bsp_filepath: StringProperty(
        name="BSP Map File",
        description="Path to the compiled .bsp file (auto-detected from active scene if left blank)",
        subtype="FILE_PATH",
        default="",
    )

    tf2_path: StringProperty(
        name="TF2 Mod Directory (tf)",
        description="Path to Team Fortress 2 'tf' folder containing gameinfo.txt and VPKs",
        subtype="DIR_PATH",
        default="",
    )

    generate_collision: BoolProperty(
        name="Generate UCX Collision",
        description="Generate simplified convex collision hulls (UCX_) for props and brush entities",
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
        name="World Chunk Size (HU)",
        description="Grid cell size in Hammer units for spatial chunking of static world geometry",
        default=2048.0,
        min=512.0,
        max=8192.0,
    )

    scale_multiplier: FloatProperty(
        name="Scale Multiplier",
        description="Extra multiplier on top of Source 1:1 (1.905 cm/HU). Use 1.2 for Fortnite character clearance.",
        default=1.0,
        min=0.1,
        max=10.0,
    )

    def invoke(self, context, event):
        if not self.tf2_path:
            self.tf2_path = _default_tf2_dir()
        if not self.directory:
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

        tf2_dir = Path(self.tf2_path) if self.tf2_path else detect_tf2_game_dir()

        # Resolve BSP file path: explicit property -> active Blender collection -> tf/maps/
        bsp_path = Path(self.bsp_filepath) if self.bsp_filepath else None
        if not bsp_path or not bsp_path.exists():
            map_name = None
            for col in bpy.data.collections:
                if col.name.endswith(".bsp"):
                    map_name = col.name[:-4]
                    break
            if map_name and tf2_dir and (tf2_dir / "maps" / f"{map_name}.bsp").exists():
                bsp_path = tf2_dir / "maps" / f"{map_name}.bsp"

        if not bsp_path or not bsp_path.exists():
            self.report(
                {"ERROR"},
                "Could not locate .bsp map file. Set 'BSP Map File' or import a BSP first.",
            )
            return {"CANCELLED"}

        options = UEFNExportOptions(
            bsp_path=bsp_path,
            tf2_game_dir=tf2_dir,
            export_root_dir=out_dir,
            generate_collision=self.generate_collision,
            max_texture_size=int(self.max_texture_size),
            world_chunk_size=self.world_chunk_size,
            export_audio=self.export_audio,
            scale_multiplier=self.scale_multiplier,
        )

        pipeline = UEFNExportPipeline(options)
        result = pipeline.execute()

        if result.get("status") == "ok":
            self.report(
                {"INFO"},
                f"Exported {result['total_actors']} actors, {result['exported_meshes']} meshes, "
                f"{result['exported_textures']} textures to {out_dir}",
            )
            return {"FINISHED"}
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
