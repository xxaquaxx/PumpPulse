import asyncio, math, random, time
from provider_base import MarketDataProvider

TF = {"15s": 15, "1m": 60, "5m": 300, "15m": 900}
ALPHA = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
NAMES = [("PEPEX", "Pepe Extreme"), ("WIFHAT", "Dog Wif Hat"), ("BONKZ", "Bonkz"),
         ("MOODENG2", "Moo Deng 2"), ("GIGACHAD", "GigaChad"), ("FROGGO", "Froggo"),
         ("SOLCAT", "Sol Cat"), ("RUGGED", "Rugged"), ("PUMPKIN", "Pumpkin"),
         ("NEURAL", "Neural Pepe"), ("TENDIES", "Tendies"), ("ZOOMER", "Zoomer")]


def addr(rng, n=44):
    return "".join(rng.choice(ALPHA) for _ in range(n))


class MockProvider(MarketDataProvider):
    """FAKE data. Replace with a live provider that implements the same 5 methods."""

    def __init__(self):
        r = random.Random(7)
        self.rng = random.Random()
        now = time.time()
        self.tokens = {}
        for sym, name in NAMES:
            m = addr(r)
            price = 10 ** r.uniform(-6, -3.4)
            liq = r.uniform(15e3, 300e3)
            h = r.randint(200, 9000)
            self.tokens[m] = dict(
                m=m, sym=sym, name=name, p=price, p0=price, p24=price * r.uniform(.5, 1.6),
                sup=1e9, liq=liq, liq0=liq, v=r.uniform(2e4, 9e5), h=h, h0=h,
                b=r.randint(100, 4000), s=r.randint(80, 3500), u=r.randint(80, 2000),
                born=now - r.randint(300, 86400 * 3), regime=0.0, act=r.uniform(.3, 2))
        self.wallets = {}
        for i in range(60):
            a = addr(r)
            smart = i < 10
            self.wallets[a] = dict(
                a=a, age=r.randint(3, 700), n=r.randint(20, 900),
                wr=round(r.uniform(55, 82) if smart else r.uniform(20, 60), 1),
                pnl=round(r.uniform(5e3, 3e5) if smart else r.uniform(-3e4, 4e4)),
                hold=r.randint(2, 240), smart=smart, big=round(r.uniform(1e3, 8e4)))
        self.wl = list(self.wallets)
        self.subs = set()
        self.task = None

    async def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop())

    def subscribe(self):
        q = asyncio.Queue(maxsize=40)
        self.subs.add(q)
        return q

    def unsubscribe(self, q):
        self.subs.discard(q)

    def snapshot(self):
        hide = ("regime", "act", "p0")
        return {"tokens": [{k: v for k, v in t.items() if k not in hide}
                           for t in self.tokens.values()],
                "wallets": list(self.wallets.values()), "tfs": list(TF)}

    def history(self, mint, tf):
        t = self.tokens.get(mint)
        sec = TF.get(tf, 60)
        if not t:
            return []
        rr = random.Random(f"{mint}{tf}")
        bucket = int(time.time() // sec * sec)
        price, out = t["p"], []
        k = math.sqrt(sec / 15)
        for i in range(300):
            c = price
            o = c * math.exp(-rr.gauss(0, 0.004 * k))
            hi = max(o, c) * (1 + abs(rr.gauss(0, 0.002 * k)))
            lo = min(o, c) * (1 - abs(rr.gauss(0, 0.002 * k)))
            out.append(dict(t=bucket - i * sec, o=o, h=hi, l=lo, c=c,
                            v=abs(rr.gauss(1, .6)) * 2000 * (sec / 15) * rr.uniform(.3, 3)))
            price = o
        out.reverse()
        return out

    async def _loop(self):
        while True:
            await asyncio.sleep(0.25)
            batch = self._step()
            for q in list(self.subs):
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(batch)

    def _step(self):
        r = self.rng
        now = time.time()
        toks = list(self.tokens.values())
        weights = [t["act"] for t in toks]
        trades = []
        for t in toks:
            if r.random() < 0.01:
                t["regime"] = r.gauss(0, 1)
            if r.random() < 0.0003:
                t["liq"] *= 0.85
            t["p"] *= math.exp(r.gauss(0, 0.0012) + t["regime"] * 0.0003)
            t["p"] *= (t["p0"] / t["p"]) ** 0.002
            t["liq"] = max(5000, t["liq"] * (1 + r.gauss(0, 0.0004)))
        for _ in range(max(1, int(r.expovariate(1 / 6)))):
            t = r.choices(toks, weights)[0]
            buy = r.random() < min(.8, max(.2, .5 + t["regime"] * .12))
            usd = min(150000, max(2, math.exp(r.gauss(4.6, 1.5))))
            if r.random() < .01:
                usd = min(150000, usd * r.uniform(5, 20))
            w = r.choice(self.wl[:10]) if r.random() < .15 else r.choice(self.wl)
            imp = min(0.3, usd / (t["liq"] * 4))
            t["p"] = max(1e-9, t["p"] * ((1 + imp) if buy else (1 - imp)))
            t["v"] += usd
            t["b" if buy else "s"] += 1
            if r.random() < .4:
                t["u"] += 1
            if buy and r.random() < .3:
                t["h"] += 1
            trades.append([t["m"], round(now, 3), "b" if buy else "s", w,
                           usd / t["p"], round(usd, 2), t["p"]])
        return {"t": "b", "ts": now,
                "tk": [[t["m"], t["p"], t["liq"], t["v"], t["h"], t["b"], t["s"], t["u"]]
                       for t in toks],
                "tr": trades}
