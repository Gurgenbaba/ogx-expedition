# app/main.py
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Form, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sqlalchemy import select, func, text, delete
from sqlalchemy.exc import IntegrityError

from .db import engine, AsyncSessionLocal, IS_SQLITE, IS_POSTGRES
from .models import Base, User, Expedition, ExpeditionImport
from .settings import settings
from .security import (
    require_jwt_user,
    hash_password,
    verify_password,
    create_access_token,
)
from .parser import parse_expedition_text
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
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

        return _template(request, "dashboard.html", {
            "user": u,
            "stats": stats,
            "outcome_counts": outcome_counts,
            "recent": recent,
            "total": len(exps),
            "active_nav": "dashboard",
        })


@app.get("/import", response_class=HTMLResponse)
async def import_page(request: Request):
    async with AsyncSessionLocal() as db:
        u, _ = await require_jwt_user(request, db)
        if not u:
            return RedirectResponse(url="/", status_code=303)
        return _template(request, "import.html", {"user": u, "active_nav": "import"})


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

        count_new = 0
        count_dup = 0
        count_fail = 0

        for p in parsed[:settings.max_expeditions_per_import]:
            if p.parse_error:
                count_fail += 1
                continue

            exp = Expedition(
                user_id=u.id,
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
            try:
                db.add(exp)
                await db.flush()
                count_new += 1
            except IntegrityError:
                await db.rollback()
                count_dup += 1
                continue

        imp = ExpeditionImport(
            user_id=u.id,
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

        return _template(request, "stats.html", {
            "user": u,
            "stats": stats,
            "by_type": by_type,
            "ships_gained": dict(sorted(ships_gained.items(), key=lambda x: -x[1])),
            "ships_lost": dict(sorted(ships_lost.items(), key=lambda x: -x[1])),
            "total": len(exps),
            "active_nav": "stats",
        })


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

        return _template(request, "optimizer.html", {
            "user": u,
            "stats": stats,
            "ship_names": list(SHIP_STATS.keys()),
            "active_nav": "optimizer",
        })


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
        slot = result.recommended_slots[0] if result.recommended_slots else None

        return {
            "ok": True,
            "slot_composition": slot.ships if slot else {},
            "slot_cargo": slot.total_cargo if slot else 0,
            "slot_count": slot.total_count if slot else 0,
            "slot_attack": slot.total_attack if slot else 0,
            "analysis": result.analysis,
            "warnings": result.warnings,
        }


@app.delete("/expeditions/all")
async def delete_all_expeditions(request: Request):
    async with AsyncSessionLocal() as db:
        u, err = await require_jwt_user(request, db)
        if err:
            return err
        await db.execute(delete(Expedition).where(Expedition.user_id == u.id))
        await db.commit()
        return {"ok": True}
