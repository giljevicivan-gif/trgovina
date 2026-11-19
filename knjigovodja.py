# -*- coding: utf-8 -*-
"""
Paket za knjigovođu: svi naši dokumenti za razdoblje na jednom mjestu.

Primke, povrati dobavljaču, otpisi i inventurne razlike, s rekapitulacijom
ulaznog PDV-a, kao ispis (A4) i kao CSV za Excel. Uz to, informativno,
promet iz Robe i isporučene narudžbe s brojem Robinog računa.

Službeni podaci o prodaji su fiskalizirani računi u Robi. Ovaj paket ih
ne zamjenjuje, nego knjigovođi olakšava usporedbu.
"""

import csv
import io

import narudzbe
from baza import ROBA_ULAZ_SQL, pdv_na_osnovicu
from izvjestaji import izlazni_pdv, razdoblje
from nabava import oznaka

VRSTE = (("primka", "Primke"), ("povrat", "Povrati dobavljaču"),
         ("otpis", "Otpisi (kalo, kvar, lom)"), ("inventura", "Inventurne razlike"))


def _eur(c):
    return ("%.2f" % (c / 100.0)).replace(".", ",")


def _kol(k):
    return ("%.3f" % k).rstrip("0").rstrip(".").replace(".", ",")


def _hr(iso):
    s = str(iso or "")[:10]
    return "%s.%s.%s." % (s[8:10], s[5:7], s[:4]) if len(s) == 10 else s


def dokumenti(con, od, do):
    out = []
    for r in con.execute(
            """SELECT d.*, p.naziv AS partner, p.oib AS partner_oib, o.vrsta AS o_vrsta,
                      o.godina AS o_godina, o.broj AS o_broj
               FROM dokumenti d LEFT JOIN partneri p ON p.id = d.partner_id
               LEFT JOIN dokumenti o ON o.id = d.storno_od
               WHERE d.datum BETWEEN ? AND ? ORDER BY d.vrsta, d.godina, d.broj""", (od, do)):
        d = dict(r)
        d["oznaka"] = oznaka(r)
        d["partner"] = d["partner"] or ""
        d["partner_oib"] = d["partner_oib"] or ""
        if r["storno_od"]:
            d["napomena"] = "Storno %s" % oznaka({"vrsta": r["o_vrsta"], "godina": r["o_godina"],
                                                  "broj": r["o_broj"]})
        elif r["storniran"]:
            d["napomena"] = ("STORNIRAN. " + d["napomena"]).strip()
        out.append(d)
    red = [v for v, _ in VRSTE]
    out.sort(key=lambda d: red.index(d["vrsta"]))  # stabilno: unutar vrste ostaje po broju
    return out


def paket(con, od, do):
    od, do = razdoblje(od, do)
    a = (od, do)
    dok = dokumenti(con, od, do)
    rekap = []
    for v, naslov in VRSTE:
        dd = [d for d in dok if d["vrsta"] == v]
        rekap.append({"vrsta": v, "naslov": naslov, "broj": len(dd),
                      "neto": sum(d["neto"] for d in dd), "pdv": sum(d["pdv"] for d in dd)})

    # ulazni PDV po stopama iz stavki (povrat umanjuje)
    ulazni = {}
    for r in con.execute(
            """SELECT s.pdv_stopa, SUM(CASE d.vrsta WHEN 'povrat' THEN -s.iznos ELSE s.iznos END)
               FROM dok_stavke s JOIN dokumenti d ON d.id = s.dokument_id
               WHERE d.vrsta IN ('primka', 'povrat') AND d.datum BETWEEN ? AND ?
               GROUP BY s.pdv_stopa""", a):
        ulazni[r[0]] = r[1]
    pdv_ulazni = [{"stopa": k, "osnovica": v, "pdv": pdv_na_osnovicu(v, k)}
                  for k, v in sorted(ulazni.items())]

    # promet iz Robe po stopama (informativno), PDV kako ga je Roba fiskalizirala
    promet = izlazni_pdv(con, od, do)
    racuna = con.execute(
        """SELECT COUNT(CASE WHEN iznos > 0 THEN 1 END), COUNT(CASE WHEN iznos < 0 THEN 1 END)
           FROM roba_dokumenti WHERE status IN (2, 3) AND datum BETWEEN ? AND ?""", a).fetchone()
    roba_primke = con.execute(
        """SELECT COUNT(*), COALESCE(SUM(neto), 0), COALESCE(SUM(porez), 0) FROM %s
           WHERE datum BETWEEN ? AND ?""" % ROBA_ULAZ_SQL, a).fetchone()

    isporuke = []
    for r in con.execute(
            """SELECT n.*, k.naziv AS kupac, k.oib AS kupac_oib FROM narudzbe n
               JOIN kupci k ON k.id = n.kupac_id
               WHERE n.status = 'isporucena' AND n.rok BETWEEN ? AND ? ORDER BY n.rok, n.id""", a):
        d = dict(r)
        d["oznaka"] = narudzbe.oznaka(r)
        isporuke.append(d)

    return {"od": od, "do": do, "dokumenti": dok, "rekap": rekap, "pdv_ulazni": pdv_ulazni,
            "promet": promet, "racuna": racuna[0], "storno_racuna": racuna[1],
            "roba_primke": {"broj": roba_primke[0], "neto": roba_primke[1], "pdv": roba_primke[2]},
            "isporuke": isporuke}


def _csv(redovi):
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    w.writerows(redovi)
    return "﻿" + out.getvalue()


def csv_dokumenti(con, od, do):
    od, do = razdoblje(od, do)
    red = [["Dokument", "Vrsta", "Datum", "Dobavljač", "OIB", "Broj računa dobavljača",
            "Nabavna bez PDV", "PDV", "Ukupno", "Napomena"]]
    for d in dokumenti(con, od, do):
        red.append([d["oznaka"], d["vrsta"], _hr(d["datum"]), d["partner"], d["partner_oib"],
                    d["broj_dobavljaca"], _eur(d["neto"]), _eur(d["pdv"]), _eur(d["neto"] + d["pdv"]),
                    d["napomena"]])
    return _csv(red)


def csv_stavke(con, od, do):
    od, do = razdoblje(od, do)
    red = [["Dokument", "Vrsta", "Datum", "Dobavljač", "Artikl", "JM", "Količina",
            "Nabavna cijena bez PDV", "Iznos bez PDV", "Stopa PDV", "MPC"]]
    for r in con.execute(
            """SELECT d.vrsta, d.godina, d.broj, d.datum, p.naziv AS partner, s.*
               FROM dok_stavke s JOIN dokumenti d ON d.id = s.dokument_id
               LEFT JOIN partneri p ON p.id = d.partner_id
               WHERE d.datum BETWEEN ? AND ?
               ORDER BY CASE d.vrsta WHEN 'primka' THEN 0 WHEN 'povrat' THEN 1 WHEN 'otpis' THEN 2
                        ELSE 3 END, d.godina, d.broj, s.id""", (od, do)):
        red.append([oznaka(r), r["vrsta"], _hr(r["datum"]), r["partner"] or "", r["naziv"], r["jm"],
                    _kol(r["kolicina"]), _eur(r["cijena"]), _eur(r["iznos"]),
                    ("%g" % r["pdv_stopa"]).replace(".", ","), _eur(r["mpc"]) if r["mpc"] else ""])
    return _csv(red)
