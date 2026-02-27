# app/parser.py
"""
OGX Expedition Message Parser — DE / EN / FR

Supports all three OGame server languages.
Handles both tab-separated and space-separated copy-paste formats.
Block splitting works via timestamp+header OR via EXPEDITION # as fallback.
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
OUTCOME_HEADLINES = {
    # German
    "Expedition erfolgreich":           "success",
    "Expedition gescheitert":           "failed",
    "Verschwinden der Flotte":          "vanished",
    "Ionensturm":                       "storm",
    "Kontakt verloren":                 "contact_lost",
    "Gravitationsanomalie":             "gravity",
    "Expeditionsbericht: Erfolgreich":  "success",
    "Keine Funde":                      "failed",
    # English
    "Expedition successful":            "success",
    "Expedition failed":                "failed",
    "Fleet disappeared":                "vanished",
    "Ion Storm":                        "storm",
    "Lost contact":                     "contact_lost",
    "Gravity anomaly":                  "gravity",
    "No finds":                         "failed",
    # French
    "Expédition réussie":               "success",
    "Expédition compromise":            "success",   # pirate loss — refined via pirate_strength
    "Expédition échouée":               "failed",
    "Disparition de la flotte":         "vanished",
    "Tempête ionique":                  "storm",
    "Contact perdu":                    "contact_lost",
    "Anomalie gravitationnelle":        "gravity",
    "Aucune découverte":                "failed",
    # Shared
    "Piraten":                          "success",
    "Pirates":                          "success",
}

RESOURCE_LABELS = {
    # German
    "Metall":           "metal",
    "Kristall":         "crystal",
    "Deuterium":        "deuterium",
    "Dunkle Materie":   "dark_matter",
    # English
    "Metal":            "metal",
    "Crystal":          "crystal",
    "Dark Matter":      "dark_matter",
    # French
    "Métal":            "metal",
    "Cristal":          "crystal",
    "Deutérium":        "deuterium",
    "Matière noire":    "dark_matter",
}

# All ship names across all languages → canonical German name
SHIP_NAME_MAP = {
    # German (canonical)
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
    # English
    "Small Cargo":          "Kleiner Transporter",
    "Large Cargo":          "Großer Transporter",
    "Light Fighter":        "Leichter Jäger",
    "Heavy Fighter":        "Schwerer Jäger",
    "Cruiser":              "Kreuzer",
    "Battleship":           "Schlachtschiff",
    "Battlecruiser":        "Schlachtkreuzer",
    "Destroyer":            "Zerstörer",
    "Deathstar":            "Todesstern",
    "Espionage Probe":      "Spionagesonde",
    "Solar Satellite":      "Solarsatellit",
    "Pathfinder":           "Pathfinder",
    # French
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
    "Faucheur":             "Reaper",
    "Eclaireur":            "Pathfinder",
    "Éclaireur":            "Pathfinder",
}

SHIP_NAMES = set(SHIP_NAME_MAP.keys())

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
_RE_TIMESTAMP = re.compile(
    r"(\d{2}\.\d{2}(?:\.\d{2,4})?)\s+(\d{2}:\d{2}:\d{2})"
)
_RE_EXP_NUMBER = re.compile(r"EXP[EÉ]DITION\s*#(\d+)", re.IGNORECASE)

_RE_LOSS_PERCENT = re.compile(
    r"(?:Verluste|Losses?|Pertes)\s*[:\s]\s*(\d+)\s*%",
    re.IGNORECASE
)
_RE_PIRATE_STRENGTH = re.compile(
    r"(?:Feindsignaturen|Enemy signatures?|Signatures ennemies)\s*[:\s]\s*([\d.,\s]+)",
    re.IGNORECASE
)
_RE_PIRATE_WIN_CHANCE = re.compile(
    r"(?:Gesch[äa]tzter Sieg|Estimated victory|Victoire estim[ée]e?)\s*[:\s]\s*~?(\d+)\s*%",
    re.IGNORECASE
)
_RE_PIRATE_LOSS_RATE = re.compile(
    r"(?:Verlustrate|Loss rate|Taux de pertes)\s*[:\s]\s*(\d+)\s*%",
    re.IGNORECASE
)
_RE_DM_BONUS = re.compile(
    r"(?:Schwarzer Horizont|Black Horizon|Horizon Noir)\s*[:\s]\s*\+?([\d.,\s]+)\s*\(\+(\d+)%\)",
    re.IGNORECASE
)
_RE_SMUGGLER_CODE = re.compile(r"\b(\d{4}-\d{4}-\d{4})\b")
_RE_SMUGGLER_TIER = re.compile(r"(?:Stufe|Level|Niveau)\s*(\d+)", re.IGNORECASE)

# Block header — all three languages (tabs or multiple spaces between parts)
_BLOCK_HEADER = re.compile(
    r"\d{2}\.\d{2}(?:\.\d{2,4})?\s+\d{2}:\d{2}:\d{2}"
    r"[\t ]+"
    r"(?:"
    r"Flottenkommando[\t ]+Expeditionsbericht"
    r"|Fleet Command[\t ]+Expedition Report"
    r"|Commandement de la flotte[\t ]+Rapport d[''\u2019]exp[ée]dition"
    r")"
)

# Fallback splitter: EXPEDITION # lines that look like a block start
# Used when header-based splitting finds nothing
_BLOCK_FALLBACK = re.compile(
    r"(?:^|\n)[ \t]*EXP[EÉ]DITION\s*#\d+",
    re.IGNORECASE
)


def _parse_num(s: str) -> int:
    s = s.strip().lstrip("+")
    s = re.sub(r"[\s\xa0]", "", s)
    s = s.replace(".", "").replace(",", "")
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
    exp_number:             Optional[int]      = None
    returned_at:            Optional[datetime] = None
    outcome_raw:            str                = ""
    outcome_type:           str                = "failed"

    metal:                  int                = 0
    crystal:                int                = 0
    deuterium:              int                = 0
    dark_matter:            int                = 0
    dark_matter_bonus:      int                = 0
    dark_matter_bonus_pct:  int                = 0

    ships_delta: dict = field(default_factory=dict)

    loss_percent:       Optional[float] = None
    pirate_strength:    Optional[int]   = None
    pirate_win_chance:  Optional[int]   = None
    pirate_loss_rate:   Optional[int]   = None

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

        if base == "vanished":  self.outcome_type = "vanished"; return
        if base == "failed":    self.outcome_type = "failed";   return
        if base in ("storm", "contact_lost", "gravity"):
            self.outcome_type = base; return

        if base == "success":
            if self.smuggler_code:
                self.outcome_type = "smuggler_code"; return

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
    """Split by timestamp+header. Falls back to EXPEDITION # if no headers found."""
    positions = [m.start() for m in _BLOCK_HEADER.finditer(text)]

    if not positions:
        # Fallback: split on lines containing EXPEDITION #NNN
        # Walk backwards to include any preceding timestamp/outcome line
        fb_positions = []
        for m in _BLOCK_FALLBACK.finditer(text):
            # Try to grab up to 3 lines before the EXPEDITION # line
            start = max(0, text.rfind("\n", 0, m.start()))
            # Look further back for a timestamp line
            chunk_before = text[max(0, start - 200):start]
            ts_m = list(_RE_TIMESTAMP.finditer(chunk_before))
            if ts_m:
                ts_start = start - 200 + ts_m[-1].start()
                fb_positions.append(max(0, ts_start))
            else:
                fb_positions.append(max(0, m.start()))
        positions = sorted(set(fb_positions))

    if not positions:
        return []

    blocks = []
    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        blocks.append(text[pos:end].strip())
    return blocks


# ---------------------------------------------------------------------------
# Line expander — handles BOTH tab-separated and space-separated columns
# ---------------------------------------------------------------------------
def _expand_lines(lines: list[str]) -> list[str]:
    """
    Convert each line into a flat list of tokens.
    Handles:
      - Tab-separated:   "Metall\t+1.200.000"  →  ["Metall", "+1.200.000"]
      - Space-separated: "Grand Transporteur        -3.218.629"  →  ["Grand Transporteur", "-3.218.629"]
    """
    expanded: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t") if p.strip()]
            expanded.extend(parts)
        else:
            # Try splitting on 2+ spaces (space-padded HTML table copy)
            parts = [p.strip() for p in re.split(r"  +", line) if p.strip()]
            if len(parts) >= 2 and re.match(r"^[+-][\d.,\s]+$", parts[-1]):
                # Last token looks like a number → label + value
                expanded.extend(parts)
            else:
                expanded.append(line)
    return expanded


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

    # Expedition number — scan first 10 lines
    for line in lines[:10]:
        m = _RE_EXP_NUMBER.search(line)
        if m:
            result.exp_number = int(m.group(1))
            break

    # Outcome headline — longest match wins
    best_len = 0
    for line in lines:
        for keyword, outcome in OUTCOME_HEADLINES.items():
            if keyword in line and len(keyword) > best_len:
                result.outcome_raw = outcome
                best_len = len(keyword)
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

    # Resources and ships — expand tabs AND spaces
    expanded = _expand_lines(lines)

    i = 0
    while i < len(expanded):
        token = expanded[i]

        if token in RESOURCE_LABELS:
            key = RESOURCE_LABELS[token]
            if i + 1 < len(expanded):
                val = _parse_num(expanded[i + 1])
                if val != 0:
                    if   key == "metal":       result.metal       = val
                    elif key == "crystal":     result.crystal     = val
                    elif key == "deuterium":   result.deuterium   = val
                    elif key == "dark_matter": result.dark_matter = val
                    i += 2
                    continue

        if token in SHIP_NAMES:
            canonical = SHIP_NAME_MAP[token]
            if i + 1 < len(expanded):
                qty = expanded[i + 1].strip()
                if re.match(r"^[+-][\d.,\s]+$", qty):
                    val = _parse_num(qty)
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
    Handles tab-separated and space-separated column formats.
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
