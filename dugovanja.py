# -*- coding: utf-8 -*-
"""
Računi dobavljača i dugovanja.

Svaka primka je račun dobavljača (broj računa, iznos s PDV-om). Ovdje se
upisuje kad dospijeva i kad je plaćeno, pa se vidi koliko se duguje kome i
što kasni. Povrat dobavljaču umanjuje dug prema tom dobavljaču.

Propisi (provjereno 23. 10. 2026.):
- Zakon o financijskom poslovanju i predstečajnoj nagodbi: između poduzetnika
  rok plaćanja može se ugovoriti do 60 dana (dulje samo uz pisani ugovor o
  robnom kreditu, najviše 360); ako rok nije ugovoren, plaća se u 30 dana od
  primitka računa. Zadani rok ovdje je 30 dana, za dobavljača se može promijeniti.
- Zakon o sprječavanju pranja novca i financiranja terorizma: plaćanje
  gotovinom od 10.000 EUR ili više je zabranjeno (i podijeljeno na više uplata).
"""

import datetime as dt

import postavke
from baza import zaokruzi
from nabava import Greska, oznaka, sada

SHEMA = """
CREATE TABLE IF NOT EXISTS dob_uvjeti (
    partner_id  INTEGER PRIMARY KEY REFERENCES partneri(id),
    rok_dana    INTEGER NOT NULL DEFAULT 30
);
CREATE TABLE IF NOT EXISTS dob_dospijece (
    dokument_id INTEGER PRIMARY KEY REFERENCES dokumenti(id),
    dospijece   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dob_placanja (
    id          INTEGER PRIMARY KEY,
    dokument_id INTEGER NOT NULL REFERENCES dokumenti(id),
    datum       TEXT NOT NULL,
    iznos       INTEGER NOT NULL,           -- centi
    nacin       TEXT NOT NULL CHECK (nacin IN ('virman', 'gotovina', 'kartica', 'kompenzacija')),
    napomena    TEXT NOT NULL DEFAULT '',
    upisano     TEXT NOT NULL,
    ponisteno   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_dob_placanja ON dob_placanja(dokument_id);
"""
ZADANI_ROK = 30
NAJDULJI_ROK = 60
GOTOVINA_ZABRANA = 1000000   # centi: 10.000 EUR


def pripremi(con):
    con.executescript(SHEMA)


def _rok(con, partner_id):
    r = con.execute("SELECT rok_dana FROM dob_uvjeti WHERE partner_id = ?", (partner_id,)).fetchone()
    return r[0] if r else ZADANI_ROK


def spremi_rok(con, p):
    try:
        pid, rok = int(p.get("partner_id")), int(p.get("rok_dana"))
    except (TypeError, ValueError):
        raise Greska("Neispravan rok.")
    if not con.execute("SELECT 1 FROM partneri WHERE id = ?", (pid,)).fetchone():
        raise Greska("Nepoznat dobavljač.")
    if not 0 <= rok <= 360:
        raise Greska("Rok 0 do 360 dana.")
    with con:
        con.execute("INSERT OR REPLACE INTO dob_uvjeti (partner_id, rok_dana) VALUES (?, ?)", (pid, rok))


def pocetak(con):
    """Od kojeg datuma računa se prate dugovanja (starije primke su plaćene prije programa)."""
    od = postavke.vrijednost(con, "dugovanja.od")
    if not od:
        od = dt.date.today().replace(day=1).isoformat()
        postavke.postavi(con, "dugovanja.od", od)
    return od


def spremi_pocetak(con, od):
    try:
        od = dt.date.fromisoformat(str(od)).isoformat()
    except ValueError:
        raise Greska("Neispravan datum.")
    postavke.postavi(con, "dugovanja.od", od)


def _racuni(con, partner_id=None):
    """Sve primke (nestornirane) od početka praćenja, s dospijećem i plaćenim iznosom."""
    sql = """SELECT d.*, p.naziv AS partner, dd.dospijece AS dosp,
                    COALESCE((SELECT SUM(iznos) FROM dob_placanja x WHERE x.dokument_id = d.id
                              AND x.ponisteno = ''), 0) AS placeno
             FROM dokumenti d LEFT JOIN partneri p ON p.id = d.partner_id
             LEFT JOIN dob_dospijece dd ON dd.dokument_id = d.id
             WHERE d.vrsta = 'primka' AND d.storno_od IS NULL AND d.storniran = 0 AND d.datum >= ?"""
    args = [pocetak(con)]
    if partner_id:
        sql += " AND d.partner_id = ?"
        args.append(partner_id)
    out = []
    danas = dt.date.today().isoformat()
    for r in con.execute(sql + " ORDER BY d.datum, d.id", args):
        x = dict(r)
        x["oznaka"] = oznaka(r)
        x["ukupno"] = r["neto"] + r["pdv"]
        x["dospijece"] = r["dosp"] or (dt.date.fromisoformat(r["datum"]) +
                                        dt.timedelta(days=_rok(con, r["partner_id"]))).isoformat()
        x["ostalo"] = x["ukupno"] - r["placeno"]
        x["kasni"] = x["ostalo"] > 0 and x["dospijece"] < danas
        x["dana_roka"] = (dt.date.fromisoformat(x["dospijece"]) - dt.date.fromisoformat(r["datum"])).days
        out.append(x)
    return out


def pregled(con, samo_otvoreni=True):
    racuni = _racuni(con)
    povrati = {}
    for r in con.execute("""SELECT partner_id, SUM(neto + pdv) FROM dokumenti
                            WHERE vrsta = 'povrat' AND storno_od IS NULL AND storniran = 0 AND datum >= ?
                            GROUP BY partner_id""", (pocetak(con),)):
        povrati[r[0]] = r[1]
    po_dob = {}
    for x in racuni:
        d = po_dob.setdefault(x["partner_id"], {"partner_id": x["partner_id"], "partner": x["partner"] or "—",
                                                "duguje": 0, "kasni": 0, "racuna": 0,
                                                "rok_dana": _rok(con, x["partner_id"])})
        if x["ostalo"] > 0:
            d["duguje"] += x["ostalo"]
            d["racuna"] += 1
            if x["kasni"]:
                d["kasni"] += x["ostalo"]
    for pid, v in povrati.items():
        if pid in po_dob:
            po_dob[pid]["povrati"] = v
    dob = sorted(po_dob.values(), key=lambda d: -d["duguje"])
    for d in dob:
        d["saldo"] = d["duguje"] - d.get("povrati", 0)
    return {"dobavljaci": [d for d in dob if d["duguje"] or not samo_otvoreni],
            "racuni": [x for x in racuni if x["ostalo"] > 0 or not samo_otvoreni],
            "ukupno": sum(x["ostalo"] for x in racuni if x["ostalo"] > 0),
            "kasni": sum(x["ostalo"] for x in racuni if x["kasni"]), "od": pocetak(con)}


def racun(con, did):
    x = next((r for r in _racuni(con) if r["id"] == did), None)
    if not x:
        raise Greska("Primka ne postoji ili je stornirana.")
    x["placanja"] = [dict(r) for r in con.execute(
        "SELECT * FROM dob_placanja WHERE dokument_id = ? ORDER BY datum, id", (did,))]
    return x


def spremi_dospijece(con, p):
    did = int(p.get("dokument_id") or 0)
    x = racun(con, did)
    try:
        dosp = dt.date.fromisoformat(str(p.get("dospijece") or "")).isoformat()
    except ValueError:
        raise Greska("Neispravan datum dospijeća.")
    if dosp < x["datum"]:
        raise Greska("Dospijeće ne može biti prije datuma računa.")
    with con:
        con.execute("INSERT OR REPLACE INTO dob_dospijece (dokument_id, dospijece) VALUES (?, ?)", (did, dosp))
    return racun(con, did)


def plati(con, p):
    did = int(p.get("dokument_id") or 0)
    nacin = p.get("nacin")
    if nacin not in ("virman", "gotovina", "kartica", "kompenzacija"):
        raise Greska("Odaberite način plaćanja.")
    try:
        iznos = zaokruzi(float(str(p.get("iznos") or "").replace(" ", "").replace(",", ".")) * 100)
        datum = dt.date.fromisoformat(str(p.get("datum") or dt.date.today().isoformat())).isoformat()
    except ValueError:
        raise Greska("Neispravan iznos ili datum.")
    if datum > dt.date.today().isoformat():
        raise Greska("Datum plaćanja je u budućnosti.")
    con.execute("BEGIN IMMEDIATE")
    try:
        x = racun(con, did)
        if not 0 < iznos <= x["ostalo"]:
            raise Greska("Za platiti je ostalo %s EUR." % ("%.2f" % (x["ostalo"] / 100.0)).replace(".", ","))
        if nacin == "gotovina":
            gotovina = sum(pl["iznos"] for pl in x["placanja"] if pl["nacin"] == "gotovina" and not pl["ponisteno"])
            if gotovina + iznos >= GOTOVINA_ZABRANA:
                raise Greska("Plaćanje gotovinom od 10.000 EUR ili više je zabranjeno, i kad se podijeli "
                             "na više uplata (Zakon o sprječavanju pranja novca). Platite virmanom.")
        con.execute("""INSERT INTO dob_placanja (dokument_id, datum, iznos, nacin, napomena, upisano)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (did, datum, iznos, nacin, " ".join(str(p.get("napomena") or "").split())[:120], sada()))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return racun(con, did)


def ponisti_placanje(con, p):
    razlog = " ".join(str(p.get("razlog") or "").split())[:120]
    if not razlog:
        raise Greska("Upišite razlog poništenja.")
    with con:
        if not con.execute("UPDATE dob_placanja SET ponisteno = ? WHERE id = ? AND ponisteno = ''",
                           (razlog, int(p.get("id") or 0))).rowcount:
            raise Greska("Plaćanje ne postoji ili je već poništeno.")


def upozorenja(con, danas=None):
    danas = danas or dt.date.today()
    za3 = (danas + dt.timedelta(days=3)).isoformat()
    r = [x for x in _racuni(con) if x["ostalo"] > 0]
    return {"kasni": [x for x in r if x["kasni"]],
            "uskoro": [x for x in r if not x["kasni"] and x["dospijece"] <= za3],
            "predug_rok": [x for x in r if x["dana_roka"] > NAJDULJI_ROK]}
