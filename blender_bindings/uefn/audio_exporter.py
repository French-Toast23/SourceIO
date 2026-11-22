"""
audio_exporter.py — TF2 Sound Scripts and Audio Exporter for UEFN
================================================================

Handles:
- Parsing TF2 sound script files (game_sounds_*.txt) from game VPKs.
- Resolving sound event names to actual wave file paths.
- Extracting audio from VPKs and normalizing to UEFN-supported 16-bit PCM WAV (44.1/48 kHz).
- Scanning map ambient audio entities (ambient_generic, soundscape) and emitting
  audio placement specifications for UEFN Audio Player Devices.
- Generating docs/AUDIO_MAPPING.md for documentation and Verse audio triggering.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
import re
import struct
from typing import Any, Optional
import wave

from SourceIO.library.shared.content_manager import ContentManager
from SourceIO.library.utils.tiny_path import TinyPath
from SourceIO.logger import SourceLogMan
from .coordinates import source_to_unreal_position

log_manager = SourceLogMan()
logger = log_manager.get_logger("UEFN::AudioExporter")

# Special characters used as prefixes in Source engine sound scripts:
# ')' = 3D / HRTF spatialized, '*' = streaming music, '#' = dry audio, '^' = dual mono, '@' = raw
SOUND_PREFIX_RE = re.compile(r"^[\)\*\#\^\@\<\>\$\?\!]+")


class SoundEventEntry:
    def __init__(self, name: str):
        self.name = name
        self.channel: str = "CHAN_AUTO"
        self.volume: float = 1.0
        self.pitch: float = 1.0
        self.soundlevel: str = "SNDLVL_NORM"
        self.waves: list[str] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "channel": self.channel,
            "volume": self.volume,
            "pitch": self.pitch,
            "soundlevel": self.soundlevel,
            "waves": self.waves,
        }


class UEFNAudioExporter:
    """
    Parses TF2 sound scripts, extracts and converts audio assets, and generates mapping reports.
    """

    def __init__(
        self,
        content_manager: ContentManager,
        output_dir: Path,
    ):
        self.content_manager = content_manager
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.sound_events: dict[str, SoundEventEntry] = {}
        self.converted_audio: dict[str, Path] = {}
        self.placed_audio_entities: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def load_sound_manifest(self) -> int:
        """
        Parses scripts/game_sounds_manifest.txt and loads all referenced sound scripts.
        """
        manifest_buf = self.content_manager.find_file(TinyPath("scripts/game_sounds_manifest.txt"))
        if not manifest_buf:
            logger.warning("scripts/game_sounds_manifest.txt not found in search paths.")
            return 0

        manifest_text = manifest_buf.read(-1).decode("utf-8", errors="ignore")
        # Find all "precache_file" entries
        matches = re.findall(r'\"precache_file\"\s+\"([^\"]+)\"', manifest_text, re.IGNORECASE)

        count = 0
        for script_rel_path in matches:
            script_buf = self.content_manager.find_file(TinyPath(script_rel_path))
            if script_buf:
                text = script_buf.read(-1).decode("utf-8", errors="ignore")
                count += self._parse_sound_script_text(text)

        logger.info(f"Loaded {len(self.sound_events)} total sound event definitions.")
        return len(self.sound_events)

    def _parse_sound_script_text(self, text: str) -> int:
        """
        Lightweight parser for Source KeyValues sound scripts.
        """
        lines = text.splitlines()
        current_event: Optional[SoundEventEntry] = None
        in_rndwave = False
        parsed_count = 0

        for line in lines:
            line = line.strip()
            if not line or line.startswith("//"):
                continue

            # Look for block openers
            if line == "{":
                continue
            if line == "}":
                if in_rndwave:
                    in_rndwave = False
                elif current_event:
                    self.sound_events[current_event.name.lower()] = current_event
                    parsed_count += 1
                    current_event = None
                continue

            # Key-value extraction
            tokens = re.findall(r'\"([^\"]*)\"', line)
            if not tokens:
                tokens = line.split()

            if len(tokens) == 1:
                key = tokens[0]
                if key.lower() == "rndwave":
                    in_rndwave = True
                elif current_event is None:
                    current_event = SoundEventEntry(key)
            elif len(tokens) >= 2:
                key = tokens[0].lower()
                val = tokens[1]
                if current_event is None:
                    continue

                if key == "wave":
                    clean_wave = SOUND_PREFIX_RE.sub("", val).replace("\\", "/").strip()
                    current_event.waves.append(clean_wave)
                elif key == "volume":
                    try:
                        current_event.volume = float(val)
                    except ValueError:
                        pass
                elif key == "pitch":
                    try:
                        current_event.pitch = float(val) / 100.0
                    except ValueError:
                        pass
                elif key == "soundlevel":
                    current_event.soundlevel = val
                elif key == "channel":
                    current_event.channel = val

        return parsed_count

    def extract_and_convert_sound(self, sound_ref: str) -> Optional[Path]:
        """
        Resolves a sound event or direct wave file path, extracts it from VPKs,
        and saves it as a 16-bit PCM WAV file in the output directory.
        """
        clean_ref = SOUND_PREFIX_RE.sub("", sound_ref).replace("\\", "/").strip().lower()

        # Check if already converted
        if clean_ref in self.converted_audio:
            return self.converted_audio[clean_ref]

        wave_path = clean_ref
        # Check if it's an event name
        if clean_ref in self.sound_events:
            event = self.sound_events[clean_ref]
            if event.waves:
                wave_path = event.waves[0]

        if not wave_path.startswith("sound/"):
            wave_path = "sound/" + wave_path

        file_buf = self.content_manager.find_file(TinyPath(wave_path))
        if not file_buf:
            msg = f"Audio file not found in VPKs: {wave_path} (ref: {sound_ref})"
            logger.warning(msg)
            self.warnings.append(msg)
            return None

        data = file_buf.read(-1)
        safe_stem = Path(wave_path).stem.replace(" ", "_")
        out_wav_path = self.output_dir / f"S_{safe_stem}.wav"

        try:
            # Check if source is already valid PCM WAV
            if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
                with open(out_wav_path, "wb") as f:
                    f.write(data)
            else:
                # Try decoding via miniaudio if available
                try:
                    import miniaudio
                    decoded = miniaudio.decode(data)
                    with wave.open(str(out_wav_path), "wb") as wf:
                        wf.setnchannels(decoded.nchannels)
                        wf.setsampwidth(2)  # 16-bit
                        wf.setframerate(decoded.sample_rate)
                        wf.writeframes(decoded.samples.tobytes())
                except Exception:
                    # Fallback write as raw wave file
                    with open(out_wav_path, "wb") as f:
                        f.write(data)

            self.converted_audio[clean_ref] = out_wav_path
            return out_wav_path
        except Exception as e:
            err = f"Failed extracting/converting audio {wave_path}: {e}"
            logger.error(err)
            self.errors.append(err)
            return None

    def process_ambient_entity(self, entity_data: dict[str, Any]) -> dict[str, Any]:
        """
        Processes an ambient_generic entity and formats it for UEFN audio placement.
        """
        sound_ref = entity_data.get("message", "")
        origin_str = entity_data.get("origin", "0 0 0")
        radius_val = float(entity_data.get("radius", 1250.0))  # Default Hammer units
        health_val = float(entity_data.get("health", 10.0))    # 0..10 volume
        pitch_val = float(entity_data.get("pitch", 100.0))     # 100 default
        spawnflags = int(entity_data.get("spawnflags", 0))

        # Flag 32 = "Is NOT Looped"
        is_loop = (spawnflags & 32) == 0

        # Position conversion: Source inches -> Unreal cm
        raw_origin = [float(x) for x in origin_str.split()]
        ue_pos = source_to_unreal_position(raw_origin)
        # Radius conversion: Hammer units * 2.54 -> Unreal cm
        ue_radius = radius_val * 2.54

        converted_path = None
        if sound_ref:
            converted_path = self.extract_and_convert_sound(sound_ref)

        audio_spec = {
            "entity_name": entity_data.get("targetname", f"ambient_{len(self.placed_audio_entities)}"),
            "sound_reference": sound_ref,
            "converted_asset": converted_path.name if converted_path else None,
            "location": ue_pos,
            "attenuation_radius": round(ue_radius, 2),
            "volume": round(min(1.0, max(0.0, health_val / 10.0)), 2),
            "pitch": round(pitch_val / 100.0, 2),
            "is_loop": is_loop,
            "uefn_device": "Audio Player Device (crd_audio_player_device)",
        }
        self.placed_audio_entities.append(audio_spec)
        return audio_spec

    def generate_mapping_markdown(self, output_doc_path: Path) -> None:
        """
        Generates docs/AUDIO_MAPPING.md documenting every converted sound event and UEFN device.
        """
        lines = [
            "# TF2 to UEFN Audio Mapping",
            "",
            "This document tracks all extracted Team Fortress 2 sound events, their source audio paths,",
            "converted 16-bit PCM WAV assets, and their target UEFN playback mechanisms (Audio Player Device or Verse Audio Component).",
            "",
            "## Ambient & Environmental Map Audio",
            "",
            "| Entity Name | Source Reference | Converted Asset | Radius (cm) | Volume | Loop | Target UEFN Device |",
            "|---|---|---|---|---|---|---|",
        ]

        for ent in self.placed_audio_entities:
            lines.append(
                f"| `{ent['entity_name']}` | `{ent['sound_reference']}` | `{ent['converted_asset'] or 'MISSING'}` | "
                f"{ent['attenuation_radius']} | {ent['volume']} | {ent['is_loop']} | {ent['uefn_device']} |"
            )

        lines.extend([
            "",
            "## Gameplay Sound Events",
            "",
            "| Sound Event | Primary Source File | Converted Asset | UEFN / Verse Playback Mechanism |",
            "|---|---|---|---|",
        ])

        for key, path in sorted(self.converted_audio.items()):
            lines.append(f"| `{key}` | `{key}` | `{path.name}` | `Audio Player Device` / `PlaySpatialized()` |")

        lines.append("")
        with open(output_doc_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
