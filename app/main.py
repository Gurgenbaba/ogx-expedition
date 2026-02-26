# app/main.py
from __future__ import annotations

import csv
import io
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
from .i18n import get_lang, make_translator, get_translations_js, SUPPORTED, FLAG, LABEL
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
    base = {"request": request}
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

@app.post("/set-lang")
async def set_lang(request: Request):
    """Set language preference cookie and redirect back."""
    from fastapi.responses import Response as FR
    payload = await request.json()
    lang = str(payload.get("lang", "en"))
    if lang not in ("en", "de", "fr"):
        lang = "en"
    resp = JSONResponse({"ok": True, "lang": lang})
    resp.set_cookie("ogx_lang", lang, max_age=60 * 60 * 24 * 365, path="/", samesite="lax")
    return resp


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

        exps = (await db.execute(
            select(Expedition).where(Expedition.user_id == u.id).order_by(Expedition.returned_at.desc()).limit(500)
        )).scalars().all()

        stats = get_user_stats_summary(list(exps))

        # Outcome distribution for chart
        outcome_counts: dict[str, int] = {}
        for e in exps:
            outcome_counts[e.outcome_type] = outcome_counts.get(e.outcome_type, 0) + 1

        # Resources over time (last 50)
        recent = [e for e in exps[:50] if e.metal > 0]

        return await _template_with_codes(request, "dashboard.html", {
            "user": u,
            "stats": stats,
            "outcome_counts": outcome_counts,
            "recent": recent,
            "total": len(exps),
            "active_nav": "dashboard",
        }, db, u)


@app.get("/import", response_class=HTMLResponse)
async def import_page(request: Request):
    async with AsyncSessionLocal() as db:
        u, _ = await require_jwt_user(request, db)
        if not u:
            return RedirectResponse(url="/", status_code=303)
        return await _template_with_codes(request, "import.html", {"user": u, "active_nav": "import"}, db, u)


@app.post("/import")
async def do_import(request: Request, raw_text: str = Form(...)):
    async with AsyncSessionLocal() as db:
        u, err = await require_jwt_user(request, db)
        if err:
            return err

        if len(raw_text.encode()) > settings.max_paste_bytes:
            return JSONResponse({"ok": False, "error": "paste_too_large"}, status_code=413)

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
                raw_text=p.raw_text[:2000] if p.raw_text else None,
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
                    code=p.smuggler_code,
                    tier=p.smuggler_tier,
                    found_at=p.returned_at,
                )
                if IS_POSTGRES:
                    sc_stmt = pg_insert(SmugglerCode).values(**sc_row)
                    sc_stmt = sc_stmt.on_conflict_do_nothing(
                        constraint="ix_smuggler_user_code"
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

    return RedirectResponse(
        url=f"/import?imported={count_new}&duplicates={count_dup}&failed={count_fail}",
        status_code=303,
    )


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    async with AsyncSessionLocal() as db:
        u, _ = await require_jwt_user(request, db)
        if not u:
            return RedirectResponse(url="/", status_code=303)

        exps = (await db.execute(
            select(Expedition).where(Expedition.user_id == u.id).order_by(Expedition.returned_at.desc())
        )).scalars().all()

        stats = get_user_stats_summary(list(exps))

        # By outcome type
        by_type: dict[str, dict] = {}
        for e in exps:
            t = e.outcome_type
            if t not in by_type:
                by_type[t] = {"count": 0, "metal": 0, "crystal": 0, "deut": 0, "dm": 0, "gt_lost": 0}
            by_type[t]["count"] += 1
            by_type[t]["metal"] += e.metal
            by_type[t]["crystal"] += e.crystal
            by_type[t]["deut"] += e.deuterium
            by_type[t]["dm"] += e.dark_matter
            if e.ships_delta:
                by_type[t]["gt_lost"] += abs(e.ships_delta.get("Großer Transporter", 0))

        # Ships gained (across all expeditions)
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

        return await _template_with_codes(request, "stats.html", {
            "user": u,
            "stats": stats,
            "by_type": by_type,
            "ships_gained": dict(sorted(ships_gained.items(), key=lambda x: -x[1])),
            "ships_lost": dict(sorted(ships_lost.items(), key=lambda x: -x[1])),
            "total": len(exps),
            "active_nav": "stats",
        }, db, u)


@app.get("/dm", response_class=HTMLResponse)
async def dm_page(request: Request):
    async with AsyncSessionLocal() as db:
        u, _ = await require_jwt_user(request, db)
        if not u:
            return RedirectResponse(url="/", status_code=303)

        exps = (await db.execute(
            select(Expedition)
            .where(Expedition.user_id == u.id)
            .where(Expedition.dark_matter > 0)
            .order_by(Expedition.returned_at.asc())
        )).scalars().all()

        all_exps_count = (await db.execute(
            select(func.count(Expedition.id)).where(Expedition.user_id == u.id)
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

        exps = (await db.execute(
            select(Expedition).where(Expedition.user_id == u.id)
            .order_by(Expedition.returned_at.desc())
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
