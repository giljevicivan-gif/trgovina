# -*- coding: utf-8 -*-
"""
Prijedlog narudžbe po danima (modul "prijedlog_dan").

Obični prijedlog (nabava.prijedlog) uzima prosjek prodaje po danu. Ovaj gleda
za svaki dan za koji se naručuje:
- koliko se tog artikla prodaje TAJ dan u tjednu (zadnjih 8 tjedana, a kad je
  podataka malo, prosjek se približi običnom dnevnom prosjeku);
- je li dan blagdan (trgovina je tada obično zatvorena, Zakon o trgovini) ili dan prije
  blagdana (više prodaje; koliko više, program nauči iz povijesti Robe);
- vremensku prognozu, ako je u postavkama upisano mjesto (Open-Meteo, besplatno, bez
  prijave): koliko se ukupno više ili manje proda kad je vruće (30 °C i više) ili
  kišovito (5 mm i više), program nauči iz prošlih dana i vremena tih dana.
Bez interneta sve radi, samo bez prognoze.

Odgovor ima isti oblik kao nabava.prijedlog, pa ga prikazuje ista kartica u Nabavi
(i Danas), uz popis dana s razlogom.
"""

import datetime as dt
import json
import math
import urllib.request

import nabava
import postavke

TJEDANA = 8
K_SMANJENJE = 2.0        # koliko "prosječnih dana" se dodaje kad je za dan u tjednu malo podataka
VRUCE = 30.0
KISA = 5.0
PRIJE_BLAGDANA = 1.25    # kad nema dovoljno povijesti
DANI = ["pon", "uto", "sri", "čet", "pet", "sub", "ned"]

SHEMA = """
CREATE TABLE IF NOT EXISTS vrijeme_dani (
    datum    TEXT PRIMARY KEY,
    tmax     REAL,
    oborine  REAL,
    prognoza INTEGER NOT NULL DEFAULT 0,
    dohvat   TEXT NOT NULL
);
"""


def pripremi(con):
    con.executescript(SHEMA)


# ---------------------------------------------------------------- blagdani

def uskrs(g):
    """Uskrs (gregorijanski), anonimni algoritam."""
    a, b, c = g % 19, g // 100, g % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g2 = (b - f + 1) // 3
    h = (19 * a + b - d - g2 + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mj = (h + l - 7 * m + 114) // 31
    dan = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(g, mj, dan)


def blagdani(g):
    """Blagdani i neradni dani u RH (Zakon o blagdanima, NN 110/19)."""
    u = uskrs(g)
    out = {dt.date(g, 1, 1): "Nova godina", dt.date(g, 1, 6): "Sveta tri kralja",
           u: "Uskrs", u + dt.timedelta(days=1): "Uskrsni ponedjeljak",
           dt.date(g, 5, 1): "Praznik rada", dt.date(g, 5, 30): "Dan državnosti",
           u + dt.timedelta(days=60): "Tijelovo", dt.date(g, 6, 22): "Dan antifašističke borbe",
           dt.date(g, 8, 5): "Dan pobjede", dt.date(g, 8, 15): "Velika Gospa",
           dt.date(g, 11, 1): "Svi sveti", dt.date(g, 11, 18): "Dan sjećanja",
           dt.date(g, 12, 25): "Božić", dt.date(g, 12, 26): "Sveti Stjepan"}
    return out


def blagdan(d):
    return blagdani(d.year).get(d)


# ---------------------------------------------------------------- vrijeme (Open-Meteo)

def _url(url):
    req = urllib.request.Request(url, headers={"User-Agent": "trgovina-prijedlog/1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def mjesto(con):
    try:
        lat = float(postavke.vrijednost(con, "prijedlog_dan.lat", "") or "x")
        lon = float(postavke.vrijednost(con, "prijedlog_dan.lon", "") or "x")
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _spremi_vrijeme(con, j, prognoza):
    d = j.get("daily") or {}
    t = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with con:
        for datum, tmax, ob in zip(d.get("time") or [], d.get("temperature_2m_max") or [],
                                   d.get("precipitation_sum") or []):
            if tmax is None and ob is None:
                continue
            if not prognoza:
                con.execute("INSERT OR REPLACE INTO vrijeme_dani VALUES (?, ?, ?, 0, ?)", (datum, tmax, ob, t))
            else:  # prognoza ne prepisuje izmjereno
                con.execute("""INSERT INTO vrijeme_dani VALUES (?, ?, ?, 1, ?) ON CONFLICT(datum) DO UPDATE
                               SET tmax = excluded.tmax, oborine = excluded.oborine, dohvat = excluded.dohvat
                               WHERE vrijeme_dani.prognoza = 1""", (datum, tmax, ob, t))


def osvjezi_vrijeme(con, od_povijesti):
    """Dohvati prognozu (7 dana) i izmjereno vrijeme za dane prodaje koje još nemamo.
    Vraća poruku greške ili ''."""
    m = mjesto(con)
    if not m:
        return "Mjesto nije upisano."
    lat, lon = m
    danas = dt.date.today()
    zadnji = postavke.vrijednost(con, "prijedlog_dan.prognoza_dohvat", "")
    greska = ""
    try:
        if zadnji[:13] != dt.datetime.now().strftime("%Y-%m-%d %H"):  # najviše jednom na sat
            _spremi_vrijeme(con, _url(
                "https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f"
                "&daily=temperature_2m_max,precipitation_sum&timezone=Europe%%2FZagreb&forecast_days=8"
                % (lat, lon)), True)
            postavke.postavi(con, "prijedlog_dan.prognoza_dohvat", dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
        # povijest: od prvog dana prodaje koji nema izmjereno vrijeme do prekjučer
        do = danas - dt.timedelta(days=2)
        ima = {r[0] for r in con.execute("SELECT datum FROM vrijeme_dani WHERE prognoza = 0 AND datum >= ?",
                                         (od_povijesti,))}
        nema = [d for d in _dani(dt.date.fromisoformat(od_povijesti), do) if d.isoformat() not in ima]
        if nema:
            _spremi_vrijeme(con, _url(
                "https://archive-api.open-meteo.com/v1/archive?latitude=%.4f&longitude=%.4f&start_date=%s"
                "&end_date=%s&daily=temperature_2m_max,precipitation_sum&timezone=Europe%%2FZagreb"
                % (lat, lon, nema[0].isoformat(), nema[-1].isoformat())), False)
    except Exception as e:  # bez interneta prijedlog radi i bez vremena
        greska = "Vremenska prognoza nije dohvaćena (%s)." % (getattr(e, "reason", None) or e)
    return greska


def _dani(od, do):
    d = od
    while d <= do:
        yield d
        d += dt.timedelta(days=1)


# ---------------------------------------------------------------- izračun

def _faktori(con, promet_po_danu, otvoreni):
    """Iz povijesti: koliko se više proda dan prije blagdana, kad je vruće, kad pada kiša.
    Omjer = promet tog dana / prosjek istog dana u tjednu."""
    po_danu_tj = {}
    for d in otvoreni:
        po_danu_tj.setdefault(d.weekday(), []).append(promet_po_danu.get(d, 0))
    prosjek_tj = {w: sum(v) / len(v) for w, v in po_danu_tj.items() if v}

    def omjeri(dani):
        o = [promet_po_danu.get(d, 0) / prosjek_tj[d.weekday()] for d in dani
             if d in otvoreni and prosjek_tj.get(d.weekday())]
        return sum(o) / len(o) if o else None, len(o)

    prije = [d for d in otvoreni if blagdan(d + dt.timedelta(days=1))]
    f_prije, n_prije = omjeri(prije)
    # blagdani u povijesti: je li trgovina radila
    sve = sorted(promet_po_danu)
    bl = [d for d in _dani(sve[0], sve[-1]) if blagdan(d)] if sve else []
    radila = [d for d in bl if d in otvoreni]
    f_bl, _ = omjeri(radila)
    vrijeme = {r[0]: (r[1], r[2]) for r in con.execute("SELECT datum, tmax, oborine FROM vrijeme_dani")}
    vruci = [d for d in otvoreni if (vrijeme.get(d.isoformat()) or (None, None))[0] is not None
             and vrijeme[d.isoformat()][0] >= VRUCE]
    kisni = [d for d in otvoreni if (vrijeme.get(d.isoformat()) or (None, None))[1] is not None
             and vrijeme[d.isoformat()][1] >= KISA]
    f_v, n_v = omjeri(vruci)
    f_k, n_k = omjeri(kisni)
    ogr = lambda f, n, zad, lo, hi: min(hi, max(lo, f)) if f and n >= (2 if zad == PRIJE_BLAGDANA else 5) else zad
    return {
        "prije_blagdana": round(ogr(f_prije, n_prije, PRIJE_BLAGDANA, 1.0, 2.0), 2), "n_prije_blagdana": n_prije,
        # blagdan: ako je trgovina u povijesti bila zatvorena (ili nema podataka), računa se 0
        "blagdan": round(min(1.5, max(0.3, f_bl)), 2) if radila else 0.0,
        "blagdana_u_povijesti": len(bl), "blagdana_radila": len(radila),
        "vruce": round(ogr(f_v, n_v, 1.0, 0.7, 1.4), 2), "n_vruce": n_v,
        "kisa": round(ogr(f_k, n_k, 1.0, 0.7, 1.4), 2), "n_kisa": n_k,
    }, vrijeme


def prijedlog(con, dana_prodaje=28, za_dana=3, sada=None):
    sada = sada or dt.datetime.now()
    try:
        dana_prodaje = max(1, min(int(dana_prodaje), 365))
        za_dana = max(0.5, min(float(str(za_dana).replace(",", ".")), 60))
    except ValueError:
        raise ValueError("Neispravan broj dana.")
    # osnovica: obični prijedlog za dugo razdoblje daje sve artikle koji se prodaju, s dobavljačem
    osnova = nabava.prijedlog(con, dana_prodaje, 60)
    zadnji = con.execute("SELECT MAX(datum) FROM roba_dokumenti WHERE status IN (2, 3)").fetchone()[0]
    if not zadnji:
        return nabava.prijedlog(con, dana_prodaje, za_dana)
    do = dt.date.fromisoformat(zadnji)
    od_profila = do - dt.timedelta(days=max(dana_prodaje, TJEDANA * 7) - 1)
    # dani kad je trgovina radila (barem jedan račun) i promet po danu (za faktore: sva povijest)
    promet = {dt.date.fromisoformat(r[0]): r[1] for r in con.execute(
        """SELECT datum, SUM(iznos) FROM roba_dokumenti WHERE status IN (2, 3) AND brisan = 0
           AND datum IS NOT NULL GROUP BY datum""")}
    otvoreni = {d for d, v in promet.items() if v > 0}
    greska_vremena = ""
    if mjesto(con):
        greska_vremena = osvjezi_vrijeme(con, max(min(promet).isoformat(),
                                                  (do - dt.timedelta(days=400)).isoformat()) if promet else zadnji)
    faktori, vrijeme = _faktori(con, promet, otvoreni)

    # profil po danu u tjednu za svaki artikl (bez blagdana i dana prije blagdana)
    obicni = [d for d in otvoreni if od_profila <= d <= do and not blagdan(d) and not blagdan(d + dt.timedelta(days=1))]
    broj_dana_tj = {w: sum(1 for d in obicni if d.weekday() == w) for w in range(7)}
    tjedana = (do - od_profila).days / 7.0
    zatvoreno_tj = {w for w in range(7) if broj_dana_tj[w] == 0 and tjedana >= 2}
    prodaja = {}
    for r in con.execute(
            """SELECT a.id, s.datum, SUM(s.kolicina) FROM roba_stavke s JOIN artikli a ON a.roba_sifra = s.artikl
               WHERE s.status IN (2, 3) AND s.datum BETWEEN ? AND ? GROUP BY a.id, s.datum""",
            (od_profila.isoformat(), do.isoformat())):
        d = dt.date.fromisoformat(r[1])
        if d in obicni or d.weekday() in zatvoreno_tj:
            prodaja.setdefault(r[0], {}).setdefault(d.weekday(), 0.0)
            prodaja[r[0]][d.weekday()] += r[2]

    # dani za koje se naručuje: od danas (ujutro) ili od sutra (poslije 14 h)
    pocetak = sada.date() + dt.timedelta(days=1 if sada.hour >= 14 else 0)
    cijelih = int(math.ceil(za_dana))
    dani = []
    for i in range(cijelih):
        d = pocetak + dt.timedelta(days=i)
        tezina = min(1.0, za_dana - i)
        f, razlozi = 1.0, []
        b = blagdan(d)
        if d.weekday() in zatvoreno_tj:
            f, razlozi = 0.0, ["trgovina ne radi taj dan u tjednu"]
        elif b:
            f = faktori["blagdan"]
            razlozi.append(b + (" (zatvoreno)" if f == 0 else ""))
        elif blagdan(d + dt.timedelta(days=1)):
            f = faktori["prije_blagdana"]
            razlozi.append("dan prije blagdana (%s)%s" % (blagdan(d + dt.timedelta(days=1)),
                                                           (" ×%g" % f).replace(".", ",") if f != 1 else ""))
        v = vrijeme.get(d.isoformat())
        if f and v:
            if v[0] is not None and v[0] >= VRUCE and faktori["vruce"] != 1.0:
                f *= faktori["vruce"]
                razlozi.append("vruće %g °C ×%s" % (round(v[0]), ("%g" % faktori["vruce"]).replace(".", ",")))
            if v[1] is not None and v[1] >= KISA and faktori["kisa"] != 1.0:
                f *= faktori["kisa"]
                razlozi.append("kiša %g mm ×%s" % (round(v[1]), ("%g" % faktori["kisa"]).replace(".", ",")))
        dani.append({"datum": d.isoformat(), "dan": DANI[d.weekday()], "tezina": tezina, "faktor": round(f, 2),
                     "razlog": ", ".join(razlozi), "tmax": v[0] if v else None, "oborine": v[1] if v else None})

    # narudžbe kupaca s rokom u razdoblju narudžbe (obični prijedlog gore gleda 60 dana)
    rez = {}
    if postavke.ukljucen(con, "narudzbe"):
        import narudzbe
        rez = narudzbe.rezervirano(con, (pocetak + dt.timedelta(days=cijelih - 1)).isoformat())
    stavke = []
    for s in osnova["stavke"]:
        prosjek = s["dnevno"]
        pt = prodaja.get(s["id"], {})
        po_danima = []
        ukupno = 0.0
        for d in dani:
            w = dt.date.fromisoformat(d["datum"]).weekday()
            n = broj_dana_tj.get(w, 0)
            dnevno_w = (pt.get(w, 0.0) + K_SMANJENJE * prosjek) / (n + K_SMANJENJE) if n else prosjek
            kol = dnevno_w * d["faktor"] * d["tezina"]
            po_danima.append(round(kol, 3))
            ukupno += kol
        rezerv = rez.get(s["id"], 0)
        potrebno = ukupno + s["min_zaliha"] + rezerv - max(s["zaliha"], 0)
        if potrebno <= 0:
            continue
        kom = s["jm"].lower() not in ("kg", "l", "m", "m2", "m3")
        t = dict(s)
        t["kolicina"] = math.ceil(potrebno - 1e-9) if kom else math.ceil(potrebno * 10 - 1e-9) / 10.0
        t["dnevno"] = round(ukupno / za_dana, 3)
        t["narudzbe"] = round(rezerv, 3)
        t["dana_zalihe"] = round(s["zaliha"] / (ukupno / za_dana), 1) if ukupno > 0 else None
        t["obicni_prosjek"] = prosjek
        t["po_danima"] = po_danima
        stavke.append(t)
    return {"od": od_profila.isoformat(), "do": do.isoformat(), "dana_prodaje": dana_prodaje, "za_dana": za_dana,
            "stavke": stavke, "po_danima": True, "dani": dani, "faktori": faktori,
            "vrijeme": bool(mjesto(con)), "greska_vremena": greska_vremena}


def postavke_modula(con):
    return {"lat": postavke.vrijednost(con, "prijedlog_dan.lat", ""), "lon": postavke.vrijednost(con, "prijedlog_dan.lon", ""),
            "mjesto": postavke.vrijednost(con, "prijedlog_dan.mjesto", "")}


def spremi_postavke(con, p):
    lat, lon = str(p.get("lat") or "").strip().replace(",", "."), str(p.get("lon") or "").strip().replace(",", ".")
    if lat or lon:
        try:
            if not (-90 <= float(lat) <= 90 and -180 <= float(lon) <= 180):
                raise ValueError
        except ValueError:
            from nabava import Greska
            raise Greska("Neispravne koordinate (npr. Dubrovnik 42,65 i 18,09).")
    postavke.postavi(con, "prijedlog_dan.lat", lat)
    postavke.postavi(con, "prijedlog_dan.lon", lon)
    postavke.postavi(con, "prijedlog_dan.mjesto", str(p.get("mjesto") or "").strip()[:60])
    postavke.postavi(con, "prijedlog_dan.prognoza_dohvat", "")
    return {"ok": True}
