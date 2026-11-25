"""
mesh_exporter.py — UEFN Static & Skeletal Mesh Exporter
======================================================

Handles:
- Deduplication: exports each unique prop MDL model once as a local-origin FBX asset.
- Collision: generates simplified convex collision hulls (UCX_ prefix) across all sub-meshes.
- World Geometry Chunking: splits worldspawn static brush faces and displacements into
  spatial grid chunks to support UEFN streaming, occlusion culling, and memory limits.
- Tool Texture Filtering: culls invisible faces (nodraw, trigger, clip, playerclip, skybox, etc.).
- Sanitized Material Slots: renames FBX material slots on temp export meshes to MI_* names
  so UEFN auto-links Material Instances cleanly.
"""

from __future__ import annotations

from collections import defaultdict
import math
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

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

from SourceIO.library.utils.math_utilities import SOURCE1_HAMMER_UNIT_TO_METERS
from SourceIO.logger import SourceLogMan
from .coordinates import material_ue_name, sanitize_ue_name

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
    "tools2dskybox",
    "toolsinvisible",
}

MAX_NON_NANITE_TRIANGLES_WARN = 25000
MAX_CHUNK_TRIANGLES_WARN = 65000


def is_tool_material(material_name: str) -> bool:
    clean = material_name.lower().replace("\\", "/").strip()
    clean = clean.split(".")[0]
    for pattern in TOOL_TEXTURE_PATTERNS:
        if pattern in clean:
            return True
    return False


def get_material_source_path(mat: Any) -> str:
    """Return the un-truncated Source material path stored by SourceIO, or fallback to mat.name."""
    if mat is None:
        return ""
    fp = mat.get("full_path", None)
    if fp:
        return str(fp)
    return str(mat.name)


def _clean_and_triangulate_bmesh(bm: Any) -> None:
    """Remove loose edges/verts and degenerate triangles so UE5 Interchange never fails MeshDescription."""
    loose_edges = [e for e in bm.edges if not e.link_faces]
    if loose_edges:
        bmesh.ops.delete(bm, geom=loose_edges, context="EDGES")
    loose_verts = [v for v in bm.verts if not v.link_faces]
    if loose_verts:
        bmesh.ops.delete(bm, geom=loose_verts, context="VERTS")
    if bm.faces:
        bmesh.ops.triangulate(bm, faces=bm.faces[:])
        zero_faces = [f for f in bm.faces if f.calc_area() < 1e-10]
        if zero_faces:
            bmesh.ops.delete(bm, geom=zero_faces, context="FACES")
        loose_edges = [e for e in bm.edges if not e.link_faces]
        if loose_edges:
            bmesh.ops.delete(bm, geom=loose_edges, context="EDGES")
        loose_verts = [v for v in bm.verts if not v.link_faces]
        if loose_verts:
            bmesh.ops.delete(bm, geom=loose_verts, context="VERTS")


class ExportedMeshInfo:
    def __init__(
        self,
        mesh_name: str,
        fbx_path: Path,
        vertex_count: int,
        triangle_count: int,
        materials: list[str],
        source_materials: list[str],
        has_collision: bool = False,
        is_chunk: bool = False,
        bounds_min_hu: Optional[list[float]] = None,
        bounds_max_hu: Optional[list[float]] = None,
    ):
        self.mesh_name = mesh_name
        self.fbx_path = fbx_path
        self.vertex_count = vertex_count
        self.triangle_count = triangle_count
        self.materials = materials
        self.source_materials = source_materials
        self.has_collision = has_collision
        self.is_chunk = is_chunk
        self.bounds_min_hu = bounds_min_hu
        self.bounds_max_hu = bounds_max_hu

    def to_dict(self) -> dict[str, Any]:
        return {
            "mesh_name": self.mesh_name,
            "fbx_path": str(self.fbx_path.as_posix()),
            "vertex_count": self.vertex_count,
            "triangle_count": self.triangle_count,
            "materials": self.materials,
            "source_materials": self.source_materials,
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
        world_chunk_size: float = 2048.0,
        blender_scale: float = SOURCE1_HAMMER_UNIT_TO_METERS,
        scale_multiplier: float = 1.0,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.generate_collision = generate_collision
        self.world_chunk_size = world_chunk_size
        self.blender_scale = blender_scale
        self.scale_multiplier = scale_multiplier
        self.exported_meshes: dict[str, ExportedMeshInfo] = {}
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def _run_fbx_export(self, out_fbx: Path, include_armature: bool = False) -> None:
        obj_types = {"MESH", "ARMATURE"} if include_armature else {"MESH"}
        bpy.ops.export_scene.fbx(
            filepath=str(out_fbx),
            check_existing=False,
            use_selection=True,
            global_scale=self.scale_multiplier,
            apply_unit_scale=True,
            apply_scale_options="FBX_SCALE_UNITS",
            bake_space_transform=False,
            object_types=obj_types,
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

    def _remap_temp_material_slots(
        self,
        temp_obj: Any,
        on_material_seen: Optional[Callable[[str], None]] = None,
    ) -> tuple[list[str], list[str]]:
        """
        Ensure material slots on a temporary export object use clean MI_* datablocks
        so FBX material slots match UEFN MaterialInstance names 1:1.
        Returns (ue_material_names, source_material_paths).
        """
        ue_names: list[str] = []
        src_paths: list[str] = []
        for idx, slot in enumerate(temp_obj.material_slots):
            mat = slot.material
            if mat is None:
                continue
            src_path = get_material_source_path(mat)
            if is_tool_material(src_path):
                continue
            if on_material_seen:
                on_material_seen(src_path)
            ue_name = material_ue_name(src_path)
            # Create or reuse an empty proxy material named exactly `ue_name`
            proxy_mat = bpy.data.materials.get(ue_name)
            if proxy_mat is None:
                proxy_mat = bpy.data.materials.new(name=ue_name)
                proxy_mat["uefn_proxy"] = True
            slot.material = proxy_mat
            if ue_name not in ue_names:
                ue_names.append(ue_name)
            if src_path not in src_paths:
                src_paths.append(src_path)
        return ue_names, src_paths

    def export_unique_prop(
        self,
        model_name: str,
        source_objects: list[Any],
        on_material_seen: Optional[Callable[[str], None]] = None,
    ) -> Optional[ExportedMeshInfo]:
        """
        Export a unique prop model (deduplicated across instances) to a local-origin FBX.
        Includes a combined convex collision hull (UCX_{name}_01).
        """
        if not IN_BLENDER:
            self.errors.append(f"Cannot export FBX for {model_name}: Blender (bpy) not available.")
            return None

        safe_name = sanitize_ue_name(model_name, prefix="SM_")
        if safe_name in self.exported_meshes:
            return self.exported_meshes[safe_name]

        out_fbx = self.output_dir / f"{safe_name}.fbx"
        mesh_objs = [obj for obj in source_objects if obj is not None and obj.type == "MESH"]
        if not mesh_objs:
            self.warnings.append(f"No mesh objects found for model {model_name}")
            return None

        temp_objs: list[Any] = []
        temp_meshes: list[Any] = []
        ue_materials: list[str] = []
        src_materials: list[str] = []

        try:
            # Duplicate mesh objects cleanly at local origin (0, 0, 0) with identity transform
            # so placed instances in map_manifest.json are never double-transformed.
            for idx, src_obj in enumerate(mesh_objs):
                dup_mesh = src_obj.data.copy()
                temp_meshes.append(dup_mesh)
                dup_obj = bpy.data.objects.new(f"{safe_name}_part_{idx}" if len(mesh_objs) > 1 else safe_name, dup_mesh)
                dup_obj.location = (0.0, 0.0, 0.0)
                dup_obj.rotation_euler = (0.0, 0.0, 0.0)
                dup_obj.scale = (1.0, 1.0, 1.0)
                bpy.context.scene.collection.objects.link(dup_obj)
                temp_objs.append(dup_obj)

                u_mats, s_mats = self._remap_temp_material_slots(dup_obj, on_material_seen)
                for m in u_mats:
                    if m not in ue_materials:
                        ue_materials.append(m)
                for s in s_mats:
                    if s not in src_materials:
                        src_materials.append(s)

            # Merge copies if prop has multiple sub-meshes so UEFN imports one StaticMesh
            if len(temp_objs) > 1:
                bpy.ops.object.select_all(action="DESELECT")
                for o in temp_objs:
                    o.select_set(True)
                bpy.context.view_layer.objects.active = temp_objs[0]
                bpy.ops.object.join()
                primary_obj = bpy.context.view_layer.objects.active
                temp_objs = [primary_obj]
            else:
                primary_obj = temp_objs[0]

            primary_obj.name = safe_name
            total_verts = len(primary_obj.data.vertices)
            total_tris = sum(max(1, len(p.vertices) - 2) for p in primary_obj.data.polygons)

            if total_tris > MAX_NON_NANITE_TRIANGLES_WARN:
                self.warnings.append(
                    f"Prop '{safe_name}' has {total_tris} triangles (> {MAX_NON_NANITE_TRIANGLES_WARN}); "
                    "enable Nanite on this StaticMesh in UEFN."
                )

            has_collision = False
            if self.generate_collision and total_verts >= 4:
                ucx_obj = self._create_convex_hull_collision(primary_obj, f"UCX_{safe_name}_01")
                if ucx_obj:
                    temp_objs.append(ucx_obj)
                    temp_meshes.append(ucx_obj.data)
                    has_collision = True

            bpy.ops.object.select_all(action="DESELECT")
            for o in temp_objs:
                o.select_set(True)
            bpy.context.view_layer.objects.active = primary_obj

            self._run_fbx_export(out_fbx, include_armature=False)

            mesh_info = ExportedMeshInfo(
                mesh_name=safe_name,
                fbx_path=out_fbx,
                vertex_count=total_verts,
                triangle_count=total_tris,
                materials=ue_materials,
                source_materials=src_materials,
                has_collision=has_collision,
                is_chunk=False,
            )
            self.exported_meshes[safe_name] = mesh_info
            return mesh_info

        except Exception as e:
            err = f"Failed to export FBX for {safe_name}: {e}"
            logger.error(err)
            self.errors.append(err)
            return None
        finally:
            for t_obj in temp_objs:
                if t_obj and t_obj.name in bpy.data.objects:
                    bpy.data.objects.remove(t_obj, do_unlink=True)
            for t_mesh in temp_meshes:
                if t_mesh and t_mesh.users == 0:
                    bpy.data.meshes.remove(t_mesh)

    def export_spatial_world_chunks(
        self,
        source_objects: list[Any],
        prefix: str = "world_chunk",
        on_material_seen: Optional[Callable[[str], None]] = None,
        face_filter: Optional[Callable[[list[float]], bool]] = None,
        pivot_offset_bu: Optional[list[float]] = None,
    ) -> list[ExportedMeshInfo]:
        """
        Cull tool textures (and optional face filter in Hammer units), then spatially
        split world geometry (or displacements) into XY grid cells of size
        `self.world_chunk_size` Hammer units and export each non-empty cell as an FBX chunk.
        """
        if not IN_BLENDER:
            self.errors.append("Cannot export world chunks: Blender (bpy) not available.")
            return []

        mesh_objs = [obj for obj in source_objects if obj is not None and obj.type == "MESH"]
        if not mesh_objs:
            return []

        chunk_size = max(256.0, float(self.world_chunk_size))

        # Join copies first so world geometry / displacements chunk cleanly in world space
        joined_copy = None
        temp_meshes: list[Any] = []
        temp_objs: list[Any] = []

        try:
            for src_obj in mesh_objs:
                dup_mesh = src_obj.data.copy()
                temp_meshes.append(dup_mesh)
                dup_obj = bpy.data.objects.new(f"__tmp_{prefix}_{len(temp_objs)}", dup_mesh)
                dup_obj.matrix_world = src_obj.matrix_world.copy()
                bpy.context.scene.collection.objects.link(dup_obj)
                # Bake object transform into mesh vertices
                dup_mesh.transform(dup_obj.matrix_world)
                dup_obj.matrix_world = Matrix.Identity(4)
                temp_objs.append(dup_obj)

            if len(temp_objs) > 1:
                bpy.ops.object.select_all(action="DESELECT")
                for o in temp_objs:
                    o.select_set(True)
                bpy.context.view_layer.objects.active = temp_objs[0]
                bpy.ops.object.join()
                joined_copy = bpy.context.view_layer.objects.active
                temp_objs = [joined_copy]
            else:
                joined_copy = temp_objs[0]

            # Build bmesh, delete tool / filtered faces, and write back to joined_copy.data
            # BEFORE indexing face_cell_map so face indices stay 1:1 with joined_copy.data.
            bm = bmesh.new()
            bm.from_mesh(joined_copy.data)

            tool_mat_indices = set()
            for idx, slot in enumerate(joined_copy.material_slots):
                if slot.material and is_tool_material(get_material_source_path(slot.material)):
                    tool_mat_indices.add(idx)

            drop_faces = []
            for f in bm.faces:
                if f.material_index in tool_mat_indices:
                    drop_faces.append(f)
                    continue
                if face_filter is not None:
                    c = f.calc_center_median()
                    if not face_filter([c.x / self.blender_scale, c.y / self.blender_scale, c.z / self.blender_scale]):
                        drop_faces.append(f)

            if drop_faces:
                bmesh.ops.delete(bm, geom=drop_faces, context="FACES")

            if len(bm.faces) == 0:
                bm.free()
                return []

            bm.to_mesh(joined_copy.data)
            joined_copy.data.update()
            bm.faces.ensure_lookup_table()

            # Bucket remaining faces (exact 1:1 indices with joined_copy.data) by XY grid cell in Hammer units
            face_cell_map: dict[tuple[int, int], list[int]] = defaultdict(list)
            for face_idx, face in enumerate(bm.faces):
                center = face.calc_center_median()
                cx_hu = center.x / self.blender_scale
                cy_hu = center.y / self.blender_scale
                gx = int(math.floor(cx_hu / chunk_size))
                gy = int(math.floor(cy_hu / chunk_size))
                face_cell_map[(gx, gy)].append(face_idx)

            bm.free()

            px, py, pz = (
                (float(pivot_offset_bu[0]), float(pivot_offset_bu[1]), float(pivot_offset_bu[2]))
                if pivot_offset_bu is not None
                else (0.0, 0.0, 0.0)
            )
            has_pivot_shift = pivot_offset_bu is not None and (px != 0.0 or py != 0.0 or pz != 0.0)

            exported_chunks: list[ExportedMeshInfo] = []
            for (gx, gy), keep_indices in sorted(face_cell_map.items()):
                keep_set = set(keep_indices)
                gx_tag = f"n{abs(gx)}" if gx < 0 else str(gx)
                gy_tag = f"n{abs(gy)}" if gy < 0 else str(gy)
                chunk_name = f"SM_{prefix}_{gx_tag}_{gy_tag}"
                out_fbx = self.output_dir / f"{chunk_name}.fbx"

                chunk_mesh = joined_copy.data.copy()
                temp_meshes.append(chunk_mesh)
                chunk_obj = bpy.data.objects.new(chunk_name, chunk_mesh)
                bpy.context.scene.collection.objects.link(chunk_obj)
                temp_objs.append(chunk_obj)

                cbm = bmesh.new()
                cbm.from_mesh(chunk_mesh)
                cbm.faces.ensure_lookup_table()
                del_faces = [f for idx, f in enumerate(cbm.faces) if idx not in keep_set]
                if del_faces:
                    bmesh.ops.delete(cbm, geom=del_faces, context="FACES")
                if has_pivot_shift:
                    for v in cbm.verts:
                        v.co.x -= px
                        v.co.y -= py
                        v.co.z -= pz
                _clean_and_triangulate_bmesh(cbm)

                cbm.to_mesh(chunk_mesh)
                cbm.free()

                if len(chunk_mesh.polygons) == 0:
                    continue

                # Remove unused material slots before remapping
                used_slot_indices = {p.material_index for p in chunk_mesh.polygons}
                ue_materials, src_materials = self._remap_used_materials(
                    chunk_obj, used_slot_indices, on_material_seen
                )

                total_verts = len(chunk_mesh.vertices)
                total_tris = sum(max(1, len(p.vertices) - 2) for p in chunk_mesh.polygons)

                if total_tris > MAX_CHUNK_TRIANGLES_WARN:
                    self.warnings.append(
                        f"World chunk '{chunk_name}' has {total_tris} triangles (> {MAX_CHUNK_TRIANGLES_WARN}). "
                        "Consider decreasing world_chunk_size."
                    )

                bpy.ops.object.select_all(action="DESELECT")
                chunk_obj.select_set(True)
                bpy.context.view_layer.objects.active = chunk_obj

                # Note: For concave world architecture chunks, UEFN should use ComplexAsSimple
                # collision (or automatic collision) rather than a single convex hull, which would block corridors!
                self._run_fbx_export(out_fbx, include_armature=False)

                info = ExportedMeshInfo(
                    mesh_name=chunk_name,
                    fbx_path=out_fbx,
                    vertex_count=total_verts,
                    triangle_count=total_tris,
                    materials=ue_materials,
                    source_materials=src_materials,
                    has_collision=False,  # Uses ComplexAsSimple in UEFN for walkable corridors
                    is_chunk=True,
                )
                self.exported_meshes[chunk_name] = info
                exported_chunks.append(info)

            return exported_chunks
        except Exception as e:
            err = f"Failed exporting world chunks ({prefix}): {e}"
            logger.error(err)
            self.errors.append(err)
            return []
        finally:
            for t_obj in temp_objs:
                if t_obj and t_obj.name in bpy.data.objects:
                    bpy.data.objects.remove(t_obj, do_unlink=True)
            for t_mesh in temp_meshes:
                if t_mesh and t_mesh.users == 0:
                    bpy.data.meshes.remove(t_mesh)

    def _remap_used_materials(
        self,
        obj: Any,
        used_indices: set[int],
        on_material_seen: Optional[Callable[[str], None]] = None,
    ) -> tuple[list[str], list[str]]:
        ue_names: list[str] = []
        src_paths: list[str] = []
        new_mats: list[Any] = []
        old_to_new: dict[int, int] = {}

        for idx in sorted(used_indices):
            if idx >= len(obj.material_slots):
                continue
            mat = obj.material_slots[idx].material
            if mat is None:
                continue
            src_path = get_material_source_path(mat)
            if is_tool_material(src_path):
                continue
            if on_material_seen:
                on_material_seen(src_path)
            ue_name = material_ue_name(src_path)
            proxy_mat = bpy.data.materials.get(ue_name)
            if proxy_mat is None:
                proxy_mat = bpy.data.materials.new(name=ue_name)
                proxy_mat["uefn_proxy"] = True

            if proxy_mat in new_mats:
                new_idx = new_mats.index(proxy_mat)
            else:
                new_idx = len(new_mats)
                new_mats.append(proxy_mat)
                ue_names.append(ue_name)
                src_paths.append(src_path)
            old_to_new[idx] = new_idx

        mesh = obj.data
        # Preserve original polygon material indices BEFORE clear(), because
        # Blender's mesh.materials.clear() resets every poly.material_index to 0!
        orig_indices = [poly.material_index for poly in mesh.polygons]
        mesh.materials.clear()
        for pm in new_mats:
            mesh.materials.append(pm)
        if old_to_new:
            for poly, old_idx in zip(mesh.polygons, orig_indices):
                poly.material_index = old_to_new.get(old_idx, 0)

        return ue_names, src_paths

    def export_brush_entity(
        self,
        entity_name: str,
        brush_obj: Any,
        on_material_seen: Optional[Callable[[str], None]] = None,
    ) -> Optional[ExportedMeshInfo]:
        """
        Export a single visible brush entity (e.g. func_door, func_brush) centered at its
        local origin, with tool faces stripped. Returns None if all faces are invisible tool faces.
        """
        if not IN_BLENDER or brush_obj is None or brush_obj.type != "MESH":
            return None

        safe_name = sanitize_ue_name(entity_name, prefix="SM_brush_")
        if safe_name in self.exported_meshes:
            return self.exported_meshes[safe_name]

        out_fbx = self.output_dir / f"{safe_name}.fbx"
        temp_objs: list[Any] = []
        temp_meshes: list[Any] = []

        try:
            dup_mesh = brush_obj.data.copy()
            temp_meshes.append(dup_mesh)
            dup_obj = bpy.data.objects.new(safe_name, dup_mesh)
            dup_obj.location = (0.0, 0.0, 0.0)
            dup_obj.rotation_euler = (0.0, 0.0, 0.0)
            dup_obj.scale = (1.0, 1.0, 1.0)
            bpy.context.scene.collection.objects.link(dup_obj)
            temp_objs.append(dup_obj)

            bm = bmesh.new()
            bm.from_mesh(dup_mesh)
            tool_indices = {
                idx for idx, slot in enumerate(dup_obj.material_slots)
                if slot.material and is_tool_material(get_material_source_path(slot.material))
            }
            del_faces = [f for f in bm.faces if f.material_index in tool_indices]
            if del_faces:
                bmesh.ops.delete(bm, geom=del_faces, context="FACES")
            _clean_and_triangulate_bmesh(bm)
            bm.to_mesh(dup_mesh)
            bm.free()

            if len(dup_mesh.polygons) == 0:
                return None

            used_slots = {p.material_index for p in dup_mesh.polygons}
            ue_materials, src_materials = self._remap_used_materials(
                dup_obj, used_slots, on_material_seen
            )

            total_verts = len(dup_mesh.vertices)
            total_tris = sum(max(1, len(p.vertices) - 2) for p in dup_mesh.polygons)

            has_collision = False
            if self.generate_collision and total_verts >= 4:
                ucx_obj = self._create_convex_hull_collision(dup_obj, f"UCX_{safe_name}_01")
                if ucx_obj:
                    temp_objs.append(ucx_obj)
                    temp_meshes.append(ucx_obj.data)
                    has_collision = True

            bpy.ops.object.select_all(action="DESELECT")
            for o in temp_objs:
                o.select_set(True)
            bpy.context.view_layer.objects.active = dup_obj
            self._run_fbx_export(out_fbx, include_armature=False)

            info = ExportedMeshInfo(
                mesh_name=safe_name,
                fbx_path=out_fbx,
                vertex_count=total_verts,
                triangle_count=total_tris,
                materials=ue_materials,
                source_materials=src_materials,
                has_collision=has_collision,
                is_chunk=False,
            )
            self.exported_meshes[safe_name] = info
            return info
        except Exception as e:
            err = f"Failed exporting brush entity {safe_name}: {e}"
            logger.error(err)
            self.errors.append(err)
            return None
        finally:
            for t_obj in temp_objs:
                if t_obj and t_obj.name in bpy.data.objects:
                    bpy.data.objects.remove(t_obj, do_unlink=True)
            for t_mesh in temp_meshes:
                if t_mesh and t_mesh.users == 0:
                    bpy.data.meshes.remove(t_mesh)

    def _create_convex_hull_collision(
        self,
        base_obj: Any,
        hull_name: str,
    ) -> Optional[Any]:
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
            geom_interior = hull_res.get("geom_interior", [])
            geom_unused = hull_res.get("geom_unused", [])
            if geom_interior or geom_unused:
                bmesh.ops.delete(bm, geom=geom_interior + geom_unused, context="VERTS")

            if len(bm.faces) == 0:
                bm.free()
                return None

            hull_mesh = bpy.data.meshes.new(f"{hull_name}_mesh")
            bm.to_mesh(hull_mesh)
            bm.free()

            hull_obj = bpy.data.objects.new(hull_name, hull_mesh)
            hull_obj.matrix_world = base_obj.matrix_world.copy()
            bpy.context.scene.collection.objects.link(hull_obj)
            return hull_obj
        except Exception as e:
            logger.warn(f"Failed to generate convex hull for {base_obj.name}: {e}")
            bm.free()
            return None
