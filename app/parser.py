# app/parser.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ----------------------------
# Data model
# ----------------------------

@dataclass
class ParsedExpedition:
    raw_text: str
    date_ddmm: Optional[str] = None
    time_hhmmss: Optional[str] = None
    expedition_no: Optional[int] = None

    # high-level outcome
    outcome_raw: Optional[str] = None
    outcome_type: Optional[str] = None  # success / pirates_win / pirates_loss / storm / gravity / contact_lost / vanished / failed / unknown

    # results
    resources: Dict[str, int] = None
    ships: Dict[str, int] = None

    # extras
    pirate_strength: Optional[int] = None
    pirate_win_chance_pct: Optional[float] = None
    loss_rate_pct: Optional[int] = None
    loss_percent_of_fleet: Optional[int] = None

    dark_horizon_bonus: Optional[int] = None
    dark_horizon_bonus_pct: Optional[int] = None

    smuggler_code: Optional[str] = None

    # errors
    parse_error: Optional[str] = None

    def __post_init__(self):
        if self.resources is None:
            self.resources = {}
        if self.ships is None:
            self.ships = {}


# ----------------------------
# Regex helpers
# ----------------------------

_BLOCK_HEADER = re.compile(
    r"^(\d{2}\.\d{2}(?:\.\d{2,4})?)\s+(\d{2}:\d{2}:\d{2})\s+.*?(?:Expeditionsbericht|Expedition Report|Rapport d[’']expédition)\s*$",
    re.I,
)

_RE_EXP_NUMBER = re.compile(r"EXP(?:E|É)DITION\s*#(\d+)", re.I)

_RE_SIGNED_INT = re.compile(r"^([+-])\s*([\d\.\s,]+)\s*$")
_RE_INT = re.compile(r"^[\d\.\s,]+$")

_RE_LOSS_PERCENT = re.compile(r"(?:Verluste|Pertes)\s*:\s*(\d+)\s*%", re.I)

_RE_PIRATE_STRENGTH = re.compile(r"(?:Feindsignaturen|Enemy signatures|Puissance ennemie)\s*:\s*([\d.,]+)", re.I)
_RE_PIRATE_WIN_CHANCE = re.compile(r"(?:Geschätzter Sieg|Estimated win|Victoire estimée)\s*:\s*~?([\d.]+)\s*%", re.I)
_RE_PIRATE_LOSS_RATE = re.compile(r"(?:Verlustrate|Loss rate|Taux de pertes)\s*:\s*(\d+)\s*%", re.I)

_RE_SCHWARZER_HORIZONT = re.compile(
    r"(?:Schwarzer Horizont|Horizon Noir|Black Horizon)\s*:\s*\+?([\d.,]+)\s*\(\+(\d+)\s*%\)",
    re.I,
)

_RE_SMUGGLER = re.compile(r"(?:Schmugglercode|Smuggler code|Code contrebandier)\s*:\s*([A-Z0-9\-]{6,})", re.I)


OUTCOME_HEADLINES = {
    # German
    "Expedition erfolgreich": "success",
    "Expedition gescheitert": "failed",
    "Ionensturm": "storm",
    "Kontakt verloren": "contact_lost",
    "Gravitationsanomalie": "gravity",
    "Flotte verschollen": "vanished",
    "Schmugglercode": "smuggler_code",

    # English
    "expedition successful": "success",
    "expedition failed": "failed",
    "ion storm": "storm",
    "contact lost": "contact_lost",
    "gravity anomaly": "gravity",
    "fleet vanished": "vanished",
    "smuggler code": "smuggler_code",

    # French
    "expédition réussie": "success",
    "expedition reussie": "success",
    "expédition compromise": "pirates_loss",
    "expedition compromise": "pirates_loss",
    "tempête ionique": "storm",
    "tempete ionique": "storm",
    "contact perdu": "contact_lost",
    "anomalie gravitationnelle": "gravity",
    "flotte disparue": "vanished",
    "code contrebandier": "smuggler_code",
}

RESOURCE_LABELS = {
    # German (existing)
    "metall": "metal",
    "kristall": "crystal",
    "deuterium": "deut",
    "dunkle materie": "dm",

    # English
    "metal": "metal",
    "crystal": "crystal",
    "dark matter": "dm",

    # French
    "métal": "metal",
    "metal": "metal",
    "cristal": "crystal",
    "deutérium": "deut",
    "deuterium": "deut",
    "matière noire": "dm",
    "matiere noire": "dm",
}

SHIP_NAMES = {
    # German (existing)
    "Kleiner Transporter",
    "Großer Transporter",
    "Leichter Jäger",
    "Schwerer Jäger",
    "Kreuzer",
    "Schlachtschiff",
    "Traqueur",  # sometimes already present in mixed servers
    "Recycler",
    "Spionagesonde",
    "Bomber",
    "Zerstörer",
    "Todesstern",
    "Pathfinder",

    # English
    "Small Cargo",
    "Large Cargo",
    "Light Fighter",
    "Heavy Fighter",
    "Cruiser",
    "Battleship",
    "Battlecruiser",
    "Recycler",
    "Espionage Probe",
    "Bomber",
    "Destroyer",
    "Deathstar",
    "Pathfinder",

    # French
    "Petit Transporteur",
    "Grand Transporteur",
    "Chasseur Léger",
    "Chasseur Lourd",
    "Croiseur",
    "Vaisseau de Bataille",
    "Traqueur",
    "Recycleur",
    "Sonde Espionnage",
    "Sonde d'Espionnage",
    "Bombardier",
    "Destructeur",
    "Étoile de la Mort",
    "Etoile de la Mort",
    "Éclaireur",
    "Eclaireur",
}


def _to_int(num: str) -> int:
    # accept: 1.234.567 / 1 234 567 / 1,234,567
    s = (num or "").strip()
    s = s.replace(" ", "").replace(".", "").replace(",", "")
    return int(s) if s else 0


def _parse_signed_int(line: str) -> Optional[int]:
    m = _RE_SIGNED_INT.match(line.strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    return sign * _to_int(m.group(2))


def _split_blocks(raw: str) -> List[str]:
    lines = [l.rstrip() for l in (raw or "").splitlines()]

    # find start lines that match a header; each is a message block start
    starts = []
    for idx, line in enumerate(lines):
        if _BLOCK_HEADER.match(line.strip()):
            starts.append(idx)

    if not starts:
        # fallback: treat everything as one block
        return [raw.strip()] if raw.strip() else []

    blocks = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(lines)
        block_lines = [l for l in lines[s:e] if l.strip() != ""]
        if block_lines:
            blocks.append("\n".join(block_lines).strip())
    return blocks


def _parse_block(block: str) -> ParsedExpedition:
    lines = [l.strip() for l in (block or "").splitlines() if l.strip()]
    exp = ParsedExpedition(raw_text=block)

    if not lines:
        exp.parse_error = "empty_block"
        return exp

    # header
    mh = _BLOCK_HEADER.match(lines[0])
    if mh:
        exp.date_ddmm = mh.group(1)
        exp.time_hhmmss = mh.group(2)

    outcome_raw = None
    for line in lines[:6]:
        line_l = line.lower()
        for keyword, outcome in OUTCOME_HEADLINES.items():
            if keyword.lower() in line_l:
                outcome_raw = outcome
                break
        if not outcome_raw and re.search(r"\bpirat", line, re.I):
            # Some servers don't prefix pirate encounters with a standard headline
            outcome_raw = "pirates"
        if outcome_raw:
            break
    exp.outcome_raw = outcome_raw

    # expedition number
    for line in lines[:8]:
        m = _RE_EXP_NUMBER.search(line)
        if m:
            exp.expedition_no = int(m.group(1))
            break

    # parse tabular sections (resources / ships)
    i = 0
    while i < len(lines):
        line = lines[i]

        # resource line: "Métal\t+123" or "Metall\t+123" or a two-line format
        for label, key in RESOURCE_LABELS.items():
            ll = line.lower()
            if ll == label:
                if i + 1 < len(lines):
                    v = _parse_signed_int(lines[i + 1])
                    if v is not None:
                        exp.resources[key] = exp.resources.get(key, 0) + v
                        i += 2
                        break
            if ll.startswith(label + "\t") or ll.startswith(label + " "):
                tail = line[len(label):].strip()
                # strip leading ":" if present
                if tail.startswith(":"):
                    tail = tail[1:].strip()
                v = _parse_signed_int(tail)
                if v is not None:
                    exp.resources[key] = exp.resources.get(key, 0) + v
                    i += 1
                    break
        else:
            # no break from resource parsing
            pass
        # if we consumed inside loop, continue
        if i > 0 and i <= len(lines) and (i == len(lines) or lines[i - 1] != line):
            continue

        # ships: can be "Grand Transporteur\t-3.218.629" or two-line
        matched_ship = False
        for ship_name in SHIP_NAMES:
            if line == ship_name:
                if i + 1 < len(lines):
                    v = _parse_signed_int(lines[i + 1])
                    if v is not None:
                        exp.ships[ship_name] = exp.ships.get(ship_name, 0) + v
                        i += 2
                        matched_ship = True
                        break
            if line.startswith(ship_name + "\t") or line.startswith(ship_name + " "):
                tail = line[len(ship_name):].strip()
                v = _parse_signed_int(tail)
                if v is not None:
                    exp.ships[ship_name] = exp.ships.get(ship_name, 0) + v
                    i += 1
                    matched_ship = True
                    break
        if matched_ship:
            continue

        # pirates meta
        m = _RE_PIRATE_STRENGTH.search(line)
        if m:
            exp.pirate_strength = _to_int(m.group(1))
        m = _RE_PIRATE_WIN_CHANCE.search(line)
        if m:
            exp.pirate_win_chance_pct = float(m.group(1))
        m = _RE_PIRATE_LOSS_RATE.search(line)
        if m:
            exp.loss_rate_pct = int(m.group(1))

        m = _RE_LOSS_PERCENT.search(line)
        if m:
            exp.loss_percent_of_fleet = int(m.group(1))

        m = _RE_SCHWARZER_HORIZONT.search(line)
        if m:
            exp.dark_horizon_bonus = _to_int(m.group(1))
            exp.dark_horizon_bonus_pct = int(m.group(2))

        m = _RE_SMUGGLER.search(line)
        if m:
            exp.smuggler_code = m.group(1).strip()

        i += 1

    # outcome type classification
    exp.outcome_type = classify_outcome(exp)
    return exp


def classify_outcome(exp: ParsedExpedition) -> str:
    raw = (exp.outcome_raw or "").lower()

    if raw == "success":
        return "success"
    if raw == "failed":
        return "failed"
    if raw == "storm":
        return "storm"
    if raw == "gravity":
        return "gravity"
    if raw == "contact_lost":
        return "contact_lost"
    if raw == "vanished":
        return "vanished"
    if raw == "smuggler_code":
        return "smuggler_code"

    # pirates: decide win vs loss using losses / negative ship deltas
    if "pirates" in raw or exp.pirate_strength is not None:
        lost_any = any(v < 0 for v in exp.ships.values())
        # if loss rate high and no gain, treat as loss
        if exp.loss_rate_pct is not None and exp.loss_rate_pct >= 40:
            return "pirates_loss"
        if lost_any:
            return "pirates_loss"
        return "pirates_win"

    # fallback by signals
    if exp.resources or exp.ships:
        return "success"

    return "unknown"


def parse_expedition_text(raw: str) -> List[ParsedExpedition]:
    """
    Parse a multi-message expedition report dump.
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