import os, json, time, hmac, hashlib, urllib.parse
os.environ.update(BOT_TOKEN="123:TEST", ADMIN_IDS="7")  # нужен DATABASE_URL (тестовая ветка Neon!)
import server as S
def init(uid, un):
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "username": un})}
    chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    key = hmac.new(b"WebAppData", b"123:TEST", hashlib.sha256).digest()
    d["hash"] = hmac.new(key, chk.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(d)
c = S.app.test_client()
def P(p, b=None, u=(7, "adm")): return c.post(p, json=b or {}, headers={"X-Init": init(*u)}).get_json()
assert P("/api/me", u=(1, "x")) and c.post("/api/me", headers={"X-Init": "bad"}).get_json()["err"]
assert P("/api/me")["bal"] == 0 and P("/api/me")["admin"]
assert "err" in P("/api/coin", {"bet": 10, "side": 0})            # нет баланса
S.add(7, 1000, "test")
for g, b in [("up", {"bet": 10, "mu": 2}), ("coin", {"bet": 10, "side": 1}), ("wheel", {"bet": 10})]:
    assert "err" not in P("/api/" + g, b), g
P("/api/promo/new", {"code": "ab", "s": 50, "max": 1}); assert P("/api/promo", {"code": "AB"})["s"] == 50
assert "err" in P("/api/promo", {"code": "AB"})
assert "err" in P("/api/promo/new", {"code": "z", "s": 5, "max": 1}, u=(8, "u"))
r = P("/api/mines/start", {"bet": 10, "M": 3}); assert "err" not in r
assert P("/api/mines/state")["on"] == 1
g = S.gget(7); safe = [i for i in range(25) if i not in g["m"]][0]
assert "err" not in P("/api/mines/open", {"i": safe}); assert P("/api/mines/cash")["w"] >= 10
r = P("/api/crash"); assert r["ph"] == "wait" and r["me"] is None
assert "err" not in P("/api/crash/bet", {"bet": 20}); assert P("/api/crash")["me"]["b"] == 20
assert "err" not in P("/api/crash/cancel")
S.CR["tl"] = time.time() * 1000 - 100; S.CR["tb"] = S.CR["tl"] + 5000; S.CR["cp"] = 5.0   # форс-полёт
assert P("/api/crash")["ph"] == "fly"
print("ALL OK, balance:", P("/api/me")["bal"])
