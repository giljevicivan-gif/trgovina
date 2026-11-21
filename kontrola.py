# -*- coding: utf-8 -*-
"""
Kontrola: popis stvari koje treba pogledati, na jednom mjestu.

Svaka provjera vraća razinu ("crveno" = treba odmah, "zuto" = pogledati,
"ok" = u redu), naslov, kratko objašnjenje i popis artikala/stavki.
"""

import datetime as dt
import os

import kopija
import kopija_van
import haccp
import bonovi
import dugovanja
import fiskal
import kupci_tvrtke
import rokovi
import narudzbe
import ponude
import postavke
import roba_dodatno
import sat
from baza import ZALIHA_SQL
from narudzbe import oznaka as oznaka_n

DANA_BEZ_PRODAJE = 7     # voće i povrće brzo propada
NISKA_MARZA = 10.0       # % na nabavnu
STARA_KOPIJA_DANA = 2
STARA_INVENTURA_DANA = 90
MAX_STAVKI = 50


def _eur(c):
    return ("%.2f" % (c / 100.0)).replace(".", ",")


def _kol(k):
    return ("%.3f" % k).rstrip("0").rstrip(".").replace(".", ",")


def kol_txt(k):
    return ("%.3f" % k).rstrip("0").rstrip(".").replace(".", ",")


def _hr(iso):
    try:
        d = dt.date.fromisoformat(str(iso)[:10])
    except ValueError:
        return str(iso)
    return "%d.%d.%d." % (d.day, d.month, d.year)


def _p(razina, naslov, opis, stavke=None, stupci=None):
    stavke = stavke or []
    return {"razina": razina, "naslov": naslov, "opis": opis, "ukupno": len(stavke),
            "stavke": stavke[:MAX_STAVKI], "stupci": stupci or []}


def provjere(con):
    out = []
    moduli = postavke.moduli(con)
    artikli = [dict(r) for r in con.execute(
        """SELECT a.id, a.sifra, a.naziv, a.jm, a.cijena, a.pdv_stopa, a.nabavna, a.usluga,
                  a.podrijetlo, a.sidrena_datum, a.akcija_prije, a.akcija_cijena, a.neto_jm, a.roba_sifra, round(%s, 3) AS zaliha
           FROM artikli a WHERE a.aktivan = 1 ORDER BY a.naziv""" % ZALIHA_SQL)]
    roba = [a for a in artikli if not a["usluga"]]

    # --- prodaja ispod nabavne i niska marža
    gubitak, niska = [], []
    for a in roba:
        if a["nabavna"] <= 0 or a["cijena"] <= 0:
            continue
        vpc = a["cijena"] / (1 + a["pdv_stopa"] / 100.0)
        marza = (vpc - a["nabavna"]) / a["nabavna"] * 100
        red = [a["naziv"], _eur(a["cijena"]), _eur(a["nabavna"]),
               ("%.1f %%" % marza).replace(".", ",")]
        if marza < 0:
            gubitak.append(red)
        elif marza < NISKA_MARZA:
            niska.append(red)
    st = ["Artikl", "MPC", "Nabavna bez PDV", "Marža"]
    out.append(_p("crveno" if gubitak else "ok", "Prodaja ispod nabavne cijene",
                  "MPC bez PDV-a je manja od prosječne nabavne. Podignite MPC u Robi ili provjerite primku."
                  if gubitak else "Nijedan artikl se ne prodaje ispod nabavne.", gubitak, st))
    if niska:
        out.append(_p("zuto", "Niska marža (ispod %g %%)" % NISKA_MARZA,
                      "Ovi artikli jedva pokrivaju nabavnu cijenu.", niska, st))

    # --- zaliha u minusu
    minus = [[a["naziv"], "%s %s" % (_kol(a["zaliha"]), a["jm"])] for a in roba if a["zaliha"] < 0]
    out.append(_p("zuto" if minus else "ok", "Zaliha u minusu",
                  "Prodano je više nego što je zaprimljeno. Najčešće fali primka ili treba inventura."
                  if minus else "Nema artikala u minusu.", minus, ["Artikl", "Zaliha"]))

    # --- roba koja stoji (ima je na zalihi, a ne prodaje se)
    zadnji = con.execute("SELECT MAX(datum) FROM roba_dokumenti WHERE status IN (2, 3)").fetchone()[0]
    if zadnji:
        od = (dt.date.fromisoformat(zadnji) - dt.timedelta(days=DANA_BEZ_PRODAJE - 1)).isoformat()
        prodano = {r[0] for r in con.execute(
            "SELECT DISTINCT artikl FROM roba_stavke WHERE status IN (2, 3) AND datum BETWEEN ? AND ?",
            (od, zadnji))}
        zadnja = {r[0]: r[1] for r in con.execute(
            "SELECT artikl, MAX(datum) FROM roba_stavke WHERE status IN (2, 3) GROUP BY artikl")}
        stoji = [[a["naziv"], "%s %s" % (_kol(a["zaliha"]), a["jm"]),
                  _hr(zadnja[a["roba_sifra"]]) if zadnja.get(a["roba_sifra"]) else "nikad",
                  _eur(int(round(a["zaliha"] * a["nabavna"])))]
                 for a in roba if a["zaliha"] > 0 and a["roba_sifra"] not in prodano]
        out.append(_p("zuto" if stoji else "ok", "Roba koja stoji (%d dana bez prodaje)" % DANA_BEZ_PRODAJE,
                      "Na zalihi je, a nije prodana. Provjerite je li pokvarena (otpis) ili je stavite na akciju."
                      if stoji else "Sve što je na zalihi se prodaje.",
                      stoji, ["Artikl", "Zaliha", "Zadnja prodaja", "Nabavna vrijednost"]))

    # --- etikete: podrijetlo i sidrena cijena
    bez_p = [[a["naziv"]] for a in roba if a["jm"].lower() == "kg" and not a["podrijetlo"]
             and moduli["voce_povrce"]]
    if bez_p:
        out.append(_p("zuto", "Rinfuzna roba bez zemlje podrijetla",
                      "Za voće i povrće podrijetlo mora pisati na etiketi. Upis: Zaliha, klik na naziv.",
                      bez_p, ["Artikl"]))
    bez_s = [[a["naziv"]] for a in roba if not a["sidrena_datum"]]
    if bez_s:
        out.append(_p("zuto", "Artikli bez sidrene cijene",
                      "Od 17. 11. 2026. sidrena cijena mora biti na etiketi.", bez_s, ["Artikl"]))

    bez_jed = [[a["naziv"], a["jm"]] for a in roba if a["jm"].lower() not in ("kg", "l") and not a["neto_jm"]]
    if bez_jed:
        out.append(_p("zuto", "Pakirana roba bez cijene za kg ili l",
                      "Hrana koja se prodaje na komad, a pakirana je (suho voće, ulje, sok...), mora uz cijenu "
                      "imati i cijenu za kg ili l (Pravilnik NN 105/2026, čl. 8). Upis: Zaliha, klik na naziv, "
                      "\"Pakiranje\". Voće i povrće na komad ili vezicu i pakiranja ispod 50 g označite "
                      "\"ne treba\".", bez_jed, ["Artikl", "JM"]))

    akc = [[a["naziv"]] for a in roba if "AKCIJA" in a["naziv"].upper() and not a["akcija_prije"]]
    if akc:
        out.append(_p("zuto", "Akcija bez najniže cijene u 30 dana",
                      "Uz sniženu cijenu mora pisati najniža cijena u 30 dana prije akcije "
                      "(Zakon o zaštiti potrošača). Upis: Zaliha, klik na naziv, \"Predloži\".",
                      akc, ["Artikl"]))

    nesnizeno = [a for a in roba if a["akcija_prije"] and a["cijena"] >= a["akcija_prije"]]
    planirane = [[a["naziv"], _eur(a["cijena"]), _eur(a["akcija_cijena"])]
                 for a in nesnizeno if a["akcija_cijena"]]
    if planirane:
        out.append(_p("zuto", "Akcija čeka upis cijene u Robu",
                      "Upišite akcijsku MPC u Robu, kliknite \"Osvježi iz Robe\" pa ispišite etikete. "
                      "Do tada etiketa ne piše AKCIJA.", planirane, ["Artikl", "MPC u Robi", "Akcijska"]))
    lazna = [[a["naziv"], _eur(a["cijena"]), _eur(a["akcija_prije"])]
             for a in nesnizeno if not a["akcija_cijena"]]
    if lazna:
        out.append(_p("crveno", "Akcija bez stvarnog sniženja",
                      "Cijena u Robi nije niža od najniže cijene u 30 dana, pa to nije sniženje "
                      "(Zakon o zaštiti potrošača). Etiketa zato ne piše AKCIJA. Spustite MPC u Robi "
                      "ili završite akciju (Zaliha, Akcije).", lazna, ["Artikl", "MPC u Robi", "Najniža u 30 dana"]))
    ceka = [[a["naziv"], _eur(a["cijena"]), _eur(a["akcija_cijena"])] for a in roba
            if a["akcija_cijena"] and a["cijena"] != a["akcija_cijena"] and a["cijena"] < a["akcija_prije"]]
    if ceka:
        out.append(_p("zuto", "Akcijska cijena drukčija nego u Robi",
                      "Na polici i na računu vrijedi cijena iz Robe. Ako je planirana druga, ispravite je "
                      "u Robi pa ispišite etikete.", ceka, ["Artikl", "MPC u Robi", "Planirano"]))

    # --- sigurnosna kopija
    kop = kopija.popis()
    if not kop:
        out.append(_p("crveno", "Nema sigurnosne kopije", "Kliknite \"Sigurnosna kopija\" gore desno."))
    else:
        zad = max(k["vrijeme"] for k in kop)
        star = (dt.datetime.now() - dt.datetime.strptime(zad, "%Y-%m-%d %H:%M:%S")).days
        if star >= STARA_KOPIJA_DANA:
            out.append(_p("crveno", "Sigurnosna kopija je stara", "Zadnja je od %s." % _hr(zad)))
        elif not os.environ.get("TRGOVINA_KOPIJA_MAPA", "").strip():
            out.append(_p("zuto", "Kopija je samo na ovom računalu",
                          "Ako se računalo pokvari, kopija nestaje s njim. U pokreni.bat upišite "
                          "TRGOVINA_KOPIJA_MAPA (USB ili OneDrive)."))
        else:
            out.append(_p("ok", "Sigurnosna kopija", "Zadnja je od %s." % _hr(zad)))

    # --- podaci iz Robe
    uvoz = con.execute("SELECT MAX(vrijeme) FROM uvozi").fetchone()[0]
    if not uvoz:
        out.append(_p("crveno", "Podaci iz Robe nisu učitani", "Pokrenite kasu preko pokreni.bat."))
    elif zadnji and (dt.date.today() - dt.date.fromisoformat(zadnji)).days > 3:
        out.append(_p("zuto", "Nema novih računa iz Robe",
                      "Zadnji račun iz Robe je od %s. Kliknite \"Osvježi iz Robe\"." % _hr(zadnji)))

    # stope PDV-a: u RH su 25, 13 i 5 %; 0 % nema za robu u ovoj trgovini.
    # 5 %: svježe i rashlađeno povrće, svježe i suho voće, orašasti plodovi, jestiva ulja,
    # sadnice i sjemenje (Zakon o PDV-u, čl. 38). Točnu stopu određuje KN oznaka proizvoda.
    nula = [[r[0], "%s - %s" % (_hr(r[1]), _hr(r[2])), _eur(r[3])] for r in con.execute(
        """SELECT naziv, MIN(datum), MAX(datum), SUM(iznos) FROM roba_stavke
           WHERE status IN (2, 3) AND pdv_stopa = 0 AND iznos != 0 GROUP BY naziv ORDER BY 4 DESC""")]
    if nula:
        out.append(_p("crveno", "Prodaja bez PDV-a (stopa 0 %)",
                      "Ovi artikli su u Robi prodani po stopi 0 %. Za voće, povrće, sokove, ulje i biljke "
                      "0 % ne postoji. Pokažite knjigovođi: možda treba ispraviti stopu u Robi i "
                      "PDV za to razdoblje.", nula, ["Artikl", "Razdoblje", "Prodano"]))
    ne5 = [[a["naziv"], ("%g %%" % a["pdv_stopa"]).replace(".", ",")] for a in roba if a["pdv_stopa"] != 5
           and moduli["voce_povrce"]]
    if ne5:
        out.append(_p("zuto", "Artikli kojima stopa PDV-a nije 5 %",
                      "Svježe i suho voće, povrće, orašasti plodovi, jestiva ulja i sadnice su po 5 %. "
                      "Prerađeno (npr. slano pečeno, kiselo) može biti 25 %. Provjerite s knjigovođom "
                      "prema KN oznaci; stopa se mijenja u Robi.", ne5, ["Artikl", "Stopa"]))

    # računi u Robi kojima zbroj stavki nije jednak ukupnom iznosu (ili nemaju stavki);
    # tada se izvještaj po artiklima i po stopama PDV-a ne slaže s ukupnim prometom.
    # Račun R1 (status 2) ide od osnovice: PDV je na zbroj osnovica, pa se uspoređuje osnovica.
    krivi = [[r["dokument"], _hr(r["datum"]), _eur(r["iznos"]) + (" (osnovica)" if r["status"] == 2 else ""),
              _eur(r["stavke"] or 0)] for r in con.execute(
        """SELECT d.dokument, d.datum, d.status,
                  CASE WHEN d.status = 2 THEN d.neto ELSE d.iznos END AS iznos,
                  (SELECT SUM(CASE WHEN d.status = 2 THEN s.neto ELSE s.iznos END) FROM roba_stavke s
                     WHERE s.godina = d.godina AND s.link = d.sifra AND s.status IN (2, 3)) AS stavke
           FROM roba_dokumenti d WHERE d.status IN (2, 3) AND d.brisan = 0
           AND COALESCE((SELECT SUM(CASE WHEN d.status = 2 THEN s.neto ELSE s.iznos END)
                         FROM roba_stavke s WHERE s.godina = d.godina
                         AND s.link = d.sifra AND s.status IN (2, 3)), 0)
               != CASE WHEN d.status = 2 THEN d.neto ELSE d.iznos END
           ORDER BY d.datum""")]
    if krivi:
        out.append(_p("zuto", "Računi u Robi kojima se stavke ne slažu s ukupnim iznosom",
                      "Zbroj stavki nije jednak iznosu računa (ili račun nema stavki). Pogledajte te račune u "
                      "Robi i pitajte podršku Robe; fiskalizirani iznos je onaj na računu.",
                      krivi, ["Račun", "Datum", "Iznos računa", "Zbroj stavki"]))

    # isti artikl prodan po dvije stope PDV-a (npr. bojanka jednom 5 %, jednom 25 %)
    dvije = [[r["naziv"], r["stope"].replace(".0", "").replace(",", " i ") + " %", _hr(r["zadnji"])]
             for r in con.execute(
        """SELECT MAX(naziv) AS naziv, group_concat(DISTINCT pdv_stopa) AS stope, MAX(datum) AS zadnji
           FROM roba_stavke WHERE status IN (2, 3) AND artikl IS NOT NULL AND iznos != 0
           GROUP BY artikl HAVING COUNT(DISTINCT pdv_stopa) > 1 ORDER BY naziv""")]
    if dvije:
        out.append(_p("zuto", "Artikl prodan po različitim stopama PDV-a",
                      "Isti artikl je u Robi prodan po dvije stope. Ako se stopa mijenjala greškom, "
                      "pokažite knjigovođi: PDV za te račune možda treba ispraviti.",
                      dvije, ["Artikl", "Stope", "Zadnja prodaja"]))

    # način plaćanja u zaglavlju računa i u Robinim plaćanjima nije isti (npr. naknadno
    # promijenjen); tada se gotovina u blagajni ne slaže s Robom
    plac = [[r["dokument"], _hr(r["datum"]), _eur(r["iznos"]), r["zaglavlje"] or "", r["placanja"] or ""]
            for r in con.execute(
        """SELECT d.dokument, d.datum, d.iznos, n1.naziv AS zaglavlje,
                  group_concat(COALESCE(n2.naziv, p.placanje), ' + ') AS placanja
           FROM roba_dokumenti d JOIN roba_placanja p ON p.godina = d.godina AND p.link = d.sifra
           LEFT JOIN nacini_placanja n1 ON n1.roba_sifra = d.placanje
           LEFT JOIN nacini_placanja n2 ON n2.roba_sifra = p.placanje
           WHERE d.status IN (2, 3) AND d.placanje != -2
           GROUP BY d.godina, d.sifra HAVING SUM(p.placanje != d.placanje) > 0 ORDER BY d.datum""")]
    if plac:
        out.append(_p("zuto", "Računi s dva različita načina plaćanja u Robi",
                      "Na računu piše jedan način plaćanja, a u Robinim plaćanjima drugi. Provjerite u "
                      "Robi kako je račun stvarno plaćen; blagajna ovdje računa po zaglavlju računa.",
                      plac, ["Račun", "Datum", "Iznos", "Na računu", "U plaćanjima"]))

    prva = con.execute("SELECT MIN(datum) FROM dokumenti WHERE vrsta = 'primka'").fetchone()[0]
    if prva:
        dupl = [[r["dokument"], _hr(r["datum"]), r["partner"]] for r in con.execute(
            """SELECT dokument, datum, partner FROM roba_dokumenti
               WHERE status = 1 AND brisan = 0 AND datum >= ? ORDER BY datum""", (prva,))]
        if dupl:
            out.append(_p("crveno", "Primke upisane i u Robi",
                          "Primke vodimo ovdje od %s, a ove su upisane i u Robu. Zaliha za te artikle "
                          "može biti dvostruka." % _hr(prva), dupl, ["Dokument", "Datum", "Dobavljač"]))

    # --- narudžbe kupaca
    danas = dt.date.today().isoformat()
    kasni = [[r["oznaka"], r["kupac"], _hr(r["rok"])] for r in narudzbe.popis(con)
             if r["rok"] < danas and moduli["narudzbe"]]
    if kasni:
        out.append(_p("crveno", "Narudžbe kojima je prošao rok",
                      "Isporučite ih ili otkažite (kartica Narudžbe).", kasni, ["Narudžba", "Kupac", "Rok"]))
    bez_racuna = [[oznaka_n(r), r["kupac"], _hr(r["rok"]), r["rok"]] for r in con.execute(
        """SELECT n.godina, n.broj, n.rok, k.naziv AS kupac FROM narudzbe n
           JOIN kupci k ON k.id = n.kupac_id
           WHERE n.status = 'isporucena' AND n.racun_roba = '' ORDER BY n.rok""")] if moduli["narudzbe"] else []
    zbirni = moduli["narudzbe"] and moduli["kupci_tvrtke"]
    if zbirni:
        # zbirni račun smije obuhvatiti isporuke istog mjeseca: tekući mjesec čeka, stariji je propust
        ceka = [r for r in bez_racuna if r[3][:7] == danas[:7]]
        bez_racuna = [r for r in bez_racuna if r[3][:7] != danas[:7]]
        if ceka:
            out.append(_p("zuto", "Isporuke ovog mjeseca čekaju račun",
                          "Ako kupac plaća virmanom, na kraju mjeseca izdajte zbirni račun u Robi "
                          "(Narudžbe, Kupci i cjenici, Zbirni pregled). Gotovina ili kartica: račun odmah.",
                          [r[:3] for r in ceka], ["Narudžba", "Kupac", "Rok"]))
    if bez_racuna:
        out.append(_p("crveno", "Isporučene narudžbe bez računa iz Robe",
                      "Za svaku isporuku mora se izdati fiskalizirani račun u Robi. "
                      "Upišite njegov broj u narudžbu" + (" ili zbirni pregled kupca." if zbirni else "."),
                      [r[:3] for r in bez_racuna], ["Narudžba", "Kupac", "Rok"]))
    if zbirni:
        ispod = [[r["kupac"], r["naziv"], kupci_tvrtke._eur(r["cijena"]), kupci_tvrtke._eur(r["nab"])]
                 for r in con.execute(
                     """SELECT k.naziv AS kupac, a.naziv, c.cijena,
                               CAST(round(a.nabavna * (1 + a.pdv_stopa / 100.0)) AS INTEGER) AS nab
                        FROM kupac_cijene c JOIN kupci k ON k.id = c.kupac_id JOIN artikli a ON a.id = c.artikl_id
                        WHERE a.nabavna > 0 AND c.cijena < round(a.nabavna * (1 + a.pdv_stopa / 100.0))
                        ORDER BY k.naziv, a.naziv""")]
        if ispod:
            out.append(_p("zuto", "Ugovorene cijene ispod nabavne",
                          "Nabavna je u međuvremenu porasla. Dogovorite novu cijenu s kupcem.",
                          ispod, ["Kupac", "Artikl", "Ugovorena", "Nabavna s PDV"]))

    # --- HACCP (samo ako je modul uključen)
    if moduli["haccp"]:
        h = haccp.upozorenja(con)
        if h["nema_uredjaja"] or h["nema_plana"]:
            out.append(_p("zuto", "HACCP nije postavljen",
                          "Upišite rashladne uređaje i plan čišćenja (kartica HACCP, Postavke HACCP-a)."))
        if h["bez_temperature"]:
            out.append(_p("zuto", "Temperatura danas nije upisana",
                          "Upišite temperaturu u kartici HACCP.", [[x] for x in h["bez_temperature"]], ["Uređaj"]))
        if h["ciscenje_kasni"]:
            out.append(_p("zuto", "Čišćenje prema planu nije upisano",
                          "Prema planu čišćenja ovo je trebalo biti napravljeno.",
                          [[x] for x in h["ciscenje_kasni"]], ["Što"]))

    # --- rokovi trajanja
    if moduli["rokovi"]:
        r = rokovi.upozorenja(con)
        red = lambda x: [x["naziv"], kol_txt(x["kolicina"]) + " " + x["jm"], _hr(x["rok"])]
        if r["zabrana"]:
            out.append(_p("crveno", "Istekao rok \"upotrijebiti do\"",
                          "Ova roba se ne smije prodavati ni donirati. Maknite je s police i napravite otpis "
                          "(Zaliha, Rokovi trajanja).", [red(x) for x in r["zabrana"]], ["Artikl", "Količina", "Rok"]))
        if r["izdvojiti"]:
            out.append(_p("zuto", "Istekao rok \"najbolje upotrijebiti do\"",
                          "Smije se prodavati samo izdvojeno i uz jasnu obavijest kupcima, ili donirati ili otpisati.",
                          [red(x) for x in r["izdvojiti"]], ["Artikl", "Količina", "Rok"]))
        if r["uskoro"]:
            out.append(_p("zuto", "Rok ističe za %d dana ili manje" % rokovi.UPOZORI_DANA,
                          "Stavite na akciju ili na vidljivije mjesto.", [red(x) for x in r["uskoro"]],
                          ["Artikl", "Količina", "Rok"]))

    # --- poklon bonovi
    if moduli["bonovi"]:
        b = bonovi.upozorenja_bonova(con)
        if b["bez_racuna"]:
            out.append(_p("zuto", "Poklon bonovi bez računa iz Robe",
                          "Prodaja bona mora proći kroz Robu (fiskalizirani račun). Upišite broj računa uz bon.",
                          [[x["kod"], _hr(x["izdan"])] for x in b["bez_racuna"]], ["Bon", "Izdan"]))

    # --- dugovanja prema dobavljačima
    if moduli["dugovanja"]:
        d = dugovanja.upozorenja(con)
        if d["kasni"]:
            out.append(_p("crveno", "Računi dobavljača kojima je prošlo dospijeće",
                          "Platite ih ili dogovorite novi rok s dobavljačem (Nabava, Dugovanja).",
                          [[x["oznaka"], x["partner"] or "", _hr(x["dospijece"]), "%.2f" % (x["ostalo"] / 100.0)]
                           for x in d["kasni"]], ["Primka", "Dobavljač", "Dospijeće", "Ostalo EUR"]))
        if d["predug_rok"]:
            out.append(_p("zuto", "Rok plaćanja dulji od 60 dana",
                          "Između poduzetnika rok smije biti najviše 60 dana, dulje samo uz pisani ugovor "
                          "(Zakon o financijskom poslovanju i predstečajnoj nagodbi).",
                          [[x["oznaka"], x["partner"] or "", str(x["dana_roka"])] for x in d["predug_rok"]],
                          ["Primka", "Dobavljač", "Dana"]))

    # --- podaci iz novije Robe: sidrena cijena i KPD
    raz = roba_dodatno.razlike_sidrene(con)
    if raz:
        e = lambda c: ("%.2f" % (c / 100.0)).replace(".", ",")
        st = [[r["naziv"], e(r["sidrena"]) + " (" + _hr(r["sidrena_datum"]) + ")",
               e(r["roba_sidrena"]) + " (" + _hr(r["roba_sidrena_datum"]) + ")"] for r in raz]
        if moduli["roba_sidrena"]:
            out.append(_p("zuto", "Ručno upisana sidrena cijena razlikuje se od Robine",
                          "Etiketa koristi našu (ručnu). Ako je Robina točna, upišite je u karticu artikla.",
                          st, ["Artikl", "Naša", "U Robi"]))
        else:
            out.append(_p("zuto", "Roba ima drukčiju sidrenu cijenu",
                          "Novija Roba sama računa sidrenu cijenu. Uključite \"Sidrena cijena iz Robe\" u Postavkama "
                          "ili provjerite koja je točna.", st, ["Artikl", "Naša", "U Robi"]))
    if moduli["kpd"]:
        bk = roba_dodatno.bez_kpd(con)
        if bk:
            out.append(_p("zuto", "Artikli prodani tvrtkama bez ispravne KPD šifre",
                          "KPD šifra je obvezna na eRačunu i mora imati točno 6 znamenki (npr. 01.13.12). "
                          "Upišite je u Robi (šifrarnik artikala) ili kod nas (Zaliha, klik na naziv).",
                          [[x["naziv"], x["kpd"], x["problem"], str(x["racuna"])] for x in bk],
                          ["Artikl", "KPD", "Problem", "Računa"]))
    if moduli["ponude"] and moduli["narudzbe"]:
        ist = [p for p in ponude.popis(con) if p["istekla"]]
        if ist:
            out.append(_p("zuto", "Ponude kojima je istekao rok",
                          "Javite se kupcu ili ponudu označite kao odbijenu (Narudžbe, Ponude).",
                          [[x["oznaka"], x["kupac"], _hr(x["vrijedi_do"])] for x in ist], ["Ponuda", "Kupac", "Vrijedi do"]))

    # --- blagajna i inventura
    sm = con.execute("SELECT otvorena FROM smjene WHERE zatvorena IS NULL").fetchone()
    if sm and (dt.datetime.now() - dt.datetime.strptime(sm[0], "%Y-%m-%d %H:%M:%S")).total_seconds() > 18 * 3600:
        out.append(_p("zuto", "Smjena nije zaključena",
                      "Smjena je otvorena od %s. Zaključite je u kartici Blagajna." % _hr(sm[0])))
    inv = con.execute("SELECT MAX(datum) FROM dokumenti WHERE vrsta = 'inventura'").fetchone()[0]
    if not inv:
        out.append(_p("zuto", "Inventura još nije napravljena",
                      "Stanje iz Robe nije pouzdano. Jedna puna inventura (Zaliha, Inventura) to ispravlja."))
    elif (dt.date.today() - dt.date.fromisoformat(inv)).days > STARA_INVENTURA_DANA:
        out.append(_p("zuto", "Inventura je stara", "Zadnja je od %s." % _hr(inv)))

    out[:0] = fiskal.kontrola(con)  # računi bez JIR-a i certifikat
    out[:0] = sat.kontrola(con)  # sat računala kasni (Porezna: greška 174)
    out[:0] = kopija_van.kontrola(con)  # kopija izvan računala (modul kopija_dojava)
    redoslijed = {"crveno": 0, "zuto": 1, "ok": 2}
    out.sort(key=lambda p: redoslijed[p["razina"]])
    return out
