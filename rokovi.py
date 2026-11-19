# -*- coding: utf-8 -*-
"""
Rokovi trajanja i kalo.

Rokovi: za pakiranu robu s rokom (jogurt, sir, sokovi, orašasti...) upiše
se količina i datum s ambalaže. Program upozorava prije isteka, da se roba
stavi na akciju, i kad istekne.

Propisi (Uredba (EU) 1169/2011 čl. 24; Ministarstvo poljoprivrede, Vodič za
označavanje rokova trajanja hrane, pogl. 5.1, provjereno 23. 10. 2026.):
- "Upotrijebiti do" (lako kvarljiva hrana): nakon tog datuma hrana se NE SMIJE
  prodavati ni donirati. Mora s police i u otpis.
- "Najbolje upotrijebiti do": hrana se smije prodavati i nakon isteka ako je
  sigurna, ali IZDVOJENO od ostale robe i uz jasnu obavijest kupcima da joj je
  rok istekao. Smije se i donirati.

Kalo: prirodni gubitak (sušenje, kvarenje) voća i povrća. Pregled po artiklu
uspoređuje otpis i inventurni manjak s nabavljenom količinom. Manjak iznad
dopuštenog kala knjigovodstveno se tretira kao prodaja (PDV), pa se postotak
dogovara s knjigovođom (HGK preporuke za voće i povrće okvirno 2 - 6 %).
"""

import datetime as dt

from baza import zaokruzi
from nabava import Greska, sada

VRSTE = {"upotrijebiti": "Upotrijebiti do", "najbolje": "Najbolje upotrijebiti do"}
UPOZORI_DANA = 3   # koliko dana prije isteka počinje upozorenje

SHEMA = """
CREATE TABLE IF NOT EXISTS rokovi (
    id         INTEGER PRIMARY KEY,
    artikl_id  INTEGER NOT NULL REFERENCES artikli(id),
    kolicina   REAL NOT NULL,
    vrsta      TEXT NOT NULL CHECK (vrsta IN ('upotrijebiti', 'najbolje')),
    rok        TEXT NOT NULL,                 -- datum s ambalaže
    upisano    TEXT NOT NULL,
    napomena   TEXT NOT NULL DEFAULT '',
    zatvoreno  TEXT NOT NULL DEFAULT '',      -- prodano / otpisano / izdvojeno / doniranje
    zatvoreno_vrijeme TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_rokovi_rok ON rokovi(rok);
"""
ZATVARANJE = {"prodano": "Prodano", "otpisano": "Otpisano", "doniranje": "Donirano",
              "izdvojeno": "Izdvojeno i označeno (istekao 'najbolje upotrijebiti do')"}


def pripremi(con):
    con.executescript(SHEMA)


def upisi(con, p):
    try:
        aid = int(p.get("artikl_id") or 0)
        kol = round(float(str(p.get("kolicina") or "").replace(",", ".")), 3)
        rok = dt.date.fromisoformat(str(p.get("rok") or "")).isoformat()
    except ValueError:
        raise Greska("Upišite artikl, količinu i datum roka.")
    a = con.execute("SELECT naziv FROM artikli WHERE id = ?", (aid,)).fetchone()
    if not a:
        raise Greska("Odaberite artikl.")
    if not 0 < kol <= 100000:
        raise Greska("Neispravna količina.")
    vrsta = p.get("vrsta")
    if vrsta not in VRSTE:
        raise Greska("Odaberite što piše na ambalaži: \"upotrijebiti do\" ili \"najbolje upotrijebiti do\".")
    if rok < (dt.date.today() - dt.timedelta(days=365)).isoformat():
        raise Greska("Datum roka je prestar.")
    with con:
        return con.execute(
            "INSERT INTO rokovi (artikl_id, kolicina, vrsta, rok, upisano, napomena) VALUES (?, ?, ?, ?, ?, ?)",
            (aid, kol, vrsta, rok, sada(), " ".join(str(p.get("napomena") or "").split())[:120])).lastrowid


def zatvori(con, p):
    kako = p.get("kako")
    if kako not in ZATVARANJE:
        raise Greska("Nepoznato.")
    r = con.execute("SELECT * FROM rokovi WHERE id = ? AND zatvoreno = ''", (int(p.get("id") or 0),)).fetchone()
    if not r:
        raise Greska("Zapis ne postoji ili je već zatvoren.")
    danas = dt.date.today().isoformat()
    if r["vrsta"] == "upotrijebiti" and r["rok"] < danas and kako in ("prodano", "izdvojeno", "doniranje"):
        raise Greska("Istekao rok \"upotrijebiti do\": roba se ne smije prodavati ni donirati, samo otpisati.")
    if kako == "izdvojeno" and r["vrsta"] != "najbolje":
        raise Greska("Izdvojiti se smije samo roba s rokom \"najbolje upotrijebiti do\".")
    with con:
        con.execute("UPDATE rokovi SET zatvoreno = ?, zatvoreno_vrijeme = ? WHERE id = ?", (kako, sada(), r["id"]))


def popis(con, danas=None):
    danas = danas or dt.date.today()
    d = danas.isoformat()
    upoz = (danas + dt.timedelta(days=UPOZORI_DANA)).isoformat()
    out = []
    for r in con.execute(
            """SELECT r.*, a.naziv, a.jm, a.sifra FROM rokovi r JOIN artikli a ON a.id = r.artikl_id
               WHERE r.zatvoreno = '' OR (r.zatvoreno = 'izdvojeno')
               ORDER BY r.rok, a.naziv"""):
        x = dict(r)
        x["dana"] = (dt.date.fromisoformat(r["rok"]) - danas).days
        if r["zatvoreno"] == "izdvojeno":
            x["stanje"] = "izdvojeno"
        elif r["rok"] < d:
            x["stanje"] = "istekao_zabrana" if r["vrsta"] == "upotrijebiti" else "istekao_izdvojiti"
        elif r["rok"] <= upoz:
            x["stanje"] = "uskoro"
        else:
            x["stanje"] = "ok"
        out.append(x)
    return out


def upozorenja(con, danas=None):
    p = popis(con, danas)
    return {"zabrana": [x for x in p if x["stanje"] == "istekao_zabrana"],
            "izdvojiti": [x for x in p if x["stanje"] == "istekao_izdvojiti"],
            "uskoro": [x for x in p if x["stanje"] == "uskoro"]}


# ---------------------------------------------------------------- Kalo

def kalo(con, od, do, norma=None):
    """Po artiklu: nabavljeno, otpisano, inventurni manjak/višak, gubitak u % od nabave."""
    a = (od, do)
    nab, otp, inv = {}, {}, {}
    for r in con.execute(
            """SELECT s.artikl_id, d.vrsta, SUM(s.kolicina), SUM(s.iznos) FROM dok_stavke s
               JOIN dokumenti d ON d.id = s.dokument_id
               WHERE d.datum BETWEEN ? AND ? GROUP BY s.artikl_id, d.vrsta""", a):
        cilj = {"primka": nab, "otpis": otp, "inventura": inv}.get(r[1])
        if cilj is not None:
            cilj[r[0]] = (r[2], r[3])
    out = []
    for aid in set(nab) | set(otp) | set(inv):
        art = con.execute("SELECT naziv, jm, nabavna FROM artikli WHERE id = ?", (aid,)).fetchone()
        n = nab.get(aid, (0, 0))[0]
        o = otp.get(aid, (0, 0))[0]
        i = inv.get(aid, (0, 0))[0]          # višak +, manjak -
        gubitak = o + max(0.0, -i)
        if not gubitak:
            continue
        posto = round(gubitak * 100.0 / n, 1) if n > 0 else None
        out.append({"artikl_id": aid, "naziv": art["naziv"], "jm": art["jm"], "nabavljeno": round(n, 3),
                    "otpisano": round(o, 3), "inventura": round(i, 3), "gubitak": round(gubitak, 3),
                    "posto": posto, "vrijednost": zaokruzi(gubitak * art["nabavna"]),
                    "iznad": bool(norma) and (posto is None or posto > norma)})
    out.sort(key=lambda x: -x["vrijednost"])
    uk_n = sum(v[1] for v in nab.values())
    return {"od": od, "do": do, "norma": norma, "stavke": out,
            "nabava": uk_n, "gubitak": sum(x["vrijednost"] for x in out)}
