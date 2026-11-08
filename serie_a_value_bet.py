#!/usr/bin/env python3
"""
Serie A - strumento di analisi quota vs probabilità (BOZZA).

Cosa fa:
  1. Scarica risultati e quote storiche della Serie A da football-data.co.uk (file I1.csv).
  2. Stima un modello Poisson (attacco/difesa per squadra, vantaggio casa, pesi nel tempo).
  3. Calcola le probabilità di 1X2 e Over/Under 2.5 per le prossime partite.
  4. Le confronta con le quote e segnala le "value bet": prob * quota - 1 > soglia.

Uso:
  pip install pandas numpy scipy
  python serie_a_value_bet.py              # partite in programma
  python serie_a_value_bet.py --backtest   # controllo storico (fallo PRIMA di fidarti)

Non è un consiglio di gioco. Il gioco d'azzardo comporta rischi di perdita.
"""
import argparse
import sys

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson

BASE_URL = "https://www.football-data.co.uk/mmz4281/{s}/I1.csv"
FIXTURES_URL = "https://www.football-data.co.uk/fixtures.csv"
SEASONS = ["2324", "2425", "2526", "2627"]

HALF_LIFE_DAYS = 365   # le partite vecchie pesano meno
MAX_GOALS = 10
BLEND = 0.5            # peso del modello vs probabilità di mercato (0 = solo mercato)
MIN_EDGE = 0.05        # vantaggio atteso minimo (5%)
MIN_PROB = 0.20        # scarta i "colpi di fortuna" con probabilità troppo bassa

ODDS_1X2 = [["AvgH", "AvgD", "AvgA"], ["B365H", "B365D", "B365A"]]
ODDS_OU = [["Avg>2.5", "Avg<2.5"], ["B365>2.5", "B365<2.5"]]


# ----------------------------------------------------------------- dati
def parse_dates(s):
    return pd.to_datetime(s, dayfirst=True, format="mixed", errors="coerce")


def load_results(seasons=SEASONS):
    frames = []
    for s in seasons:
        try:
            frames.append(pd.read_csv(BASE_URL.format(s=s)))
        except Exception as e:  # stagione non disponibile
            print(f"[avviso] stagione {s} non scaricata: {e}", file=sys.stderr)
    if not frames:
        sys.exit("Nessun dato scaricato. Controlla la connessione.")
    df = pd.concat(frames, ignore_index=True)
    df["Date"] = parse_dates(df["Date"])
    df = df.dropna(subset=["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"])
    return df.sort_values("Date").reset_index(drop=True)


def load_fixtures():
    fx = pd.read_csv(FIXTURES_URL)
    fx = fx[fx["Div"] == "I1"].copy()
    fx["Date"] = parse_dates(fx["Date"])
    return fx.dropna(subset=["HomeTeam", "AwayTeam"]).reset_index(drop=True)


# ----------------------------------------------------------------- modello
def fit_model(df, half_life=HALF_LIFE_DAYS, reg=0.5):
    """Poisson: log(gol_casa) = mu + casa + att[h] - dif[a]; log(gol_trasf) = mu + att[a] - dif[h]."""
    teams = sorted(set(df["HomeTeam"]) | set(df["AwayTeam"]))
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    h = df["HomeTeam"].map(idx).to_numpy()
    a = df["AwayTeam"].map(idx).to_numpy()
    gh = df["FTHG"].to_numpy(float)
    ga = df["FTAG"].to_numpy(float)
    age = (df["Date"].max() - df["Date"]).dt.days.to_numpy()
    w = 0.5 ** (age / half_life)

    def unpack(p):
        return p[:n], p[n:2 * n], p[2 * n], p[2 * n + 1]

    def nll(p):
        att, dif, home, mu = unpack(p)
        lh = mu + home + att[h] - dif[a]
        la = mu + att[a] - dif[h]
        ll = w * (gh * lh - np.exp(lh)) + w * (ga * la - np.exp(la))
        return -ll.sum() + reg * (np.sum(att ** 2) + np.sum(dif ** 2))

    def grad(p):
        att, dif, home, mu = unpack(p)
        lh = mu + home + att[h] - dif[a]
        la = mu + att[a] - dif[h]
        rh = w * (gh - np.exp(lh))
        ra = w * (ga - np.exp(la))
        g_att = np.bincount(h, rh, n) + np.bincount(a, ra, n)
        g_dif = -np.bincount(a, rh, n) - np.bincount(h, ra, n)
        g = np.concatenate([g_att, g_dif, [rh.sum(), rh.sum() + ra.sum()]])
        pen = np.concatenate([2 * reg * att, 2 * reg * dif, [0, 0]])
        return -g + pen

    p0 = np.zeros(2 * n + 2)
    p0[-1] = np.log(max(gh.mean(), 0.5))
    res = minimize(nll, p0, jac=grad, method="L-BFGS-B")
    att, dif, home, mu = unpack(res.x)
    return {"idx": idx, "att": att, "dif": dif, "home": home, "mu": mu}


def score_matrix(m, home_team, away_team):
    i, j = m["idx"][home_team], m["idx"][away_team]
    lh = np.exp(m["mu"] + m["home"] + m["att"][i] - m["dif"][j])
    la = np.exp(m["mu"] + m["att"][j] - m["dif"][i])
    g = np.arange(MAX_GOALS + 1)
    M = np.outer(poisson.pmf(g, lh), poisson.pmf(g, la))  # righe = gol casa
    return M / M.sum()


def markets(M):
    p1 = np.tril(M, -1).sum()
    px = np.trace(M)
    p2 = np.triu(M, 1).sum()
    tot = np.add.outer(np.arange(M.shape[0]), np.arange(M.shape[1]))
    over = M[tot >= 3].sum()
    return p1, px, p2, over, 1 - over


# ----------------------------------------------------------------- value bet
def pick_cols(df, options):
    for cols in options:
        if all(c in df.columns for c in cols):
            return cols
    return None


def devig(odds):
    inv = 1 / np.asarray(odds, float)
    return inv / inv.sum()


def candidates(m, row, c1, c2, blend=BLEND):
    home, away = row["HomeTeam"], row["AwayTeam"]
    if home not in m["idx"] or away not in m["idx"]:
        return []  # squadra mai vista (es. neopromossa)
    p1, px, p2, po, pu = markets(score_matrix(m, home, away))
    out = []
    for cols, probs, labels in ((c1, [p1, px, p2], ["1", "X", "2"]),
                                (c2, [po, pu], ["Over 2.5", "Under 2.5"])):
        if cols is None:
            continue
        odds = [row[c] for c in cols]
        if any(pd.isna(o) for o in odds):
            continue
        mk = devig(odds)
        for lab, pm, pk, o in zip(labels, probs, mk, odds):
            p = blend * pm + (1 - blend) * pk
            out.append({"date": row["Date"], "match": f"{home} - {away}", "pick": lab,
                        "odds": o, "p_model": pm, "p_market": pk, "p": p,
                        "edge": p * o - 1})
    return out


def select(cands, min_edge=MIN_EDGE, min_prob=MIN_PROB):
    sel = [c for c in cands if c["edge"] >= min_edge and c["p"] >= min_prob]
    for c in sel:  # Kelly frazionato (1/4), massimo 2% del budget
        c["stake_%"] = 100 * min(0.25 * c["edge"] / (c["odds"] - 1), 0.02)
    return sel


def won(pick, row):
    if pick in ("1", "X", "2"):
        return {"H": "1", "D": "X", "A": "2"}.get(row["FTR"]) == pick
    total = row["FTHG"] + row["FTAG"]
    return (total > 2.5) if pick == "Over 2.5" else (total < 2.5)


# ----------------------------------------------------------------- comandi
def run_predict():
    df = load_results()
    m = fit_model(df)
    fx = load_fixtures()
    c1, c2 = pick_cols(fx, ODDS_1X2), pick_cols(fx, ODDS_OU)
    cands = []
    for _, r in fx.iterrows():
        cands += candidates(m, r, c1, c2)
    sel = sorted(select(cands), key=lambda c: -c["edge"])
    if not sel:
        print("Nessuna value bet con i filtri attuali.")
        return
    out = pd.DataFrame(sel)
    out["date"] = out["date"].dt.strftime("%d/%m")
    out = out[["date", "match", "pick", "odds", "p", "edge", "stake_%"]]
    print(out.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    print("\np = probabilità stimata | edge = p*quota-1 | stake_% = % del budget (Kelly 1/4, max 2%)")


def run_export(path="docs/data.json"):
    """Scrive le value bet in un JSON letto dalla web app."""
    import json
    import os
    df = load_results()
    m = fit_model(df)
    fx = load_fixtures()
    c1, c2 = pick_cols(fx, ODDS_1X2), pick_cols(fx, ODDS_OU)
    cands = []
    for _, r in fx.iterrows():
        cands += candidates(m, r, c1, c2)
    # soglie larghe: i filtri veri li imposta l'utente nella pagina
    sel = sorted(select(cands, min_edge=0.0, min_prob=0.10), key=lambda c: -c["edge"])
    bets = [{"date": c["date"].strftime("%Y-%m-%d") if pd.notna(c["date"]) else "",
             "match": c["match"], "pick": c["pick"],
             "odds": round(float(c["odds"]), 2), "p": round(float(c["p"]), 3),
             "edge": round(float(c["edge"]), 3), "stake": round(float(c["stake_%"]), 2)}
            for c in sel]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"updated": pd.Timestamp.now().strftime("%d/%m/%Y %H:%M"), "bets": bets},
                  f, ensure_ascii=False)
    print(f"Scritte {len(bets)} giocate in {path}")


def run_backtest(test_n=380):
    df = load_results()
    train, test = df.iloc[:-test_n], df.iloc[-test_n:]
    m = fit_model(train)
    c1, c2 = pick_cols(test, ODDS_1X2), pick_cols(test, ODDS_OU)
    ll_model = ll_mkt = ll_blend = 0.0
    n = 0
    bets = []
    for _, r in test.iterrows():
        cands = candidates(m, r, c1, c2)
        one = [c for c in cands if c["pick"] in ("1", "X", "2")]
        for c in one:
            if won(c["pick"], r):
                ll_model -= np.log(c["p_model"])
                ll_mkt -= np.log(c["p_market"])
                ll_blend -= np.log(c["p"])
                n += 1
        for c in select(cands):
            bets.append(c["odds"] - 1 if won(c["pick"], r) else -1.0)
    print(f"Partite di test: {n}  (modello addestrato solo sul periodo precedente)")
    print(f"Log-loss 1X2  modello: {ll_model / n:.4f} | mercato: {ll_mkt / n:.4f} | misto: {ll_blend / n:.4f}")
    print("(più basso = meglio; se il modello non batte il mercato, non c'è vantaggio)")
    if bets:
        print(f"Value bet segnalate: {len(bets)} | ROI a puntata fissa: {100 * np.mean(bets):+.1f}%")
    else:
        print("Nessuna value bet segnalata nel test.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--export", action="store_true", help="scrive docs/data.json per la web app")
    args = ap.parse_args()
    if args.backtest:
        run_backtest()
    elif args.export:
        run_export()
    else:
        run_predict()
