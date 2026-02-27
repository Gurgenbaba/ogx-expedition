# app/parser.py
"""
OGX Expedition Message Parser — DE / EN / FR

Parses raw copy-pasted text from the OGame message inbox.
Each expedition message block is separated by the fleet command header line.

Supports all three OGame server languages:
  DE: Flottenkommando / Expeditionsbericht
  EN: Fleet Command / Expedition Report
  FR: Commandement de la flotte / Rapport d'expédition
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Multi-language keyword maps
# ---------------------------------------------------------------------------

# Outcome headlines — all three languages map to the same internal keys
OUTCOME_HEADLINES = {
    # German
    "Expedition erfolgreich":       "success",
    "Expedition gescheitert":       "failed",
    "Verschwinden der Flotte":      "vanished",
    "Ionensturm":                   "storm",
    "Kontakt verloren":             "contact_lost",
    "Gravitationsanomalie":         "gravity",
    "Expeditionsbericht: Erfolgreich": "success",
    "Keine Funde":                  "failed",
    "Piraten":                      "success",
    # English
    "Expedition successful":        "success",
    "Expedition failed":            "failed",
    "Fleet disappeared":            "vanished",
    "Ion Storm":                    "storm",
    "Lost contact":                 "contact_lost",
    "Gravity anomaly":              "gravity",
    "No finds":                     "failed",
    "Pirates":                      "success",
    # French
    "Expédition réussie":           "success",
    "Expédition compromise":        "success",  # pirate encounter — classified via pirate_strength
    "Expédition échouée":           "failed",
    "Disparition de la flotte":     "vanished",
    "Tempête ionique":              "storm",
    "Contact perdu":                "contact_lost",
    "Anomalie gravitationnelle":    "gravity",
    "Aucune découverte":            "failed",
    "Pirates":                      "success",  # same word
}

# Resource labels → internal key (all languages)
RESOURCE_LABELS = {
    # German
    "Metall":           "metal",
    "Kristall":         "crystal",
    "Deuterium":        "deuterium",
    "Dunkle Materie":   "dark_matter",
    # English
    "Metal":            "metal",
    "Crystal":          "crystal",
    "Deuterium":        "deuterium",
    "Dark Matter":      "dark_matter",
    # French
    "Métal":            "metal",
    "Cristal":          "crystal",
    "Deutérium":        "deuterium",
    "Matière noire":    "dark_matter",
}

# Ship names — all languages map to German canonical name (stored in DB as DE)
SHIP_NAME_MAP = {
    # German (canonical — stored as-is)
    "Kleiner Transporter":  "Kleiner Transporter",
    "Großer Transporter":   "Großer Transporter",
    "Leichter Jäger":       "Leichter Jäger",
    "Schwerer Jäger":       "Schwerer Jäger",
    "Kreuzer":              "Kreuzer",
    "Schlachtschiff":       "Schlachtschiff",
    "Schlachtkreuzer":      "Schlachtkreuzer",
    "Bomber":               "Bomber",
    "Zerstörer":            "Zerstörer",
    "Todesstern":           "Todesstern",
    "Recycler":             "Recycler",
    "Spionagesonde":        "Spionagesonde",
    "Solarsatellit":        "Solarsatellit",
    "Crawler":              "Crawler",
    "Reaper":               "Reaper",
    "Pathfinder":           "Pathfinder",
    # English → German canonical
    "Small Cargo":          "Kleiner Transporter",
    "Large Cargo":          "Großer Transporter",
    "Light Fighter":        "Leichter Jäger",
    "Heavy Fighter":        "Schwerer Jäger",
    "Cruiser":              "Kreuzer",
    "Battleship":           "Schlachtschiff",
    "Battlecruiser":        "Schlachtkreuzer",
    "Bomber":               "Bomber",
    "Destroyer":            "Zerstörer",
    "Deathstar":            "Todesstern",
    "Recycler":             "Recycler",
    "Espionage Probe":      "Spionagesonde",
    "Solar Satellite":      "Solarsatellit",
    "Crawler":              "Crawler",
    "Reaper":               "Reaper",
    "Pathfinder":           "Pathfinder",
    # French → German canonical
    "Petit Transporteur":   "Kleiner Transporter",
    "Grand Transporteur":   "Großer Transporter",
    "Chasseur Léger":       "Leichter Jäger",
    "Chasseur Lourd":       "Schwerer Jäger",
    "Croiseur":             "Kreuzer",
    "Vaisseau de Bataille": "Schlachtschiff",
    "Traqueur":             "Schlachtkreuzer",
    "Bombardier":           "Bomber",
    "Destructeur":          "Zerstörer",
    "Étoile de la Mort":    "Todesstern",
    "Recycleur":            "Recycler",
    "Sonde Espionnage":     "Spionagesonde",
    "Sonde d'Espionnage":   "Spionagesonde",
    "Satellite Solaire":    "Solarsatellit",
    "Crawler":              "Crawler",
    "Faucheur":             "Reaper",
    "Eclaireur":            "Pathfinder",
}

# All known ship names across all languages (for lookup)
SHIP_NAMES = set(SHIP_NAME_MAP.keys())

# ---------------------------------------------------------------------------
# Regex patterns — language-aware where needed
# ---------------------------------------------------------------------------
_RE_TIMESTAMP = re.compile(
    r"(\d{2}\.\d{2}(?:\.\d{2,4})?)\s+(\d{2}:\d{2}:\d{2})"
)
_RE_EXP_NUMBER = re.compile(r"EXP[EÉ]DITION\s*#(\d+)", re.IGNORECASE)

_RE_RESOURCE_LINE = re.compile(r"^([+-][\d.,]+)$")
_RE_SHIP_QTY      = re.compile(r"^([+-][\d.,]+)$")

# Loss percent — DE/EN/FR
_RE_LOSS_PERCENT = re.compile(
    r"(?:Verluste|Losses?|Pertes)\s*:\s*(\d+)\s*%",
    re.IGNORECASE
)
# Pirate strength — DE/EN/FR
_RE_PIRATE_STRENGTH = re.compile(
    r"(?:Feindsignaturen|Enemy signatures?|Signatures ennemies)\s*:\s*([\d.,]+)",
    re.IGNORECASE
)
# Pirate win chance — DE/EN/FR
_RE_PIRATE_WIN_CHANCE = re.compile(
    r"(?:Gesch[äa]tzter Sieg|Estimated victory|Victoire estim[ée]e?)\s*:\s*~(\d+)\s*%",
    re.IGNORECASE
)
# Pirate loss rate — DE/EN/FR
_RE_PIRATE_LOSS_RATE = re.compile(
    r"(?:Verlustrate|Loss rate|Taux de pertes)\s*:\s*(\d+)\s*%",
    re.IGNORECASE
)
# Dark matter bonus (Schwarzer Horizont / Black Horizon / Horizon Noir)
_RE_DM_BONUS = re.compile(
    r"(?:Schwarzer Horizont|Black Horizon|Horizon Noir)\s*:\s*\+?([\d.,]+)\s*\(\+(\d+)%\)",
    re.IGNORECASE
)
# Smuggler code
_RE_SMUGGLER_CODE = re.compile(r"\b(\d{4}-\d{4}-\d{4})\b")
_RE_SMUGGLER_TIER = re.compile(r"(?:Stufe|Level|Niveau)\s*(\d+)", re.IGNORECASE)

# Block header — all three languages
_BLOCK_HEADER = re.compile(
    r"\d{2}\.\d{2}(?:\.\d{2,4})?\s+\d{2}:\d{2}:\d{2}\s+"
    r"(?:"
    r"Flottenkommando\s+Expeditionsbericht"          # DE
    r"|Fleet Command\s+Expedition Report"             # EN
    r"|Commandement de la flotte\s+Rapport d['']exp[ée]dition"  # FR
    r")"
)


def _parse_num(s: str) -> int:
    """Parse '1.200.800' or '+179.941.271.650' or '-4.202.800' to int."""
    s = s.strip().lstrip("+")
    s = s.replace(".", "").replace(",", "").replace("\xa0", "").replace(" ", "")
    try:
        return int(s)
    except ValueError:
        return 0


def _parse_timestamp(date_str: str, time_str: str) -> Optional[datetime]:
    try:
        parts = date_str.split(".")
        if len(parts) == 2:
            day, month = int(parts[0]), int(parts[1])
            now = datetime.utcnow()
            year = now.year
            dt = datetime(year, month, day)
            if (dt - now).days > 60:
                year -= 1
        elif len(parts) == 3:
            day, month = int(parts[0]), int(parts[1])
            y = int(parts[2])
            year = 2000 + y if y < 100 else y
        else:
            return None
        h, m, sec = (int(x) for x in time_str.split(":"))
        return datetime(year, month, day, h, m, sec)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Parsed result dataclass
# ---------------------------------------------------------------------------
@dataclass
class ParsedExpedition:
    exp_number:           Optional[int]      = None
    returned_at:          Optional[datetime] = None
    outcome_raw:          str                = ""
    outcome_type:         str                = "failed"

    metal:                int                = 0
    crystal:              int                = 0
    deuterium:            int                = 0
    dark_matter:          int                = 0
    dark_matter_bonus:    int                = 0
    dark_matter_bonus_pct: int               = 0

    ships_delta: dict = field(default_factory=dict)

    loss_percent:      Optional[float] = None
    pirate_strength:   Optional[int]   = None
    pirate_win_chance: Optional[int]   = None
    pirate_loss_rate:  Optional[int]   = None

    raw_text:    str           = ""
    parse_error: Optional[str] = None

    smuggler_code: Optional[str] = None
    smuggler_tier: Optional[int] = None

    @property
    def dedup_key(self) -> str:
        if self.exp_number:
            return hashlib.sha256(f"exp#{self.exp_number}".encode()).hexdigest()[:32]
        s = f"{self.returned_at}|{self.outcome_type}|{self.metal}|{self.crystal}"
        return hashlib.sha256(s.encode()).hexdigest()[:32]

    @property
    def total_resources(self) -> int:
        return self.metal + self.crystal + self.deuterium

    @property
    def is_loss_event(self) -> bool:
        return self.outcome_type in (
            "storm", "contact_lost", "gravity", "vanished",
            "pirates_loss", "pirates_win"
        )

    def classify_outcome(self) -> None:
        base = self.outcome_raw

        if base == "vanished":   self.outcome_type = "vanished";     return
        if base == "failed":     self.outcome_type = "failed";       return
        if base in ("storm", "contact_lost", "gravity"):
            self.outcome_type = base;                                return

        if base == "success":
            if self.smuggler_code:
                self.outcome_type = "smuggler_code";                 return

            has_res   = self.total_resources > 0
            has_dm    = self.dark_matter > 0
            has_ships = bool(self.ships_delta)

            if self.pirate_strength:
                if self.loss_percent is not None and self.loss_percent > 40:
                    self.outcome_type = "pirates_loss"
                else:
                    self.outcome_type = "pirates_win"
                return

            if   has_res and has_ships and has_dm: self.outcome_type = "success_full"
            elif has_res and has_dm:               self.outcome_type = "success_mix_dm"
            elif has_res and has_ships:            self.outcome_type = "success_mix"
            elif has_res:                          self.outcome_type = "success_res"
            elif has_dm:                           self.outcome_type = "success_dm"
            elif has_ships:                        self.outcome_type = "success_ships"
            else:                                  self.outcome_type = "failed"


# ---------------------------------------------------------------------------
# Block splitter
# ---------------------------------------------------------------------------
def _split_blocks(text: str) -> list[str]:
    positions = [m.start() for m in _BLOCK_HEADER.finditer(text)]
    if not positions:
        return []
    blocks = []
    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        blocks.append(text[pos:end].strip())
    return blocks


# ---------------------------------------------------------------------------
# Single block parser
# ---------------------------------------------------------------------------
def _parse_block(block: str) -> ParsedExpedition:
    result = ParsedExpedition(raw_text=block)

    lines = [l.strip() for l in block.splitlines()]
    if not lines:
        result.parse_error = "empty block"
        return result

    # Timestamp
    ts_match = _RE_TIMESTAMP.search(lines[0])
    if ts_match:
        result.returned_at = _parse_timestamp(ts_match.group(1), ts_match.group(2))

    # Expedition number
    for line in lines[:8]:
        m = _RE_EXP_NUMBER.search(line)
        if m:
            result.exp_number = int(m.group(1))
            break

    # Outcome headline — scan all lines, longest match wins
    best_match_len = 0
    for line in lines:
        for keyword, outcome in OUTCOME_HEADLINES.items():
            if keyword in line and len(keyword) > best_match_len:
                result.outcome_raw = outcome
                best_match_len = len(keyword)

    if not result.outcome_raw:
        result.outcome_raw = "failed"

    # Loss percent
    for line in lines:
        m = _RE_LOSS_PERCENT.search(line)
        if m:
            result.loss_percent = float(m.group(1))
            break

    # Pirate data
    for line in lines:
        m = _RE_PIRATE_STRENGTH.search(line)
        if m:
            result.pirate_strength = _parse_num(m.group(1))
        m2 = _RE_PIRATE_WIN_CHANCE.search(line)
        if m2:
            result.pirate_win_chance = int(m2.group(1))
        m3 = _RE_PIRATE_LOSS_RATE.search(line)
        if m3:
            result.pirate_loss_rate = int(m3.group(1))

    # DM bonus
    for line in lines:
        m = _RE_DM_BONUS.search(line)
        if m:
            result.dark_matter_bonus     = _parse_num(m.group(1))
            result.dark_matter_bonus_pct = int(m.group(2))
            break

    # Resources and ships
    # Expand tab-separated pairs first
    expanded: list[str] = []
    for line in lines:
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
            expanded.extend(p for p in parts if p)
        else:
            expanded.append(line)

    i = 0
    while i < len(expanded):
        line = expanded[i]

        # Resource?
        if line in RESOURCE_LABELS:
            key = RESOURCE_LABELS[line]
            if i + 1 < len(expanded):
                val = _parse_num(expanded[i + 1])
                if val != 0:
                    if   key == "metal":       result.metal       = val
                    elif key == "crystal":     result.crystal     = val
                    elif key == "deuterium":   result.deuterium   = val
                    elif key == "dark_matter": result.dark_matter = val
                    i += 2
                    continue

        # Ship?
        if line in SHIP_NAMES:
            canonical = SHIP_NAME_MAP[line]
            if i + 1 < len(expanded):
                qty_line = expanded[i + 1].strip()
                if re.match(r"^[+-][\d.,\s]+$", qty_line):
                    val = _parse_num(qty_line)
                    result.ships_delta[canonical] = result.ships_delta.get(canonical, 0) + val
                    i += 2
                    continue

        i += 1

    # Smuggler codes
    for line in lines:
        m = _RE_SMUGGLER_CODE.search(line)
        if m:
            result.smuggler_code = m.group(1)
        m2 = _RE_SMUGGLER_TIER.search(line)
        if m2:
            result.smuggler_tier = int(m2.group(1))

    result.classify_outcome()
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def parse_expedition_text(raw: str) -> list[ParsedExpedition]:
    """
    Parse a full copy-pasted expedition message dump (DE, EN or FR).
    Returns a list of ParsedExpedition objects (one per message block).
    """
    blocks = _split_blocks(raw)
    results = []
    for block in blocks:
        try:
            parsed = _parse_block(block)
            results.append(parsed)
        except Exception as e:
            err = ParsedExpedition(raw_text=block, parse_error=str(e))
            results.append(err)
    return results
