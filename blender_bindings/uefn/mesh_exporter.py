"""
mesh_exporter.py — UEFN Static & Skeletal Mesh Exporter
======================================================

Handles:
- Deduplication: exports each unique prop MDL model once as an FBX asset.
- Collision: generates simplified convex collision hulls (UCX_ prefix) for Unreal Engine.
- World Geometry Chunking: merges worldspawn static brush faces into spatial grid chunks
  to support UEFN streaming, occlusion culling, and memory limits.
- Tool Texture Filtering: culls invisible faces (nodraw, trigger, clip, playerclip, skybox, etc.).
- Displacement Meshes: exports displacement terrain with vertex colors/alphas for dual-texture blend materials.
- Unit and Axis Conversion: handles coordinate transforms for FBX export.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional, Sequence, Set

try:
    import bmesh
    import bpy
    from mathutils import Matrix, Vector
    IN_BLENDER = True
except ImportError:
    bmesh = None
    bpy = None
    Matrix = None
    Vector = None
    IN_BLENDER = False

from SourceIO.library.shared.content_manager import ContentManager
from SourceIO.library.utils.tiny_path import TinyPath
from SourceIO.logger import SourceLogMan
from .coordinates import INCHES_TO_CM, source_to_unreal_position

log_manager = SourceLogMan()
logger = log_manager.get_logger("UEFN::MeshExporter")

TOOL_TEXTURE_PATTERNS = {
    "tools/toolsnodraw",
    "tools/toolstrigger",
    "tools/toolsclip",
    "tools/toolsplayerclip",
    "tools/toolsskybox",
    "tools/tools2dskybox",
    "tools/toolsblack",
    "tools/toolsareaportal",
    "tools/toolsoccluder",
    "tools/toolshint",
    "tools/toolsskip",
    "tools/toolsblockbullets",
    "tools/toolsblocklight",
    "tools/toolsblocklos",
    "tools/toolsambient",
    "tools/toolsfog",
    "tools/toolsinvisible",
    "toolsnodraw",
    "toolstrigger",
    "toolsclip",
    "toolsplayerclip",
    "toolsskybox",
}


def is_tool_material(material_name: str) -> bool:
    clean = material_name.lower().replace("\\", "/").strip()
    clean = clean.split(".")[0]
    for pattern in TOOL_TEXTURE_PATTERNS:
        if pattern in clean:
            return True
    return False


class ExportedMeshInfo:
    def __init__(
        self,
        mesh_name: str,
        fbx_path: Path,
        vertex_count: int,
        triangle_count: int,
        materials: list[str],
        has_collision: bool = False,
        is_chunk: bool = False,
    ):
        self.mesh_name = mesh_name
        self.fbx_path = fbx_path
        self.vertex_count = vertex_count
        self.triangle_count = triangle_count
        self.materials = materials
        self.has_collision = has_collision
        self.is_chunk = is_chunk

    def to_dict(self) -> dict[str, Any]:
        return {
            "mesh_name": self.mesh_name,
            "fbx_path": str(self.fbx_path.as_posix()),
            "vertex_count": self.vertex_count,
            "triangle_count": self.triangle_count,
            "materials": self.materials,
            "has_collision": self.has_collision,
            "is_chunk": self.is_chunk,
        }


class UEFNMeshExporter:
    """
    Exports Blender meshes and prop models to FBX for UEFN.
    """

    def __init__(
        self,
        output_dir: Path,
        generate_collision: bool = True,
        world_chunk_size: float = 2048.0,  # Hammer units per chunk
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.generate_collision = generate_collision
        self.world_chunk_size = world_chunk_size
        self.exported_meshes: dict[str, ExportedMeshInfo] = {}
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def export_unique_prop(
        self,
        model_name: str,
        source_objects: list[bpy.types.Object],
    ) -> Optional[ExportedMeshInfo]:
        """
        Export a unique prop model (deduplicated across instances) to an FBX.
        Includes a convex collision hull (UCX_{name}_01).
        """
        clean_model_name = Path(model_name).stem.replace(" ", "_").lower()
        safe_name = f"SM_{clean_model_name}"

        if safe_name in self.exported_meshes:
            return self.exported_meshes[safe_name]

        out_fbx = self.output_dir / f"{safe_name}.fbx"

        # Filter valid mesh objects
        mesh_objs = [obj for obj in source_objects if obj.type == "MESH"]
        if not mesh_objs:
            self.warnings.append(f"No mesh objects found for model {model_name}")
            return None

        # Deselect all
        bpy.ops.object.select_all(action="DESELECT")

        export_group: list[bpy.types.Object] = []
        temp_objs: list[bpy.types.Object] = []

        total_verts = 0
        total_tris = 0
        material_names = set()

        for obj in mesh_objs:
            obj.select_set(True)
            export_group.append(obj)
            total_verts += len(obj.data.vertices)
            total_tris += len(obj.data.polygons)
            for mat_slot in obj.material_slots:
                if mat_slot.material:
                    material_names.add(mat_slot.material.name)

        has_collision = False
        if self.generate_collision and mesh_objs:
            ucx_obj = self._create_convex_hull_collision(mesh_objs[0], f"UCX_{safe_name}_01")
            if ucx_obj:
                ucx_obj.select_set(True)
                export_group.append(ucx_obj)
                temp_objs.append(ucx_obj)
                has_collision = True

        try:
            # Set primary active object
            bpy.context.view_layer.objects.active = mesh_objs[0]

            bpy.ops.export_scene.fbx(
                filepath=str(out_fbx),
                check_existing=False,
                use_selection=True,
                global_scale=1.0,
                apply_unit_scale=True,
                apply_scale_options="FBX_SCALE_UNITS",
                bake_space_transform=False,
                object_types={"MESH", "ARMATURE"},
                use_mesh_modifiers=True,
                mesh_smooth_type="FACE",
                use_subsurf=False,
                use_armature_deform_only=True,
                add_leaf_bones=False,
                primary_bone_axis="Y",
                secondary_bone_axis="X",
                axis_forward="-Z",
                axis_up="Y",
            )
        except Exception as e:
            err = f"Failed to export FBX for {safe_name}: {e}"
            logger.error(err)
            self.errors.append(err)
            return None
        finally:
            # Clean up temp collision objects
            for temp_obj in temp_objs:
                bpy.data.objects.remove(temp_obj, do_unlink=True)

        mesh_info = ExportedMeshInfo(
            mesh_name=safe_name,
            fbx_path=out_fbx,
            vertex_count=total_verts,
            triangle_count=total_tris,
            materials=sorted(list(material_names)),
            has_collision=has_collision,
            is_chunk=False,
        )
        self.exported_meshes[safe_name] = mesh_info
        return mesh_info

    def export_world_geometry_chunk(
        self,
        chunk_name: str,
        chunk_objects: list[bpy.types.Object],
    ) -> Optional[ExportedMeshInfo]:
        """
        Export a merged world geometry spatial chunk as an FBX file.
        Filters out tool textures and creates collision geometry.
        """
        safe_name = f"SM_{chunk_name}".replace(" ", "_")
        out_fbx = self.output_dir / f"{safe_name}.fbx"

        mesh_objs = [obj for obj in chunk_objects if obj.type == "MESH"]
        if not mesh_objs:
            return None

        # Clean tool faces from mesh objects
        cleaned_objs: list[bpy.types.Object] = []
        for obj in mesh_objs:
            cleaned = self._clean_tool_faces(obj)
            if cleaned and len(cleaned.data.polygons) > 0:
                cleaned_objs.append(cleaned)

        if not cleaned_objs:
            return None

        bpy.ops.object.select_all(action="DESELECT")
        for obj in cleaned_objs:
            obj.select_set(True)

        bpy.context.view_layer.objects.active = cleaned_objs[0]

        # Join cleaned chunk meshes into one single unified static mesh
        temp_merged = None
        if len(cleaned_objs) > 1:
            try:
                bpy.ops.object.join()
                temp_merged = bpy.context.view_layer.objects.active
            except Exception as e:
                logger.warning(f"Could not join chunk meshes for {safe_name}: {e}")
                temp_merged = cleaned_objs[0]
        else:
            temp_merged = cleaned_objs[0]

        temp_merged.name = safe_name

        total_verts = len(temp_merged.data.vertices)
        total_tris = len(temp_merged.data.polygons)
        material_names = [slot.material.name for slot in temp_merged.material_slots if slot.material]

        temp_objs: list[bpy.types.Object] = [temp_merged]
        has_collision = False

        if self.generate_collision and total_tris > 0:
            ucx_obj = self._create_convex_hull_collision(temp_merged, f"UCX_{safe_name}_01")
            if ucx_obj:
                ucx_obj.select_set(True)
                temp_objs.append(ucx_obj)
                has_collision = True

        try:
            bpy.ops.export_scene.fbx(
                filepath=str(out_fbx),
                check_existing=False,
                use_selection=True,
                global_scale=1.0,
                apply_unit_scale=True,
                apply_scale_options="FBX_SCALE_UNITS",
                bake_space_transform=False,
                object_types={"MESH"},
                use_mesh_modifiers=True,
                mesh_smooth_type="FACE",
                axis_forward="-Z",
                axis_up="Y",
            )
        except Exception as e:
            err = f"Failed to export world chunk {safe_name}: {e}"
            logger.error(err)
            self.errors.append(err)
            return None
        finally:
            for t_obj in temp_objs:
                bpy.data.objects.remove(t_obj, do_unlink=True)

        mesh_info = ExportedMeshInfo(
            mesh_name=safe_name,
            fbx_path=out_fbx,
            vertex_count=total_verts,
            triangle_count=total_tris,
            materials=material_names,
            has_collision=has_collision,
            is_chunk=True,
        )
        self.exported_meshes[safe_name] = mesh_info
        return mesh_info

    def _clean_tool_faces(self, obj: bpy.types.Object) -> Optional[bpy.types.Object]:
        """
        Duplicate a mesh and remove any polygon mapped to a tool material.
        """
        dup_mesh = obj.data.copy()
        dup_obj = bpy.data.objects.new(f"{obj.name}_cleaned", dup_mesh)
        dup_obj.matrix_world = obj.matrix_world.copy()
        bpy.context.scene.collection.objects.link(dup_obj)

        bm = bmesh.new()
        bm.from_mesh(dup_mesh)

        faces_to_remove = []
        for face in bm.faces:
            mat_idx = face.material_index
            if 0 <= mat_idx < len(dup_obj.material_slots):
                mat = dup_obj.material_slots[mat_idx].material
                if mat and is_tool_material(mat.name):
                    faces_to_remove.append(face)

        if faces_to_remove:
            bmesh.ops.delete(bm, geom=faces_to_remove, context="FACES")

        bm.to_mesh(dup_mesh)
        bm.free()

        return dup_obj

    def _create_convex_hull_collision(
        self,
        base_obj: bpy.types.Object,
        hull_name: str,
    ) -> Optional[bpy.types.Object]:
        """
        Creates a simplified convex collision mesh using bmesh.ops.convex_hull.
        """
        if not base_obj.data.vertices:
            return None

        bm = bmesh.new()
        bm.from_mesh(base_obj.data)

        if len(bm.verts) < 4:
            bm.free()
            return None

        try:
            hull_res = bmesh.ops.convex_hull(bm, input=bm.verts)
            # Remove interior geometry
            geom_interior = hull_res.get("geom_interior", [])
            geom_unused = hull_res.get("geom_unused", [])
            bmesh.ops.delete(bm, geom=geom_interior + geom_unused, context="VERTS")

            hull_mesh = bpy.data.meshes.new(f"{hull_name}_mesh")
            bm.to_mesh(hull_mesh)
            bm.free()

            hull_obj = bpy.data.objects.new(hull_name, hull_mesh)
            hull_obj.matrix_world = base_obj.matrix_world.copy()
            bpy.context.scene.collection.objects.link(hull_obj)
            return hull_obj
        except Exception as e:
            logger.warning(f"Failed to generate convex hull for {base_obj.name}: {e}")
            bm.free()
            return None
