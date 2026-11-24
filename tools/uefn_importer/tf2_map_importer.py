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
    include_skybox: bool = False,
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
        spawn_res = _spawn_actors_in_batches(actors_data, base_pkg, batch_size=max(1, batch_size))
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
    include_skybox: bool = False,
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
    res = _spawn_actors_in_batches(actors, _base_content_path(), batch_size=max(1, batch_size))
    return {
        "status": "ok",
        "total_manifest_actors": total_actors,
        "expected_actors": len(actors),
        "spawned_count": res.get("spawned_count", 0),
        "failures": res.get("failures", []),
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
    tasks: list[unreal.AssetImportTask] = []
    failures: list[str] = []

    meshes_dir = manifest_dir / "Meshes"
    textures_dir = manifest_dir / "Textures"
    audio_dir = manifest_dir / "Audio"

    for sub in ("Meshes", "Textures", "Materials", "Audio"):
        ensure_folder(f"{base_pkg}/{sub}")

    if meshes_dir.exists():
        for fbx in sorted(meshes_dir.glob("*.fbx")):
            task = unreal.AssetImportTask()
            task.filename = str(fbx)
            task.destination_path = f"{base_pkg}/Meshes"
            task.destination_name = fbx.stem
            task.replace_existing = True
            task.automated = True
            task.save = True

            fbx_options = unreal.FbxImportUI()
            fbx_options.import_mesh = True
            fbx_options.import_textures = False
            fbx_options.import_materials = False
            fbx_options.import_as_skeletal = False
            fbx_options.static_mesh_import_data.combine_meshes = True
            fbx_options.static_mesh_import_data.auto_generate_collision = False
            task.options = fbx_options
            tasks.append(task)

    if textures_dir.exists():
        for tex_file in sorted(list(textures_dir.glob("*.png")) + list(textures_dir.glob("*.tga"))):
            task = unreal.AssetImportTask()
            task.filename = str(tex_file)
            task.destination_path = f"{base_pkg}/Textures"
            task.destination_name = tex_file.stem
            task.replace_existing = True
            task.automated = True
            task.save = True
            tasks.append(task)

    if audio_dir.exists():
        for wav_file in sorted(audio_dir.glob("*.wav")):
            task = unreal.AssetImportTask()
            task.filename = str(wav_file)
            task.destination_path = f"{base_pkg}/Audio"
            task.destination_name = wav_file.stem
            task.replace_existing = True
            task.automated = True
            task.save = True
            tasks.append(task)

    total_tasks = len(tasks)
    if offset > 0 or limit > 0:
        end = offset + limit if limit > 0 else total_tasks
        tasks = tasks[offset:end]

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

    # Configure ComplexAsSimple collision on world/displacement chunks so players can walk inside rooms
    if meshes_dir.exists():
        for fbx in meshes_dir.glob("*.fbx"):
            if "world_chunk" in fbx.stem or "disp_chunk" in fbx.stem:
                sm = load_asset(f"{base_pkg}/Meshes/{fbx.stem}")
                if isinstance(sm, unreal.StaticMesh):
                    try:
                        body_setup = sm.get_editor_property("body_setup")
                        if body_setup:
                            body_setup.set_editor_property(
                                "collision_trace_flag",
                                unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE,
                            )
                            save_asset(f"{base_pkg}/Meshes/{fbx.stem}")
                    except Exception as e:
                        log_warning(f"Could not set ComplexAsSimple on {fbx.stem}: {e}")

    return {"total_tasks": total_tasks, "imported_count": imported_count, "failures": failures}


def _configure_textures_and_materials(manifest_dir: Path, base_pkg: str) -> dict[str, Any]:
    """
    Sets Texture2D sRGB/CompressionSettings and creates MaterialInstanceConstants
    from Materials/materials_index.json.
    """
    index_file = manifest_dir / "Materials" / "materials_index.json"
    if not index_file.exists():
        return {"materials_created": 0, "manual_steps": [], "failures": []}

    with open(index_file, "r", encoding="utf-8") as f:
        index_data = json.load(f)

    failures: list[str] = []
    manual_steps: list[str] = []

    # 1. Configure Textures (sRGB + Compression)
    for tex_stem, meta in index_data.get("textures", {}).items():
        tex_path = f"{base_pkg}/Textures/{tex_stem}"
        tex_obj = load_asset(tex_path)
        if not isinstance(tex_obj, unreal.Texture2D):
            continue
        try:
            is_srgb = bool(meta.get("is_srgb", True))
            tex_type = meta.get("texture_type", "diffuse")
            tex_obj.set_editor_property("srgb", is_srgb)
            if tex_type == "normal":
                tex_obj.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_NORMALMAP)
                tex_obj.set_editor_property("lod_group", unreal.TextureGroup.TEXTUREGROUP_WORLD_NORMAL_MAP)
            elif tex_type in ("mask", "blend"):
                tex_obj.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_MASKS)
            save_asset(tex_path)
        except Exception as e:
            failures.append(f"Texture config error ({tex_stem}): {e}")

    # 2. Check Master Materials & Build Material Instances
    materials_pkg = f"{base_pkg}/Materials"
    ensure_folder(materials_pkg)
    materials_created = 0
    missing_masters: set[str] = set()

    for mi_name, spec in index_data.get("materials", {}).items():
        master_name = spec.get("master_material_name", "M_Master_World")
        master_path = f"{materials_pkg}/{master_name}"
        if not unreal.EditorAssetLibrary.does_asset_exist(master_path):
            fallback_master = f"{materials_pkg}/M_Master_Fallback"
            if unreal.EditorAssetLibrary.does_asset_exist(fallback_master):
                master_path = fallback_master
            else:
                missing_masters.add(master_name)
                continue

        mi = create_material_instance(master_path, mi_name, materials_pkg)
        if mi is None:
            failures.append(f"Failed creating MaterialInstance {mi_name}")
            continue

        for param_name, tex_stem in spec.get("textures", {}).items():
            tex_obj = load_asset(f"{base_pkg}/Textures/{tex_stem}")
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

        save_asset(f"{materials_pkg}/{mi_name}")
        materials_created += 1

    if missing_masters:
        manual_steps.append(
            f"Create Master Materials in '{materials_pkg}/' exposing parameters "
            "(BaseColorTexture, NormalTexture, Roughness, Specular, BaseColorTint) for: "
            + ", ".join(sorted(missing_masters))
            + ", then re-run tf2_import_manifest(import_assets=False, build_materials=True, spawn_actors=False)."
        )

    return {
        "materials_created": materials_created,
        "manual_steps": manual_steps,
        "failures": failures,
    }


def _spawn_actors_in_batches(
    actors_data: list[dict[str, Any]],
    base_pkg: str,
    batch_size: int = 250,
) -> dict[str, Any]:
    actor_sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    spawned_count = 0
    failures: list[str] = []

    mesh_cache: dict[str, Optional[unreal.StaticMesh]] = {}
    mat_cache: dict[str, Optional[unreal.MaterialInterface]] = {}
    sound_cache: dict[str, Optional[unreal.SoundWave]] = {}

    total = len(actors_data)
    with unreal.ScopedSlowTask(total, "Spawning TF2 Map Actors...") as slow_task:
        slow_task.make_dialog(True)

        for start in range(0, total, batch_size):
            batch = actors_data[start:start + batch_size]
            with unreal.ScopedEditorTransaction(f"TF2 Spawn Batch {start}-{start + len(batch)}") as _:
                for a_data in batch:
                    if slow_task.should_cancel():
                        failures.append("Spawn cancelled by user.")
                        return {"spawned_count": spawned_count, "failures": failures}
                    slow_task.enter_progress_frame(1)

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
                                    for slot_i, mi_name in enumerate(mat_overrides):
                                        if mi_name not in mat_cache:
                                            mat_cache[mi_name] = load_asset(f"{base_pkg}/Materials/{mi_name}")
                                        mi_obj = mat_cache[mi_name]
                                        if isinstance(mi_obj, unreal.MaterialInterface):
                                            sm_comp.set_material(slot_i, mi_obj)
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
