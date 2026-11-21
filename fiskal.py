# -*- coding: utf-8 -*-
"""
Fiskalizacija računa u krajnjoj potrošnji (gotovina, kartice) prema Tehničkoj
specifikaciji Porezne uprave v2.7 (srpanj 2026.):
    - zahtjev RacunZahtjev (SOAP, imenski prostor http://www.apis-it.hr/fin/2012/types/f73)
    - XML potpis RSA-SHA256, sažetak SHA-256, exc-c14n (SHA-1 se na testu odbija od 1.7.2026.,
      u produkciji od 1.1.2027.)
    - ZKI = MD5(RSA-SHA256 potpis od OIB + datum i vrijeme + broj + prostor + uređaj + iznos)
    - QR kod na računu: https://porezna.gov.hr/rn?jir=...&datv=GGGGMMDD_SSMM&izn=iznos u centima
    - račun bez JIR-a (nema interneta) ima ZKI, a šalje se naknadno s NakDost=true

Modul "fiskalizacija" u Postavkama (zadano isključen). Dvije okoline:
    test        demo certifikat FINA-e, testni poslužitelj Porezne (cistest.apis-it.hr);
                računi NE vrijede, na ispisu piše TEST
    produkcija  pravi certifikat, pravi poslužitelj; uključuje se samo uz potvrdu

Treba Python paket "cryptography" (čitanje .p12 certifikata i potpis). Ako ga nema,
Postavke fiskalizacije nude gumb koji ga instalira (pip, treba internet).

Fiskalni podaci računa su u tablici fiskal_racuni (račun iz tablice racuni se ne mijenja).
Broj računa za fiskalizaciju ide posebno po okolini, godini, prostoru i uređaju
(oznaka slijeda N = na razini naplatnog uređaja), da se ne miješa s Robom ni s vježbom.
"""

import base64
import http.client
import datetime as dt
import hashlib
import html
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET

import postavke
import potpis_odgovora
import sat
from baza import oib_ispravan, pdv_iz_bruto, spoji
from nabava import Greska

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CERT_MAPA = os.environ.get("TRGOVINA_CERT_MAPA") or os.path.join(SCRIPT_DIR, "certifikati")

URL = {
    "test": "https://cistest.apis-it.hr:8449/FiskalizacijaServiceTest",
    "produkcija": "https://cis.porezna-uprava.hr:8449/FiskalizacijaService",
}
if os.environ.get("TRGOVINA_FISKAL_TEST_URL"):  # samo za automatske testove (lažni poslužitelj)
    URL["test"] = os.environ["TRGOVINA_FISKAL_TEST_URL"]
NS = "http://www.apis-it.hr/fin/2012/types/f73"
DS = "http://www.w3.org/2000/09/xmldsig#"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
AKCIJA = "http://e-porezna.porezna-uprava.hr/fiskalizacija/2012/services/FiskalizacijaService/"
QR_URL = "https://porezna.gov.hr/rn"

NACINI = (("G", "Gotovina"), ("K", "Kartice"), ("T", "Transakcijski račun"), ("O", "Ostalo"))
PONOVI_SVAKIH = 120      # sekundi između pokušaja slanja računa bez JIR-a
TRAKA_MINUTA = 60        # račun bez JIR-a stariji od ovoga: crvena traka na kasi i e-mail vlasniku
MAIL_PONOVI = 15 * 60    # sekundi između pokušaja slanja upozorenja e-mailom ako ne uspije
# Greške poslužitelja koje se mogu ponoviti (privremene); ostale traže ispravak podataka
PRIVREMENE = ("s006",)

ZADANO = {
    "okolina": "test",
    "oib": "",
    "u_pdv": "1",
    "prostor": "",
    "uredjaj": "1",
    "oib_operatera": "",
    "operater": "",
    "naziv_tvrtke": "",
    "adresa": "",
    "cert_putanja": "",
    "cert_lozinka": "",
    "ca_putanja": "",
    "tls_bez_provjere": "0",
    "timeout": "6",
}

SHEMA = """
CREATE TABLE IF NOT EXISTS fiskal_racuni (
    racun_id      INTEGER PRIMARY KEY REFERENCES racuni(id),
    okolina       TEXT NOT NULL,             -- test / produkcija
    godina        INTEGER NOT NULL,
    broj          INTEGER NOT NULL,          -- BrOznRac
    prostor       TEXT NOT NULL,             -- OznPosPr
    uredjaj       TEXT NOT NULL,             -- OznNapUr
    oib           TEXT NOT NULL,
    u_pdv         INTEGER NOT NULL,
    oib_operatera TEXT NOT NULL,
    operater      TEXT NOT NULL DEFAULT '',
    dat_vrijeme   TEXT NOT NULL,             -- dd.mm.gggg.Thh:mm:ss kako je poslano
    ukupno        INTEGER NOT NULL,          -- centi
    nacin         TEXT NOT NULL,             -- G K T O
    zki           TEXT NOT NULL,
    jir           TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'ceka',   -- ceka / poslan / greska
    nak_dost      INTEGER NOT NULL DEFAULT 0,
    pokusaja      INTEGER NOT NULL DEFAULT 0,
    zadnji        TEXT NOT NULL DEFAULT '',  -- vrijeme zadnjeg pokušaja
    poruka        TEXT NOT NULL DEFAULT '',  -- zadnja greška
    poslan        TEXT NOT NULL DEFAULT '',  -- kad je dobiven JIR
    UNIQUE (okolina, godina, prostor, uredjaj, broj)
);
CREATE INDEX IF NOT EXISTS ix_fiskal_status ON fiskal_racuni(status);
"""

_lock = threading.Lock()
_cert_cache = {}
_sat = {"radi": False, "mail_pokusaj": 0.0}


def pripremi(con):
    con.executescript(SHEMA)


# ---------------------------------------------------------------- postavke

def _v(con, k):
    return postavke.vrijednost(con, "fiskal." + k, ZADANO[k])


def sve_postavke(con):
    return {k: _v(con, k) for k in ZADANO}


def _zastiti(tekst):
    """Lozinka certifikata: na Windowsima šifrirana za ovog korisnika (DPAPI), inače base64."""
    if not tekst:
        return ""
    if os.name == "nt":
        try:
            return "dpapi:" + base64.b64encode(_dpapi(tekst.encode("utf-8"), True)).decode()
        except Exception:
            pass
    return "b64:" + base64.b64encode(tekst.encode("utf-8")).decode()


def _otkrij(zapis):
    if not zapis:
        return ""
    if zapis.startswith("dpapi:"):
        return _dpapi(base64.b64decode(zapis[6:]), False).decode("utf-8")
    if zapis.startswith("b64:"):
        return base64.b64decode(zapis[4:]).decode("utf-8")
    return zapis


def _dpapi(podaci, sifriraj):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    ulaz = BLOB(len(podaci), ctypes.create_string_buffer(podaci, len(podaci)))
    izlaz = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if sifriraj else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(ulaz), None, None, None, None, 0, ctypes.byref(izlaz)):
        raise OSError("DPAPI")
    try:
        return ctypes.string_at(izlaz.pbData, izlaz.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(izlaz.pbData)


def spremi_postavke(con, p):
    stare = sve_postavke(con)
    nove = dict(stare)
    for k in ("oib", "oib_operatera"):
        if k in p:
            v = re.sub(r"\D", "", str(p[k] or ""))
            if v and not oib_ispravan(v):
                raise Greska("%s nije ispravan (kontrolna znamenka)." % ("OIB obveznika" if k == "oib" else "OIB operatera"))
            nove[k] = v
    if "prostor" in p:
        v = str(p["prostor"] or "").strip()
        if v and not re.fullmatch(r"[0-9A-Za-z]{1,20}", v):
            raise Greska("Oznaka poslovnog prostora smije imati samo slova i brojke (bez razmaka i kvačica), do 20 znakova.")
        nove["prostor"] = v
    if "uredjaj" in p:
        v = str(p["uredjaj"] or "").strip()
        if not re.fullmatch(r"[1-9][0-9]{0,19}", v):
            raise Greska("Oznaka naplatnog uređaja je broj bez vodećih nula (npr. 2).")
        nove["uredjaj"] = v
    for k in ("operater", "naziv_tvrtke", "adresa", "cert_putanja", "ca_putanja"):
        if k in p:
            nove[k] = str(p[k] or "").strip()[:300]
    if "u_pdv" in p:
        nove["u_pdv"] = "1" if p["u_pdv"] in (True, 1, "1", "true") else "0"
    if "tls_bez_provjere" in p:
        nove["tls_bez_provjere"] = "1" if p["tls_bez_provjere"] in (True, 1, "1", "true") else "0"
    if "timeout" in p:
        try:
            t = int(p["timeout"])
        except (TypeError, ValueError):
            raise Greska("Vrijeme čekanja mora biti broj sekundi.")
        if not 2 <= t <= 30:
            raise Greska("Vrijeme čekanja: 2 do 30 sekundi.")
        nove["timeout"] = str(t)
    if p.get("cert_lozinka") is not None and p.get("cert_lozinka") != "":
        nove["cert_lozinka"] = _zastiti(str(p["cert_lozinka"]))
    if "okolina" in p:
        ok = str(p["okolina"])
        if ok not in URL:
            raise Greska("Okolina je test ili produkcija.")
        if ok == "produkcija" and stare["okolina"] != "produkcija":
            if str(p.get("potvrda") or "").strip().upper() != "PRODUKCIJA":
                raise Greska("Za prelazak na pravu fiskalizaciju upišite riječ PRODUKCIJA u polje za potvrdu.")
        nove["okolina"] = ok
    if nove["okolina"] == "produkcija":
        _provjeri_za_produkciju(nove)
    with con:
        for k, v in nove.items():
            if v != stare[k]:
                con.execute("INSERT OR REPLACE INTO postavke (kljuc, vrijednost) VALUES (?, ?)", ("fiskal." + k, v))
    return stanje(con)


def _provjeri_za_produkciju(pst):
    if not (pst["oib"] and pst["prostor"]):
        raise Greska("Za produkciju trebaju OIB i oznaka poslovnog prostora.")
    info = podaci_certifikata(pst)
    if info.get("greska"):
        raise Greska("Certifikat: %s" % info["greska"])
    if info["demo"]:
        raise Greska("Učitan je DEMO certifikat. Za produkciju treba pravi (produkcijski) certifikat za fiskalizaciju.")
    if info["istekao"]:
        raise Greska("Certifikat je istekao.")
    if pst["tls_bez_provjere"] == "1":
        raise Greska("Isključena provjera poslužitelja smije se koristiti samo na testu.")


def _nacin_racuna(con, rid, pid):
    """Račun plaćen s više načina (gotovina + kartica) šalje se kao O (ostalo)."""
    if con.execute("SELECT COUNT(DISTINCT nacin_placanja_id) FROM racun_placanja WHERE racun_id = ?",
                   (rid,)).fetchone()[0] > 1:
        return "O"
    return nacin_za(con, pid)


def nacin_za(con, pid, naziv=None):
    """Način plaćanja za fiskalizaciju (G, K, T, O): ručno odabran u postavkama ili po nazivu."""
    v = postavke.vrijednost(con, "fiskal.nacin.%s" % pid, "")
    if v in dict(NACINI):
        return v
    if naziv is None:
        r = con.execute("SELECT naziv FROM nacini_placanja WHERE id = ?", (pid,)).fetchone()
        naziv = r[0] if r else ""
    n = naziv.lower()
    if "gotov" in n:
        return "G"
    if re.search(r"kartic|card|visa|master|maestro|amex|american|diners|discover|debit", n):
        return "K"
    if re.search(r"transak|virman|uplat|ž?iro|banka|predra", n):
        return "T"
    return "O"


def spremi_nacine(con, p):
    with con:
        for pid, v in (p or {}).items():
            if v not in dict(NACINI) and v != "":
                raise Greska("Nepoznat način plaćanja: %s." % v)
            con.execute("INSERT OR REPLACE INTO postavke (kljuc, vrijednost) VALUES (?, ?)",
                        ("fiskal.nacin.%d" % int(pid), v))


# ---------------------------------------------------------------- certifikat

def ima_cryptography():
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def instaliraj_cryptography():
    """pip install --user cryptography (treba internet). Vraća tekst za prikaz."""
    if ima_cryptography():
        return "Paket cryptography je već instaliran."
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--user", "cryptography"],
                           capture_output=True, text=True, timeout=600)
    except Exception as e:
        raise Greska("Instalacija nije uspjela: %s" % e)
    if r.returncode != 0:
        raise Greska("Instalacija nije uspjela (treba internet):\n%s" % (r.stderr or r.stdout)[-800:])
    import importlib
    import site
    try:
        site.addsitedir(site.getusersitepackages())
    except Exception:
        pass
    importlib.invalidate_caches()
    if not ima_cryptography():
        return "Instalirano. Zatvorite program i pokrenite ga ponovno."
    return "Paket cryptography je instaliran."


def spremi_certifikat(con, ime, sadrzaj_b64):
    """Certifikat poslan iz preglednika sprema se u mapu certifikati pokraj programa."""
    ime = os.path.basename(str(ime or "")).strip() or "certifikat.p12"
    if not ime.lower().endswith((".p12", ".pfx")):
        raise Greska("Certifikat za fiskalizaciju je datoteka .p12 ili .pfx.")
    try:
        podaci = base64.b64decode(sadrzaj_b64 or "", validate=True)
    except Exception:
        raise Greska("Datoteka nije ispravno poslana.")
    if not 100 < len(podaci) < 200000:
        raise Greska("To ne izgleda kao certifikat (veličina %d bajtova)." % len(podaci))
    os.makedirs(CERT_MAPA, exist_ok=True)
    put = os.path.join(CERT_MAPA, re.sub(r"[^\w.\- ]", "_", ime))
    with open(put, "wb") as f:
        f.write(podaci)
    postavke.postavi(con, "fiskal.cert_putanja", put)
    _cert_cache.clear()
    return stanje(con)


def _ucitaj(pst):
    """(ključ, certifikat) iz .p12; pamti se dok se datoteka ili lozinka ne promijene."""
    put = pst["cert_putanja"]
    if not put:
        raise Greska("Certifikat nije odabran (Fiskalizacija, Certifikat).")
    if not os.path.isfile(put):
        raise Greska("Datoteka certifikata ne postoji: %s" % put)
    if not ima_cryptography():
        raise Greska("Nedostaje Python paket cryptography. U postavkama fiskalizacije kliknite Instaliraj.")
    from cryptography.hazmat.primitives.serialization import pkcs12
    lozinka = _otkrij(pst["cert_lozinka"])
    kljuc = (put, os.path.getmtime(put), lozinka)
    if kljuc in _cert_cache:
        return _cert_cache[kljuc]
    with open(put, "rb") as f:
        podaci = f.read()
    try:
        key, cert, _ = pkcs12.load_key_and_certificates(podaci, lozinka.encode("utf-8") if lozinka else None)
    except ValueError:
        raise Greska("Certifikat se ne može otvoriti: kriva lozinka ili datoteka nije .p12 certifikat.")
    if key is None or cert is None:
        raise Greska("U datoteci nema privatnog ključa ili certifikata.")
    _cert_cache.clear()
    _cert_cache[kljuc] = (key, cert)
    return key, cert


def _ime(name):
    try:
        return name.rfc4514_string()
    except Exception:
        return str(name)


def podaci_certifikata(pst):
    try:
        key, cert = _ucitaj(pst)
    except Greska as e:
        return {"greska": str(e)}
    try:
        do = cert.not_valid_after_utc.replace(tzinfo=None)
        od = cert.not_valid_before_utc.replace(tzinfo=None)
    except AttributeError:  # starija verzija paketa
        do, od = cert.not_valid_after, cert.not_valid_before
    subjekt, izdavatelj = _ime(cert.subject), _ime(cert.issuer)
    oibi = re.findall(r"(?<!\d)(\d{11})(?!\d)", subjekt)
    sada = dt.datetime.utcnow()
    return {"subjekt": subjekt, "izdavatelj": izdavatelj,
            "vrijedi_od": od.strftime("%d.%m.%Y."), "vrijedi_do": do.strftime("%d.%m.%Y."),
            "dana_do_isteka": (do - sada).days, "istekao": do < sada,
            "demo": bool(re.search(r"demo|test", izdavatelj, re.I)),
            "oib": next((o for o in oibi if oib_ispravan(o)), oibi[0] if oibi else "")}


# ---------------------------------------------------------------- XML

def _t(s):
    """Tekst za XML u kanonskom obliku (c14n)."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", "&#xD;")


def iznos(c):
    """Centi -> '12.50' / '-3.05'."""
    return "%s%d.%02d" % ("-" if c < 0 else "", abs(c) // 100, abs(c) % 100)


def dat_vrijeme(iso):
    """'2026-11-18 17:05:09' -> '18.11.2026T17:05:09'."""
    d = dt.datetime.strptime(iso[:19], "%Y-%m-%d %H:%M:%S")
    return d.strftime("%d.%m.%YT%H:%M:%S")


def izracunaj_zki(key, oib, datv, broj, prostor, uredjaj, ukupno_c):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    ulaz = "%s%s%s%s%s%s" % (oib, datv.replace("T", " "), broj, prostor, uredjaj, iznos(ukupno_c))
    potpis = key.sign(ulaz.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return hashlib.md5(potpis).hexdigest()


def _pdv_stavke(stavke, u_pdv):
    """Porez po stopama iz stavki računa (bruto u centima)."""
    if not u_pdv:
        return []
    po = {}
    for s in stavke:
        po[s["pdv_stopa"]] = po.get(s["pdv_stopa"], 0) + s["iznos"]
    out = []
    for stopa, bruto in sorted(po.items()):
        pdv = pdv_iz_bruto(abs(bruto), stopa) * (1 if bruto >= 0 else -1)
        out.append((stopa, bruto - pdv, pdv))
    return out


def racun_xml(f, stavke):
    """<tns:Racun> za zapis iz fiskal_racuni (kanonski, bez razmaka)."""
    x = ["<tns:Racun>",
         "<tns:Oib>%s</tns:Oib>" % f["oib"],
         "<tns:USustPdv>%s</tns:USustPdv>" % ("true" if f["u_pdv"] else "false"),
         "<tns:DatVrijeme>%s</tns:DatVrijeme>" % f["dat_vrijeme"],
         "<tns:OznSlijed>N</tns:OznSlijed>",
         "<tns:BrRac><tns:BrOznRac>%d</tns:BrOznRac><tns:OznPosPr>%s</tns:OznPosPr>"
         "<tns:OznNapUr>%s</tns:OznNapUr></tns:BrRac>" % (f["broj"], _t(f["prostor"]), _t(f["uredjaj"]))]
    pdv = _pdv_stavke(stavke, f["u_pdv"])
    if pdv:
        x.append("<tns:Pdv>")
        for stopa, osn, p in pdv:
            x.append("<tns:Porez><tns:Stopa>%.2f</tns:Stopa><tns:Osnovica>%s</tns:Osnovica>"
                     "<tns:Iznos>%s</tns:Iznos></tns:Porez>" % (stopa, iznos(osn), iznos(p)))
        x.append("</tns:Pdv>")
    # Porezna (greška 166): ukupni iznos = zbroj osnovica + PDV (tolerancija 0,01 EUR).
    # Ovdje je osnovica = bruto - PDV po stopi, pa je zbroj točan na cent ako su stavke
    # zbrojene u ukupno; inače račun ne šaljemo krivo nego javimo grešku.
    if pdv and abs(sum(o + p for _, o, p in pdv) - f["ukupno"]) > 1:
        raise Greska("Zbroj osnovica i PDV-a (%s) nije jednak ukupnom iznosu računa (%s)."
                     % (iznos(sum(o + p for _, o, p in pdv)), iznos(f["ukupno"])))
    x += ["<tns:IznosUkupno>%s</tns:IznosUkupno>" % iznos(f["ukupno"]),
          "<tns:NacinPlac>%s</tns:NacinPlac>" % f["nacin"],
          "<tns:OibOper>%s</tns:OibOper>" % f["oib_operatera"],
          "<tns:ZastKod>%s</tns:ZastKod>" % f["zki"],
          "<tns:NakDost>%s</tns:NakDost>" % ("true" if f["nak_dost"] else "false"),
          "</tns:Racun>"]
    return "".join(x)


def _zaglavlje():
    return ("<tns:Zaglavlje><tns:IdPoruke>%s</tns:IdPoruke><tns:DatumVrijeme>%s</tns:DatumVrijeme></tns:Zaglavlje>"
            % (uuid.uuid4(), dt.datetime.now().strftime("%d.%m.%YT%H:%M:%S")))


def potpisi(element, sadrzaj, key, cert):
    """Potpisani <tns:element Id=...>sadrzaj<Signature/></tns:element> (XML-DSig, enveloped,
    exc-c14n, RSA-SHA256). Sadržaj mora biti u kanonskom obliku (kako ga gradimo gore)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    rid = "%s-%s" % (element, uuid.uuid4().hex[:12])
    otvori = '<tns:%s xmlns:tns="%s" Id="%s">' % (element, NS, rid)
    zatvori = "</tns:%s>" % element
    sazetak = base64.b64encode(hashlib.sha256((otvori + sadrzaj + zatvori).encode("utf-8")).digest()).decode()
    signed_info = (
        '<SignedInfo xmlns="%s">'
        '<CanonicalizationMethod Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"></CanonicalizationMethod>'
        '<SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"></SignatureMethod>'
        '<Reference URI="#%s"><Transforms>'
        '<Transform Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature"></Transform>'
        '<Transform Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"></Transform>'
        '</Transforms><DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"></DigestMethod>'
        '<DigestValue>%s</DigestValue></Reference></SignedInfo>' % (DS, rid, sazetak))
    vrijednost = base64.b64encode(key.sign(signed_info.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())).decode()
    der = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
    potpis = ('<Signature xmlns="%s">%s<SignatureValue>%s</SignatureValue><KeyInfo><X509Data>'
              '<X509Certificate>%s</X509Certificate><X509IssuerSerial><X509IssuerName>%s</X509IssuerName>'
              '<X509SerialNumber>%d</X509SerialNumber></X509IssuerSerial></X509Data></KeyInfo></Signature>'
              % (DS, signed_info, vrijednost, der, _t(_ime(cert.issuer)), cert.serial_number))
    return otvori + sadrzaj + potpis + zatvori


def omotnica(tijelo):
    return ('<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope xmlns:soapenv="%s"><soapenv:Body>%s'
            '</soapenv:Body></soapenv:Envelope>' % (SOAP_NS, tijelo))


# ---------------------------------------------------------------- slanje

class NemaVeze(Exception):
    """Poslužitelj nedostupan (nema interneta, istek vremena...): račun čeka ponovno slanje."""


def _ssl(pst):
    if pst["okolina"] == "test" and pst["tls_bez_provjere"] == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    dodatni = []
    if pst["ca_putanja"]:
        dodatni.append(pst["ca_putanja"])
    if os.path.isdir(CERT_MAPA):
        dodatni += [os.path.join(CERT_MAPA, f) for f in os.listdir(CERT_MAPA)
                    if f.lower().endswith((".pem", ".crt", ".cer"))]
    for put in dodatni:
        try:
            ctx.load_verify_locations(cafile=put)
        except (ssl.SSLError, OSError):
            try:  # .cer u DER obliku
                with open(put, "rb") as f:
                    ctx.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(f.read()))
            except Exception:
                pass
    return ctx


def posalji_soap(pst, tijelo, akcija):
    """Šalje SOAP poruku, vraća tekst odgovora. NemaVeze ako poslužitelj nije dostupan."""
    zahtjev = urllib.request.Request(
        URL[pst["okolina"]], data=omotnica(tijelo).encode("utf-8"), method="POST",
        headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": AKCIJA + akcija})
    try:
        with urllib.request.urlopen(zahtjev, timeout=int(pst["timeout"]), context=_ssl(pst)) as r:
            tekst = r.read().decode("utf-8", "replace")
            sat.zabiljezi_odgovor(r, "Porezna")  # sat računala prema poslužitelju (greška 174)
        if "Envelope" not in tekst:  # npr. stranica proxyja ili Wi-Fi prijave, a ne Porezna
            raise NemaVeze("Odgovor nije stigao od Porezne (%s)." % re.sub(r"\s+", " ", tekst)[:80])
        return tekst
    except urllib.error.HTTPError as e:  # SOAP greška dolazi s HTTP 500, ali tijelo je odgovor
        tekst = e.read().decode("utf-8", "replace")
        if "Envelope" in tekst:
            sat.zabiljezi_odgovor(e, "Porezna")
            return tekst
        raise NemaVeze("Poslužitelj je vratio HTTP %d." % e.code)
    except ssl.SSLCertVerificationError as e:
        raise NemaVeze("Provjera certifikata poslužitelja nije uspjela (%s). Za test: u postavkama "
                       "fiskalizacije uključite 'Ne provjeravaj poslužitelja (samo test)' ili stavite "
                       "FINA CA certifikat u mapu certifikati." % getattr(e, "verify_message", e))
    except http.client.HTTPException as e:  # prekinut ili neispravan odgovor
        raise NemaVeze("Veza s Poreznom je prekinuta (%s)." % (type(e).__name__))
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        razlog = getattr(e, "reason", e)
        raise NemaVeze("Nema veze s poslužiteljem Porezne (%s)." % razlog)


def provjeri_potpis(con, odgovor):
    """Provjeri potpis Porezne na odgovoru i zapamti zadnji rezultat (za Kontrolu i postavke).
    Vraća stanje: ok / neispravan / upozorenje / nije_provjeren."""
    try:
        stanje, opis, cert = potpis_odgovora.provjeri(odgovor, CERT_MAPA)
    except Exception as e:  # greška u samoj provjeri ne smije zaustaviti račune
        stanje, opis, cert = "nije_provjeren", "Provjera potpisa nije uspjela (%s: %s)." % (type(e).__name__, e), {}
    try:
        postavke.postavi(con, "fiskal.potpis_stanje", stanje)
        postavke.postavi(con, "fiskal.potpis_opis", opis)
        postavke.postavi(con, "fiskal.potpis_kada", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if cert:
            postavke.postavi(con, "fiskal.potpis_cert", "%s, izdao %s, vrijedi do %s, otisak %s" % (
                cert.get("cn", ""), cert.get("izdavatelj", ""), cert.get("vrijedi_do", ""), cert.get("otisak", "")))
            postavke.postavi(con, "fiskal.potpis_cert_do", cert.get("isteka_iso", ""))
    except Exception:
        pass
    return stanje


def potpis_stanje(con):
    """Zadnja provjera potpisa Porezne (za prikaz)."""
    v = lambda k: postavke.vrijednost(con, "fiskal.potpis_" + k, "")
    return {"stanje": v("stanje"), "opis": v("opis"), "kada": v("kada"), "cert": v("cert"), "cert_do": v("cert_do")}


def procitaj_odgovor(tekst):
    """(jir, [(šifra, poruka)]) iz odgovora."""
    try:
        root = ET.fromstring(tekst.encode("utf-8"))
    except ET.ParseError:
        return "", [("xml", "Odgovor nije ispravan XML: %s" % tekst[:200])]
    jir = root.find(".//{%s}Jir" % NS)
    greske = [((g.findtext("{%s}SifraGreske" % NS) or "").strip(), (g.findtext("{%s}PorukaGreske" % NS) or "").strip())
              for g in root.iter("{%s}Greska" % NS)]
    fault = root.find(".//{%s}Fault" % SOAP_NS)
    if fault is not None and not greske:
        greske = [("soap", (fault.findtext("faultstring") or "SOAP greška").strip())]
    return (jir.text.strip() if jir is not None and jir.text else ""), greske


# ---------------------------------------------------------------- računi

def spreman(con):
    """Greška ako fiskalizacija nije spremna (zove se prije naplate)."""
    pst = sve_postavke(con)
    if not pst["oib"]:
        raise Greska("Fiskalizacija: upišite OIB obveznika (Dodaci, Fiskalizacija).")
    if not pst["prostor"]:
        raise Greska("Fiskalizacija: upišite oznaku poslovnog prostora (Dodaci, Fiskalizacija).")
    _ucitaj(pst)
    return pst


def pri_naplati(con, rid, operater="", oib_radnika=""):
    """U istoj transakciji kao račun: fiskalni broj i ZKI. Slanje ide poslije (posalji)."""
    pst = spreman(con)
    key, _ = _ucitaj(pst)
    r = con.execute("SELECT * FROM racuni WHERE id = ?", (rid,)).fetchone()
    godina = int(r["vrijeme"][:4])
    broj = con.execute(
        "SELECT COALESCE(MAX(broj), 0) + 1 FROM fiskal_racuni WHERE okolina = ? AND godina = ? "
        "AND prostor = ? AND uredjaj = ?", (pst["okolina"], godina, pst["prostor"], pst["uredjaj"])).fetchone()[0]
    datv = dat_vrijeme(r["vrijeme"])
    oib_oper = oib_radnika or pst["oib_operatera"] or pst["oib"]  # OIB prijavljenog radnika ako ga ima
    zki = izracunaj_zki(key, pst["oib"], datv, broj, pst["prostor"], pst["uredjaj"], r["ukupno"])
    con.execute(
        """INSERT INTO fiskal_racuni (racun_id, okolina, godina, broj, prostor, uredjaj, oib, u_pdv,
               oib_operatera, operater, dat_vrijeme, ukupno, nacin, zki)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (rid, pst["okolina"], godina, broj, pst["prostor"], pst["uredjaj"], pst["oib"], int(pst["u_pdv"]),
         oib_oper, (operater or pst["operater"] or "")[:60], datv, r["ukupno"],
         _nacin_racuna(con, rid, r["nacin_placanja_id"]), zki))


def posalji(con, rid):
    """Šalje račun (prvi put ili ponovno). Vraća zapis fiskal_racuni kao dict."""
    with _lock:
        f = con.execute("SELECT * FROM fiskal_racuni WHERE racun_id = ?", (rid,)).fetchone()
        if not f:
            raise Greska("Račun nije fiskalni.")
        if f["status"] == "poslan":
            return dict(f)
        pst = sve_postavke(con)
        if pst["okolina"] != f["okolina"]:
            # račun izdan u drugoj okolini šalje se tamo gdje je izdan
            pst = dict(pst, okolina=f["okolina"])
        sada = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        nak = 1 if f["pokusaja"] > 0 else 0  # kupac je već dobio račun bez JIR-a
        try:
            key, cert = _ucitaj(pst)
            stavke = [dict(s) for s in con.execute("SELECT * FROM racun_stavke WHERE racun_id = ?", (rid,))]
            podaci = dict(f)
            podaci["nak_dost"] = nak
            xml = potpisi("RacunZahtjev", _zaglavlje() + racun_xml(podaci, stavke), key, cert)
            odgovor = posalji_soap(pst, xml, "racuni")
            jir, greske = procitaj_odgovor(odgovor)
            if jir and provjeri_potpis(con, odgovor) == "neispravan":
                # JIR iz odgovora koji nije potpisala Porezna (ili je promijenjen) ne vrijedi
                jir, greske = "", [("potpis", "Potpis na odgovoru Porezne nije ispravan: %s JIR nije prihvaćen, "
                                              "račun se šalje ponovno." % postavke.vrijednost(con, "fiskal.potpis_opis", ""))]
        except NemaVeze as e:
            jir, greske, status = "", [("veza", str(e))], "ceka"
        except Greska as e:
            jir, greske, status = "", [("postavke", str(e))], "ceka"
        except Exception as e:  # bilo što drugo: račun ostaje za ponovno slanje, naplata se ne ruši
            jir, greske, status = "", [("veza", "Neočekivana greška pri slanju (%s: %s)." % (type(e).__name__, e))], "ceka"
        else:
            if jir:
                status = "poslan"
            elif greske and all(g[0] in PRIVREMENE or g[0] in ("xml", "potpis") for g in greske):
                status = "ceka"
            elif not greske:  # odgovor bez JIR-a i bez greške: pokušaj ponovno
                status, greske = "ceka", [("?", "Odgovor bez JIR-a, šalje se ponovno.")]
            else:
                status = "greska"
                if not greske:
                    greske = [("?", "Odgovor bez JIR-a.")]
        poruka = "; ".join(("%s: %s" % g) if g[0] not in ("veza", "postavke", "potpis") else g[1] for g in greske)
        with con:
            con.execute(
                """UPDATE fiskal_racuni SET jir = ?, status = ?, nak_dost = ?, pokusaja = pokusaja + 1,
                       zadnji = ?, poruka = ?, poslan = CASE WHEN ? = 'poslan' THEN ? ELSE poslan END
                   WHERE racun_id = ?""",
                (jir, status, nak, sada, poruka[:1000], status, sada, rid))
        return dict(con.execute("SELECT * FROM fiskal_racuni WHERE racun_id = ?", (rid,)).fetchone())


def posalji_neposlane(con, i_greske=False):
    """Ponovno slanje računa bez JIR-a. Vraća (poslano, ostalo)."""
    statusi = ("ceka", "greska") if i_greske else ("ceka",)
    ids = [r[0] for r in con.execute(
        "SELECT racun_id FROM fiskal_racuni WHERE status IN (%s) ORDER BY racun_id" % ",".join("?" * len(statusi)),
        statusi)]
    ok = 0
    for rid in ids:
        f = posalji(con, rid)
        if f["status"] == "poslan":
            ok += 1
        elif f["poruka"].startswith("Nema veze") or "certifikata poslužitelja" in f["poruka"]:
            break  # nema interneta: ne čekati na svakom računu
    return ok, len(ids) - ok


def pokreni_sat():
    """Pozadinska dretva: svake dvije minute šalje račune koji čekaju."""
    if _sat["radi"]:
        return
    _sat["radi"] = True

    def petlja():
        while True:
            time.sleep(PONOVI_SVAKIH)
            try:
                con = spoji()
                try:
                    if postavke.ukljucen(con, "fiskalizacija") and con.execute(
                            "SELECT 1 FROM fiskal_racuni WHERE status = 'ceka' LIMIT 1").fetchone():
                        posalji_neposlane(con)
                    upozori_mailom(con)
                finally:
                    con.close()
            except Exception:
                pass

    threading.Thread(target=petlja, daemon=True, name="fiskal-ponovi").start()


def echo(con):
    """Provjera veze (Echo): ne treba certifikat."""
    pst = sve_postavke(con)
    poslano = "Proba veze %s" % dt.datetime.now().strftime("%H:%M:%S")
    t0 = time.time()
    try:
        odg = posalji_soap(pst, '<tns:EchoRequest xmlns:tns="%s">%s</tns:EchoRequest>' % (NS, poslano), "echo")
    except NemaVeze as e:
        raise Greska(str(e))
    ms = int((time.time() - t0) * 1000)
    if poslano not in odg:
        raise Greska("Poslužitelj je odgovorio, ali ne očekivano: %s" % odg[:300])
    poruka = "Veza s poslužiteljem (%s) radi, odgovor za %d ms." % (pst["okolina"], ms)
    razlika, _ = sat.zadnje(con)
    if razlika is not None:
        poruka += " " + (sat.opis(razlika) if abs(razlika) >= sat.ZUTO else "Sat računala je točan.")
    return {"ok": True, "poruka": poruka}


def provjera(con):
    """Probni račun 1,00 EUR porukom ProvjeraZahtjev (samo test okolina): provjerava certifikat,
    OIB i potpis, a ne troši broj računa i ne upisuje se kao račun."""
    pst = spreman(con)
    if pst["okolina"] != "test":
        raise Greska("Probni račun (provjera) postoji samo na testnoj okolini.")
    key, cert = _ucitaj(pst)
    datv = dt.datetime.now().strftime("%d.%m.%YT%H:%M:%S")
    f = {"oib": pst["oib"], "u_pdv": pst["u_pdv"] == "1", "dat_vrijeme": datv, "broj": 1,
         "prostor": pst["prostor"], "uredjaj": pst["uredjaj"], "ukupno": 100, "nacin": "G",
         "oib_operatera": pst["oib_operatera"] or pst["oib"], "nak_dost": 0}
    f["zki"] = izracunaj_zki(key, f["oib"], datv, 1, f["prostor"], f["uredjaj"], 100)
    xml = potpisi("ProvjeraZahtjev", _zaglavlje() + racun_xml(f, [{"pdv_stopa": 5.0, "iznos": 100}]), key, cert)
    try:
        odg = posalji_soap(pst, xml, "provjera")
    except NemaVeze as e:
        raise Greska(str(e))
    _, greske = procitaj_odgovor(odg)
    # Provjera vraća poruke "vNNN"; v100 "Poruka je ispravna" znači da je sve u redu.
    greske = [g for g in greske if g[0].lower() != "v100"]
    if greske:
        raise Greska("Porezna je javila grešku: " + "; ".join("%s: %s" % g for g in greske))
    stanje = provjeri_potpis(con, odg)
    p = potpis_stanje(con)
    dodatak = {"ok": " Potpis odgovora Porezne je ispravan (%s)." % p["cert"],
               "neispravan": " POZOR: potpis odgovora Porezne NIJE ispravan: %s" % p["opis"],
               "upozorenje": " Potpis Porezne je ispravan, ali: %s" % p["opis"],
               "nije_provjeren": " Potpis odgovora nije provjeren: %s" % p["opis"]}[stanje]
    return {"ok": stanje != "neispravan", "poruka": "Probni račun je prošao provjeru Porezne (v100: poruka je "
                                                    "ispravna). Certifikat, OIB i potpis su u redu." + dodatak}


# ---------------------------------------------------------------- prikaz

def zapis(con, rid):
    r = con.execute("SELECT * FROM fiskal_racuni WHERE racun_id = ?", (rid,)).fetchone()
    return dict(r) if r else None


def oznaka(f):
    return "%d/%s/%s" % (f["broj"], f["prostor"], f["uredjaj"])


def qr_url(f):
    d = dt.datetime.strptime(f["dat_vrijeme"], "%d.%m.%YT%H:%M:%S")
    kod = ("jir=%s" % f["jir"]) if f["jir"] else ("zki=%s" % f["zki"])
    return "%s?%s&datv=%s&izn=%d" % (QR_URL, kod, d.strftime("%Y%m%d_%H%M"), f["ukupno"])


def qr_svg(tekst, mm=24):
    from qrcodegen import QrCode
    qr = QrCode.encode_text(tekst, QrCode.Ecc.MEDIUM)
    n, rub = qr.get_size(), 2
    put = "".join("M%d,%dh1v1h-1z" % (x + rub, y + rub) for y in range(n) for x in range(n) if qr.get_module(x, y))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%dmm" height="%dmm" '
            'shape-rendering="crispEdges"><rect width="100%%" height="100%%" fill="#fff"/>'
            '<path d="%s" fill="#000"/></svg>' % (n + 2 * rub, n + 2 * rub, mm, mm, put))


def za_ispis(con, rid):
    """Podaci za fiskalni dio ispisa računa ili None."""
    f = zapis(con, rid)
    if not f:
        return None
    pst = sve_postavke(con)
    return dict(f, oznaka=oznaka(f), qr=qr_svg(qr_url(f)), naziv_tvrtke=pst["naziv_tvrtke"], adresa=pst["adresa"],
                nacin_naziv=dict(NACINI).get(f["nacin"], f["nacin"]))


def dodaj_u_popis(con, rows):
    """Računi u popisu dobiju fiskalnu oznaku i status."""
    if not rows:
        return rows
    ids = [r["id"] for r in rows]
    fz = {r["racun_id"]: r for r in con.execute(
        "SELECT racun_id, broj, prostor, uredjaj, status, okolina, jir FROM fiskal_racuni WHERE racun_id IN (%s)"
        % ",".join("?" * len(ids)), ids)}
    for r in rows:
        f = fz.get(r["id"])
        if f:
            r["fiskal"] = {"oznaka": oznaka(f), "status": f["status"], "okolina": f["okolina"], "jir": f["jir"]}
    return rows


def stanje(con):
    pst = sve_postavke(con)
    javno = {k: v for k, v in pst.items() if k != "cert_lozinka"}
    javno["lozinka_upisana"] = bool(pst["cert_lozinka"])
    cert = podaci_certifikata(pst) if pst["cert_putanja"] else None
    upozorenja = []
    if cert and not cert.get("greska"):
        if pst["oib"] and cert["oib"] and cert["oib"] != pst["oib"]:
            upozorenja.append("OIB u certifikatu (%s) nije isti kao upisani OIB (%s). Porezna će odbiti račun (s005)."
                              % (cert["oib"], pst["oib"]))
        if pst["okolina"] == "test" and not cert["demo"]:
            upozorenja.append("Na testu se koristi DEMO certifikat; ovaj izgleda kao pravi.")
        if cert["istekao"]:
            upozorenja.append("Certifikat je istekao %s." % cert["vrijedi_do"])
        elif cert["dana_do_isteka"] < 30:
            upozorenja.append("Certifikat ističe za %d dana (%s)." % (cert["dana_do_isteka"], cert["vrijedi_do"]))
    if sat.upozorenje(con):
        upozorenja.append(sat.upozorenje(con))
    placanja = [{"id": r["id"], "naziv": r["naziv"], "nacin": nacin_za(con, r["id"], r["naziv"]),
                 "rucno": postavke.vrijednost(con, "fiskal.nacin.%d" % r["id"], "")}
                for r in con.execute("SELECT id, naziv FROM nacini_placanja WHERE aktivan = 1 ORDER BY roba_sifra, id")]
    neposlani = [dict(r) for r in con.execute(
        """SELECT f.racun_id, f.broj, f.prostor, f.uredjaj, f.okolina, f.status, f.pokusaja, f.zadnji, f.poruka,
                  f.ukupno, r.vrijeme FROM fiskal_racuni f JOIN racuni r ON r.id = f.racun_id
           WHERE f.status != 'poslan' ORDER BY f.racun_id""")]
    for n in neposlani:
        n["oznaka"] = oznaka(n)
    zadnji = [dict(r) for r in con.execute(
        """SELECT f.racun_id, f.broj, f.prostor, f.uredjaj, f.okolina, f.status, f.jir, f.zki, f.ukupno, r.vrijeme
           FROM fiskal_racuni f JOIN racuni r ON r.id = f.racun_id ORDER BY f.racun_id DESC LIMIT 15""")]
    for n in zadnji:
        n["oznaka"] = oznaka(n)
    return {"postavke": javno, "certifikat": cert, "upozorenja": upozorenja, "cryptography": ima_cryptography(),
            "url": URL[pst["okolina"]], "nacini": NACINI, "placanja": placanja,
            "neposlani": neposlani, "zadnji": zadnji, "ukljucen": postavke.ukljucen(con, "fiskalizacija"),
            "potpis_porezne": potpis_stanje(con), "mail_greska": postavke.vrijednost(con, "fiskal.mail_greska", "")}


def kontrola(con):
    """Provjere za karticu Kontrola i Danas (format kao kontrola._p)."""
    if not postavke.ukljucen(con, "fiskalizacija"):
        return []
    out = []
    rows = con.execute(
        """SELECT f.*, r.vrijeme FROM fiskal_racuni f JOIN racuni r ON r.id = f.racun_id
           WHERE f.status != 'poslan' ORDER BY f.racun_id""").fetchall()
    if rows:
        star = (dt.datetime.now() - dt.timedelta(minutes=TRAKA_MINUTA)).strftime("%Y-%m-%d %H:%M:%S")
        crveno = any(r["status"] == "greska" or r["vrijeme"] < star for r in rows)
        stavke = [[oznaka(r), r["vrijeme"][:16], ("%.2f" % (r["ukupno"] / 100)).replace(".", ","),
                   "TEST" if r["okolina"] == "test" else "", r["poruka"][:120]] for r in rows]
        out.append({"razina": "crveno" if crveno else "zuto", "naslov": "Fiskalni računi bez JIR-a",
                    "opis": "Ovi računi nisu potvrđeni u Poreznoj. Program ih sam šalje ponovno svake 2 minute; "
                            "kod greške ispravite postavke i kliknite Pošalji ponovno (Dodaci, Fiskalizacija).",
                    "ukupno": len(stavke), "stavke": stavke[:50], "stupci": ["Račun", "Vrijeme", "Iznos", "", "Greška"]})
    p = potpis_stanje(con)
    if p["stanje"] in ("neispravan", "upozorenje", "nije_provjeren"):
        out.append({"razina": "crveno" if p["stanje"] == "neispravan" else "zuto",
                    "naslov": "Potpis na odgovoru Porezne",
                    "opis": "Zadnja provjera (%s): %s%s" % (p["kada"][:16], p["opis"],
                            " Račun se šalje ponovno dok ne stigne ispravno potpisan odgovor."
                            if p["stanje"] == "neispravan" else " JIR je prihvaćen."),
                    "ukupno": 0, "stavke": [], "stupci": []})
    pst = sve_postavke(con)
    if pst["cert_putanja"]:
        c = podaci_certifikata(pst)
        if c.get("greska"):
            out.append({"razina": "crveno", "naslov": "Certifikat za fiskalizaciju", "opis": c["greska"],
                        "ukupno": 0, "stavke": [], "stupci": []})
        elif c["istekao"] or c["dana_do_isteka"] < 30:
            out.append({"razina": "crveno" if c["istekao"] or c["dana_do_isteka"] < 7 else "zuto",
                        "naslov": "Certifikat za fiskalizaciju ističe",
                        "opis": "Vrijedi do %s. Novi certifikat se naručuje kod FINA-e." % c["vrijedi_do"],
                        "ukupno": 0, "stavke": [], "stupci": []})
    return out


def bez_jir(con):
    """Računi koji još nemaju JIR: broj, najstariji i koliko minuta čeka. None ako ih nema."""
    r = con.execute(
        """SELECT COUNT(*) n, MIN(r.vrijeme) najstariji, MIN(f.racun_id) prvi,
                  SUM(f.status = 'greska') greske, SUM(f.okolina = 'test') test
           FROM fiskal_racuni f JOIN racuni r ON r.id = f.racun_id WHERE f.status != 'poslan'""").fetchone()
    if not r or not r["n"]:
        return None
    try:
        minuta = int((dt.datetime.now() - dt.datetime.strptime(r["najstariji"][:19], "%Y-%m-%d %H:%M:%S"))
                     .total_seconds() // 60)
    except (TypeError, ValueError):
        minuta = 0
    return {"broj": r["n"], "najstariji": r["najstariji"][:16], "prvi": r["prvi"], "minuta": max(minuta, 0),
            "greske": r["greske"] or 0, "samo_test": r["test"] == r["n"]}


def _racuna(n):
    return "%d %s" % (n, "račun" if n % 10 == 1 and n % 100 != 11 else "računa")


def _koliko(minuta):
    if minuta < 60:
        return "%d min" % minuta
    if minuta < 48 * 60:
        return "%d h %d min" % (minuta // 60, minuta % 60)
    return "%d dana" % (minuta // (24 * 60))


def traka(con):
    """Upozorenje za ekran kase: računi bez JIR-a stariji od sat vremena ili s greškom. None ako je sve u redu."""
    if not postavke.ukljucen(con, "fiskalizacija"):
        return None
    b = bez_jir(con)
    if not b or (b["minuta"] < TRAKA_MINUTA and not b["greske"]):
        return None
    d, v = b["najstariji"][:10], b["najstariji"][11:16]
    kada = "%s.%s.%s. %s" % (d[8:10], d[5:7], d[:4], v)
    tekst = ("%s bez JIR-a (nije potvrđeno u Poreznoj), najstariji od %s, čeka %s. "
             % (_racuna(b["broj"]), kada, _koliko(b["minuta"])))
    if b["greske"]:
        tekst += "Porezna je javila grešku: pogledajte Dodaci, Fiskalizacija i kliknite Pošalji ponovno. "
    else:
        tekst += "Provjerite internet; program ih sam šalje svake 2 minute. "
    tekst += "Kasno poslane račune Porezna kažnjava, a starije od 2 dana ne prima."
    if b["samo_test"]:
        tekst = "TEST: " + tekst
    return {"razina": "crveno", "tekst": tekst, "broj": b["broj"], "minuta": b["minuta"]}


def upozori_mailom(con):
    """Jednom po slučaju (i još jednom nakon 24 h) pošalje vlasniku e-mail kad računi predugo čekaju JIR.
    Koristi SMTP postavke e-mail sažetka; bez njih ne radi ništa."""
    try:
        import sazetak
        t = traka(con)
        if not t or not sazetak.smtp_upisan(con):
            return False
        za = postavke.vrijednost(con, "sazetak.za", "")
        if not za:
            return False
        b = bez_jir(con)
        kljuc = "%s:%s" % (b["prvi"], "24h" if b["minuta"] >= 24 * 60 else "1h")
        if postavke.vrijednost(con, "fiskal.mail_poslan", "") == kljuc:
            return False
        if time.time() - _sat["mail_pokusaj"] < MAIL_PONOVI:
            return False
        _sat["mail_pokusaj"] = time.time()
        from email.message import EmailMessage
        from email.utils import formatdate, make_msgid
        msg = EmailMessage()
        msg["Subject"] = "Kasa: %s bez JIR-a (čeka %s)" % (_racuna(b["broj"]), _koliko(b["minuta"]))
        msg["To"] = za
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        msg.set_content(t["tekst"] + "\n\nPopis računa je u programu: kartica Kontrola, \"Fiskalni računi bez "
                        "JIR-a\". Ova poruka stiže jednom (i još jednom ako računi čekaju dulje od 24 sata).\n")
        sazetak.posalji_poruku(con, msg)
        postavke.postavi(con, "fiskal.mail_poslan", kljuc)
        postavke.postavi(con, "fiskal.mail_greska", "")
        return True
    except Exception as e:
        try:
            postavke.postavi(con, "fiskal.mail_greska", "%s %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), e))
        except Exception:
            pass
        return False


def ispis_dio(f):
    """HTML fiskalnog dijela računa (80 mm)."""
    e = html.escape
    jir = e(f["jir"]) if f["jir"] else "naknadno (nema veze s Poreznom)"
    return ("<p>ZKI: <span class=sitno>%s</span><br>JIR: <span class=sitno>%s</span></p>"
            "<div class=c>%s</div>" % (e(f["zki"]), jir, f["qr"]))
