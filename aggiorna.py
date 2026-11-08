import csv, io, json, os, re, time, unicodedata, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

STAGIONE = "2627"  # stagione 2026/27
URL_RISULTATI = f"https://www.football-data.co.uk/mmz4281/{STAGIONE}/I1.csv"
URL_FUTURE = "https://www.football-data.co.uk/fixtures.csv"
ODDS_KEY = os.environ.get("ODDS_API_KEY", "").strip()
ODDS_HOST = "https://api.the-odds-api.com/v4"
REGIONE = "eu"
MERCATI_EXTRA = "btts,double_chance,alternate_totals,alternate_totals_corners,alternate_totals_cards"
FINESTRA_ORE = 26    # mercati extra solo per le partite che iniziano entro queste ore
RIUSO_ORE = 20       # non si ripete la richiesta se è più recente di così
CREDITI_MINIMI = 60  # sotto questa soglia non si scaricano i mercati extra
os.makedirs("data", exist_ok=True)
diag = []


def http(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    r = urllib.request.urlopen(req, timeout=60)
    return r.read(), r.headers


def scarica(url):
    try:
        corpo, _ = http(url)
    except Exception as e:
        print("Impossibile scaricare", url, "->", e)
        raise
    testo = corpo.decode("utf-8-sig", errors="replace")
    return [r for r in csv.DictReader(io.StringIO(testo)) if r.get("HomeTeam")]


def num(x):
    try:
        return float(str(x).strip())
    except ValueError:
        return None


def media(v):
    return sum(v) / len(v) if v else 0


def ridotto(somma, n, m, k=3):
    # media della squadra avvicinata alla media del campionato quando ha poche partite
    return (somma + k * m) / (n + k)


def lim(x):
    return round(max(0.3, min(3.5, x)), 2)


def virgola(x):
    return str(float(x)).replace(".", ",")


# --- risultati della stagione: gol, corner, cartellini ---
risultati = [r for r in scarica(URL_RISULTATI) if num(r.get("FTHG")) is not None]
mh = media([num(r["FTHG"]) for r in risultati]) or 1.4
ma = media([num(r["FTAG"]) for r in risultati]) or 1.1
g = (mh + ma) / 2
gol, tot, lista_c, lista_k = {}, {}, [], []
for r in risultati:
    h, a = r["HomeTeam"], r["AwayTeam"]
    gh, ga = num(r["FTHG"]), num(r["FTAG"])
    for sq, fatti, subiti in ((h, gh, ga), (a, ga, gh)):
        d = gol.setdefault(sq, {"n": 0, "f": 0, "s": 0})
        d["n"] += 1
        d["f"] += fatti
        d["s"] += subiti
    hc, ac, hy, ay = num(r.get("HC")), num(r.get("AC")), num(r.get("HY")), num(r.get("AY"))
    if None in (hc, ac, hy, ay):
        continue
    kh = hy + (num(r.get("HR")) or 0)
    ka = ay + (num(r.get("AR")) or 0)
    for sq, cf, cs, k in ((h, hc, ac, kh), (a, ac, hc, ka)):
        d = tot.setdefault(sq, {"n": 0, "cf": 0, "cs": 0, "k": 0})
        d["n"] += 1
        d["cf"] += cf
        d["cs"] += cs
        d["k"] += k
        lista_c.append(cf)
        lista_k.append(k)
mc = media(lista_c) or 5.0
mk = media(lista_k) or 2.3


def forza(sq, campo):
    d = gol.get(sq, {"n": 0, "f": 0, "s": 0})
    return ridotto(d[campo], d["n"], g) / g


def tasso(sq, campo, m):
    d = tot.get(sq, {"n": 0, "cf": 0, "cs": 0, "k": 0})
    return ridotto(d[campo], d["n"], m)


# --- prossime partite e quote del file gratuito ---
COL = {
    "Esito 1X2|1": [("MaxH", "migliore"), ("B365H", "Bet365"), ("AvgH", "media")],
    "Esito 1X2|X": [("MaxD", "migliore"), ("B365D", "Bet365"), ("AvgD", "media")],
    "Esito 1X2|2": [("MaxA", "migliore"), ("B365A", "Bet365"), ("AvgA", "media")],
    "Gol|Over 2,5": [("Max>2.5", "migliore"), ("B365>2.5", "Bet365"), ("Avg>2.5", "media")],
    "Gol|Under 2,5": [("Max<2.5", "migliore"), ("B365<2.5", "Bet365"), ("Avg<2.5", "media")],
}


def quando(riga):
    d = None
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            d = datetime.strptime((riga.get("Date") or "").strip(), fmt)
            break
        except ValueError:
            pass
    if d is None:
        return None
    try:
        ora, minuti = map(int, (riga.get("Time") or "12:00").strip().split(":"))
    except ValueError:
        ora, minuti = 12, 0
    return d.replace(hour=ora, minute=minuti, tzinfo=ZoneInfo("Europe/London")).isoformat()


tutte = scarica(URL_FUTURE)
future = [r for r in tutte if r.get("Div") == "I1" and quando(r)]
future.sort(key=quando)
uscita = []
for i, r in enumerate(future):
    h, a = r["HomeTeam"], r["AwayTeam"]
    lh = lim(mh * forza(h, "f") * forza(a, "s"))
    la = lim(ma * forza(a, "f") * forza(h, "s"))
    lc = round((tasso(h, "cf", mc) + tasso(a, "cs", mc)) / 2 + (tasso(a, "cf", mc) + tasso(h, "cs", mc)) / 2, 1)
    lk = round(tasso(h, "k", mk) + tasso(a, "k", mk), 1)
    quote = {}
    for chiave, candidati in COL.items():
        for col, nome in candidati:
            v = num(r.get(col))
            if v and v > 1:
                quote[chiave] = {"q": v, "b": nome}
                break
    uscita.append({"id": i, "data": quando(r), "h": h, "a": a, "arbitro": r.get("Referee") or None,
                   "lam": [lh, la, lc, lk], "quote": quote})


# --- The Odds API: più mercati (btts, doppia chance, linee alternative, corner, cartellini) ---
STOP = {"fc", "ac", "as", "ssc", "ss", "us", "bc", "sc", "cfc", "calcio", "acf", "afc", "hellas",
        "1907", "1909", "1913", "1919", "1927", "1928"}
ALIAS = {"inter milan": "inter", "internazionale": "inter", "fc internazionale milano": "inter",
         "ac milan": "milan", "hellas verona": "verona"}


def norm(nome):
    n = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode().lower()
    n = " ".join(re.sub(r"[^a-z0-9 ]", " ", n).split())
    if n in ALIAS:
        return ALIAS[n]
    return " ".join(w for w in n.split() if w not in STOP) or n


def trova_evento(h, a, eventi):
    nh, na = norm(h), norm(a)
    for ev in eventi:
        if norm(ev["home_team"]) == nh and norm(ev["away_team"]) == na:
            return ev
    for ev in eventi:
        if set(nh.split()) <= set(norm(ev["home_team"]).split()) and set(na.split()) <= set(norm(ev["away_team"]).split()):
            return ev
    return None


crediti = {"rimasti": None}
visti = {}


def odds_get(percorso, **p):
    p["apiKey"] = ODDS_KEY
    try:
        corpo, h = http(f"{ODDS_HOST}/{percorso}?" + urllib.parse.urlencode(p))
    except Exception as e:  # il messaggio non contiene la chiave
        diag.append(f"The Odds API, errore su {percorso}: {e}")
        return None
    r = h.get("x-requests-remaining")
    if r is not None:
        crediti["rimasti"] = int(float(r))
    time.sleep(1)
    return json.loads(corpo)


def migliora(quote, chiave, prezzo, nome):
    if prezzo and prezzo > 1 and (chiave not in quote or prezzo > quote[chiave]["q"]):
        quote[chiave] = {"q": float(prezzo), "b": nome}


def doppia(nome, casa, ospite):
    parti = {norm(x) for x in re.split(r"\s+or\s+|\s*/\s*", nome.strip())}
    nc, no = norm(casa), norm(ospite)
    if parti == {nc, "draw"}:
        return "Doppia chance|1X"
    if parti == {no, "draw"}:
        return "Doppia chance|X2"
    if parti == {nc, no}:
        return "Doppia chance|12"
    return None


GRUPPI = {"totals": "Gol", "alternate_totals": "Gol", "alternate_totals_corners": "Corner",
          "alternate_totals_cards": "Cartellini"}


def leggi_mercati(ev, quote):
    casa, ospite = ev["home_team"], ev["away_team"]
    for book in ev.get("bookmakers", []):
        for m in book.get("markets", []):
            mk_ = m["key"]
            for o in m.get("outcomes", []):
                nome, prezzo = o.get("name", ""), o.get("price")
                esempi = visti.setdefault(mk_, [])
                if nome not in esempi and len(esempi) < 8:
                    esempi.append(nome)
                chiave = None
                if mk_ == "h2h":
                    chiave = {casa: "Esito 1X2|1", "Draw": "Esito 1X2|X", ospite: "Esito 1X2|2"}.get(nome)
                elif mk_ == "btts":
                    chiave = {"Yes": "Gol|Gol (GG)", "No": "Gol|No gol (NG)"}.get(nome)
                elif mk_ == "double_chance":
                    chiave = doppia(nome, casa, ospite)
                elif mk_ in GRUPPI and nome in ("Over", "Under") and o.get("point") is not None:
                    chiave = f"{GRUPPI[mk_]}|{nome} {virgola(o['point'])}"
                if chiave:
                    migliora(quote, chiave, prezzo, book.get("title", book.get("key", "")))


def odds_api():
    if not ODDS_KEY:
        diag.append("ODDS_API_KEY mancante: si usano solo le quote del file gratuito.")
        return
    sport = odds_get("sports")
    if not sport:
        return
    chiave = next((s["key"] for s in sport if s["key"] == "soccer_italy_serie_a"), None) or \
        next((s["key"] for s in sport if s["key"].startswith("soccer_italy") and "serie_a" in s["key"]), None)
    if not chiave:
        diag.append("Serie A non trovata tra gli sport attivi di The Odds API.")
        return
    eventi = odds_get(f"sports/{chiave}/events") or []
    diag.append(f"The Odds API: sport {chiave}, {len(eventi)} partite elencate.")
    if not eventi:
        return
    # quote principali (1X2 e Over/Under): 2 crediti per tutte le partite
    base = odds_get(f"sports/{chiave}/odds", regions=REGIONE, markets="h2h,totals", oddsFormat="decimal") or []
    quote_base = {}
    for ev in base:
        leggi_mercati(ev, quote_base.setdefault(ev["id"], {}))
    cache = {}
    if os.path.exists("data/odds_cache.json"):
        with open("data/odds_cache.json", encoding="utf-8") as f:
            cache = json.load(f)
    ora = datetime.now(timezone.utc)
    cache = {k: v for k, v in cache.items() if datetime.fromisoformat(v["inizio"]) > ora - timedelta(days=1)}
    for p in uscita:
        ev = trova_evento(p["h"], p["a"], eventi)
        if not ev:
            diag.append(f"Nessuna corrispondenza su The Odds API per {p['h']} - {p['a']}")
            continue
        eid = ev["id"]
        for k, v in quote_base.get(eid, {}).items():
            migliora(p["quote"], k, v["q"], v["b"])
        inizio = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        c = cache.get(eid)
        recente = c and datetime.fromisoformat(c["scaricato"]) > ora - timedelta(hours=RIUSO_ORE)
        if timedelta(0) < inizio - ora <= timedelta(hours=FINESTRA_ORE) and not recente:
            if crediti["rimasti"] is not None and crediti["rimasti"] < CREDITI_MINIMI:
                diag.append(f"Crediti quasi finiti ({crediti['rimasti']}): mercati extra saltati.")
            else:
                q2 = {}
                dati = odds_get(f"sports/{chiave}/events/{eid}/odds", regions=REGIONE,
                                markets=MERCATI_EXTRA, oddsFormat="decimal")
                if dati is None:  # se un mercato non è accettato si prova uno alla volta
                    for mk_ in MERCATI_EXTRA.split(","):
                        d1 = odds_get(f"sports/{chiave}/events/{eid}/odds", regions=REGIONE,
                                      markets=mk_, oddsFormat="decimal")
                        if d1:
                            leggi_mercati(d1, q2)
                else:
                    leggi_mercati(dati, q2)
                c = cache[eid] = {"scaricato": ora.isoformat(), "inizio": inizio.isoformat(), "quote": q2}
        if c:
            for k, v in c["quote"].items():
                migliora(p["quote"], k, v["q"], v["b"])
    with open("data/odds_cache.json", "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    diag.append(f"Crediti The Odds API rimasti: {crediti['rimasti']}")


odds_api()

with open("data/dati.json", "w", encoding="utf-8") as f:
    json.dump({"aggiornato": datetime.now(timezone.utc).isoformat(timespec="minutes"), "partite": uscita},
              f, ensure_ascii=False, indent=1)
with open("data/mercati_oddsapi.json", "w", encoding="utf-8") as f:
    json.dump(visti, f, ensure_ascii=False, indent=1)
with open("data/diagnostica.txt", "w", encoding="utf-8") as f:
    f.write(f"Partite giocate nel file dei risultati: {len(risultati)}\n")
    f.write(f"Righe nel file delle prossime partite: {len(tutte)}, di cui Serie A: {len(future)}\n")
    f.write("Colonne del file delle prossime partite: " + ", ".join(tutte[0].keys() if tutte else []) + "\n")
    f.write("\n".join(diag) + "\n")
print("Fatto:", len(risultati), "partite giocate,", len(uscita), "prossime partite Serie A")
print("\n".join(diag))
