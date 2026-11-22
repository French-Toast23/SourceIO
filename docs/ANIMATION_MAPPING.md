# Team Fortress 2 to UEFN Animation Mapping

This document details the animation pipeline from SourceIO MDL animation imports to Unreal Editor for Fortnite (UEFN), evaluating custom skeletal meshes, character rigs, weapon animations, prop movers, and UEFN engine constraints.

---

## 1. Verified UEFN Animation Constraints & Capabilities

Based on official UEFN and Epic Games documentation:
- **Custom Skeletal Meshes (`SKM_*`)**:
  - Fully supported for import via FBX (binary FBX 2013–2020).
  - Maximum bone influence per vertex: 4 bones (for mobile/Switch compatibility) or up to 8 bones (desktop/high-end).
  - Maximum bones per skeleton: 256–512 recommended for runtime performance.
- **Custom Animation Sequences (`AS_*`)**:
  - FBX files containing skeletal keyframe animations can be imported as `AnimSequence` assets.
  - Can be driven by Animation Blueprints (AnimBP), Control Rig, or Level Sequencer (`Cinematic Sequence Device`).
- **Player Character Animation Constraints in UEFN**:
  - Fortnite player characters use the standardized Fortnite humanoid skeleton (`UEFN Mannequin` / `Fortnite Skeleton`).
  - Player locomotion (running, sprinting, crouching, sliding, jumping) is driven by Fortnite's internal player character movement component and animation graph.
  - **Custom Player Rigs**: While an NPC Spawner or Character Device can display a custom skeletal mesh with custom anims, applying custom animations directly to active playable players in place of Fortnite's standard locomotion is restricted by the Fortnite player capsule system.
  - **First-Person Viewmodels**: UEFN does not provide a native first-person weapon viewmodel camera arm system by default (though the Orbit Camera / Fixed Point Camera and custom Verse camera rigs can approximate first-person perspective).

---

## 2. Animation Categories & Export Paths

### 2.1 Mechanical & Prop Animations (Full Custom Export)
Props with articulated moving parts, doors, resupply lockers, and capture point mechanisms are exported as Skeletal Meshes with associated AnimSequences or handled via UEFN Mover devices:

| Asset | TF2 Model Path | Bones / Rigs | Sequences | UEFN Implementation |
|:---|:---|:---|:---|:---|
| **Respawn Doors** | `models/props_gameplay/door_slide.mdl` | Root, Door_Left, Door_Right | `open`, `close`, `idle` | `Mover Device` or Skeletal Mesh with AnimSequence triggered via Verse `OnTriggered()` |
| **Resupply Cabinet** | `models/props_gameplay/resupply_locker.mdl` | Root, Door_L, Door_R | `open`, `close` | Skeletal Mesh AnimSequence triggered when player touches locker collision |
| **Intelligence Briefcase** | `models/flag/briefcase.mdl` | Root, Handle, Latches | `spin`, `idle` | Custom Skeletal Mesh or Static Mesh with Niagara trail attached to player back |
| **Control Point Hologram** | `models/props_gameplay/cap_point_base.mdl` | Base, Hologram_Arm | `idle`, `capturing` | Skeletal Mesh + Material Parameter collection for team color pulse |
| **Sentry Gun Level 1** | `models/buildables/sentry1.mdl` | Root, Pitch, Yaw, Gun | `aim_pitch`, `aim_yaw`, `fire` | Verse-controlled bone rotation or Cinematic Sequence Device |

### 2.2 TF2 Class Models & Character Animations

SourceIO can import TF2 character MDLs (e.g. `scout.mdl`, `heavy.mdl`, `soldier.mdl`) including armatures and animation sequences (`run_PRIMARY`, `idle_SECONDARY`, `attack_MELEE`, etc.).

#### Options for UEFN:
1. **Option A: NPC Spawner / Custom Character Device (Recommended for bots, dioramas, and custom NPCs)**:
   - Import the TF2 class model as a Skeletal Mesh (`SKM_Scout`, `SKM_Heavy`).
   - Import TF2 animation sequences as AnimSequences (`AS_Scout_Run`, `AS_Heavy_Spin`).
   - Assign to `Custom Character Device` or `NPC Spawner Device`.
2. **Option B: Fortnite Humanoid Player with Custom Weapon/Emote Devices (Recommended for live playable TF2 classes)**:
   - Use Fortnite player character models configured with team loadouts and class speeds (via Verse `fort_character` movement tuning).
   - Use Item Granter and Custom Weapons (e.g. Rocket Launcher, Shotgun, Minigun).
   - What is preserved: 100% fluid, responsive multiplayer networking, climbing, jumping, and combat feel.
   - What is approximated: Player visual model uses selected Fortnite character skins instead of the original 2007 Valve mesh.

### 2.3 Viewmodels & Weapon Animations

TF2 weapon models come in two forms:
- `v_models` (Viewmodels: arms and weapon close-up in first-person camera).
- `w_models` (Worldmodels: simplified models carried in player hands or dropped on the floor).

In UEFN:
- Worldmodels (`w_models`) are exported as Static Meshes and attached to player hands or item pick-up bases (`SM_w_rocketlauncher.fbx`, `SM_w_scattergun.fbx`).
- Viewmodel animations (`draw`, `fire`, `reload`) are exported for use with custom camera overlays or first-person camera rigs.
