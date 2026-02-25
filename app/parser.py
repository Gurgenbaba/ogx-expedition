# app/parser.py
"""
OGX Expedition Message Parser (German)

Parses the raw copy-pasted text from the OGame message inbox.
Each expedition message block is separated by the pattern:
  "DD.MM HH:MM:SS\tFlottenkommando\tExpeditionsbericht"

Handles all outcome types:
  - Erfolgreich (Ressourcen, Schiffe, DM, Mix)
  - Ionensturm / Kontakt verloren / Gravitationsanomalie (partial loss)
  - Verschwinden der Flotte (total loss)
  - Expedition gescheitert (nothing)
  - Piratenkampf (won or lost)
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# German keyword maps
# ---------------------------------------------------------------------------
OUTCOME_HEADLINES = {
    "Expedition erfolgreich": "success",
    "Expedition gescheitert": "failed",
    "Verschwinden der Flotte": "vanished",
    "Ionensturm": "storm",
    "Kontakt verloren": "contact_lost",
    "Gravitationsanomalie": "gravity",
}

RESOURCE_LABELS = {
    "Metall": "metal",
    "Kristall": "crystal",
    "Deuterium": "deuterium",
    "Dunkle Materie": "dark_matter",
}

# All known ship names in German OGame
SHIP_NAMES = {
    "Kleiner Transporter",
    "Großer Transporter",
    "Leichter Jäger",
    "Schwerer Jäger",
    "Kreuzer",
    "Schlachtschiff",
    "Schlachtkreuzer",
    "Bomber",
    "Zerstörer",
    "Todesstern",
    "Recycler",
    "Spionagesonde",
    "Solarsatellit",
    "Crawler",
    "Reaper",
    "Pathfinder",
}

# Regex patterns
_RE_TIMESTAMP = re.compile(
    r"(\d{2}\.\d{2}(?:\.\d{2,4})?)\s+(\d{2}:\d{2}:\d{2})"
)
_RE_EXP_NUMBER = re.compile(r"EXPEDITION\s*#(\d+)", re.IGNORECASE)
_RE_RESOURCE_LINE = re.compile(r"^([+-][\d.,]+)$")
_RE_SHIP_QTY = re.compile(r"^([+-][\d.,]+)$")
_RE_LOSS_PERCENT = re.compile(r"Verluste:\s*(\d+)\s*%")
_RE_PIRATE_STRENGTH = re.compile(r"Feindsignaturen:\s*([\d.,]+)")
_RE_PIRATE_WIN_CHANCE = re.compile(r"Geschätzter Sieg:\s*~(\d+)\s*%")
_RE_PIRATE_LOSS_RATE = re.compile(r"Verlustrate:\s*(\d+)\s*%")
_RE_SCHWARZER_HORIZONT = re.compile(
    r"Schwarzer Horizont:\s*\+?([\d.,]+)\s*\(\+(\d+)%\)"
)


def _parse_num(s: str) -> int:
    """Parse '1.200.800' or '+179.941.271.650' or '-4.202.800' to int."""
    s = s.strip().lstrip("+")
    s = s.replace(".", "").replace(",", "").replace("\xa0", "")
    try:
        return int(s)
    except ValueError:
        return 0


def _parse_timestamp(date_str: str, time_str: str) -> Optional[datetime]:
    """Parse '25.02' or '25.02.26' + '02:14:33' to datetime."""
    try:
        parts = date_str.split(".")
        if len(parts) == 2:
            day, month = int(parts[0]), int(parts[1])
            year = datetime.utcnow().year
        elif len(parts) == 3:
            day, month = int(parts[0]), int(parts[1])
            y = int(parts[2])
            year = 2000 + y if y < 100 else y
        else:
            return None
        h, m, s = (int(x) for x in time_str.split(":"))
        return datetime(year, month, day, h, m, s)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Parsed result dataclass
# ---------------------------------------------------------------------------
@dataclass
class ParsedExpedition:
    exp_number: Optional[int] = None
    returned_at: Optional[datetime] = None
    outcome_raw: str = ""          # raw headline keyword
    outcome_type: str = "failed"   # normalised type

    metal: int = 0
    crystal: int = 0
    deuterium: int = 0
    dark_matter: int = 0
    dark_matter_bonus: int = 0
    dark_matter_bonus_pct: int = 0

    ships_delta: dict = field(default_factory=dict)  # ship_name → int (+/-)

    loss_percent: Optional[float] = None
    pirate_strength: Optional[int] = None
    pirate_win_chance: Optional[int] = None
    pirate_loss_rate: Optional[int] = None

    raw_text: str = ""
    parse_error: Optional[str] = None

    @property
    def dedup_key(self) -> str:
        if self.exp_number:
            return hashlib.sha256(f"exp#{self.exp_number}".encode()).hexdigest()[:32]
        # fallback: hash of timestamp + outcome
        s = f"{self.returned_at}|{self.outcome_type}|{self.metal}|{self.crystal}"
        return hashlib.sha256(s.encode()).hexdigest()[:32]

    @property
    def total_resources(self) -> int:
        return self.metal + self.crystal + self.deuterium

    @property
    def is_loss_event(self) -> bool:
        return self.outcome_type in ("storm", "contact_lost", "gravity", "vanished",
                                     "pirates_loss", "pirates_win")

    def classify_outcome(self) -> None:
        """Determine precise outcome_type from collected data."""
        base = self.outcome_raw

        if base == "vanished":
            self.outcome_type = "vanished"
            return
        if base == "failed":
            self.outcome_type = "failed"
            return
        if base in ("storm", "contact_lost", "gravity"):
            self.outcome_type = base
            return

        if base == "success":
            has_res = self.total_resources > 0
            has_dm = self.dark_matter > 0
            has_ships = bool(self.ships_delta)

            # Check for pirate combat markers
            if self.pirate_strength:
                if self.loss_percent is not None and self.loss_percent > 40:
                    self.outcome_type = "pirates_loss"
                else:
                    self.outcome_type = "pirates_win"
                return

            if has_res and has_ships and has_dm:
                self.outcome_type = "success_full"
            elif has_res and has_dm:
                self.outcome_type = "success_mix_dm"
            elif has_res and has_ships:
                self.outcome_type = "success_mix"
            elif has_res:
                self.outcome_type = "success_res"
            elif has_dm:
                self.outcome_type = "success_dm"
            elif has_ships:
                self.outcome_type = "success_ships"
            else:
                self.outcome_type = "failed"


# ---------------------------------------------------------------------------
# Block splitter
# ---------------------------------------------------------------------------
_BLOCK_HEADER = re.compile(
    r"\d{2}\.\d{2}(?:\.\d{2,4})?\s+\d{2}:\d{2}:\d{2}\s+Flottenkommando\s+Expeditionsbericht"
)


def _split_blocks(text: str) -> list[str]:
    """Split raw pasted text into individual expedition message blocks."""
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

    # --- Timestamp from first line ---
    ts_match = _RE_TIMESTAMP.search(lines[0])
    if ts_match:
        result.returned_at = _parse_timestamp(ts_match.group(1), ts_match.group(2))

    # --- Expedition number ---
    for line in lines[:6]:
        m = _RE_EXP_NUMBER.search(line)
        if m:
            result.exp_number = int(m.group(1))
            break

    # --- Outcome headline ---
    # The headline is the line that contains one of the known keywords,
    # typically right after the header line and "EXPEDITION #XXXXX"
    for line in lines:
        for keyword, outcome in OUTCOME_HEADLINES.items():
            if keyword in line:
                result.outcome_raw = outcome
                break
        if result.outcome_raw:
            break

    if not result.outcome_raw:
        result.outcome_raw = "failed"

    # --- Loss percent (storm / contact / gravity) ---
    for line in lines:
        m = _RE_LOSS_PERCENT.search(line)
        if m:
            result.loss_percent = float(m.group(1))
            break

    # --- Pirate data ---
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

    # --- Schwarzer Horizont bonus ---
    for line in lines:
        m = _RE_SCHWARZER_HORIZONT.search(line)
        if m:
            result.dark_matter_bonus = _parse_num(m.group(1))
            result.dark_matter_bonus_pct = int(m.group(2))
            break

    # --- Resources and ships ---
    # The copy-paste from OGame can arrive in two formats:
    # Format A (tab-separated on one line):  "Metall\t+179.941.271.650"
    # Format B (label on one line, qty next): "Metall\n+179.941.271.650"
    # We handle both by first expanding tab-pairs, then scanning.

    # Expand tab-pairs into individual label/value items
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

        # Resource label?
        if line in RESOURCE_LABELS:
            key = RESOURCE_LABELS[line]
            if i + 1 < len(expanded):
                qty_line = expanded[i + 1].strip()
                val = _parse_num(qty_line)
                if val != 0:
                    if key == "metal":
                        result.metal = val
                    elif key == "crystal":
                        result.crystal = val
                    elif key == "deuterium":
                        result.deuterium = val
                    elif key == "dark_matter":
                        result.dark_matter = val
                    i += 2
                    continue

        # Ship name?
        if line in SHIP_NAMES:
            if i + 1 < len(expanded):
                qty_line = expanded[i + 1].strip()
                qty_match = re.match(r"^([+-][\d.,\s]+)$", qty_line)
                if qty_match:
                    val = _parse_num(qty_line)
                    result.ships_delta[line] = result.ships_delta.get(line, 0) + val
                    i += 2
                    continue

        i += 1

    # --- Classify ---
    result.classify_outcome()
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def parse_expedition_text(raw: str) -> list[ParsedExpedition]:
    """
    Parse a full copy-pasted expedition message dump.
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
