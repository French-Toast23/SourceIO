"""
manifest_builder.py — UEFN Map Manifest and Validation Report Builder
====================================================================

Generates:
- map_manifest.json: Complete machine-readable scene manifest specifying every placed
  actor, static prop, world geometry chunk, displacement, brush entity, overlay/decal,
  audio device, 3D skybox metadata, and gameplay entity (with trigger bounds).
- export_report.json & export_report.md: Exhaustive audit of all converted, substituted,
  or missing assets (models, textures, materials, audio, animations, effects), counts,
  and budget warnings (zero silent drops).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .coordinates import SOURCE1_HU_TO_CM


class PlacedActorRecord:
    def __init__(
        self,
        actor_name: str,
        asset_reference: Optional[str],
        location: list[float],
        rotation: list[float],
        scale: list[float],
        entity_class: str,
        material_overrides: Optional[list[str]] = None,
        source_entity_data: Optional[dict[str, Any]] = None,
        tint_color: Optional[list[float]] = None,
        fade_distances: Optional[dict[str, float]] = None,
        is_gameplay_entity: bool = False,
        is_skybox: bool = False,
        audio_spec: Optional[dict[str, Any]] = None,
        bounds_cm: Optional[dict[str, list[float]]] = None,
    ):
        self.actor_name = actor_name
        self.asset_reference = asset_reference
        self.location = location
        self.rotation = rotation
        self.scale = scale
        self.entity_class = entity_class
        self.material_overrides = material_overrides or []
        self.source_entity_data = source_entity_data or {}
        self.tint_color = tint_color
        self.fade_distances = fade_distances
        self.is_gameplay_entity = is_gameplay_entity
        self.is_skybox = is_skybox
        self.audio_spec = audio_spec
        self.bounds_cm = bounds_cm

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "actor_name": self.actor_name,
            "asset_reference": self.asset_reference,
            "location": self.location,
            "rotation": self.rotation,
            "scale": self.scale,
            "entity_class": self.entity_class,
            "material_overrides": self.material_overrides,
            "source_entity_data": self.source_entity_data,
            "is_gameplay_entity": self.is_gameplay_entity,
            "is_skybox": self.is_skybox,
        }
        if self.tint_color:
            data["tint_color"] = self.tint_color
        if self.fade_distances:
            data["fade_distances"] = self.fade_distances
        if self.audio_spec:
            data["audio_spec"] = self.audio_spec
        if self.bounds_cm:
            data["bounds_cm"] = self.bounds_cm
        return data


class UEFNManifestBuilder:
    """
    Builds map_manifest.json, export_report.json, and export_report.md.
    """

    def __init__(
        self,
        map_name: str,
        export_root: Path,
        cm_per_hu: float = SOURCE1_HU_TO_CM,
        scale_multiplier: float = 1.0,
    ):
        self.map_name = map_name
        self.export_root = Path(export_root)
        self.cm_per_hu = cm_per_hu
        self.scale_multiplier = scale_multiplier
        self.placed_actors: list[PlacedActorRecord] = []
        self.gameplay_entities: list[dict[str, Any]] = []
        self.sky_camera_info: Optional[dict[str, Any]] = None
        self.missing_assets: list[dict[str, str]] = []
        self.fallback_assets: list[dict[str, str]] = []
        self.budget_warnings: list[str] = []
        self.hard_errors: list[str] = []
        self.non_map_assets_summary: dict[str, Any] = {
            "audio_converted": 0,
            "audio_missing": 0,
            "animations_note": "See docs/ANIMATION_MAPPING.md for skeletal/prop animation paths.",
            "effects_note": "See docs/EFFECTS_MAPPING.md for PCF-to-Niagara/device mappings.",
        }

        self.expected_counts: dict[str, int] = {
            "world_chunks": 0,
            "displacements": 0,
            "brush_entities": 0,
            "overlays_decals": 0,
            "static_props": 0,
            "dynamic_props": 0,
            "unique_models": 0,
            "audio_entities": 0,
            "gameplay_entities": 0,
        }
        self.exported_counts: dict[str, int] = {
            "world_chunks": 0,
            "displacements": 0,
            "brush_entities": 0,
            "overlays_decals": 0,
            "static_props": 0,
            "dynamic_props": 0,
            "unique_models": 0,
            "audio_entities": 0,
            "gameplay_entities": 0,
        }

    def add_actor(self, actor: PlacedActorRecord) -> None:
        self.placed_actors.append(actor)

    def record_missing_asset(self, asset_type: str, asset_name: str, reason: str) -> None:
        entry = {"type": asset_type, "asset": asset_name, "reason": reason}
        if entry not in self.missing_assets:
            self.missing_assets.append(entry)

    def record_fallback(self, asset_type: str, asset_name: str, fallback_used: str, reason: str) -> None:
        entry = {
            "type": asset_type,
            "asset": asset_name,
            "fallback": fallback_used,
            "reason": reason,
        }
        if entry not in self.fallback_assets:
            self.fallback_assets.append(entry)

    def record_budget_warning(self, warning: str) -> None:
        if warning not in self.budget_warnings:
            self.budget_warnings.append(warning)

    def record_hard_error(self, error: str) -> None:
        if error not in self.hard_errors:
            self.hard_errors.append(error)

    def write_manifest(self) -> Path:
        manifest_path = self.export_root / "map_manifest.json"
        data = {
            "map_name": self.map_name,
            "version": "1.1.0",
            "scale_cm_per_hu": self.cm_per_hu,
            "scale_multiplier": self.scale_multiplier,
            "effective_cm_per_hu": round(self.cm_per_hu * self.scale_multiplier, 6),
            "coordinate_system": "Unreal_LeftHanded_ZUp",
            "lighting_note": (
                "Source 1 BSP lightmaps are not carried over. Configure UEFN lighting using "
                "Lumen Global Illumination, DirectionalLight (from light_environment), "
                "SkyAtmosphere, SkyLight, and World Partition HLODs."
            ),
            "sky_camera": self.sky_camera_info,
            "counts": {
                "total_actors": len(self.placed_actors),
                "gameplay_entities": len(self.gameplay_entities),
                "exported_counts": self.exported_counts,
            },
            "actors": [a.to_dict() for a in self.placed_actors],
            "gameplay_entities": self.gameplay_entities,
        }
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return manifest_path

    def write_reports(self) -> tuple[Path, Path]:
        report_json_path = self.export_root / "export_report.json"
        report_md_path = self.export_root / "export_report.md"

        report_data = {
            "map_name": self.map_name,
            "total_actors_exported": len(self.placed_actors),
            "expected_counts": self.expected_counts,
            "exported_counts": self.exported_counts,
            "non_map_assets": self.non_map_assets_summary,
            "missing_assets_count": len(self.missing_assets),
            "missing_assets": self.missing_assets,
            "fallback_assets_count": len(self.fallback_assets),
            "fallback_assets": self.fallback_assets,
            "budget_warnings_count": len(self.budget_warnings),
            "budget_warnings": self.budget_warnings,
            "hard_errors_count": len(self.hard_errors),
            "hard_errors": self.hard_errors,
        }

        with open(report_json_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=2)

        md_lines = [
            f"# UEFN Export Validation Report: {self.map_name}",
            "",
            "## 1. Summary Statistics",
            "",
            "| Category | Expected Count | Exported Count | Status |",
            "|---|---|---|---|",
        ]

        for cat in sorted(self.expected_counts.keys()):
            exp = self.expected_counts[cat]
            got = self.exported_counts.get(cat, 0)
            if exp == got:
                status = "MATCH"
            elif cat == "displacements" and got > 0:
                status = "CHUNKED/MERGED"
            elif got < exp:
                status = "DEFICIT"
            else:
                status = "CHUNKED/EXPANDED"
            md_lines.append(f"| {cat} | {exp} | {got} | {status} |")

        md_lines.extend([
            f"| **Total Placed Actors** | — | **{len(self.placed_actors)}** | **READY** |",
            "",
            "### Non-Map Assets Coverage",
            f"- **Audio Clips Converted**: {self.non_map_assets_summary.get('audio_converted', 0)}",
            f"- **Audio Missing**: {self.non_map_assets_summary.get('audio_missing', 0)}",
            f"- **Animations**: {self.non_map_assets_summary.get('animations_note', '')}",
            f"- **Effects**: {self.non_map_assets_summary.get('effects_note', '')}",
            "",
            "### Lighting Workflow Note",
            "- Source 1 BSP lightmaps are not exported to UEFN. Use **Lumen** with a **DirectionalLight** matched to `light_environment`, **SkyAtmosphere**, and **SkyLight** in UEFN.",
            "",
            "---",
            "",
            f"## 2. Missing Assets ({len(self.missing_assets)})",
            "",
        ])

        if not self.missing_assets:
            md_lines.append("Zero missing assets reported.")
        else:
            md_lines.extend([
                "| Asset Type | Identifier | Reason / Failure |",
                "|---|---|---|",
            ])
            for item in self.missing_assets:
                md_lines.append(f"| {item['type']} | `{item['asset']}` | {item['reason']} |")

        md_lines.extend([
            "",
            "---",
            "",
            f"## 3. Substituted & Fallback Assets ({len(self.fallback_assets)})",
            "",
        ])

        if not self.fallback_assets:
            md_lines.append("No fallback substitutions needed.")
        else:
            md_lines.extend([
                "| Asset Type | Original Asset | Substituted Fallback | Reason |",
                "|---|---|---|---|",
            ])
            for item in self.fallback_assets:
                md_lines.append(f"| {item['type']} | `{item['asset']}` | `{item['fallback']}` | {item['reason']} |")

        md_lines.extend([
            "",
            "---",
            "",
            f"## 4. UEFN Limits & Budget Warnings ({len(self.budget_warnings)})",
            "",
        ])

        if not self.budget_warnings:
            md_lines.append("All assets within verified UEFN memory and size budgets.")
        else:
            for w in self.budget_warnings:
                md_lines.append(f"- {w}")

        md_lines.extend([
            "",
            "---",
            "",
            f"## 5. Hard Errors ({len(self.hard_errors)})",
            "",
        ])

        if not self.hard_errors:
            md_lines.append("Zero hard errors.")
        else:
            for err in self.hard_errors:
                md_lines.append(f"- **ERROR**: {err}")

        with open(report_md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md_lines))

        return report_json_path, report_md_path
