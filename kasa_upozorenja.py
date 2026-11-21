# -*- coding: utf-8 -*-
"""
Rok trajanja i minimalna zaliha na kasi (modul "kasa_upozorenja").

Kasa pri učitavanju (i svakih 5 minuta, i nakon naplate) dohvati popis artikala na koje treba
upozoriti kod dodavanja u košaricu:
- rokovi (samo uz modul "rokovi"): otvoreni upisi roka po artiklu, najgore stanje
  (istekao "upotrijebiti do" = ne smije se prodati, kasa traži potvrdu da je roba iz druge serije;
  istekao "najbolje upotrijebiti do" = prodaja samo izdvojeno uz obavijest kupcu;
  rok ističe za najviše rokovi.UPOZORI_DANA dana = podsjetnik za sniženje);
- minimalna zaliha: artikli kojima je upisana minimalna zaliha, s trenutnom zalihom,
  pa kasa javi kad prodaja spusti zalihu ispod minimalne.

Ništa se ne mijenja u bazi; kasa samo prikazuje poruku.
"""

import postavke
import rokovi
from baza import ZALIHA_SQL

# što je gore, to je važnije (jedan artikl može imati više upisa roka)
VAZNOST = {"istekao_zabrana": 3, "istekao_izdvojiti": 2, "izdvojeno": 1, "uskoro": 1}


def za_kasu(con, danas=None):
    if not postavke.ukljucen(con, "kasa_upozorenja"):
        return {"ukljuceno": False, "rokovi": {}, "zaliha": {}}
    po_artiklu = {}
    if postavke.ukljucen(con, "rokovi"):
        for x in rokovi.popis(con, danas):
            if x["stanje"] not in VAZNOST:
                continue
            k = str(x["artikl_id"])
            st = po_artiklu.get(k)
            if not st or VAZNOST[x["stanje"]] > VAZNOST[st["stanje"]] or (
                    VAZNOST[x["stanje"]] == VAZNOST[st["stanje"]] and x["rok"] < st["rok"]):
                po_artiklu[k] = {"stanje": x["stanje"], "rok": x["rok"], "dana": x["dana"],
                                 "vrsta": rokovi.VRSTE[x["vrsta"]], "kolicina": x["kolicina"]}
    zaliha = {}
    for r in con.execute(
            """SELECT a.id, a.min_zaliha, round(%s, 3) AS zaliha FROM artikli a
               WHERE a.aktivan = 1 AND a.usluga = 0 AND a.min_zaliha > 0""" % ZALIHA_SQL):
        zaliha[str(r["id"])] = {"min": r["min_zaliha"], "zaliha": r["zaliha"]}
    return {"ukljuceno": True, "rokovi": po_artiklu, "zaliha": zaliha}
