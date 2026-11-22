"""
UEFN Export Module for SourceIO
===============================
Provides Blender and standalone tools for exporting Source 1 (TF2) maps, models,
materials, textures, and audio to Unreal Editor for Fortnite (UEFN).
"""

from .coordinates import (
    source_to_unreal_position,
    unreal_to_source_position,
    source_to_unreal_rotation,
    unreal_to_source_rotation,
    source_to_unreal_scale,
)
from .texture_converter import UEFNTextureConverter, TextureConversionResult
from .material_mapper import UEFNMaterialMapper, UEFNMaterialSpec
from .mesh_exporter import UEFNMeshExporter, ExportedMeshInfo
from .audio_exporter import UEFNAudioExporter
from .manifest_builder import UEFNManifestBuilder, PlacedActorRecord
from .export_pipeline import UEFNExportPipeline, UEFNExportOptions
from .operators import register as register_operators, unregister as unregister_operators, classes as uefn_classes


def register():
    register_operators()


def unregister():
    unregister_operators()
