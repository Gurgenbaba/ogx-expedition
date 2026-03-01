# app/prestige.py
# Prestige engine + service for OGX Expedition (and shared with Oracle).
# Pure business logic — no FastAPI deps here.

from __future__ import annotations
from datetime import datetime, date, timedelta
from typing import Optional, List, Tuple, Dict, Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .models import (
    OPTransaction, UserPrestige, Achievement,
    UserAchievement, LeaderboardSnapshot,
)

# ─── Rank ladder ──────────────────────────────────────────────────────────────
RANKS = [
    ("Cadet",        0),
    ("Analyst",      500),
    ("Commander",    2_000),
    ("Strategist",   5_000),
    ("High Oracle",  15_000),
    ("Grand Oracle", 50_000),
]

SCANNER_TITLES = [
    ("Oracle Sentinel",       5_000),
    ("Galactic Cartographer", 1_500),
    ("Sector Analyst",          500),
    ("Deep Scanner",            200),
    ("Scout",                    50),
]

ACHIEVEMENT_DEFS = [
    # (slug, category, name, description, icon, op_reward, threshold)
    ("expo_100",         "expedition", "First Hundred",         "Import 100 expedition reports",       "📡", 0,    100),
    ("expo_250",         "expedition", "Deep Space Researcher", "Import 250 expedition reports",       "🔭", 50,   250),
    ("expo_1000",        "expedition", "Horizon Breaker",       "Import 1000 expedition reports",      "🌌", 150,  1000),
    ("expo_5000",        "expedition", "Void Architect",        "Import 5000 expedition reports",      "🕳️", 500,  5000),
    ("scan_50",          "scanner",    "Scout",                 "Scan 50 unique systems",              "🛸", 0,    50),
    ("scan_200",         "scanner",    "Deep Scanner",          "Scan 200 unique systems",             "📻", 50,   200),
    ("scan_500",         "scanner",    "Sector Analyst",        "Scan 500 unique systems",             "🗺️", 150,  500),
    ("scan_1500",        "scanner",    "Galactic Cartographer", "Scan 1500 unique systems",            "🌐", 400,  1500),
    ("scan_5000",        "scanner",    "Oracle Sentinel",       "Scan 5000 unique systems",            "👁️", 1000, 5000),
    ("streak_7",         "streak",     "Persistent Mind",       "7 consecutive active days",           "🔥", 0,    7),
    ("streak_30",        "streak",     "System Addict",         "30 consecutive active days",          "💎", 0,    30),
    ("streak_180",       "streak",     "Oracle Veteran",        "180 consecutive active days",         "🏛️", 500,  180),
    ("code_1",           "smuggler",   "First Contact",         "Find your first smuggler code",       "📦", 20,   1),
    ("code_10",          "smuggler",   "Code Collector",        "Find 10 smuggler codes",              "🗝️", 100,  10),
    ("rank_analyst",     "prestige",   "Analyst",               "Reach Analyst rank (500 OP)",         "📊", 0,    500),
    ("rank_commander",   "prestige",   "Commander",             "Reach Commander rank (2000 OP)",      "⚔️", 0,    2000),
    ("rank_strategist",  "prestige",   "Strategist",            "Reach Strategist rank (5000 OP)",     "🧠", 0,    5000),
    ("rank_high_oracle", "prestige",   "High Oracle",           "Reach High Oracle rank (15000 OP)",   "🔮", 0,    15000),
    ("rank_grand_oracle","prestige",   "Grand Oracle",          "Reach Grand Oracle rank (50000 OP)",  "👑", 0,    50000),
]


# ─── Pure functions ───────────────────────────────────────────────────────────

def get_rank(total_op: int) -> str:
    rank = "Cadet"
    for name, threshold in RANKS:
        if total_op >= threshold:
            rank = name
    return rank


def get_next_rank(total_op: int) -> Optional[Tuple[str, int]]:
    for name, threshold in RANKS:
        if total_op < threshold:
            return (name, threshold - total_op)
    return None


def get_scanner_title(scan_count: int) -> Optional[str]:
    for title, threshold in SCANNER_TITLES:
        if scan_count >= threshold:
            return title
    return None


def op_for_expo_import(new_count: int, total_before: int) -> List[Tuple[str, int]]:
    if new_count <= 0:
        return []
    awards = [("expo_import", new_count)]
    milestones = [
        (100,   50,  "expo_milestone_100"),
        (250,   75,  "expo_milestone_250"),
        (500,  100,  "expo_milestone_500"),
        (1000, 200,  "expo_milestone_1000"),
        (2500, 400,  "expo_milestone_2500"),
        (5000, 750,  "expo_milestone_5000"),
    ]
    total_after = total_before + new_count
    for threshold, bonus, reason in milestones:
        if total_before < threshold <= total_after:
            awards.append((reason, bonus))
    return awards


def op_for_galaxy_scan(new_systems: int, total_before: int) -> List[Tuple[str, int]]:
    if new_systems <= 0:
        return []
    awards = [("galaxy_scan", new_systems * 5)]
    milestones = [
        (50,    100, "scan_milestone_50"),
        (200,   200, "scan_milestone_200"),
        (500,   400, "scan_milestone_500"),
        (1500,  750, "scan_milestone_1500"),
        (5000, 1500, "scan_milestone_5000"),
    ]
    total_after = total_before + new_systems
    for threshold, bonus, reason in milestones:
        if total_before < threshold <= total_after:
            awards.append((reason, bonus))
    return awards


def op_for_daily_login(
    last_active_date: Optional[date],
    today: date,
    current_streak: int,
    streak_7d_claimed: bool,
    streak_30d_claimed: bool,
) -> Tuple[List[Tuple[str, int]], int]:
    awards = []
    if last_active_date is None:
        new_streak = 1
        awards.append(("daily_login", 10))
    elif last_active_date == today:
        return ([], current_streak)
    elif last_active_date == today - timedelta(days=1):
        new_streak = current_streak + 1
        awards.append(("daily_login", 10))
    else:
        new_streak = 1
        awards.append(("daily_login", 10))

    if new_streak >= 7 and not streak_7d_claimed:
        awards.append(("streak_7d", 100))
    if new_streak >= 30 and not streak_30d_claimed:
        awards.append(("streak_30d", 500))
    return (awards, new_streak)


def check_achievements(prestige: UserPrestige, unlocked_slugs: set) -> List[str]:
    checks = {
        "expo_100":          prestige.expo_count >= 100,
        "expo_250":          prestige.expo_count >= 250,
        "expo_1000":         prestige.expo_count >= 1000,
        "expo_5000":         prestige.expo_count >= 5000,
        "scan_50":           prestige.scan_count >= 50,
        "scan_200":          prestige.scan_count >= 200,
        "scan_500":          prestige.scan_count >= 500,
        "scan_1500":         prestige.scan_count >= 1500,
        "scan_5000":         prestige.scan_count >= 5000,
        "streak_7":          prestige.longest_streak >= 7,
        "streak_30":         prestige.longest_streak >= 30,
        "streak_180":        prestige.longest_streak >= 180,
        "code_1":            prestige.smuggler_count >= 1,
        "code_10":           prestige.smuggler_count >= 10,
        "rank_analyst":      prestige.total_op >= 500,
        "rank_commander":    prestige.total_op >= 2000,
        "rank_strategist":   prestige.total_op >= 5000,
        "rank_high_oracle":  prestige.total_op >= 15000,
        "rank_grand_oracle": prestige.total_op >= 50000,
    }
    return [slug for slug, condition in checks.items() if condition and slug not in unlocked_slugs]


# ─── Async service functions ──────────────────────────────────────────────────

async def _get_or_create_prestige(db: AsyncSession, user_id: int) -> UserPrestige:
    result = await db.execute(select(UserPrestige).where(UserPrestige.user_id == user_id))
    prestige = result.scalar_one_or_none()
    if not prestige:
        prestige = UserPrestige(user_id=user_id)
        db.add(prestige)
        await db.flush()
    return prestige


async def _unlock_achievements(db: AsyncSession, user_id: int, prestige: UserPrestige) -> None:
    result = await db.execute(
        select(Achievement.slug, Achievement.id, Achievement.op_reward)
        .join(UserAchievement, UserAchievement.achievement_id == Achievement.id)
        .where(UserAchievement.user_id == user_id)
    )
    unlocked_slugs = {r[0] for r in result.fetchall()}
    newly_unlocked = check_achievements(prestige, unlocked_slugs)
    if not newly_unlocked:
        return

    result2 = await db.execute(select(Achievement).where(Achievement.slug.in_(newly_unlocked)))
    achievements = result2.scalars().all()
    now = datetime.utcnow()
    for a in achievements:
        db.add(UserAchievement(user_id=user_id, achievement_id=a.id, unlocked_at=now))
        if a.op_reward > 0:
            db.add(OPTransaction(
                user_id=user_id, amount=a.op_reward,
                reason=f"achievement_{a.slug}", source_app="system", created_at=now,
            ))
            prestige.total_op += a.op_reward
            prestige.prestige_rank = get_rank(prestige.total_op)


async def award_op(
    db: AsyncSession,
    user_id: int,
    awards: List[Tuple[str, int]],
    source_app: str,
    ref_id: Optional[int] = None,
) -> int:
    if not awards:
        return 0
    total = 0
    now = datetime.utcnow()
    for reason, amount in awards:
        if amount == 0:
            continue
        db.add(OPTransaction(
            user_id=user_id, amount=amount, reason=reason,
            source_app=source_app, ref_id=ref_id, created_at=now,
        ))
        total += amount
    if total == 0:
        return 0
    prestige = await _get_or_create_prestige(db, user_id)
    prestige.total_op += total
    prestige.prestige_rank = get_rank(prestige.total_op)
    prestige.updated_at = now
    await _unlock_achievements(db, user_id, prestige)
    await db.flush()
    return total


async def handle_expo_import(db: AsyncSession, user_id: int, new_count: int) -> Dict[str, Any]:
    prestige = await _get_or_create_prestige(db, user_id)
    awards = op_for_expo_import(new_count, prestige.expo_count)
    prestige.expo_count += new_count
    op_total = await award_op(db, user_id, awards, "expedition")
    return {
        "op_awarded": op_total,
        "new_total_op": prestige.total_op,
        "prestige_rank": prestige.prestige_rank,
        "expo_count": prestige.expo_count,
        "awards": [{"reason": r, "amount": a} for r, a in awards],
    }


async def handle_galaxy_scan(db: AsyncSession, user_id: int, new_unique_systems: int) -> Dict[str, Any]:
    prestige = await _get_or_create_prestige(db, user_id)
    awards = op_for_galaxy_scan(new_unique_systems, prestige.scan_count)
    prestige.scan_count += new_unique_systems
    prestige.scanner_title = get_scanner_title(prestige.scan_count)
    op_total = await award_op(db, user_id, awards, "oracle")
    return {
        "op_awarded": op_total,
        "new_total_op": prestige.total_op,
        "prestige_rank": prestige.prestige_rank,
        "scan_count": prestige.scan_count,
        "scanner_title": prestige.scanner_title,
        "awards": [{"reason": r, "amount": a} for r, a in awards],
    }


async def handle_smuggler_code(db: AsyncSession, user_id: int) -> Dict[str, Any]:
    prestige = await _get_or_create_prestige(db, user_id)
    prestige.smuggler_count += 1
    awards = [("smuggler_code", 20)]
    op_total = await award_op(db, user_id, awards, "expedition")
    return {"op_awarded": op_total, "new_total_op": prestige.total_op, "prestige_rank": prestige.prestige_rank}


async def handle_daily_login(db: AsyncSession, user_id: int, source_app: str) -> Dict[str, Any]:
    prestige = await _get_or_create_prestige(db, user_id)
    today = date.today()
    last = prestige.last_active_date.date() if prestige.last_active_date else None
    awards, new_streak = op_for_daily_login(
        last, today, prestige.current_streak,
        prestige.streak_milestone_7_claimed, prestige.streak_milestone_30_claimed,
    )
    if not awards:
        return {"op_awarded": 0, "streak": prestige.current_streak, "already_claimed": True}

    prestige.current_streak = new_streak
    prestige.longest_streak = max(prestige.longest_streak, new_streak)
    prestige.last_active_date = datetime.utcnow()
    for reason, _ in awards:
        if reason == "streak_7d":  prestige.streak_milestone_7_claimed = True
        if reason == "streak_30d": prestige.streak_milestone_30_claimed = True

    op_total = await award_op(db, user_id, awards, source_app)
    return {
        "op_awarded": op_total,
        "new_total_op": prestige.total_op,
        "prestige_rank": prestige.prestige_rank,
        "streak": new_streak,
        "awards": [{"reason": r, "amount": a} for r, a in awards],
    }


async def get_prestige_summary(db: AsyncSession, user_id: int) -> Dict[str, Any]:
    prestige = await _get_or_create_prestige(db, user_id)
    next_rank = get_next_rank(prestige.total_op)

    # Progress to next rank as percentage
    if next_rank:
        current_rank_op = next(t for n, t in RANKS if n == prestige.prestige_rank)
        next_rank_op = current_rank_op + next_rank[1]
        progress_pct = round((prestige.total_op - current_rank_op) / next_rank[1] * 100)
    else:
        progress_pct = 100

    # Unlocked achievements
    result = await db.execute(
        select(Achievement)
        .join(UserAchievement, UserAchievement.achievement_id == Achievement.id)
        .where(UserAchievement.user_id == user_id)
        .order_by(UserAchievement.unlocked_at.desc())
    )
    unlocked = [
        {"slug": a.slug, "name": a.name, "icon": a.icon,
         "category": a.category, "description": a.description}
        for a in result.scalars().all()
    ]

    # Next achievements to unlock (locked, not hidden)
    result2 = await db.execute(
        select(Achievement).where(
            Achievement.hidden.is_(False),
            ~Achievement.id.in_(
                select(UserAchievement.achievement_id)
                .where(UserAchievement.user_id == user_id)
            )
        ).order_by(Achievement.threshold)
    )
    locked = [
        {"slug": a.slug, "name": a.name, "icon": a.icon,
         "category": a.category, "description": a.description,
         "threshold": a.threshold, "op_reward": a.op_reward}
        for a in result2.scalars().all()[:8]
    ]

    return {
        "user_id":        user_id,
        "total_op":       prestige.total_op,
        "prestige_rank":  prestige.prestige_rank,
        "scanner_title":  prestige.scanner_title,
        "expo_count":     prestige.expo_count,
        "scan_count":     prestige.scan_count,
        "smuggler_count": prestige.smuggler_count,
        "current_streak": prestige.current_streak,
        "longest_streak": prestige.longest_streak,
        "progress_pct":   progress_pct,
        "next_rank":      {"name": next_rank[0], "op_needed": next_rank[1]} if next_rank else None,
        "achievements_unlocked": unlocked,
        "achievements_locked":   locked,
    }


async def get_leaderboard(db: AsyncSession, limit: int = 20) -> List[Dict]:
    result = await db.execute(
        select(UserPrestige)
        .order_by(UserPrestige.total_op.desc())
        .limit(limit)
    )
    return [
        {
            "rank":          i + 1,
            "user_id":       r.user_id,
            "total_op":      r.total_op,
            "prestige_rank": r.prestige_rank,
            "scanner_title": r.scanner_title,
            "expo_count":    r.expo_count,
            "scan_count":    r.scan_count,
        }
        for i, r in enumerate(result.scalars().all())
    ]


async def seed_achievements(db: AsyncSession) -> None:
    """Idempotent: run at startup to ensure all achievement definitions exist."""
    for (slug, category, name, description, icon, op_reward, threshold) in ACHIEVEMENT_DEFS:
        existing = await db.execute(select(Achievement).where(Achievement.slug == slug))
        if not existing.scalar_one_or_none():
            db.add(Achievement(
                slug=slug, category=category, name=name,
                description=description, icon=icon,
                op_reward=op_reward, threshold=threshold,
            ))
    await db.commit()
