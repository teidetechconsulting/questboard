from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import json, os, threading, time, uuid

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

# The browser clients POST whole documents and race each other (last write
# wins). The /api/* endpoints below are called by Home Assistant, which has no
# local copy of state to merge, so they must read-modify-write server-side.
# This lock only serialises those; it deliberately does not touch the
# frontend's own POST /state path, so no frontend change is needed.
_LOCK = threading.Lock()


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


def mutate(path, fn):
    with _LOCK:
        data = read_json(path) or {}
        result = fn(data)
        write_json(path, data)
        return result


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


@app.post("/api/bounty")
async def api_bounty(request: Request):
    """Append a bounty. createdBy is left null so the house funds it and every
    player can claim it — see BountyBoard.jsx isCreator/canClaim."""
    body = await request.json()
    if not isinstance(body, dict):
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
    dedupe_key = body.get("dedupeKey") or None

    def apply(state):
        bounties = state.get("bounties") or []
        # An NFC tag tapped twice, or a kid tapping it again because nothing
        # visibly happened, must not post the same quest twice.
        if dedupe_key and any(
            b.get("dedupeKey") == dedupe_key and not b.get("completedAt")
            for b in bounties
        ):
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


@app.post("/api/grant")
async def api_grant(request: Request):
    """Mint gold for a player, so large cash-backed garden bounties can be
    funded without grinding chores first."""
    body = await request.json()
    if not isinstance(body, dict):
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

    def apply(state):
        current = (state.get("gold") or {}).get(player_id, 0)
        new_total = max(0, current + gold)
        state["gold"] = {**(state.get("gold") or {}), player_id: new_total}
        return {"ok": True, "playerId": player_id, "gold": new_total}

    return mutate(STATE_FILE, apply)


@app.post("/api/vacation")
async def api_vacation(request: Request):
    """Toggle away-mode. With enabled=true and no dates, isVacationDay() covers
    every day, so the overnight monster penalty never fires and kill streaks
    freeze instead of resetting."""
    body = await request.json()
    if not isinstance(body, dict):
        return {"ok": False, "error": "invalid"}
    enabled = bool(body.get("enabled"))

    def apply(config):
        vacation = dict(config.get("vacation") or {})
        vacation["enabled"] = enabled
        vacation.setdefault("start", "")
        vacation.setdefault("end", "")
        config["vacation"] = vacation
        return {"ok": True, "vacation": vacation}

    return mutate(CONFIG_FILE, apply)


@app.get("/api/summary")
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

    chores_this_week = {}
    rewards_redeemed = {}
    for entry in history:
        pid = entry.get("playerId")
        if not pid:
            continue
        if entry.get("type") == "chore":
            chores_this_week[pid] = chores_this_week.get(pid, 0) + 1
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
                "choresLogged": chores_this_week.get(p.get("id"), 0),
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
