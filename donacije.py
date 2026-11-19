# -*- coding: utf-8 -*-
"""
Evidencija donacija hrane (modul "donacije").

Donacija smanjuje zalihu kao OTPIS: spremanje donacije napravi običan dokument otpisa
(nabava.spremi, vrsta "otpis", razlog "Donacija <primatelj>") po prosječnoj nabavnoj cijeni.
Zašto ne nova vrsta dokumenta: vrste su u CHECK ograničenju tablice "dokumenti" (promjena traži
prepisivanje tablice) i u izračunu zalihe (ZALIHA_SQL), a otpis već radi sve što treba (zaliha,
storno, ispis, knjigovođa). Ovdje se čuva samo ono što otpis nema: primatelj, OIB,
registrirani posrednik i napomena, uz vezu na otpis (dokument_id). Storno otpisa u Nabavi
poništava i donaciju (više se ne broji u godišnji zbroj).

Godišnji zbroj (nabavna vrijednost) uspoređuje se s 2 % prihoda prošle godine. Prihod upisuje
vlasnik (knjigovodstveni prihod nije u našoj bazi); kao pomoć se nudi promet iz Robe i kase.
Donacija hrane bez PDV-a: primatelj mora biti registrirani posrednik, do 2 % prihoda prošle
godine. Potvrdite s knjigovođom.
"""

import datetime as dt
import html

import nabava
import postavke
from baza import centi, oib_ispravan, pdv_iz_bruto
from nabava import Greska

UPOZORENJE = ("Donacija hrane bez PDV-a: primatelj mora biti registrirani posrednik, "
              "do 2 % prihoda prošle godine. Potvrdite s knjigovođom.")
POSTO = 2.0

SHEMA = """
CREATE TABLE IF NOT EXISTS donacije (
    id           INTEGER PRIMARY KEY,
    godina       INTEGER NOT NULL,
    broj         INTEGER NOT NULL,
    datum        TEXT NOT NULL,
    vrijeme      TEXT NOT NULL,
    primatelj    TEXT NOT NULL,
    oib          TEXT NOT NULL DEFAULT '',
    posrednik    INTEGER NOT NULL DEFAULT 0,   -- registrirani posrednik u lancu doniranja hrane
    napomena     TEXT NOT NULL DEFAULT '',
    dokument_id  INTEGER NOT NULL REFERENCES dokumenti(id),   -- otpis koji je smanjio zalihu
    radnik       TEXT NOT NULL DEFAULT '',
    UNIQUE (godina, broj)
);
"""


def pripremi(con):
    con.executescript(SHEMA)


def _red(r):
    d = dict(r)
    d["oznaka"] = "DON-%d-%d" % (d["godina"], d["broj"])
    d["otpis"] = "O-%d-%d" % (d["o_godina"], d["o_broj"]) if d.get("o_godina") else ""
    return d


_SELECT = """SELECT n.*, d.neto, d.storniran, d.godina AS o_godina, d.broj AS o_broj
             FROM donacije n JOIN dokumenti d ON d.id = n.dokument_id"""


def popis(con, godina):
    return [_red(r) for r in con.execute(_SELECT + " WHERE n.godina = ? ORDER BY n.datum DESC, n.id DESC",
                                         (int(godina),))]


def donacija(con, did):
    r = con.execute(_SELECT + " WHERE n.id = ?", (did,)).fetchone()
    if not r:
        raise Greska("Donacija ne postoji.")
    d = _red(r)
    d["stavke"] = [dict(s) for s in con.execute(
        "SELECT naziv, jm, kolicina, cijena, iznos FROM dok_stavke WHERE dokument_id = ? ORDER BY id",
        (d["dokument_id"],))]
    return d


def primatelji(con):
    return [dict(r) for r in con.execute(
        """SELECT primatelj, oib, posrednik FROM donacije WHERE id IN
           (SELECT MAX(id) FROM donacije GROUP BY primatelj, oib) ORDER BY primatelj""")]


def spremi(con, p, radnik=""):
    """Sprema donaciju: otpis (smanjuje zalihu) + zapis o primatelju. Vraća id donacije."""
    primatelj = " ".join(str(p.get("primatelj") or "").split())[:120]
    if not primatelj:
        raise Greska("Upišite primatelja donacije.")
    oib = str(p.get("oib") or "").strip()
    if not oib_ispravan(oib):
        raise Greska("OIB primatelja nije ispravan (11 znamenki, kontrolna znamenka se ne slaže).")
    posrednik = 1 if p.get("posrednik") in (True, 1, "1", "true") else 0
    napomena = " ".join(str(p.get("napomena") or "").split())[:200]
    stavke = [{"artikl_id": s.get("artikl_id"), "kolicina": s.get("kolicina")} for s in (p.get("stavke") or [])]
    razlog = "Donacija %s (OIB %s)%s" % (primatelj, oib, (": " + napomena) if napomena else "")
    # 1. otpis: provjere datuma, artikala i količina radi nabava.spremi (vlastita transakcija)
    dok_id = nabava.spremi(con, {"vrsta": "otpis", "datum": p.get("datum"), "napomena": razlog[:300],
                                 "stavke": stavke})
    # 2. zapis donacije; ako ne uspije, otpis se stornira da zaliha ostane točna
    try:
        datum = con.execute("SELECT datum FROM dokumenti WHERE id = ?", (dok_id,)).fetchone()[0]
        godina = int(datum[:4])
        con.execute("BEGIN IMMEDIATE")
        try:
            broj = con.execute("SELECT COALESCE(MAX(broj), 0) + 1 FROM donacije WHERE godina = ?",
                               (godina,)).fetchone()[0]
            did = con.execute(
                """INSERT INTO donacije (godina, broj, datum, vrijeme, primatelj, oib, posrednik, napomena,
                                         dokument_id, radnik) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (godina, broj, datum, nabava.sada(), primatelj, oib, posrednik, napomena, dok_id,
                 str(radnik or "")[:60])).lastrowid
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    except Exception:
        nabava.storniraj(con, dok_id)
        raise
    return did


# ---------------------------------------------------------------- godišnji zbroj i granica

def _prihod_kljuc(godina):
    return "donacije.prihod.%d" % godina


def prijedlog_prihoda(con, godina):
    """Promet godine iz naše baze, kao pomoć vlasniku (centi): Roba (bez PDV-a i s PDV-om)
    i računi naše kase."""
    od, do = "%d-01-01" % godina, "%d-12-31" % godina
    r = con.execute("""SELECT COALESCE(SUM(neto), 0), COALESCE(SUM(iznos), 0) FROM roba_dokumenti
                       WHERE status IN (2, 3) AND brisan = 0 AND datum BETWEEN ? AND ?""", (od, do)).fetchone()
    kasa_bruto, kasa_neto = 0, 0
    for stopa, iznos in con.execute(
            """SELECT s.pdv_stopa, SUM(s.iznos) FROM racun_stavke s JOIN racuni r ON r.id = s.racun_id
               WHERE r.godina = ? GROUP BY s.pdv_stopa""", (godina,)):
        kasa_bruto += iznos
        kasa_neto += iznos - pdv_iz_bruto(iznos, stopa)
    return {"roba_neto": r[0], "roba_bruto": r[1], "kasa_neto": kasa_neto, "kasa_bruto": kasa_bruto}


def pregled(con, godina=None):
    godina = int(godina or dt.date.today().year)
    prosla = godina - 1
    prihod = postavke.vrijednost(con, _prihod_kljuc(prosla), "")
    prihod = int(prihod) if prihod.strip().lstrip("-").isdigit() else 0
    zbroj = con.execute(
        """SELECT COALESCE(SUM(d.neto), 0), COUNT(*) FROM donacije n JOIN dokumenti d ON d.id = n.dokument_id
           WHERE n.godina = ? AND d.storniran = 0""", (godina,)).fetchone()
    granica = round(prihod * POSTO / 100) if prihod > 0 else 0
    posto = round(zbroj[0] * 100.0 / granica, 1) if granica else None
    razina = "" if posto is None else "crveno" if posto > 100 else "zuto" if posto >= 80 else "ok"
    return {"godina": godina, "prosla": prosla, "prihod": prihod, "granica": granica,
            "zbroj": zbroj[0], "broj": zbroj[1], "posto_granice": posto, "razina": razina,
            "prijedlog_prihoda": prijedlog_prihoda(con, prosla), "upozorenje": UPOZORENJE,
            "donacije": popis(con, godina), "primatelji": primatelji(con)}


def spremi_prihod(con, p):
    try:
        godina = int(p.get("godina"))
        v = str(p.get("prihod") or "").strip().replace(".", "").replace(",", ".") if "," in str(p.get("prihod") or "") \
            else str(p.get("prihod") or "").strip()
        iznos = centi(float(v)) if v else 0
    except (TypeError, ValueError):
        raise Greska("Neispravan iznos prihoda.")
    if not 2000 <= godina <= 2100 or iznos < 0 or iznos > 10 ** 13:
        raise Greska("Neispravan iznos prihoda.")
    postavke.postavi(con, _prihod_kljuc(godina), iznos)
    return pregled(con, godina + 1)


# ---------------------------------------------------------------- zapisnik (A4)

def _eur(c):
    s = "%.2f" % (abs(c) / 100.0)
    cijeli, dec = s.split(".")
    cijeli = "{:,}".format(int(cijeli)).replace(",", ".")
    return ("-" if c < 0 else "") + cijeli + "," + dec


def _kol(k):
    return ("%.3f" % k).rstrip("0").rstrip(".").replace(".", ",")


def zapisnik_html(con, did, naziv_trgovine):
    d = donacija(con, did)
    e = html.escape
    redovi = "".join(
        "<tr><td>%d</td><td>%s</td><td>%s</td><td class=r>%s</td><td class=r>%s</td><td class=r>%s</td></tr>"
        % (i, e(s["naziv"]), e(s["jm"]), _kol(s["kolicina"]), _eur(s["cijena"]), _eur(s["iznos"]))
        for i, s in enumerate(d["stavke"], 1))
    datum = ".".join(reversed(d["datum"].split("-"))) + "."
    storno = "<p><b>STORNIRANO</b> (otpis %s je storniran).</p>" % e(d["otpis"]) if d["storniran"] else ""
    return """<!doctype html><html lang=hr><head><meta charset=utf-8><title>%(oznaka)s</title>
<style>
@page { size: A4; margin: 15mm; }
body { font: 13px/1.4 Arial, sans-serif; color: #000; max-width: 190mm; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; } .m { color: #444; }
table { width: 100%%; border-collapse: collapse; margin-top: 14px; }
th, td { padding: 5px 6px; border-bottom: 1px solid #bbb; text-align: left; }
th { border-bottom: 2px solid #000; font-size: 12px; } .r { text-align: right; white-space: nowrap; }
.tot { width: 50%%; margin-left: auto; } .b td { font-weight: bold; border-top: 2px solid #000; }
.pot { display: flex; justify-content: space-between; margin-top: 60px; }
.pot div { flex: 1; margin: 0 4%%; border-top: 1px solid #000; padding-top: 4px; text-align: center; }
.nap { font-size: 11px; color: #444; margin-top: 18px; }
@media screen { body { margin: 24px auto; } } @media print { .gumbi { display: none; } }
</style></head><body>
<p class=m>%(trgovina)s</p>
<h1>ZAPISNIK O PREDAJI DONIRANE HRANE %(oznaka)s</h1>
<p>Datum predaje: <b>%(datum)s</b> &nbsp; · &nbsp; Upisano: %(vrijeme)s &nbsp; · &nbsp; Otpis zalihe: %(otpis)s</p>
<p>Primatelj: <b>%(primatelj)s</b>, OIB %(oib)s<br>
Registrirani posrednik u lancu doniranja hrane: <b>%(posrednik)s</b>%(napomena)s</p>
%(storno)s
<table><tr><th>#</th><th>Artikl</th><th>JM</th><th class=r>Količina</th><th class=r>Nab. cijena</th><th class=r>Nabavna vrijednost</th></tr>%(redovi)s</table>
<table class=tot><tr class=b><td>Ukupno po nabavnoj cijeni (bez PDV-a)</td><td class=r>%(ukupno)s</td></tr></table>
<p>Predana hrana je ispravna i sigurna za ljudsku upotrebu. Primatelj potvrđuje da je hranu preuzeo
u navedenoj količini.</p>
<div class=pot><div>Predao (donator)</div><div>Preuzeo (primatelj)</div></div>
<p class=nap>%(upozorenje)s</p>
<p class=gumbi><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button></p>
<script>if (location.hash === '#ispis') { addEventListener('load', () => print()); }</script>
</body></html>""" % {
        "oznaka": e(d["oznaka"]), "trgovina": e(naziv_trgovine), "datum": e(datum), "vrijeme": e(d["vrijeme"]),
        "otpis": e(d["otpis"]), "primatelj": e(d["primatelj"]), "oib": e(d["oib"]),
        "posrednik": "DA" if d["posrednik"] else "NE (donacija nije oslobođena PDV-a)",
        "napomena": ("<br>Napomena: " + e(d["napomena"])) if d["napomena"] else "",
        "storno": storno, "redovi": redovi, "ukupno": _eur(d["neto"]), "upozorenje": e(UPOZORENJE)}
