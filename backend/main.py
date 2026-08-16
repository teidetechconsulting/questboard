from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import json, os
# Second line on purpose: upstream edits the import above, and keeping these
# separate stops `git merge upstream/main` from conflicting on it.
import threading, time, uuid

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_DATA_DIR   = os.environ.get("QUESTBOARD_DATA", "/data")
STATE_FILE  = os.path.join(_DATA_DIR, "state.json")
CONFIG_FILE = os.path.join(_DATA_DIR, "config.json")


def read_json(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return None


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


@app.get("/state")
def get_state():
    return read_json(STATE_FILE) or {}


@app.post("/state")
async def post_state(request: Request):
    data = await request.json()
    if not isinstance(data, dict):
        return {"ok": False, "error": "invalid"}
    write_json(STATE_FILE, data)
    return {"ok": True}


@app.get("/config")
def get_config():
    config = read_json(CONFIG_FILE)
    if config is None:
        return {"needs_setup": True}
    return config


@app.post("/config")
async def post_config(request: Request):
    data = await request.json()
    if not isinstance(data, dict):
        return {"ok": False, "error": "invalid"}
    write_json(CONFIG_FILE, data)
    return {"ok": True}


# ── Home Assistant integration ────────────────────────────────────────────────
# Additive endpoints only. Nothing below is called by the frontend, so upstream
# merges stay conflict-free as long as edits stay under this banner.
#
# Paths are bare, exactly like /state and /config above. nginx proxies
# `location /api/` to `http://127.0.0.1:5050/` and the trailing slash strips the
# prefix, which is why the SPA's fetch('/api/state') lands on /state here.
# Home Assistant calls http://host:8099/api/bounty and it arrives as /bounty.
# Registering these as "/api/bounty" would only be reachable at /api/api/bounty.

# The browser clients POST whole documents and race each other (last write
# wins). The /api/* endpoints below are called by Home Assistant, which has no
# local copy of state to merge, so they must read-modify-write server-side.
# This lock only serialises those; it deliberately does not wrap POST /state.
_LOCK = threading.Lock()


def mutate(path, fn):
    with _LOCK:
        data = read_json(path) or {}
        result = fn(data)
        write_json(path, data)
        return result


def known_players():
    """id -> name, from config. Empty until the setup wizard has run."""
    return {p.get("id"): p.get("name") for p in (read_json(CONFIG_FILE) or {}).get("players") or []}


async def read_body(request):
    # Home Assistant hand-builds this JSON in a template, so an empty payload or
    # an unescaped quote in a chore title is a live failure mode. Unguarded,
    # request.json() raises and Starlette answers 500 -- the one error path that
    # would not match the {"ok": false} shape the caller parses.
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


@app.post("/bounty")
async def api_bounty(request: Request):
    """Append a bounty. createdBy is left null so the house funds it and every
    player can claim it — see BountyBoard.jsx isCreator/canClaim."""
    body = await read_body(request)
    if body is None:
        return {"ok": False, "error": "invalid"}

    title = str(body.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "title required"}

    try:
        gold = int(body.get("gold", 5))
    except (TypeError, ValueError):
        return {"ok": False, "error": "gold must be an integer"}
    if gold < 1:
        return {"ok": False, "error": "gold must be >= 1"}

    icon = str(body.get("icon") or "🔧")
    assigned_to = body.get("assignedTo") or None
    if assigned_to is not None:
        assigned_to = str(assigned_to)
        # An assignee the frontend cannot match is unrecoverable: canClaim fails
        # for everyone, and createdBy is null so nobody sees a cancel button.
        if assigned_to not in known_players():
            return {"ok": False, "error": "unknown assignedTo"}
    dedupe_key = body.get("dedupeKey") or None

    def apply(state):
        bounties = state.get("bounties") or []
        # Match completed bounties too. Otherwise a kid can scan, claim, and
        # rescan the same tag for unlimited house-funded gold.
        if dedupe_key and any(b.get("dedupeKey") == dedupe_key for b in bounties):
            return {"ok": True, "deduped": True}

        now = int(time.time() * 1000)
        bounty = {
            "id": "bounty_ha_%d_%s" % (now, uuid.uuid4().hex[:6]),
            "title": title,
            "icon": icon,
            "gold": gold,
            "createdBy": None,
            "assignedTo": assigned_to,
            "createdAt": now,
            "completedAt": None,
            "completedBy": None,
        }
        if dedupe_key:
            bounty["dedupeKey"] = dedupe_key

        state["bounties"] = bounties + [bounty]
        state["history"] = (state.get("history") or []) + [{
            "type": "bounty_post",
            "player": "Home Assistant",
            "playerId": None,
            "name": title,
            "pts": gold,
            "ts": now,
        }]
        return {"ok": True, "id": bounty["id"]}

    return mutate(STATE_FILE, apply)


@app.post("/grant")
async def api_grant(request: Request):
    """Mint gold for a player, so large cash-backed garden bounties can be
    funded without grinding chores first."""
    body = await read_body(request)
    if body is None:
        return {"ok": False, "error": "invalid"}

    player_id = str(body.get("playerId") or "").strip()
    if not player_id:
        return {"ok": False, "error": "playerId required"}
    try:
        gold = int(body.get("gold", 0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "gold must be an integer"}
    if gold == 0:
        return {"ok": False, "error": "gold must be non-zero"}

    players = known_players()
    if players and player_id not in players:
        return {"ok": False, "error": "unknown playerId"}
    reason = str(body.get("reason") or "bonus").strip()[:48]

    def apply(state):
        current = (state.get("gold") or {}).get(player_id, 0)
        new_total = max(0, current + gold)
        delta = new_total - current
        state["gold"] = {**(state.get("gold") or {}), player_id: new_total}
        # HistoryTab renders 'loot' as "found (+Ng)" and 'penalty' as
        # "attacked by (-Ng)", so this needs no frontend change. Log the
        # realised delta, not the requested one -- new_total is clamped at 0.
        if delta:
            state["history"] = (state.get("history") or []) + [{
                "type": "loot" if delta > 0 else "penalty",
                "player": players.get(player_id) or player_id,
                "playerId": player_id,
                "name": reason,
                "pts": abs(delta),
                "ts": int(time.time() * 1000),
            }]
        return {"ok": True, "playerId": player_id, "gold": new_total}

    return mutate(STATE_FILE, apply)


@app.post("/vacation")
async def api_vacation(request: Request):
    """Toggle away-mode. With enabled=true and no dates, isVacationDay() covers
    every day, so the overnight monster penalty never fires and kill streaks
    freeze instead of resetting."""
    body = await read_body(request)
    if body is None:
        return {"ok": False, "error": "invalid"}

    # GET /config only reports needs_setup when the file is absent, so writing a
    # vacation-only config makes the frontend skip the wizard and then crash on
    # cfg.players. mutate() always writes, so this has to short-circuit above it.
    if not (read_json(CONFIG_FILE) or {}).get("players"):
        return {"ok": False, "error": "not set up"}

    raw = body.get("enabled")
    if isinstance(raw, bool):
        enabled = raw
    elif isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
        enabled = raw.strip().lower() == "true"
    else:
        # Guessing here inverts away-mode, which drains gold nightly and looks
        # like nothing happened. Better to fail the call.
        return {"ok": False, "error": "enabled must be a boolean"}

    def apply(config):
        vacation = dict(config.get("vacation") or {})
        vacation["enabled"] = enabled
        vacation.setdefault("start", "")
        vacation.setdefault("end", "")
        config["vacation"] = vacation
        return {"ok": True, "vacation": vacation}

    return mutate(CONFIG_FILE, apply)


@app.get("/summary")
def api_summary():
    """Small aggregate for Home Assistant to poll. state.json grows without
    bound (history is never trimmed upstream), so HA must never fetch /state."""
    state = read_json(STATE_FILE) or {}
    config = read_json(CONFIG_FILE) or {}

    players = config.get("players") or []
    gold = state.get("gold") or {}
    weekly_gold = state.get("weeklyGold") or {}
    bounties = state.get("bounties") or []
    history = state.get("history") or []

    chores_logged = {}
    rewards_redeemed = {}
    for entry in history:
        pid = entry.get("playerId")
        if not pid:
            continue
        if entry.get("type") == "chore":
            chores_logged[pid] = chores_logged.get(pid, 0) + 1
        elif entry.get("type") == "reward":
            rewards_redeemed[pid] = rewards_redeemed.get(pid, 0) + 1

    return {
        "players": [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "mode": p.get("mode"),
                "gold": gold.get(p.get("id"), 0),
                "weeklyGold": weekly_gold.get(p.get("id"), 0),
                "choresLogged": chores_logged.get(p.get("id"), 0),
                "rewardsRedeemed": rewards_redeemed.get(p.get("id"), 0),
            }
            for p in players
        ],
        "openBounties": [
            {"id": b.get("id"), "title": b.get("title"), "gold": b.get("gold")}
            for b in bounties if not b.get("completedAt")
        ],
        "vacation": bool((config.get("vacation") or {}).get("enabled")),
        "historyEntries": len(history),
    }
