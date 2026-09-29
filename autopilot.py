import asyncio, base64, datetime, json, math, os, time
import httpx
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse

try:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    from solders.message import to_bytes_versioned
except Exception:
    Keypair = None

TOKEN = os.getenv("PUMPPULSE_TOKEN", "change-me")
RPC = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
ALLOW_LIVE = os.getenv("ALLOW_LIVE", "").lower() == "yes"
JUP = "https://lite-api.jup.ag/swap/v1"
GECKO = "https://api.geckoterminal.com/api/v2/networks/solana"
WSOL = "So11111111111111111111111111111111111111112"
TOKEN_PROG = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN22_PROG = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
LAM = 1_000_000_000
HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(HERE, "auto_state.json")

DEFAULTS = dict(size_sol=0.05, max_open=3, tp_pct=35, sl_pct=15, trail_arm_pct=15,
                trail_pct=12, max_hold_min=30, min_score=65, slippage_bps=1200,
                daily_loss_sol=0.15, min_liq_usd=15000, min_age_min=10, max_age_min=720,
                min_roundtrip=0.80, priority_lamports=500000, reserve_sol=0.03)
# hard limits the phone UI cannot exceed
CAPS = dict(size_sol=(0.01, 0.25), max_open=(1, 5), tp_pct=(5, 500), sl_pct=(3, 50),
            trail_arm_pct=(3, 500), trail_pct=(3, 50), max_hold_min=(2, 240),
            min_score=(40, 95), slippage_bps=(50, 3000), daily_loss_sol=(0.01, 1.0),
            min_liq_usd=(5000, 1e7), min_age_min=(1, 600), max_age_min=(10, 10000),
            min_roundtrip=(0.5, 0.99), priority_lamports=(1000, 2_000_000),
            reserve_sol=(0.01, 1.0))
INTS = {"max_open", "max_hold_min", "slippage_bps", "priority_lamports",
        "min_age_min", "max_age_min"}

ST = {"mode": "off", "cfg": dict(DEFAULTS), "pos": {}, "hist": [], "seen": {},
      "log": [], "sol": None}
KP = None
PUB = None
C = None
LOCK = None


def log(msg):
    ST["log"] = ([f"{time.strftime('%H:%M:%S')} {msg}"] + ST["log"])[:80]
    print("[auto]", msg)


def save():
    try:
        json.dump({k: ST[k] for k in ("mode", "cfg", "pos", "hist", "seen", "log")},
                  open(FILE, "w"))
    except Exception:
        pass


def load_state():
    try:
        if os.path.exists(FILE):
            d = json.load(open(FILE))
            ST["cfg"].update(d.get("cfg", {}))
            for k in ("mode", "pos", "hist", "seen", "log"):
                ST[k] = d.get(k, ST[k])
    except Exception:
        pass


def init_key():
    global KP, PUB
    s = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    if not s or Keypair is None:
        return
    try:
        KP = (Keypair.from_bytes(bytes(json.loads(s))) if s.startswith("[")
              else Keypair.from_base58_string(s))
        PUB = str(KP.pubkey())
    except Exception as e:
        log(f"KEY ERROR {type(e).__name__}")


def live_ready():
    return KP is not None and ALLOW_LIVE


def day_pnl():
    start = time.time() - (time.time() % 86400)
    return sum(h["pnl_sol"] for h in ST["hist"] if h["closed"] >= start)


# ---------------- HTTP / RPC ----------------
async def jget(url, params=None, quiet=False):
    host = url.split("/")[2]
    try:
        r = await C.get(url, params=params)
        if r.status_code != 200:
            if not quiet:
                log(f"HTTP {r.status_code} from {host}")
            return None
        return r.json()
    except Exception as e:
        if not quiet:
            log(f"FETCH FAIL {host}: {type(e).__name__}")
        return None


async def rpc(method, params):
    r = await C.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    j = r.json()
    if "error" in j:
        raise RuntimeError(str(j["error"])[:200])
    return j["result"]


async def sol_balance():
    return (await rpc("getBalance", [PUB, {"commitment": "confirmed"}]))["value"]


async def token_bal(mint):
    res = await rpc("getTokenAccountsByOwner",
                    [PUB, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
               for a in res["value"])


async def quote(inp, out, amount, slip):
    return await jget(f"{JUP}/quote", {
        "inputMint": inp, "outputMint": out, "amount": str(int(amount)),
        "slippageBps": int(slip), "restrictIntermediateTokens": "true"}, quiet=True)


async def swap_live(q):
    """Build, sign (with the bot wallet) and send a Jupiter swap. Raises on failure."""
    r = await C.post(f"{JUP}/swap", json={
        "quoteResponse": q, "userPublicKey": PUB, "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
            "maxLamports": ST["cfg"]["priority_lamports"], "priorityLevel": "high"}}})
    if r.status_code != 200:
        raise RuntimeError(f"swap api {r.status_code} {r.text[:100]}")
    tx = VersionedTransaction.from_bytes(base64.b64decode(r.json()["swapTransaction"]))
    sig = KP.sign_message(to_bytes_versioned(tx.message))
    signed = VersionedTransaction.populate(tx.message, [sig])
    txid = await rpc("sendTransaction", [
        base64.b64encode(bytes(signed)).decode(),
        {"encoding": "base64", "skipPreflight": False, "maxRetries": 3,
         "preflightCommitment": "confirmed"}])
    for _ in range(40):
        await asyncio.sleep(1.5)
        st = await rpc("getSignatureStatuses", [[txid], {"searchTransactionHistory": False}])
        v = st["value"][0]
        if v:
            if v.get("err"):
                raise RuntimeError(f"tx failed {v['err']}")
            if v.get("confirmationStatus") in ("confirmed", "finalized"):
                return txid
    raise RuntimeError("confirmation timeout")


# ---------------- signal engine ("AI" score) ----------------
def cl(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def age_min(a):
    try:
        t = datetime.datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00"))
        return (time.time() - t.timestamp()) / 60
    except Exception:
        return None


def score(a, cfg):
    """Returns (score 0-100, info). Score 0 = failed a hard filter."""
    def f(d, k):
        return float((d or {}).get(k) or 0)
    t5 = (a.get("transactions") or {}).get("m5") or {}
    b5, s5 = t5.get("buys", 0) or 0, t5.get("sells", 0) or 0
    buyers = t5.get("buyers")
    v = a.get("volume_usd") or {}
    v5, v1 = f(v, "m5"), f(v, "h1")
    pc = a.get("price_change_percentage") or {}
    c5, c1 = f(pc, "m5"), f(pc, "h1")
    liq = float(a.get("reserve_in_usd") or 0)
    ag = age_min(a)
    if ag is None or not (cfg["min_age_min"] <= ag <= cfg["max_age_min"]):
        return 0, "age"
    if liq < cfg["min_liq_usd"]:
        return 0, "liq"
    if b5 + s5 < 15:
        return 0, "thin"
    if not (1 <= c5 <= 40) or c1 > 400:
        return 0, "move"
    br = b5 / (b5 + s5)
    acc = (v5 * 12) / max(v1, 1)
    pts = cl((br - .5) / .3) * 30 + cl((acc - .8) / 1.2) * 25
    pts += min(1, c5 / 8) * 20 if c5 <= 20 else max(0, 1 - (c5 - 20) / 20) * 20
    pts += cl(math.log10(liq / cfg["min_liq_usd"])) * 10
    pts += 10 if 0 < c1 <= 150 else 4 if -10 < c1 <= 0 else 0
    pts += cl((buyers or 10) / 20) * 5
    return pts, f"br={br:.2f} acc={acc:.1f} m5={c5:.0f}% h1={c1:.0f}% liq=${liq/1e3:.0f}K age={ag:.0f}m"


async def rug_ok(mint):
    d = await jget(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary", quiet=True)
    if not isinstance(d, dict):
        return False
    return not any(x.get("level") == "danger" for x in d.get("risks") or [])


async def candidates():
    out = {}
    for path, params in (("trending_pools", {"page": 1, "duration": "1h"}),
                         ("new_pools", {"page": 1})):
        d = await jget(f"{GECKO}/{path}", params)
        for p in (d or {}).get("data", []) or []:
            out[p.get("id")] = p
        await asyncio.sleep(1)
    return list(out.values())


# ---------------- trading ----------------
async def open_position(mint, sym, sc):
    async with LOCK:
        cfg = ST["cfg"]
        live = ST["mode"] == "live"
        lam = int(cfg["size_sol"] * LAM)
        if live:
            bal = await sol_balance()
            ST["sol"] = bal / LAM
            if bal < lam + cfg["reserve_sol"] * LAM:
                log("SKIP: SOL balance too low (need trade size + reserve)")
                return
        q = await quote(WSOL, mint, lam, cfg["slippage_bps"])
        if not q:
            log(f"SKIP {sym}: no buy route")
            return
        out = int(q["outAmount"])
        qs = await quote(mint, WSOL, out, cfg["slippage_bps"])
        if not qs:
            log(f"SKIP {sym}: no sell route (possible honeypot)")
            return
        rt = int(qs["outAmount"]) / lam
        if rt < cfg["min_roundtrip"]:
            log(f"SKIP {sym}: round-trip only {rt:.2f}")
            return
        tokens = out
        if live:
            try:
                await swap_live(q)
                await asyncio.sleep(2)
                tokens = await token_bal(mint) or out
            except Exception as e:
                log(f"BUY FAIL {sym}: {e}")
                return
        ST["pos"][mint] = dict(mint=mint, sym=sym, cost=cfg["size_sol"], tokens=tokens,
                               opened=time.time(), peak=0.0, value=rt * cfg["size_sol"],
                               pnl_pct=(rt - 1) * 100, score=round(sc), mode=ST["mode"],
                               fails=0)
        log(f"{'LIVE' if live else 'PAPER'} BUY {sym} {cfg['size_sol']} SOL (score {sc:.0f}, rt {rt:.2f})")
        save()


async def close_position(mint, reason):
    async with LOCK:
        p = ST["pos"].get(mint)
        if not p:
            return
        live = p["mode"] == "live"
        slip = min(3000, ST["cfg"]["slippage_bps"] * 2)
        if live:
            try:
                amt = await token_bal(mint)
                if amt <= 0:
                    proceeds = 0.0
                else:
                    q = await quote(mint, WSOL, amt, slip)
                    if not q:
                        raise RuntimeError("no sell route")
                    before = await sol_balance()
                    await swap_live(q)
                    await asyncio.sleep(2)
                    proceeds = (await sol_balance() - before) / LAM
            except Exception as e:
                p["sellfail"] = p.get("sellfail", 0) + 1
                log(f"SELL FAIL {p['sym']} ({p['sellfail']}): {e}")
                if p["sellfail"] >= 4:
                    p["stuck"] = True
                    log(f"{p['sym']} marked STUCK. Sell it manually in Phantom/Jupiter, then Forget it.")
                return
        else:
            q = await quote(mint, WSOL, p["tokens"], slip)
            proceeds = int(q["outAmount"]) / LAM if q else 0.0
        pnl = proceeds - p["cost"]
        ST["hist"].insert(0, dict(sym=p["sym"], mint=mint, cost=p["cost"],
                                  proceeds=round(proceeds, 5), pnl_sol=round(pnl, 5),
                                  pnl_pct=round(pnl / p["cost"] * 100, 1), reason=reason,
                                  mode=p["mode"], opened=p["opened"], closed=time.time()))
        ST["hist"] = ST["hist"][:300]
        del ST["pos"][mint]
        log(f"SELL {p['sym']} [{reason}] {pnl:+.4f} SOL ({pnl / p['cost'] * 100:+.1f}%)")
        save()


async def monitor_once():
    cfg = ST["cfg"]
    for mint, p in list(ST["pos"].items()):
        if p.get("stuck"):
            continue
        q = await quote(mint, WSOL, p["tokens"], cfg["slippage_bps"])
        if not q:
            p["fails"] = p.get("fails", 0) + 1
            if p["fails"] >= 4:
                await close_position(mint, "no-route")
            continue
        p["fails"] = 0
        val = int(q["outAmount"]) / LAM
        p["value"] = val
        p["pnl_pct"] = (val / p["cost"] - 1) * 100
        p["peak"] = max(p["peak"], p["pnl_pct"])
        held = (time.time() - p["opened"]) / 60
        reason = None
        if p["pnl_pct"] >= cfg["tp_pct"]:
            reason = "take-profit"
        elif p["pnl_pct"] <= -cfg["sl_pct"]:
            reason = "stop-loss"
        elif p["peak"] >= cfg["trail_arm_pct"] and p["peak"] - p["pnl_pct"] >= cfg["trail_pct"]:
            reason = "trailing-stop"
        elif held >= cfg["max_hold_min"]:
            reason = "time-stop"
        if reason:
            await close_position(mint, reason)
        await asyncio.sleep(0.5)


async def scan_once():
    cfg = ST["cfg"]
    if ST["mode"] == "off" or len(ST["pos"]) >= cfg["max_open"]:
        return
    if day_pnl() <= -cfg["daily_loss_sol"]:
        return
    ranked = []
    for p in await candidates():
        a = p.get("attributes") or {}
        name = a.get("name", "")
        try:
            mint = p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
        except Exception:
            continue
        if not name.endswith("/ SOL") or mint in ST["pos"]:
            continue
        if time.time() - ST["seen"].get(mint, 0) < 3600:
            continue
        sc, info = score(a, cfg)
        if sc > 0:
            ranked.append((sc, info, mint, name.split(" / ")[0]))
    ranked.sort(reverse=True)
    if not ranked:
        return
    sc, info, mint, sym = ranked[0]
    if sc < cfg["min_score"]:
        log(f"best candidate {sym} scored {sc:.0f} (< {cfg['min_score']})")
        return
    ST["seen"][mint] = time.time()
    if not await rug_ok(mint):
        log(f"REJECT {sym}: rugcheck")
        return
    log(f"SIGNAL {sym} score {sc:.0f} | {info}")
    await open_position(mint, sym, sc)


async def reconcile():
    """On restart, adopt tokens already in the bot wallet so they still get managed."""
    if not KP:
        return
    for prog in (TOKEN_PROG, TOKEN22_PROG):
        try:
            res = await rpc("getTokenAccountsByOwner",
                            [PUB, {"programId": prog}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        except Exception as e:
            log(f"reconcile error: {e}")
            continue
        for a in res["value"]:
            info = a["account"]["data"]["parsed"]["info"]
            mint, amt = info["mint"], int(info["tokenAmount"]["amount"])
            if amt <= 0 or mint in ST["pos"]:
                continue
            q = await quote(mint, WSOL, amt, ST["cfg"]["slippage_bps"])
            val = int(q["outAmount"]) / LAM if q else 0
            if val < 0.002:
                continue
            ST["pos"][mint] = dict(mint=mint, sym=mint[:5], cost=val, tokens=amt,
                                   opened=time.time(), peak=0.0, value=val, pnl_pct=0.0,
                                   score=0, mode="live", fails=0, adopted=True)
            log(f"ADOPTED {mint[:6]}… worth {val:.3f} SOL")


async def _main():
    await asyncio.sleep(3)
    if ST["mode"] == "live":
        ST["mode"] = "off"
        log("Restarted: live mode was switched OFF for safety. Re-enable it from the app.")
    if KP:
        try:
            await reconcile()
        except Exception as e:
            log(f"reconcile failed: {e}")
    last_scan = last_bal = 0
    while True:
        try:
            now = time.time()
            if ST["pos"]:
                await monitor_once()
            if ST["mode"] != "off" and now - last_scan >= 20:
                last_scan = now
                await scan_once()
            if KP and now - last_bal >= 30:
                last_bal = now
                ST["sol"] = (await sol_balance()) / LAM
            save()
        except Exception as e:
            log(f"ERR {type(e).__name__}: {e}")
        await asyncio.sleep(8)


def start():
    global C, LOCK
    C = httpx.AsyncClient(timeout=20, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 PumpPulse", "Accept": "application/json"})
    LOCK = asyncio.Lock()
    load_state()
    init_key()
    asyncio.create_task(_main())


# ---------------- API ----------------
router = APIRouter()


def auth(t):
    if t != TOKEN:
        raise HTTPException(401)


@router.get("/auto")
def auto_page():
    return FileResponse(os.path.join(HERE, "autopilot.html"))


@router.get("/api/auto/state")
def api_state(x_token: str = Header(None)):
    auth(x_token)
    h = ST["hist"]
    return dict(mode=ST["mode"], wallet=PUB, sol=ST["sol"], live_ready=live_ready(),
                cfg=ST["cfg"], pos=list(ST["pos"].values()), hist=h[:30], log=ST["log"][:40],
                day_pnl=round(day_pnl(), 4),
                total_pnl=round(sum(x["pnl_sol"] for x in h), 4),
                halted=day_pnl() <= -ST["cfg"]["daily_loss_sol"],
                trades=len(h), wins=sum(1 for x in h if x["pnl_sol"] > 0))


@router.post("/api/auto/mode")
def api_mode(body: dict, x_token: str = Header(None)):
    auth(x_token)
    m = body.get("mode")
    if m not in ("off", "paper", "live"):
        return {"ok": False, "error": "bad mode"}
    if m == "live":
        if not KP:
            return {"ok": False, "error": "No wallet key configured (SOLANA_PRIVATE_KEY)"}
        if not ALLOW_LIVE:
            return {"ok": False, "error": "Set ALLOW_LIVE=yes in Render environment first"}
        if body.get("confirm") != "GO LIVE":
            return {"ok": False, "error": "You must type GO LIVE"}
    ST["mode"] = m
    log(f"MODE -> {m.upper()}")
    save()
    return {"ok": True}


@router.post("/api/auto/cfg")
def api_cfg(body: dict, x_token: str = Header(None)):
    auth(x_token)
    for k, v in body.items():
        if k not in DEFAULTS:
            continue
        try:
            v = float(v)
        except Exception:
            continue
        lo, hi = CAPS[k]
        v = max(lo, min(hi, v))
        ST["cfg"][k] = int(v) if k in INTS else v
    save()
    return {"ok": True}


@router.post("/api/auto/close/{mint}")
async def api_close(mint: str, x_token: str = Header(None)):
    auth(x_token)
    await close_position(mint, "manual")
    return {"ok": True}


@router.post("/api/auto/forget/{mint}")
def api_forget(mint: str, x_token: str = Header(None)):
    auth(x_token)
    ST["pos"].pop(mint, None)
    save()
    return {"ok": True}


@router.post("/api/auto/panic")
async def api_panic(x_token: str = Header(None)):
    auth(x_token)
    ST["mode"] = "off"
    log("PANIC: mode OFF, selling everything")
    for mint in list(ST["pos"]):
        await close_position(mint, "PANIC")
    save()
    return {"ok": True}
