# -*- coding: utf-8 -*-
"""
Večernji sažetak dana na e-mail (modul "sazetak").

Kad se u Blagajni zaključi smjena (i/ili svaki dan u zadano vrijeme), program pošalje
kratak e-mail: promet iz Robe, blagajna (manjak/višak), što treba naručiti, sniženja,
gajbe i što je crveno u Kontroli.

Šalje se preko vašeg e-mail računa (SMTP). Za Gmail treba "lozinka za aplikacije"
(Google račun, Sigurnost, Potvrda u 2 koraka, Lozinke za aplikacije), ne obična lozinka.
Lozinka se sprema samo u trgovina.db na ovom računalu (i u njezine sigurnosne kopije),
ekran je nikad ne prikazuje, a program je šalje samo vašem SMTP poslužitelju.
"""

import datetime as dt
import os
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import postavke
from baza import spoji
from nabava import Greska

KLJUCEVI = ("posluzitelj", "port", "korisnik", "od", "za", "kod_zakljucka", "vrijeme", "sigurnost")
DANI = ["ponedjeljak", "utorak", "srijeda", "četvrtak", "petak", "subota", "nedjelja"]
_lock = threading.Lock()


def _v(con, k, zad=""):
    return postavke.vrijednost(con, "sazetak." + k, zad)


def postavke_modula(con):
    p = {k: _v(con, k) for k in KLJUCEVI}
    p["port"] = p["port"] or "587"
    p["sigurnost"] = p["sigurnost"] or "starttls"
    p["kod_zakljucka"] = p["kod_zakljucka"] != "0"
    p["lozinka_spremljena"] = bool(_v(con, "lozinka"))
    p["zadnje"] = _v(con, "zadnje")
    p["zadnja_greska"] = _v(con, "zadnja_greska")
    return p


def spremi_postavke(con, p):
    port = str(p.get("port") or "587").strip()
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise Greska("Neispravan port (obično 587 ili 465).")
    sig = p.get("sigurnost") or "starttls"
    if sig not in ("starttls", "ssl"):
        raise Greska("Sigurnost: starttls ili ssl.")
    za = ",".join(x.strip() for x in str(p.get("za") or "").replace(";", ",").split(",") if x.strip())
    for adr in [x for x in za.split(",") if x] + ([str(p.get("od")).strip()] if p.get("od") else []):
        if "@" not in adr or " " in adr or len(adr) > 120:
            raise Greska("Neispravna e-mail adresa: %s" % adr)
    vrijeme = str(p.get("vrijeme") or "").strip()
    if vrijeme:
        try:
            vrijeme = dt.datetime.strptime(vrijeme, "%H:%M").strftime("%H:%M")
        except ValueError:
            raise Greska("Vrijeme slanja upišite kao 21:30 (ili ostavite prazno).")
    vals = {"posluzitelj": str(p.get("posluzitelj") or "").strip()[:100], "port": port,
            "korisnik": str(p.get("korisnik") or "").strip()[:120], "od": str(p.get("od") or "").strip()[:120],
            "za": za, "kod_zakljucka": "1" if p.get("kod_zakljucka") in (True, 1, "1", "true") else "0",
            "vrijeme": vrijeme, "sigurnost": sig}
    for k, v in vals.items():
        postavke.postavi(con, "sazetak." + k, v)
    if p.get("lozinka"):
        postavke.postavi(con, "sazetak.lozinka", str(p["lozinka"]))
    if p.get("obrisi_lozinku"):
        postavke.postavi(con, "sazetak.lozinka", "")
    return postavke_modula(con)


# ---------------------------------------------------------------- sadržaj

def _eur(c):
    return ("%.2f" % ((c or 0) / 100.0)).replace(".", ",")


def _kol(k):
    return ("%g" % round(k, 3)).replace(".", ",")


def sadrzaj(con, datum=None, smjena_id=None):
    """(naslov, tekst) sažetka za dan."""
    import blagajna
    import danas as jutro
    import nabava
    datum = datum or dt.date.today().isoformat()
    d = dt.date.fromisoformat(datum)
    m = postavke.moduli(con)
    naziv = os.environ.get("TRGOVINA_NAZIV", "Trgovina")
    dijelovi = []

    p = jutro._promet(con, datum)
    zadnji = con.execute("SELECT MAX(vrijeme) FROM roba_dokumenti WHERE status IN (2, 3)").fetchone()[0] or ""
    tj = jutro._promet(con, (d - dt.timedelta(days=7)).isoformat())
    red = ["PROMET (Roba) %s %s" % (DANI[d.weekday()], "%d.%d.%d." % (d.day, d.month, d.year)),
           "  Promet: %s EUR, računa: %s" % (_eur(p.get("iznos")), p.get("racuna") or 0)]
    if tj.get("iznos"):
        raz = round((p.get("iznos", 0) - tj["iznos"]) * 100.0 / tj["iznos"])
        red.append("  Isti dan prošli tjedan: %s EUR (%+d %%)" % (_eur(tj["iznos"]), raz))
    if zadnji[:10] < datum:
        red.append("  Pažnja: zadnji račun iz Robe je od %s; osvježite podatke iz Robe." % (zadnji or "nikad"))
    dijelovi.append("\n".join(red))

    # blagajna: zadnja smjena tog dana (ili zadana)
    if smjena_id is None:
        r = con.execute("SELECT id FROM smjene WHERE substr(COALESCE(zatvorena, otvorena), 1, 10) = ? "
                        "ORDER BY id DESC LIMIT 1", (datum,)).fetchone()
        smjena_id = r[0] if r else None
    if smjena_id:
        s = blagajna.izracun(con, smjena_id)
        red = ["BLAGAJNA (smjena od %s)" % s["otvorena"][11:16]]
        if s.get("zatvorena"):
            raz = s.get("razlika") or 0
            red += ["  Zaključena u %s" % s["zatvorena"][11:16],
                    "  Gotovina iz Robe: %s EUR, kartice i ostalo: %s EUR" % (_eur(s["gotovina"]), _eur(s["kartice"])),
                    "  Očekivano u ladici: %s EUR, prebrojano: %s EUR" % (_eur(s["ocekivano"]), _eur(s["prebrojano"])),
                    "  %s" % ("Blagajna se slaže." if raz == 0 else
                              ("MANJAK %s EUR" % _eur(-raz)) if raz < 0 else "VIŠAK %s EUR" % _eur(raz))]
            if s.get("napomena"):
                red.append("  Napomena: %s" % s["napomena"])
        else:
            red.append("  Smjena još nije zaključena (gotovina sada %s EUR)." % _eur(s["gotovina_sada"]))
        if s["isplate"]:
            red.append("  Isplate iz blagajne: %s EUR" % _eur(s["isplate"]))
        dijelovi.append("\n".join(red))
    else:
        dijelovi.append("BLAGAJNA\n  Danas nije bilo smjene u programu.")

    # za naručiti
    try:
        if m.get("prijedlog_dan"):
            import prijedlog_dan
            pr = prijedlog_dan.prijedlog(con, 28, 2, dt.datetime.combine(d, dt.time(20, 0)))
        else:
            pr = nabava.prijedlog(con, 28, 2)
        st = pr["stavke"]
        red = ["ZA NARUČITI (%d)" % len(st)]
        dob = None
        for x in st[:40]:
            if x["dobavljac"] != dob:
                dob = x["dobavljac"]
                red.append("  %s:" % (dob or "Bez dobavljača"))
            red.append("    %s: %s %s (ima %s)" % (x["naziv"], _kol(x["kolicina"]), x["jm"], _kol(max(0, x["zaliha"]))))
        if len(st) > 40:
            red.append("  ... i još %d (Nabava, Prijedlog narudžbe)" % (len(st) - 40))
        if not st:
            red.append("  Zaliha je dovoljna.")
        dijelovi.append("\n".join(red))
    except Exception as e:
        dijelovi.append("ZA NARUČITI\n  Nije izračunato: %s" % e)

    if m.get("snizenje"):
        import snizenje
        dana = [x for x in snizenje.popis(con, 1)["snizenja"] if x["od"][:10] == datum]
        if dana:
            red = ["SNIŽENJA DANAS"]
            for x in dana:
                red.append("  %s -%g %%: prodano %s %s za %s EUR%s" % (
                    x["naziv"], x["postotak"], _kol(x["prodano"]), x["jm"], _eur(x["prihod"]),
                    ", otpisano %s" % _kol(x["otpisano"]) if x["otpisano"] else ""))
            dijelovi.append("\n".join(red))

    if m.get("ambalaza"):
        import ambalaza
        st = [(d_["partner"], v) for d_ in ambalaza.stanje(con) for v in d_["vrste"] if v["saldo"] > 0]
        if st:
            dijelovi.append("GAJBE I AMBALAŽA KOD NAS\n" + "\n".join(
                "  %s: %s %d kom" % (p_, v["vrsta"], v["saldo"]) for p_, v in st))

    try:
        import kontrola
        crveno = [k for k in kontrola.provjere(con) if k["razina"] == "crveno"]
        if crveno:
            dijelovi.append("KONTROLA (crveno)\n" + "\n".join(
                "  %s%s" % (k["naslov"], " (%d)" % k["ukupno"] if k["ukupno"] else "") for k in crveno))
    except Exception:
        pass

    naslov = "%s: sažetak %d.%d.%d. – %s EUR" % (naziv, d.day, d.month, d.year, _eur(p.get("iznos")))
    tekst = "\n\n".join(dijelovi) + "\n\n--\nPoslao program za trgovinu. Ovo nije račun ni službeni izvještaj."
    return naslov, tekst


# ---------------------------------------------------------------- slanje

def posalji(con, datum=None, smjena_id=None, probni=False):
    p = postavke_modula(con)
    if not p["posluzitelj"] or not p["za"]:
        raise Greska("Upišite SMTP poslužitelj i kome se šalje (Dodaci, E-mail sažetak).")
    lozinka = _v(con, "lozinka")
    naslov, tekst = sadrzaj(con, datum, smjena_id)
    if probni:
        naslov = "PROBA: " + naslov
    msg = EmailMessage()
    msg["Subject"] = naslov
    msg["From"] = p["od"] or p["korisnik"]
    msg["To"] = p["za"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(tekst)
    posalji_poruku(con, msg)
    return naslov


def smtp_upisan(con):
    """Je li upisan SMTP poslužitelj (koriste ga i drugi moduli, npr. narudžba dobavljaču)."""
    return bool(_v(con, "posluzitelj"))


def posalji_poruku(con, msg):
    """Pošalji gotovu EmailMessage preko SMTP postavki sažetka."""
    p = postavke_modula(con)
    if not p["posluzitelj"]:
        raise Greska("SMTP poslužitelj nije upisan (Dodaci, Večernji sažetak na e-mail).")
    lozinka = _v(con, "lozinka")
    if not msg["From"]:
        msg["From"] = p["od"] or p["korisnik"]
    port = int(p["port"])
    try:
        if p["sigurnost"] == "ssl":
            s = smtplib.SMTP_SSL(p["posluzitelj"], port, timeout=30, context=ssl.create_default_context())
        else:
            s = smtplib.SMTP(p["posluzitelj"], port, timeout=30)
            s.ehlo()
            if s.has_extn("starttls"):
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
            elif p["korisnik"]:
                s.quit()
                raise Greska("Poslužitelj ne podržava STARTTLS; lozinka se ne šalje nezaštićeno. Probajte ssl i port 465.")
        try:
            if p["korisnik"]:
                s.login(p["korisnik"], lozinka)
            s.send_message(msg)
        finally:
            try:
                s.quit()
            except Exception:
                pass
    except Greska:
        raise
    except smtplib.SMTPAuthenticationError:
        raise Greska("Prijava na e-mail nije uspjela (korisnik ili lozinka; za Gmail treba lozinka za aplikacije).")
    except Exception as e:
        raise Greska("Slanje nije uspjelo: %s" % e)


def _zabiljezi(con, ok, poruka):
    t = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    if ok:
        postavke.postavi(con, "sazetak.zadnje", "%s %s" % (t, poruka))
        postavke.postavi(con, "sazetak.zadnja_greska", "")
    else:
        postavke.postavi(con, "sazetak.zadnja_greska", "%s %s" % (t, poruka))


def posalji_u_pozadini(smjena_id=None, datum=None, oznaci_dan=True):
    """Šalje u zasebnoj niti, da zaključak smjene ne čeka na e-mail."""
    def rad():
        with _lock:
            con = spoji()
            try:
                if not postavke.ukljucen(con, "sazetak"):
                    return
                naslov = posalji(con, datum, smjena_id)
                _zabiljezi(con, True, naslov)
                if oznaci_dan:
                    postavke.postavi(con, "sazetak.poslano_dana", datum or dt.date.today().isoformat())
            except Exception as e:
                _zabiljezi(con, False, str(e))
            finally:
                con.close()
    t = threading.Thread(target=rad, daemon=True)
    t.start()
    return t


def nakon_zakljucka(smjena_id):
    con = spoji()
    try:
        if not postavke.ukljucen(con, "sazetak") or _v(con, "kod_zakljucka") == "0":
            return None
    finally:
        con.close()
    return posalji_u_pozadini(smjena_id=smjena_id)


def _sat():
    """Pozadinska provjera: svaki dan u zadano vrijeme, ako sažetak tog dana još nije poslan."""
    while True:
        time.sleep(60)
        try:
            con = spoji()
            try:
                if not postavke.ukljucen(con, "sazetak"):
                    continue
                v = _v(con, "vrijeme")
                danas = dt.date.today().isoformat()
                if v and dt.datetime.now().strftime("%H:%M") >= v and _v(con, "poslano_dana") != danas:
                    postavke.postavi(con, "sazetak.poslano_dana", danas)  # i kad ne uspije: ne pokušava svake minute
                    posalji_u_pozadini(datum=danas)
            finally:
                con.close()
        except Exception:
            pass


def pokreni_sat():
    threading.Thread(target=_sat, daemon=True).start()
