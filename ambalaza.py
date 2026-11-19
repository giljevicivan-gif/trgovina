# -*- coding: utf-8 -*-
"""
Gajbe i povratna ambalaža (modul "ambalaza").

Dobavljač donese robu u svojim gajbama ili na paletama i očekuje ih natrag;
što ne vratite, naplati. Ovdje se vodi za svakog dobavljača i svaku vrstu
ambalaže koliko je primljeno i koliko vraćeno, pa se vidi koliko još dugujete.
Kod povrata se ispiše potvrda koju vozač potpiše.

Vrijednost (polog) po komadu nije obvezna; služi samo da se vidi koliko
vrijedi ambalaža koju još imate. Ovo nije knjigovodstvo: ako dobavljač
ambalažu fakturira, to ide na njegov račun (primku) kao i do sada.
"""

import datetime as dt

from baza import centi
from nabava import Greska

POCETNE = (("Gajba plastična", 0), ("Euro paleta", 0), ("Drvena gajbica", 0))

SHEMA = """
CREATE TABLE IF NOT EXISTS ambalaza_vrste (
    id          INTEGER PRIMARY KEY,
    naziv       TEXT NOT NULL UNIQUE,
    vrijednost  INTEGER NOT NULL DEFAULT 0,   -- polog po komadu, centi (0 = ne zna se)
    aktivan     INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS ambalaza_promet (
    id          INTEGER PRIMARY KEY,
    datum       TEXT NOT NULL,
    vrijeme     TEXT NOT NULL,
    partner_id  INTEGER NOT NULL REFERENCES partneri(id),
    vrsta_id    INTEGER NOT NULL REFERENCES ambalaza_vrste(id),
    primljeno   INTEGER NOT NULL DEFAULT 0,
    vraceno     INTEGER NOT NULL DEFAULT 0,
    dokument_id INTEGER REFERENCES dokumenti(id),   -- primka uz koju je došla (nije obvezno)
    napomena    TEXT NOT NULL DEFAULT '',
    radnik      TEXT NOT NULL DEFAULT '',
    ponisten    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_ambalaza_partner ON ambalaza_promet(partner_id, vrsta_id);
"""


def sada():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def pripremi(con):
    con.executescript(SHEMA)
    if not con.execute("SELECT 1 FROM ambalaza_vrste").fetchone():
        con.executemany("INSERT INTO ambalaza_vrste (naziv, vrijednost) VALUES (?, ?)", POCETNE)
        con.commit()


def vrste(con, sve=False):
    return [dict(r) for r in con.execute(
        "SELECT * FROM ambalaza_vrste" + ("" if sve else " WHERE aktivan = 1") + " ORDER BY naziv")]


def spremi_vrstu(con, p):
    naziv = " ".join(str(p.get("naziv") or "").split())[:60]
    if not naziv:
        raise Greska("Upišite naziv ambalaže (npr. Gajba plava).")
    try:
        vrijednost = centi(float(str(p.get("vrijednost") or "0").replace(",", ".")))
    except (ValueError, TypeError):
        raise Greska("Neispravna vrijednost.")
    if vrijednost < 0:
        raise Greska("Vrijednost ne može biti negativna.")
    aktivan = 0 if p.get("aktivan") in (False, 0, "0") else 1
    vid = int(p.get("id") or 0)
    try:
        with con:
            if vid:
                con.execute("UPDATE ambalaza_vrste SET naziv = ?, vrijednost = ?, aktivan = ? WHERE id = ?",
                            (naziv, vrijednost, aktivan, vid))
            else:
                vid = con.execute("INSERT INTO ambalaza_vrste (naziv, vrijednost) VALUES (?, ?)",
                                  (naziv, vrijednost)).lastrowid
    except Exception as e:
        if "UNIQUE" in str(e):
            raise Greska("Ambalaža s tim nazivom već postoji.")
        raise
    return vid


def _cijeli(v, ime):
    try:
        n = int(str(v or "0").strip() or 0)
    except ValueError:
        raise Greska("%s: upišite cijeli broj komada." % ime)
    if not 0 <= n <= 100000:
        raise Greska("%s: neispravan broj komada." % ime)
    return n


def upisi(con, p, radnik=""):
    """Jedan upis: za dobavljača i datum, više vrsta ambalaže (primljeno/vraćeno)."""
    try:
        partner_id = int(p.get("partner_id"))
    except (TypeError, ValueError):
        raise Greska("Odaberite dobavljača.")
    if not con.execute("SELECT 1 FROM partneri WHERE id = ?", (partner_id,)).fetchone():
        raise Greska("Nepoznat dobavljač.")
    datum = str(p.get("datum") or dt.date.today().isoformat())[:10]
    try:
        dt.date.fromisoformat(datum)
    except ValueError:
        raise Greska("Neispravan datum.")
    if datum > dt.date.today().isoformat():
        raise Greska("Datum ne može biti u budućnosti.")
    dok = p.get("dokument_id") or None
    if dok:
        dok = int(dok)
        if not con.execute("SELECT 1 FROM dokumenti WHERE id = ? AND vrsta = 'primka'", (dok,)).fetchone():
            raise Greska("Primka ne postoji.")
    redovi = []
    for s in p.get("stavke") or []:
        vid = int(s.get("vrsta_id") or 0)
        if not con.execute("SELECT 1 FROM ambalaza_vrste WHERE id = ?", (vid,)).fetchone():
            raise Greska("Nepoznata vrsta ambalaže.")
        pr, vr = _cijeli(s.get("primljeno"), "Primljeno"), _cijeli(s.get("vraceno"), "Vraćeno")
        if pr or vr:
            redovi.append((vid, pr, vr))
    if not redovi:
        raise Greska("Upišite koliko je komada primljeno ili vraćeno.")
    napomena = str(p.get("napomena") or "").strip()[:200]
    t = sada()
    ids = []
    with con:
        for vid, pr, vr in redovi:
            ids.append(con.execute(
                """INSERT INTO ambalaza_promet (datum, vrijeme, partner_id, vrsta_id, primljeno, vraceno,
                                                dokument_id, napomena, radnik) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (datum, t, partner_id, vid, pr, vr, dok, napomena, radnik or "")).lastrowid)
    return {"id": ids[0], "ids": ids}


def ponisti(con, p):
    pid = int(p.get("id") or 0)
    razlog = str(p.get("razlog") or "").strip()
    if not razlog:
        raise Greska("Upišite razlog poništavanja.")
    with con:
        if not con.execute("UPDATE ambalaza_promet SET ponisten = ? WHERE id = ? AND ponisten = ''",
                           ("%s %s" % (sada(), razlog[:100]), pid)).rowcount:
            raise Greska("Upis ne postoji ili je već poništen.")
    return {"id": pid}


def stanje(con):
    """Saldo po dobavljaču i vrsti: koliko ambalaže još imamo (dugujemo)."""
    out = {}
    for r in con.execute(
            """SELECT p.id AS partner_id, p.naziv AS partner, v.id AS vrsta_id, v.naziv AS vrsta, v.vrijednost,
                      SUM(m.primljeno) AS primljeno, SUM(m.vraceno) AS vraceno, MAX(m.datum) AS zadnji
               FROM ambalaza_promet m JOIN partneri p ON p.id = m.partner_id
               JOIN ambalaza_vrste v ON v.id = m.vrsta_id
               WHERE m.ponisten = '' GROUP BY p.id, v.id ORDER BY p.naziv, v.naziv"""):
        d = out.setdefault(r["partner_id"], {"partner_id": r["partner_id"], "partner": r["partner"],
                                             "vrste": [], "vrijednost": 0, "komada": 0})
        saldo = r["primljeno"] - r["vraceno"]
        d["vrste"].append({"vrsta_id": r["vrsta_id"], "vrsta": r["vrsta"], "primljeno": r["primljeno"],
                           "vraceno": r["vraceno"], "saldo": saldo, "zadnji": r["zadnji"],
                           "vrijednost": saldo * r["vrijednost"]})
        d["vrijednost"] += saldo * r["vrijednost"]
        d["komada"] += saldo
    return list(out.values())


def promet(con, partner_id=None, dana=90):
    od = (dt.date.today() - dt.timedelta(days=int(dana))).isoformat()
    sql = """SELECT m.*, p.naziv AS partner, v.naziv AS vrsta FROM ambalaza_promet m
             JOIN partneri p ON p.id = m.partner_id JOIN ambalaza_vrste v ON v.id = m.vrsta_id
             WHERE m.datum >= ?"""
    args = [od]
    if partner_id:
        sql += " AND m.partner_id = ?"
        args.append(int(partner_id))
    return [dict(r) for r in con.execute(sql + " ORDER BY m.datum DESC, m.id DESC LIMIT 500", args)]


def pregled(con, q):
    return {"stanje": stanje(con), "promet": promet(con, q.get("partner"), q.get("dana") or 90),
            "vrste": vrste(con, sve=True)}


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def potvrda_html(con, ids, naziv_trgovine):
    """Potvrda o primitku/povratu ambalaže za potpis vozača (svi upisi jednog unosa)."""
    ids = [int(i) for i in ids if str(i).isdigit()][:50]
    if not ids:
        raise Greska("Nema upisa.")
    redovi = [dict(r) for r in con.execute(
        """SELECT m.*, p.naziv AS partner, p.oib, v.naziv AS vrsta FROM ambalaza_promet m
           JOIN partneri p ON p.id = m.partner_id JOIN ambalaza_vrste v ON v.id = m.vrsta_id
           WHERE m.id IN (%s) ORDER BY m.id""" % ",".join("?" * len(ids)), ids)]
    if not redovi:
        raise Greska("Nema upisa.")
    r0 = redovi[0]
    saldo = {v["vrsta_id"]: v["saldo"] for d in stanje(con) if d["partner_id"] == r0["partner_id"] for v in d["vrste"]}
    d = dt.date.fromisoformat(r0["datum"])
    tr = "".join("<tr><td>%s</td><td class=r>%s</td><td class=r>%s</td><td class=r>%s</td></tr>" % (
        _esc(r["vrsta"]), r["primljeno"] or "", r["vraceno"] or "", saldo.get(r["vrsta_id"], 0)) for r in redovi)
    return """<!doctype html><html lang=hr><head><meta charset=utf-8><title>Potvrda ambalaža</title>
<style>
@page { size: A4; margin: 15mm; }
body { font: 13px/1.5 Arial, sans-serif; color: #000; max-width: 180mm; margin: 0 auto; }
h1 { font-size: 18px; } table { width: 100%%; border-collapse: collapse; margin-top: 10px; }
th, td { border-bottom: 1px solid #999; padding: 5px; text-align: left; } .r { text-align: right; }
.pot { display: flex; justify-content: space-between; margin-top: 50px; }
.pot div { width: 40%%; border-top: 1px solid #000; padding-top: 4px; text-align: center; }
@media print { .gumbi { display: none; } }
</style></head><body>
<p>%s</p>
<h1>Potvrda o povratnoj ambalaži</h1>
<p>Dobavljač: <b>%s</b>%s<br>Datum: %d.%d.%d.%s</p>
<table><tr><th>Ambalaža</th><th class=r>Primljeno kom</th><th class=r>Vraćeno kom</th><th class=r>Ostaje kod nas</th></tr>%s</table>
<div class=pot><div>Predao</div><div>Preuzeo (vozač)</div></div>
<p class=gumbi><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button></p>
<script>if (location.hash === '#ispis') { addEventListener('load', () => print()); }</script>
</body></html>""" % (_esc(naziv_trgovine), _esc(r0["partner"]), (" (OIB %s)" % _esc(r0["oib"])) if r0["oib"] else "",
                     d.day, d.month, d.year, ("<br>Napomena: " + _esc(r0["napomena"])) if r0["napomena"] else "", tr)
