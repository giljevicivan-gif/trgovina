# -*- coding: utf-8 -*-
"""
Akcije (sniženja) za robu koja stoji.

Propisi (provjereno 22. 10. 2026.):
- Zakon o zaštiti potrošača (izmjene NN 59/2026): snižena cijena se uspoređuje
  s NAJNIŽOM cijenom koju je trgovac primjenjivao za isti proizvod u 30 dana
  prije početka sniženja, i ta se cijena ističe uz sniženu. Ako se ističe
  postotak sniženja, računa se od te najniže cijene, ne od "redovne".
- Za lako pokvarljivu robu postoje posebna pravila (ističe se cijena
  neposredno prije sniženja i krajnji rok uporabe). Mi uvijek koristimo
  strože opće pravilo (najniža u 30 dana), jer ono vrijedi u svakom slučaju.
- Istaknuta cijena mora biti ista kao naplaćena. Račune izdaje Roba, pa se
  akcijska MPC upisuje u Robu. Etiketa uvijek ispisuje cijenu iz Robe, a
  "AKCIJA" samo kad je ta cijena stvarno niža od najniže u 30 dana.

Stanje akcije pamtimo u artiklu: akcija_prije (najniža u 30 dana, ide na
etiketu), akcija_cijena (planirana akcijska MPC) i akcija_od (datum).
"""

import datetime as dt

import cijene
from baza import ZALIHA_SQL, zaokruzi
from nabava import Greska

DANA_BEZ_PRODAJE = 7
POPUST = 20  # % od najniže cijene u 30 dana


def pripremi(con):
    stupci = {r[1] for r in con.execute("PRAGMA table_info(artikli)")}
    if "akcija_cijena" not in stupci:
        con.execute("ALTER TABLE artikli ADD COLUMN akcija_cijena INTEGER NOT NULL DEFAULT 0")
        con.execute("ALTER TABLE artikli ADD COLUMN akcija_od TEXT NOT NULL DEFAULT ''")
        con.commit()


def na_9_dolje(c):
    """Najveća cijena oblika x,x9 koja nije veća od c (centi)."""
    return c if c % 10 == 9 else c - c % 10 - 1


def na_9_gore(c):
    """Najmanja cijena oblika x,x9 koja nije manja od c (centi)."""
    return c if c % 10 == 9 else c - c % 10 + 9


def _nabavna_s_pdv(a):
    return zaokruzi(a["nabavna"] * (1 + a["pdv_stopa"] / 100.0))


def _zadnji_dan(con):
    return con.execute("SELECT MAX(datum) FROM roba_dokumenti WHERE status IN (2, 3)").fetchone()[0]


def prijedlog(con, dana=DANA_BEZ_PRODAJE, popust=POPUST):
    """Roba na zalihi koja se ne prodaje, s prijedlogom akcijske cijene."""
    try:
        dana, popust = int(dana), float(str(popust).replace(",", "."))
    except (TypeError, ValueError):
        raise Greska("Neispravan broj dana ili popust.")
    if not 1 <= dana <= 90 or not 1 <= popust <= 90:
        raise Greska("Dani 1 - 90, popust 1 - 90 %.")
    zadnji = _zadnji_dan(con)
    if not zadnji:
        return {"dana": dana, "popust": popust, "do": None, "stavke": []}
    od = (dt.date.fromisoformat(zadnji) - dt.timedelta(days=dana - 1)).isoformat()
    prodano = {r[0] for r in con.execute(
        "SELECT DISTINCT artikl FROM roba_stavke WHERE status IN (2, 3) AND datum BETWEEN ? AND ?",
        (od, zadnji))}
    zadnja = {r[0]: r[1] for r in con.execute(
        "SELECT artikl, MAX(datum) FROM roba_stavke WHERE status IN (2, 3) GROUP BY artikl")}
    out = []
    for a in con.execute(
            """SELECT a.*, round(%s, 3) AS zaliha FROM artikli a
               WHERE a.aktivan = 1 AND a.usluga = 0 AND a.akcija_prije = 0
               ORDER BY a.naziv""" % ZALIHA_SQL):
        if a["zaliha"] <= 0 or a["roba_sifra"] in prodano or "AKCIJA" in a["naziv"].upper():
            continue
        najniza = cijene.najniza_30(con, a["id"], s_trenutnom=True)
        nab = _nabavna_s_pdv(a)
        cilj = na_9_dolje(int(najniza * (1 - popust / 100.0) + 1e-7))
        napomena = ""
        if nab and cilj < nab:
            cilj = na_9_gore(nab)
            napomena = "do nabavne"
        if cilj >= najniza or cilj <= 0:
            cilj = 0
            napomena = "akcija bi bila ispod nabavne; razmislite o otpisu ili donaciji"
        out.append({"id": a["id"], "sifra": a["sifra"], "naziv": a["naziv"], "jm": a["jm"],
                    "zaliha": a["zaliha"], "zadnja_prodaja": zadnja.get(a["roba_sifra"]),
                    "cijena": a["cijena"], "najniza_30": najniza, "nabavna_pdv": nab,
                    "akcijska": cilj,
                    "snizenje": round((najniza - cilj) * 100.0 / najniza) if cilj else 0,
                    "vrijednost": zaokruzi(a["zaliha"] * a["nabavna"]), "napomena": napomena})
    out.sort(key=lambda s: -s["vrijednost"])
    return {"dana": dana, "popust": popust, "do": zadnji, "stavke": out}


def pokreni(con, stavke):
    """Upiše akciju za odabrane artikle: [{id, cijena (centi)}]. Vraća popis za upis u Robu."""
    if not stavke:
        raise Greska("Odaberite artikle.")
    danas = dt.date.today().isoformat()
    upis = []
    vidjeni = set()
    for s in stavke:
        try:
            aid, cijena = int(s["id"]), int(s["cijena"])
        except (TypeError, ValueError, KeyError):
            raise Greska("Neispravna akcijska cijena.")
        if aid in vidjeni:
            raise Greska("Isti artikl je dvaput u popisu.")
        vidjeni.add(aid)
        a = con.execute("SELECT * FROM artikli WHERE id = ?", (aid,)).fetchone()
        if not a:
            raise Greska("Artikl ne postoji.")
        if a["akcija_prije"]:
            raise Greska("%s je već na akciji." % a["naziv"])
        najniza = cijene.najniza_30(con, aid, s_trenutnom=True)
        if not 0 < cijena < najniza:
            raise Greska("%s: akcijska cijena mora biti niža od najniže cijene u 30 dana (%s EUR)."
                         % (a["naziv"], cijene._eur(najniza)))
        nab = _nabavna_s_pdv(a)
        if nab and cijena < nab:
            # prodaja ispod nabavne može biti nepošteno trgovanje (Zakon o trgovini); za
            # robu koja se kvari bolje je otpis ili donacija, a iznimke s knjigovođom
            raise Greska("%s: akcijska cijena je ispod nabavne s PDV-om (%s EUR). Za robu koja se "
                         "kvari napravite otpis ili donaciju." % (a["naziv"], cijene._eur(nab)))
        upis.append((najniza, cijena, danas, aid, a))
    con.execute("BEGIN IMMEDIATE")
    try:
        for najniza, cijena, danas, aid, _ in upis:
            con.execute("UPDATE artikli SET akcija_prije = ?, akcija_cijena = ?, akcija_od = ? WHERE id = ?",
                        (najniza, cijena, danas, aid))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return [{"id": aid, "sifra": a["sifra"], "naziv": a["naziv"], "jm": a["jm"], "stara": a["cijena"],
             "nova": cijena, "najniza_30": najniza} for najniza, cijena, _, aid, a in upis]


def zavrsi(con, aid):
    a = con.execute("SELECT naziv, cijena FROM artikli WHERE id = ?", (aid,)).fetchone()
    if not a:
        raise Greska("Artikl ne postoji.")
    with con:
        con.execute("UPDATE artikli SET akcija_prije = 0, akcija_cijena = 0, akcija_od = '' WHERE id = ?",
                    (aid,))


def aktivne(con):
    out = []
    for a in con.execute(
            """SELECT a.*, round(%s, 3) AS zaliha FROM artikli a
               WHERE a.aktivan = 1 AND a.akcija_prije > 0 ORDER BY a.naziv""" % ZALIHA_SQL):
        if a["cijena"] >= a["akcija_prije"]:
            stanje = "cijena u Robi nije snižena - etiketa ne piše AKCIJA"
        elif a["akcija_cijena"] and a["cijena"] != a["akcija_cijena"]:
            stanje = "u Robi je %s, planirano %s" % (cijene._eur(a["cijena"]), cijene._eur(a["akcija_cijena"]))
        else:
            stanje = "ok"
        out.append({"id": a["id"], "sifra": a["sifra"], "naziv": a["naziv"], "jm": a["jm"],
                    "zaliha": a["zaliha"], "cijena": a["cijena"], "najniza_30": a["akcija_prije"],
                    "akcijska": a["akcija_cijena"], "od": a["akcija_od"], "stanje": stanje})
    return out
