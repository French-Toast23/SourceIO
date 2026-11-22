[![Blender](https://img.shields.io/badge/Blender->=_4.5-orange?logo=blender&logoColor=white)](https://www.blender.org/download)
[![Download](https://img.shields.io/github/downloads/REDxEYE/SourceIO/total?label=Download&logo=data:image/svg%2bxml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyNCIgaGVpZ2h0PSIyNCIgdmlld0JveD0iMCAwIDI0IDI0Ij4KCTxwYXRoIGZpbGw9IndoaXRlIiBkPSJNMTEuMiAwYS44LjggMCAwIDAtLjguOHYxMS40TDcuMjYgOS40NGEuODAzLjgwMyAwIDAgMC0xLjEzLjA3NGwtMS4wNSAxLjJhLjguOCAwIDAgMCAuMDczIDEuMTNsNi4zMyA1LjU0YS43OTUuNzk1IDAgMCAwIDEuMDUgMGw2LjMyLTUuNTRhLjguOCAwIDAgMCAuMDc0LTEuMTNsLTEuMDUtMS4yYS44MDQuODA0IDAgMCAwLTEuMTMtLjA3NGwtMy4xNCAyLjc2Vi44YS44LjggMCAwIDAtLjgtLjh6bS04IDIwLjhhLjguOCAwIDAgMC0uOC44djEuNmEuOC44IDAgMCAwIC44LjhoMTcuNmEuOC44IDAgMCAwIC44LS44di0xLjZhLjguOCAwIDAgMC0uOC0uOHoiPjwvcGF0aD4KPC9zdmc+Cg==)](https://github.com/REDxEYE/SourceIO/archive/refs/heads/master.zip)
[![Discord](https://img.shields.io/discord/554001378532655104?label=Chat&logo=discord&logoColor=white)](https://discord.gg/SF82W6aZ67)

# SourceIO
SourceIO is a Blender(4.5+) addon for importing source engine textures/models/maps
Our Discord server https://discord.gg/SF82W6aZ67

Current TODO list -> [TODO.md](TODO.md)

Small WIKI -> [WIKI](https://github.com/REDxEYE/SourceIO/wiki)
# Usage
In order to find the import tools you simply need to go to File>Import>Source Engine Assets
![](https://cdn.discordapp.com/attachments/786989240529059900/1143975506589515886/image.png)
# Credits
* [datamodel.py](https://github.com/Artfunkel/BlenderSourceTools/blob/master/io_scene_valvesource/datamodel.py) by [Artfunkel](https://github.com/Artfunkel)
* [ValveResourceFormat](https://github.com/SteamDatabase/ValveResourceFormat) For initial research on Source2 file formats
* [BlenderVertexLitGeneric](https://github.com/syborg64/BlenderVertexLitGeneric) Shader nodegroup by [syborg64](https://github.com/syborg64)
* [equilib](https://github.com/haruishi43/equilib) Cubemap to equirectangular converter by [haruishi43](https://github.com/haruishi43/equilib)
* [HFSExtract](https://github.com/yretenai/HFSExtract) HFS extractor that was used to write native decryptor by [yretenai](https://github.com/yretenai)
* Idea for better HWM expression handling by [hisanimations](https://youtube.com/c/hisanimations)
# Supported formats (v5.3.0):

## Source 1
| File Type | Contents                          | Import             | Export            |
| ------    | ------                            | ------             | ------            |
| .MDL      | Model                             | :heavy_check_mark: | Model graph (native DMX/SMD; StudioMDL) |
| .BSP      | Map Files (Compiled)              | :heavy_check_mark: | Not Planned       |
| .VMF      | Map Files (Hammer Format)         | Not Planned        | Not Planned       |
| .VTF      | Textures                          | :heavy_check_mark: | :heavy_check_mark: |
| .VMT      | Materials                         | :heavy_check_mark: | Model graph |

## Source 2
| File Type | Contents                          | Import              | Export       |
|-----------| ------                            |---------------------|--------------|
| .VMDL     | Model                             | :heavy_check_mark:  | Not Planned  |
| .VMAP     | Map Files (Compiled)              | :heavy_check_mark:  | Not Planned  |
| .VMAP     | Map Files (Hammer Format)         | Not Planned         | Not Planned  |
| .VTEX     | Textures                          | :heavy_check_mark:  | :x:          |
| .VMAT     | Materials                         | :heavy_check_mark:  | :x:          |

## Supported games
| Game      | Status                                                                                          |
|-----------| ------------------------------------------------------------------------------------------------|
| CSGO              | Partial support: models, maps(not all entities), textures, materials                    | 
| TF2               | Full support                                                                            |
| Source FilmMaker  | Full support                                                                            |
| Garry's Mod       | Full support                                                                            |
| HL2 and episodes  | Full support                                                                            |
| Portal 1/2        | Full support                                                                            |
| L4D2              | Full support(expect infected materials)                                                 | 
| Vindictus         | Partial support: (models, maps(not all entities), textures, materials(not all shaders)  | 
| BlackMesa         | Full support                                                                            |
| Titanfall 1       | Partial support: maps(not all entities), models, textures, materials                    |
| Counter Strike 2  | Partial support: (models, maps(not all entities), textures, materials(not all shaders, require blender 3.4 or newer)  |
| Half-Life: Alyx   | Partial support: (models, maps(not all entities), textures, materials(not all shaders)  |
| S&BOX             | Partial support: Waiting for full release                                               |
| Aperture Desk Job | Partial support: (models, maps(not all entities), textures, materials(not all shaders)  |

---

## UEFN (Unreal Editor for Fortnite) Export

The `UNREAL-UEFN` branch provides an automated export and import pipeline to transfer Source 1 (Team Fortress 2) maps, static props, textures, materials, and audio into **Unreal Editor for Fortnite (UEFN)** via the **UEFN Toolbelt**.

### Features:
- **Meshes (`Meshes/`)**: Exports deduplicated static props as FBX assets with automatic convex collision hulls (`UCX_{mesh}_01`).
- **Textures (`Textures/`)**: Converts VTF textures to PNG/TGA with normal map green channel inversion (OpenGL to DirectX convention), sRGB management, and power-of-two memory budget clamping.
- **Materials (`Materials/`)**: Translates VMT shaders (`LightmappedGeneric`, `VertexLitGeneric`, `WorldVertexTransition`, `Water`, etc.) into structured UEFN Material Instance specifications.
- **Audio (`Audio/`)**: Resolves and extracts TF2 soundscript events into UEFN-compliant 16-bit PCM WAV assets and emits `docs/AUDIO_MAPPING.md`.
- **Manifest (`map_manifest.json`)**: Emits complete scene transforms (Source to Unreal cm, left-handed Z-up) and catalogs all gameplay entities (spawns, capture points, flags, resupply cabinets).
- **Validation Report**: Generates `export_report.md` and `export_report.json` guaranteeing zero silent drops.
- **UEFN Toolbelt Integration**: Includes `tf2_map_importer.py` for automated one-click actor placement and asset importation inside UEFN.

> **Notice:** Team Fortress 2 assets and intellectual property belong to Valve Corporation; this tool and its exported outputs are strictly for private, non-commercial educational research and personal prototyping only. No publishing features are provided or supported.

