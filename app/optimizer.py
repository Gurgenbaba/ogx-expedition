# app/optimizer.py
"""
Fleet Optimizer for OGX Expeditions.

Based on OGame mechanics:
- Expedition cargo capacity = sum of cargo capacity of sent ships
- The game CAPS loot at: base_loot × (cargo_capacity / needed_capacity)
  where needed_capacity ≈ 3× the loot found
- Sending more than needed cargo = wasted slots
- Combat ships protect against pirate losses
- More fleet points = higher chance of finding better loot / more ships

Key insight from your data:
- You send 15.010.000 GT per slot (cargo: 25.000 each = 375,250,000,000 capacity)
- Your average resource find: ~326 Mrd total resources
- You are HEAVILY over-capac — you could reduce GT and add combat ships
  without losing a single resource

Ship stats (OGame standard):
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

SHIP_STATS: dict[str, dict] = {
    "Kleiner Transporter":  {"cargo": 5_000,    "cost_metal": 2_000,   "cost_crystal": 2_000,  "attack": 5,        "shield": 10,   "hull": 400},
    "Großer Transporter":   {"cargo": 25_000,   "cost_metal": 6_000,   "cost_crystal": 6_000,  "attack": 5,        "shield": 25,   "hull": 1_200},
    "Leichter Jäger":       {"cargo": 50,       "cost_metal": 3_000,   "cost_crystal": 1_000,  "attack": 50,       "shield": 10,   "hull": 400},
    "Schwerer Jäger":       {"cargo": 100,      "cost_metal": 6_000,   "cost_crystal": 4_000,  "attack": 150,      "shield": 25,   "hull": 1_000},
    "Kreuzer":              {"cargo": 800,      "cost_metal": 20_000,  "cost_crystal": 7_000,  "attack": 400,      "shield": 50,   "hull": 2_700},
    "Schlachtschiff":       {"cargo": 1_500,    "cost_metal": 45_000,  "cost_crystal": 15_000, "attack": 1_000,    "shield": 200,  "hull": 6_000},
    "Schlachtkreuzer":      {"cargo": 750,      "cost_metal": 30_000,  "cost_crystal": 40_000, "attack": 700,      "shield": 400,  "hull": 7_000},
    "Bomber":               {"cargo": 500,      "cost_metal": 50_000,  "cost_crystal": 25_000, "attack": 1_000,    "shield": 500,  "hull": 7_500},
    "Zerstörer":            {"cargo": 2_000,    "cost_metal": 60_000,  "cost_crystal": 50_000, "attack": 2_000,    "shield": 500,  "hull": 11_000},
    "Todesstern":           {"cargo": 1_000_000,"cost_metal": 5_000_000,"cost_crystal":4_000_000,"attack":200_000, "shield":50_000,"hull":900_000},
    "Recycler":             {"cargo": 20_000,   "cost_metal": 10_000,  "cost_crystal": 6_000,  "attack": 1,        "shield": 10,   "hull": 1_600},
    "Spionagesonde":        {"cargo": 5,        "cost_metal": 0,       "cost_crystal": 1_000,  "attack": 0,        "shield": 0,    "hull": 100},
}


@dataclass
class FleetSlot:
    ships: dict[str, int] = field(default_factory=dict)  # ship_name → count

    @property
    def total_cargo(self) -> int:
        return sum(
            SHIP_STATS.get(name, {}).get("cargo", 0) * count
            for name, count in self.ships.items()
        )

    @property
    def total_count(self) -> int:
        return sum(self.ships.values())

    @property
    def total_attack(self) -> int:
        return sum(
            SHIP_STATS.get(name, {}).get("attack", 0) * count
            for name, count in self.ships.items()
        )

    @property
    def fleet_points(self) -> int:
        """Approximate fleet points (based on build cost / 1000)."""
        return sum(
            ((SHIP_STATS.get(n, {}).get("cost_metal", 0) +
              SHIP_STATS.get(n, {}).get("cost_crystal", 0)) // 1000) * c
            for n, c in self.ships.items()
        )


@dataclass
class OptimizerInput:
    available_ships: dict[str, int]   # ship_name → available count
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


def optimize_fleet(inp: OptimizerInput) -> OptimizerResult:
    """
    Recommend optimal fleet composition per expedition slot.

    Strategy:
    1. Calculate needed cargo capacity (= avg total loot + 20% buffer)
    2. Fill cargo with most efficient cargo ships (GT > Recycler > KT)
    3. Fill remaining ship slots with combat ships to protect against pirates
    4. Divide across slots, respecting max_ships_per_slot

    Key constraint: total ships across all slots ≤ available ships
    """
    warnings = []
    avg_total_loot = inp.avg_loot_metal + inp.avg_loot_crystal + inp.avg_loot_deut
    needed_cargo = int(avg_total_loot * 1.2)  # 20% buffer

    # --- Step 1: How many GT needed per slot? ---
    gt_per_slot = needed_cargo // SHIP_STATS["Großer Transporter"]["cargo"]
    gt_per_slot = min(gt_per_slot, inp.max_ships_per_slot)

    # Actual available GT per slot
    avail_gt = inp.available_ships.get("Großer Transporter", 0)
    gt_total_available = avail_gt

    gt_per_slot_actual = min(gt_per_slot, gt_total_available // inp.slots)

    cargo_per_slot_with_gt = gt_per_slot_actual * SHIP_STATS["Großer Transporter"]["cargo"]
    cargo_deficit = max(0, needed_cargo - cargo_per_slot_with_gt)

    # --- Step 2: Fill remaining ships with combat ---
    remaining_slots_per_fleet = inp.max_ships_per_slot - gt_per_slot_actual

    # Combat ship priority: Schlachtkreuzer > Schlachtschiff > Zerstörer
    combat_priority = ["Schlachtkreuzer", "Schlachtschiff", "Zerstörer", "Bomber", "Kreuzer"]
    combat_ships_per_slot: dict[str, int] = {}

    remaining = remaining_slots_per_fleet
    for ship in combat_priority:
        avail = inp.available_ships.get(ship, 0)
        if avail <= 0 or remaining <= 0:
            continue
        per_slot = min(avail // inp.slots, remaining)
        if per_slot > 0:
            combat_ships_per_slot[ship] = per_slot
            remaining -= per_slot

    # --- Build slot ---
    slot_ships = {"Großer Transporter": gt_per_slot_actual}
    slot_ships.update(combat_ships_per_slot)

    slot = FleetSlot(ships=slot_ships)

    # --- Analysis ---
    total_attack = slot.total_attack
    pirate_threshold = 20_000_000  # typical pirate fleet strength from data
    win_chance_estimate = min(95, int(50 + (total_attack / pirate_threshold) * 10))

    # GT reduction vs current setup
    current_gt_per_slot = inp.max_ships_per_slot  # user currently sends all GT
    gt_reduction = current_gt_per_slot - gt_per_slot_actual
    gt_freed = gt_reduction * inp.slots

    analysis = {
        "needed_cargo_per_slot": needed_cargo,
        "cargo_covered_per_slot": slot.total_cargo,
        "cargo_coverage_pct": int(slot.total_cargo / needed_cargo * 100) if needed_cargo else 100,
        "total_ships_per_slot": slot.total_count,
        "fleet_points_per_slot": slot.fleet_points,
        "estimated_pirate_win_pct": win_chance_estimate,
        "gt_freed_total": gt_freed,
        "gt_per_slot_recommended": gt_per_slot_actual,
        "gt_per_slot_current": current_gt_per_slot,
        "combat_ships_added": sum(combat_ships_per_slot.values()),
    }

    # Warnings
    if gt_per_slot_actual * SHIP_STATS["Großer Transporter"]["cargo"] < needed_cargo:
        warnings.append(
            f"Cargo deficit: {cargo_deficit:,.0f} cargo short per slot. "
            "Consider keeping more GT or add Recycler."
        )
    if cargo_per_slot_with_gt > needed_cargo * 3:
        warnings.append(
            "You are sending far more cargo than needed. "
            f"You could free up ~{gt_freed:,} GT across all slots and add combat ships."
        )
    if not combat_ships_per_slot:
        warnings.append(
            "No combat ships available. Pirate encounters will be risky. "
            "Consider adding Schlachtkreuzer or Schlachtschiff."
        )
    if win_chance_estimate < 50:
        warnings.append("Low estimated win chance vs pirates. Add more combat ships.")

    return OptimizerResult(
        recommended_slots=[slot] * inp.slots,
        analysis=analysis,
        warnings=warnings,
    )


def get_user_stats_summary(expeditions: list) -> dict:
    """
    Compute aggregate stats from a list of Expedition ORM objects.
    Used to seed the optimizer with real historical data.
    """
    if not expeditions:
        return {}

    total = len(expeditions)
    success_res = [e for e in expeditions if e.outcome_type.startswith("success")]
    losses = [e for e in expeditions if e.outcome_type in ("storm", "contact_lost", "gravity", "vanished", "pirates_win", "pirates_loss")]
    vanished = [e for e in expeditions if e.outcome_type == "vanished"]
    failed = [e for e in expeditions if e.outcome_type == "failed"]

    # Average resources from successful resource runs
    res_runs = [e for e in expeditions if e.metal > 0]
    avg_metal = int(sum(e.metal for e in res_runs) / len(res_runs)) if res_runs else 0
    avg_crystal = int(sum(e.crystal for e in res_runs) / len(res_runs)) if res_runs else 0
    avg_deut = int(sum(e.deuterium for e in res_runs) / len(res_runs)) if res_runs else 0

    # Total gains
    total_metal = sum(e.metal for e in expeditions)
    total_crystal = sum(e.crystal for e in expeditions)
    total_deut = sum(e.deuterium for e in expeditions)
    total_dm = sum(e.dark_matter for e in expeditions)

    # Loss analysis
    gt_losses = sum(
        abs(e.ships_delta.get("Großer Transporter", 0))
        for e in expeditions
        if e.ships_delta
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
