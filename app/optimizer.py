# app/optimizer.py
"""
Fleet Optimizer for OGX Expeditions.

Modes:
  safe       - maximize cargo coverage, minimize losses
  balanced   - good cargo + solid combat protection
  aggressive - lean cargo, maximum combat ships (high loot potential)

OGame expedition mechanics:
  - Cargo cap: loot is capped at your cargo capacity
  - More fleet points = higher loot tier (more resources/ships found)
  - Combat ships = higher chance to beat pirates (no GT losses)
  - GT are the weakest ships — pure cargo, zero combat value
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

SHIP_STATS: dict[str, dict] = {
    "Kleiner Transporter":  {"cargo": 5_000,     "attack": 5,       "points": 4,      "type": "cargo"},
    "Großer Transporter":   {"cargo": 25_000,    "attack": 5,       "points": 12,     "type": "cargo"},
    "Leichter Jäger":       {"cargo": 50,        "attack": 50,      "points": 4,      "type": "combat"},
    "Schwerer Jäger":       {"cargo": 100,       "attack": 150,     "points": 10,     "type": "combat"},
    "Kreuzer":              {"cargo": 800,       "attack": 400,     "points": 27,     "type": "combat"},
    "Schlachtschiff":       {"cargo": 1_500,     "attack": 1_000,   "points": 60,     "type": "combat"},
    "Schlachtkreuzer":      {"cargo": 750,       "attack": 700,     "points": 70,     "type": "combat"},
    "Bomber":               {"cargo": 500,       "attack": 1_000,   "points": 75,     "type": "combat"},
    "Zerstörer":            {"cargo": 2_000,     "attack": 2_000,   "points": 110,    "type": "combat"},
    "Todesstern":           {"cargo": 1_000_000, "attack": 200_000, "points": 9_000,  "type": "combat"},
    "Recycler":             {"cargo": 20_000,    "attack": 1,       "points": 16,     "type": "cargo"},
    "Spionagesonde":        {"cargo": 5,         "attack": 0,       "points": 1,      "type": "support"},
}

# Combat ships ordered by attack/slot efficiency
COMBAT_PRIORITY = ["Zerstörer", "Schlachtschiff", "Bomber", "Schlachtkreuzer", "Kreuzer", "Schwerer Jäger", "Leichter Jäger"]
CARGO_PRIORITY  = ["Großer Transporter", "Recycler", "Kleiner Transporter"]


@dataclass
class FleetSlot:
    ships: dict[str, int] = field(default_factory=dict)

    @property
    def total_cargo(self) -> int:
        return sum(SHIP_STATS.get(n, {}).get("cargo", 0) * c for n, c in self.ships.items())

    @property
    def total_count(self) -> int:
        return sum(self.ships.values())

    @property
    def total_attack(self) -> int:
        return sum(SHIP_STATS.get(n, {}).get("attack", 0) * c for n, c in self.ships.items())

    @property
    def total_points(self) -> int:
        return sum(SHIP_STATS.get(n, {}).get("points", 0) * c for n, c in self.ships.items())


@dataclass
class OptimizerInput:
    available_ships: dict[str, int]   # ships available PER SLOT
    slots: int = 7
    max_ships_per_slot: int = 15_010_000
    avg_loot_metal: int = 163_000_000_000
    avg_loot_crystal: int = 108_000_000_000
    avg_loot_deut: int = 55_000_000_000


@dataclass
class OptimizerResult:
    recommended_slots: list[FleetSlot]
    analysis: dict
    warnings: list[str]


def _build_slot(available: dict[str, int], max_ships: int,
                gt_count: int, combat_budget: int) -> FleetSlot:
    """Build a single slot with given GT count + fill remaining budget with combat ships."""
    ships: dict[str, int] = {}

    if gt_count > 0:
        ships["Großer Transporter"] = gt_count

    remaining = combat_budget
    for ship in COMBAT_PRIORITY:
        avail = available.get(ship, 0)
        if avail <= 0 or remaining <= 0:
            continue
        take = min(avail, remaining)
        if take > 0:
            ships[ship] = take
            remaining -= take

    return FleetSlot(ships=ships)


def optimize_fleet(inp: OptimizerInput) -> OptimizerResult:
    warnings: list[str] = []
    avg_total_loot = inp.avg_loot_metal + inp.avg_loot_crystal + inp.avg_loot_deut
    needed_cargo   = int(avg_total_loot * 1.2)   # 20% buffer
    gt_cargo       = SHIP_STATS["Großer Transporter"]["cargo"]  # 25,000

    avail_gt = inp.available_ships.get("Großer Transporter", 0)

    # ── How much cargo do combat ships already provide? ───────────────────────
    # Fill a full slot with pure combat ships and measure their cargo contribution
    combat_only_slot = _build_slot(inp.available_ships, inp.max_ships_per_slot, 0, inp.max_ships_per_slot)
    combat_cargo = combat_only_slot.total_cargo

    # ── Three modes ──────────────────────────────────────────────────────────
    results = {}

    for mode in ("safe", "balanced", "aggressive"):
        if mode == "safe":
            # Max cargo: fill with GT first, top up with combat
            gt_needed     = max(0, -(-needed_cargo // gt_cargo))
            gt_used       = min(gt_needed, avail_gt, inp.max_ships_per_slot)
            combat_budget = max(0, inp.max_ships_per_slot - gt_used)

        elif mode == "balanced":
            # Cover 80% of needed cargo with GT, rest with combat ships
            target_gt_cargo = int(needed_cargo * 0.80)
            gt_used = min(
                -(-target_gt_cargo // gt_cargo),
                avail_gt,
                inp.max_ships_per_slot,
            )
            combat_budget = max(0, inp.max_ships_per_slot - gt_used)

        else:  # aggressive
            # Minimum GT to cover 50% of needed cargo — pack the rest with combat
            target_gt_cargo = int(needed_cargo * 0.50)
            gt_used = min(
                -(-target_gt_cargo // gt_cargo),
                avail_gt,
                inp.max_ships_per_slot,
            )
            combat_budget = max(0, inp.max_ships_per_slot - gt_used)

        slot = _build_slot(inp.available_ships, inp.max_ships_per_slot, gt_used, combat_budget)

        cargo_coverage = int(slot.total_cargo / needed_cargo * 100) if needed_cargo else 100
        cargo_deficit  = max(0, needed_cargo - slot.total_cargo)

        # Pirate win chance estimate based on attack power
        # From your data: ~20M attack ≈ 50% win chance
        pirate_threshold = 20_000_000
        raw_win = 30 + int((slot.total_attack / pirate_threshold) * 25)
        win_est = min(98, max(5, raw_win))

        mode_warnings = []
        if cargo_deficit > 0:
            mode_warnings.append(
                f"Cargo deficit: {cargo_deficit / 1e9:.1f} Mrd short per slot. "
                "Some loot may be uncollectable."
            )
        if cargo_coverage > 300:
            gt_excess = gt_used - (-(-needed_cargo // gt_cargo))
            mode_warnings.append(
                f"Heavy cargo overcapacity ({cargo_coverage}% of needed). "
                f"~{gt_excess:,} GT could be replaced with combat ships."
            )
        if slot.total_attack == 0:
            mode_warnings.append("No combat ships — pirate encounters will be very risky.")
        elif win_est < 40:
            mode_warnings.append("Low pirate win chance. Consider adding more combat ships.")

        results[mode] = {
            "slot": slot,
            "gt_used": gt_used,
            "combat_budget": combat_budget,
            "cargo_coverage": cargo_coverage,
            "cargo_deficit": cargo_deficit,
            "win_est": win_est,
            "warnings": mode_warnings,
        }

    # ── Current setup (what user entered) ─────────────────────────────────────
    current_gt_capped = min(avail_gt, inp.max_ships_per_slot)
    current_slot = _build_slot(
        inp.available_ships,
        inp.max_ships_per_slot,
        current_gt_capped,
        max(0, inp.max_ships_per_slot - current_gt_capped),
    )
    current_coverage = int(current_slot.total_cargo / needed_cargo * 100) if needed_cargo else 100
    current_win = min(98, max(5, 30 + int((current_slot.total_attack / 20_000_000) * 25)))

    analysis = {
        "needed_cargo": needed_cargo,
        "avg_total_loot": avg_total_loot,
        "current": {
            "slot": current_slot,
            "cargo_coverage": current_coverage,
            "win_est": current_win,
            "gt_used": current_gt_capped,
        },
        "safe":       results["safe"],
        "balanced":   results["balanced"],
        "aggressive": results["aggressive"],
    }

    # Top-level warnings (independent of mode)
    if avail_gt == 0 and combat_cargo < needed_cargo:
        warnings.append("No GT entered and combat ships don't cover needed cargo. Add GT or Recycler.")

    return OptimizerResult(
        recommended_slots=[results["balanced"]["slot"]] * inp.slots,
        analysis=analysis,
        warnings=warnings,
    )


def get_user_stats_summary(expeditions: list) -> dict:
    if not expeditions:
        return {}

    total = len(expeditions)
    success_res = [e for e in expeditions if e.outcome_type.startswith("success")]
    losses      = [e for e in expeditions if e.outcome_type in ("storm", "contact_lost", "gravity", "vanished", "pirates_win", "pirates_loss")]
    vanished    = [e for e in expeditions if e.outcome_type == "vanished"]
    failed      = [e for e in expeditions if e.outcome_type == "failed"]

    res_runs    = [e for e in expeditions if e.metal > 0]
    avg_metal   = int(sum(e.metal for e in res_runs) / len(res_runs)) if res_runs else 0
    avg_crystal = int(sum(e.crystal for e in res_runs) / len(res_runs)) if res_runs else 0
    avg_deut    = int(sum(e.deuterium for e in res_runs) / len(res_runs)) if res_runs else 0

    total_metal   = sum(e.metal for e in expeditions)
    total_crystal = sum(e.crystal for e in expeditions)
    total_deut    = sum(e.deuterium for e in expeditions)
    total_dm      = sum(e.dark_matter for e in expeditions)
    gt_losses     = sum(
        abs(e.ships_delta.get("Großer Transporter", 0))
        for e in expeditions if e.ships_delta
    )

    return {
        "total": total,
        "success_count": len(success_res),
        "loss_event_count": len(losses),
        "vanished_count": len(vanished),
        "failed_count": len(failed),
        "success_rate_pct": int(len(success_res) / total * 100) if total else 0,
        "vanish_rate_pct": int(len(vanished) / total * 100) if total else 0,
        "avg_metal": avg_metal,
        "avg_crystal": avg_crystal,
        "avg_deut": avg_deut,
        "avg_total_res": avg_metal + avg_crystal + avg_deut,
        "total_metal": total_metal,
        "total_crystal": total_crystal,
        "total_deut": total_deut,
        "total_dm": total_dm,
        "total_resources": total_metal + total_crystal + total_deut,
        "total_gt_lost": gt_losses,
    }
