import asyncio, json, os, time
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

TOKEN = os.getenv("PUMPPULSE_TOKEN", "change-me")
STATE_FILE = "state.json"
CFG = dict(
    buy_usd=25, max_open=4,
    min_liq=15_000, min_vol_h1=20_000,
    min_age_min=10, max_age_min=360,
    max_m5_pct=40,
    tp_pct=30, sl_pct=15, max_hold_min=45,
    slippage_pct=3,
    daily_loss_limit_usd=100,
)
state = {"paused": False, "positions": {}, "history": [], "seen": [], "log": []}

def log(msg):
    state["log"] = ([f"{time.strftime('%H:%M:%S')} {msg}"] + state["log"])[:50]
    print(msg)

def save():
    json.dump(state, open(STATE_FILE, "w"))

def load():
    try:
        if os.path.exists(STATE_FILE):
            state.update(json.load(open(STATE_FILE)))
    except Exception:
        pass

def day_pnl():
    start = time.time() - (time.time() % 86400)
    return sum(h["pnl_usd"] for h in state["history"] if h["closed"] >= start)

async def jget(c, url):
    """Safe GET that returns parsed JSON or None, and logs why it failed."""
    host = url.split("/")[2]
    try:
        r = await c.get(url, headers={"Accept": "application/json",
                                      "User-Agent": "Mozilla/5.0 PumpPulse"})
        if r.status_code != 200:
            log(f"HTTP {r.status_code} from {host}")
            return None
        if not r.text.strip():
            log(f"EMPTY reply from {host}")
            return None
        return r.json()
    except Exception as e:
        log(f"FETCH FAIL {host}: {type(e).__name__}")
        return None

async def best_pair(c, mint):
    data = await jget(c, f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
    if not isinstance(data, dict):
        return None
    pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0), default=None)

async def rug_ok(c, mint):
    data = await jget(c, f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary")
    if not isinstance(data, dict):
        return False
    return not any(x.get("level") == "danger" for x in data.get("risks") or [])

async def scan(c):
    if state["paused"] or day_pnl() <= -CFG["daily_loss_limit_usd"]:
        return
    profiles = await jget(c, "https://api.dexscreener.com/token-profiles/latest/v1")
    if not isinstance(profiles, list):
        return
    checked = 0
    for prof in profiles:
        if checked >= 15:
            break
        if prof.get("chainId") != "solana":
            continue
        mint = prof.get("tokenAddress")
        if not mint or mint in state["seen"] or mint in state["positions"]:
            continue
        if len(state["positions"]) >= CFG["max_open"]:
            return
        checked += 1
        await asyncio.sleep(0.4)  # be gentle with rate limits
        pair = await best_pair(c, mint)
        if not pair or not pair.get("pairCreatedAt"):
            continue
        age = (time.time() * 1000 - pair["pairCreatedAt"]) / 60000
        if age < CFG["min_age_min"]:
            continue
        state["seen"] = (state["seen"] + [mint])[-2000:]
        liq = (pair.get("liquidity") or {}).get("usd", 0)
        vol = (pair.get("volume") or {}).get("h1", 0)
        m5 = (pair.get("priceChange") or {}).get("m5", 0)
        if not (CFG["min_liq"] <= liq and CFG["min_vol_h1"] <= vol
                and age <= CFG["max_age_min"] and 0 < m5 <= CFG["max_m5_pct"]):
            continue
        if not await rug_ok(c, mint):
            log(f"REJECT {pair['baseToken']['symbol']} (rugcheck)")
            continue
        base_price = float(pair.get("priceUsd") or 0)
        if base_price <= 0:
            continue
        price = base_price * (1 + CFG["slippage_pct"] / 100)
        state["positions"][mint] = dict(
            symbol=pair["baseToken"]["symbol"], entry=price,
            qty=CFG["buy_usd"] / price, cost=CFG["buy_usd"],
            opened=time.time(), last=price, pnl_pct=0.0)
        log(f"PAPER BUY {pair['baseToken']['symbol']} ${CFG['buy_usd']} @ {price:.8f}")

def close(mint, exit_price, reason):
    p = state["positions"].pop(mint)
    value = p["qty"] * exit_price * (1 - CFG["slippage_pct"] / 100)
    pnl = value - p["cost"]
    state["history"].insert(0, dict(symbol=p["symbol"], pnl_usd=round(pnl, 2),
        pnl_pct=round(pnl / p["cost"] * 100, 1), reason=reason, closed=time.time()))
    state["history"] = state["history"][:200]
    log(f"SELL {p['symbol']} {reason} pnl ${pnl:.2f}")

async def monitor(c):
    for mint, p in list(state["positions"].items()):
        pair = await best_pair(c, mint)
        if pair and pair.get("priceUsd"):
            p["last"] = float(pair["priceUsd"])
        net = p["qty"] * p["last"] * (1 - CFG["slippage_pct"] / 100)
        p["pnl_pct"] = round((net - p["cost"]) / p["cost"] * 100, 1)
        held = (time.time() - p["opened"]) / 60
        if p["pnl_pct"] >= CFG["tp_pct"]:
            close(mint, p["last"], "take-profit")
        elif p["pnl_pct"] <= -CFG["sl_pct"]:
            close(mint, p["last"], "stop-loss")
        elif held >= CFG["max_hold_min"]:
            close(mint, p["last"], "time-stop")
        await asyncio.sleep(0.3)

async def runner():
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        n = 0
        while True:
            try:
                await monitor(c)
            except Exception as e:
                log(f"ERR monitor: {type(e).__name__} {e}")
            if n % 6 == 0:
                try:
                    await scan(c)
                except Exception as e:
                    log(f"ERR scan: {type(e).__name__} {e}")
            try:
                save()
            except Exception:
                pass
            n += 1
            await asyncio.sleep(10)

@asynccontextmanager
async def lifespan(app):
    load()
    t = asyncio.create_task(runner())
    yield
    t.cancel()

app = FastAPI(lifespan=lifespan)

def auth(x_token):
    if x_token != TOKEN:
        raise HTTPException(401)

@app.get("/api/state")
def get_state(x_token: str = Header(None)):
    auth(x_token)
    return {**{k: state[k] for k in ("paused", "positions", "history", "log")},
            "day_pnl": round(day_pnl(), 2),
            "total_pnl": round(sum(h["pnl_usd"] for h in state["history"]), 2),
            "mode": "PAPER"}

@app.post("/api/pause")
def pause(x_token: str = Header(None)):
    auth(x_token); state["paused"] = True; return {"ok": True}

@app.post("/api/resume")
def resume(x_token: str = Header(None)):
    auth(x_token); state["paused"] = False; return {"ok": True}

@app.post("/api/close/{mint}")
def manual_close(mint: str, x_token: str = Header(None)):
    auth(x_token)
    if mint in state["positions"]:
        close(mint, state["positions"][mint]["last"], "manual")
    return {"ok": True}

PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta name=apple-mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-title content=PumpPulse>
<title>PumpPulse</title><style>
body{background:#0b0f14;color:#e6edf3;font-family:-apple-system,sans-serif;margin:0;padding:16px}
h1{margin:0 0 4px}.tag{color:#f0b429;font-size:12px}
.card{background:#151b23;border-radius:12px;padding:12px;margin:10px 0}
.g{color:#3fb950}.r{color:#f85149}.row{display:flex;justify-content:space-between;margin:4px 0}
button{background:#238636;color:#fff;border:0;border-radius:8px;padding:10px 14px;font-size:15px;margin-right:6px}
button.stop{background:#da3633}small{color:#8b949e}
</style></head><body>
<h1>⚡ PumpPulse</h1><div class=tag id=mode></div>
<div class=card><div class=row><span>Today</span><b id=day></b></div>
<div class=row><span>Total</span><b id=tot></b></div>
<button id=pb onclick=tg()>…</button></div>
<h3>Open</h3><div id=pos></div><h3>History</h3><div id=hist></div>
<h3>Log</h3><small id=log></small>
<script>
let T=localStorage.t||(localStorage.t=prompt("Access token")),paused=false;
const H={"X-Token":T},cl=v=>v>=0?"g":"r";
async function load(){const r=await fetch("/api/state",{headers:H});if(r.status==401){localStorage.removeItem("t");location.reload()}
const s=await r.json();paused=s.paused;mode.textContent=s.mode+" MODE"+(paused?" • PAUSED":"");
day.textContent="$"+s.day_pnl;day.className=cl(s.day_pnl);tot.textContent="$"+s.total_pnl;tot.className=cl(s.total_pnl);
pb.textContent=paused?"Resume":"Pause";pb.className=paused?"":"stop";
pos.innerHTML=Object.entries(s.positions).map(([m,p])=>`<div class=card><div class=row><b>${p.symbol}</b><b class=${cl(p.pnl_pct)}>${p.pnl_pct}%</b></div><button class=stop onclick="cls('${m}')">Sell now</button></div>`).join("")||"<small>None</small>";
hist.innerHTML=s.history.slice(0,15).map(h=>`<div class=row><span>${h.symbol} <small>${h.reason}</small></span><span class=${cl(h.pnl_usd)}>$${h.pnl_usd} (${h.pnl_pct}%)</span></div>`).join("");
log.innerHTML=s.log.slice(0,10).join("<br>")}
async function tg(){await fetch(paused?"/api/resume":"/api/pause",{method:"POST",headers:H});load()}
async function cls(m){await fetch("/api/close/"+m,{method:"POST",headers:H});load()}
load();setInterval(load,5000)
</script></body></html>"""

@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE
