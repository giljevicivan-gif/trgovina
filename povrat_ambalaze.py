# -*- coding: utf-8 -*-
"""
Povrat ambalaže na kasi: isplata povratne naknade (modul "povrat_ambalaze").

Trgovina koja prodaje pića u povratnoj ambalaži mora od kupca primiti praznu
bocu ili limenku i isplatiti mu povratnu naknadu (od 1. 1. 2025. 0,10 € po
komadu, Uredba o naknadi gospodarenja otpadom i povratnoj naknadi, NN 137/2024).
Trgovina taj novac dobije natrag od Fonda za zaštitu okoliša (FZOEU) preko
skupljača ambalaže.

To NIJE prodaja i NIJE stavka fiskalnog računa: novac izlazi iz ladice kao
isplata, a kupcu se (po želji) ispiše potvrda "nije račun". Ako je u Blagajni
otvorena smjena, isplata se upiše i tamo, pa prebrojana gotovina odgovara.

Nije isto što i modul "ambalaza" (ambalaza.py): tamo su gajbe i palete koje
dugujemo dobavljačima.

Potvrdite s knjigovođom i Fondom kako se vodi i potražuje isplaćena naknada.
"""

import csv
import datetime as dt
import io

from baza import centi
from nabava import Greska

# naziv, iznos po komadu (centi); iznos se mijenja u dijalogu (Vrste i iznosi)
POCETNE = (("Boca/limenka", 10),)
NAPOMENA = "Isplata povratne naknade za ambalažu — nije račun"
MAX_KOMADA = 5000

SHEMA = """
CREATE TABLE IF NOT EXISTS povrat_vrste (
    id        INTEGER PRIMARY KEY,
    naziv     TEXT NOT NULL UNIQUE,
    iznos     INTEGER NOT NULL,              -- povratna naknada po komadu, centi
    aktivan   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS povrat_isplate (
    id         INTEGER PRIMARY KEY,
    godina     INTEGER NOT NULL,
    broj       INTEGER NOT NULL,
    vrijeme    TEXT NOT NULL,
    datum      TEXT NOT NULL,
    komada     INTEGER NOT NULL,
    ukupno     INTEGER NOT NULL,             -- isplaćeno kupcu, centi
    radnik     TEXT NOT NULL DEFAULT '',
    smjena_id  INTEGER,                      -- smjena u Blagajni (ako je bila otvorena)
    promet_id  INTEGER,                      -- redak u blagajna_promet
    ponisteno  TEXT NOT NULL DEFAULT '',     -- vrijeme poništenja
    UNIQUE (godina, broj)
);
CREATE TABLE IF NOT EXISTS povrat_stavke (
    id          INTEGER PRIMARY KEY,
    isplata_id  INTEGER NOT NULL REFERENCES povrat_isplate(id),
    vrsta_id    INTEGER NOT NULL REFERENCES povrat_vrste(id),
    naziv       TEXT NOT NULL,
    iznos       INTEGER NOT NULL,            -- po komadu u trenutku isplate, centi
    komada      INTEGER NOT NULL,
    ukupno      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_povrat_isplate_datum ON povrat_isplate(datum);
"""


def sada():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def eur(c):
    return ("%.2f" % (c / 100.0)).replace(".", ",")


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def oznaka(r):
    return "PN-%d-%d" % (r["godina"], r["broj"])


def pripremi(con):
    con.executescript(SHEMA)
    if not con.execute("SELECT 1 FROM povrat_vrste").fetchone():
        con.executemany("INSERT INTO povrat_vrste (naziv, iznos) VALUES (?, ?)", POCETNE)
    con.commit()


def vrste(con, sve=False):
    return [dict(r) for r in con.execute(
        "SELECT * FROM povrat_vrste" + ("" if sve else " WHERE aktivan = 1") + " ORDER BY id")]


def spremi_vrstu(con, p):
    naziv = " ".join(str(p.get("naziv") or "").split())[:60]
    if not naziv:
        raise Greska("Upišite naziv vrste (npr. Boca/limenka).")
    try:
        iznos = centi(float(str(p.get("iznos")).replace(",", ".")))
    except (TypeError, ValueError):
        raise Greska("Neispravan iznos naknade po komadu.")
    if not 0 < iznos <= 1000:
        raise Greska("Naknada po komadu mora biti između 0,01 i 10,00 €.")
    aktivan = 0 if p.get("aktivan") in (False, 0, "0") else 1
    vid = int(p.get("id") or 0)
    with con:
        if con.execute("SELECT 1 FROM povrat_vrste WHERE naziv = ? AND id != ?", (naziv, vid)).fetchone():
            raise Greska("Vrsta s tim nazivom već postoji.")
        if vid:
            if not con.execute("SELECT 1 FROM povrat_vrste WHERE id = ?", (vid,)).fetchone():
                raise Greska("Vrsta ne postoji.")
            con.execute("UPDATE povrat_vrste SET naziv = ?, iznos = ?, aktivan = ? WHERE id = ?",
                        (naziv, iznos, aktivan, vid))
        else:
            vid = con.execute("INSERT INTO povrat_vrste (naziv, iznos, aktivan) VALUES (?, ?, ?)",
                              (naziv, iznos, aktivan)).lastrowid
    return vid


def isplati(con, p, radnik=""):
    """p = {stavke: [{vrsta_id, komada}]}. Sprema isplatu i (ako je smjena otvorena)
    upisuje je u blagajnu kao isplatu iz ladice. Vraća isplatu."""
    po_vrsti = {}
    for s in p.get("stavke") or []:
        if not isinstance(s, dict):
            raise Greska("Neispravna stavka.")
        kom = s.get("komada")
        if isinstance(kom, float) and not kom.is_integer():
            raise Greska("Broj komada mora biti cijeli broj.")
        kom = int(kom or 0)
        if kom < 0:
            raise Greska("Broj komada ne može biti negativan.")
        if kom:
            vid = int(s.get("vrsta_id") or 0)
            po_vrsti[vid] = po_vrsti.get(vid, 0) + kom
    if not po_vrsti:
        raise Greska("Upišite koliko je boca ili limenki vraćeno.")
    if sum(po_vrsti.values()) > MAX_KOMADA:
        raise Greska("Najviše %d komada u jednoj isplati." % MAX_KOMADA)
    t = sada()
    with con:
        stavke = []
        for vid, kom in po_vrsti.items():
            v = con.execute("SELECT * FROM povrat_vrste WHERE id = ? AND aktivan = 1", (vid,)).fetchone()
            if not v:
                raise Greska("Vrsta ambalaže ne postoji ili je isključena.")
            stavke.append((v, kom, v["iznos"] * kom))
        ukupno = sum(s[2] for s in stavke)
        komada = sum(s[1] for s in stavke)
        godina = int(t[:4])
        broj = con.execute("SELECT COALESCE(MAX(broj), 0) + 1 FROM povrat_isplate WHERE godina = ?",
                           (godina,)).fetchone()[0]
        smjena = con.execute("SELECT id FROM smjene WHERE zatvorena IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        promet_id = None
        if smjena:
            promet_id = con.execute(
                "INSERT INTO blagajna_promet (smjena_id, vrijeme, iznos, opis) VALUES (?, ?, ?, ?)",
                (smjena["id"], t, -ukupno, "Povratna naknada PN-%d-%d (%d kom)" % (godina, broj, komada))).lastrowid
        iid = con.execute(
            """INSERT INTO povrat_isplate (godina, broj, vrijeme, datum, komada, ukupno, radnik, smjena_id, promet_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (godina, broj, t, t[:10], komada, ukupno, str(radnik or "")[:60],
             smjena["id"] if smjena else None, promet_id)).lastrowid
        for v, kom, uk in stavke:
            con.execute("""INSERT INTO povrat_stavke (isplata_id, vrsta_id, naziv, iznos, komada, ukupno)
                           VALUES (?, ?, ?, ?, ?, ?)""", (iid, v["id"], v["naziv"], v["iznos"], kom, uk))
    return isplata(con, iid)


def isplata(con, iid):
    r = con.execute("SELECT * FROM povrat_isplate WHERE id = ?", (iid,)).fetchone()
    if not r:
        raise Greska("Isplata ne postoji.")
    d = dict(r)
    d["oznaka"] = oznaka(r)
    d["stavke"] = [dict(s) for s in con.execute(
        "SELECT * FROM povrat_stavke WHERE isplata_id = ? ORDER BY id", (iid,))]
    return d


def ponisti(con, iid):
    """Poništenje krivo upisane isplate. Ako je njezina smjena još otvorena, iznos se
    u blagajni vraća (polog); iz zaključene smjene se ne dira."""
    t = sada()
    with con:
        r = con.execute("SELECT * FROM povrat_isplate WHERE id = ?", (int(iid or 0),)).fetchone()
        if not r:
            raise Greska("Isplata ne postoji.")
        if r["ponisteno"]:
            raise Greska("Isplata je već poništena.")
        con.execute("UPDATE povrat_isplate SET ponisteno = ? WHERE id = ?", (t, r["id"]))
        if r["promet_id"]:
            s = con.execute("SELECT id FROM smjene WHERE id = ? AND zatvorena IS NULL", (r["smjena_id"],)).fetchone()
            if s:
                con.execute("INSERT INTO blagajna_promet (smjena_id, vrijeme, iznos, opis) VALUES (?, ?, ?, ?)",
                            (s["id"], t, r["ukupno"], "Poništena povratna naknada %s" % oznaka(r)))
    return isplata(con, r["id"])


def zadnje(con, n=20):
    return [dict(r, oznaka=oznaka(r)) for r in con.execute(
        "SELECT * FROM povrat_isplate ORDER BY id DESC LIMIT ?", (n,))]


def _razdoblje(od, do):
    danas = dt.date.today()
    try:
        od = dt.date.fromisoformat(od).isoformat() if od else danas.replace(day=1).isoformat()
        do = dt.date.fromisoformat(do).isoformat() if do else danas.isoformat()
    except ValueError:
        raise Greska("Neispravan datum.")
    return (od, do) if od <= do else (do, od)


def pregled(con, od=None, do=None):
    """Zbroj po vrsti i po danu (za potraživanje od Fonda). Poništene isplate se ne broje."""
    od, do = _razdoblje(od, do)
    uvjet = "i.ponisteno = '' AND i.datum BETWEEN ? AND ?"
    po_vrstama = [dict(r) for r in con.execute(
        """SELECT s.naziv, s.iznos, SUM(s.komada) AS komada, SUM(s.ukupno) AS ukupno
           FROM povrat_stavke s JOIN povrat_isplate i ON i.id = s.isplata_id
           WHERE %s GROUP BY s.naziv, s.iznos ORDER BY s.naziv, s.iznos""" % uvjet, (od, do))]
    po_danima = [dict(r) for r in con.execute(
        """SELECT i.datum, s.naziv, s.iznos, SUM(s.komada) AS komada, SUM(s.ukupno) AS ukupno
           FROM povrat_stavke s JOIN povrat_isplate i ON i.id = s.isplata_id
           WHERE %s GROUP BY i.datum, s.naziv, s.iznos ORDER BY i.datum, s.naziv""" % uvjet, (od, do))]
    isplata_n = con.execute("SELECT COUNT(*) FROM povrat_isplate i WHERE %s" % uvjet, (od, do)).fetchone()[0]
    return {"od": od, "do": do, "po_vrstama": po_vrstama, "po_danima": po_danima, "isplata": isplata_n,
            "komada": sum(x["komada"] for x in po_vrstama), "ukupno": sum(x["ukupno"] for x in po_vrstama)}


def csv_pregled(con, od=None, do=None):
    p = pregled(con, od, do)
    hr = lambda s: "%s.%s.%s." % (s[8:10], s[5:7], s[:4])
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    w.writerow(["Isplaćena povratna naknada", "od %s do %s" % (hr(p["od"]), hr(p["do"]))])
    w.writerow(["Datum", "Vrsta", "Naknada po komadu", "Komada", "Iznos"])
    for r in p["po_danima"]:
        w.writerow([hr(r["datum"]), r["naziv"], eur(r["iznos"]), r["komada"], eur(r["ukupno"])])
    w.writerow([])
    w.writerow(["Ukupno po vrsti", "Vrsta", "Naknada po komadu", "Komada", "Iznos"])
    for r in p["po_vrstama"]:
        w.writerow(["", r["naziv"], eur(r["iznos"]), r["komada"], eur(r["ukupno"])])
    w.writerow(["UKUPNO", "", "", p["komada"], eur(p["ukupno"])])
    return "﻿" + out.getvalue()


def potvrda_html(con, iid, naziv_trgovine):
    """Potvrda za kupca, termalni printer 80 mm (ili bilo koji). Nije račun."""
    d = isplata(con, iid)
    tr = "".join("<tr><td colspan=2>%s</td></tr><tr><td>%d kom x %s</td><td class=r>%s</td></tr>" % (
        _esc(s["naziv"]), s["komada"], eur(s["iznos"]), eur(s["ukupno"])) for s in d["stavke"])
    vrijeme = dt.datetime.strptime(d["vrijeme"], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y. %H:%M")
    ponisteno = "<div class=upoz>PONIŠTENO %s</div>" % _esc(d["ponisteno"][:16]) if d["ponisteno"] else ""
    return """<!doctype html><html lang=hr><head><meta charset=utf-8><title>%s</title>
<style>
@page { size: 80mm auto; margin: 3mm; }
body { font: 12px/1.35 'Courier New', monospace; width: 72mm; margin: 0 auto; color: #000; }
table { width: 100%%; border-collapse: collapse; } td { vertical-align: top; padding: 0; }
.r { text-align: right; white-space: nowrap; } .c { text-align: center; }
.tot td { font-size: 15px; font-weight: bold; border-top: 1px dashed #000; padding-top: 4px; }
.upoz { border: 2px solid #000; padding: 4px; text-align: center; font-weight: bold; margin: 6px 0; }
hr { border: 0; border-top: 1px dashed #000; }
@media screen { body { margin: 20px auto; } .gumbi { text-align: center; margin: 12px 0; } }
@media print { .gumbi { display: none; } }
</style></head><body>
<p class=c><b>%s</b></p>
<p class=c><b>POTVRDA O ISPLATI<br>POVRATNE NAKNADE</b></p>
%s
<p>Broj: %s<br>Vrijeme: %s%s</p>
<hr><table>%s</table>
<table><tr class=tot><td>ISPLAĆENO EUR</td><td class=r>%s</td></tr></table>
<div class=upoz>%s</div>
<div class=gumbi><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button></div>
<script>if (location.hash === '#ispis') { addEventListener('load', () => print()); }</script>
</body></html>""" % (_esc(d["oznaka"]), _esc(naziv_trgovine), ponisteno, _esc(d["oznaka"]), vrijeme,
                     ("<br>Isplatio: " + _esc(d["radnik"])) if d["radnik"] else "", tr, eur(d["ukupno"]),
                     _esc(NAPOMENA))
