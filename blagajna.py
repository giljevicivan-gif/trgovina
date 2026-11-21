# -*- coding: utf-8 -*-
"""
Blagajna po smjenama (ideja iz POS programa, npr. Loyverse "Shifts").

Smjena: otvori s početnom gotovinom u ladici -> tijekom dana polog (npr. sitniš)
ili isplata (npr. plaćen dobavljač iz blagajne) -> zaključak: upišete prebrojanu
gotovinu, program izračuna koliko bi trebalo biti i pokaže razliku (manjak/višak).

Gotovinska prodaja se uzima iz Robe (fiskalni računi), jer kupac plaća preko Robe.
Zato se prije zaključka podaci iz Robe osvježe (ako je poznata mapa Robe).
"""

import datetime as dt

from baza import ROBA_PLACANJA_SQL, centi
from nabava import Greska


def sada():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _iznos(v, ime):
    try:
        c = centi(float(str(v).replace(",", ".")))
    except (TypeError, ValueError):
        raise Greska("Neispravan iznos: %s." % ime)
    if c < 0 or c > 100000000:
        raise Greska("Neispravan iznos: %s." % ime)
    return c


def trenutna(con):
    r = con.execute("SELECT * FROM smjene WHERE zatvorena IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    return izracun(con, r["id"]) if r else None


def _gotovinski_kodovi(con):
    return [r[0] for r in con.execute(
        "SELECT roba_sifra FROM nacini_placanja WHERE lower(naziv) LIKE 'gotov%' "
        "AND roba_sifra IS NOT NULL")]


def izracun(con, sid):
    s = con.execute("SELECT * FROM smjene WHERE id = ?", (sid,)).fetchone()
    if not s:
        raise Greska("Smjena ne postoji.")
    d = dict(s)
    do = s["zatvorena"] or sada()
    kodovi = _gotovinski_kodovi(con) or [1]
    ph = ",".join("?" * len(kodovi))
    # račun plaćen dijelom gotovinom, dijelom karticom: u ladicu ide samo gotovinski dio
    got = con.execute(
        """SELECT COALESCE(SUM(iznos), 0) FROM %s
           WHERE status IN (2, 3) AND vrijeme >= ? AND vrijeme <= ?
           AND placanje IN (%s)""" % (ROBA_PLACANJA_SQL, ph), [s["otvorena"], do] + kodovi).fetchone()
    ost = con.execute(
        """SELECT COALESCE(SUM(iznos), 0) FROM %s
           WHERE status IN (2, 3) AND vrijeme >= ? AND vrijeme <= ?
           AND placanje NOT IN (%s)""" % (ROBA_PLACANJA_SQL, ph), [s["otvorena"], do] + kodovi).fetchone()
    racuna = con.execute(
        """SELECT COUNT(*) FROM roba_dokumenti WHERE status IN (2, 3) AND iznos > 0
           AND vrijeme >= ? AND vrijeme <= ?""", (s["otvorena"], do)).fetchone()[0]
    promet = [dict(r) for r in con.execute(
        "SELECT * FROM blagajna_promet WHERE smjena_id = ? ORDER BY id", (sid,))]
    polozi = sum(p["iznos"] for p in promet if p["iznos"] > 0)
    isplate = -sum(p["iznos"] for p in promet if p["iznos"] < 0)
    zadnji = con.execute(
        "SELECT MAX(vrijeme) FROM roba_dokumenti WHERE status IN (2, 3)").fetchone()[0]
    kasa = gotovina_nase_kase(con, s["otvorena"], do)
    brojanja = [dict(r) for r in con.execute(
        "SELECT * FROM blagajna_brojanja WHERE smjena_id = ? ORDER BY id", (sid,))]
    for b in brojanja:
        b["razlika"] = b["prebrojano"] - b["ocekivano"]
    d.update({
        "gotovina_sada": got[0], "kartice_sada": ost[0], "racuna_sada": racuna,
        "gotovina_kasa": kasa,
        "polozi": polozi, "isplate": isplate, "promet": promet, "brojanja": brojanja,
        "ocekivano_sada": s["pocetno"] + got[0] + kasa + polozi - isplate,
        "zadnji_racun_robe": zadnji,
    })
    if s["zatvorena"]:
        d["razlika"] = s["prebrojano"] - s["ocekivano"]
    return d


def gotovina_nase_kase(con, od, do):
    """Gotovina s pravih fiskalnih računa naše kase (prava fiskalizacija, ne test ni vježba)
    u razdoblju; račun plaćen dijelom gotovinom broji samo gotovinski dio. Storno je u minusu."""
    try:
        r = con.execute(
            """SELECT COALESCE(SUM(CASE
                   WHEN EXISTS (SELECT 1 FROM racun_placanja x WHERE x.racun_id = r.id)
                   THEN (SELECT COALESCE(SUM(x.iznos), 0) FROM racun_placanja x
                         JOIN nacini_placanja n ON n.id = x.nacin_placanja_id
                         WHERE x.racun_id = r.id AND lower(n.naziv) LIKE 'gotov%')
                   WHEN lower(p.naziv) LIKE 'gotov%' THEN r.ukupno ELSE 0 END), 0)
               FROM racuni r JOIN fiskal_racuni f ON f.racun_id = r.id AND f.okolina = 'produkcija'
               JOIN nacini_placanja p ON p.id = r.nacin_placanja_id
               WHERE r.vrijeme >= ? AND r.vrijeme <= ?""", (od, do)).fetchone()
    except Exception:  # fiskalizacija nikad nije pripremljena (nema tablice)
        return 0
    return r[0] or 0


def brojanje(con, podaci):
    """Brzo brojanje ladice usred smjene: zapamti prebrojano i očekivano, smjena ostaje otvorena."""
    s = con.execute("SELECT id FROM smjene WHERE zatvorena IS NULL").fetchone()
    if not s:
        raise Greska("Nema otvorene smjene.")
    v = podaci.get("prebrojano")
    if v is None or not str(v).strip():
        raise Greska("Upišite koliko je gotovine u ladici.")
    prebrojano = _iznos(v, "prebrojana gotovina")
    d = izracun(con, s["id"])
    with con:
        bid = con.execute(
            "INSERT INTO blagajna_brojanja (smjena_id, vrijeme, ocekivano, prebrojano, napomena) VALUES (?, ?, ?, ?, ?)",
            (s["id"], sada(), d["ocekivano_sada"], prebrojano, str(podaci.get("napomena") or "").strip()[:200])).lastrowid
    return {"id": bid, "ocekivano": d["ocekivano_sada"], "prebrojano": prebrojano,
            "razlika": prebrojano - d["ocekivano_sada"], "zadnji_racun_robe": d["zadnji_racun_robe"]}


def otvori(con, podaci):
    if con.execute("SELECT 1 FROM smjene WHERE zatvorena IS NULL").fetchone():
        raise Greska("Smjena je već otvorena.")
    pocetno = _iznos(podaci.get("pocetno") or 0, "početna gotovina")
    with con:
        return con.execute("INSERT INTO smjene (otvorena, pocetno) VALUES (?, ?)",
                           (sada(), pocetno)).lastrowid


def promet(con, podaci):
    s = con.execute("SELECT id FROM smjene WHERE zatvorena IS NULL").fetchone()
    if not s:
        raise Greska("Nema otvorene smjene.")
    iznos = _iznos(podaci.get("iznos"), "iznos")
    if not iznos:
        raise Greska("Upišite iznos.")
    opis = str(podaci.get("opis") or "").strip()[:200]
    if podaci.get("vrsta") == "isplata":
        iznos = -iznos
        if not opis:
            raise Greska("Upišite za što je isplata.")
    with con:
        con.execute("INSERT INTO blagajna_promet (smjena_id, vrijeme, iznos, opis) VALUES (?, ?, ?, ?)",
                    (s["id"], sada(), iznos, opis))


def zatvori(con, podaci):
    s = con.execute("SELECT id FROM smjene WHERE zatvorena IS NULL").fetchone()
    if not s:
        raise Greska("Nema otvorene smjene.")
    prebrojano = _iznos(podaci.get("prebrojano"), "prebrojana gotovina")
    d = izracun(con, s["id"])
    with con:
        con.execute(
            """UPDATE smjene SET zatvorena = ?, ocekivano = ?, prebrojano = ?, gotovina = ?,
                      kartice = ?, racuna = ?, napomena = ? WHERE id = ?""",
            (sada(), d["ocekivano_sada"], prebrojano, d["gotovina_sada"] + d["gotovina_kasa"], d["kartice_sada"],
             d["racuna_sada"], str(podaci.get("napomena") or "").strip()[:300], s["id"]))
    return s["id"]


def popis(con, n=30):
    out = []
    for r in con.execute("SELECT * FROM smjene WHERE zatvorena IS NOT NULL ORDER BY id DESC LIMIT ?", (n,)):
        d = dict(r)
        d["razlika"] = r["prebrojano"] - r["ocekivano"]
        out.append(d)
    return out
