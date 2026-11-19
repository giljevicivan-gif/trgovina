# -*- coding: utf-8 -*-
"""
2D barkod za plaćanje (modul "qr_uplata") na otpremnici i zbirnom pregledu za kupce tvrtke.

Hrvatske bankovne aplikacije (mobilno bankarstvo svih banaka u HUB-u) skeniraju barkod s
uplatnice HUB-3: PDF417 s podacima "HRVHUB30" (specifikacija HUB, 2DBK EUR). EPC QR kod
(SEPA "BCD") hrvatske banke ne navode kao standard, zato se crta HUB-3 PDF417 (pdf417.py).

Polja (redom, odvojena znakom LF): zaglavlje HRVHUB30, valuta EUR, iznos u centima na 15
znamenki, platitelj (naziv, ulica, mjesto), primatelj (naziv, ulica, mjesto), IBAN primatelja,
model (HR00, HR01, HR99), poziv na broj, šifra namjene (ISO 20022, npr. GDSV), opis plaćanja.

Ovo nije račun: barkod samo olakšava plaćanje. Potvrdite s knjigovođom treba li kupac plaćati
po otpremnici / zbirnom pregledu ili tek po računu iz Robe (tada poziv na broj = broj računa).
"""

import re

import pdf417
import postavke
from nabava import Greska

MODELI = ("HR00", "HR01", "HR99")
NAMJENE = (("GDSV", "kupnja/prodaja robe i usluga"), ("GDDS", "kupnja/prodaja robe"),
           ("OTHR", "ostalo"), ("SUPP", "plaćanje dobavljaču"))
KLJUCEVI = ("iban", "naziv", "adresa", "mjesto", "model", "namjena")
# dopušteni znakovi HUB-3 (uz slova hrvatske abecede i Q W X Y)
_DOPUSTENO = re.compile(r"[^0-9A-Za-zČčĆćĐđŠšŽž ,.:\-+?'/()]")


def _v(con, k, zad=""):
    return postavke.vrijednost(con, "uplata." + k, zad)


def iban_ispravan(iban):
    s = re.sub(r"\s+", "", str(iban or "")).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return False
    if s.startswith("HR") and len(s) != 21:
        return False
    broj = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(broj) % 97 == 1


def postavke_modula(con):
    p = {k: _v(con, k) for k in KLJUCEVI}
    p["model"] = p["model"] if p["model"] in MODELI else "HR00"
    p["namjena"] = p["namjena"] or "GDSV"
    p["modeli"] = list(MODELI)
    p["namjene"] = [{"sifra": s, "opis": o} for s, o in NAMJENE]
    p["spremno"] = bool(p["iban"])
    return p


def spremi_postavke(con, p):
    iban = re.sub(r"\s+", "", str(p.get("iban") or "")).upper()
    if iban and not iban_ispravan(iban):
        raise Greska("IBAN nije ispravan (provjerite znamenke; hrvatski IBAN ima 21 znak, HR + 19 brojki).")
    model = str(p.get("model") or "HR00").upper()
    if model not in MODELI:
        raise Greska("Model: HR00, HR01 ili HR99.")
    namjena = str(p.get("namjena") or "GDSV").strip().upper()
    if not re.fullmatch(r"[A-Z]{4}", namjena):
        raise Greska("Šifra namjene ima 4 slova (npr. GDSV ili OTHR).")
    naziv = str(p.get("naziv") or "").strip()
    if iban and not naziv:
        raise Greska("Upišite naziv primatelja (vaša tvrtka ili obrt).")
    vals = {"iban": iban, "naziv": naziv[:25], "adresa": str(p.get("adresa") or "").strip()[:25],
            "mjesto": str(p.get("mjesto") or "").strip()[:27], "model": model, "namjena": namjena}
    for k, v in vals.items():
        postavke.postavi(con, "uplata." + k, v)
    return postavke_modula(con)


def _polje(s, n):
    s = _DOPUSTENO.sub(" ", str(s or "").replace("\n", " "))
    return re.sub(r"\s+", " ", s).strip()[:n].strip()


def mod11ini(znamenke):
    z = sum(int(c) * (i + 2) for i, c in enumerate(reversed(znamenke)))
    o = z % 11
    return 0 if o in (0, 1) else 11 - o


def poziv(model, dijelovi):
    """Poziv na broj po modelu: dijelovi su brojke (npr. godina i broj dokumenta)."""
    if model == "HR99":
        return ""
    d = [re.sub(r"\D", "", str(x))[:12] for x in dijelovi if re.sub(r"\D", "", str(x))][:3]
    if not d:
        return ""
    while sum(len(x) for x in d) > 20:  # najviše 20 znamenki u tri dijela
        d.pop(0)
    if model == "HR01":
        d[-1] += str(mod11ini("".join(d)))
        if len(d[-1]) > 12:
            d[-1] = d[-1][-12:]
    return "-".join(d)


def podaci_hub3(p, iznos, platitelj, platitelj_adresa, platitelj_mjesto, poziv_na_broj, opis):
    """Tekst za barkod (UTF-8, svako polje završava s LF)."""
    if not 0 < int(iznos) <= 999999999999999:
        raise Greska("Iznos za uplatu mora biti veći od 0.")
    polja = ["HRVHUB30", "EUR", "%015d" % int(iznos),
             _polje(platitelj, 30), _polje(platitelj_adresa, 27), _polje(platitelj_mjesto, 27),
             _polje(p["naziv"], 25), _polje(p["adresa"], 25), _polje(p["mjesto"], 27),
             p["iban"][:21], p["model"], "" if p["model"] == "HR99" else _polje(poziv_na_broj, 22),
             p["namjena"][:4], _polje(opis, 35)]
    return "\n".join(polja) + "\n"


def _eur(c):
    s = "%.2f" % (c / 100.0)
    cijeli, dec = s.split(".")
    return "{:,}".format(int(cijeli)).replace(",", ".") + "," + dec


def _esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def kutija(con, iznos, platitelj, adresa, mjesto, dijelovi_poziva, opis):
    """HTML okvir s barkodom za kut ispisa, ili "" kad modul nije uključen / nema IBAN-a / iznosa."""
    if not postavke.ukljucen(con, "qr_uplata"):
        return ""
    p = postavke_modula(con)
    if not p["iban"] or not iznos or iznos <= 0:
        return ""
    pnb = poziv(p["model"], dijelovi_poziva)
    tekst = podaci_hub3(p, iznos, platitelj, adresa, mjesto, pnb, opis)
    slika = pdf417.svg(tekst, stupaca=9, razina=4)
    return ("<style>.uplata2d{width:62mm;margin:8mm 0 0 auto;border:1px solid #000;padding:2mm;"
            "text-align:center;font:10px/1.3 Arial,sans-serif;page-break-inside:avoid;break-inside:avoid}"
            ".uplata2d b{font-size:11px}.uplata2d svg{display:block;margin:1mm auto}</style>"
            "<div class=uplata2d><b>Plati mobilnim bankarstvom</b>%s<div>%s EUR · IBAN %s<br>%s %s</div></div>"
            % (slika, _eur(iznos), _esc(p["iban"]), _esc(p["model"]), _esc(pnb)))


def umetni(html, okvir):
    """Okvir ide u donji desni kut, iza potpisa (prije gumba Ispiši): ostatak ispisa se ne mijenja."""
    if not okvir or "<p class=gumbi>" not in html:
        return html
    return html.replace("<p class=gumbi>", okvir + "\n<p class=gumbi>", 1)


def za_otpremnicu(con, d, html):
    try:
        g, b = d["godina"], d["broj"]
        okvir = kutija(con, d.get("ukupno") or 0, d.get("kupac"), d.get("kupac_adresa"), d.get("kupac_mjesto"),
                       (g, b), "Otpremnica " + d["oznaka"])
    except Greska:
        okvir = ""
    return umetni(html, okvir)


def za_zbirni(con, z, html):
    k = z["kupac"]
    mj = z["mjesec"]
    try:
        okvir = kutija(con, z.get("ukupno") or 0, k.get("naziv"), k.get("adresa"), k.get("mjesto"),
                       (k["id"], mj.replace("-", "")),
                       "Isporuke %s.%s. %s" % (mj[5:], mj[:4], k.get("naziv") or ""))
    except Greska:
        okvir = ""
    return umetni(html, okvir)
