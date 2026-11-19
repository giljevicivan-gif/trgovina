# -*- coding: utf-8 -*-
"""
HACCP dnevnik (dodatni modul, uključuje se u Postavkama).

Za trgovinu hranom bez gastro ponude (nizak rizik) vodiče dobre higijenske
prakse (HGK, Vodič dobre higijenske prakse za trgovinu u poslovanju s hranom)
traže evidencije: temperature rashladnih uređaja gdje se mora održavati
hladni lanac, plan i evidenciju čišćenja, pranja i dezinfekcije, te podatke o
prijemu robe (dobavljač, datum, roba, količina) radi sljedivosti.
Granice temperature upisuju se po uređaju prema vašem HACCP planu
(zamrznuto -18 °C ili hladnije).

Zapis se nikad ne mijenja ni briše. Pogrešan unos se poništi uz razlog, a
ostaje vidljiv (prekrižen) na ispisu, kao što inspekcija i očekuje.
Temperatura izvan granica ne može se upisati bez korektivne mjere.
"""

import datetime as dt

from nabava import Greska, sada

VRSTE_UREDJAJA = {"hladnjak": (0.0, 4.0), "vitrina": (2.0, 8.0), "zamrzivac": (-30.0, -18.0),
                  "prostorija": (10.0, 25.0)}
UCESTALOST = {"dnevno": 1, "tjedno": 7, "mjesecno": 31}
NACINI = ("čišćenje", "pranje", "dezinfekcija", "čišćenje i dezinfekcija")

SHEMA = """
CREATE TABLE IF NOT EXISTS haccp_uredjaji (
    id      INTEGER PRIMARY KEY,
    naziv   TEXT NOT NULL,
    vrsta   TEXT NOT NULL,
    min_t   REAL NOT NULL,
    max_t   REAL NOT NULL,
    aktivan INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS haccp_temperature (
    id          INTEGER PRIMARY KEY,
    uredjaj_id  INTEGER NOT NULL REFERENCES haccp_uredjaji(id),
    vrijeme     TEXT NOT NULL,              -- kad je izmjereno
    upisano     TEXT NOT NULL,              -- kad je upisano u program
    temperatura REAL NOT NULL,
    min_t       REAL NOT NULL,              -- granice u trenutku mjerenja
    max_t       REAL NOT NULL,
    tko         TEXT NOT NULL,
    mjera       TEXT NOT NULL DEFAULT '',   -- korektivna mjera ako je izvan granica
    ponisteno   TEXT NOT NULL DEFAULT ''    -- razlog poništenja; prazno = vrijedi
);
CREATE TABLE IF NOT EXISTS haccp_plan (
    id         INTEGER PRIMARY KEY,
    podrucje   TEXT NOT NULL,               -- npr. pod, police, hladnjak, vage
    ucestalost TEXT NOT NULL,               -- dnevno, tjedno, mjesecno
    nacin      TEXT NOT NULL,
    sredstvo   TEXT NOT NULL DEFAULT '',
    aktivan    INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS haccp_ciscenje (
    id        INTEGER PRIMARY KEY,
    plan_id   INTEGER REFERENCES haccp_plan(id),
    vrijeme   TEXT NOT NULL,
    upisano   TEXT NOT NULL,
    podrucje  TEXT NOT NULL,
    nacin     TEXT NOT NULL,
    sredstvo  TEXT NOT NULL DEFAULT '',
    tko       TEXT NOT NULL,
    ponisteno TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS haccp_prijem (
    id          INTEGER PRIMARY KEY,
    vrijeme     TEXT NOT NULL,
    upisano     TEXT NOT NULL,
    dobavljac   TEXT NOT NULL,
    dokument    TEXT NOT NULL DEFAULT '',   -- otpremnica / račun dobavljača
    roba        TEXT NOT NULL,
    temperatura REAL,                       -- samo za robu u hladnom lancu
    prihvaceno  INTEGER NOT NULL DEFAULT 1, -- 0 = odbijeno / vraćeno
    napomena    TEXT NOT NULL DEFAULT '',
    tko         TEXT NOT NULL,
    ponisteno   TEXT NOT NULL DEFAULT ''
);
"""

TABLICE = {"temperatura": "haccp_temperature", "ciscenje": "haccp_ciscenje", "prijem": "haccp_prijem"}


def pripremi(con):
    con.executescript(SHEMA)


def _tekst(p, k, n=120, obavezno=None):
    v = " ".join(str(p.get(k) or "").split())[:n]
    if obavezno and not v:
        raise Greska(obavezno)
    return v


def _broj(v, poruka):
    try:
        x = round(float(str(v).replace(",", ".")), 1)
    except (TypeError, ValueError):
        raise Greska(poruka)
    if not -100 <= x <= 300:  # odbija i NaN / beskonačno
        raise Greska(poruka)
    return x


def _vrijeme(p):
    """Vrijeme mjerenja: zadano ili sada; ne u budućnosti, ne starije od 7 dana."""
    v = str(p.get("vrijeme") or "").strip().replace("T", " ")
    if not v:
        return sada()
    try:
        t = dt.datetime.strptime(v[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        raise Greska("Neispravno vrijeme.")
    sad = dt.datetime.now()
    if t > sad + dt.timedelta(minutes=5):
        raise Greska("Vrijeme je u budućnosti.")
    if t < sad - dt.timedelta(days=7):
        raise Greska("Može se upisati najviše 7 dana unatrag.")
    return t.strftime("%Y-%m-%d %H:%M:00")


# ---------------------------------------------------------------- Uređaji i plan

def spremi_uredjaj(con, p):
    naziv = _tekst(p, "naziv", 60, "Upišite naziv uređaja (npr. Hladnjak mliječni).")
    vrsta = p.get("vrsta")
    if vrsta not in VRSTE_UREDJAJA:
        raise Greska("Nepoznata vrsta uređaja.")
    z = VRSTE_UREDJAJA[vrsta]
    mn = _broj(p.get("min_t") if p.get("min_t") not in (None, "") else z[0], "Neispravna najniža temperatura.")
    mx = _broj(p.get("max_t") if p.get("max_t") not in (None, "") else z[1], "Neispravna najviša temperatura.")
    if mn >= mx:
        raise Greska("Najniža temperatura mora biti manja od najviše.")
    if vrsta == "zamrzivac" and mx > -18:
        raise Greska("Zamrznuta hrana mora biti na -18 °C ili hladnije.")
    aktivan = 0 if p.get("aktivan") in (0, False, "0") else 1
    uid = int(p.get("id") or 0)
    with con:
        if uid:
            con.execute("UPDATE haccp_uredjaji SET naziv = ?, vrsta = ?, min_t = ?, max_t = ?, aktivan = ? "
                        "WHERE id = ?", (naziv, vrsta, mn, mx, aktivan, uid))
            return uid
        return con.execute("INSERT INTO haccp_uredjaji (naziv, vrsta, min_t, max_t, aktivan) VALUES (?, ?, ?, ?, ?)",
                           (naziv, vrsta, mn, mx, aktivan)).lastrowid


def spremi_plan(con, p):
    podrucje = _tekst(p, "podrucje", 80, "Upišite što se čisti (npr. pod prodavaonice).")
    if p.get("ucestalost") not in UCESTALOST:
        raise Greska("Odaberite koliko često.")
    nacin = p.get("nacin") if p.get("nacin") in NACINI else NACINI[0]
    aktivan = 0 if p.get("aktivan") in (0, False, "0") else 1
    pid = int(p.get("id") or 0)
    vr = (podrucje, p["ucestalost"], nacin, _tekst(p, "sredstvo", 80), aktivan)
    with con:
        if pid:
            con.execute("UPDATE haccp_plan SET podrucje = ?, ucestalost = ?, nacin = ?, sredstvo = ?, aktivan = ? "
                        "WHERE id = ?", vr + (pid,))
            return pid
        return con.execute("INSERT INTO haccp_plan (podrucje, ucestalost, nacin, sredstvo, aktivan) "
                           "VALUES (?, ?, ?, ?, ?)", vr).lastrowid


# ---------------------------------------------------------------- Unosi

def upisi_temperaturu(con, p):
    tko = _tekst(p, "tko", 40, "Upišite tko je mjerio.")
    u = con.execute("SELECT * FROM haccp_uredjaji WHERE id = ? AND aktivan = 1",
                    (int(p.get("uredjaj_id") or 0),)).fetchone()
    if not u:
        raise Greska("Odaberite uređaj.")
    t = _broj(p.get("temperatura"), "Upišite temperaturu.")
    if not -40 <= t <= 50:
        raise Greska("Temperatura nije moguća za %s." % u["naziv"])
    mjera = _tekst(p, "mjera", 200)
    if not u["min_t"] <= t <= u["max_t"] and not mjera:
        raise Greska("Temperatura je izvan granica (%g do %g °C). Upišite korektivnu mjeru "
                     "(npr. roba premještena, serviser pozvan, roba povučena iz prodaje)."
                     % (u["min_t"], u["max_t"]))
    with con:
        return con.execute(
            """INSERT INTO haccp_temperature (uredjaj_id, vrijeme, upisano, temperatura, min_t, max_t, tko, mjera)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (u["id"], _vrijeme(p), sada(), t, u["min_t"], u["max_t"], tko, mjera)).lastrowid


def upisi_ciscenje(con, p):
    tko = _tekst(p, "tko", 40, "Upišite tko je čistio.")
    plan = None
    if p.get("plan_id"):
        plan = con.execute("SELECT * FROM haccp_plan WHERE id = ?", (int(p["plan_id"]),)).fetchone()
        if not plan:
            raise Greska("Nepoznata stavka plana čišćenja.")
    podrucje = plan["podrucje"] if plan else _tekst(p, "podrucje", 80, "Upišite što je očišćeno.")
    nacin = p.get("nacin") if p.get("nacin") in NACINI else (plan["nacin"] if plan else NACINI[0])
    sredstvo = _tekst(p, "sredstvo", 80) or (plan["sredstvo"] if plan else "")
    with con:
        return con.execute(
            """INSERT INTO haccp_ciscenje (plan_id, vrijeme, upisano, podrucje, nacin, sredstvo, tko)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (plan["id"] if plan else None, _vrijeme(p), sada(), podrucje, nacin, sredstvo, tko)).lastrowid


def upisi_prijem(con, p):
    tko = _tekst(p, "tko", 40, "Upišite tko je zaprimio robu.")
    dob = _tekst(p, "dobavljac", 120, "Upišite dobavljača.")
    roba = _tekst(p, "roba", 200, "Upišite koju robu.")
    t = None
    if str(p.get("temperatura") or "").strip():
        t = _broj(p.get("temperatura"), "Neispravna temperatura.")
    prihvaceno = 0 if p.get("prihvaceno") in (0, False, "0") else 1
    napomena = _tekst(p, "napomena", 200)
    if not prihvaceno and not napomena:
        raise Greska("Upišite zašto je roba odbijena.")
    with con:
        return con.execute(
            """INSERT INTO haccp_prijem (vrijeme, upisano, dobavljac, dokument, roba, temperatura,
                                         prihvaceno, napomena, tko)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_vrijeme(p), sada(), dob, _tekst(p, "dokument", 60), roba, t, prihvaceno, napomena, tko)).lastrowid


def ponisti(con, p):
    tablica = TABLICE.get(p.get("vrsta"))
    if not tablica:
        raise Greska("Nepoznata vrsta zapisa.")
    razlog = _tekst(p, "razlog", 120, "Upišite razlog poništenja (npr. krivi uređaj, krivo upisano).")
    with con:
        n = con.execute("UPDATE %s SET ponisteno = ? WHERE id = ? AND ponisteno = ''" % tablica,
                        (razlog, int(p.get("id") or 0))).rowcount
    if not n:
        raise Greska("Zapis ne postoji ili je već poništen.")


# ---------------------------------------------------------------- Pregled

def _rokovi_plana(con, danas):
    out = []
    for r in con.execute("SELECT * FROM haccp_plan WHERE aktivan = 1 ORDER BY ucestalost, podrucje"):
        zadnje = con.execute("SELECT MAX(vrijeme) FROM haccp_ciscenje WHERE plan_id = ? AND ponisteno = ''",
                             (r["id"],)).fetchone()[0]
        od = (danas - dt.timedelta(days=UCESTALOST[r["ucestalost"]] - 1)).isoformat()
        d = dict(r)
        d["zadnje"] = zadnje
        d["napravljeno"] = bool(zadnje and zadnje[:10] >= od)
        out.append(d)
    return out


def pregled(con, danas=None):
    danas = danas or dt.date.today()
    d = danas.isoformat()
    uredjaji = []
    for u in con.execute("SELECT * FROM haccp_uredjaji ORDER BY aktivan DESC, naziv"):
        x = dict(u)
        x["danas"] = [dict(r) for r in con.execute(
            """SELECT id, vrijeme, temperatura, tko, mjera, ponisteno FROM haccp_temperature
               WHERE uredjaj_id = ? AND substr(vrijeme, 1, 10) = ? ORDER BY vrijeme""", (u["id"], d))]
        uredjaji.append(x)
    return {
        "datum": d,
        "uredjaji": uredjaji,
        "plan": _rokovi_plana(con, danas),
        "ciscenje": [dict(r) for r in con.execute(
            "SELECT * FROM haccp_ciscenje WHERE substr(vrijeme, 1, 10) = ? ORDER BY vrijeme DESC", (d,))],
        "prijem": [dict(r) for r in con.execute(
            "SELECT * FROM haccp_prijem WHERE substr(vrijeme, 1, 10) >= ? ORDER BY vrijeme DESC",
            ((danas - dt.timedelta(days=6)).isoformat(),))],
        "vrste": {k: {"min": v[0], "max": v[1]} for k, v in VRSTE_UREDJAJA.items()},
        "nacini": list(NACINI),
    }


def upozorenja(con, danas=None):
    """Za Kontrolu i jutarnji pregled: što danas još nije upisano."""
    danas = danas or dt.date.today()
    d = danas.isoformat()
    bez = [r[0] for r in con.execute(
        """SELECT u.naziv FROM haccp_uredjaji u WHERE u.aktivan = 1 AND NOT EXISTS (
               SELECT 1 FROM haccp_temperature t WHERE t.uredjaj_id = u.id AND t.ponisteno = ''
               AND substr(t.vrijeme, 1, 10) = ?) ORDER BY u.naziv""", (d,))]
    kasni = [p["podrucje"] + " (" + p["ucestalost"].replace("mjesecno", "mjesečno") + ")"
             for p in _rokovi_plana(con, danas) if not p["napravljeno"]]
    nema_uredjaja = not con.execute("SELECT 1 FROM haccp_uredjaji WHERE aktivan = 1").fetchone()
    nema_plana = not con.execute("SELECT 1 FROM haccp_plan WHERE aktivan = 1").fetchone()
    return {"bez_temperature": bez, "ciscenje_kasni": kasni,
            "nema_uredjaja": nema_uredjaja, "nema_plana": nema_plana}


def mjesec(con, mj):
    """Svi zapisi za mjesec 'YYYY-MM' (za ispis)."""
    try:
        dt.date.fromisoformat(mj + "-01")
    except (TypeError, ValueError):
        raise Greska("Neispravan mjesec.")
    return {
        "mjesec": mj,
        "temperature": [dict(r) for r in con.execute(
            """SELECT t.*, u.naziv AS uredjaj FROM haccp_temperature t
               JOIN haccp_uredjaji u ON u.id = t.uredjaj_id
               WHERE substr(t.vrijeme, 1, 7) = ? ORDER BY u.naziv, t.vrijeme""", (mj,))],
        "ciscenje": [dict(r) for r in con.execute(
            "SELECT * FROM haccp_ciscenje WHERE substr(vrijeme, 1, 7) = ? ORDER BY vrijeme", (mj,))],
        "prijem": [dict(r) for r in con.execute(
            "SELECT * FROM haccp_prijem WHERE substr(vrijeme, 1, 7) = ? ORDER BY vrijeme", (mj,))],
        "plan": [dict(r) for r in con.execute(
            "SELECT * FROM haccp_plan WHERE aktivan = 1 ORDER BY ucestalost, podrucje")],
    }
