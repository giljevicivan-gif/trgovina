# -*- coding: utf-8 -*-
"""
Ponude kupcima (škole, firme, ustanove) i broj narudžbenice kupca.

Ponuda NIJE račun ni obveza isporuke: ispisuje se kupcu s rokom važenja.
Kad je kupac prihvati, jednim klikom postaje narudžba (iste stavke i cijene),
a dalje sve ide kao i do sada: otpremnica, račun u Robi.

Ustanove (domovi zdravlja, škole) često šalju svoju narudžbenicu s brojem koji
mora pisati na otpremnici i računu; zato narudžba ima polje "broj narudžbenice
kupca", koje se ispisuje na narudžbi, otpremnici i zbirnom pregledu. Taj broj
upišite i na račun u Robi (napomena), jer ga kupac traži za plaćanje.
"""

import datetime as dt

import narudzbe
from baza import pdv_iz_bruto
from nabava import Greska, sada

STATUSI = ("otvorena", "prihvacena", "odbijena")
ROK_DANA = 15

SHEMA = """
CREATE TABLE IF NOT EXISTS ponude (
    id           INTEGER PRIMARY KEY,
    godina       INTEGER NOT NULL,
    broj         INTEGER NOT NULL,
    kupac_id     INTEGER NOT NULL REFERENCES kupci(id),
    datum        TEXT NOT NULL,
    vrijedi_do   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'otvorena' CHECK (status IN ('otvorena', 'prihvacena', 'odbijena')),
    ukupno       INTEGER NOT NULL DEFAULT 0,
    napomena     TEXT NOT NULL DEFAULT '',
    narudzba_id  INTEGER REFERENCES narudzbe(id),
    promijenjena TEXT NOT NULL DEFAULT '',
    UNIQUE (godina, broj)
);
CREATE TABLE IF NOT EXISTS ponuda_stavke (
    id         INTEGER PRIMARY KEY,
    ponuda_id  INTEGER NOT NULL REFERENCES ponude(id),
    artikl_id  INTEGER NOT NULL REFERENCES artikli(id),
    naziv      TEXT NOT NULL,
    jm         TEXT NOT NULL,
    kolicina   REAL NOT NULL,
    cijena     INTEGER NOT NULL,
    popust     REAL NOT NULL DEFAULT 0,
    iznos      INTEGER NOT NULL,
    pdv_stopa  REAL NOT NULL,
    usluga     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_ponuda_stavke ON ponuda_stavke(ponuda_id);
"""


def pripremi(con):
    con.executescript(SHEMA)
    stupci = {r[1] for r in con.execute("PRAGMA table_info(narudzbe)")}
    if "narudzbenica" not in stupci:
        con.execute("ALTER TABLE narudzbe ADD COLUMN narudzbenica TEXT NOT NULL DEFAULT ''")
        con.commit()


def oznaka(r):
    return "PON-%d-%d" % (r["godina"], r["broj"])


def popis(con, sve=False):
    sql = """SELECT p.*, k.naziv AS kupac FROM ponude p JOIN kupci k ON k.id = p.kupac_id"""
    sql += "" if sve else " WHERE p.status = 'otvorena'"
    danas = dt.date.today().isoformat()
    out = []
    for r in con.execute(sql + " ORDER BY p.id DESC LIMIT 300"):
        d = dict(r)
        d["oznaka"] = oznaka(r)
        d["istekla"] = r["status"] == "otvorena" and r["vrijedi_do"] < danas
        out.append(d)
    return out


def ponuda(con, pid):
    r = con.execute("""SELECT p.*, k.naziv AS kupac, k.oib AS kupac_oib, k.adresa AS kupac_adresa,
                              k.mjesto AS kupac_mjesto, k.telefon AS kupac_telefon
                       FROM ponude p JOIN kupci k ON k.id = p.kupac_id WHERE p.id = ?""", (pid,)).fetchone()
    if not r:
        raise Greska("Ponuda ne postoji.")
    d = dict(r)
    d["oznaka"] = oznaka(r)
    d["stavke"] = [dict(s) for s in con.execute(
        "SELECT * FROM ponuda_stavke WHERE ponuda_id = ? ORDER BY id", (pid,))]
    pdv = {}
    for s in d["stavke"]:
        pdv[s["pdv_stopa"]] = pdv.get(s["pdv_stopa"], 0) + s["iznos"]
    d["pdv"] = [{"stopa": k, "osnovica": v - pdv_iz_bruto(v, k), "pdv": pdv_iz_bruto(v, k)}
                for k, v in sorted(pdv.items())]
    if r["narudzba_id"]:
        n = con.execute("SELECT godina, broj FROM narudzbe WHERE id = ?", (r["narudzba_id"],)).fetchone()
        d["narudzba"] = narudzbe.oznaka(n) if n else ""
    return d


def spremi(con, p):
    try:
        kupac_id = int(p.get("kupac_id"))
    except (TypeError, ValueError):
        raise Greska("Odaberite kupca.")
    if not con.execute("SELECT 1 FROM kupci WHERE id = ?", (kupac_id,)).fetchone():
        raise Greska("Nepoznat kupac.")
    danas = dt.date.today()
    try:
        do = dt.date.fromisoformat(str(p.get("vrijedi_do"))).isoformat() if p.get("vrijedi_do") else \
            (danas + dt.timedelta(days=ROK_DANA)).isoformat()
    except ValueError:
        raise Greska("Neispravan datum važenja.")
    if do < danas.isoformat():
        raise Greska("Ponuda mora vrijediti barem do danas.")
    stavke = narudzbe._stavke(con, p.get("stavke") or [], kupac_id)
    ukupno = sum(s[6] for s in stavke)
    napomena = str(p.get("napomena") or "").strip()[:300]
    pid = int(p.get("id") or 0)
    con.execute("BEGIN IMMEDIATE")
    try:
        if pid:
            r = con.execute("SELECT status FROM ponude WHERE id = ?", (pid,)).fetchone()
            if not r:
                raise Greska("Ponuda ne postoji.")
            if r["status"] != "otvorena":
                raise Greska("Prihvaćena ili odbijena ponuda se ne mijenja.")
            con.execute("""UPDATE ponude SET kupac_id = ?, vrijedi_do = ?, ukupno = ?, napomena = ?,
                           promijenjena = ? WHERE id = ?""", (kupac_id, do, ukupno, napomena, sada(), pid))
            con.execute("DELETE FROM ponuda_stavke WHERE ponuda_id = ?", (pid,))
        else:
            broj = con.execute("SELECT COALESCE(MAX(broj), 0) + 1 FROM ponude WHERE godina = ?",
                               (danas.year,)).fetchone()[0]
            pid = con.execute(
                """INSERT INTO ponude (godina, broj, kupac_id, datum, vrijedi_do, ukupno, napomena)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (danas.year, broj, kupac_id, danas.isoformat(), do, ukupno, napomena)).lastrowid
        con.executemany(
            """INSERT INTO ponuda_stavke (ponuda_id, artikl_id, naziv, jm, kolicina, cijena, popust, iznos,
                                          pdv_stopa, usluga) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(pid,) + s for s in stavke])
        con.execute("COMMIT")
        return pid
    except Exception:
        con.execute("ROLLBACK")
        raise


def odbij(con, pid):
    with con:
        if not con.execute("UPDATE ponude SET status = 'odbijena', promijenjena = ? WHERE id = ? "
                           "AND status = 'otvorena'", (sada(), pid)).rowcount:
            raise Greska("Ponuda nije otvorena.")


def u_narudzbu(con, pid, p):
    """Prihvaćena ponuda postaje narudžba s istim stavkama i cijenama iz ponude."""
    d = ponuda(con, pid)
    if d["status"] != "otvorena":
        raise Greska("Ponuda nije otvorena.")
    if d["vrijedi_do"] < dt.date.today().isoformat() and not p.get("i_istekla"):
        raise Greska("Ponuda je istekla %s. Provjerite cijene ili potvrdite da kupac prihvaća stare."
                     % d["vrijedi_do"])
    nid = narudzbe.spremi(con, {
        "kupac_id": d["kupac_id"], "rok": p.get("rok"), "napomena": ("Po ponudi %s. " % d["oznaka"] + d["napomena"]).strip(),
        "narudzbenica": p.get("narudzbenica") or "",
        "stavke": [{"artikl_id": s["artikl_id"], "kolicina": s["kolicina"], "popust": s["popust"]}
                   for s in d["stavke"]]})
    with con:
        # cijene iz ponude vrijede i ako se MPC u međuvremenu promijenila
        ids = [r[0] for r in con.execute("SELECT id FROM narudzba_stavke WHERE narudzba_id = ? ORDER BY id", (nid,))]
        for sid, s in zip(ids, d["stavke"]):  # stavke se upisuju istim redom
            con.execute("UPDATE narudzba_stavke SET cijena = ?, iznos = ? WHERE id = ?",
                        (s["cijena"], s["iznos"], sid))
        con.execute("UPDATE narudzbe SET ukupno = (SELECT COALESCE(SUM(iznos), 0) FROM narudzba_stavke "
                    "WHERE narudzba_id = ?) WHERE id = ?", (nid, nid))
        con.execute("UPDATE ponude SET status = 'prihvacena', narudzba_id = ?, promijenjena = ? WHERE id = ?",
                    (nid, sada(), pid))
    return nid
