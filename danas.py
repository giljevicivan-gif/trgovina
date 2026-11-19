# -*- coding: utf-8 -*-
"""
Jutarnji pregled: sve što treba napraviti danas, na jednom ekranu.

Samo čita: promet iz Robe (zadnji dan s računima), narudžbe kupaca, prijedlog
narudžbe dobavljačima, akcije, crvene stavke iz Kontrole i rokove propisa.
"""

import datetime as dt

import akcije
import dugovanja
import haccp
import kontrola
import nabava
import narudzbe
import postavke
import rokovi

# Rokovi propisa koji se tiču trgovine (provjereno 19. 11. 2026.)
ROKOVI = (
    ("2026-11-17", "Bazna cijena (prije zvana sidrena) na etiketama je obvezna (NN 132/2026). "
                   "Ispišite nove etikete (Zaliha, Etikete za police)."),
    ("2027-01-01", "Fiskalizacija 2.0: i obveznici koji nisu u sustavu PDV-a moraju izdavati eRačune "
                   "tvrtkama. Provjerite s knjigovođom vrijedi li to za vas i radi li Roba to."),
    ("2027-01-01", "Porezna od danas odbija račune potpisane starim načinom (SHA-1). Provjerite da Roba "
                   "ima nadogradnju za fiskalizaciju v2.7 (SHA-256); naša kasa je već spremna."),
)
ROK_UNAPRIJED = 45  # dana prije roka počinje podsjetnik

DANI = ["ponedjeljak", "utorak", "srijeda", "četvrtak", "petak", "subota", "nedjelja"]


def _promet(con, datum):
    r = con.execute(
        """SELECT COUNT(CASE WHEN iznos > 0 THEN 1 END), COALESCE(SUM(iznos), 0)
           FROM roba_dokumenti WHERE status IN (2, 3) AND datum = ?""", (datum,)).fetchone()
    return {"datum": datum, "racuna": r[0], "iznos": r[1]}


def pregled(con, danas=None):
    danas = danas or dt.date.today()
    sutra = (danas + dt.timedelta(days=1)).isoformat()
    moduli = postavke.moduli(con)
    out = {"danas": danas.isoformat(), "dan": DANI[danas.weekday()], "moduli": moduli}

    # --- promet zadnjeg dana u Robi i isti dan tjedan ranije
    zadnji = con.execute("SELECT MAX(datum) FROM roba_dokumenti WHERE status IN (2, 3)").fetchone()[0]
    if zadnji:
        z = dt.date.fromisoformat(zadnji)
        out["promet"] = _promet(con, zadnji)
        out["promet"]["dan"] = DANI[z.weekday()]
        out["promet_tjedan_prije"] = _promet(con, (z - dt.timedelta(days=7)).isoformat())
        out["stari_podaci"] = (danas - z).days > 3
    else:
        out["promet"] = None

    # --- narudžbe kupaca: kasne, danas, sutra
    otvorene = narudzbe.popis(con)
    out["narudzbe"] = [{"id": n["id"], "oznaka": n["oznaka"], "kupac": n["kupac"], "rok": n["rok"],
                        "status": n["status"], "ukupno": n["ukupno"],
                        "kada": "kasni" if n["rok"] < out["danas"] else
                                ("danas" if n["rok"] == out["danas"] else "sutra")}
                       for n in otvorene if n["rok"] <= sutra] if moduli["narudzbe"] else []

    # --- za naručiti (zaliha za 2 dana + minimalna)
    p = nabava.prijedlog(con, 28, 2)
    st = p["stavke"] if isinstance(p, dict) else p
    out["naruciti"] = [{"naziv": s["naziv"], "jm": s["jm"], "zaliha": s["zaliha"],
                        "kolicina": s["kolicina"], "dobavljac": s["dobavljac"]} for s in st]

    # --- akcije
    akt = akcije.aktivne(con)
    out["akcije_ceka"] = [a for a in akt if a["stanje"] != "ok"]
    out["akcije_traju"] = len(akt)
    try:
        out["roba_stoji"] = len(akcije.prijedlog(con)["stavke"])
    except Exception:
        out["roba_stoji"] = 0

    out["haccp"] = haccp.upozorenja(con, danas) if moduli["haccp"] else None
    out["rokovi_trajanja"] = rokovi.upozorenja(con, danas) if moduli["rokovi"] else None
    if moduli["dugovanja"]:
        d = dugovanja.upozorenja(con, danas)
        out["dugovanja"] = {"kasni": [{"oznaka": x["oznaka"], "partner": x["partner"], "ostalo": x["ostalo"],
                                       "dospijece": x["dospijece"]} for x in d["kasni"]],
                            "uskoro": [{"oznaka": x["oznaka"], "partner": x["partner"], "ostalo": x["ostalo"],
                                        "dospijece": x["dospijece"]} for x in d["uskoro"]]}
    else:
        out["dugovanja"] = None

    # --- crveno iz Kontrole
    out["upozorenja"] = [{"naslov": k["naslov"], "opis": k["opis"], "ukupno": k["ukupno"]}
                         for k in kontrola.provjere(con)
                         if k["razina"] == "crveno" and not k["naslov"].startswith(
                             ("Narudžbe", "Istekao rok", "Računi dobavljača"))]  # imaju svoju karticu

    # --- smjena
    sm = con.execute("SELECT otvorena FROM smjene WHERE zatvorena IS NULL").fetchone()
    out["smjena_otvorena"] = sm[0] if sm else None

    # --- rokovi propisa
    out["rokovi"] = []
    for datum, tekst in ROKOVI:
        d = dt.date.fromisoformat(datum)
        do = (d - danas).days
        if 0 <= do <= ROK_UNAPRIJED:
            out["rokovi"].append({"datum": datum, "dana": do, "tekst": tekst})
    return out
