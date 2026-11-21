# -*- coding: utf-8 -*-
"""
Spoj šest dodatnih modula (listopad 2026.) s programom (kasa.py):
    prijedlog_dan  prijedlog narudžbe po danu u tjednu, blagdanima i vremenu (prijedlog_dan.py)
    snizenje       brzo sniženje robe koja vene (snizenje.py)
    ambalaza       gajbe i povratna ambalaža po dobavljaču (ambalaza.py)
    lanci          usporedba s javnim cjenicima lanaca (lanci.py)
    radnici        radnici s PIN-om, ovlasti i dnevnik radnji (radnici.py)
    sazetak        večernji sažetak na e-mail (sazetak.py)
    fiskalizacija  fiskalizacija računa s kase, test i produkcija (fiskal.py), studeni 2026.
Svi su u Postavkama i zadano isključeni. Ekran je u dodaci.js (kartica "Dodaci").

kasa.py zove samo četiri funkcije: get() i post() na početku obrade zahtjeva,
pri_slanju() prije slanja odgovora i pri_pokretanju() kod pokretanja.
"""

import datetime as dt
import json
import os
import re

import ambalaza
import fiskal
import lanci
import postavke
import prijedlog_dan
import radnici
import sazetak
import sat
import snizenje
from baza import DB_PATH, spoji
from nabava import Greska

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NAZIV = os.environ.get("TRGOVINA_NAZIV", "Trgovina")
_pripremljeno = set()
# zaboravljen PIN vlasnika: pokrenuti s TRGOVINA_BEZ_RADNIKA=1, upisati novi PIN, pa opet normalno
BEZ_RADNIKA = os.environ.get("TRGOVINA_BEZ_RADNIKA", "").strip() == "1"

# putanja -> modul koji mora biti uključen
MODULI_PUTANJA = (
    ("/api/snizenje", "snizenje"), ("/ispis_snizenje", "snizenje"),
    ("/api/ambalaza", "ambalaza"), ("/ispis_ambalaza", "ambalaza"),
    ("/api/fiskal", "fiskalizacija"), ("/api/lanci", "lanci"), ("/api/sazetak", "sazetak"), ("/api/prijedlog_dan", "prijedlog_dan"),
)
BEZ_PRIJAVE = ("/", "/dodaci.js", "/api/dodaci/stanje", "/api/radnici/prijava", "/api/radnici/odjava",
               "/api/radnici/prvi", "/favicon.ico", "/kopija_van.js", "/donacije.js")


def pripremi(con):
    kljuc = os.path.abspath(DB_PATH)
    if kljuc in _pripremljeno:
        return
    for m in (radnici, snizenje, ambalaza, lanci, prijedlog_dan, fiskal):
        m.pripremi(con)
    con.commit()
    _pripremljeno.add(kljuc)


def pri_pokretanju(con):
    pripremi(con)
    sazetak.pokreni_sat()
    fiskal.pokreni_sat()
    sat.pokreni()


def _json(h, code, body, kolacic=None):
    tijelo = json.dumps(body, ensure_ascii=False).encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(tijelo)))
    h.send_header("Cache-Control", "no-store")
    if kolacic is not None:
        h.send_header("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict%s" % (
            radnici.KOLACIC, kolacic, "" if kolacic else "; Max-Age=0"))
    h.end_headers()
    h.wfile.write(tijelo)


def _pocetak(h, con, put, metoda, podaci=None):
    """Prijava i ovlasti. Vraća True ako je odgovor već poslan (odbijeno)."""
    pripremi(con)
    h._dodaci = {"put": put, "radnik": None, "skrij": False, "radnja": None}
    if not postavke.ukljucen(con, "radnici") or BEZ_RADNIKA:
        return False
    token = radnici.iz_kolacica(h.headers.get("Cookie"))
    r = radnici.tko(con, token)
    h._dodaci["radnik"] = r
    if put in BEZ_PRIJAVE:
        return False
    if not r:
        _json(h, 401, {"greska": "Prijavite se svojim PIN-om.", "prijava": True})
        return True
    for o in radnici.potrebne_ovlasti(put, metoda):
        if not radnici.smije(r, o):
            naziv = dict(radnici.OVLASTI)[o]
            _json(h, 403, {"greska": "%s nema ovlast: %s." % (r["ime"], naziv)})
            return True
    h._dodaci["skrij"] = not radnici.smije(r, "cijene")
    if metoda == "POST":
        h._dodaci["radnja"] = podaci
    return False


def _modul(con, put):
    for poc, m in MODULI_PUTANJA:
        if put.startswith(poc):
            postavke.provjeri(con, m)


def _ime(h):
    r = h._dodaci.get("radnik")
    return r["ime"] if r else ""


def _oib(h):
    """OIB prijavljenog radnika (za OibOper na fiskalnom računu), ako ga ima."""
    r = h._dodaci.get("radnik")
    return (r.get("oib") or "") if r else ""


def get(h, con, put, q):
    try:
        if _pocetak(h, con, put, "GET"):
            return True
        _modul(con, put)
        if put == "/dodaci.js":
            with open(os.path.join(SCRIPT_DIR, "dodaci.js"), encoding="utf-8") as f:
                h._send(200, f.read(), "text/javascript; charset=utf-8")
        elif put == "/api/dodaci/stanje":
            m = postavke.moduli(con)
            r = h._dodaci["radnik"]
            h._send(200, {"moduli": m, "radnik": r, "ovlasti": radnici.OVLASTI,
                          "radnici": [{"id": x["id"], "ime": x["ime"]} for x in radnici.popis(con)] if m["radnici"] else [],
                          "ima_vlasnika": radnici.ima_vlasnika(con), "prijava": bool(m["radnici"] and not r and not BEZ_RADNIKA)})
        elif put == "/api/kasa_upozorenje":
            # traka na vrhu kase (svi prijavljeni radnici): računi bez JIR-a i sat računala
            poruke = []
            t = fiskal.traka(con)
            if t:
                poruke.append(t)
            razlika, _ = sat.zadnje(con)
            if razlika is not None and abs(razlika) >= sat.CRVENO:
                poruke.append({"razina": "crveno", "tekst": sat.upozorenje(con)})
            h._send(200, {"poruke": poruke})
        elif put == "/api/dodaci/danas":
            m = postavke.moduli(con)
            out = {}
            if m["snizenje"]:
                s = snizenje.popis(con, 1)["snizenja"]
                out["snizenja"] = [x for x in s if x["u_tijeku"]]
            if m["ambalaza"]:
                out["ambalaza"] = [{"partner": d["partner"], "komada": d["komada"],
                                    "vrste": [v for v in d["vrste"] if v["saldo"]]} for d in ambalaza.stanje(con) if d["komada"]]
            h._send(200, out)
        elif put == "/api/prijedlog" and postavke.ukljucen(con, "prijedlog_dan"):
            try:
                h._send(200, prijedlog_dan.prijedlog(con, q.get("dana") or 28, q.get("za") or 3))
            except ValueError as e:
                raise Greska(str(e))
        elif put == "/api/prijedlog_dan/postavke":
            h._send(200, prijedlog_dan.postavke_modula(con))
        elif put == "/api/snizenje":
            h._send(200, dict(snizenje.popis(con, q.get("dana") or 30), kandidati=snizenje.kandidati(con)))
        elif put == "/api/snizenje/izracun":
            h._send(200, snizenje.izracun(con, int(q.get("id") or 0), q.get("posto") or 30))
        elif re.fullmatch(r"/ispis_snizenje/\d+", put):
            h._send(200, snizenje.etiketa_html(con, int(put.rsplit("/", 1)[1]), NAZIV,
                                               postavke.ukljucen(con, "voce_povrce")), "text/html; charset=utf-8")
        elif put == "/api/ambalaza":
            h._send(200, ambalaza.pregled(con, q))
        elif put == "/ispis_ambalaza":
            h._send(200, ambalaza.potvrda_html(con, (q.get("ids") or "").split(","), NAZIV), "text/html; charset=utf-8")
        elif put == "/api/lanci":
            h._send(200, dict(lanci.usporedba(con, q.get("sve") == "1"), stanje=lanci.stanje(),
                              postavke=lanci.postavke_modula(con)))
        elif put == "/api/lanci/trazi":
            h._send(200, {"rezultati": lanci.trazi(q.get("q") or "", q.get("lanac") or "")})
        elif put == "/api/radnici":
            h._send(200, dict(radnici.postavke_radnika(con), radnici=radnici.popis(con, svi=True)))
        elif put == "/api/radnici/radnje":
            try:
                h._send(200, radnici.radnje(con, q))
            except ValueError:
                raise Greska("Neispravan upit.")
        elif put == "/api/fiskal":
            h._send(200, fiskal.stanje(con))
        elif put == "/api/sazetak":
            try:
                datum = dt.date.fromisoformat(q["datum"]).isoformat() if q.get("datum") else None
            except ValueError:
                raise Greska("Neispravan datum.")
            naslov, tekst = sazetak.sadrzaj(con, datum)
            h._send(200, dict(postavke=sazetak.postavke_modula(con), naslov=naslov, tekst=tekst))
        else:
            return False
    except Greska as e:
        h._send(400, {"greska": str(e)})
    return True


def post(h, con, put, p):
    try:
        if _pocetak(h, con, put, "POST", p):
            return True
        _modul(con, put)
        if put == "/api/postavke" and p.get("radnici") in (True, 1, "1") and not radnici.ima_vlasnika(con):
            raise Greska("Prije uključivanja radnika upišite vlasnika s PIN-om.")
        if put == "/api/radnici/prijava":
            postavke.provjeri(con, "radnici")
            token = radnici.prijavi(con, p)
            _json(h, 200, {"ok": True}, kolacic=token)
        elif put == "/api/radnici/odjava":
            token = radnici.iz_kolacica(h.headers.get("Cookie"))
            r = radnici.tko(con, token)
            if r:
                radnici.zapisi(con, r, put, "Odjava")
            radnici.odjavi(token)
            _json(h, 200, {"ok": True}, kolacic="")
        elif put == "/api/radnici/prvi":
            # prvi vlasnik (prije uključivanja modula); poslije samo vlasnik dodaje radnike
            if radnici.ima_vlasnika(con):
                raise Greska("Vlasnik već postoji. Radnike dodaje vlasnik (Dodaci, Radnici).")
            rid = radnici.spremi(con, {"ime": p.get("ime"), "pin": p.get("pin"), "vlasnik": True})
            token = radnici.prijavi(con, {"id": rid, "pin": p.get("pin")})
            _json(h, 200, {"id": rid}, kolacic=token)
        elif put == "/api/radnici/spremi":
            h._send(200, {"id": radnici.spremi(con, p)})
        elif put == "/api/radnici/postavke":
            h._send(200, radnici.spremi_postavke(con, p))
        elif put == "/api/prijedlog_dan/postavke":
            h._send(200, prijedlog_dan.spremi_postavke(con, p))
        elif put == "/api/snizenje/pokreni":
            h._send(200, snizenje.pokreni(con, p, _ime(h)))
        elif put == "/api/snizenje/zavrsi":
            h._send(200, snizenje.zavrsi(con, p))
        elif put == "/api/ambalaza/upisi":
            h._send(200, ambalaza.upisi(con, p, _ime(h)))
        elif put == "/api/ambalaza/ponisti":
            h._send(200, ambalaza.ponisti(con, p))
        elif put == "/api/ambalaza/vrsta":
            h._send(200, {"id": ambalaza.spremi_vrstu(con, p)})
        elif put == "/api/lanci/postavke":
            h._send(200, lanci.spremi_postavke(con, p))
        elif put == "/api/lanci/preuzmi":
            lanci.preuzmi(con)
            h._send(200, lanci.stanje())
        elif put == "/api/lanci/povezi":
            h._send(200, lanci.povezi(con, p))
        elif put == "/api/fiskal/postavke":
            h._send(200, fiskal.spremi_postavke(con, p))
        elif put == "/api/fiskal/nacini":
            fiskal.spremi_nacine(con, p)
            h._send(200, fiskal.stanje(con))
        elif put == "/api/fiskal/certifikat":
            h._send(200, fiskal.spremi_certifikat(con, p.get("ime"), p.get("sadrzaj")))
        elif put == "/api/fiskal/echo":
            h._send(200, fiskal.echo(con))
        elif put == "/api/fiskal/provjera":
            h._send(200, fiskal.provjera(con))
        elif put == "/api/fiskal/posalji":
            if p.get("id"):
                f = fiskal.posalji(con, int(p["id"]))
                h._send(200, {"poslano": int(f["status"] == "poslan"), "ostalo": int(f["status"] != "poslan"),
                              "poruka": f["poruka"]})
            else:
                ok, ostalo = fiskal.posalji_neposlane(con, i_greske=True)
                h._send(200, {"poslano": ok, "ostalo": ostalo})
        elif put == "/api/fiskal/instaliraj":
            h._send(200, {"poruka": fiskal.instaliraj_cryptography()})
        elif put == "/api/sazetak/postavke":
            h._send(200, sazetak.spremi_postavke(con, p))
        elif put == "/api/sazetak/probni":
            try:
                naslov = sazetak.posalji(con, probni=True)
            except Greska as e:
                sazetak._zabiljezi(con, False, str(e))
                raise
            sazetak._zabiljezi(con, True, naslov)
            h._send(200, {"ok": True, "naslov": naslov})
        else:
            return False
    except Greska as e:
        h._send(400, {"greska": str(e)})
    return True


def _kontrola_bez_cijena(body):
    provjere = body if isinstance(body, list) else [v for x in (body or {}).values() if isinstance(x, list) for v in x] \
        if isinstance(body, dict) else []
    for k in provjere:
        if isinstance(k, dict) and "stupci" in k and any(re.search(r"nabav|marž|marz|zarad", str(s), re.I) for s in k.get("stupci") or []):
            k["stavke"] = []
            k["stupci"] = []
            k["opis"] = (k.get("opis") or "") + " (Detalji samo uz ovlast za nabavne cijene.)"
    return body


def pri_slanju(h, code, body):
    """Zove se iz Handler._send prije svakog odgovora."""
    d = getattr(h, "_dodaci", None)
    if not d or isinstance(body, (bytes, str)):
        return body
    put = d["put"]
    if d["skrij"]:
        body = radnici.skrij(body)
        if put == "/api/kontrola":
            body = _kontrola_bez_cijena(body)
    if code == 200:
        if d["radnja"] is not None and put not in ("/api/radnici/prijava", "/api/radnici/odjava"):
            try:
                con = spoji()
                try:
                    radnici.zapisi(con, d["radnik"], put, radnici.opis_radnje(con, put, d["radnja"], body))
                finally:
                    con.close()
            except Exception:
                pass
        if put == "/api/smjena/zatvori" and isinstance(body, dict) and body.get("id"):
            try:
                sazetak.nakon_zakljucka(body["id"])
            except Exception:
                pass
    return body


def stranica(html):
    """Ubaci dodaci.js u kasa.html."""
    return html.replace("</body>", '<script src="/dodaci.js"></script>\n</body>', 1)
