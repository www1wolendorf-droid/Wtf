import os, json, time, math, hmac, hashlib, secrets, threading, urllib.parse
import requests, psycopg2, psycopg2.extras
from flask import Flask, request, jsonify, send_from_directory

env = lambda k, d="": os.environ.get(k, d).strip().strip("\"'")  # убираем пробелы и кавычки из значений
TOKEN = env("BOT_TOKEN")
ADMINS = {int(x) for x in env("ADMIN_IDS").replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}
BASE = (env("PUBLIC_URL") or env("RENDER_EXTERNAL_URL")).rstrip("/")
MAXB = int(os.environ.get("MAX_BET", "100000"))
API = f"https://api.telegram.org/bot{TOKEN}/"
WH = hashlib.sha256(("wh" + TOKEN).encode()).hexdigest()[:32]
HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
L = threading.RLock()
rnd = secrets.SystemRandom()
def clean_dsn(u):
    """Строка Neon: убираем channel_binding (ломает часть клиентов), гарантируем sslmode=require."""
    p = urllib.parse.urlsplit(u)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if k != "channel_binding"]
    if not any(k == "sslmode" for k, _ in q):
        q.append(("sslmode", "require"))
    return urllib.parse.urlunsplit(p._replace(query=urllib.parse.urlencode(q)))


DSN = clean_dsn(env("DATABASE_URL")) if env("DATABASE_URL") else ""


class DB:
    """Тонкая обёртка над psycopg2: переподключается, если Neon закрыл соединение."""
    c = None

    def conn(self):
        if self.c is None or self.c.closed:
            self.c = psycopg2.connect(DSN, connect_timeout=10, cursor_factory=psycopg2.extras.RealDictCursor)
        return self.c

    def execute(self, q, a=()):
        q = q.replace("?", "%s")
        for i in (0, 1):
            try:
                cur = self.conn().cursor()
                cur.execute(q, a)
                return cur
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                try:
                    self.c.close()
                except Exception:
                    pass
                self.c = None
                if i:
                    raise

    def commit(self):
        self.conn().commit()

    def rollback(self):
        try:
            if self.c is not None and not self.c.closed:
                self.c.rollback()
        except Exception:
            self.c = None


db = DB()
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS users(id BIGINT PRIMARY KEY, name TEXT, bal BIGINT NOT NULL DEFAULT 0 CHECK(bal>=0))",
    "CREATE TABLE IF NOT EXISTS hist(id BIGSERIAL PRIMARY KEY, uid BIGINT, t TEXT, d BIGINT, ts TEXT)",
    "CREATE INDEX IF NOT EXISTS hist_uid ON hist(uid, id DESC)",
    "CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, s BIGINT, mx BIGINT, n BIGINT DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS used(uid BIGINT, code TEXT, PRIMARY KEY(uid, code))",
    "CREATE TABLE IF NOT EXISTS games(uid BIGINT PRIMARY KEY, data TEXT)",
    "CREATE TABLE IF NOT EXISTS wd(id BIGSERIAL PRIMARY KEY, uid BIGINT, amt BIGINT, ts TEXT, status TEXT DEFAULT 'pending')",
    "CREATE TABLE IF NOT EXISTS pays(charge TEXT PRIMARY KEY)",
    "CREATE TABLE IF NOT EXISTS cases(uid BIGINT, kind TEXT, ts BIGINT, PRIMARY KEY(uid, kind))",
    "CREATE TABLE IF NOT EXISTS inv(id BIGSERIAL PRIMARY KEY, uid BIGINT, item TEXT, ts TEXT)",
)
ready = False


def ensure():
    global ready
    if ready:
        return
    for q in SCHEMA:
        db.execute(q)
    db.commit()
    ready = True


class E(Exception):
    pass


def tg(method, **p):
    try:
        return requests.post(API + method, json=p, timeout=10).json()
    except Exception:
        return {}


def auth():
    q = dict(urllib.parse.parse_qsl(request.headers.get("X-Init", ""), keep_blank_values=True))
    h = q.pop("hash", "")
    chk = "\n".join(f"{k}={v}" for k, v in sorted(q.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    ok = h and hmac.compare_digest(hmac.new(key, chk.encode(), hashlib.sha256).hexdigest(), h)
    if not ok or time.time() - int(q.get("auth_date", 0)) > 86400:
        raise E("Откройте приложение через Telegram")
    u = json.loads(q["user"])
    uid, name = int(u["id"]), u.get("username") or f"id{u['id']}"
    db.execute("INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name", (uid, name))
    db.commit()
    return uid, name


def api(f):
    def w():
        try:
            with L:
                ensure()
                uid, name = auth()
                return jsonify(f(uid, name, request.get_json(silent=True) or {}))
        except E as e:
            db.rollback()
            return jsonify(err=str(e))
        except (ValueError, KeyError, TypeError):
            db.rollback()
            return jsonify(err="Неверный запрос")
        except psycopg2.Error as e:
            app.logger.error("DB error: %s", e)
            db.rollback()
            return jsonify(err="Ошибка базы данных, попробуйте ещё раз")
    w.__name__ = f.__name__
    return app.route("/api/" + f.__name__.replace("_", "/"), methods=["POST"])(w)


def bal(uid):
    return db.execute("SELECT bal FROM users WHERE id=?", (uid,)).fetchone()["bal"]


def log(uid, t, d):
    db.execute("INSERT INTO hist(uid,t,d,ts) VALUES(?,?,?,?)", (uid, t, d, time.strftime("%d.%m.%Y, %H:%M")))


def add(uid, d, why=None):
    ok = db.execute("UPDATE users SET bal=bal+? WHERE id=? AND bal+?>=0", (d, uid, d)).rowcount > 0
    if ok and why:
        log(uid, why, d)
    db.commit()
    return ok


def take(uid, b):
    if b < 1:
        raise E("Введите ставку")
    if b > MAXB:
        raise E(f"Максимальная ставка {MAXB}")
    if not add(uid, -b):
        raise E("Недостаточно звёзд")


def out(uid, **kw):
    h = [dict(r) for r in db.execute("SELECT t,d,ts FROM hist WHERE uid=? ORDER BY id DESC LIMIT 30", (uid,))]
    return dict(bal=bal(uid), hist=h, **kw)


def plist():
    return {r["code"]: dict(s=r["s"], n=r["n"], max=r["mx"]) for r in db.execute("SELECT * FROM promos")}


def need_admin(uid):
    if uid not in ADMINS:
        raise E("Нет доступа")


@api
def me(uid, name, d):
    kw = dict(name=name, admin=uid in ADMINS,
              inv=[dict(r) for r in db.execute("SELECT item,ts FROM inv WHERE uid=? ORDER BY id DESC LIMIT 50", (uid,))])
    if uid in ADMINS:
        kw["promos"] = plist()
    return out(uid, **kw)


@api
def top(uid, name, d):
    return dict(top=[dict(n=r["name"], b=r["bal"]) for r in db.execute("SELECT name,bal FROM users ORDER BY bal DESC LIMIT 10")])


# ---------- одиночные игры (результат считает сервер) ----------
MU = (1.5, 2, 3, 5, 10, 50, 100)


@api
def up(uid, name, d):
    mu, b = float(d["mu"]), int(d["bet"])
    if mu not in MU:
        raise E("Неверный множитель")
    take(uid, b)
    roll = rnd.random() * 100
    win = roll < round(97 / mu, 2)
    if win:
        add(uid, round(b * mu), f"Апгрейд x{mu:g}")
    else:
        log(uid, "Апгрейд (проигрыш)", -b); db.commit()
    return out(uid, roll=roll, win=win)


@api
def coin(uid, name, d):
    b, side = int(d["bet"]), int(d["side"])
    if side not in (0, 1):
        raise E("Неверный выбор")
    take(uid, b)
    res = rnd.randrange(2)
    if res == side:
        add(uid, round(b * 1.96), "Монетка")
    else:
        log(uid, "Монетка (проигрыш)", -b); db.commit()
    return out(uid, res=res)


SEG = [0, .5, 0, 1.5, 0, 2, .5, 3]


@api
def wheel(uid, name, d):
    b = int(d["bet"])
    take(uid, b)
    i = rnd.randrange(8)
    m = SEG[i]
    if m:
        add(uid, round(b * m), f"Колесо x{m}")
    else:
        log(uid, "Колесо (проигрыш)", -b); db.commit()
    return out(uid, i=i)


# ---------- мины ----------
def mult(k, M):
    r = .97
    for i in range(k):
        r *= (25 - i) / (25 - M - i)
    return r


def gget(uid):
    r = db.execute("SELECT data FROM games WHERE uid=?", (uid,)).fetchone()
    return json.loads(r["data"]) if r else None


def gput(uid, g):
    if g is None:
        db.execute("DELETE FROM games WHERE uid=?", (uid,))
    else:
        db.execute("INSERT INTO games VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET data=EXCLUDED.data", (uid, json.dumps(g)))
    db.commit()


@api
def mines_state(uid, name, d):
    g = gget(uid)
    return dict(on=1, b=g["b"], M=g["M"], op=g["op"]) if g else dict(on=0)


@api
def mines_start(uid, name, d):
    b, M = int(d["bet"]), int(d["M"])
    if M not in (1, 3, 5, 10):
        raise E("Неверное число мин")
    if gget(uid):
        raise E("Игра уже идёт")
    take(uid, b)
    gput(uid, dict(b=b, M=M, op=[], m=rnd.sample(range(25), M)))
    return out(uid)


def mines_pay(uid, g):
    w = round(g["b"] * mult(len(g["op"]), g["M"]))
    add(uid, w, f"Мины x{mult(len(g['op']), g['M']):.2f}")
    gput(uid, None)
    return w


@api
def mines_open(uid, name, d):
    g, i = gget(uid), int(d["i"])
    if not g:
        raise E("Нет активной игры")
    if not 0 <= i < 25 or i in g["op"]:
        raise E("Клетка уже открыта")
    if i in g["m"]:
        log(uid, "Мины (проигрыш)", -g["b"]); gput(uid, None)
        return out(uid, boom=True, m=g["m"])
    g["op"].append(i)
    if len(g["op"]) == 25 - g["M"]:
        return out(uid, done=True, w=mines_pay(uid, g))
    gput(uid, g)
    return out(uid)


@api
def mines_cash(uid, name, d):
    g = gget(uid)
    if not g or not g["op"]:
        raise E("Нечего забирать")
    return out(uid, w=mines_pay(uid, g))


# ---------- краш (общий раунд для всех игроков) ----------
CR = dict(id=0, ph="boom", tl=0, tb=0, cp=1.0, bets={}, past=[])
now = lambda: time.time() * 1000
K = .00018


def new_round():
    cp = min(50.0, max(1.0, .97 / (1 - rnd.random())))
    t = now() + 7000
    CR.update(id=CR["id"] + 1, ph="wait", tl=t, tb=t + math.log(cp) / K, cp=cp, bets={})


def adv():
    c, t = CR, now()
    if c["ph"] == "wait" and t >= c["tl"]:
        c["ph"] = "fly"
    if c["ph"] == "fly" and t >= c["tb"]:
        c["ph"] = "boom"
        c["past"] = (c["past"] + [c["cp"]])[-20:]
        for uid, b in c["bets"].items():
            if b["st"] == 0:
                if c["cp"] >= 50:
                    b["st"], b["w"] = 50.0, b["b"] * 50
                    add(uid, b["w"], "Краш x50.00")
                else:
                    b["st"] = -1
                    log(uid, "Краш (проигрыш)", -b["b"])
        db.commit()
    if c["ph"] == "boom" and t >= c["tb"] + 3000:
        new_round()


@api
def crash(uid, name, d):
    adv()
    c, t = CR, now()
    b = c["bets"].get(uid)
    r = dict(bal=bal(uid), id=c["id"], ph=c["ph"], past=c["past"], left=max(0, c["tl"] - t), el=max(0, t - c["tl"]),
             since=max(0, t - c["tb"]), me=dict(b=b["b"], st=b["st"], w=b["w"]) if b else None,
             players=[dict(n=x["n"], b=x["b"], st=x["st"], w=x["w"]) for k, x in c["bets"].items() if k != uid])
    if c["ph"] == "boom":
        r["cp"] = c["cp"]
    return r


@api
def crash_bet(uid, name, d):
    adv()
    b = int(d["bet"])
    if CR["ph"] != "wait" or uid in CR["bets"]:
        raise E("Ставки не принимаются")
    take(uid, b)
    CR["bets"][uid] = dict(n=name, b=b, st=0, w=0)
    return dict(bal=bal(uid))


@api
def crash_cancel(uid, name, d):
    adv()
    b = CR["bets"].get(uid)
    if CR["ph"] != "wait" or not b:
        raise E("Нельзя отменить")
    add(uid, b["b"])
    del CR["bets"][uid]
    return dict(bal=bal(uid))


@api
def crash_cash(uid, name, d):
    adv()
    b = CR["bets"].get(uid)
    if CR["ph"] != "fly" or not b or b["st"] != 0:
        raise E("Слишком поздно")
    m = min(50.0, math.exp(K * (now() - CR["tl"])))
    b["st"], b["w"] = m, int(b["b"] * m)
    add(uid, b["w"], f"Краш x{m:.2f}")
    return dict(bal=bal(uid), st=m, w=b["w"])


# ---------- кейсы ----------
DAY, WEEK = 86400 * 1000, 7 * 86400 * 1000
# (приз, вес %) — меняйте шансы здесь. NFT в недельном кейсе очень редкий: 0.2%
POOL = dict(
    daily=[(15, 45), (25, 25), (35, 15), (50, 9), (75, 4.5), (100, 1.5)],
    weekly=[(15, 34), (25, 26), (35, 18), (50, 12), (75, 6.5), (100, 3.3), ("nft", 0.2)],
)
PERIOD = dict(daily=DAY, weekly=WEEK)


def case_left(uid):
    t, r = now(), {}
    for k, p in PERIOD.items():
        row = db.execute("SELECT ts FROM cases WHERE uid=? AND kind=?", (uid, k)).fetchone()
        r[k] = max(0, int(row["ts"] + p - t)) if row else 0
    return r


@api
def case_state(uid, name, d):
    return dict(left=case_left(uid), pool={k: [str(p) for p, _ in v] for k, v in POOL.items()})


@api
def case_open(uid, name, d):
    kind = d["kind"]
    if kind not in POOL:
        raise E("Неверный кейс")
    if case_left(uid)[kind] > 0:
        raise E("Кейс ещё недоступен")
    prize = rnd.choices([p for p, _ in POOL[kind]], [w for _, w in POOL[kind]])[0]
    db.execute("INSERT INTO cases VALUES(?,?,?) ON CONFLICT(uid,kind) DO UPDATE SET ts=EXCLUDED.ts", (uid, kind, int(now())))
    if prize == "nft":
        db.execute("INSERT INTO inv(uid,item,ts) VALUES(?,?,?)", (uid, "cap", time.strftime("%d.%m.%Y")))
        log(uid, "Недельный кейс: NFT Кепка", 0)
        db.commit()
        for ad in ADMINS:
            tg("sendMessage", chat_id=ad, text=f"🎁 NFT выигран: @{name} (id {uid}) — Кепка Telegram. Отправьте подарок вручную.")
    else:
        add(uid, prize, f"{'Дневной' if kind == 'daily' else 'Недельный'} кейс +{prize}")
    return out(uid, prize=f"s{prize}" if prize != "nft" else "nft", left=case_left(uid))


# ---------- промокоды, админка, кошелёк ----------
@api
def promo(uid, name, d):
    c = str(d["code"]).strip().upper()
    p = db.execute("SELECT * FROM promos WHERE code=?", (c,)).fetchone()
    if not p:
        raise E("Промокод не найден")
    if db.execute("SELECT 1 FROM used WHERE uid=? AND code=?", (uid, c)).fetchone():
        raise E("Уже использован")
    if p["n"] >= p["mx"]:
        raise E("Промокод закончился")
    db.execute("UPDATE promos SET n=n+1 WHERE code=?", (c,))
    db.execute("INSERT INTO used VALUES(?,?)", (uid, c))
    add(uid, p["s"], "Промокод " + c)
    return out(uid, s=p["s"])


@api
def promo_new(uid, name, d):
    need_admin(uid)
    c, s, m = str(d["code"]).strip().upper(), int(d["s"]), int(d["max"])
    if not c or s < 1 or m < 1:
        raise E("Неверные данные")
    db.execute("INSERT INTO promos(code,s,mx,n) VALUES(?,?,?,0) ON CONFLICT(code) DO UPDATE SET s=EXCLUDED.s, mx=EXCLUDED.mx, n=0", (c, s, m))
    db.commit()
    return dict(promos=plist())


@api
def admin_find(uid, name, d):
    need_admin(uid)
    q = str(d.get("q", "")).strip().lstrip("@").lower()
    rows = db.execute("SELECT name,bal FROM users WHERE lower(name) LIKE ? LIMIT 10", (f"%{q}%",)) if q else []
    return dict(users=[dict(n=r["name"], b=r["bal"]) for r in rows])


@api
def withdraw(uid, name, d):
    a = int(d["amt"])
    if a < 100:
        raise E("Минимум 100 ★")
    if not add(uid, -a, "Вывод (заявка)"):
        raise E("Недостаточно звёзд")
    db.execute("INSERT INTO wd(uid,amt,ts) VALUES(?,?,?)", (uid, a, time.strftime("%d.%m.%Y, %H:%M")))
    db.commit()
    for ad in ADMINS:
        tg("sendMessage", chat_id=ad, text=f"Заявка на вывод: @{name} (id {uid}) — {a} ★")
    return out(uid)


@api
def admin_stats(uid, name, d):
    need_admin(uid)
    one = lambda q: db.execute(q).fetchone()
    u = one("SELECT COUNT(*) c, COALESCE(SUM(bal),0) s FROM users")
    w = one("SELECT COUNT(*) c, COALESCE(SUM(amt),0) s FROM wd WHERE status='pending'")
    dep = one("SELECT COALESCE(SUM(d),0) s FROM hist WHERE t='Пополнение'")
    wd = [dict(id=r["id"], n=r["name"], amt=r["amt"], ts=r["ts"]) for r in db.execute(
        "SELECT wd.id, COALESCE(users.name,'?') AS name, wd.amt, wd.ts FROM wd LEFT JOIN users ON users.id=wd.uid "
        "WHERE wd.status='pending' ORDER BY wd.id LIMIT 30")]
    nft = [dict(n=r["name"], ts=r["ts"]) for r in db.execute(
        "SELECT COALESCE(users.name,'?') AS name, inv.ts FROM inv LEFT JOIN users ON users.id=inv.uid ORDER BY inv.id DESC LIMIT 10")]
    return dict(users=u["c"], total=int(u["s"]), wd_n=w["c"], wd_sum=int(w["s"]), dep=int(dep["s"]), wd=wd, nft=nft,
                promos=plist())


@api
def admin_wd(uid, name, d):
    need_admin(uid)
    r = db.execute("SELECT * FROM wd WHERE id=? AND status='pending'", (int(d["id"]),)).fetchone()
    if not r:
        raise E("Заявка уже обработана")
    if d.get("ok"):
        db.execute("UPDATE wd SET status='paid' WHERE id=?", (r["id"],))
        db.commit()
        tg("sendMessage", chat_id=r["uid"], text=f"✅ Вывод {r['amt']} ★ выполнен")
    else:
        db.execute("UPDATE wd SET status='rejected' WHERE id=?", (r["id"],))
        add(r["uid"], r["amt"], "Возврат вывода")
        tg("sendMessage", chat_id=r["uid"], text=f"Заявка на вывод {r['amt']} ★ отклонена, звёзды возвращены")
    return dict(ok=1)


@api
def admin_adjust(uid, name, d):
    need_admin(uid)
    u = db.execute("SELECT id FROM users WHERE lower(name)=?", (str(d["name"]).strip().lstrip("@").lower(),)).fetchone()
    a = int(d["d"])
    if not u or a == 0:
        raise E("Пользователь не найден или сумма 0")
    if not add(u["id"], a, "Корректировка админом"):
        raise E("Баланс не может уйти в минус")
    return dict(ok=1)


@api
def invoice(uid, name, d):
    a = int(d["amt"])
    if not 1 <= a <= 100000:
        raise E("Сумма от 1 до 100 000")
    r = tg("createInvoiceLink", title="Пополнение BlackUp", description=f"{a} ★ в BlackUp", payload=f"dep:{uid}",
           provider_token="", currency="XTR", prices=[{"label": "★", "amount": a}])
    if not r.get("ok"):
        raise E("Не удалось создать счёт")
    return dict(link=r["result"])


# ---------- вебхук бота ----------
@app.post("/wh/<s>")
def wh(s):
    if s != WH:
        return "", 404
    u = request.get_json(silent=True) or {}
    m = u.get("message") or {}
    if "pre_checkout_query" in u:
        q = u["pre_checkout_query"]
        tg("answerPreCheckoutQuery", pre_checkout_query_id=q["id"], ok=q.get("currency") == "XTR")
    elif m.get("successful_payment"):
        p, uid = m["successful_payment"], m["from"]["id"]
        with L:
            if p["currency"] == "XTR" and p["invoice_payload"] == f"dep:{uid}" and \
                    db.execute("INSERT INTO pays VALUES(?) ON CONFLICT DO NOTHING", (p["telegram_payment_charge_id"],)).rowcount:
                db.execute("INSERT INTO users(id,name) VALUES(?,?) ON CONFLICT DO NOTHING", (uid, f"id{uid}"))
                add(uid, p["total_amount"], "Пополнение")
    elif (m.get("text") or "").startswith("/start"):
        tg("sendMessage", chat_id=m["chat"]["id"], text="Добро пожаловать в BlackUp ♛",
           reply_markup={"inline_keyboard": [[{"text": "Играть", "web_app": {"url": BASE}}]]})
    return "ok"


@app.get("/static/<path:f>")
def static_files(f):
    r = send_from_directory(os.path.join(HERE, "static"), f)
    r.headers["Cache-Control"] = "public, max-age=86400"
    return r


@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.get("/health")
def health():
    return "ok"


@app.get("/diag")
def diag():
    """Открой https://твой-сервис.onrender.com/diag — покажет, что именно не настроено."""
    r = dict(BOT_TOKEN="ok" if TOKEN and ":" in TOKEN else "НЕ ЗАДАН или неверный формат",
             DATABASE_URL="ok" if DSN else "НЕ ЗАДАН", ADMIN_IDS=f"{len(ADMINS)} шт.", PUBLIC_URL=BASE or "нет")
    me = tg("getMe") if TOKEN else {}
    res = me.get("result")
    r["bot"] = "@" + str(res.get("username")) if me.get("ok") and isinstance(res, dict) else "токен отклонён Telegram (отзовите и вставьте новый)"
    try:
        with L:
            ensure()
            db.execute("SELECT 1")
        r["database"] = "ok"
    except Exception as e:
        app.logger.error("diag DB error: %s", e)
        m = str(e).lower()
        r["database"] = "ОШИБКА: " + ("неверный пароль/логин в строке" if "password" in m else "хост не найден, проверьте строку" if "translate host" in m
                                      else "нет соединения/таймаут" if "timeout" in m or "refused" in m else type(e).__name__)
    return jsonify(r)


@app.errorhandler(Exception)
def oops(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    app.logger.exception(e)
    return jsonify(err="Ошибка сервера: " + type(e).__name__), 500


def setup():
    r1 = tg("setWebhook", url=f"{BASE}/wh/{WH}", allowed_updates=["message", "pre_checkout_query"])
    r2 = tg("setChatMenuButton", menu_button={"type": "web_app", "text": "Играть", "web_app": {"url": BASE}})
    print("webhook:", r1.get("ok"), r1.get("description", ""), "| menu:", r2.get("ok"), r2.get("description", ""), flush=True)
    try:
        with L:
            ensure()
        print("database: ok", flush=True)
    except Exception as e:
        print("database ERROR:", type(e).__name__, str(e)[:200], flush=True)


if BASE and TOKEN:
    threading.Thread(target=setup, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
