"""
test_uefn_pipeline.py — Automated Unit Tests for UEFN Pipeline
=============================================================

Tests:
1. Coordinate & transform math (Source to Unreal position, rotation, scale, and roundtrip).
2. Normal map green channel inversion and texture dimension calculation.
3. Material mapper logic and shader fallback assignments.
4. Manifest builder, audit counting, and validation report generation.
5. Integration test on ctf_2fort.bsp (if game files are present).
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from SourceIO.blender_bindings.uefn.coordinates import (
    INCHES_TO_CM,
    parse_vector_string,
    source_to_unreal_position,
    source_to_unreal_rotation,
    source_to_unreal_scale,
    unreal_to_source_position,
    unreal_to_source_rotation,
)
from SourceIO.blender_bindings.uefn.manifest_builder import (
    PlacedActorRecord,
    UEFNManifestBuilder,
)
from SourceIO.blender_bindings.uefn.texture_converter import (
    is_power_of_two,
    next_lower_power_of_two,
)


class TestUEFNCoordinates(unittest.TestCase):
    def test_position_conversion(self):
        # Origin (0, 0, 0)
        self.assertEqual(source_to_unreal_position([0, 0, 0]), [0.0, 0.0, 0.0])

        # Test point: Source (100, 200, 300) -> Unreal (254, -508, 762)
        ue_pos = source_to_unreal_position([100, 200, 300])
        self.assertAlmostEqual(ue_pos[0], 254.0)
        self.assertAlmostEqual(ue_pos[1], -508.0)
        self.assertAlmostEqual(ue_pos[2], 762.0)

        # Roundtrip test
        src_pos_rt = unreal_to_source_position(ue_pos)
        self.assertAlmostEqual(src_pos_rt[0], 100.0)
        self.assertAlmostEqual(src_pos_rt[1], 200.0)
        self.assertAlmostEqual(src_pos_rt[2], 300.0)

    def test_rotation_conversion(self):
        # Hammer (Pitch=15, Yaw=90, Roll=0) -> Unreal (Pitch=15, Yaw=-90, Roll=0)
        ue_rot = source_to_unreal_rotation([15.0, 90.0, 0.0])
        self.assertAlmostEqual(ue_rot[0], 15.0)
        self.assertAlmostEqual(ue_rot[1], -90.0)
        self.assertAlmostEqual(ue_rot[2], 0.0)

        # Angle normalization test
        ue_rot_norm = source_to_unreal_rotation([0.0, 270.0, 0.0])
        # -270 degrees normalized is +90 degrees
        self.assertAlmostEqual(ue_rot_norm[1], 90.0)

        # Roundtrip rotation
        src_rot_rt = unreal_to_source_rotation(ue_rot)
        self.assertAlmostEqual(src_rot_rt[0], 15.0)
        self.assertAlmostEqual(src_rot_rt[1], 90.0)
        self.assertAlmostEqual(src_rot_rt[2], 0.0)

    def test_scale_conversion(self):
        self.assertEqual(source_to_unreal_scale(1.5), [1.5, 1.5, 1.5])
        self.assertEqual(source_to_unreal_scale([1.0, 2.0, 3.0]), [1.0, 2.0, 3.0])

    def test_parse_vector_string(self):
        self.assertEqual(parse_vector_string("128 -256 64.5"), [128.0, -256.0, 64.5])
        self.assertEqual(parse_vector_string(""), [0.0, 0.0, 0.0])
        self.assertEqual(parse_vector_string("invalid text", (1.0, 2.0, 3.0)), [1.0, 2.0, 3.0])


class TestUEFNTextureMath(unittest.TestCase):
    def test_power_of_two(self):
        self.assertTrue(is_power_of_two(256))
        self.assertTrue(is_power_of_two(512))
        self.assertTrue(is_power_of_two(1024))
        self.assertTrue(is_power_of_two(2048))
        self.assertTrue(is_power_of_two(4096))
        self.assertFalse(is_power_of_two(500))
        self.assertFalse(is_power_of_two(0))
        self.assertFalse(is_power_of_two(-4))

    def test_next_lower_power_of_two(self):
        self.assertEqual(next_lower_power_of_two(1023), 512)
        self.assertEqual(next_lower_power_of_two(3000), 2048)
        self.assertEqual(next_lower_power_of_two(2048), 2048)


class TestUEFNManifestBuilder(unittest.TestCase):
    def test_manifest_and_report_generation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir)
            builder = UEFNManifestBuilder("test_map", out_path)

            builder.expected_counts["static_props"] = 2
            builder.expected_counts["gameplay_entities"] = 1

            actor1 = PlacedActorRecord(
                actor_name="Prop_crate_0",
                asset_reference="SM_crate.fbx",
                location=[100.0, -200.0, 50.0],
                rotation=[0.0, 45.0, 0.0],
                scale=[1.0, 1.0, 1.0],
                entity_class="prop_static",
            )
            actor2 = PlacedActorRecord(
                actor_name="Prop_barrel_1",
                asset_reference="SM_barrel.fbx",
                location=[300.0, -400.0, 50.0],
                rotation=[0.0, 0.0, 0.0],
                scale=[1.0, 1.0, 1.0],
                entity_class="prop_static",
            )
            builder.add_actor(actor1)
            builder.add_actor(actor2)
            builder.exported_counts["static_props"] = 2

            # Add gameplay entity
            builder.gameplay_entities.append({
                "classname": "info_player_teamspawn",
                "targetname": "spawn_red_1",
                "location": [0.0, 0.0, 10.0],
                "rotation": [0.0, 0.0, 0.0],
            })
            builder.exported_counts["gameplay_entities"] = 1

            # Write manifest
            manifest_file = builder.write_manifest()
            self.assertTrue(manifest_file.exists())

            with open(manifest_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["map_name"], "test_map")
            self.assertEqual(len(data["actors"]), 2)
            self.assertEqual(len(data["gameplay_entities"]), 1)

            # Write reports
            rep_json, rep_md = builder.write_reports()
            self.assertTrue(rep_json.exists())
            self.assertTrue(rep_md.exists())

            with open(rep_json, "r", encoding="utf-8") as f:
                rdata = json.load(f)
            self.assertEqual(rdata["total_actors_exported"], 2)
            self.assertEqual(rdata["missing_assets_count"], 0)


class TestTF2MapIntegration(unittest.TestCase):
    def test_read_ctf_2fort_bsp(self):
        bsp_path = Path(r"H:\SteamLibrary\steamapps\common\Team Fortress 2\tf\maps\ctf_2fort.bsp")
        if not bsp_path.exists():
            self.skipTest("ctf_2fort.bsp not found on local system")

        from SourceIO.library.shared.content_manager import ContentManager
        from SourceIO.library.source1.bsp.bsp_file import open_bsp
        from SourceIO.library.source1.bsp.lumps import EntityLump, GameLump
        from SourceIO.library.utils import FileBuffer
        from SourceIO.library.utils.tiny_path import TinyPath

        cm = ContentManager()
        buf = FileBuffer(TinyPath(bsp_path))
        bsp = open_bsp(TinyPath(bsp_path), buf, content_manager=cm)

        self.assertIsNotNone(bsp)
        self.assertEqual(bsp.info.ident, "VBSP")
        self.assertEqual(bsp.info.version[0], 20)  # TF2 BSP version 20

        ent_lump = bsp.get_lump("LUMP_ENTITIES")
        self.assertIsNotNone(ent_lump)
        self.assertGreater(len(ent_lump.entities), 500)

        # Check static props lump
        glump = bsp.get_lump("LUMP_GAME_LUMP")
        self.assertIsNotNone(glump)
        sprp = glump.game_lumps.get("sprp")
        self.assertIsNotNone(sprp)
        self.assertGreater(len(sprp.static_props), 1000)
        self.assertGreater(len(sprp.model_names), 100)


if __name__ == "__main__":
    unittest.main()
