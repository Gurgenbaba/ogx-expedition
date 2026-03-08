# app/main.py
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Form, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sqlalchemy import select, func, text, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .db import engine, AsyncSessionLocal, IS_SQLITE, IS_POSTGRES
from .models import Base, User, Expedition, ExpeditionImport, SmugglerCode
from .settings import settings
from .security import (
    require_jwt_user,
    hash_password,
    verify_password,
    create_access_token,
)
from .parser import parse_expedition_text
from .prestige import (
    handle_expo_import as prestige_expo,
    handle_smuggler_code as prestige_smuggler,
    handle_daily_login as prestige_login,
    get_prestige_summary,
    get_leaderboard as prestige_leaderboard,
    seed_achievements,
)
from .i18n import get_lang, make_translator, get_translations_js, SUPPORTED, FLAG, LABEL
from .crypto import encrypt_code, decrypt_code, hash_code
from .optimizer import optimize_fleet, get_user_stats_summary, OptimizerInput, SHIP_STATS

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.globals["now_utc"] = lambda: datetime.now(timezone.utc)


def _fmt_num(n) -> str:
    """Format large numbers: 163.5 Mrd, 1.2 M etc."""
    try:
        n = int(n)
    except Exception:
        return "0"
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f} Mrd"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f} M"
    if n >= 1_000:
        return f"{n/1_000:.1f} K"
    return str(n)


templates.env.filters["fmt_num"] = _fmt_num
templates.env.filters["fmt_int"] = lambda n: f"{int(n):,}".replace(",", ".")
templates.env.filters["decrypt"] = decrypt_code


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.env == "dev":
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    else:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
    yield


app = FastAPI(title="OGX Expedition", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

CSRF_COOKIE = "ogx_csrf"


def _template(request: Request, name: str, ctx: dict) -> HTMLResponse:
    lang = get_lang(request)
    base = {
        "request": request,
        "t":       make_translator(lang),
        "lang":    lang,
        "langs":   [{"code": c, "flag": FLAG[c], "label": LABEL[c]} for c in SUPPORTED],
        "i18n_js": get_translations_js(lang),
    }
    base.update(ctx)
    return templates.TemplateResponse(request, name, base)


async def _template_with_codes(request: Request, name: str, ctx: dict, db, user) -> HTMLResponse:
    """Like _template but injects pending_codes_count, t(), lang, and lang switcher info."""
    pending_codes = 0
    if user:
        try:
            pending_codes = (await db.execute(
                select(func.count(SmugglerCode.id))
                .where(SmugglerCode.user_id == user.id, SmugglerCode.redeemed == False)  # noqa: E712
            )).scalar() or 0
        except Exception:
            pass

    lang = get_lang(request)
    t    = make_translator(lang)

    ctx["pending_codes_count"] = pending_codes
    ctx["t"]      = t
    ctx["lang"]   = lang
    ctx["langs"]  = [{"code": c, "flag": FLAG[c], "label": LABEL[c]} for c in SUPPORTED]
    ctx["i18n_js"] = get_translations_js(lang)
    return _template(request, name, ctx)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------




@app.get("/api/prestige")
async def api_prestige(request: Request):
    """JSON prestige summary + leaderboard for the current user."""
    async with AsyncSessionLocal() as db:
        u, err = await require_jwt_user(request, db)
        if err:
            return err
        summary = await get_prestige_summary(db, int(u.id))
        board   = await prestige_leaderboard(db, limit=20)
        # Enrich with usernames + is_current_user flag
        for entry in board:
            result = await db.execute(select(User).where(User.id == entry["user_id"]))
            usr = result.scalar_one_or_none()
            entry["username"]        = usr.username if usr else f"user_{entry['user_id']}"
            entry["is_current_user"] = (entry["user_id"] == int(u.id))
        return JSONResponse({"ok": True, **summary, "leaderboard": board})


@app.get("/api/leaderboard")
async def api_leaderboard(request: Request):
    async with AsyncSessionLocal() as db:
        u, err = await require_jwt_user(request, db)
        if err:
            return err
        board = await prestige_leaderboard(db, limit=20)
        # Enrich with usernames
        for entry in board:
            result = await db.execute(select(User).where(User.id == entry["user_id"]))
            usr = result.scalar_one_or_none()
            entry["username"] = usr.username if usr else f"user_{entry['user_id']}"
        return JSONResponse({"ok": True, "leaderboard": board})


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Auth (mirrors ogx-oraclev2 — reads/writes shared users table)
# ---------------------------------------------------------------------------
@app.post("/auth/login")
async def auth_login(payload: dict = Body(...)):
    username = str(payload.get("username") or "").strip().lower()
    password = str(payload.get("password") or "")
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.username == username))
        u = res.scalar_one_or_none()
        if not u or not u.is_active or not verify_password(password, u.password_hash):
            return JSONResponse({"ok": False, "error": "invalid_login"}, status_code=401)
        u.last_login_at = _utcnow()
        await db.commit()
        # Award daily login OP
        try:
            async with AsyncSessionLocal() as prestige_db:
                await prestige_login(prestige_db, int(u.id), "expedition")
                await prestige_db.commit()
        except Exception:
            pass  # never block login on prestige errors
        token = create_access_token(user=u)
        return {"ok": True, "token": token, "username": u.username, "is_admin": u.is_admin}


@app.post("/auth/register")
async def auth_register(payload: dict = Body(...)):
    if not settings.allow_registration:
        return JSONResponse({"ok": False, "error": "registration_disabled"}, status_code=403)
    username = str(payload.get("username") or "").strip().lower()
    password = str(payload.get("password") or "")
    if len(username) < settings.username_min_len or len(username) > settings.username_max_len:
        return JSONResponse({"ok": False, "error": "invalid_username"}, status_code=400)
    if len(password) < settings.password_min_len:
        return JSONResponse({"ok": False, "error": "password_too_short"}, status_code=400)
    async with AsyncSessionLocal() as db:
        make_admin = False
        if True:  # bootstrap first user
            cnt = (await db.execute(select(func.count(User.id)))).scalar() or 0
            if cnt == 0:
                make_admin = True
        exists = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if exists:
            return JSONResponse({"ok": False, "error": "username_taken"}, status_code=409)
        u = User(username=username, password_hash=hash_password(password), is_admin=make_admin, is_active=True, token_version=0)
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return {"ok": True, "token": create_access_token(u), "username": u.username, "is_admin": u.is_admin}


@app.get("/auth/me")
async def auth_me(request: Request):
    async with AsyncSessionLocal() as db:
        u, err = await require_jwt_user(request, db)
        if err:
            return err
        return {"ok": True, "username": u.username, "is_admin": u.is_admin}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    async with AsyncSessionLocal() as db:
        u, _ = await require_jwt_user(request, db)
        if not u:
            return _template(request, "login.html", {"active_nav": "dashboard"})

        # Server filter from query param
        server_filter = request.query_params.get("server") or None

        # Fetch all expeditions for this user
        q = select(Expedition).where(Expedition.user_id == u.id)
        if server_filter:
            q = q.where(Expedition.server_id == server_filter)
        q = q.order_by(Expedition.returned_at.desc())
        exps = (await db.execute(q)).scalars().all()

        stats = get_user_stats_summary(list(exps))

        # Outcome distribution for chart
        outcome_counts: dict[str, int] = {}
        for e in exps:
            outcome_counts[e.outcome_type] = outcome_counts.get(e.outcome_type, 0) + 1

        # All distinct server_ids this user has data for
        server_rows = (await db.execute(
            text("SELECT DISTINCT server_id FROM expeditions WHERE user_id = :uid AND server_id IS NOT NULL ORDER BY server_id"),
            {"uid": u.id}
        )).fetchall()
        servers = [r[0] for r in server_rows]

        # Check if user has a link code (for bridge sync button)
        link_row = (await db.execute(
            text("SELECT code FROM link_codes WHERE user_id = :uid ORDER BY id DESC LIMIT 1"),
            {"uid": u.id}
        )).fetchone()
        has_link_code = bool(link_row and link_row[0])

        recent = exps[:50]

        return await _template_with_codes(request, "dashboard.html", {
            "user": u,
            "stats": stats,
            "outcome_counts": outcome_counts,
            "recent": recent,
            "total": len(exps),
            "active_nav": "dashboard",
            "servers": servers,
            "server_filter": server_filter,
            "has_link_code": has_link_code,
        }, db, u)


@app.get("/import", response_class=HTMLResponse)
async def import_page(request: Request):
    async with AsyncSessionLocal() as db:
        u, _ = await require_jwt_user(request, db)
        if not u:
            return RedirectResponse(url="/", status_code=303)
        return await _template_with_codes(request, "import.html", {
            "user": u,
            "active_nav": "import",
            "known_servers": KNOWN_SERVERS,
        }, db, u)


@app.post("/import")
async def do_import(request: Request, raw_text: str = Form(...), server_id: str = Form(default="")):
    async with AsyncSessionLocal() as db:
        u, err = await require_jwt_user(request, db)
        if err:
            return err

        if len(raw_text.encode()) > settings.max_paste_bytes:
            return JSONResponse({"ok": False, "error": "paste_too_large"}, status_code=413)

        # Validate server_id
        import_server_id: Optional[str] = server_id.strip() or None
        if import_server_id and import_server_id not in KNOWN_SERVERS:
            import_server_id = None

        parsed = parse_expedition_text(raw_text)
        if not parsed:
            return RedirectResponse(url="/import?error=no_expeditions_found", status_code=303)

        # Cache user_id before any flushes/rollbacks expire the ORM object
        uid = int(u.id)

        count_new = 0
        count_dup = 0
        count_fail = 0

        for p in parsed[:settings.max_expeditions_per_import]:
            if p.parse_error:
                count_fail += 1
                continue

            row = dict(
                user_id=uid,
                server_id=import_server_id,
                exp_number=p.exp_number,
                returned_at=p.returned_at,
                outcome_type=p.outcome_type,
                metal=p.metal,
                crystal=p.crystal,
                deuterium=p.deuterium,
                dark_matter=p.dark_matter,
                dark_matter_bonus=p.dark_matter_bonus,
                dark_matter_bonus_pct=p.dark_matter_bonus_pct,
                ships_delta=p.ships_delta or None,
                loss_percent=p.loss_percent,
                pirate_strength=p.pirate_strength,
                pirate_win_chance=p.pirate_win_chance,
                pirate_loss_rate=p.pirate_loss_rate,
                raw_text=None,
                dedup_key=p.dedup_key,
            )

            if IS_POSTGRES:
                # Silent upsert — zero errors in DB log, no savepoint overhead
                stmt = pg_insert(Expedition).values(**row)
                stmt = stmt.on_conflict_do_nothing(index_elements=["dedup_key"])
                result = await db.execute(stmt)
                if result.rowcount:
                    count_new += 1
                else:
                    count_dup += 1
            else:
                # SQLite fallback
                exp = Expedition(**row)
                try:
                    async with db.begin_nested():
                        db.add(exp)
                        await db.flush()
                    count_new += 1
                except IntegrityError:
                    count_dup += 1

        # Save smuggler codes found in this import
        for p in parsed:
            if p.smuggler_code:
                sc_row = dict(
                    user_id=uid,
                    exp_number=p.exp_number,
                    code=encrypt_code(p.smuggler_code),
                    code_hash=hash_code(p.smuggler_code),
                    tier=p.smuggler_tier,
                    found_at=p.returned_at,
                )
                if IS_POSTGRES:
                    sc_stmt = pg_insert(SmugglerCode).values(**sc_row)
                    sc_stmt = sc_stmt.on_conflict_do_nothing(
                        index_elements=["user_id", "code_hash"]
                    )
                    await db.execute(sc_stmt)
                else:
                    sc = SmugglerCode(**sc_row)
                    try:
                        async with db.begin_nested():
                            db.add(sc)
                            await db.flush()
                    except IntegrityError:
                        pass  # duplicate code

        imp = ExpeditionImport(
            user_id=uid,
            count_parsed=len(parsed),
            count_new=count_new,
            count_duplicate=count_dup,
            count_failed=count_fail,
        )
        db.add(imp)
        await db.commit()

    # Award OP for new expedition imports
    if count_new > 0:
        async with AsyncSessionLocal() as prestige_db:
            await prestige_expo(prestige_db, uid, count_new)
            await prestige_db.commit()

    return RedirectResponse(
        url=f"/import?imported={count_new}&duplicates={count_dup}&failed={count_fail}",
        status_code=303,
    )


# ─── API endpoints for OGX Expedition Collector userscript ────────────────

@app.post("/api/import")
async def api_import(request: Request, payload: dict = Body(...)):
    """JSON import endpoint for the Collector userscript. Returns JSON instead of redirect.
    Payload may include server_id: 'uni1' | 'beta' to tag expeditions by universe.
    """
    async with AsyncSessionLocal() as db:
        u, err = await require_jwt_user(request, db)
        if err:
            return err

        raw_text = str(payload.get("raw_text") or "").strip()
        if not raw_text:
            return JSONResponse({"ok": False, "error": "empty_text"}, status_code=400)

        if len(raw_text.encode()) > settings.max_paste_bytes:
            return JSONResponse({"ok": False, "error": "paste_too_large"}, status_code=413)

        # server_id from userscript payload (e.g. "uni1" or "beta")
        api_server_id: Optional[str] = str(payload.get("server_id") or "").strip() or None
        if api_server_id and api_server_id not in KNOWN_SERVERS:
            api_server_id = None

        parsed = parse_expedition_text(raw_text)
        if not parsed:
            return JSONResponse({"ok": True, "count_new": 0, "count_duplicate": 0, "count_failed": 0, "message": "no_expeditions_found"})

        uid = int(u.id)
        count_new = count_dup = count_fail = 0

        for p in parsed[:settings.max_expeditions_per_import]:
            if p.parse_error:
                count_fail += 1
                continue

            row = dict(
                user_id=uid,
                server_id=api_server_id,
                exp_number=p.exp_number,
                returned_at=p.returned_at,
                outcome_type=p.outcome_type,
                metal=p.metal,
                crystal=p.crystal,
                deuterium=p.deuterium,
                dark_matter=p.dark_matter,
                dark_matter_bonus=p.dark_matter_bonus,
                dark_matter_bonus_pct=p.dark_matter_bonus_pct,
                ships_delta=p.ships_delta or None,
                loss_percent=p.loss_percent,
                pirate_strength=p.pirate_strength,
                pirate_win_chance=p.pirate_win_chance,
                pirate_loss_rate=p.pirate_loss_rate,
                raw_text=None,
                dedup_key=p.dedup_key,
            )

            if IS_POSTGRES:
                stmt = pg_insert(Expedition).values(**row)
                stmt = stmt.on_conflict_do_nothing(index_elements=["dedup_key"])
                result = await db.execute(stmt)
                if result.rowcount:
                    count_new += 1
                else:
                    count_dup += 1
            else:
                exp = Expedition(**row)
                try:
                    async with db.begin_nested():
                        db.add(exp)
                        await db.flush()
                    count_new += 1
                except IntegrityError:
                    count_dup += 1

        for p in parsed:
            if p.smuggler_code:
                sc_row = dict(
                    user_id=uid,
                    exp_number=p.exp_number,
                    code=encrypt_code(p.smuggler_code),
                    code_hash=hash_code(p.smuggler_code),
                    tier=p.smuggler_tier,
                    found_at=p.returned_at,
                )
                if IS_POSTGRES:
                    sc_stmt = pg_insert(SmugglerCode).values(**sc_row)
                    sc_stmt = sc_stmt.on_conflict_do_nothing(index_elements=["user_id", "code_hash"])
                    await db.execute(sc_stmt)
                else:
                    sc = SmugglerCode(**sc_row)
                    try:
                        async with db.begin_nested():
                            db.add(sc)
                            await db.flush()
                    except IntegrityError:
                        pass

        imp = ExpeditionImport(
            user_id=uid,
            count_parsed=len(parsed),
            count_new=count_new,
            count_duplicate=count_dup,
            count_failed=count_fail,
        )
        db.add(imp)
        await db.commit()

    # Award OP for new expedition imports
    prestige_result = {}
    if count_new > 0:
        async with AsyncSessionLocal() as prestige_db:
            prestige_result = await prestige_expo(prestige_db, uid, count_new)
            await prestige_db.commit()

    return JSONResponse({
        "ok": True,
        "count_new": count_new,
        "count_duplicate": count_dup,
        "count_failed": count_fail,
        "count_parsed": len(parsed),
        "prestige": prestige_result,
    })


@app.post("/api/fleet")
async def api_fleet(request: Request, payload: dict = Body(...)):
    """
    Receives fleet data from the Collector userscript.
    Stores ships/slots/astro as the user's saved fleet in localStorage-compatible format.
    Returns the fleet_key data so the optimizer can auto-load it.
    """
    async with AsyncSessionLocal() as db:
        u, err = await require_jwt_user(request, db)
        if err:
            return err

    ships       = {k: int(v) for k, v in (payload.get("ships") or {}).items() if int(v or 0) > 0}
    slots       = int(payload.get("slots") or 0) or None
    astro_level = int(payload.get("astro_level") or 0) or None
    max_per_slot = int(payload.get("max_per_slot") or 0) or None

    # Build the fleet object compatible with optimizer localStorage key "ogx_fleet_v1"
    fleet = dict(ships)
    if slots:
        fleet["__slots"] = slots
    if max_per_slot:
        fleet["__max"] = max_per_slot

    return JSONResponse({
        "ok": True,
        "fleet_key": "ogx_fleet_v1",
        "fleet_data": fleet,
        "ships_count": len(ships),
        "slots": slots,
        "astro_level": astro_level,
        "max_per_slot": max_per_slot,
    })



@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    async with AsyncSessionLocal() as db:
        u, _ = await require_jwt_user(request, db)
        if not u:
            return RedirectResponse(url="/", status_code=303)

        server_filter = request.query_params.get("server") or None
        servers = await _get_user_servers(db, u.id)

        exps = (await db.execute(
            _server_filter_query(u.id, server_filter).order_by(Expedition.returned_at.desc())
        )).scalars().all()

        stats = get_user_stats_summary(list(exps))

        from collections import defaultdict
        import datetime as _dt

        # By outcome type (for table)
        by_type: dict[str, dict] = {}
        for e in exps:
            ot = e.outcome_type
            if ot not in by_type:
                by_type[ot] = {"count": 0, "metal": 0, "crystal": 0, "deut": 0, "dm": 0, "gt_lost": 0,
                                "ships_lost": {}, "ships_gained": {}}
            by_type[ot]["count"] += 1
            by_type[ot]["metal"] += e.metal
            by_type[ot]["crystal"] += e.crystal
            by_type[ot]["deut"] += e.deuterium
            by_type[ot]["dm"] += e.dark_matter
            if e.ships_delta:
                by_type[ot]["gt_lost"] += abs(e.ships_delta.get("Großer Transporter", 0))
                for ship, qty in e.ships_delta.items():
                    if qty < 0:
                        by_type[ot]["ships_lost"][ship] = by_type[ot]["ships_lost"].get(ship, 0) + abs(qty)
                    elif qty > 0:
                        by_type[ot]["ships_gained"][ship] = by_type[ot]["ships_gained"].get(ship, 0) + qty

        # Ships totals
        ships_gained: dict[str, int] = {}
        ships_lost: dict[str, int] = {}
        for e in exps:
            if not e.ships_delta:
                continue
            for ship, qty in e.ships_delta.items():
                if qty > 0:
                    ships_gained[ship] = ships_gained.get(ship, 0) + qty
                else:
                    ships_lost[ship] = ships_lost.get(ship, 0) + abs(qty)

        # Weekly timeline (for chart)
        weekly: dict = defaultdict(lambda: defaultdict(int))
        for e in exps:
            if e.returned_at:
                iso = e.returned_at.isocalendar()
                wk = f"{iso[0]}-W{iso[1]:02d}"
                weekly[wk][e.outcome_type] += 1

        today = _dt.date.today()
        timeline = []
        all_outcome_types = list(by_type.keys())
        for i in range(15, -1, -1):
            d = today - _dt.timedelta(weeks=i)
            iso = d.isocalendar()
            wk = f"{iso[0]}-W{iso[1]:02d}"
            entry = {"week": wk}
            for ot in all_outcome_types:
                entry[ot] = weekly[wk].get(ot, 0)
            timeline.append(entry)

        return await _template_with_codes(request, "stats.html", {
            "user": u,
            "stats": stats,
            "by_type": dict(sorted(by_type.items(), key=lambda x: -x[1]["count"])),
            "ships_gained": dict(sorted(ships_gained.items(), key=lambda x: -x[1])),
            "ships_lost": dict(sorted(ships_lost.items(), key=lambda x: -x[1])),
            "total": len(exps),
            "timeline": timeline,
            "outcome_types": all_outcome_types,
            "active_nav": "stats",
            "servers": servers,
            "server_filter": server_filter,
        }, db, u)


@app.get("/dm", response_class=HTMLResponse)
async def dm_page(request: Request):
    async with AsyncSessionLocal() as db:
        u, _ = await require_jwt_user(request, db)
        if not u:
            return RedirectResponse(url="/", status_code=303)

        server_filter = request.query_params.get("server") or None
        servers = await _get_user_servers(db, u.id)

        exps = (await db.execute(
            _server_filter_query(u.id, server_filter)
            .where(Expedition.dark_matter > 0)
            .order_by(Expedition.returned_at.asc())
        )).scalars().all()

        all_exps_count = (await db.execute(
            _server_filter_query(u.id, server_filter).with_only_columns(func.count(Expedition.id))
        )).scalar() or 0

        # Build weekly/monthly buckets
        from collections import defaultdict
        import datetime

        weekly: dict[str, int] = defaultdict(int)
        monthly: dict[str, int] = defaultdict(int)
        total_dm = 0
        total_bonus = 0

        for e in exps:
            if not e.returned_at:
                continue
            dm = e.dark_matter
            total_dm += dm
            total_bonus += e.dark_matter_bonus

            # ISO week key: "2025-W42"
            iso = e.returned_at.isocalendar()
            wk = f"{iso[0]}-W{iso[1]:02d}"
            weekly[wk] += dm

            # Month key: "2025-10"
            mk = e.returned_at.strftime("%Y-%m")
            monthly[mk] += dm

        # Fill missing weeks (last 12 weeks) with 0
        today = datetime.date.today()
        last_12_weeks = []
        for i in range(11, -1, -1):
            d = today - datetime.timedelta(weeks=i)
            iso = d.isocalendar()
            wk = f"{iso[0]}-W{iso[1]:02d}"
            last_12_weeks.append({"label": wk, "dm": weekly.get(wk, 0)})

        last_12_months = []
        for i in range(11, -1, -1):
            # subtract months
            year = today.year
            month = today.month - i
            while month <= 0:
                month += 12
                year -= 1
            mk = f"{year}-{month:02d}"
            label = datetime.date(year, month, 1).strftime("%b %Y")
            last_12_months.append({"label": label, "key": mk, "dm": monthly.get(mk, 0)})

        dm_per_expo = round(total_dm / all_exps_count) if all_exps_count else 0
        dm_expos_count = len(exps)
        dm_rate = round(dm_expos_count / all_exps_count * 100, 1) if all_exps_count else 0

        return await _template_with_codes(request, "dm.html", {
            "user": u,
            "active_nav": "dm",
            "total_dm": total_dm,
            "total_bonus": total_bonus,
            "dm_per_expo": dm_per_expo,
            "dm_expos_count": dm_expos_count,
            "all_exps_count": all_exps_count,
            "dm_rate": dm_rate,
            "weekly": last_12_weeks,
            "monthly": last_12_months,
            "servers": servers,
            "server_filter": server_filter,
        }, db, u)


@app.get("/optimizer", response_class=HTMLResponse)
async def optimizer_page(request: Request):
    async with AsyncSessionLocal() as db:
        u, _ = await require_jwt_user(request, db)
        if not u:
            return RedirectResponse(url="/", status_code=303)

        exps = (await db.execute(
            select(Expedition).where(Expedition.user_id == u.id)
        )).scalars().all()
        stats = get_user_stats_summary(list(exps))

        return await _template_with_codes(request, "optimizer.html", {
            "user": u,
            "stats": stats,
            "ship_names": list(SHIP_STATS.keys()),
            "active_nav": "optimizer",
        }, db, u)


@app.post("/optimizer/calculate")
async def optimizer_calculate(request: Request, payload: dict = Body(...)):
    async with AsyncSessionLocal() as db:
        u, err = await require_jwt_user(request, db)
        if err:
            return err

        exps = (await db.execute(
            select(Expedition).where(Expedition.user_id == u.id)
        )).scalars().all()
        stats = get_user_stats_summary(list(exps))

        available_ships = {k: int(v) for k, v in (payload.get("ships") or {}).items() if int(v or 0) > 0}
        slots = int(payload.get("slots") or 7)
        max_per_slot = int(payload.get("max_per_slot") or 15_010_000)

        inp = OptimizerInput(
            available_ships=available_ships,
            slots=slots,
            max_ships_per_slot=max_per_slot,
            avg_loot_metal=stats.get("avg_metal") or 163_000_000_000,
            avg_loot_crystal=stats.get("avg_crystal") or 108_000_000_000,
            avg_loot_deut=stats.get("avg_deut") or 55_000_000_000,
            lang=get_lang(request),
        )

        result = optimize_fleet(inp)
        a = result.analysis

        def slot_to_dict(s):
            return {
                "ships": s.ships,
                "total_count": s.total_count,
                "total_cargo": s.total_cargo,
                "total_attack": s.total_attack,
                "total_points": s.total_points,
            }

        return {
            "ok": True,
            "needed_cargo": a["needed_cargo"],
            "avg_total_loot": a["avg_total_loot"],
            "current": {**slot_to_dict(a["current"]["slot"]), **{k:v for k,v in a["current"].items() if k!="slot"}},
            "safe":       {**slot_to_dict(a["safe"]["slot"]),       **{k:v for k,v in a["safe"].items()       if k!="slot"}},
            "balanced":   {**slot_to_dict(a["balanced"]["slot"]),   **{k:v for k,v in a["balanced"].items()   if k!="slot"}},
            "aggressive": {**slot_to_dict(a["aggressive"]["slot"]), **{k:v for k,v in a["aggressive"].items() if k!="slot"}},
            "warnings": result.warnings,
            "suggestions": a.get("suggestions", []),
        }



@app.get("/expeditions/export.csv")
async def export_csv(request: Request):
    async with AsyncSessionLocal() as db:
        u, err = await require_jwt_user(request, db)
        if err:
            return err

        server_filter = request.query_params.get("server") or None
        exps = (await db.execute(
            _server_filter_query(u.id, server_filter).order_by(Expedition.returned_at.desc())
        )).scalars().all()

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "exp_number", "returned_at", "outcome_type",
            "metal", "crystal", "deuterium", "dark_matter",
            "dark_matter_bonus", "dark_matter_bonus_pct",
            "ships_delta", "loss_percent",
            "pirate_strength", "pirate_win_chance", "pirate_loss_rate",
        ])
        for e in exps:
            writer.writerow([
                e.exp_number or "",
                e.returned_at.strftime("%Y-%m-%d %H:%M:%S") if e.returned_at else "",
                e.outcome_type,
                e.metal, e.crystal, e.deuterium, e.dark_matter,
                e.dark_matter_bonus, e.dark_matter_bonus_pct,
                str(e.ships_delta) if e.ships_delta else "",
                e.loss_percent if e.loss_percent is not None else "",
                e.pirate_strength or "", e.pirate_win_chance or "", e.pirate_loss_rate or "",
            ])

        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=expeditions_{u.username}.csv"},
        )


@app.delete("/expeditions/all")
async def delete_all_expeditions(request: Request):
    async with AsyncSessionLocal() as db:
        u, err = await require_jwt_user(request, db)
        if err:
            return err
        await db.execute(delete(Expedition).where(Expedition.user_id == u.id))
        await db.commit()
        return {"ok": True}


@app.get("/outcomes", response_class=HTMLResponse)
async def outcomes_page(request: Request):
    return RedirectResponse(url="/stats", status_code=301)


@app.get("/outcomes_old", response_class=HTMLResponse)
async def outcomes_old(request: Request):
    async with AsyncSessionLocal() as db:
        u, _ = await require_jwt_user(request, db)
        if not u:
            return RedirectResponse(url="/", status_code=303)

        exps = (await db.execute(
            select(Expedition)
            .where(Expedition.user_id == u.id)
            .order_by(Expedition.returned_at.asc())
        )).scalars().all()

        from collections import defaultdict
        import datetime

        total = len(exps)

        # Per outcome: count, resources, ships_lost, ships_gained, dm
        outcomes: dict = defaultdict(lambda: {
            "count": 0, "metal": 0, "crystal": 0, "deut": 0, "dm": 0,
            "ships_lost": {}, "ships_gained": {},
        })

        # Weekly timeline: {week: {outcome_type: count}}
        weekly: dict = defaultdict(lambda: defaultdict(int))

        for e in exps:
            o = e.outcome_type
            outcomes[o]["count"] += 1
            outcomes[o]["metal"]   += e.metal
            outcomes[o]["crystal"] += e.crystal
            outcomes[o]["deut"]    += e.deuterium
            outcomes[o]["dm"]      += e.dark_matter

            if e.ships_delta:
                for ship, qty in e.ships_delta.items():
                    if qty < 0:
                        d = outcomes[o]["ships_lost"]
                        d[ship] = d.get(ship, 0) + abs(qty)
                    elif qty > 0:
                        d = outcomes[o]["ships_gained"]
                        d[ship] = d.get(ship, 0) + qty

            if e.returned_at:
                iso = e.returned_at.isocalendar()
                wk = f"{iso[0]}-W{iso[1]:02d}"
                weekly[wk][o] += 1

        # Last 16 weeks
        today = datetime.date.today()
        timeline = []
        all_outcome_types = list(outcomes.keys())
        for i in range(15, -1, -1):
            d = today - datetime.timedelta(weeks=i)
            iso = d.isocalendar()
            wk = f"{iso[0]}-W{iso[1]:02d}"
            entry = {"week": wk}
            for ot in all_outcome_types:
                entry[ot] = weekly[wk].get(ot, 0)
            timeline.append(entry)

        return await _template_with_codes(request, "outcomes.html", {
            "user": u,
            "active_nav": "outcomes",
            "total": total,
            "outcomes": dict(sorted(outcomes.items(), key=lambda x: -x[1]["count"])),
            "timeline": timeline,
            "outcome_types": all_outcome_types,
        }, db, u)


@app.get("/codes", response_class=HTMLResponse)
async def codes_page(request: Request):
    async with AsyncSessionLocal() as db:
        u, _ = await require_jwt_user(request, db)
        if not u:
            return RedirectResponse(url="/", status_code=303)

        codes = (await db.execute(
            select(SmugglerCode)
            .where(SmugglerCode.user_id == u.id)
            .order_by(SmugglerCode.redeemed, SmugglerCode.found_at.desc())
        )).scalars().all()

        pending = [c for c in codes if not c.redeemed]
        redeemed = [c for c in codes if c.redeemed]

        return await _template_with_codes(request, "codes.html", {
            "title": "Smuggler Codes",
            "active_nav": "codes",
            "pending": pending,
            "redeemed": redeemed,
        }, db, u)


@app.post("/codes/{code_id}/redeem", response_class=JSONResponse)
async def redeem_code(code_id: int, request: Request):
    async with AsyncSessionLocal() as db:
        u, err = await require_jwt_user(request, db)
        if err:
            return err
        sc = (await db.execute(
            select(SmugglerCode).where(SmugglerCode.id == code_id, SmugglerCode.user_id == u.id)
        )).scalar_one_or_none()
        if not sc:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        sc.redeemed = True
        sc.redeemed_at = datetime.utcnow()
        await db.commit()
        return {"ok": True}


@app.post("/codes/{code_id}/unredeem", response_class=JSONResponse)
async def unredeem_code(code_id: int, request: Request):
    async with AsyncSessionLocal() as db:
        u, err = await require_jwt_user(request, db)
        if err:
            return err
        sc = (await db.execute(
            select(SmugglerCode).where(SmugglerCode.id == code_id, SmugglerCode.user_id == u.id)
        )).scalar_one_or_none()
        if not sc:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        sc.redeemed = False
        sc.redeemed_at = None
        await db.commit()
        return {"ok": True}


@app.delete("/codes/{code_id}", response_class=JSONResponse)
async def delete_code(code_id: int, request: Request):
    async with AsyncSessionLocal() as db:
        u, err = await require_jwt_user(request, db)
        if err:
            return err
        sc = (await db.execute(
            select(SmugglerCode).where(SmugglerCode.id == code_id, SmugglerCode.user_id == u.id)
        )).scalar_one_or_none()
        if not sc:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        await db.delete(sc)
        await db.commit()
        return {"ok": True}




def _server_filter_query(user_id: int, server_filter: Optional[str]):
    """Returns a SQLAlchemy select for expeditions filtered by server_id."""
    q = select(Expedition).where(Expedition.user_id == user_id)
    if server_filter:
        q = q.where(Expedition.server_id == server_filter)
    return q


async def _get_user_servers(db, user_id: int) -> list[str]:
    """Returns list of distinct server_ids for a user."""
    rows = (await db.execute(
        text("SELECT DISTINCT server_id FROM expeditions WHERE user_id = :uid AND server_id IS NOT NULL ORDER BY server_id"),
        {"uid": user_id}
    )).fetchall()
    return [r[0] for r in rows]

# ---------------------------------------------------------------------------
# Bridge sync
# ---------------------------------------------------------------------------

BRIDGE_OUTCOME_MAP = {
    "resources":   "success_res",
    "ships":       "success_ships",
    "dark_matter": "success_dm",
    "nothing":     "failed",
    "pirates":     "pirates_win",
    "aliens":      "pirates_win",
    "lost":        "vanished",
}

# Bridge supports these server IDs
KNOWN_SERVERS = ["beta", "uni1"]


def _bridge_request(action: str, params: dict) -> dict:
    """Synchronous Bridge call — run via asyncio.to_thread."""
    qs = urllib.parse.urlencode({
        "action": action,
        "secret": settings.glad_bridge_secret,
        **params,
    })
    url = f"{settings.glad_bridge_url}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "OGX-Expedition/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def _sync_server(db, user_id: int, link_code: str, server_id: str) -> dict:
    """
    Fetch expedition data from bridge for one server and insert into DB.
    Returns {"inserted": int, "skipped": int, "error": str|None}.
    """
    try:
        data = await asyncio.to_thread(
            _bridge_request, "expo",
            {"code": link_code, "server_id": server_id, "limit": 5000}
        )
    except Exception as exc:
        return {"inserted": 0, "skipped": 0, "error": f"bridge_unreachable: {exc}"}

    if not data.get("ok"):
        err = data.get("error", "bridge_error")
        # code_not_found on a server just means user has no account there — not fatal
        if err in ("code_not_found", "no_account"):
            return {"inserted": 0, "skipped": 0, "error": None}
        return {"inserted": 0, "skipped": 0, "error": err}

    expeditions_raw = data.get("expeditions") or []
    inserted = 0
    skipped = 0

    for item in expeditions_raw:
        outcome_type = BRIDGE_OUTCOME_MAP.get(item.get("result_type", "nothing"), "failed")

        try:
            returned_at = datetime.fromisoformat(
                item["date"].replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except Exception:
            returned_at = _utcnow()

        metal       = int(item.get("metal", 0))
        crystal     = int(item.get("crystal", 0))
        deuterium   = int(item.get("deuterium", 0))
        dark_matter = int(item.get("dark_matter", 0))
        ships_raw   = item.get("ships_found")
        ships_delta = ships_raw if isinstance(ships_raw, dict) else None

        # Use same dedup_key format as manual parser so bridge data matches existing imports.
        # Format: sha256(returned_at|outcome_type|metal|crystal)[:32]
        raw_key   = f"{returned_at}|{outcome_type}|{metal}|{crystal}"
        dedup_key = hashlib.sha256(raw_key.encode()).hexdigest()[:32]

        stmt = pg_insert(Expedition).values(
            user_id     = user_id,
            server_id   = server_id,
            returned_at = returned_at,
            imported_at = _utcnow(),
            outcome_type= outcome_type,
            metal       = metal,
            crystal     = crystal,
            deuterium   = deuterium,
            dark_matter = dark_matter,
            ships_delta = ships_delta,
            dedup_key   = dedup_key,
        ).on_conflict_do_nothing(index_elements=["dedup_key"])

        result = await db.execute(stmt)
        if result.rowcount:
            inserted += 1
            try:
                fake_parsed = type("P", (), {
                    "outcome_type": outcome_type,
                    "metal": metal, "crystal": crystal,
                    "deuterium": deuterium, "dark_matter": dark_matter,
                    "dark_matter_bonus": 0, "dark_matter_bonus_pct": 0,
                    "ships_delta": ships_delta, "fleet_sent": None,
                    "loss_percent": None, "pirate_strength": None,
                    "pirate_win_chance": None, "pirate_loss_rate": None,
                    "exp_number": None, "raw_text": None, "dedup_key": dedup_key,
                })()
                await prestige_expo(db, user_id, fake_parsed)
            except Exception:
                pass
        else:
            skipped += 1

    return {"inserted": inserted, "skipped": skipped, "error": None}


@app.post("/api/bridge/sync")
async def bridge_sync(request: Request):
    """
    Pull expedition data from Glad's Bridge for ALL servers and insert.
    Uses link_codes.code directly — does not require linked_accounts row.
    Returns {ok, inserted, skipped, servers: [{server_id, inserted, skipped, error}]}.
    """
    async with AsyncSessionLocal() as db:
        user, err = await require_jwt_user(request, db)
        if err:
            return err

        # Use the most recent link code for this user
        link_row = (await db.execute(
            text("SELECT code FROM link_codes WHERE user_id = :uid ORDER BY id DESC LIMIT 1"),
            {"uid": user.id}
        )).fetchone()

        if not link_row or not link_row[0]:
            return JSONResponse({"ok": False, "error": "no_link_code"}, status_code=400)

        link_code = link_row[0]

        # Optionally sync only a specific server (from request body)
        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass
        target_server = body.get("server_id") if body else None

        servers_to_sync = [target_server] if target_server else KNOWN_SERVERS

        total_inserted = 0
        total_skipped  = 0
        server_results = []

        for srv in servers_to_sync:
            res = await _sync_server(db, int(user.id), link_code, srv)
            server_results.append({"server_id": srv, **res})
            total_inserted += res["inserted"]
            total_skipped  += res["skipped"]

        await db.commit()
        return {
            "ok": True,
            "inserted": total_inserted,
            "skipped":  total_skipped,
            "servers":  server_results,
        }


@app.get("/api/bridge/status")
async def bridge_status(request: Request):
    """
    Returns link code status and which servers have expedition data.
    Does not require linked_accounts — only link_codes.
    """
    async with AsyncSessionLocal() as db:
        user, err = await require_jwt_user(request, db)
        if err:
            return err

        link_row = (await db.execute(
            text("SELECT code FROM link_codes WHERE user_id = :uid ORDER BY id DESC LIMIT 1"),
            {"uid": user.id}
        )).fetchone()

        has_code = bool(link_row and link_row[0])

        server_rows = (await db.execute(
            text("SELECT DISTINCT server_id FROM expeditions WHERE user_id = :uid AND server_id IS NOT NULL"),
            {"uid": user.id}
        )).fetchall()
        servers_with_data = [r[0] for r in server_rows]

        # Still check linked_accounts for game_player_id if available
        linked_row = (await db.execute(
            text("SELECT game_player_id, COALESCE(server_id, 'beta') FROM linked_accounts WHERE user_id = :uid LIMIT 1"),
            {"uid": user.id}
        )).fetchone()

        return {
            "ok": True,
            "has_code": has_code,
            "linked": bool(linked_row),
            "game_player_id": linked_row[0] if linked_row else None,
            "servers_with_data": servers_with_data,
        }


@app.get("/lang/{code}")
async def set_lang(code: str, request: Request):
    code = (code or "").strip().lower()[:2]
    if code not in SUPPORTED:
        code = "en"

    # zurück zur Seite, von der der User kommt
    ref = request.headers.get("referer") or "/"
    resp = RedirectResponse(url=ref, status_code=303)

    resp.set_cookie(
        "ogx_lang",
        code,
        max_age=60 * 60 * 24 * 365,
        path="/",
        samesite="lax",
        secure=True,
    )
    return resp