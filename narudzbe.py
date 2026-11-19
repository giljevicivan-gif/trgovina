# -*- coding: utf-8 -*-
"""
Narudžbe kupaca (restorani, škole, košare voća...) i usluge (dostava, pakiranje).

Narudžba NIJE račun. Kad se roba isporuči, račun se izdaje i fiskalizira
u programu Roba (za tvrtke R1 / eRačun), a ovdje se upiše broj tog računa.
Narudžba ne mijenja zalihu: prodaja se broji iz Robe, kao i sve ostalo.

Statusi: nova -> spremna -> isporucena, ili otkazana. Ništa se ne briše.
Stavke se mogu mijenjati dok je narudžba "nova" ili "spremna".
"""

import datetime as dt

from baza import oib_ispravan, pdv_iz_bruto, zaokruzi
from nabava import Greska, sada

STATUSI = ("nova", "spremna", "isporucena", "otkazana")
OTVORENE = ("nova", "spremna")

SHEMA = """
CREATE TABLE IF NOT EXISTS kupci (
    id        INTEGER PRIMARY KEY,
    naziv     TEXT NOT NULL,
    oib       TEXT NOT NULL DEFAULT '',
    adresa    TEXT NOT NULL DEFAULT '',
    mjesto    TEXT NOT NULL DEFAULT '',
    telefon   TEXT NOT NULL DEFAULT '',
    email     TEXT NOT NULL DEFAULT '',
    napomena  TEXT NOT NULL DEFAULT '',
    aktivan   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS narudzbe (
    id          INTEGER PRIMARY KEY,
    godina      INTEGER NOT NULL,
    broj        INTEGER NOT NULL,
    kupac_id    INTEGER NOT NULL REFERENCES kupci(id),
    vrijeme     TEXT NOT NULL,                 -- kad je upisana
    rok         TEXT NOT NULL,                 -- datum isporuke
    status      TEXT NOT NULL DEFAULT 'nova'
                CHECK (status IN ('nova', 'spremna', 'isporucena', 'otkazana')),
    ukupno      INTEGER NOT NULL DEFAULT 0,    -- s PDV-om, centi (informativno)
    napomena    TEXT NOT NULL DEFAULT '',
    racun_roba  TEXT NOT NULL DEFAULT '',      -- broj fiskalnog računa iz Robe
    promijenjena TEXT NOT NULL DEFAULT '',
    UNIQUE (godina, broj)
);
CREATE TABLE IF NOT EXISTS narudzba_stavke (
    id            INTEGER PRIMARY KEY,
    narudzba_id   INTEGER NOT NULL REFERENCES narudzbe(id),
    artikl_id     INTEGER NOT NULL REFERENCES artikli(id),
    naziv         TEXT NOT NULL,
    jm            TEXT NOT NULL,
    kolicina      REAL NOT NULL,
    cijena        INTEGER NOT NULL,   -- MPC s PDV-om u trenutku upisa
    popust        REAL NOT NULL DEFAULT 0,
    iznos         INTEGER NOT NULL,
    pdv_stopa     REAL NOT NULL,
    usluga        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_narudzba_stavke ON narudzba_stavke(narudzba_id);
"""


def pripremi(con):
    con.executescript(SHEMA)


def oznaka(r):
    return "N-%d-%d" % (r["godina"], r["broj"])


# ---------------------------------------------------------------- Kupci

def kupci(con, svi=False):
    sql = "SELECT * FROM kupci" + ("" if svi else " WHERE aktivan = 1") + " ORDER BY naziv"
    return [dict(r) for r in con.execute(sql)]


def spremi_kupca(con, p):
    """Novi ili izmijenjeni kupac. Za građane dovoljno je ime i telefon. Vraća id."""
    naziv = " ".join(str(p.get("naziv") or "").split())[:120]
    if not naziv:
        raise Greska("Upišite naziv kupca.")
    oib = str(p.get("oib") or "").strip()
    if oib and not oib_ispravan(oib):
        raise Greska("OIB nije ispravan (11 znamenki, kontrolna znamenka se ne slaže).")
    polja = {k: str(p.get(k) or "").strip()[:200]
             for k in ("adresa", "mjesto", "telefon", "email", "napomena")}
    aktivan = 0 if p.get("aktivan") in (0, False, "0") else 1
    kid = int(p.get("id") or 0)
    if oib and con.execute("SELECT 1 FROM kupci WHERE oib = ? AND id != ?", (oib, kid)).fetchone():
        raise Greska("Kupac s tim OIB-om već postoji.")
    vrijednosti = (naziv, oib, polja["adresa"], polja["mjesto"], polja["telefon"], polja["email"],
                   polja["napomena"], aktivan)
    with con:
        if kid:
            if not con.execute(
                    """UPDATE kupci SET naziv = ?, oib = ?, adresa = ?, mjesto = ?, telefon = ?,
                       email = ?, napomena = ?, aktivan = ? WHERE id = ?""",
                    vrijednosti + (kid,)).rowcount:
                raise Greska("Kupac ne postoji.")
            return kid
        return con.execute(
            """INSERT INTO kupci (naziv, oib, adresa, mjesto, telefon, email, napomena, aktivan)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", vrijednosti).lastrowid


# ---------------------------------------------------------------- Narudžbe

def popis(con, samo_otvorene=True, mjesec=None):
    sql = """SELECT n.*, k.naziv AS kupac,
                    (SELECT COUNT(*) FROM narudzba_stavke s WHERE s.narudzba_id = n.id) AS stavki
             FROM narudzbe n JOIN kupci k ON k.id = n.kupac_id"""
    args = []
    if samo_otvorene:
        sql += " WHERE n.status IN ('nova', 'spremna') ORDER BY n.rok, n.id"
    else:
        sql += " WHERE substr(n.rok, 1, 7) = ? ORDER BY n.rok DESC, n.id DESC"
        args.append(mjesec or dt.date.today().isoformat()[:7])
    out = []
    for r in con.execute(sql, args):
        d = dict(r)
        d["oznaka"] = oznaka(r)
        out.append(d)
    return out


def narudzba(con, nid):
    r = con.execute("""SELECT n.*, k.naziv AS kupac, k.oib AS kupac_oib, k.adresa AS kupac_adresa,
                              k.mjesto AS kupac_mjesto, k.telefon AS kupac_telefon
                       FROM narudzbe n JOIN kupci k ON k.id = n.kupac_id WHERE n.id = ?""",
                    (nid,)).fetchone()
    if not r:
        raise Greska("Narudžba ne postoji.")
    d = dict(r)
    d["oznaka"] = oznaka(r)
    d["stavke"] = [dict(s) for s in con.execute(
        "SELECT * FROM narudzba_stavke WHERE narudzba_id = ? ORDER BY id", (nid,))]
    pdv = {}
    for s in d["stavke"]:
        pdv[s["pdv_stopa"]] = pdv.get(s["pdv_stopa"], 0) + s["iznos"]
    d["pdv"] = [{"stopa": k, "osnovica": v - pdv_iz_bruto(v, k), "pdv": pdv_iz_bruto(v, k)}
                for k, v in sorted(pdv.items())]
    return d


def _stavke(con, stavke, kupac_id=None):
    if not stavke:
        raise Greska("Narudžba nema stavki.")
    import kupci_tvrtke
    _, ugovorene = kupci_tvrtke.uvjeti(con, kupac_id)
    out = []
    for s in stavke:
        a = con.execute("SELECT * FROM artikli WHERE id = ?", (int(s["artikl_id"]),)).fetchone()
        if not a:
            raise Greska("Artikl ne postoji.")
        try:
            kol = round(float(s["kolicina"]), 3)
            popust = round(float(s.get("popust") or 0), 2)
        except (TypeError, ValueError, KeyError):
            raise Greska("Neispravna količina za %s." % a["naziv"])
        if not 0 < kol <= 100000:
            raise Greska("Neispravna količina za %s." % a["naziv"])
        if not 0 <= popust <= 100:
            raise Greska("Popust mora biti između 0 i 100 %.")
        # zaokruživanje kao na ekranu (pola centa gore), da se iznosi slažu
        cijena = ugovorene.get(a["id"], a["cijena"])  # ugovorena cijena s kupcem, inače MPC
        iznos = zaokruzi(cijena * kol * (1 - popust / 100.0))
        out.append((a["id"], a["naziv"], a["jm"], kol, cijena, popust, iznos,
                    a["pdv_stopa"], a["usluga"]))
    return out


def spremi(con, p):
    """Nova narudžba ili izmjena otvorene. Vraća id."""
    try:
        kupac_id = int(p.get("kupac_id"))
    except (TypeError, ValueError):
        raise Greska("Odaberite kupca.")
    if not con.execute("SELECT 1 FROM kupci WHERE id = ?", (kupac_id,)).fetchone():
        raise Greska("Nepoznat kupac.")
    try:
        rok = dt.date.fromisoformat(str(p.get("rok") or "")).isoformat()
    except ValueError:
        raise Greska("Upišite datum isporuke.")
    stavke = _stavke(con, p.get("stavke") or [], kupac_id)
    ukupno = sum(s[6] for s in stavke)
    napomena = str(p.get("napomena") or "").strip()[:300]
    narudzbenica = " ".join(str(p.get("narudzbenica") or "").split())[:60]
    nid = int(p.get("id") or 0)
    con.execute("BEGIN IMMEDIATE")
    try:
        if nid:
            r = con.execute("SELECT status FROM narudzbe WHERE id = ?", (nid,)).fetchone()
            if not r:
                raise Greska("Narudžba ne postoji.")
            if r["status"] not in OTVORENE:
                raise Greska("Isporučena ili otkazana narudžba se ne mijenja.")
            con.execute("""UPDATE narudzbe SET kupac_id = ?, rok = ?, ukupno = ?, napomena = ?,
                           promijenjena = ?, narudzbenica = ? WHERE id = ?""",
                        (kupac_id, rok, ukupno, napomena, sada(), narudzbenica, nid))
            con.execute("DELETE FROM narudzba_stavke WHERE narudzba_id = ?", (nid,))
        else:
            godina = dt.date.today().year
            broj = con.execute("SELECT COALESCE(MAX(broj), 0) + 1 FROM narudzbe WHERE godina = ?",
                               (godina,)).fetchone()[0]
            nid = con.execute(
                """INSERT INTO narudzbe (godina, broj, kupac_id, vrijeme, rok, ukupno, napomena, narudzbenica)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (godina, broj, kupac_id, sada(), rok, ukupno, napomena, narudzbenica)).lastrowid
        con.executemany(
            """INSERT INTO narudzba_stavke (narudzba_id, artikl_id, naziv, jm, kolicina, cijena,
                                            popust, iznos, pdv_stopa, usluga)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", [(nid,) + s for s in stavke])
        con.execute("COMMIT")
        return nid
    except Exception:
        con.execute("ROLLBACK")
        raise


def status(con, nid, p):
    """Promjena statusa. Za "isporucena" se upisuje broj računa iz Robe."""
    novi = p.get("status")
    if novi not in STATUSI:
        raise Greska("Nepoznat status.")
    r = con.execute("SELECT status FROM narudzbe WHERE id = ?", (nid,)).fetchone()
    if not r:
        raise Greska("Narudžba ne postoji.")
    # isporučenoj se smije naknadno upisati broj računa iz Robe, ništa drugo
    if r["status"] not in OTVORENE and not (r["status"] == novi == "isporucena"):
        raise Greska("Narudžba je već zatvorena.")
    racun = str(p.get("racun_roba") or "").strip()[:60]
    stari = con.execute("SELECT racun_roba FROM narudzbe WHERE id = ?", (nid,)).fetchone()[0]
    if r["status"] == "isporucena" and stari and racun != stari:
        raise Greska("Broj računa iz Robe je već upisan (%s) i ne mijenja se." % stari)
    with con:
        con.execute("UPDATE narudzbe SET status = ?, racun_roba = ?, promijenjena = ? WHERE id = ?",
                    (novi, racun, sada(), nid))


def rezervirano(con, do_datuma):
    """Količine iz otvorenih narudžbi s rokom do zadanog datuma: {artikl_id: kolicina}."""
    return {r[0]: r[1] for r in con.execute(
        """SELECT s.artikl_id, SUM(s.kolicina) FROM narudzba_stavke s
           JOIN narudzbe n ON n.id = s.narudzba_id
           WHERE n.status IN ('nova', 'spremna') AND n.rok <= ? AND s.usluga = 0
           GROUP BY s.artikl_id""", (do_datuma,))}
