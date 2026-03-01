# app/parser.py
"""
OGX Expedition Message Parser — Multilingual (DE / EN / FR)

Parses raw copy-pasted text from the OGame message inbox.
Each expedition message block is separated by a header line:
  "DD.MM HH:MM:SS\t<Fleet Command>\t<Expedition Report>"

Supported languages:
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
# Keyword maps — all three languages
# ---------------------------------------------------------------------------

OUTCOME_HEADLINES = {
    # DE
    "Expedition erfolgreich":        "success",
    "Expedition gescheitert":        "failed",
    "Verschwinden der Flotte":       "vanished",
    "Ionensturm":                    "storm",
    "Kontakt verloren":              "contact_lost",
    "Gravitationsanomalie":          "gravity",
    "Piraten":                       "success",   # pirate — sub-classified below
    # DE alternates
    "Expeditionsbericht: Erfolgreich": "success",
    "Keine Funde":                   "failed",

    # EN
    "Expedition Successful":         "success",
    "Expedition Compromised":        "failed",
    "Fleet Disappearance":           "vanished",
    "Ion Storm":                     "storm",
    "Contact Lost":                  "contact_lost",
    "Gravitational Anomaly":         "gravity",
    "Pirates":                       "success",

    # FR
    "Expédition réussie":            "success",
    "Expédition compromise":         "failed",
    "Disparition de la flotte":      "vanished",
    "Tempête ionique":               "storm",
    "Contact perdu":                 "contact_lost",
    "Anomalie gravitationnelle":     "gravity",
    "Pirates":                       "success",
}

RESOURCE_LABELS = {
    # DE
    "Metall":         "metal",
    "Kristall":       "crystal",
    "Deuterium":      "deuterium",
    "Dunkle Materie": "dark_matter",
    # EN
    "Metal":          "metal",
    "Crystal":        "crystal",
    "Dark Matter":    "dark_matter",
    # FR
    "Métal":          "metal",
    "Cristal":        "crystal",
    "Deutérium":      "deuterium",
    "Matière noire":  "dark_matter",
}

# Canonical DE ship name → internal key (used for DB storage)
# Parser always stores canonical DE name
SHIP_NAME_MAP: dict[str, str] = {
    # DE → canonical
    "Kleiner Transporter":   "Kleiner Transporter",
    "Großer Transporter":    "Großer Transporter",
    "Leichter Jäger":        "Leichter Jäger",
    "Schwerer Jäger":        "Schwerer Jäger",
    "Kreuzer":               "Kreuzer",
    "Schlachtschiff":        "Schlachtschiff",
    "Kolonieschiff":         "Kolonieschiff",
    "Recycler":              "Recycler",
    "Spionagesonde":         "Spionagesonde",
    "Bomber":                "Bomber",
    "Solarsatellit":         "Solarsatellit",
    "Zerstörer":             "Zerstörer",
    "Todesstern":            "Todesstern",
    "Schlachtkreuzer":       "Schlachtkreuzer",
    "Pathfinder":            "Pathfinder",
    "Reaper":                "Reaper",
    "Crawler":               "Crawler",
    # EN → canonical DE
    "Small Cargo":           "Kleiner Transporter",
    "Large Cargo":           "Großer Transporter",
    "Light Fighter":         "Leichter Jäger",
    "Heavy Fighter":         "Schwerer Jäger",
    "Cruiser":               "Kreuzer",
    "Battleship":            "Schlachtschiff",
    "Colony Ship":           "Kolonieschiff",
    "Recycler":              "Recycler",
    "Espionage Probe":       "Spionagesonde",
    "Bomber":                "Bomber",
    "Solar Satellite":       "Solarsatellit",
    "Destroyer":             "Zerstörer",
    "Deathstar":             "Todesstern",
    "Battlecruiser":         "Schlachtkreuzer",
    "Pathfinder":            "Pathfinder",
    "Reaper":                "Reaper",
    "Crawler":               "Crawler",
    # FR → canonical DE
    "Petit Transporteur":          "Kleiner Transporter",
    "Grand Transporteur":          "Großer Transporter",
    "Chasseur Léger":              "Leichter Jäger",
    "Chasseur Lourd":              "Schwerer Jäger",
    "Croiseur":                    "Kreuzer",
    "Vaisseau de Bataille":        "Schlachtschiff",
    "Vaisseau de Colonisation":    "Kolonieschiff",
    "Recycleur":                   "Recycler",
    "Sonde Espionnage":            "Spionagesonde",
    "Sonde d'Espionnage":          "Spionagesonde",
    "Bombardier":                  "Bomber",
    "Satellite Solaire":           "Solarsatellit",
    "Destructeur":                 "Zerstörer",
    "Étoile de la Mort":           "Todesstern",
    "Traqueur":                    "Schlachtkreuzer",
    "Éclaireur":                   "Pathfinder",
}

SHIP_NAMES = set(SHIP_NAME_MAP.keys())

# Smuggler code pattern: XXXX-XXXX-XXXX
_RE_SMUGGLER_CODE = re.compile(r"\b(\d{4}-\d{4}-\d{4})\b")
_RE_SMUGGLER_TIER = re.compile(r"(?:Stufe|Level|Niveau)\s*(\d+)", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Block splitting — all three language headers
# ---------------------------------------------------------------------------

_BLOCK_HEADER = re.compile(
    r"\d{2}\.\d{2}(?:\.\d{2,4})?\s+\d{2}:\d{2}:\d{2}\s+"
    r"(?:"
    r"Flottenkommando\s+Expeditionsbericht"
    r"|Fleet\s+Command\s+Expedition\s+Report"
    r"|Commandement\s+de\s+la\s+flotte\s+Rapport\s+d.exp.dition"
    r")"
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
# Regex patterns
# ---------------------------------------------------------------------------

_RE_TIMESTAMP   = re.compile(r"(\d{2}\.\d{2}(?:\.\d{2,4})?)\s+(\d{2}:\d{2}:\d{2})")
_RE_EXP_NUMBER  = re.compile(r"EXP[ÉE]DITION\s*#(\d+)", re.IGNORECASE)
_RE_RESOURCE_LINE = re.compile(r"^([+-][\d.,\s]+)$")
_RE_SHIP_QTY    = re.compile(r"^([+-][\d.,\s]+)$")
_RE_LOSS_PERCENT = re.compile(
    r"(?:Verluste|Losses?|Pertes?)\s*:\s*(\d+)\s*%", re.IGNORECASE
)
_RE_PIRATE_STRENGTH = re.compile(
    r"(?:Feindsignaturen|Enemy\s+Signatures?|Signatures?\s+ennemies?)\s*:\s*([\d.,]+)",
    re.IGNORECASE,
)
_RE_PIRATE_WIN_CHANCE = re.compile(
    r"(?:Geschätzter\s+Sieg|Estimated\s+Victory|Victoire\s+estim[ée]e?)\s*:\s*~?(\d+)\s*%",
    re.IGNORECASE,
)
_RE_PIRATE_LOSS_RATE = re.compile(
    r"(?:Verlustrate|Loss\s+Rate|Taux\s+de\s+pertes?)\s*:\s*(\d+)\s*%",
    re.IGNORECASE,
)
_RE_BLACK_HORIZON = re.compile(
    r"(?:Schwarzer\s+Horizont|Black\s+Horizon|Horizon\s+Noir)\s*:\s*\+?([\d.,]+)\s*\(\+(\d+)%\)",
    re.IGNORECASE,
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
    """Parse '25.02' or '25.02.26' + '02:14:33' to datetime."""
    now = datetime.utcnow()
    parts = date_str.split(".")
    try:
        day, month = int(parts[0]), int(parts[1])
        year = int("20" + parts[2]) if len(parts) > 2 else now.year
        h, m, s = map(int, time_str.split(":"))
        dt = datetime(year, month, day, h, m, s)
        # If no year given and date is in the future, subtract one year
        if len(parts) <= 2 and dt > now:
            dt = dt.replace(year=now.year - 1)
        return dt
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ParsedExpedition:
    raw_text: str = ""
    returned_at: Optional[datetime] = None
    exp_number: Optional[int] = None
    outcome_type: str = "unknown"
    metal: int = 0
    crystal: int = 0
    deuterium: int = 0
    dark_matter: int = 0
    dark_matter_bonus: int = 0        # Schwarzer Horizont absolute amount
    ships_gained: dict = field(default_factory=dict)
    ships_lost: dict = field(default_factory=dict)
    gt_lost: int = 0
    loss_percent: int = 0
    pirate_strength: int = 0
    pirate_win_chance: int = 0
    pirate_loss_rate: int = 0
    smuggler_code: Optional[str] = None
    smuggler_tier: Optional[int] = None
    pirates_won: Optional[bool] = None
    parse_error: Optional[str] = None

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
        return self.outcome_type in ("storm", "contact_lost", "gravity")

    def classify_outcome(self) -> None:
        """Refine outcome_type based on parsed data."""
        if self.outcome_type == "success":
            has_res   = self.total_resources > 0
            has_dm    = self.dark_matter > 0
            has_ships = bool(self.ships_gained)
            has_code  = bool(self.smuggler_code)

            if has_code:
                self.outcome_type = "smuggler_code"
            elif has_res and has_ships and has_dm:
                self.outcome_type = "success_full"
            elif has_res and has_ships:
                self.outcome_type = "success_mix"
            elif has_res and has_dm:
                self.outcome_type = "success_mix_dm"
            elif has_res:
                self.outcome_type = "success_res"
            elif has_dm:
                self.outcome_type = "success_dm"
            elif has_ships:
                self.outcome_type = "success_ships"

            # Pirate sub-classification
            if self.pirate_strength > 0:
                if self.pirates_won is True:
                    self.outcome_type = "pirates_win"
                elif self.pirates_won is False:
                    self.outcome_type = "pirates_loss"

        # GT lost tracking
        self.gt_lost = self.ships_lost.get("Großer Transporter", 0)


# ---------------------------------------------------------------------------
# Single block parser
# ---------------------------------------------------------------------------

def _parse_block(block: str) -> ParsedExpedition:
    result = ParsedExpedition(raw_text=block)

    lines = [l.strip() for l in block.splitlines()]
    if not lines:
        result.parse_error = "empty block"
        return result

    # --- Timestamp ---
    ts_match = _RE_TIMESTAMP.search(lines[0])
    if ts_match:
        result.returned_at = _parse_timestamp(ts_match.group(1), ts_match.group(2))

    # --- Expedition number ---
    for line in lines[:8]:
        m = _RE_EXP_NUMBER.search(line)
        if m:
            result.exp_number = int(m.group(1))
            break

    # --- Outcome headline ---
    for line in lines[:12]:
        for headline, outcome in OUTCOME_HEADLINES.items():
            if headline.lower() in line.lower():
                result.outcome_type = outcome
                break
        if result.outcome_type != "unknown":
            break

    # --- Pirates won/lost ---
    full_text = "\n".join(lines)
    # Win indicators (DE/EN/FR)
    pirate_win_patterns = [
        "Express-Bergung", "Sieg!", "Sektor gesäubert", "Kampf gewonnen",
        "Sie sichern Schiffe", "Die Piraten brechen die Formation",
        "Capture", "Victory", "Sector secured", "Pirates routed",
        "Vous sécurisez des vaisseaux", "Sektor gesäubert",
        "Les pirates rompent la formation", "Jackpot",
    ]
    pirate_loss_patterns = [
        "Kampf verloren", "Notrückzug", "Niederlage",
        "Combat lost", "Emergency retreat", "Defeat",
        "Combat perdu", "Repli d'urgence", "Défaite",
        "Totale Störung",
    ]
    if any(p.lower() in full_text.lower() for p in pirate_win_patterns):
        result.pirates_won = True
    elif any(p.lower() in full_text.lower() for p in pirate_loss_patterns):
        result.pirates_won = False

    # --- Schwarzer Horizont / Black Horizon ---
    bh = _RE_BLACK_HORIZON.search(full_text)
    if bh:
        result.dark_matter_bonus = _parse_num(bh.group(1))

    # --- Loss percent ---
    lp = _RE_LOSS_PERCENT.search(full_text)
    if lp:
        result.loss_percent = int(lp.group(1))

    # --- Pirate stats ---
    ps = _RE_PIRATE_STRENGTH.search(full_text)
    if ps:
        result.pirate_strength = _parse_num(ps.group(1))
    pw = _RE_PIRATE_WIN_CHANCE.search(full_text)
    if pw:
        result.pirate_win_chance = int(pw.group(1))
    plr = _RE_PIRATE_LOSS_RATE.search(full_text)
    if plr:
        result.pirate_loss_rate = int(plr.group(1))

    # --- Smuggler code ---
    sc = _RE_SMUGGLER_CODE.search(full_text)
    if sc:
        result.smuggler_code = sc.group(1)
        st = _RE_SMUGGLER_TIER.search(full_text)
        if st:
            result.smuggler_tier = int(st.group(1))

    # --- Resource & ship parsing (line-by-line state machine) ---
    MODE_NONE     = 0
    MODE_RES      = 1
    MODE_GAINED   = 2
    MODE_LOST     = 3

    mode = MODE_NONE
    pending_label: Optional[str] = None

    # Section header detection (all languages)
    _SECTION_RES = {
        "ressource", "resource",
        # "menge"/"quantity"/"quantité" excluded — they only appear as column headers (right-of-tab)
    }
    _SECTION_RECOVERED = {
        "geborgene schiffe", "geborgene einheiten", "recovered ships",
        "erbeutete schiffe", "captured ships", "vaisseaux récupérés",
        "vaisseaux capturés",
    }
    _SECTION_LOST = {
        "bestätigte verluste", "confirmed losses", "pertes confirmées",
    }

    for line in lines[1:]:
        ll = line.lower()

        # Section detection
        # Guard: "Vaisseaux\tQuantité" - the word "quantité" in value position should NOT
        # trigger RES mode. Only trigger RES if the word is on a standalone line (left of tab is a section header)
        _tab_parts = line.split("\t", 1) if "\t" in line else None
        _right_side = _tab_parts[1].lower() if _tab_parts else ""
        _left_side  = _tab_parts[0].strip().lower() if _tab_parts else ll

        # If "quantity/quantité/menge" appears only on the RIGHT of a tab, it's a column header - skip
        _right_triggers_res = any(h in _right_side for h in _SECTION_RES) if _tab_parts else False
        _left_is_section    = any(h in _left_side for h in _SECTION_RES) if not _tab_parts else (_left_side in _SECTION_RES)

        if _left_is_section and not _right_triggers_res:
            mode = MODE_RES
            pending_label = None
            continue
        elif not _tab_parts and any(h in ll for h in _SECTION_RES):
            mode = MODE_RES
            pending_label = None
            continue
        if any(h in ll for h in _SECTION_RECOVERED):
            mode = MODE_GAINED
            pending_label = None
            continue
        if any(h in ll for h in _SECTION_LOST):
            mode = MODE_LOST
            pending_label = None
            continue

        # Skip pure header lines (Schiffe / Ships / Vaisseaux  |  Menge / Qty)
        if ll in ("schiffe", "ships", "vaisseaux", "menge", "quantity", "quantité",
                  "schiffe\tmenge", "ships\tquantity", "vaisseaux\tquantité"):
            continue

        if mode == MODE_RES:
            # Try tab-separated "Label\tValue"
            if "\t" in line:
                parts = line.split("\t", 1)
                label_raw, val_raw = parts[0].strip(), parts[1].strip()
                key = RESOURCE_LABELS.get(label_raw)
                if key:
                    setattr(result, key, _parse_num(val_raw))
                    continue

            # Pending label pattern (label on one line, value on next)
            if pending_label is not None:
                m = _RE_RESOURCE_LINE.match(line)
                if m:
                    key = RESOURCE_LABELS.get(pending_label)
                    if key:
                        setattr(result, key, _parse_num(m.group(1)))
                    pending_label = None
                    continue
                else:
                    pending_label = None

            if line in RESOURCE_LABELS:
                pending_label = line
            else:
                # Maybe "Label  Value" space-separated
                for label, key in RESOURCE_LABELS.items():
                    if line.startswith(label):
                        val_part = line[len(label):].strip().lstrip(":").strip()
                        if val_part:
                            setattr(result, key, _parse_num(val_part))
                        break

        elif mode in (MODE_GAINED, MODE_LOST):
            # Try tab-separated "ShipName\tQty"
            if "\t" in line:
                parts = line.split("\t", 1)
                ship_raw, qty_raw = parts[0].strip(), parts[1].strip()
                canonical = SHIP_NAME_MAP.get(ship_raw)
                if canonical:
                    qty = abs(_parse_num(qty_raw))
                    if mode == MODE_GAINED:
                        result.ships_gained[canonical] = result.ships_gained.get(canonical, 0) + qty
                    else:
                        result.ships_lost[canonical]  = result.ships_lost.get(canonical, 0) + qty
                continue

            # Pending ship name pattern
            if pending_label is not None:
                m = _RE_SHIP_QTY.match(line)
                if m:
                    canonical = SHIP_NAME_MAP.get(pending_label)
                    if canonical:
                        qty = abs(_parse_num(m.group(1)))
                        if mode == MODE_GAINED:
                            result.ships_gained[canonical] = result.ships_gained.get(canonical, 0) + qty
                        else:
                            result.ships_lost[canonical]  = result.ships_lost.get(canonical, 0) + qty
                    pending_label = None
                    continue
                else:
                    pending_label = None

            if line in SHIP_NAME_MAP:
                pending_label = line

    result.classify_outcome()
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_expeditions(raw: str) -> list[ParsedExpedition]:
    """Parse a full copy-pasted inbox dump. Returns list of ParsedExpedition."""
    blocks = _split_blocks(raw)
    results = []
    for block in blocks:
        try:
            r = _parse_block(block)
        except Exception as e:
            r = ParsedExpedition(raw_text=block, parse_error=str(e))
        results.append(r)
    return results
