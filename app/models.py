# app/models.py
from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlalchemy import (
    String, Integer, BigInteger, Boolean, DateTime,
    Float, Text, Index, text, JSON
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.utcnow()


SERVER_NOW = text("CURRENT_TIMESTAMP")

# ---------------------------------------------------------------------------
# Shared: we mirror only what we need from ogx-oraclev2's users table.
# If running on the SAME DB, this table already exists — we don't recreate it.
# If running on a SEPARATE DB, we create a minimal local users table.
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(100), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, server_default=SERVER_NOW)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = {"extend_existing": True}


# ---------------------------------------------------------------------------
# Expedition outcome types
# ---------------------------------------------------------------------------
# success_res   = resources found
# success_ships = ships found (no resources)
# success_mix   = resources + ships
# success_dm    = dark matter only
# success_mix_dm= resources + dark matter
# success_full  = resources + ships + dark matter
# pirates_win   = fought pirates, won
# pirates_loss  = fought pirates, lost
# storm         = ion storm (partial loss)
# contact_lost  = contact lost (partial loss)
# gravity       = gravity anomaly (partial loss)
# vanished      = fleet vanished (total loss)
# failed        = nothing happened, no loss
OUTCOME_TYPES = (
    "success_res", "success_ships", "success_mix",
    "success_dm", "success_mix_dm", "success_full",
    "pirates_win", "pirates_loss",
    "storm", "contact_lost", "gravity",
    "vanished", "failed",
)


class Expedition(Base):
    __tablename__ = "expeditions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    # Game-assigned expedition number (unique per universe/account)
    exp_number: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)

    # When the expedition returned (from the message timestamp)
    returned_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)

    # When we imported it
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, server_default=SERVER_NOW)

    # Outcome classification
    outcome_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)

    # Resources gained (positive = gained, stored as raw integers)
    metal: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    crystal: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    deuterium: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    dark_matter: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # Dark matter bonus from "Schwarzer Horizont"
    dark_matter_bonus: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    dark_matter_bonus_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Ships gained / lost as JSON: {"Großer Transporter": 1200, ...}
    # Positive = gained, negative = lost
    ships_delta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Fleet sent (filled by user in optimizer, optional)
    fleet_sent: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Loss percentage (for storm/contact/gravity events)
    loss_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Pirate encounter details
    pirate_strength: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    pirate_win_chance: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # ~54%
    pirate_loss_rate: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)   # 28%

    # Raw message text (for debugging / re-parsing)
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Dedup: prevent importing same expedition twice
    # Hash of (user_id, exp_number) — or (user_id, returned_at, outcome_type) if no number
    dedup_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True, index=True)

    __table_args__ = (
        Index("ix_exp_user_returned", "user_id", "returned_at"),
        Index("ix_exp_user_outcome", "user_id", "outcome_type"),
        Index("ix_exp_user_number", "user_id", "exp_number"),
    )


class ExpeditionImport(Base):
    __tablename__ = "expedition_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_now, server_default=SERVER_NOW)
    count_parsed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    count_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    count_duplicate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    count_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
