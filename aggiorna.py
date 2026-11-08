import json, os, time, urllib.parse, urllib.request
from datetime import datetime, timezone

CHIAVE = os.environ["API_FOOTBALL_KEY"]
BASE = "https://v3.football.api-sports.io"
LEGA, STAGIONE = 135, 2026   # Serie A, stagione 2026/27
MAX_STATISTICHE = 80         # richieste al giorno per scaricare corner e cartellini
PROSSIME = 10                # partite per cui scaricare le quote
os.makedirs("data", exist_ok=True)


def api(percorso, **parametri):
    url = f"{BASE}/{percorso}?" + urllib.parse.urlencode(parametri)
    req = urllib.request.Request(url, headers={"x-apisports-key": CHIAVE})
    d = json.load(urllib.request.urlopen(req, timeout=60))
    if d.get("errors"):
        print("Errore API:", percorso, parametri, d["errors"])
    time.sleep(7)  # il piano gratuito ammette 10 richieste al minuto
    return d.get("response", [])


def leggi(percorso, default):
    if os.path.exists(percorso):
        with open(percorso, encoding="utf-8") as f:
            return json.load(f)
    return default


def salva(percorso, dati):
    with open(percorso, "w", encoding="utf-8") as f:
        json.dump(dati, f, ensure_ascii=False, indent=1)


def media(v):
    return sum(v) / len(v) if v else 0


def ridotto(somma, n, m, k=3):
    # media della squadra avvicinata alla media del campionato quando ha poche partite
    return (somma + k * m) / (n + k)


partite = api("fixtures", league=LEGA, season=STAGIONE)
giocate = [f for f in partite if f["fixture"]["status"]["short"] == "FT"]
future = sorted((f for f in partite if f["fixture"]["status"]["short"] == "NS"),
                key=lambda f: f["fixture"]["timestamp"])[:PROSSIME]

# --- gol: forza di attacco e difesa di ogni squadra ---
mh = media([f["goals"]["home"] for f in giocate]) or 1.4
ma = media([f["goals"]["away"] for f in giocate]) or 1.1
g = (mh + ma) / 2
gol = {}
for f in giocate:
    h, a = f["teams"]["home"]["name"], f["teams"]["away"]["name"]
    gh, ga = f["goals"]["home"], f["goals"]["away"]
    for sq, fatti, subiti in ((h, gh, ga), (a, ga, gh)):
        d = gol.setdefault(sq, {"n": 0, "f": 0, "s": 0})
        d["n"] += 1
        d["f"] += fatti
        d["s"] += subiti


def forza(sq, campo):
    d = gol.get(sq, {"n": 0, "f": 0, "s": 0})
    return ridotto(d[campo], d["n"], g) / g


# --- corner e cartellini: si scaricano a blocchi e restano in data/stats.json ---
cache = leggi("data/stats.json", {})
mancanti = [f for f in giocate if str(f["fixture"]["id"]) not in cache][:MAX_STATISTICHE]
for f in mancanti:
    r = api("fixtures/statistics", fixture=f["fixture"]["id"])
    voce = {}
    for blocco in r:
        v = {s["type"]: s["value"] for s in blocco["statistics"]}
        if "Corner Kicks" in v:
            voce[blocco["team"]["id"]] = {
                "c": int(v.get("Corner Kicks") or 0),
                "k": int(v.get("Yellow Cards") or 0) + int(v.get("Red Cards") or 0),
            }
    ih, ia = f["teams"]["home"]["id"], f["teams"]["away"]["id"]
    cache[str(f["fixture"]["id"])] = {"h": voce[ih], "a": voce[ia]} if ih in voce and ia in voce else None
salva("data/stats.json", cache)

tot = {}
for f in giocate:
    s = cache.get(str(f["fixture"]["id"]))
    if not s:
        continue
    h, a = f["teams"]["home"]["name"], f["teams"]["away"]["name"]
    for sq, mio, altro in ((h, s["h"], s["a"]), (a, s["a"], s["h"])):
        d = tot.setdefault(sq, {"n": 0, "cf": 0, "cs": 0, "k": 0})
        d["n"] += 1
        d["cf"] += mio["c"]
        d["cs"] += altro["c"]
        d["k"] += mio["k"]
validi = [x for v in cache.values() if v for x in (v["h"], v["a"])]
mc = media([x["c"] for x in validi]) or 5.0
mk = media([x["k"] for x in validi]) or 2.3


def tasso(sq, campo, m):
    d = tot.get(sq, {"n": 0, "cf": 0, "cs": 0, "k": 0})
    return ridotto(d[campo], d["n"], m)


# --- quote: si associa il nome del mercato dell'API alle etichette dell'app ---
ESCLUDI = ("half", "1st", "2nd", "first", "second", "home", "away", "asian",
           "team", "handicap", "even", "odd", "exact")


def chiave(mercato, valore):
    m, v = mercato.lower().strip(), str(valore).strip()
    if m == "match winner":
        return {"Home": "Esito 1X2|1", "Draw": "Esito 1X2|X", "Away": "Esito 1X2|2"}.get(v)
    if m == "double chance":
        return {"Home/Draw": "Doppia chance|1X", "Draw/Away": "Doppia chance|X2",
                "Home/Away": "Doppia chance|12"}.get(v)
    if m == "both teams score":
        return {"Yes": "Gol|Gol (GG)", "No": "Gol|No gol (NG)"}.get(v)
    if any(x in m for x in ESCLUDI) or not v.startswith(("Over", "Under")):
        return None
    if "corner" in m:
        gr = "Corner"
    elif "card" in m:
        gr = "Cartellini"
    elif m == "goals over/under":
        gr = "Gol"
    else:
        return None
    return f"{gr}|{v.replace('.', ',')}"


def lim(x):
    return round(max(0.3, min(3.5, x)), 2)


visti, uscita = set(), []
for f in future:
    h, a = f["teams"]["home"]["name"], f["teams"]["away"]["name"]
    lh = lim(mh * forza(h, "f") * forza(a, "s"))
    la = lim(ma * forza(a, "f") * forza(h, "s"))
    lc = round((tasso(h, "cf", mc) + tasso(a, "cs", mc)) / 2 + (tasso(a, "cf", mc) + tasso(h, "cs", mc)) / 2, 1)
    lk = round(tasso(h, "k", mk) + tasso(a, "k", mk), 1)
    quote = {}
    for r in api("odds", fixture=f["fixture"]["id"]):
        for book in r.get("bookmakers", []):
            for bet in book.get("bets", []):
                visti.add(bet["name"])
                for v in bet["values"]:
                    k = chiave(bet["name"], v["value"])
                    if not k:
                        continue
                    q = float(v["odd"])
                    if k not in quote or q > quote[k]["q"]:
                        quote[k] = {"q": q, "b": book["name"]}
    uscita.append({"id": f["fixture"]["id"], "data": f["fixture"]["date"], "h": h, "a": a,
                   "arbitro": f["fixture"].get("referee"), "lam": [lh, la, lc, lk], "quote": quote})

salva("data/mercati_visti.json", sorted(visti))
salva("data/dati.json", {"aggiornato": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                         "partite": uscita})
print("Fatto:", len(giocate), "partite giocate,", len(uscita), "future,",
      len(mancanti), "statistiche scaricate")
