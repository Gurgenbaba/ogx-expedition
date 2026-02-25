# app/optimizer.py
"""
Fleet Optimizer for OGX Expeditions — v3

Modes:
  safe       - maximize cargo (GT + Recycler first), then combat
  balanced   - 80% cargo target, fill rest with best combat ships
  aggressive - 50% cargo, max combat ships for higher loot tier + pirate wins

OGame mechanics:
  - Cargo cap: loot is hard-capped at your cargo capacity per slot
  - Fleet points = sum(build_cost/1000) → higher points = better loot rolls
  - Combat ships protect against pirates (GT losses)
  - Recycler have very high cargo (20k) and count as fleet points
"""
from __future__ import annotations
from dataclasses import dataclass, field

SHIP_STATS: dict[str, dict] = {
    "Kleiner Transporter":  {"cargo": 5_000,     "attack": 5,       "points": 4,      "type": "cargo"},
    "Großer Transporter":   {"cargo": 25_000,    "attack": 5,       "points": 12,     "type": "cargo"},
    "Recycler":             {"cargo": 20_000,    "attack": 1,       "points": 16,     "type": "cargo"},
    "Leichter Jäger":       {"cargo": 50,        "attack": 50,      "points": 4,      "type": "combat"},
    "Schwerer Jäger":       {"cargo": 100,       "attack": 150,     "points": 10,     "type": "combat"},
    "Kreuzer":              {"cargo": 800,       "attack": 400,     "points": 27,     "type": "combat"},
    "Schlachtschiff":       {"cargo": 1_500,     "attack": 1_000,   "points": 60,     "type": "combat"},
    "Schlachtkreuzer":      {"cargo": 750,       "attack": 700,     "points": 70,     "type": "combat"},
    "Bomber":               {"cargo": 500,       "attack": 1_000,   "points": 75,     "type": "combat"},
    "Zerstörer":            {"cargo": 2_000,     "attack": 2_000,   "points": 110,    "type": "combat"},
    "Todesstern":           {"cargo": 1_000_000, "attack": 200_000, "points": 9_000,  "type": "combat"},
    "Kleiner Transporter":  {"cargo": 5_000,     "attack": 5,       "points": 4,      "type": "cargo"},
    "Spionagesonde":        {"cargo": 5,         "attack": 0,       "points": 1,      "type": "support"},
}

# Priority order for filling cargo budget (best cargo/slot efficiency)
CARGO_SHIPS   = ["Großer Transporter", "Recycler", "Kleiner Transporter"]
# Priority order for combat (best attack/slot)
COMBAT_SHIPS  = ["Zerstörer", "Schlachtschiff", "Bomber", "Schlachtkreuzer", "Kreuzer", "Schwerer Jäger", "Leichter Jäger"]


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
    available_ships: dict[str, int]   # TOTAL fleet owned (will be divided by slots)
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
                cargo_target: int, combat_budget: int) -> FleetSlot:
    """
    Build a slot:
    1. Fill cargo ships up to cargo_target (by cargo value, highest first)
    2. Fill remaining ship budget with combat ships (best attack first)
    """
    ships: dict[str, int] = {}
    remaining_budget = max_ships
    remaining_cargo_need = cargo_target

    # --- Step 1: Fill cargo ships ---
    for ship in CARGO_SHIPS:
        if remaining_budget <= 0 or remaining_cargo_need <= 0:
            break
        avail = available.get(ship, 0)
        if avail <= 0:
            continue
        cargo_per_ship = SHIP_STATS[ship]["cargo"]
        # How many do we need to cover remaining cargo?
        needed = max(1, -(-remaining_cargo_need // cargo_per_ship))  # ceiling
        take = min(avail, needed, remaining_budget)
        if take > 0:
            ships[ship] = take
            remaining_budget -= take
            remaining_cargo_need -= take * cargo_per_ship

    # --- Step 2: Fill combat ships ---
    remaining_combat = min(combat_budget, remaining_budget)
    for ship in COMBAT_SHIPS:
        if remaining_combat <= 0:
            break
        avail = available.get(ship, 0)
        if avail <= 0:
            continue
        take = min(avail, remaining_combat)
        if take > 0:
            ships[ship] = take
            remaining_combat -= take

    return FleetSlot(ships=ships)


def _win_estimate(total_attack: int) -> int:
    """Estimate pirate win chance from attack power."""
    # From expedition data: ~20M attack ≈ 50%, scales up from there
    if total_attack <= 0:
        return 5
    raw = 30 + int((total_attack / 20_000_000) * 25)
    return min(98, max(5, raw))


def _cargo_coverage(slot: FleetSlot, needed_cargo: int) -> int:
    if needed_cargo <= 0:
        return 100
    return min(999, int(slot.total_cargo / needed_cargo * 100))


def optimize_fleet(inp: OptimizerInput) -> OptimizerResult:
    warnings: list[str] = []
    avg_total_loot = inp.avg_loot_metal + inp.avg_loot_crystal + inp.avg_loot_deut
    needed_cargo   = int(avg_total_loot * 1.2)  # 20% buffer

    cap = inp.max_ships_per_slot

    # Divide total fleet by slots — all 7 fly simultaneously
    per_slot: dict[str, int] = {
        ship: count // max(inp.slots, 1)
        for ship, count in inp.available_ships.items()
        if count > 0
    }

    # --- Current setup (total fleet ÷ slots, capped to max_ships_per_slot) ---
    current_slot = _build_slot(per_slot, cap, needed_cargo, cap)

    modes = {}
    for mode in ("safe", "balanced", "aggressive"):
        if mode == "safe":
            # 100% cargo target — fill cargo first, then combat with leftovers
            cargo_target  = needed_cargo
            combat_budget = cap  # leftovers after cargo go to combat

        elif mode == "balanced":
            # 80% cargo target — meaningful combat presence
            cargo_target  = int(needed_cargo * 0.80)
            combat_budget = cap  # remaining ships after 80% cargo → combat

        else:  # aggressive
            # 50% cargo target — pack combat for higher loot tier + pirate wins
            cargo_target  = int(needed_cargo * 0.50)
            combat_budget = cap

        slot = _build_slot(per_slot, cap, cargo_target, combat_budget)
        coverage = _cargo_coverage(slot, needed_cargo)
        win      = _win_estimate(slot.total_attack)
        deficit  = max(0, needed_cargo - slot.total_cargo)

        mode_warnings = []
        if deficit > 0:
            mode_warnings.append(
                f"Cargo deficit: {deficit/1e9:.1f} Mrd short. "
                "Some loot may not be fully collected."
            )
        if coverage > 300:
            mode_warnings.append(
                f"Cargo overcapacity ({coverage}% of needed). "
                "Consider replacing some cargo ships with combat ships."
            )
        if slot.total_attack == 0:
            mode_warnings.append("No combat ships — pirate encounters will be very risky.")
        elif win < 40:
            mode_warnings.append("Low pirate win chance. Consider more combat ships.")

        # GT freed vs safe mode (how many GT you could reduce)
        safe_gt = sum(
            v for k, v in _build_slot(per_slot, cap, needed_cargo, 0).ships.items()
            if k == "Großer Transporter"
        )
        mode_gt = slot.ships.get("Großer Transporter", 0)
        gt_freed = max(0, safe_gt - mode_gt) * inp.slots

        modes[mode] = {
            "slot": slot,
            "gt_used": mode_gt,
            "cargo_coverage": coverage,
            "cargo_deficit": deficit,
            "win_est": win,
            "gt_freed_total": gt_freed,
            "warnings": mode_warnings,
        }

    analysis = {
        "needed_cargo": needed_cargo,
        "avg_total_loot": avg_total_loot,
        "current": {
            "slot": current_slot,
            "gt_used": current_slot.ships.get("Großer Transporter", 0),
            "cargo_coverage": _cargo_coverage(current_slot, needed_cargo),
            "win_est": _win_estimate(current_slot.total_attack),
        },
        **modes,
    }

    return OptimizerResult(
        recommended_slots=[modes["balanced"]["slot"]] * inp.slots,
        analysis=analysis,
        warnings=warnings,
    )


def get_user_stats_summary(expeditions: list) -> dict:
    if not expeditions:
        return {}

    total       = len(expeditions)
    success_res = [e for e in expeditions if e.outcome_type.startswith("success")]
    losses      = [e for e in expeditions if e.outcome_type in ("storm", "contact_lost", "gravity", "vanished", "pirates_win", "pirates_loss")]
    vanished    = [e for e in expeditions if e.outcome_type == "vanished"]
    failed      = [e for e in expeditions if e.outcome_type == "failed"]
    res_runs    = [e for e in expeditions if e.metal > 0]

    avg_metal   = int(sum(e.metal for e in res_runs) / len(res_runs)) if res_runs else 0
    avg_crystal = int(sum(e.crystal for e in res_runs) / len(res_runs)) if res_runs else 0
    avg_deut    = int(sum(e.deuterium for e in res_runs) / len(res_runs)) if res_runs else 0

    return {
        "total": total,
        "success_count": len(success_res),
        "loss_event_count": len(losses),
        "vanished_count": len(vanished),
        "failed_count": len(failed),
        "success_rate_pct": int(len(success_res) / total * 100) if total else 0,
        "vanish_rate_pct":  int(len(vanished) / total * 100) if total else 0,
        "avg_metal":    avg_metal,
        "avg_crystal":  avg_crystal,
        "avg_deut":     avg_deut,
        "avg_total_res": avg_metal + avg_crystal + avg_deut,
        "total_metal":   sum(e.metal for e in expeditions),
        "total_crystal": sum(e.crystal for e in expeditions),
        "total_deut":    sum(e.deuterium for e in expeditions),
        "total_dm":      sum(e.dark_matter for e in expeditions),
        "total_resources": sum(e.metal + e.crystal + e.deuterium for e in expeditions),
        "total_gt_lost": sum(
            abs(e.ships_delta.get("Großer Transporter", 0))
            for e in expeditions if e.ships_delta
        ),
    }
