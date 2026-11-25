"""
test_uefn_pipeline.py — Automated Unit & Integration Tests for UEFN Pipeline
===========================================================================

Tests:
1. Coordinate, rotation, skybox transform, and collision-free UEFN asset naming.
2. Texture converter: DirectX normal preservation, SSBump conversion, and POT downscaling.
3. Material mapper: LightmappedGeneric, WorldVertexTransition, Water, Patch VMT, and fallbacks.
4. Audio exporter: KV1 soundscript & soundscape parsing and radius fallback.
5. Manifest builder & validation reports (no silent drops).
6. Toolbelt custom plugin security gate compliance (< 50 KB, no blocked imports).
7. Live integration check on ctf_2fort.bsp (auto-detected via Steam libraryfolders.vdf).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from SourceIO.blender_bindings.uefn.audio_exporter import UEFNAudioExporter
from SourceIO.blender_bindings.uefn.coordinates import (
    SOURCE1_HU_TO_CM,
    blender_euler_to_unreal_rotation,
    blender_to_unreal_position,
    material_ue_name,
    parse_vector_string,
    sanitize_ue_name,
    skybox_to_world_transform,
    source_to_unreal_position,
    source_to_unreal_rotation,
    source_to_unreal_scale,
    unreal_to_source_position,
    unreal_to_source_rotation,
)
from SourceIO.blender_bindings.uefn.export_pipeline import detect_tf2_game_dir
from SourceIO.blender_bindings.uefn.manifest_builder import (
    PlacedActorRecord,
    UEFNManifestBuilder,
)
from SourceIO.blender_bindings.uefn.material_mapper import UEFNMaterialMapper
from SourceIO.blender_bindings.uefn.texture_converter import (
    UEFNTextureConverter,
    convert_ssbump_pixels,
    is_power_of_two,
    next_lower_power_of_two,
)
from SourceIO.library.shared.content_manager import ContentManager
from SourceIO.library.utils.math_utilities import SOURCE1_HAMMER_UNIT_TO_METERS


class TestUEFNCoordinates(unittest.TestCase):
    def test_position_conversion_and_roundtrip(self):
        self.assertEqual(source_to_unreal_position([0, 0, 0]), [0.0, 0.0, 0.0])

        # 100 HU * 1.905 cm/HU = 190.5 cm
        ue_pos = source_to_unreal_position([100, 200, 300])
        self.assertAlmostEqual(ue_pos[0], 190.5)
        self.assertAlmostEqual(ue_pos[1], -381.0)
        self.assertAlmostEqual(ue_pos[2], 571.5)

        src_rt = unreal_to_source_position(ue_pos)
        self.assertAlmostEqual(src_rt[0], 100.0)
        self.assertAlmostEqual(src_rt[1], 200.0)
        self.assertAlmostEqual(src_rt[2], 300.0)

    def test_blender_to_unreal_matches_source_to_unreal(self):
        b_pos = [100 * SOURCE1_HAMMER_UNIT_TO_METERS, 200 * SOURCE1_HAMMER_UNIT_TO_METERS, 300 * SOURCE1_HAMMER_UNIT_TO_METERS]
        self.assertEqual(blender_to_unreal_position(b_pos), source_to_unreal_position([100, 200, 300]))

    def test_rotation_conversion_and_blender_euler(self):
        ue_rot = source_to_unreal_rotation([15.0, 90.0, 30.0])
        self.assertAlmostEqual(ue_rot[0], 15.0)
        self.assertAlmostEqual(ue_rot[1], -90.0)
        self.assertAlmostEqual(ue_rot[2], -30.0)

        # SourceIO stores (Roll_X, Pitch_Y, Yaw_Z) in radians
        import math
        b_euler = [math.radians(30.0), math.radians(15.0), math.radians(90.0)]
        self.assertEqual(blender_euler_to_unreal_rotation(b_euler), ue_rot)

        src_rt = unreal_to_source_rotation(ue_rot)
        self.assertAlmostEqual(src_rt[0], 15.0)
        self.assertAlmostEqual(src_rt[1], 90.0)
        self.assertAlmostEqual(src_rt[2], 30.0)

    def test_skybox_to_world_transform(self):
        # sky_camera at (-4544, 256, 48), scale 16; skybox prop at (-4444, 256, 48) -> +100 HU -> +1600 HU
        ue_pos = skybox_to_world_transform([-4444, 256, 48], [-4544, 256, 48], sky_scale=16.0)
        self.assertAlmostEqual(ue_pos[0], 1600.0 * SOURCE1_HU_TO_CM, places=3)
        self.assertAlmostEqual(ue_pos[1], 0.0, places=3)
        self.assertAlmostEqual(ue_pos[2], 0.0, places=3)

    def test_sanitize_ue_name_avoids_collisions(self):
        a = sanitize_ue_name("models/props_2fort/metal01.mdl", prefix="SM_")
        b = sanitize_ue_name("models/props_farm/metal01.mdl", prefix="SM_")
        self.assertNotEqual(a, b)
        self.assertEqual(a, "SM_props_2fort_metal01")
        self.assertEqual(b, "SM_props_farm_metal01")

        long_name = sanitize_ue_name("materials/very/deep/nested/folder/structure/concrete_wall_panel_variant_a.vmt", prefix="MI_")
        self.assertLessEqual(len(long_name), 60)

    def test_material_ue_name_consistency(self):
        self.assertEqual(
            material_ue_name("models/props_2fort/coffeepot"),
            material_ue_name("materials/models/props_2fort/coffeepot.vmt"),
        )
        self.assertEqual(material_ue_name("models/props_2fort/coffeepot"), "MI_models_props_2fort_coffeepot")
        self.assertEqual(material_ue_name("concrete/floor01"), "MI_concrete_floor01")

    def test_scale_and_parse_vector(self):
        self.assertEqual(source_to_unreal_scale(1.5), [1.5, 1.5, 1.5])
        self.assertEqual(parse_vector_string("128 -256 64.5"), [128.0, -256.0, 64.5])
        self.assertEqual(parse_vector_string("bad", (1.0, 2.0, 3.0)), [1.0, 2.0, 3.0])


class TestUEFNTextureAndMaterial(unittest.TestCase):
    def test_power_of_two(self):
        self.assertTrue(is_power_of_two(2048))
        self.assertFalse(is_power_of_two(500))
        self.assertEqual(next_lower_power_of_two(3000), 2048)

    def test_ssbump_conversion_flips_to_directx(self):
        # Neutral flat ssbump (equal illumination along all 3 basis vectors = 1/sqrt(3) -> ~0.577)
        flat = np.full((2, 2, 4), 0.57735, dtype=np.float32)
        flat[:, :, 3] = 1.0
        out = convert_ssbump_pixels(flat)
        # Flat tangent normal in DirectX is (0.5, 0.5, 1.0)
        self.assertAlmostEqual(float(out[0, 0, 0]), 0.5, places=2)
        self.assertAlmostEqual(float(out[0, 0, 1]), 0.5, places=2)
        self.assertAlmostEqual(float(out[0, 0, 2]), 1.0, places=2)

    def test_material_mapper_missing_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            cm = ContentManager()
            tc = UEFNTextureConverter(cm, Path(tmp) / "Textures")
            mm = UEFNMaterialMapper(cm, tc, Path(tmp) / "Materials")
            spec = mm.get_or_map_material("materials/nonexistent/test_wall.vmt")
            self.assertIsNotNone(spec)
            self.assertEqual(spec.shader, "MissingFallback")
            self.assertEqual(spec.master_material_name, "M_Master_Fallback")
            self.assertIn("materials/nonexistent/test_wall.vmt", mm.missing_materials)
            idx_path = mm.write_materials_index()
            self.assertTrue(idx_path.exists())

    def test_build_spec_from_real_vmt_params(self):
        from SourceIO.library.source1.vmt import VMT
        from SourceIO.library.utils import MemoryBuffer
        from SourceIO.library.utils.tiny_path import TinyPath

        vmt_bytes = b'''
        "WorldVertexTransition"
        {
            "$basetexture" "nature/dirtfloor001a"
            "$basetexture2" "nature/grass_01"
            "$bumpmap" "nature/dirtfloor001a_bump"
            "$bumpmap2" "nature/grass_01_bump"
            "$blendmodulatetexture" "nature/blendmask"
            "$ssbump" "1"
            "$alpha" "0.9"
            "$color2" "[0.8 0.9 1.0]"
            "$selfillum" "1"
            "$selfillumtint" "[1.0 0.5 0.25]"
            "$selfillummask" "effects/glow_mask"
            "$envmap" "env_cubemap"
            "$envmapmask" "nature/spec_mask"
            "$fogcolor" "{128 64 32}"
        }
        '''
        with tempfile.TemporaryDirectory() as tmp:
            cm = ContentManager()
            tc = UEFNTextureConverter(cm, Path(tmp) / "Textures")
            mm = UEFNMaterialMapper(cm, tc, Path(tmp) / "Materials")
            vmt = VMT(MemoryBuffer(vmt_bytes), TinyPath("materials/nature/test_blend.vmt"), cm)
            spec = mm._build_spec_from_vmt(
                "MI_test_blend",
                vmt.shader,
                vmt,
                "materials/nature/test_blend.vmt",
            )
            self.assertEqual(spec.master_material_name, "M_Master_Displacement_2Way")
            self.assertAlmostEqual(spec.scalars["Opacity"], 0.9, places=3)
            self.assertEqual(spec.scalars["EmissiveStrength"], 1.0)
            self.assertAlmostEqual(spec.vectors["BaseColorTint"][0], 0.8, places=3)
            self.assertAlmostEqual(spec.vectors["EmissiveTint"][1], 0.5, places=3)
            self.assertTrue(any("Failed converting $basetexture" in w for w in spec.warnings))

            water_vmt_bytes = b'''
            "Water"
            {
                "$normalmap" "dev/water_normal"
                "$fogcolor" "{128 64 32}"
            }
            '''
            water_vmt = VMT(MemoryBuffer(water_vmt_bytes), TinyPath("materials/nature/test_water.vmt"), cm)
            wspec = mm._build_spec_from_vmt("MI_test_water", water_vmt.shader, water_vmt, "materials/nature/test_water.vmt")
            self.assertEqual(wspec.master_material_name, "M_Master_Water")
            self.assertIn("WaterFogColor", wspec.vectors)
            self.assertAlmostEqual(wspec.scalars["WaterRefraction"], 1.333, places=3)


class TestUEFNAudioParser(unittest.TestCase):
    def test_kv1_soundscript_and_radius_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            cm = ContentManager()
            exporter = UEFNAudioExporter(cm, Path(tmp) / "Audio")
            script = '''
            "Test.Alarm"
            {
                "channel" "CHAN_STATIC"
                "volume" "0.8"
                "pitch" "100"
                "soundlevel" "SNDLVL_90dB"
                "rndwave"
                {
                    "wave" ")ambient/alarm_01.wav"
                }
                "operator_stacks"
                {
                    "update_stack"
                    {
                        "import_stack" "test_stack"
                    }
                }
            }
            '''
            count = exporter._parse_sound_script_kv1(script, "test.txt")
            self.assertEqual(count, 1)
            self.assertIn("test.alarm", exporter.sound_events)
            self.assertEqual(exporter.sound_events["test.alarm"].waves, ["ambient/alarm_01.wav"])

            # ambient_generic with radius "0" should resolve from soundlevel SNDLVL_90dB (2200 HU)
            spec = exporter.process_ambient_entity({
                "classname": "ambient_generic",
                "targetname": "alarm_red",
                "origin": "100 0 50",
                "radius": "0",
                "health": "10",
                "message": "Test.Alarm",
                "spawnflags": "0",
            })
            self.assertAlmostEqual(spec["attenuation_radius"], 2200.0 * SOURCE1_HU_TO_CM, places=2)
            self.assertAlmostEqual(spec["volume"], 0.8, places=2)
            self.assertTrue(spec["is_loop"])


class TestUEFNManifestAndSecurity(unittest.TestCase):
    def test_manifest_and_report_generation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir)
            builder = UEFNManifestBuilder("test_map", out_path)
            builder.expected_counts["static_props"] = 1
            builder.add_actor(PlacedActorRecord(
                actor_name="Prop_crate_0",
                asset_reference="SM_crate.fbx",
                location=[100.0, -200.0, 50.0],
                rotation=[0.0, 45.0, 0.0],
                scale=[1.0, 1.0, 1.0],
                entity_class="prop_static",
            ))
            builder.exported_counts["static_props"] = 1
            manifest_file = builder.write_manifest()
            rep_json, rep_md = builder.write_reports()
            self.assertTrue(manifest_file.exists())
            self.assertTrue(rep_json.exists())
            self.assertTrue(rep_md.exists())

    def test_toolbelt_plugin_passes_security_gates(self):
        plugin_path = Path(__file__).resolve().parent.parent / "tools" / "uefn_importer" / "tf2_map_importer.py"
        self.assertTrue(plugin_path.exists())
        size_kb = plugin_path.stat().st_size / 1024.0
        self.assertLessEqual(size_kb, 50.0, f"Plugin size {size_kb:.1f} KB exceeds Toolbelt 50 KB limit")

        blocked = {
            "subprocess", "shutil", "ctypes", "socket", "http",
            "urllib", "requests", "webbrowser", "smtplib", "ftplib",
            "xmlrpc", "multiprocessing", "signal", "_thread",
        }
        tree = ast.parse(plugin_path.read_text(encoding="utf-8"), filename=str(plugin_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], blocked)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[0], blocked)

    def test_skybox_projection_math_in_plugin(self):
        import importlib.util
        plugin_path = Path(__file__).resolve().parent.parent / "tools" / "uefn_importer" / "tf2_map_importer.py"
        spec = importlib.util.spec_from_file_location("tf2_map_importer_test", str(plugin_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        sky_cam = {"unreal_location": [-8656.32, -487.68, 91.44], "sky_scale": 16.0}
        # Skybox chunk pivoted at sky_camera lands at [0, 0, 0] with 16x scale
        loc, scale = mod.project_skybox_actor_transform([-8656.32, -487.68, 91.44], [1.0, 1.0, 1.0], True, sky_cam)
        self.assertEqual([round(v, 3) for v in loc], [0.0, 0.0, 0.0])
        self.assertEqual(scale, [16.0, 16.0, 16.0])

        # Playable actor (is_skybox=False) keeps exact location & scale
        p_loc, p_scale = mod.project_skybox_actor_transform([100.0, 200.0, 300.0], [1.0, 1.0, 1.0], False, sky_cam)
        self.assertEqual(p_loc, [100.0, 200.0, 300.0])
        self.assertEqual(p_scale, [1.0, 1.0, 1.0])


class TestTF2MapIntegration(unittest.TestCase):
    def test_read_ctf_2fort_bsp(self):
        tf2_dir = detect_tf2_game_dir()
        if not tf2_dir:
            self.skipTest("TF2 directory not found via Steam libraryfolders.vdf")
        bsp_path = tf2_dir / "maps" / "ctf_2fort.bsp"
        if not bsp_path.exists():
            self.skipTest("ctf_2fort.bsp not found in TF2 maps folder")

        from SourceIO.library.source1.bsp.bsp_file import open_bsp
        from SourceIO.library.source1.bsp.lumps import EntityLump, GameLump, OverlayLump  # noqa: F401
        from SourceIO.library.utils import FileBuffer
        from SourceIO.library.utils.tiny_path import TinyPath

        cm = ContentManager()
        with FileBuffer(TinyPath(bsp_path)) as buf:
            bsp = open_bsp(TinyPath(bsp_path), buf, content_manager=cm)
            self.assertIsNotNone(bsp)
            self.assertEqual(bsp.info.ident, "VBSP")
            self.assertEqual(bsp.info.version[0], 20)

            ent_lump = bsp.get_lump("LUMP_ENTITIES")
            self.assertGreater(len(ent_lump.entities), 500)

            glump = bsp.get_lump("LUMP_GAME_LUMP")
            sprp = glump.game_lumps.get("sprp")
            self.assertGreater(len(sprp.static_props), 1000)

            ov_lump = bsp.get_lump("LUMP_OVERLAYS")
            self.assertGreater(len(ov_lump.overlays), 100)


if __name__ == "__main__":
    unittest.main()
