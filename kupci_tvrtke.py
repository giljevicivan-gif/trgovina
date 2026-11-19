# -*- coding: utf-8 -*-
"""
Kupci na tvrtku: rabat i ugovorene cijene po kupcu, i zbirni pregled za račun.

Za restorane, škole i druge tvrtke koje kupuju redovito. Uključuje se u
Postavkama (modul "kupci_tvrtke") i radi uz narudžbe.

- Rabat (%) vrijedi za sve artikle kupca koji nemaju ugovorenu cijenu.
- Ugovorena cijena (s PDV-om) vrijedi za taj artikl umjesto MPC-a iz Robe.
  Pravila o isticanju cijena i najnižoj cijeni u 30 dana odnose se na
  potrošače; ugovorene cijene s tvrtkom nisu javna akcija.
- Ne prodaje se ispod nabavne cijene s PDV-om (kao i kod akcija).

Računi se i dalje izdaju i fiskaliziraju u Robi (tvrtkama R1 / eRačun).
Zbirni pregled skuplja isporučene narudžbe kupca u mjesecu koje još nemaju
račun, kao podlogu za JEDAN zbirni račun u Robi (Zakon o PDV-u čl. 78
dopušta zbirni račun za više isporuka u istom kalendarskom mjesecu). Kad se
plaća gotovinom ili karticom, račun se izdaje odmah kod isporuke.
"""

import re

import narudzbe
import postavke
from baza import pdv_iz_bruto, zaokruzi
from nabava import Greska, sada

SHEMA = """
CREATE TABLE IF NOT EXISTS kupac_cijene (
    kupac_id   INTEGER NOT NULL REFERENCES kupci(id),
    artikl_id  INTEGER NOT NULL REFERENCES artikli(id),
    cijena     INTEGER NOT NULL,          -- s PDV-om, centi
    promijenjena TEXT NOT NULL,
    PRIMARY KEY (kupac_id, artikl_id)
);
"""


def pripremi(con):
    con.executescript(SHEMA)
    stupci = {r[1] for r in con.execute("PRAGMA table_info(kupci)")}
    if "rabat" not in stupci:
        con.execute("ALTER TABLE kupci ADD COLUMN rabat REAL NOT NULL DEFAULT 0")
        con.execute("ALTER TABLE kupci ADD COLUMN rok_placanja INTEGER NOT NULL DEFAULT 0")
        con.commit()


def ukljucen(con):
    m = postavke.moduli(con)
    return m["narudzbe"] and m["kupci_tvrtke"]


def _eur(c):
    return ("%.2f" % (c / 100.0)).replace(".", ",")


def _centi(v):
    s = str(v or "").strip().replace(" ", "").replace(",", ".")
    if not s:
        return 0
    try:
        return zaokruzi(float(s) * 100)
    except ValueError:
        raise Greska("Neispravna cijena.")


def _nabavna_s_pdv(a):
    return zaokruzi(a["nabavna"] * (1 + a["pdv_stopa"] / 100.0))


def _kupac(con, kid):
    k = con.execute("SELECT * FROM kupci WHERE id = ?", (kid,)).fetchone()
    if not k:
        raise Greska("Kupac ne postoji.")
    return k


def uvjeti(con, kid):
    """Za upis narudžbe: (rabat, {artikl_id: ugovorena cijena}). Prazno ako je modul isključen."""
    if not kid or not ukljucen(con):
        return 0, {}
    k = con.execute("SELECT rabat FROM kupci WHERE id = ?", (kid,)).fetchone()
    return (k["rabat"] if k else 0), {r[0]: r[1] for r in con.execute(
        "SELECT artikl_id, cijena FROM kupac_cijene WHERE kupac_id = ?", (kid,))}


def cjenik(con, kid):
    k = dict(_kupac(con, kid))
    k["cijene"] = [dict(r) for r in con.execute(
        """SELECT c.artikl_id, c.cijena, c.promijenjena, a.sifra, a.naziv, a.jm, a.cijena AS mpc,
                  a.nabavna, a.pdv_stopa
           FROM kupac_cijene c JOIN artikli a ON a.id = c.artikl_id
           WHERE c.kupac_id = ? ORDER BY a.naziv""", (kid,))]
    for c in k["cijene"]:
        c["ispod_nabavne"] = bool(c["nabavna"]) and c["cijena"] < _nabavna_s_pdv(c)
    return k


def spremi_uvjete(con, kid, p):
    _kupac(con, kid)
    try:
        rabat = round(float(str(p.get("rabat") or 0).replace(",", ".")), 2)
        rok = int(p.get("rok_placanja") or 0)
    except ValueError:
        raise Greska("Neispravan rabat ili rok plaćanja.")
    if not 0 <= rabat <= 50:
        raise Greska("Rabat mora biti između 0 i 50 %.")
    if not 0 <= rok <= 120:
        raise Greska("Rok plaćanja 0 do 120 dana.")
    with con:
        con.execute("UPDATE kupci SET rabat = ?, rok_placanja = ? WHERE id = ?", (rabat, rok, kid))


def spremi_cijenu(con, kid, p):
    """Ugovorena cijena za artikl; prazno ili 0 briše (vrijedi MPC i rabat)."""
    _kupac(con, kid)
    a = con.execute("SELECT * FROM artikli WHERE id = ?", (int(p.get("artikl_id") or 0),)).fetchone()
    if not a:
        raise Greska("Artikl ne postoji.")
    cijena = _centi(p.get("cijena"))
    with con:
        if not cijena:
            con.execute("DELETE FROM kupac_cijene WHERE kupac_id = ? AND artikl_id = ?", (kid, a["id"]))
            return
        if cijena < 0 or cijena > 10000000:
            raise Greska("Neispravna cijena.")
        nab = _nabavna_s_pdv(a)
        if nab and cijena < nab:
            raise Greska("%s: cijena je ispod nabavne s PDV-om (%s EUR)." % (a["naziv"], _eur(nab)))
        con.execute("INSERT OR REPLACE INTO kupac_cijene (kupac_id, artikl_id, cijena, promijenjena) "
                    "VALUES (?, ?, ?, ?)", (kid, a["id"], cijena, sada()))


def _mjesec(mj):
    if not re.fullmatch(r"\d{4}-\d{2}", str(mj or "")):
        raise Greska("Odaberite mjesec.")
    return mj


def zbirni(con, kid, mj):
    """Isporučene narudžbe kupca u mjesecu: bez računa (za zbirni račun) i već fakturirane."""
    mj = _mjesec(mj)
    k = dict(_kupac(con, kid))
    nar = [dict(r, oznaka=narudzbe.oznaka(r)) for r in con.execute(
        """SELECT * FROM narudzbe WHERE kupac_id = ? AND status = 'isporucena'
           AND substr(rok, 1, 7) = ? ORDER BY rok, broj""", (kid, mj))]
    bez = [n for n in nar if not n["racun_roba"]]
    ids = [n["id"] for n in bez]
    stavke, pdv = [], {}
    if ids:
        for r in con.execute(
                """SELECT s.artikl_id, s.naziv, s.jm, s.cijena, s.popust, s.pdv_stopa,
                          SUM(s.kolicina) AS kolicina, SUM(s.iznos) AS iznos
                   FROM narudzba_stavke s WHERE s.narudzba_id IN (%s)
                   GROUP BY s.artikl_id, s.cijena, s.popust, s.pdv_stopa
                   ORDER BY s.naziv""" % ",".join("?" * len(ids)), ids):
            stavke.append(dict(r))
            pdv[r["pdv_stopa"]] = pdv.get(r["pdv_stopa"], 0) + r["iznos"]
    return {"kupac": k, "mjesec": mj, "bez_racuna": bez,
            "fakturirane": [n for n in nar if n["racun_roba"]],
            "stavke": stavke, "ukupno": sum(s["iznos"] for s in stavke),
            "pdv": [{"stopa": s, "osnovica": v - pdv_iz_bruto(v, s), "pdv": pdv_iz_bruto(v, s)}
                    for s, v in sorted(pdv.items())]}


def upisi_racun(con, kid, p):
    """Broj zbirnog računa iz Robe upiše se svim navedenim narudžbama koje ga nemaju."""
    racun = str(p.get("racun_roba") or "").strip()[:60]
    if not racun:
        raise Greska("Upišite broj računa iz Robe.")
    try:
        ids = [int(i) for i in p.get("narudzbe") or []]
    except (TypeError, ValueError):
        raise Greska("Neispravan popis narudžbi.")
    if not ids:
        raise Greska("Nema narudžbi bez računa.")
    con.execute("BEGIN IMMEDIATE")
    try:
        n = 0
        for nid in ids:
            n += con.execute(
                """UPDATE narudzbe SET racun_roba = ?, promijenjena = ? WHERE id = ? AND kupac_id = ?
                   AND status = 'isporucena' AND racun_roba = ''""", (racun, sada(), nid, kid)).rowcount
        if n != len(ids):
            raise Greska("Neke narudžbe su u međuvremenu promijenjene. Osvježite pregled.")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return n
