# -*- coding: utf-8 -*-
"""
Narudžba dobavljaču e-mailom (modul "narudzba_mail").

Iz Nabave, Prijedlog narudžbe: po dobavljaču gumb "Pošalji narudžbu e-mailom". Količine,
napomenu i adresu (iz kartice dobavljača) se može promijeniti, a poruka ide kao običan tekst
i HTML (ista narudžba koja se može ispisati na A4: /ispis_narudzba_dob/<id>).

Šalje se preko SMTP postavki večernjeg sažetka (sazetak.py, Dodaci, Večernji sažetak na e-mail).
Svaka poslana (i neuspjela) narudžba ostaje zapisana: vrijeme, dobavljač, kome, stavke, greška.
"""

import datetime as dt
import json
import os
import re
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import postavke
from nabava import Greska

SHEMA = """
CREATE TABLE IF NOT EXISTS narudzbe_dobavljacima (
    id          INTEGER PRIMARY KEY,
    vrijeme     TEXT NOT NULL,
    partner_id  INTEGER,
    partner     TEXT NOT NULL DEFAULT '',
    za          TEXT NOT NULL DEFAULT '',
    naslov      TEXT NOT NULL DEFAULT '',
    stavke      TEXT NOT NULL DEFAULT '[]',   -- JSON [{artikl_id, naziv, jm, kolicina, sifra_dob}]
    napomena    TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'poslano',   -- poslano / greska
    greska      TEXT NOT NULL DEFAULT ''
);
"""


def pripremi(con):
    con.executescript(SHEMA)


def _kol(k):
    return ("%.3f" % k).rstrip("0").rstrip(".").replace(".", ",")


def _esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _datum_hr(d):
    return "%d.%d.%d." % (d.day, d.month, d.year)


def _trgovina(con):
    """Naziv i adresa trgovine: iz postavki uplate (ako su upisane), inače naziv programa."""
    naziv = postavke.vrijednost(con, "uplata.naziv") or os.environ.get("TRGOVINA_NAZIV", "Trgovina")
    adresa = ", ".join(x for x in (postavke.vrijednost(con, "uplata.adresa"),
                                   postavke.vrijednost(con, "uplata.mjesto")) if x)
    return naziv, adresa


def _partner(con, pid):
    r = con.execute("SELECT * FROM partneri WHERE id = ?", (int(pid or 0),)).fetchone()
    if not r:
        raise Greska("Dobavljač ne postoji.")
    return dict(r)


def _sifre_dobavljaca(con, pid):
    """{artikl_id: šifra dobavljača} iz veza eRačuna (ako modul eRačun postoji i ima veze)."""
    try:
        return {r[0]: r[1][2:] for r in con.execute(
            "SELECT artikl_id, kljuc FROM eracun_veze WHERE partner_id = ? AND kljuc LIKE 's:%'", (pid,))}
    except Exception:
        return {}


def _adrese(za):
    adr = [x.strip() for x in str(za or "").replace(";", ",").split(",") if x.strip()]
    if not adr:
        raise Greska("Upišite e-mail adresu dobavljača.")
    for a in adr:
        if not re.fullmatch(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+", a) or len(a) > 120:
            raise Greska("Neispravna e-mail adresa: %s" % a)
    return ", ".join(adr)


def _stavke(con, pid, stavke):
    sifre = _sifre_dobavljaca(con, pid)
    out = []
    for s in stavke or []:
        try:
            kol = round(float(str(s.get("kolicina")).replace(",", ".")), 3)
        except (TypeError, ValueError):
            raise Greska("Neispravna količina.")
        if kol == 0:
            continue  # nula = ne naručuje se
        if not 0 < kol <= 100000:
            raise Greska("Neispravna količina.")
        a = con.execute("SELECT id, naziv, jm, barkod FROM artikli WHERE id = ?",
                        (int(s.get("artikl_id") or 0),)).fetchone()
        if not a:
            raise Greska("Artikl ne postoji.")
        out.append({"artikl_id": a["id"], "naziv": a["naziv"], "jm": a["jm"], "kolicina": kol,
                    "barkod": a["barkod"] or "", "sifra_dob": sifre.get(a["id"], "")})
    if not out:
        raise Greska("Narudžba nema stavki (sve količine su 0).")
    return out


def priprema(con, pid):
    """Za ekran: dobavljač, adresa i je li SMTP upisan."""
    import sazetak
    p = _partner(con, pid)
    return {"partner_id": p["id"], "partner": p["naziv"], "email": p.get("email") or "",
            "smtp": sazetak.smtp_upisan(con), "sazetak_ukljucen": postavke.ukljucen(con, "sazetak"),
            "od": postavke.vrijednost(con, "sazetak.od") or postavke.vrijednost(con, "sazetak.korisnik"),
            "zadnje": popis(con, 10, pid)}


def sadrzaj(con, p, vrijeme=None):
    """Naslov, tekst i HTML narudžbe iz {partner_id, stavke, napomena}."""
    partner = _partner(con, p.get("partner_id"))
    stavke = p["stavke"] if p.get("_gotove") else _stavke(con, partner["id"], p.get("stavke"))
    napomena = str(p.get("napomena") or "").strip()[:1000]
    naziv, adresa = _trgovina(con)
    vrijeme = vrijeme or dt.datetime.now()
    datum = _datum_hr(vrijeme)
    naslov = "Narudžba: %s, %s" % (naziv, datum)
    ima_sifru = any(s["sifra_dob"] for s in stavke)

    red = ["Poštovani,", "", "molimo isporuku sljedeće robe:", ""]
    for i, s in enumerate(stavke, 1):
        red.append("%2d. %s%s: %s %s" % (i, s["naziv"], " (vaša šifra %s)" % s["sifra_dob"] if s["sifra_dob"] else "",
                                          _kol(s["kolicina"]), s["jm"]))
    if napomena:
        red += ["", "Napomena: " + napomena]
    red += ["", "Molimo potvrdu narudžbe i rok isporuke.", "", "Lijep pozdrav,", naziv]
    if adresa:
        red.append(adresa)
    tekst = "\n".join(red) + "\n"

    redovi = "".join(
        "<tr><td>%d</td><td>%s</td>%s<td class=r><b>%s</b></td><td>%s</td></tr>"
        % (i, _esc(s["naziv"]), "<td>%s</td>" % _esc(s["sifra_dob"]) if ima_sifru else "",
           _kol(s["kolicina"]), _esc(s["jm"])) for i, s in enumerate(stavke, 1))
    tijelo = """<p class=m>%s%s</p>
<h1>NARUDŽBA</h1>
<p>Dobavljač: <b>%s</b>%s<br>Datum: %s</p>
<table><tr><th>#</th><th>Artikl</th>%s<th class=r>Količina</th><th>JM</th></tr>%s</table>
%s<p>Molimo potvrdu narudžbe i rok isporuke.</p>
<p>Lijep pozdrav,<br>%s</p>""" % (
        _esc(naziv), "<br>" + _esc(adresa) if adresa else "", _esc(partner["naziv"]),
        "<br>OIB " + _esc(partner["oib"]) if partner.get("oib") else "", datum,
        "<th>Vaša šifra</th>" if ima_sifru else "", redovi,
        "<p>Napomena: %s</p>" % _esc(napomena).replace("\n", "<br>") if napomena else "", _esc(naziv))
    stil = """body { font: 13px/1.4 Arial, sans-serif; color: #000; max-width: 190mm; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; } .m { color: #444; }
table { width: 100%; border-collapse: collapse; margin: 14px 0; }
th, td { padding: 5px 6px; border-bottom: 1px solid #bbb; text-align: left; }
th { border-bottom: 2px solid #000; font-size: 12px; } .r { text-align: right; white-space: nowrap; }"""
    html_mail = "<!doctype html><html lang=hr><head><meta charset=utf-8><style>%s</style></head><body>%s</body></html>" % (
        stil, tijelo)
    html_ispis = """<!doctype html><html lang=hr><head><meta charset=utf-8><title>%s</title>
<style>@page { size: A4; margin: 15mm; }
%s
@media screen { body { margin: 24px auto; } } @media print { .gumbi { display: none; } }</style></head><body>
%s
<p class=gumbi><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button></p>
<script>if (location.hash === '#ispis') { addEventListener('load', () => print()); }</script>
</body></html>""" % (_esc(naslov), stil, tijelo)
    return {"naslov": naslov, "tekst": tekst, "html": html_mail, "ispis": html_ispis,
            "stavke": stavke, "napomena": napomena, "partner": partner}


def pregled(con, p):
    d = sadrzaj(con, p)
    return {"naslov": d["naslov"], "tekst": d["tekst"], "ispis": d["ispis"]}


def posalji(con, p):
    import sazetak
    if not sazetak.smtp_upisan(con):
        raise Greska("E-mail nije postavljen. SMTP poslužitelj, korisnika i lozinku upišite u Dodaci, "
                     "Večernji sažetak na e-mail (uključite taj modul u Postavkama; ako sažetak ne želite, "
                     "odznačite 'Pošalji kad se zaključi smjena' i ostavite vrijeme prazno).")
    za = _adrese(p.get("za"))
    d = sadrzaj(con, p)
    msg = EmailMessage()
    msg["Subject"] = d["naslov"]
    od = postavke.vrijednost(con, "sazetak.od") or postavke.vrijednost(con, "sazetak.korisnik")
    if od:
        msg["From"] = od
        msg["Reply-To"] = od
    msg["To"] = za
    if p.get("kopija_meni") and od:
        msg["Bcc"] = od
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(d["tekst"])
    msg.add_alternative(d["html"], subtype="html")
    vrijeme = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st = json.dumps(d["stavke"], ensure_ascii=False)
    try:
        sazetak.posalji_poruku(con, msg)
    except Greska as e:
        con.execute("""INSERT INTO narudzbe_dobavljacima (vrijeme, partner_id, partner, za, naslov, stavke,
                       napomena, status, greska) VALUES (?, ?, ?, ?, ?, ?, ?, 'greska', ?)""",
                    (vrijeme, d["partner"]["id"], d["partner"]["naziv"], za, d["naslov"], st, d["napomena"],
                     str(e)[:500]))
        con.commit()
        raise
    nid = con.execute("""INSERT INTO narudzbe_dobavljacima (vrijeme, partner_id, partner, za, naslov, stavke,
                         napomena, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'poslano')""",
                      (vrijeme, d["partner"]["id"], d["partner"]["naziv"], za, d["naslov"], st,
                       d["napomena"])).lastrowid
    # adresa upisana prvi put ostaje na kartici dobavljača
    if not d["partner"].get("email"):
        con.execute("UPDATE partneri SET email = ? WHERE id = ?", (za, d["partner"]["id"]))
    con.commit()
    return {"id": nid, "naslov": d["naslov"], "za": za}


def popis(con, n=30, pid=None):
    sql = "SELECT * FROM narudzbe_dobavljacima"
    arg = []
    if pid:
        sql += " WHERE partner_id = ?"
        arg.append(int(pid))
    out = []
    for r in con.execute(sql + " ORDER BY id DESC LIMIT ?", arg + [int(n)]):
        d = dict(r)
        d["stavke"] = json.loads(d["stavke"] or "[]")
        out.append(d)
    return out


def ispis(con, nid):
    r = con.execute("SELECT * FROM narudzbe_dobavljacima WHERE id = ?", (nid,)).fetchone()
    if not r:
        raise Greska("Narudžba ne postoji.")
    st = json.loads(r["stavke"] or "[]")
    for s in st:
        s.setdefault("sifra_dob", "")
    p = {"partner_id": r["partner_id"], "stavke": st, "napomena": r["napomena"], "_gotove": True}
    return sadrzaj(con, p, dt.datetime.strptime(r["vrijeme"][:19], "%Y-%m-%d %H:%M:%S"))["ispis"]
