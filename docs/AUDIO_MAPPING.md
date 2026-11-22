# Team Fortress 2 to UEFN Audio Mapping

This document details the mapping of Team Fortress 2 sound events, raw audio files, and soundscapes from TF2's VPK archives (`scripts/game_sounds_*.txt`) into Unreal Editor for Fortnite (UEFN) devices and Verse audio APIs.

---

## 1. UEFN Audio Specifications & Constraints

- **Format**: 16-bit or 24-bit uncompressed PCM WAV.
- **Sample Rate**: 44.1 kHz or 48.0 kHz (48 kHz recommended for engine mixing).
- **Channels**: Mono (recommended for 3D spatialized in-world sounds) or Stereo (recommended for 2D UI, music, announcer).
- **File Size & Memory**: Individual audio clips should remain below 10–15 MB; long music tracks should stream from disk.
- **Playback Mechanisms**:
  - `Audio Player Device` (`crd_audio_player_device`): Used for ambient sounds, looping machinery, alarms, and zone music. Exposes Attenuation Radius, Volume, Pitch, and Auto-Play options in the editor and Verse.
  - `Radio Device` (`crd_radio_device`): Used for background soundtrack playback and team radio cues.
  - Verse Audio Component / `audio_component`: For dynamic runtime sound playback attached to players or moving actors.

---

## 2. Map Environmental & Ambient Audio (ctf_2fort)

The table below catalogs every ambient audio entity from `ctf_2fort.bsp`, its source sound event/file, the converted UEFN WAV asset, and the target UEFN device placement.

| Source Entity | Hammer Location | Sound Reference | Converted Asset | Radius (cm) | Volume | Loop | Target UEFN Device |
|:---|:---|:---|:---|:---:|:---:|:---:|:---|
| `ambient_generic` | `(-1568, 64, 40)` | `2Fort.Alarm` | `S_2fort_alarm.wav` | 3175.0 | 1.00 | Yes | Audio Player Device (`crd_audio_player_device`) |
| `ambient_generic` | `(1568, -64, 40)` | `2Fort.Alarm` | `S_2fort_alarm.wav` | 3175.0 | 1.00 | Yes | Audio Player Device (`crd_audio_player_device`) |
| `ambient_generic` | `(0, 0, -220)` | `Ambient.Water` | `S_ambient_water.wav` | 2540.0 | 0.85 | Yes | Audio Player Device (`crd_audio_player_device`) |
| `ambient_generic` | `(-832, 1280, 160)` | `Computer.Loop` | `S_computer_loop.wav` | 1524.0 | 0.60 | Yes | Audio Player Device (`crd_audio_player_device`) |
| `ambient_generic` | `(832, -1280, 160)` | `Computer.Loop` | `S_computer_loop.wav` | 1524.0 | 0.60 | Yes | Audio Player Device (`crd_audio_player_device`) |
| `ambient_generic` | `(0, 0, 128)` | `Wind.Exterior` | `S_wind_exterior.wav` | 4000.0 | 0.50 | Yes | Audio Player Device (`crd_audio_player_device`) |
| `trigger_soundscape` | Base Corridors | `2Fort.Indoors` | `S_soundscape_indoors.wav` | Volume | 0.75 | Loop | Audio Player Device (Trigger Volume) |
| `trigger_soundscape` | Bridge / Courtyard | `2Fort.Outdoors` | `S_soundscape_outdoors.wav` | Volume | 0.75 | Loop | Audio Player Device (Trigger Volume) |

---

## 3. Core Gameplay Sound Events

These sound events are resolved from `scripts/game_sounds_manifest.txt` and `game_sounds_weapons.txt` / `game_sounds_vo.txt`, converted to 16-bit WAV, and hooked via Verse gameplay logic.

| Sound Event | TF2 VPK Source File | Converted Asset | Verse / UEFN Playback Hook |
|:---|:---|:---|:---|
| `TFPlayer.CritHit` | `sound/weapons/cappoint_beep.wav` | `S_crit_hit.wav` | Verse: `PlayAudioAtLocation(PlayerPos)` on critical damage |
| `TFPlayer.AttackerCrit` | `sound/weapons/crit_hit.wav` | `S_attacker_crit.wav` | Verse: 2D Client Audio Player |
| `Weapon_RocketLauncher.Single` | `sound/weapons/rocket_shoot.wav` | `S_rocket_shoot.wav` | Verse: Weapon fire trigger hook |
| `Weapon_RocketLauncher.Explosion` | `sound/weapons/explode1.wav` | `S_explode1.wav` | Verse / Explosive Device trigger |
| `Weapon_Shotgun.Single` | `sound/weapons/shotgun_shoot.wav` | `S_shotgun_shoot.wav` | Verse: Hitscan weapon fire hook |
| `Weapon_Minigun.Spin` | `sound/weapons/minigun_spin.wav` | `S_minigun_spin.wav` | Verse: Looping audio component on Heavy |
| `Weapon_Medigun.Healing` | `sound/weapons/medigun_heal.wav` | `S_medigun_heal.wav` | Verse: Active beam loop audio |
| `Building_Sentrygun.Alert` | `sound/weapons/sentry_spot.wav` | `S_sentry_spot.wav` | Verse: Sentry target acquisition |
| `Building_Sentrygun.Fire` | `sound/weapons/sentry_shoot.wav` | `S_sentry_shoot.wav` | Verse: Sentry shooting loop |
| `Announcer.ControlPointContested` | `sound/vo/announcer_control_point_contested.wav` | `S_announcer_contested.wav` | Radio Device / Team Audio Player |
| `Announcer.IntelPickedUp` | `sound/vo/intel_pickedup_red.wav` | `S_intel_pickedup_red.wav` | Radio Device on flag pickup |
| `Announcer.IntelCaptured` | `sound/vo/intel_captured_red.wav` | `S_intel_captured_red.wav` | Radio Device on flag capture |
| `DoorSound.DefaultMove` | `sound/doors/default_move.wav` | `S_door_move.wav` | Verse: On Door trigger open/close |
| `Regenerator.Touch` | `sound/items/regenerate.wav` | `S_regenerate.wav` | Verse: Resupply cabinet touch event |
