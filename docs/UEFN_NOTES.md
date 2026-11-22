# UEFN Integration & Architecture Notes

This document details the architecture of **SourceIO**, its Source 1 (TF2) asset pipelines, and the constraints, limits, and integration surfaces for exporting Source 1 maps and assets to **Unreal Editor for Fortnite (UEFN)**.

---

## 1. SourceIO Architecture Summary (TF2 / BSP / MDL / VMT)

SourceIO is designed with a two-tier architecture:
- `library/`: Pure Python / Cython / C-extension data parsing layer. Independent of Blender UI. Contains binary parsers, math structures, and content managers.
- `blender_bindings/`: Blender operator, mesh construction, material node setup, and scene integration layer.

### 1.1 Team Fortress 2 (Source 1) Pipeline Overview

```
                      ┌──────────────────────┐
                      │ TF2 Game Files (VPK) │
                      │  or BSP PakFile L40  │
                      └──────────┬───────────┘
                                 │
     ┌───────────────────────────┼───────────────────────────┐
     │                           │                           │
┌────▼─────────────┐     ┌───────▼──────────┐      ┌─────────▼───────────┐
│   BSP Map Lumps  │     │   MDL v49 Models │      │    VMT Materials    │
│  (Faces, Brushes,│     │    + VVD / VTX   │      │    + VTF Textures   │
│   Displacements, │     │    + PHY Mesh    │      │ (LightmappedGeneric │
│   Static Props,  │     │  (Meshes, Bones, │      │  VertexLitGeneric   │
│     Entities)    │     │   Anims, Skins)  │      │  WorldVertexTrans)  │
└────┬─────────────┘     └───────┬──────────┘      └─────────┬───────────┘
     │                           │                           │
     └───────────────────────────┼───────────────────────────┘
                                 │
                   ┌─────────────▼─────────────┐
                   │   Blender Scene Graph     │
                   │ (Collections, Transforms) │
                   └─────────────┬─────────────┘
                                 │
                   ┌─────────────▼─────────────┐
                   │    UEFN Export Pipeline   │
                   │  - Meshes/ (FBX + UCX)    │
                   │  - Textures/ (PNG/TGA)    │
                   │  - Materials/ (Defs)      │
                   │  - Audio/ (WAV)           │
                   │  - map_manifest.json      │
                   └─────────────┬─────────────┘
                                 │
                   ┌─────────────▼─────────────┐
                   │ UEFN Toolbelt MCP Importer│
                   │ (Spawns Actors, Materials)│
                   └───────────────────────────┘
```

### 1.2 TF2 Asset Specifics & Code Paths

1. **Content Management (`library/shared/content_manager/`)**:
   - `ContentManager`: Hierarchical provider resolution. In TF2, search order priority:
     1. BSP embedded `PakFile` (Lump 40) — custom textures, sounds, and props embedded by map makers.
     2. Mod / map custom game folders (`tf/custom/`).
     3. VPK multi-chunk archives (`tf/tf2_misc_dir.vpk`, `tf/tf2_textures_dir.vpk`, `tf/tf2_sound_misc_dir.vpk`, `tf/tf2_sound_vo_english_dir.vpk`).
     4. Base Source engine files.

2. **BSP Map File (`library/source1/bsp/` & `blender_bindings/source1/bsp/`)**:
   - `bsp_file.py`: Reads the 64 lumps of Source 1 BSP (version 20 for TF2):
     - **Lump 0 (Entities)**: KeyValues text format for all placed entities (`info_player_teamspawn`, `team_control_point`, `func_detail`, `prop_static`, `ambient_generic`, etc.).
     - **Lump 7 (Faces) & Lump 1 (Planes) & Lump 3 (Vertices)**: World geometry polygonal faces.
     - **Lump 26 (DispInfo) & Lump 33 (DispVerts) & Lump 48 (DispTris)**: Displacement terrain surfaces with power-of-two vertex grids ($2^N+1$) and alpha blend factors for terrain texturing.
     - **Lump 35 (GameLump)**: Contains `sprp` (Static Prop Lump v4, v5, v6, v10, v11) with static prop definitions: model index, origin, angles, skin, scale, fade distances, collision flags.
     - **Lump 45 (Overlays) & Decals**: Decals and wall overlays with UVs and projection vectors.
     - **Lump 40 (PakFile)**: ZIP archive containing embedded custom assets.
   - `import_bsp.py`: Coordinates the import process into Blender. Creates collections, generates mesh geometry, builds displacement terrain, places static and dynamic props, and attaches materials.

3. **MDL Model Loader (`library/models/mdl/v49/`, `library/models/vtx/`, `library/models/vvd/`, `library/models/phy/`)**:
   - TF2 uses **StudioMDL version 48/49**.
   - `vvd`: Flat vertex array (positions, normals, tangents, UV coordinates, bone weights).
   - `vtx`: Stripped and indexed triangle lists per Level-of-Detail (LOD 0 through LOD 7), bodyparts, and mesh subsets.
   - `phy`: Collision mesh hulls and joint constraints (`IVPS` format).
   - `blender_bindings/models/common.py`: Instantiates armatures, meshes, shape keys (flex controllers for facial expressions), and attaches materials based on material search paths ($cdmaterials).

4. **VMT & VTF Shaders (`library/source1/vmt/`, `library/source1/vtf/`, `blender_bindings/material_loader/`)**:
   - `vmt`: KeyValues definition specifying shader name and properties.
     - `LightmappedGeneric`: Diffuse (`$basetexture`), normal map (`$bumpmap`), self-illumination (`$selfillum`), specular/envmap (`$envmap`).
     - `VertexLitGeneric`: Model shader with phong specular (`$phong`, `$phongexponent`, `$phongboost`), rim lighting (`$rimlight`), and half-lambert diffuse.
     - `WorldVertexTransition`: Dual-texture terrain blend shader (`$basetexture` + `$basetexture2`) modulated by displacement vertex alpha and `$blendmodulatetexture`.
     - `Water`: Real-time reflection/refraction water shader with normal maps (`$normalmap`), flowmaps, and fog params.
   - `vtf`: Texture binary containing mipmaps, format headers (DXT1, DXT5, BGR888, BGRA8888, UV88), and flags.

---

## 2. UEFN Technical Constraints & Limits Verification

Constraints verified against official Epic Games UEFN Documentation ([Epic Developer Community - UEFN Documentation](https://dev.epicgames.com/documentation/en-us/uefn/)) and the UEFN Toolbelt technical specifications ([UEFN Toolbelt Architecture & Quirks](https://github.com/undergroundrap/UEFN-TOOLBELT)).

### 2.1 File Formats & Import Restrictions
- **Static Meshes**: FBX (2013–2020 format, binary) or glTF/GLB. Must be imported as Static Mesh assets (`/Game/Meshes/SM_*`).
- **Skeletal Meshes & Animations**: FBX with skeletal armature and skin weights. Max 4 bone influences per vertex for standard performance tier; 8 supported under desktop/high tier.
- **Textures**: PNG, TGA, EXR, HDR. VTF is NOT natively supported by Unreal Engine and must be decoded to uncompressed PNG/TGA before import.
- **Audio**: Uncompressed 16-bit or 24-bit PCM WAV, 44.1 kHz or 48 kHz (48 kHz recommended). Source engine `.wav` and `.mp3` files must be normalized and converted to standard 44.1/48 kHz 16-bit WAV.
- **Materials**: UEFN does not support VMT files directly. Materials must be authored as Unreal Material assets (`M_*`) or Material Instances (`MI_*`) with parameter collections.

### 2.2 Geometry & Triangle Limits
- **Nanite**: Static Meshes in UEFN support Nanite virtualized geometry, eliminating manual LOD authoring for complex static props.
- **Non-Nanite & Mobile/Switch Targets**: Props with transparency, two-sided foliage, or dynamic deformation cannot use Nanite and should stay within 5,000–25,000 triangles per asset.
- **Collision Meshes**: Custom collision in FBX must follow the standard Unreal prefix naming convention:
  - `UCX_MeshName_##` for convex collision hulls.
  - `UBX_MeshName_##` for box collision.
  - `USP_MeshName_##` for sphere collision.
- **Coordinate Conversion**:
  - Source Engine: 1 Source Unit = 1 inch = 2.54 cm. Right-handed coordinate system (X Forward, Y Left, Z Up).
  - Unreal Engine: 1 Unreal Unit (uu) = 1 centimeter (cm). Left-handed coordinate system (X Forward, Y Right, Z Up).
  - Conversion transform:
    $$\text{Position}_{UE} = (X_{Source} \times 2.54, -Y_{Source} \times 2.54, Z_{Source} \times 2.54)$$
    $$\text{Rotation}_{UE}: \text{Pitch}_{UE} = \text{Pitch}_{Source}, \text{Yaw}_{UE} = -\text{Yaw}_{Source}, \text{Roll}_{UE} = -\text{Roll}_{Source}$$

### 2.3 Texture Conventions & Limits
- **Max Resolution**: 2048x2048 or 4096x4096 (power-of-two dimensions required for streaming and mipmap generation). Textures exceeding 4096 must be downscaled.
- **Color Spaces**:
  - Base Color / Albedo / Emissive: sRGB = True.
  - Normal Map, Roughness, Metallic, Ambient Occlusion, Masks: Linear / Non-Color (sRGB = False).
- **Normal Map Green Channel**:
  - Source Engine uses **OpenGL convention** (Y+ Up).
  - Unreal Engine uses **DirectX convention** (Y- Down).
  - When converting VTF normal maps for UEFN, the green channel must be inverted ($G_{UE} = 1.0 - G_{Source}$).

### 2.4 Project Budgets & Memory
- **Memory Thermometer**: UEFN enforces a strict island memory budget of 100,000 units.
- **World Partition & Streaming**: Large maps must use World Partition or be chunked into sensible streaming grid cells.
- **Prop Deduplication**: Unique static meshes consume substantial memory; instanced meshes (HISM / ISM) or repeated prop actor instances share geometry memory. Every unique prop model from TF2 must be exported once and instanced in the manifest.

### 2.5 UEFN Python API Capabilities & Sandboxing
- **Access Level**: Editor Scripting only (`unreal.EditorLevelLibrary`, `unreal.EditorAssetLibrary`, `unreal.EditorActorSubsystem`).
- **Main Thread Execution**: Python runs on the main Slate render tick; long blocking loops freeze the editor. Batching and ScopedEditorTransaction are mandatory.
- **What Python CAN do**:
  - Import assets (`unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks(...)`).
  - Spawn StaticMeshActors, Verse devices, and Creative devices in the level.
  - Set actor transforms (Location, Rotation, Scale).
  - Set StaticMesh and Material overrides on mesh components.
  - Create and configure Material Instances (`unreal.MaterialInstanceConstant`).
- **What Python CANNOT do**:
  - Modify locked editor-managed C++ struct properties (e.g. raw `FBodyInstance.bSimulatePhysics` reflection).
  - Run arbitrary unauthenticated network commands inside the sandbox.
  - Execute arbitrary runtime gameplay code during live island play (runtime gameplay is strictly Verse).
