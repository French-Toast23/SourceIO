# Team Fortress 2 to UEFN Visual Effects (VFX) Mapping

This document details the mapping of Team Fortress 2 particle systems (PCF format) to Unreal Editor for Fortnite (UEFN) Niagara particle systems, Visual Effect (VFX) Spawner devices, and Creative device effects.

---

## 1. Technical Context

Source Engine particle effects are authored in Valve's proprietary **PCF (Particle Configuration File)** format.
- PCF files contain binary operators, sprite card sheets, and simulation initializers evaluated by the Source 1 particle engine (`particles/tf_particles.pcf`).
- There is no direct binary or mathematical converter from PCF into Unreal Engine's **Niagara** simulation graph due to differing physics models, HLSL GPU simulation shaders, and curve interpolations.
- Therefore, each required TF2 visual effect is mapped to either:
  1. A UEFN **VFX Spawner Device** preset or built-in particle effect.
  2. A custom UEFN **Niagara System** (`NS_*`) recreating the timing, sprite texture, and emitter behavior.
  3. A specialized UEFN gameplay device with built-in visuals (e.g. `Explosive Device`, `Capture Area Device`, `Beacon Device`).

---

## 2. Core Gamemode VFX Mapping Table

| TF2 Particle Effect | TF2 PCF Identifier | Visual Description | Target UEFN Effect / Device | Implementation Notes |
|:---|:---|:---|:---|:---|
| **Rocket Explosion** | `rockettrail_explosion` | Orange fireball, black smoke puffs, shrapnel sparks | `Explosive Device` Explosion FX or `NS_Rocket_Explosion` | Triggered at projectile impact point via Verse `Explode()` |
| **Rocket Smoke Trail** | `rockettrail` | Continuous swirling white/gray smoke puff trail | `NS_Rocket_Trail` (Ribbon + Sprite Emitter) | Attached to projectile actor socket |
| **Stickybomb Detonation** | `stickybomb_explosion` | Quick concussive burst, radial dust ring | `Explosive Device` Shockwave FX | Instantaneous radial decal + shockwave ring |
| **Flamethrower Flame** | `flamethrower` | Expanding cone of swirling red/blue fire particles | `VFX Spawner Device` (Fire Burst) or `NS_Flame_Cone` | Spawned from weapon nozzle; Verse damage volume follows flame cone |
| **Medigun Healing Beam** | `medicgun_beam_red` / `blue` | Flowing sine-wave energy lightning beam from nozzle to heal target | `NS_Medigun_Beam` (Spline / Ribbon Particle with UV scrolling) | Spline endpoints updated per tick to Medic nozzle and heal target root |
| **Ubercharge Visual** | `ubercharge_red` / `blue` | Glowing golden/team-colored metallic sheen over character model | Dynamic Material Instance (`MI_Ubercharge`) with Emissive Fresnel pulse | Material overlay applied to character mesh components on trigger |
| **Muzzle Flash** | `muzzle_shotgun` / `minigun` | Brief star-burst flash + smoke ring | `VFX Spawner Device` (Flash) or `NS_Muzzle_Flash` | Single-frame sprite emitter at weapon muzzle bone |
| **Capture Point Hologram** | `cap_point_base` | Vertical light column / holographic ring | `Beacon Device` (`crd_beacon_device`) | Pulse speed increases as capture percentage rises |
| **Capture Success Flare** | `cappoint_capture` | Fireworks / confetti burst in capturing team color | `Celebration Device` or `NS_Capture_Burst` | Fired when capture reaches 100% |
| **Resupply / Health Pickup** | `healthkit_pickup` | Green cross particle burst | `Visual Effect Spawner` (Health Aura) | Attached to pickup location on pickup trigger |
| **Critical Hit Sparkle** | `crit_hit_text` / `crit_sparks` | Floating "CRIT!" text and lightning sparks on weapon | `Text Render Actor` or `Damage Popup Device` + `NS_Crit_Sparks` | Triggered when damage roll is critical |
| **Teleporter Swirl** | `teleport_red` / `blue` | Spinning team-colored vortex at entrance and exit pad | `VFX Spawner Device` (Vortex / Swirl) | Placed at Teleporter entrance/exit pads |
| **Water Splash** | `water_splash` | Splash spray and expanding surface ripples | `Water Body Device` / `NS_Water_Splash` | Triggered when player or projectile crosses water plane ($Z = -220$) |

---

## 3. Implementation Guidelines for UEFN

1. **Niagara System Construction**:
   - Master sprite cards (e.g. smoke puffs, fireball textures) are converted from TF2's `effects/` VTF textures into uncompressed PNGs and assigned to Niagara particle materials.
   - For beam effects (Medigun beam, Sniper laser sight): use a Niagara Ribbon Renderer driven by start and end position parameters exposed to Verse.
2. **Device-Driven Alternatives**:
   - Where a built-in Fortnite Creative device already provides a polished visual (such as the `Explosive Device` or `Beacon Device`), prefer the device to minimize custom asset cooking and memory footprint.
