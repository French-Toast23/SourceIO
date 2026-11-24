"""
audio_exporter.py — TF2 Sound Scripts, Soundscapes, and Audio Exporter for UEFN
===============================================================================

Handles:
- Parsing TF2 sound script files (scripts/game_sounds_manifest.txt) and soundscape
  files (scripts/soundscapes_manifest.txt) using SourceIO's KV1 parser.
- Resolving sound event names to actual wave/mp3 file paths, volume, pitch, and falloff.
- Extracting audio from BSP PakFile / VPKs and validating/normalizing to UEFN-supported
  uncompressed 16-bit PCM WAV (44.1/48 kHz).
- Enforcing per-file and total audio size budgets with explicit warnings.
- Scanning ambient_generic, env_soundscape, and door sound references.
- Generating AUDIO_MAPPING.md from real extracted assets.
"""

from __future__ import annotations

import io
from pathlib import Path
import re
import struct
from typing import Any, Optional
import wave

from SourceIO.library.shared.content_manager import ContentManager
from SourceIO.library.utils import kv1
from SourceIO.library.utils.tiny_path import TinyPath
from SourceIO.logger import SourceLogMan
from .coordinates import SOURCE1_HU_TO_CM, parse_vector_string, sanitize_ue_name, source_to_unreal_position

log_manager = SourceLogMan()
logger = log_manager.get_logger("UEFN::AudioExporter")

# Special characters used as prefixes in Source engine sound scripts:
# ')' = 3D spatialized, '*' = streaming music, '#' = dry, '^' = dual mono, '@' = raw
SOUND_PREFIX_RE = re.compile(r"^[\)\*\#\^\@\<\>\$\?\!]+")

MAX_CLIP_SIZE_MB = 10.0
MAX_TOTAL_AUDIO_MB = 50.0

# Approximate attenuation distance in Hammer units by Source soundlevel
_SOUNDLEVEL_TO_HU = {
    "SNDLVL_NONE": 0.0,
    "SNDLVL_20dB": 400.0,
    "SNDLVL_25dB": 500.0,
    "SNDLVL_30dB": 600.0,
    "SNDLVL_40dB": 800.0,
    "SNDLVL_50dB": 1000.0,
    "SNDLVL_60dB": 1100.0,
    "SNDLVL_70dB": 1200.0,
    "SNDLVL_NORM": 1250.0,
    "SNDLVL_75dB": 1250.0,
    "SNDLVL_80dB": 1500.0,
    "SNDLVL_85dB": 1800.0,
    "SNDLVL_90dB": 2200.0,
    "SNDLVL_95dB": 2600.0,
    "SNDLVL_100dB": 3200.0,
    "SNDLVL_120dB": 5000.0,
    "SNDLVL_140dB": 8000.0,
}


def _parse_float_range(val: Any, default: float = 1.0) -> float:
    """Parse '0.8' or '0.7, 0.9' (take average) into a float."""
    if val is None:
        return default
    s = str(val).strip()
    if "," in s:
        parts = [p.strip() for p in s.split(",") if p.strip()]
        nums = []
        for p in parts:
            try:
                nums.append(float(p))
            except ValueError:
                pass
        return sum(nums) / len(nums) if nums else default
    try:
        return float(s)
    except ValueError:
        return default


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
    Parses TF2 sound scripts & soundscapes, extracts/validates audio assets,
    and generates mapping reports.
    """

    def __init__(
        self,
        content_manager: ContentManager,
        output_dir: Path,
        cm_per_hu: float = SOURCE1_HU_TO_CM,
        scale_multiplier: float = 1.0,
    ):
        self.content_manager = content_manager
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cm_per_hu = cm_per_hu
        self.scale_multiplier = scale_multiplier
        self.sound_events: dict[str, SoundEventEntry] = {}
        self.soundscapes: dict[str, list[str]] = {}  # soundscape name -> list of wave paths
        self.converted_audio: dict[str, Path] = {}
        self.missing_audio: list[str] = []
        self.placed_audio_entities: list[dict[str, Any]] = []
        self.total_audio_bytes: int = 0
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def load_sound_manifest(self) -> int:
        """
        Parse scripts/game_sounds_manifest.txt and scripts/soundscapes_manifest.txt via KV1.
        """
        manifest_buf = self.content_manager.find_file(TinyPath("scripts/game_sounds_manifest.txt"))
        if manifest_buf:
            raw_bytes = manifest_buf.read(-1)
            text = kv1.decode_text(raw_bytes, "game_sounds_manifest.txt")
            root = kv1.loads(text, "game_sounds_manifest.txt")
            _, block = root.top()
            if isinstance(block, kv1.KV1Block):
                for entry in block.entries:
                    if entry.key.lower() == "precache_file" and isinstance(entry.value, str):
                        script_buf = self.content_manager.find_file(TinyPath(entry.value))
                        if script_buf:
                            s_bytes = script_buf.read(-1)
                            s_text = kv1.decode_text(s_bytes, entry.value)
                            self._parse_sound_script_kv1(s_text, entry.value)
        else:
            logger.warn("scripts/game_sounds_manifest.txt not found in search paths.")

        # Also load soundscapes manifest
        ss_buf = self.content_manager.find_file(TinyPath("scripts/soundscapes_manifest.txt"))
        if ss_buf:
            raw_bytes = ss_buf.read(-1)
            text = kv1.decode_text(raw_bytes, "soundscapes_manifest.txt")
            root = kv1.loads(text, "soundscapes_manifest.txt")
            _, block = root.top()
            if isinstance(block, kv1.KV1Block):
                for entry in block.entries:
                    if entry.key.lower() == "file" and isinstance(entry.value, str):
                        ss_script = self.content_manager.find_file(TinyPath(entry.value))
                        if ss_script:
                            s_bytes = ss_script.read(-1)
                            s_text = kv1.decode_text(s_bytes, entry.value)
                            self._parse_soundscape_kv1(s_text, entry.value)

        logger.info(
            f"Loaded {len(self.sound_events)} sound events and {len(self.soundscapes)} soundscapes."
        )
        return len(self.sound_events)

    def _parse_sound_script_kv1(self, text: str, filename: str) -> int:
        root = kv1.loads(text, filename)
        parsed = 0
        for event_name, block in root.blocks():
            if not isinstance(block, kv1.KV1Block):
                continue
            entry = SoundEventEntry(event_name)
            for kv in block.entries:
                k = kv.key.lower()
                if k == "wave" and isinstance(kv.value, str):
                    clean_w = SOUND_PREFIX_RE.sub("", kv.value).replace("\\", "/").strip()
                    if clean_w:
                        entry.waves.append(clean_w)
                elif k == "rndwave" and isinstance(kv.value, kv1.KV1Block):
                    for sub in kv.value.entries:
                        if sub.key.lower() == "wave" and isinstance(sub.value, str):
                            clean_w = SOUND_PREFIX_RE.sub("", sub.value).replace("\\", "/").strip()
                            if clean_w:
                                entry.waves.append(clean_w)
                elif k == "volume":
                    val_str = str(kv.value).upper()
                    entry.volume = 1.0 if val_str == "VOL_NORM" else _parse_float_range(kv.value, 1.0)
                elif k == "pitch":
                    val_str = str(kv.value).upper()
                    if val_str == "PITCH_NORM":
                        entry.pitch = 1.0
                    elif val_str == "PITCH_LOW":
                        entry.pitch = 0.95
                    elif val_str == "PITCH_HIGH":
                        entry.pitch = 1.2
                    else:
                        entry.pitch = _parse_float_range(kv.value, 100.0) / 100.0
                elif k == "soundlevel" and isinstance(kv.value, str):
                    entry.soundlevel = kv.value
                elif k == "channel" and isinstance(kv.value, str):
                    entry.channel = kv.value

            self.sound_events[event_name.lower()] = entry
            parsed += 1
        return parsed

    def _parse_soundscape_kv1(self, text: str, filename: str) -> int:
        root = kv1.loads(text, filename)
        parsed = 0
        for ss_name, block in root.blocks():
            if not isinstance(block, kv1.KV1Block):
                continue
            waves: list[str] = []
            for kv in block.entries:
                if not isinstance(kv.value, kv1.KV1Block):
                    continue
                for sub in kv.value.entries:
                    if sub.key.lower() == "wave" and isinstance(sub.value, str):
                        clean_w = SOUND_PREFIX_RE.sub("", sub.value).replace("\\", "/").strip()
                        if clean_w:
                            waves.append(clean_w)
                    elif sub.key.lower() == "rndwave" and isinstance(sub.value, kv1.KV1Block):
                        for r in sub.value.entries:
                            if r.key.lower() == "wave" and isinstance(r.value, str):
                                clean_w = SOUND_PREFIX_RE.sub("", r.value).replace("\\", "/").strip()
                                if clean_w:
                                    waves.append(clean_w)
            if waves:
                self.soundscapes[ss_name.lower()] = waves
                parsed += 1
        return parsed

    def resolve_sound_waves(self, sound_ref: str) -> tuple[list[str], Optional[SoundEventEntry]]:
        clean_ref = SOUND_PREFIX_RE.sub("", sound_ref).replace("\\", "/").strip().lower()
        if clean_ref in self.sound_events:
            ev = self.sound_events[clean_ref]
            return list(ev.waves), ev
        if clean_ref in self.soundscapes:
            return list(self.soundscapes[clean_ref]), None
        return [clean_ref], None

    def extract_and_convert_sound(self, sound_ref: str) -> Optional[Path]:
        """
        Resolve a sound event, soundscape, or direct wave path, extract it from VPKs/PakFile,
        validate/convert to uncompressed 16-bit PCM WAV, and check size budgets.
        """
        clean_ref = SOUND_PREFIX_RE.sub("", sound_ref).replace("\\", "/").strip().lower()
        if not clean_ref:
            return None

        if clean_ref in self.converted_audio:
            return self.converted_audio[clean_ref]

        waves, _ = self.resolve_sound_waves(clean_ref)
        if not waves:
            msg = f"Sound event '{sound_ref}' has no wave entries."
            if sound_ref not in self.missing_audio:
                self.missing_audio.append(sound_ref)
            self.warnings.append(msg)
            return None

        wave_path = waves[0]
        if not wave_path.startswith("sound/"):
            wave_path = "sound/" + wave_path

        file_buf = self.content_manager.find_file(TinyPath(wave_path))
        if not file_buf:
            msg = f"Audio file not found in search paths: {wave_path} (ref: {sound_ref})"
            logger.warn(msg)
            if wave_path not in self.missing_audio:
                self.missing_audio.append(wave_path)
            self.warnings.append(msg)
            return None

        data = file_buf.read(-1)
        out_name = sanitize_ue_name(wave_path, prefix="S_") + ".wav"
        out_wav_path = self.output_dir / out_name

        try:
            pcm_written = False
            if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
                # Verify if already standard uncompressed 16-bit PCM WAV
                try:
                    with wave.open(io.BytesIO(data), "rb") as wf:
                        if wf.getcomptype() == "NONE" and wf.getsampwidth() == 2:
                            with open(out_wav_path, "wb") as f:
                                f.write(data)
                            pcm_written = True
                except wave.Error:
                    pcm_written = False

            if not pcm_written:
                try:
                    import miniaudio
                    decoded = miniaudio.decode(data, output_format=miniaudio.SampleFormat.SIGNED16)
                    with wave.open(str(out_wav_path), "wb") as wf:
                        wf.setnchannels(decoded.nchannels)
                        wf.setsampwidth(2)
                        wf.setframerate(decoded.sample_rate)
                        wf.writeframes(decoded.samples.tobytes())
                    pcm_written = True
                except Exception as conv_err:
                    # Fallback write raw data with explicit warning if non-PCM (e.g. ADPCM WAV)
                    with open(out_wav_path, "wb") as f:
                        f.write(data)
                    self.warnings.append(
                        f"Audio '{wave_path}' could not be transcoded to 16-bit PCM ({conv_err}); "
                        "written raw — verify format before UEFN import."
                    )

            clip_bytes = out_wav_path.stat().st_size
            clip_mb = clip_bytes / (1024 * 1024)
            self.total_audio_bytes += clip_bytes
            if clip_mb > MAX_CLIP_SIZE_MB:
                self.warnings.append(
                    f"Audio clip '{out_wav_path.name}' is {clip_mb:.2f} MB (> {MAX_CLIP_SIZE_MB} MB budget)."
                )
            total_mb = self.total_audio_bytes / (1024 * 1024)
            if total_mb > MAX_TOTAL_AUDIO_MB:
                self.warnings.append(
                    f"Total exported audio ({total_mb:.2f} MB) exceeds {MAX_TOTAL_AUDIO_MB} MB recommended island budget."
                )

            self.converted_audio[clean_ref] = out_wav_path
            return out_wav_path
        except Exception as e:
            err = f"Failed extracting/converting audio {wave_path}: {e}"
            logger.error(err)
            self.errors.append(err)
            return None

    def process_ambient_entity(self, entity_data: dict[str, Any]) -> dict[str, Any]:
        """
        Process an ambient_generic or env_soundscape entity and record placement data.
        """
        classname = entity_data.get("classname", "ambient_generic").lower()
        sound_ref = entity_data.get("message", "") or entity_data.get("soundscape", "")
        origin_raw = parse_vector_string(entity_data.get("origin", "0 0 0"))

        try:
            radius_hu = float(entity_data.get("radius", 0.0))
        except (TypeError, ValueError):
            radius_hu = 0.0

        _, ev_entry = self.resolve_sound_waves(sound_ref)
        if radius_hu <= 0.0:
            if ev_entry and ev_entry.soundlevel in _SOUNDLEVEL_TO_HU:
                radius_hu = _SOUNDLEVEL_TO_HU[ev_entry.soundlevel]
            else:
                radius_hu = 1250.0  # Standard Source ambient_generic default

        try:
            health_val = float(entity_data.get("health", 10.0))
        except (TypeError, ValueError):
            health_val = 10.0

        try:
            pitch_val = float(entity_data.get("pitch", 100.0))
        except (TypeError, ValueError):
            pitch_val = 100.0

        try:
            spawnflags = int(entity_data.get("spawnflags", 0))
        except (TypeError, ValueError):
            spawnflags = 0

        # Flag 16 = Start Silent, Flag 32 = Is NOT Looped
        is_loop = (spawnflags & 32) == 0 if classname == "ambient_generic" else True

        ue_pos = source_to_unreal_position(
            origin_raw,
            cm_per_hu=self.cm_per_hu,
            scale_multiplier=self.scale_multiplier,
        )
        ue_radius = radius_hu * self.cm_per_hu * self.scale_multiplier

        converted_path = self.extract_and_convert_sound(sound_ref) if sound_ref else None

        vol = round(min(1.0, max(0.0, health_val / 10.0)), 2)
        if ev_entry:
            vol = round(min(1.0, vol * ev_entry.volume), 2)

        audio_spec = {
            "entity_name": entity_data.get("targetname") or f"{classname}_{entity_data.get('hammerid', len(self.placed_audio_entities))}",
            "classname": classname,
            "sound_reference": sound_ref,
            "converted_asset": converted_path.stem if converted_path else None,
            "converted_filename": converted_path.name if converted_path else None,
            "location": ue_pos,
            "attenuation_radius": round(ue_radius, 2),
            "volume": vol,
            "pitch": round(pitch_val / 100.0, 2),
            "is_loop": is_loop,
            "uefn_device": "AmbientSound / Audio Player Device (crd_audio_player_device)",
        }
        self.placed_audio_entities.append(audio_spec)
        return audio_spec

    def generate_mapping_markdown(self, output_doc_path: Path) -> None:
        """
        Generate AUDIO_MAPPING.md listing every resolved sound event, source file,
        converted WAV file, and UEFN playback device.
        """
        lines = [
            "# TF2 to UEFN Audio Mapping",
            "",
            "This report is generated directly from the exported map entities and resolved TF2 soundscripts.",
            "",
            "## 1. Map Ambient & Soundscape Entities",
            "",
            "| Entity Name | Class | Sound Event / Script | Converted WAV | Radius (cm) | Volume | Loop | Target UEFN Device |",
            "|---|---|---|---|---|---|---|---|",
        ]

        for ent in self.placed_audio_entities:
            lines.append(
                f"| `{ent['entity_name']}` | `{ent['classname']}` | `{ent['sound_reference']}` | "
                f"`{ent['converted_filename'] or 'MISSING'}` | {ent['attenuation_radius']} | "
                f"{ent['volume']} | {ent['is_loop']} | {ent['uefn_device']} |"
            )

        lines.extend([
            "",
            "## 2. Extracted Sound Events & Files",
            "",
            "| Sound Reference | Resolved Wave Source | Converted WAV | Playback Hook |",
            "|---|---|---|---|",
        ])

        for ref, out_path in sorted(self.converted_audio.items()):
            waves, _ = self.resolve_sound_waves(ref)
            src_wave = waves[0] if waves else ref
            lines.append(f"| `{ref}` | `{src_wave}` | `{out_path.name}` | `Audio Player Device` / `AmbientSound` |")

        if self.missing_audio:
            lines.extend([
                "",
                "## 3. Missing / Unresolved Audio",
                "",
            ])
            for m in self.missing_audio:
                lines.append(f"- `{m}`")

        lines.append("")
        with open(output_doc_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
