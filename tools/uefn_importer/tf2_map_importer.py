"""
tf2_map_importer.py — UEFN Toolbelt Custom Plugin
=================================================

Custom Toolbelt plugin that imports a TF2 map manifest exported by SourceIO into UEFN.
Depends on UEFN_Toolbelt.core (never copies AGPL-3.0 Toolbelt source).

Features:
- Resolves the real UEFN project mount via UEFN_Toolbelt.core.detect_project_mount()
  (e.g., /TwoFortDef/TF2_Map), avoiding /Game crashes.
- Imports FBX meshes, PNG/TGA textures, and WAV audio in safe batches.
- Configures Texture2D sRGB and CompressionSettings (TC_NORMALMAP / TC_MASKS / TC_DEFAULT)
  from Materials/materials_index.json.
- Creates MaterialInstanceConstant assets and assigns per-skin material overrides.
- Sets ComplexAsSimple collision on concave world/displacement chunks while preserving
  UCX_ convex hulls on props.
- Spawns StaticMeshActors, AmbientSound actors, and Gameplay TargetPoint markers
  in batches of `batch_size` inside ScopedEditorTransactions (idempotent & undoable).
- Writes import_report.json with spawned vs expected counts and any failures.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

try:
    import unreal
    IN_UNREAL = True
except ImportError:
    unreal = None
    IN_UNREAL = False

try:
    from UEFN_Toolbelt.registry import register_tool
    from UEFN_Toolbelt.core import (
        create_material_instance,
        detect_project_mount,
        ensure_folder,
        load_asset,
        log_error,
        log_info,
        log_warning,
        save_asset,
        set_mi_scalar,
        set_mi_texture,
        set_mi_vector,
    )
except ImportError:
    def register_tool(*args, **kwargs):
        def decorator(f):
            return f
        return decorator

    def detect_project_mount() -> str:
        return "TwoFortDef"

    def ensure_folder(path: str) -> None:
        pass

    def load_asset(path: str):
        return None

    def save_asset(path: str) -> bool:
        return False

    def create_material_instance(parent: str, name: str, pkg: str):
        return None

    def set_mi_scalar(*a, **kw):
        pass

    def set_mi_vector(*a, **kw):
        pass

    def set_mi_texture(*a, **kw):
        pass

    def log_info(msg: str):
        print(msg)

    def log_warning(msg: str):
        print(f"WARN: {msg}")

    def log_error(msg: str):
        print(f"ERROR: {msg}")


MIN_TOOLBELT_VERSION = "2.0.0"
TAG_TF2_IMPORTED = "TF2_Imported"


def _base_content_path() -> str:
    if IN_UNREAL:
        try:
            world = unreal.EditorLevelLibrary.get_editor_world()
            if world:
                parts = [p for p in str(world.get_path_name()).split("/") if p]
                if parts:
                    return f"/{parts[0]}/TF2_Map"
        except Exception:
            pass
    return f"/{detect_project_mount()}/TF2_Map"


@register_tool(
    name="tf2_import_manifest",
    category="TF2",
    description="Import a complete TF2 map from map_manifest.json into UEFN with batching and idempotency.",
    tags=["tf2", "import", "bsp", "manifest", "actors", "mcp"],
)
def run_import_manifest(
    manifest_path: str = "",
    import_assets: bool = True,
    build_materials: bool = True,
    spawn_actors: bool = True,
    batch_size: int = 250,
    clean_existing: bool = True,
    include_skybox: bool = True,
    **kwargs,
) -> dict[str, Any]:
    """
    Main entry point for importing a TF2 map into UEFN.

    Args:
        manifest_path:   Absolute path to map_manifest.json (defaults to Saved/UEFN_Export/map_manifest.json).
        import_assets:   Import Meshes, Textures, and Audio into the project Content folder.
        build_materials: Configure Texture2D sRGB/compression and create MaterialInstanceConstants.
        spawn_actors:    Spawn StaticMeshActors, AmbientSounds, and Gameplay markers in the active level.
        batch_size:      Actors spawned per ScopedEditorTransaction batch.
        clean_existing:  Remove previously imported TF2_Imported actors first (idempotent re-run).
        include_skybox:  Whether to spawn 3D skybox actors tagged is_skybox=True.
    """
    if not IN_UNREAL:
        return {"status": "error", "error": "This tool must be run inside UEFN with Python enabled."}

    manifest_file = _resolve_manifest_path(manifest_path)
    if not manifest_file.exists():
        return {
            "status": "error",
            "error": f"Manifest file not found: {manifest_file}. Run SourceIO UEFN Exporter first.",
        }

    try:
        with open(manifest_file, "r", encoding="utf-8") as f:
            manifest_data = json.load(f)
    except Exception as e:
        return {"status": "error", "error": f"Failed reading manifest JSON: {e}"}

    manifest_dir = manifest_file.parent
    base_pkg = _base_content_path()
    actors_data = manifest_data.get("actors", [])
    if not include_skybox:
        actors_data = [a for a in actors_data if not a.get("is_skybox", False)]

    results: dict[str, Any] = {
        "status": "ok",
        "project_mount": base_pkg,
        "map_name": manifest_data.get("map_name", "TF2_Map"),
        "manifest_path": str(manifest_file),
        "expected_actors": len(actors_data),
        "assets_imported": 0,
        "materials_created": 0,
        "actors_spawned": 0,
        "actors_cleaned": 0,
        "manual_steps": [],
        "failures": [],
    }

    if clean_existing:
        clean_res = run_clean_imported()
        results["actors_cleaned"] = clean_res.get("deleted_count", 0)

    if import_assets:
        asset_res = _import_all_assets(manifest_dir, base_pkg, batch_size=max(25, batch_size // 5))
        results["assets_imported"] = asset_res.get("imported_count", 0)
        results["failures"].extend(asset_res.get("failures", []))

    if build_materials:
        mat_res = _configure_textures_and_materials(manifest_dir, base_pkg)
        results["materials_created"] = mat_res.get("materials_created", 0)
        results["manual_steps"].extend(mat_res.get("manual_steps", []))
        results["failures"].extend(mat_res.get("failures", []))

    if spawn_actors:
        spawn_res = _spawn_actors_in_batches(
            actors_data,
            base_pkg,
            batch_size=max(1, batch_size),
            sky_camera=manifest_data.get("sky_camera"),
        )
        results["actors_spawned"] = spawn_res.get("spawned_count", 0)
        results["failures"].extend(spawn_res.get("failures", []))

    if results["failures"] and results["actors_spawned"] == 0 and spawn_actors:
        results["status"] = "error"

    _write_import_report(manifest_dir, results)
    return results


@register_tool(
    name="tf2_clean_imported",
    category="TF2",
    description="Safely removes all TF2 actors tagged with 'TF2_Imported' from the current level.",
    tags=["tf2", "clean", "undo", "delete", "safe"],
)
def run_clean_imported(**kwargs) -> dict[str, Any]:
    if not IN_UNREAL:
        return {"status": "error", "error": "This tool must be run inside UEFN."}

    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    all_actors = actor_sub.get_all_level_actors()

    deleted = 0
    with unreal.ScopedEditorTransaction("TF2 Clean Imported Actors") as _:
        for actor in all_actors:
            if not actor:
                continue
            actor_tags = {str(t) for t in actor.tags}
            folder = str(actor.get_folder_path())
            if TAG_TF2_IMPORTED in actor_tags or folder.startswith("TF2_Map"):
                actor_sub.destroy_actor(actor)
                deleted += 1

    return {"status": "ok", "deleted_count": deleted}


@register_tool(
    name="tf2_import_assets_only",
    category="TF2",
    description="Imports only Meshes, Textures, and Audio from the UEFN export folder.",
    tags=["tf2", "import", "assets"],
)
def run_import_assets_only(
    manifest_path: str = "",
    batch_size: int = 50,
    build_materials: bool = True,
    offset: int = 0,
    limit: int = 0,
    **kwargs,
) -> dict[str, Any]:
    if not IN_UNREAL:
        return {"status": "error", "error": "This tool must be run inside UEFN."}
    manifest_file = _resolve_manifest_path(manifest_path)
    if not manifest_file.exists():
        return {"status": "error", "error": f"Manifest not found: {manifest_file}"}

    base_pkg = _base_content_path()
    asset_res = _import_all_assets(manifest_file.parent, base_pkg, batch_size=batch_size, offset=offset, limit=limit)
    mat_res = _configure_textures_and_materials(manifest_file.parent, base_pkg) if build_materials else {"materials_created": 0, "manual_steps": [], "failures": []}
    return {
        "status": "ok",
        "project_mount": base_pkg,
        "total_tasks": asset_res.get("total_tasks", 0),
        "imported_count": asset_res.get("imported_count", 0),
        "materials_created": mat_res.get("materials_created", 0),
        "manual_steps": mat_res.get("manual_steps", []),
        "failures": asset_res.get("failures", []) + mat_res.get("failures", []),
    }


@register_tool(
    name="tf2_spawn_actors_only",
    category="TF2",
    description="Spawns actors from map_manifest.json assuming assets are already imported.",
    tags=["tf2", "spawn", "actors", "batch"],
)
def run_spawn_actors_only(
    manifest_path: str = "",
    batch_size: int = 250,
    include_skybox: bool = True,
    offset: int = 0,
    limit: int = 0,
    **kwargs,
) -> dict[str, Any]:
    if not IN_UNREAL:
        return {"status": "error", "error": "This tool must be run inside UEFN."}
    manifest_file = _resolve_manifest_path(manifest_path)
    if not manifest_file.exists():
        return {"status": "error", "error": f"Manifest not found: {manifest_file}"}

    with open(manifest_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    actors = data.get("actors", [])
    if not include_skybox:
        actors = [a for a in actors if not a.get("is_skybox", False)]
    total_actors = len(actors)
    if offset > 0 or limit > 0:
        end = offset + limit if limit > 0 else total_actors
        actors = actors[offset:end]
    res = _spawn_actors_in_batches(
        actors,
        _base_content_path(),
        batch_size=max(1, batch_size),
        sky_camera=data.get("sky_camera"),
    )
    return {
        "status": "ok",
        "total_manifest_actors": total_actors,
        "expected_actors": len(actors),
        "spawned_count": res.get("spawned_count", 0),
        "failures": res.get("failures", []),
    }


@register_tool(
    name="tf2_build_materials",
    category="TF2",
    description="Auto-creates Master Materials and builds MaterialInstanceConstants from materials_index.json.",
    tags=["tf2", "materials", "textures", "batch"],
)
def run_build_materials(
    manifest_path: str = "",
    configure_textures: bool = False,
    offset: int = 0,
    limit: int = 0,
    **kwargs,
) -> dict[str, Any]:
    if not IN_UNREAL:
        return {"status": "error", "error": "This tool must be run inside UEFN."}
    manifest_file = _resolve_manifest_path(manifest_path)
    if not manifest_file.exists():
        return {"status": "error", "error": f"Manifest not found: {manifest_file}"}
    base_pkg = _base_content_path()
    res = _configure_textures_and_materials(
        manifest_file.parent,
        base_pkg,
        configure_textures=configure_textures,
        mi_offset=offset,
        mi_limit=limit,
    )
    return {"status": "ok", "project_mount": base_pkg, **res}


def _assign_actor_materials(
    sm_comp: Any,
    mat_overrides: list[str],
    base_pkg: str,
    mat_cache: dict[str, Optional[unreal.MaterialInterface]],
    prefer_slot_names: bool = True,
) -> int:
    """Assign MI_* materials matching UEFN slot names when possible (Interchange may reorder slots)."""
    assigned = 0
    slot_names: list[str] = []
    if prefer_slot_names:
        try:
            slot_names = [str(n) for n in sm_comp.get_material_slot_names()]
        except Exception:
            slot_names = []

    if slot_names:
        override_set = set(mat_overrides)
        for slot_i, s_name in enumerate(slot_names):
            mi_name = (
                s_name
                if (s_name in override_set or s_name.startswith("MI_"))
                else (mat_overrides[slot_i] if slot_i < len(mat_overrides) else "")
            )
            if not mi_name:
                continue
            if mi_name not in mat_cache:
                mat_cache[mi_name] = load_asset(f"{base_pkg}/Materials/{mi_name}")
            mi_obj = mat_cache[mi_name]
            if isinstance(mi_obj, unreal.MaterialInterface):
                sm_comp.set_material(slot_i, mi_obj)
                assigned += 1
        if assigned > 0:
            return assigned

    for slot_i, mi_name in enumerate(mat_overrides):
        if mi_name not in mat_cache:
            mat_cache[mi_name] = load_asset(f"{base_pkg}/Materials/{mi_name}")
        mi_obj = mat_cache[mi_name]
        if isinstance(mi_obj, unreal.MaterialInterface):
            sm_comp.set_material(slot_i, mi_obj)
            assigned += 1
    return assigned


@register_tool(
    name="tf2_apply_materials",
    category="TF2",
    description="Applies MI_* MaterialInstances (and refreshes StaticMesh assets) on spawned TF2 actors in batches.",
    tags=["tf2", "materials", "actors", "batch"],
)
def run_apply_materials(
    manifest_path: str = "",
    offset: int = 0,
    limit: int = 0,
    refresh_mesh: bool = False,
    **kwargs,
) -> dict[str, Any]:
    if not IN_UNREAL:
        return {"status": "error", "error": "This tool must be run inside UEFN."}
    manifest_file = _resolve_manifest_path(manifest_path)
    if not manifest_file.exists():
        return {"status": "error", "error": f"Manifest not found: {manifest_file}"}

    with open(manifest_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    manifest_map = {
        a.get("actor_name", ""): a
        for a in data.get("actors", [])
        if a.get("actor_name")
    }
    base_pkg = _base_content_path()
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    all_actors = actor_sub.get_all_level_actors()
    tf2_meshes = [
        a for a in all_actors
        if isinstance(a, unreal.StaticMeshActor) and a.get_actor_label() in manifest_map
    ]
    total = len(tf2_meshes)
    if offset > 0 or limit > 0:
        end = offset + limit if limit > 0 else total
        tf2_meshes = tf2_meshes[offset:end]

    mat_cache: dict[str, Optional[unreal.MaterialInterface]] = {}
    mesh_cache: dict[str, Optional[unreal.StaticMesh]] = {}
    updated = 0
    slots_set = 0

    with unreal.ScopedEditorTransaction(f"TF2 Apply Materials {offset}-{offset + len(tf2_meshes)}") as _:
        for actor in tf2_meshes:
            rec = manifest_map[actor.get_actor_label()]
            sm_comp = getattr(actor, "static_mesh_component", None)
            if not sm_comp:
                continue
            asset_ref = rec.get("asset_reference") or ""
            if refresh_mesh and asset_ref.endswith(".fbx"):
                mesh_stem = Path(asset_ref).stem
                if mesh_stem not in mesh_cache:
                    mesh_cache[mesh_stem] = load_asset(f"{base_pkg}/Meshes/{mesh_stem}")
                sm = mesh_cache[mesh_stem]
                if isinstance(sm, unreal.StaticMesh):
                    sm_comp.set_static_mesh(sm)

            mat_overrides = rec.get("material_overrides", [])
            skin_idx = int((rec.get("source_entity_data") or {}).get("skin", 0) or 0)
            slots_set += _assign_actor_materials(
                sm_comp,
                mat_overrides,
                base_pkg,
                mat_cache,
                prefer_slot_names=(skin_idx == 0),
            )
            updated += 1

    return {
        "status": "ok",
        "total_actors": total,
        "updated_actors": updated,
        "slots_assigned": slots_set,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Internal Implementation Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_manifest_path(user_path: str) -> Path:
    if user_path and user_path.strip():
        return Path(user_path.strip())

    proj_dir = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_dir())) if IN_UNREAL else Path.cwd()
    candidates = [
        proj_dir / "Saved" / "UEFN_Export" / "map_manifest.json",
        proj_dir / "Content" / "TF2_Export" / "map_manifest.json",
        proj_dir / "docs" / "map_manifest.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _import_all_assets(
    manifest_dir: Path,
    base_pkg: str,
    batch_size: int = 50,
    offset: int = 0,
    limit: int = 0,
) -> dict[str, Any]:
    asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
    failures: list[str] = []

    meshes_dir = manifest_dir / "Meshes"
    textures_dir = manifest_dir / "Textures"
    audio_dir = manifest_dir / "Audio"

    for sub in ("Meshes", "Textures", "Materials", "Audio"):
        ensure_folder(f"{base_pkg}/{sub}")

    pending_specs: list[tuple[str, Path]] = []
    if meshes_dir.exists():
        for fbx in sorted(meshes_dir.glob("*.fbx")):
            pending_specs.append(("mesh", fbx))
    if textures_dir.exists():
        for tex_file in sorted(list(textures_dir.glob("*.png")) + list(textures_dir.glob("*.tga"))):
            pending_specs.append(("tex", tex_file))
    if audio_dir.exists():
        for wav_file in sorted(audio_dir.glob("*.wav")):
            pending_specs.append(("audio", wav_file))

    total_tasks = len(pending_specs)
    if offset > 0 or limit > 0:
        end = offset + limit if limit > 0 else total_tasks
        pending_specs = pending_specs[offset:end]

    tasks: list[unreal.AssetImportTask] = []
    for kind, src_path in pending_specs:
        task = unreal.AssetImportTask()
        task.filename = str(src_path)
        task.destination_name = src_path.stem
        task.replace_existing = True
        task.automated = True
        task.save = True
        if kind == "mesh":
            task.destination_path = f"{base_pkg}/Meshes"
            fbx_options = unreal.FbxImportUI()
            fbx_options.import_mesh = True
            fbx_options.import_textures = False
            fbx_options.import_materials = False
            fbx_options.import_as_skeletal = False
            fbx_options.static_mesh_import_data.combine_meshes = True
            fbx_options.static_mesh_import_data.auto_generate_collision = False
            task.options = fbx_options
        elif kind == "tex":
            task.destination_path = f"{base_pkg}/Textures"
        else:
            task.destination_path = f"{base_pkg}/Audio"
        tasks.append(task)

    imported_count = 0
    for i in range(0, len(tasks), batch_size):
        chunk = tasks[i:i + batch_size]
        try:
            asset_tools.import_asset_tasks(chunk)
            for t in chunk:
                objs = t.get_objects() if hasattr(t, "get_objects") else []
                if getattr(t, "imported_object_paths", None) or objs:
                    imported_count += 1
        except Exception as e:
            failures.append(f"Asset import batch {i} error: {e}")

    # Configure ComplexAsSimple collision only for world/displacement chunks in this slice
    chunk_stems = sorted(
        {t.destination_name for t in tasks if "world_chunk" in t.destination_name or "disp_chunk" in t.destination_name}
    )
    for stem in chunk_stems:
        sm = load_asset(f"{base_pkg}/Meshes/{stem}")
        if isinstance(sm, unreal.StaticMesh):
            try:
                body_setup = sm.get_editor_property("body_setup")
                if body_setup:
                    body_setup.set_editor_property(
                        "collision_trace_flag",
                        unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE,
                    )
                    save_asset(f"{base_pkg}/Meshes/{stem}")
            except Exception as e:
                log_warning(f"Could not set ComplexAsSimple on {stem}: {e}")

    try:
        unreal.SystemLibrary.collect_garbage()
    except Exception:
        pass

    return {"total_tasks": total_tasks, "imported_count": imported_count, "failures": failures}


def _ensure_master_material(
    materials_pkg: str,
    master_name: str,
    default_diffuse: Optional[Any],
    default_normal: Optional[Any],
) -> Optional[Any]:
    """Create a UEFN Master Material with MaterialEditingLibrary if missing."""
    master_path = f"{materials_pkg}/{master_name}"
    if unreal.EditorAssetLibrary.does_asset_exist(master_path):
        return load_asset(master_path)

    try:
        asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
        mat = asset_tools.create_asset(master_name, materials_pkg, unreal.Material, unreal.MaterialFactoryNew())
        if not isinstance(mat, unreal.Material):
            return None

        is_masked = "Masked" in master_name
        is_translucent = any(k in master_name for k in ("Translucent", "Water", "Decal"))
        is_unlit = "Unlit" in master_name
        is_disp = "Displacement" in master_name
        is_two_sided = any(k in master_name for k in ("World", "Water", "Displacement", "Decal", "Masked", "Translucent"))

        try:
            if is_masked:
                mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_MASKED)
            elif is_translucent:
                mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
            if is_unlit:
                mat.set_editor_property("shading_model", unreal.MaterialShadingModel.MSM_UNLIT)
            if is_two_sided:
                mat.set_editor_property("two_sided", True)
        except Exception:
            pass

        mel = unreal.MaterialEditingLibrary
        base_tex = mel.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -500, -150)
        base_tex.set_editor_property("parameter_name", "BaseColorTexture")
        if default_diffuse:
            base_tex.set_editor_property("texture", default_diffuse)

        color_src = base_tex
        if is_disp:
            base_tex2 = mel.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -500, 120)
            base_tex2.set_editor_property("parameter_name", "BaseColorTexture2")
            if default_diffuse:
                base_tex2.set_editor_property("texture", default_diffuse)
            vcol = mel.create_material_expression(mat, unreal.MaterialExpressionVertexColor, -500, 320)
            lerp = mel.create_material_expression(mat, unreal.MaterialExpressionLinearInterpolate, -240, -20)
            mel.connect_material_expressions(base_tex, "RGB", lerp, "A")
            mel.connect_material_expressions(base_tex2, "RGB", lerp, "B")
            mel.connect_material_expressions(vcol, "A", lerp, "Alpha")
            color_src = lerp

        tint = mel.create_material_expression(mat, unreal.MaterialExpressionVectorParameter, -260, -300)
        tint.set_editor_property("parameter_name", "BaseColorTint")
        tint.set_editor_property("default_value", unreal.LinearColor(1.0, 1.0, 1.0, 1.0))

        mul = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -80, -150)
        mel.connect_material_expressions(color_src, "" if is_disp else "RGB", mul, "A")
        mel.connect_material_expressions(tint, "RGB", mul, "B")

        if is_unlit:
            mel.connect_material_property(mul, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)
        else:
            mel.connect_material_property(mul, "", unreal.MaterialProperty.MP_BASE_COLOR)
            rough = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -200, 150)
            rough.set_editor_property("parameter_name", "Roughness")
            rough.set_editor_property("default_value", 0.85)
            mel.connect_material_property(rough, "", unreal.MaterialProperty.MP_ROUGHNESS)

            spec = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -200, 260)
            spec.set_editor_property("parameter_name", "Specular")
            spec.set_editor_property("default_value", 0.2)
            mel.connect_material_property(spec, "", unreal.MaterialProperty.MP_SPECULAR)

            if default_normal:
                norm_tex = mel.create_material_expression(mat, unreal.MaterialExpressionTextureSampleParameter2D, -500, 420)
                norm_tex.set_editor_property("parameter_name", "NormalTexture")
                try:
                    norm_tex.set_editor_property("sampler_type", unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
                except Exception:
                    pass
                norm_tex.set_editor_property("texture", default_normal)
                mel.connect_material_property(norm_tex, "RGB", unreal.MaterialProperty.MP_NORMAL)

        if is_masked:
            mel.connect_material_property(base_tex, "A", unreal.MaterialProperty.MP_OPACITY_MASK)
        elif is_translucent:
            op_param = mel.create_material_expression(mat, unreal.MaterialExpressionScalarParameter, -260, 40)
            op_param.set_editor_property("parameter_name", "Opacity")
            op_param.set_editor_property("default_value", 0.7 if "Water" in master_name else 1.0)
            op_mul = mel.create_material_expression(mat, unreal.MaterialExpressionMultiply, -80, 40)
            mel.connect_material_expressions(base_tex, "A", op_mul, "A")
            mel.connect_material_expressions(op_param, "", op_mul, "B")
            mel.connect_material_property(op_mul, "", unreal.MaterialProperty.MP_OPACITY)

        mel.recompile_material(mat)
        return mat
    except Exception as e:
        log_warning(f"Failed creating Master Material {master_name}: {e}")
        return None


def _create_tf2_material_instance(
    master_path: str,
    mi_name: str,
    materials_pkg: str,
) -> tuple[Optional[Any], bool]:
    """Create (or reuse) a MaterialInstanceConstant using UEFN-safe MaterialEditingLibrary API."""
    full_path = f"{materials_pkg}/{mi_name}"
    if unreal.EditorAssetLibrary.does_asset_exist(full_path):
        existing = load_asset(full_path)
        if isinstance(existing, unreal.MaterialInstanceConstant):
            return existing, False

    parent = load_asset(master_path)
    if not isinstance(parent, unreal.MaterialInterface):
        return None, False

    try:
        asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
        factory = unreal.MaterialInstanceConstantFactoryNew()
        mi = asset_tools.create_asset(mi_name, materials_pkg, unreal.MaterialInstanceConstant, factory)
        if not isinstance(mi, unreal.MaterialInstanceConstant):
            return None, False
        try:
            unreal.MaterialEditingLibrary.set_material_instance_parent(mi, parent)
        except Exception:
            mi.set_editor_property("parent", parent)
        return mi, True
    except Exception as e:
        log_warning(f"Failed creating MI {mi_name} from {master_path}: {e}")
        return None, False


def _configure_textures_and_materials(
    manifest_dir: Path,
    base_pkg: str,
    configure_textures: bool = True,
    mi_offset: int = 0,
    mi_limit: int = 0,
) -> dict[str, Any]:
    """
    Sets Texture2D sRGB/CompressionSettings, auto-creates Master Materials if missing,
    and creates MaterialInstanceConstants from Materials/materials_index.json.
    """
    index_file = manifest_dir / "Materials" / "materials_index.json"
    if not index_file.exists():
        return {"total_materials": 0, "materials_created": 0, "manual_steps": [], "failures": []}

    with open(index_file, "r", encoding="utf-8") as f:
        index_data = json.load(f)

    failures: list[str] = []
    manual_steps: list[str] = []
    dirty_assets: list[Any] = []
    tex_cache: dict[str, Optional[Any]] = {}

    def _get_tex(stem: str) -> Optional[Any]:
        if stem not in tex_cache:
            tex_cache[stem] = load_asset(f"{base_pkg}/Textures/{stem}")
        return tex_cache[stem]

    # 1. Configure Textures (sRGB + Compression) — only modify & batch-save changed textures
    if configure_textures:
        for tex_stem, meta in index_data.get("textures", {}).items():
            tex_obj = _get_tex(tex_stem)
            if not isinstance(tex_obj, unreal.Texture2D):
                continue
            try:
                is_srgb = bool(meta.get("is_srgb", True))
                tex_type = meta.get("texture_type", "diffuse")
                changed = False
                if bool(tex_obj.get_editor_property("srgb")) != is_srgb:
                    tex_obj.set_editor_property("srgb", is_srgb)
                    changed = True
                if tex_type == "normal":
                    if tex_obj.get_editor_property("compression_settings") != unreal.TextureCompressionSettings.TC_NORMALMAP:
                        tex_obj.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_NORMALMAP)
                        changed = True
                    if tex_obj.get_editor_property("lod_group") != unreal.TextureGroup.TEXTUREGROUP_WORLD_NORMAL_MAP:
                        tex_obj.set_editor_property("lod_group", unreal.TextureGroup.TEXTUREGROUP_WORLD_NORMAL_MAP)
                        changed = True
                elif tex_type in ("mask", "blend"):
                    if tex_obj.get_editor_property("compression_settings") != unreal.TextureCompressionSettings.TC_MASKS:
                        tex_obj.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_MASKS)
                        changed = True
                if changed:
                    dirty_assets.append(tex_obj)
            except Exception as e:
                failures.append(f"Texture config error ({tex_stem}): {e}")

    # 2. Ensure Master Materials & Build Material Instances
    materials_pkg = f"{base_pkg}/Materials"
    ensure_folder(materials_pkg)
    default_diffuse = load_asset("/Engine/EngineResources/DefaultTexture")
    default_normal = None
    for tex_stem, meta in index_data.get("textures", {}).items():
        if meta.get("texture_type") == "diffuse" and default_diffuse is None:
            cand = _get_tex(tex_stem)
            if isinstance(cand, unreal.Texture):
                default_diffuse = cand
        elif meta.get("texture_type") == "normal" and default_normal is None:
            cand = _get_tex(tex_stem)
            if isinstance(cand, unreal.Texture):
                default_normal = cand
        if default_diffuse is not None and default_normal is not None:
            break

    all_mat_items = list(index_data.get("materials", {}).items())
    total_materials = len(all_mat_items)
    if mi_offset > 0 or mi_limit > 0:
        end = mi_offset + mi_limit if mi_limit > 0 else total_materials
        all_mat_items = all_mat_items[mi_offset:end]

    materials_created = 0
    missing_masters: set[str] = set()

    for mi_name, spec in all_mat_items:
        master_name = spec.get("master_material_name", "M_Master_World")
        master_path = f"{materials_pkg}/{master_name}"
        if not unreal.EditorAssetLibrary.does_asset_exist(master_path):
            m_obj = _ensure_master_material(materials_pkg, master_name, default_diffuse, default_normal)
            if m_obj is not None:
                dirty_assets.append(m_obj)
        if not unreal.EditorAssetLibrary.does_asset_exist(master_path):
            fallback_master = f"{materials_pkg}/M_Master_Fallback"
            if not unreal.EditorAssetLibrary.does_asset_exist(fallback_master):
                fb_obj = _ensure_master_material(materials_pkg, "M_Master_Fallback", default_diffuse, default_normal)
                if fb_obj is not None:
                    dirty_assets.append(fb_obj)
            if unreal.EditorAssetLibrary.does_asset_exist(fallback_master):
                master_path = fallback_master
            else:
                missing_masters.add(master_name)
                continue

        mi, _created = _create_tf2_material_instance(master_path, mi_name, materials_pkg)
        if mi is None:
            failures.append(f"Failed creating MaterialInstance {mi_name}")
            continue
        if not _created:
            continue

        for param_name, tex_stem in spec.get("textures", {}).items():
            tex_obj = _get_tex(tex_stem)
            if isinstance(tex_obj, unreal.Texture):
                try:
                    set_mi_texture(mi, param_name, tex_obj)
                except Exception:
                    pass

        for s_name, s_val in spec.get("scalars", {}).items():
            try:
                set_mi_scalar(mi, s_name, float(s_val))
            except Exception:
                pass

        for v_name, v_val in spec.get("vectors", {}).items():
            if len(v_val) >= 4:
                try:
                    set_mi_vector(mi, v_name, unreal.LinearColor(v_val[0], v_val[1], v_val[2], v_val[3]))
                except Exception:
                    pass

        try:
            unreal.MaterialEditingLibrary.update_material_instance(mi)
        except Exception:
            pass

        dirty_assets.append(mi)
        materials_created += 1

    if dirty_assets:
        try:
            unreal.EditorAssetLibrary.save_loaded_assets(dirty_assets, only_if_is_dirty=True)
        except Exception:
            for obj in dirty_assets:
                try:
                    unreal.EditorAssetLibrary.save_loaded_asset(obj, only_if_is_dirty=True)
                except Exception:
                    pass
    try:
        unreal.SystemLibrary.collect_garbage()
    except Exception:
        pass

    if missing_masters:
        manual_steps.append(
            f"Create Master Materials in '{materials_pkg}/' exposing parameters "
            "(BaseColorTexture, NormalTexture, Roughness, Specular, BaseColorTint) for: "
            + ", ".join(sorted(missing_masters))
            + ", then re-run tf2_import_manifest(import_assets=False, build_materials=True, spawn_actors=False)."
        )

    return {
        "total_materials": total_materials,
        "materials_created": materials_created,
        "manual_steps": manual_steps,
        "failures": failures,
    }


def project_skybox_actor_transform(
    loc_list: list[float],
    scale_list: list[float],
    is_skybox: bool,
    sky_camera: Optional[dict[str, Any]],
) -> tuple[list[float], list[float]]:
    """Project a 3D skybox actor's miniature location & scale to full 1:1 world space."""
    if not is_skybox or not sky_camera:
        return [float(loc_list[0]), float(loc_list[1]), float(loc_list[2])], [
            float(scale_list[0]), float(scale_list[1]), float(scale_list[2])
        ]
    sky_origin = sky_camera.get("unreal_location", [0.0, 0.0, 0.0])
    sky_scale = float(sky_camera.get("sky_scale", 16.0))
    return (
        [(float(loc_list[i]) - float(sky_origin[i])) * sky_scale for i in range(3)],
        [float(scale_list[i]) * sky_scale for i in range(3)],
    )


def _spawn_actors_in_batches(
    actors_data: list[dict[str, Any]],
    base_pkg: str,
    batch_size: int = 250,
    sky_camera: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    spawned_count = 0
    failures: list[str] = []

    mesh_cache: dict[str, Optional[unreal.StaticMesh]] = {}
    mat_cache: dict[str, Optional[unreal.MaterialInterface]] = {}
    sound_cache: dict[str, Optional[unreal.SoundWave]] = {}

    total = len(actors_data)
    for start in range(0, total, batch_size):
        batch = actors_data[start:start + batch_size]
        with unreal.ScopedEditorTransaction(f"TF2 Spawn Batch {start}-{start + len(batch)}") as _:
            for a_data in batch:
                actor_name = a_data.get("actor_name", f"TF2_Actor_{spawned_count}")
                asset_ref = a_data.get("asset_reference")
                loc_list = a_data.get("location", [0.0, 0.0, 0.0])
                rot_list = a_data.get("rotation", [0.0, 0.0, 0.0])
                scale_list = a_data.get("scale", [1.0, 1.0, 1.0])
                ent_class = a_data.get("entity_class", "prop_static")
                is_gameplay = a_data.get("is_gameplay_entity", False)
                is_skybox = a_data.get("is_skybox", False)
                mat_overrides = a_data.get("material_overrides", [])
                audio_spec = a_data.get("audio_spec")

                loc_list, scale_list = project_skybox_actor_transform(
                    loc_list, scale_list, is_skybox, sky_camera
                )

                loc = unreal.Vector(float(loc_list[0]), float(loc_list[1]), float(loc_list[2]))
                rot = unreal.Rotator(
                    pitch=float(rot_list[0]),
                    yaw=float(rot_list[1]),
                    roll=float(rot_list[2]),
                )
                scale = unreal.Vector(float(scale_list[0]), float(scale_list[1]), float(scale_list[2]))

                try:
                    if audio_spec is not None or ent_class in ("ambient_generic", "env_soundscape"):
                        actor = actor_sub.spawn_actor_from_class(unreal.AmbientSound, loc, rot)
                        if actor and asset_ref:
                            snd_stem = Path(asset_ref).stem
                            if snd_stem not in sound_cache:
                                sound_cache[snd_stem] = load_asset(f"{base_pkg}/Audio/{snd_stem}")
                            snd = sound_cache[snd_stem]
                            comp = actor.get_component_by_class(unreal.AudioComponent)
                            if comp and snd:
                                comp.set_sound(snd)
                            if comp and audio_spec:
                                comp.set_volume_multiplier(float(audio_spec.get("volume", 1.0)))
                        folder = "TF2_Map/Audio"
                    elif is_gameplay and not asset_ref:
                        actor = actor_sub.spawn_actor_from_class(unreal.TargetPoint, loc, rot)
                        folder = f"TF2_Map/Gameplay/{ent_class}"
                    else:
                        actor = actor_sub.spawn_actor_from_class(unreal.StaticMeshActor, loc, rot)
                        if actor and asset_ref:
                            mesh_stem = Path(asset_ref).stem
                            if mesh_stem not in mesh_cache:
                                mesh_cache[mesh_stem] = load_asset(f"{base_pkg}/Meshes/{mesh_stem}")
                            mesh_obj = mesh_cache[mesh_stem]
                            sm_comp = getattr(actor, "static_mesh_component", None)
                            if sm_comp and isinstance(mesh_obj, unreal.StaticMesh):
                                sm_comp.set_static_mesh(mesh_obj)
                                skin_idx = int((a_data.get("source_entity_data") or {}).get("skin", 0) or 0)
                                _assign_actor_materials(
                                    sm_comp,
                                    mat_overrides,
                                    base_pkg,
                                    mat_cache,
                                    prefer_slot_names=(skin_idx == 0),
                                )
                        folder = (
                            "TF2_Map/Skybox" if is_skybox
                            else ("TF2_Map/World" if ent_class == "worldspawn"
                            else ("TF2_Map/Displacements" if ent_class == "displacement"
                            else ("TF2_Map/Decals" if ent_class in ("info_overlay", "infodecal")
                            else ("TF2_Map/Brushes" if ent_class.startswith("func_")
                            else "TF2_Map/Props"))))
                        )

                    if not actor:
                        failures.append(f"spawn_actor_from_class returned None for {actor_name}")
                        continue

                    actor.set_actor_label(actor_name)
                    actor.set_actor_scale3d(scale)
                    actor.tags.append(unreal.Name(TAG_TF2_IMPORTED))
                    actor.set_folder_path(unreal.Name(folder))
                    spawned_count += 1
                except Exception as e:
                    failures.append(f"Exception spawning {actor_name}: {e}")

    return {"spawned_count": spawned_count, "failures": failures}


def _write_import_report(output_dir: Path, results: dict[str, Any]) -> None:
    report_file = output_dir / "import_report.json"
    try:
        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
    except Exception as e:
        log_warning(f"Could not write import_report.json: {e}")
