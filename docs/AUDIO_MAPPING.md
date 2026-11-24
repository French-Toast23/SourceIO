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

The table below catalogs the verified ambient audio and soundscape entities extracted from `ctf_2fort.bsp` (`1.905 cm/HU`), their resolved TF2 VPK soundscript waves, converted 16-bit PCM WAV assets, and target UEFN device placements (`10` audio entities resolving to `7` sound events across `4` unique WAV files).

| Entity Name | Class | Sound Event / Script | Converted Asset | Radius (cm) | Volume | Loop | Target UEFN Device |
|:---|:---|:---|:---|:---:|:---:|:---:|:---|
| `outdoor_pond` | `env_soundscape` | `2fort.OutdoorPond` | `S_ambient_outdoors_quiet_birds.wav` | 2381.25 | 1.00 | Yes | Audio Player Device (`crd_audio_player_device`) |
| `outdoor_fort` | `env_soundscape` | `2fort.OutdoorFort` | `S_ambient_outdoors_quiet_birds.wav` | 2381.25 | 1.00 | Yes | Audio Player Device (`crd_audio_player_device`) |
| `underground2` | `env_soundscape` | `2fort.Underground2` | `S_ambient_underground.wav` | 2381.25 | 1.00 | Yes | Audio Player Device (`crd_audio_player_device`) |
| `ambient_generic_3316153` | `ambient_generic` | `Ambient.MachineHum` | `S_ambient_machine_hum.wav` | 2381.25 | 1.00 | Yes | Audio Player Device (`crd_audio_player_device`) |
| `ambient_generic_3340479` | `ambient_generic` | `Ambient.MachineHum` | `S_ambient_machine_hum.wav` | 2381.25 | 1.00 | Yes | Audio Player Device (`crd_audio_player_device`) |
| `ambient_generic_3374451` | `ambient_generic` | `Ambient.MachineHum` | `S_ambient_machine_hum.wav` | 2381.25 | 1.00 | Yes | Audio Player Device (`crd_audio_player_device`) |
| `ambient_generic_3374486` | `ambient_generic` | `Ambient.MachineHum` | `S_ambient_machine_hum.wav` | 2381.25 | 1.00 | Yes | Audio Player Device (`crd_audio_player_device`) |
| `soundscape_respawn` | `env_soundscape` | `tf2.respawn_room` | `S_ambient_underground.wav` | 426.72 | 1.00 | Yes | Audio Player Device (`crd_audio_player_device`) |
| `underground` | `env_soundscape` | `2fort.Underground` | `S_ambient_underground.wav` | 2381.25 | 1.00 | Yes | Audio Player Device (`crd_audio_player_device`) |
| `indoor` | `env_soundscape` | `2fort.Indoor` | `S_ambient_indoors.wav` | 2381.25 | 1.00 | Yes | Audio Player Device (`crd_audio_player_device`) |

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
