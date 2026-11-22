# Issue and Pull Request Triage

This document tracks all open issues and pull requests from upstream `REDxEYE/SourceIO` (evaluated as of November 2026), triaged for relevance to exporting Team Fortress 2 (Source 1) maps, props, textures, and entities into Unreal Editor for Fortnite (UEFN).

---

## Triage Table

| Number | Title | Type | Status | Relevance to TF2 → UEFN | Decision | One-line Reason |
|:---|:---|:---|:---|:---:|:---:|:---|
| **PR #114** | Material Loader : add proper 4wayblend | PR | Open (Draft) | None | **Skip** | Stale draft from 2021 targeting obsolete `bpy_utilities`; TF2 uses 2-way `WorldVertexTransition`, not 4-way blend. |
| **#466** | Missing geometry | Issue | Open | Low | **Skip** | Incomplete report on CS:S map; no repro or response from reporter on current SourceIO. |
| **#464** | Waiting for S&Box compatibility | Issue | Open | None | **Skip** | Targets Facepunch S&Box (Source 2); irrelevant to Source 1 / TF2 BSP pipeline. |
| **#431** | Source 2 Map Import Does not import materials Properly | Issue | Open | None | **Skip** | S2 Half-Life: Alyx tint and shader graph issue; does not affect Source 1 VMT/VTF. |
| **#380** | workshop maps dont work | Issue | Open | None | **Skip** | Specific to GoldSrc (CS 1.6) workshop map packaging; unrelated to TF2 VPK/BSP. |
| **#376** | [Feature request] Baked lighting import | Issue | Open | Medium | **Skip** | UEFN uses Lumen / Unreal dynamic and baked lighting; Source 1 lightmaps are intentionally omitted per UEFN design spec. |
| **#303** | "IndexError: list index out of range" error when importing custom maps | Issue | Open | None | **Skip** | GoldSrc custom map texture palette overflow bug (`de_dust2_2020.bsp`); unrelated to Source 1 BSP. |
| **#290** | Physics model imported flipped and out of scale | Issue | Open | High | **Fix myself** | Source 1 `.phy` collision meshes can import inverted or misaligned; addressed in UEFN exporter by generating correct `UCX_` collision hulls. |
| **#288** | Add SourceIO compatibility for HL1 Console Ports' models and maps | Issue | Open | None | **Skip** | Targets Dreamcast/PS2 GoldSrc assets; irrelevant to TF2. |
| **#232** | Unable to enable addon after renaming blender.exe | Issue | Open | Low | **Skip** | Artifact of custom binary naming check when distinguishing library vs addon mode. |
| **#153** | 27-37 mdl format not supported | Issue | Open | None | **Skip** | Targets 2001 Half-Life 2 Beta pre-release models; TF2 uses modern MDL v48/v49. |
| **#117** | Vampire The Masquerade Importing | Issue | Open | None | **Skip** | Specific to Troika's early 2004 Source branch; unrelated to Valve's TF2 branch. |
| **#107** | Vindictus Models and/or Maps refuse to open | Issue | Open | None | **Skip** | Specific to Nexon's proprietary custom Source engine branch; unrelated to TF2. |
| **#106** | s&box map support | Issue | Open | None | **Skip** | Targets S&Box compile format changes; unrelated to TF2 Source 1. |

---

## Targeted Improvements & Local Fixes

1. **Blender 4.2 LTS Compatibility (`__init__.py`)**:
   - Upstream commit `a50cea6e` erroneously bumped the minimum Blender version check to `(4, 5, 0)` under the mistaken assumption that Blender 4.5/4.6 is the oldest release with Python 3.11.
   - Blender 4.2 LTS ships with Python 3.11.7 and loads the bundled `pylib.pyd` (ABI3) binary without issues.
   - **Fix applied**: Set minimum version check to `bpy.app.version < (4, 2, 0)` to support Blender 4.2 LTS.

2. **Collision Geometry for UEFN (`#290` resolution)**:
   - For UEFN Static Mesh actors, collision should not rely on problematic `.phy` meshes which often have reversed winding or coordinate flip issues.
   - **Fix applied**: The UEFN exporter generates clean convex collision hulls (`UCX_{mesh_name}_{idx:02d}`) or enables UEFN project-level simplified collision on import.
