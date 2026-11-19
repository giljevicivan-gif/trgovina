# -*- coding: utf-8 -*-
"""
Izvještaji za razdoblje (od - do, uključivo).

Prodaja dolazi iz Robe (računi koje je Roba fiskalizirala, uvezeni kod pokretanja).
Nabava i otpisi: iz Robe (prije nego što smo ih počeli voditi ovdje) i naši dokumenti.
Iznosi su u centima.
"""

import datetime as dt

from baza import ROBA_PLACANJA_SQL, ROBA_ULAZ_SQL, pdv_iz_bruto, pdv_na_osnovicu

# U Robi: 3 = maloprodajni račun, 2 = račun R1 (pravnoj osobi), 1 = primka, 10 = kalo/otpis
PRODAJA = (2, 3)


def razdoblje(od, do):
    danas = dt.date.today()
    try:
        od = dt.date.fromisoformat(od).isoformat() if od else danas.replace(day=1).isoformat()
        do = dt.date.fromisoformat(do).isoformat() if do else danas.isoformat()
    except ValueError:
        raise ValueError("Neispravan datum.")
    return (od, do) if od <= do else (do, od)


def izlazni_pdv(con, od, do):
    """Izlazni PDV po stopama s računa iz Robe.

    Maloprodajni računi (status 3): Roba računa PDV za svaku stavku posebno i zbraja ga, pa na
    otprilike svakom trećem računu osnovica + PDV odstupa od ukupnog iznosa za 1 cent. Službeni
    su iznosi koje je Roba fiskalizirala, pa ih ovdje zbrajamo takve kakvi jesu (osnovica = VPC
    stavki, PDV = PDV stavki), a razliku zaokruživanja pokazujemo posebno.

    Računi R1 (status 2) idu od osnovice: na računu je PDV po stopi na zbroj osnovica tog
    računa (tako ga računa i Roba u zaglavlju), a ne zbroj PDV-a stavki."""
    po = {}

    def dodaj(stopa, osnovica, pdv, ukupno):
        z = po.setdefault(stopa, [0, 0, 0])
        z[0] += osnovica
        z[1] += pdv
        z[2] += ukupno

    for r in con.execute(
            """SELECT pdv_stopa, SUM(iznos) AS iznos, SUM(neto) AS neto, SUM(porez) AS porez,
                      COUNT(porez) AS s_porezom, COUNT(*) AS n
               FROM roba_stavke WHERE status = 3 AND datum BETWEEN ? AND ?
               GROUP BY pdv_stopa""", (od, do)):
        if r["s_porezom"] == r["n"]:
            osnovica, pdv = r["neto"], r["porez"]
        else:  # baza prije prvog uvoza sa stupcem porez
            pdv = pdv_iz_bruto(r["iznos"], r["pdv_stopa"])
            osnovica = r["iznos"] - pdv
        dodaj(r["pdv_stopa"], osnovica, pdv, r["iznos"])
    for r in con.execute(
            """SELECT pdv_stopa, SUM(neto) AS neto FROM roba_stavke
               WHERE status = 2 AND datum BETWEEN ? AND ? GROUP BY godina, link, pdv_stopa""", (od, do)):
        pdv = pdv_na_osnovicu(r["neto"], r["pdv_stopa"])
        dodaj(r["pdv_stopa"], r["neto"], pdv, r["neto"] + pdv)
    return [{"stopa": k, "osnovica": v[0], "pdv": v[1], "ukupno": v[2], "razlika": v[2] - v[0] - v[1]}
            for k, v in sorted(po.items())]


def _redovi(con, sql, args):
    return [dict(r) for r in con.execute(sql, args)]


def izvjestaj(con, od, do):
    od, do = razdoblje(od, do)
    a = (od, do)
    dana = (dt.date.fromisoformat(do) - dt.date.fromisoformat(od)).days + 1
    po_mjesecu = dana > 62
    kljuc = "substr(datum, 1, 7)" if po_mjesecu else "datum"

    # --- promet (Roba)
    ukupno = con.execute(
        """SELECT COUNT(CASE WHEN iznos > 0 AND brisan = 0 THEN 1 END) AS racuna, COALESCE(SUM(iznos), 0) AS iznos,
                  COALESCE(SUM(neto), 0) AS neto, COALESCE(SUM(porez), 0) AS pdv
           FROM roba_dokumenti WHERE status IN (2, 3) AND datum BETWEEN ? AND ?""", a).fetchone()
    promet = _redovi(con,
        """SELECT %s AS razdoblje, COUNT(CASE WHEN iznos > 0 AND brisan = 0 THEN 1 END) AS racuna,
                  SUM(iznos) AS iznos, SUM(neto) AS neto, SUM(porez) AS pdv
           FROM roba_dokumenti WHERE status IN (2, 3) AND datum BETWEEN ? AND ?
           GROUP BY razdoblje ORDER BY razdoblje""" % kljuc, a)

    # račun plaćen dijelom gotovinom, dijelom karticom broji se kod oba načina
    placanja = _redovi(con,
        """SELECT COALESCE(p.naziv, CASE d.placanje WHEN 0 THEN 'Transakcijski (R1 račun)'
                                     ELSE 'Ostalo' END) AS nacin,
                  COUNT(CASE WHEN d.iznos_racuna > 0 AND d.brisan = 0 THEN 1 END) AS racuna,
                  SUM(d.iznos) AS iznos
           FROM %s d LEFT JOIN nacini_placanja p ON p.roba_sifra = d.placanje
           WHERE d.status IN (2, 3) AND d.datum BETWEEN ? AND ?
           GROUP BY nacin ORDER BY iznos DESC""" % ROBA_PLACANJA_SQL, a)

    # --- PDV: izlazni po stopama kako ga je Roba izračunala na računima, ulazni s primki
    pdv_izlazni = izlazni_pdv(con, od, do)
    ulazni = {}
    for r in con.execute(
            """SELECT s.pdv_stopa,
                      SUM(CASE d.vrsta WHEN 'povrat' THEN -s.iznos ELSE s.iznos END) AS osnovica
               FROM dok_stavke s JOIN dokumenti d ON d.id = s.dokument_id
               WHERE d.vrsta IN ('primka', 'povrat') AND d.datum BETWEEN ? AND ?
               GROUP BY s.pdv_stopa""", a):
        ulazni[r["pdv_stopa"]] = r["osnovica"]
    pdv_ulazni = [{"stopa": k, "osnovica": v, "pdv": pdv_na_osnovicu(v, k), "izvor": "naše primke i povrati"}
                  for k, v in sorted(ulazni.items())]
    roba_ul = con.execute(
        """SELECT COALESCE(SUM(neto), 0), COALESCE(SUM(porez), 0) FROM %s
           WHERE datum BETWEEN ? AND ?""" % ROBA_ULAZ_SQL, a).fetchone()
    if roba_ul[0]:
        pdv_ulazni.append({"stopa": None, "osnovica": roba_ul[0], "pdv": roba_ul[1],
                           "izvor": "primke iz Robe"})

    # --- zarada po artiklu (samo stavke s poznatom nabavnom cijenom)
    artikli = _redovi(con,
        """SELECT naziv, jm, SUM(kolicina) AS kolicina, SUM(iznos) AS iznos, SUM(neto) AS neto,
                  SUM(CASE WHEN nabavna > 0 THEN neto - nabavna END) AS zarada,
                  SUM(CASE WHEN nabavna > 0 THEN neto END) AS neto_s_nabavnom
           FROM roba_stavke WHERE status IN (2, 3) AND datum BETWEEN ? AND ?
           GROUP BY artikl, naziv ORDER BY iznos DESC""", a)
    zarada = sum(r["zarada"] or 0 for r in artikli)
    neto_s_nab = sum(r["neto_s_nabavnom"] or 0 for r in artikli)
    for r in artikli:
        r["marza"] = (round(r["zarada"] * 100.0 / r["neto_s_nabavnom"], 1)
                      if r["neto_s_nabavnom"] else None)
        del r["neto_s_nabavnom"]

    # --- ABC analiza: A = artikli koji donose prvih 80 % prometa, B = do 95 %, C = ostatak
    ukupno_art = sum(max(r["iznos"], 0) for r in artikli) or 1
    zbroj = 0
    abc = {"A": [0, 0], "B": [0, 0], "C": [0, 0]}
    for r in artikli:  # već poredano po prometu
        zbroj += max(r["iznos"], 0)
        udio = zbroj * 100.0 / ukupno_art
        r["abc"] = "A" if udio <= 80 or zbroj == max(r["iznos"], 0) else ("B" if udio <= 95 else "C")
        abc[r["abc"]][0] += 1
        abc[r["abc"]][1] += r["iznos"]
    abc_sazetak = [{"razred": k, "artikala": v[0], "iznos": v[1],
                    "udio": round(v[1] * 100.0 / ukupno_art, 1)} for k, v in abc.items()]

    # --- kad se prodaje: po satu i po danu u tjednu (za raspored rada)
    po_satima = _redovi(con,
        """SELECT CAST(substr(vrijeme, 12, 2) AS INTEGER) AS sat,
                  COUNT(CASE WHEN iznos > 0 THEN 1 END) AS racuna, SUM(iznos) AS iznos
           FROM roba_dokumenti WHERE status IN (2, 3) AND vrijeme IS NOT NULL
           AND datum BETWEEN ? AND ? GROUP BY sat ORDER BY sat""", a)
    dani = ["Nedjelja", "Ponedjeljak", "Utorak", "Srijeda", "Četvrtak", "Petak", "Subota"]
    po_danima = []
    for r in con.execute(
            """SELECT CAST(strftime('%w', datum) AS INTEGER) AS d, COUNT(DISTINCT datum) AS dana,
                      COUNT(CASE WHEN iznos > 0 THEN 1 END) AS racuna, SUM(iznos) AS iznos
               FROM roba_dokumenti WHERE status IN (2, 3) AND datum BETWEEN ? AND ?
               GROUP BY d""", a):
        po_danima.append({"dan": dani[r["d"]], "rb": (r["d"] + 6) % 7, "dana": r["dana"],
                          "racuna": r["racuna"], "iznos": r["iznos"],
                          "prosjek": int(round(r["iznos"] / float(r["dana"])))})
    po_danima.sort(key=lambda x: x["rb"])

    # --- nabava po dobavljaču (Roba + naše)
    nab = {}
    for r in con.execute(
            """SELECT partner AS dobavljac, SUM(primka) AS n, SUM(neto) AS neto, SUM(porez) AS pdv
               FROM %s WHERE datum BETWEEN ? AND ?
               GROUP BY partner""" % ROBA_ULAZ_SQL, a):
        nab[r["dobavljac"]] = [r["n"], r["neto"], r["pdv"]]
    for r in con.execute(
            """SELECT p.naziv AS dobavljac,
                      COUNT(CASE WHEN d.vrsta = 'primka' AND d.storno_od IS NULL
                                  AND d.storniran = 0 THEN 1 END) AS n,
                      SUM(CASE d.vrsta WHEN 'povrat' THEN -d.neto ELSE d.neto END) AS neto,
                      SUM(CASE d.vrsta WHEN 'povrat' THEN -d.pdv ELSE d.pdv END) AS pdv
               FROM dokumenti d JOIN partneri p ON p.id = d.partner_id
               WHERE d.vrsta IN ('primka', 'povrat') AND d.datum BETWEEN ? AND ? GROUP BY p.id""", a):
        z = nab.setdefault(r["dobavljac"], [0, 0, 0])
        z[0] += r["n"]
        z[1] += r["neto"]
        z[2] += r["pdv"]
    nabava = sorted(({"dobavljac": k, "primki": v[0], "neto": v[1], "pdv": v[2],
                      "ukupno": v[1] + v[2]} for k, v in nab.items()),
                    key=lambda x: -x["neto"])

    # --- otpisi po artiklu, po nabavnoj vrijednosti (Roba kalo + naši otpisi)
    otp = {}
    for r in con.execute(
            """SELECT naziv, jm, SUM(kolicina) AS kol, SUM(nabavna) AS vrijednost FROM roba_stavke
               WHERE status = 10 AND datum BETWEEN ? AND ? GROUP BY artikl, naziv""", a):
        otp[r["naziv"]] = [r["jm"], r["kol"], r["vrijednost"]]
    for r in con.execute(
            """SELECT s.naziv, s.jm, SUM(s.kolicina) AS kol, SUM(s.iznos) AS vrijednost
               FROM dok_stavke s JOIN dokumenti d ON d.id = s.dokument_id
               WHERE d.vrsta = 'otpis' AND d.datum BETWEEN ? AND ? GROUP BY s.artikl_id""", a):
        z = otp.setdefault(r["naziv"], [r["jm"], 0, 0])
        z[1] += r["kol"]
        z[2] += r["vrijednost"]
    # kalo u % prodane količine; Odluka HGK o dopuštenom manjku (NN 129/07) za voće i
    # povrće dopušta 2 - 6 % ovisno o vrsti, a manjak iznad toga treba pogledati s knjigovođom
    prodano = {r[0]: r[1] for r in con.execute(
        """SELECT naziv, SUM(kolicina) FROM roba_stavke WHERE status IN (2, 3)
           AND datum BETWEEN ? AND ? GROUP BY naziv""", a)}
    otpisi = sorted(({"naziv": k, "jm": v[0], "kolicina": round(v[1], 3), "vrijednost": v[2],
                      "kalo": round(v[1] / prodano[k] * 100, 1) if prodano.get(k, 0) > 0 else None}
                     for k, v in otp.items() if abs(v[1]) > 0.0005),
                    key=lambda x: -x["vrijednost"])

    zadnji = con.execute("SELECT MAX(datum) FROM roba_dokumenti WHERE status IN (2, 3)").fetchone()[0]
    uvoz = con.execute("SELECT vrijeme FROM uvozi ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "od": od, "do": do, "po_mjesecu": po_mjesecu,
        "zadnji_racun_u_robi": zadnji, "zadnji_uvoz": uvoz[0] if uvoz else None,
        "ukupno": dict(ukupno), "zarada": zarada,
        "marza": round(zarada * 100.0 / neto_s_nab, 1) if neto_s_nab else None,
        "promet": promet, "placanja": placanja,
        "pdv_izlazni": pdv_izlazni, "pdv_ulazni": pdv_ulazni,
        "artikli": artikli, "nabava": nabava, "otpisi": otpisi,
        "abc": abc_sazetak, "po_satima": po_satima, "po_danima": po_danima,
    }
