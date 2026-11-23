# -*- coding: utf-8 -*-
"""
eRačun preko informacijskog posrednika (modul "eracun_posrednik", zadano isključen).

Fiskalizacija 2.0: eRačuni između tvrtki idu isključivo preko informacijskog posrednika s popisa
Porezne. Posrednik šalje i prima (AS4, adresar AMS), sam fiskalizira eRačun i prijavljuje
naplatu i odbijanje (eIzvještavanje). Naš program samo razgovara s API-jem posrednika:

    primanje   ulazni eRačuni dobavljača -> pregled -> primka (postojeći uvoz iz eracun.py)
    odbijanje  ulazni eRačun koji ne prihvaćamo (razlog N/U/O), posrednik javlja Poreznoj
    slanje     zbirni račun iz "Kupci na tvrtku" kao UBL 2.1 eRačun (HR CIUS 2025)
    naplata    izdani eRačun označen plaćenim (datum, iznos, način T/O/Z) -> eIzvještavanje

Zajedničko sučelje je klasa Posrednik; po jedna izvedba za svakog od tri posrednika:

    mer          moj-eRačun (ELEKTRONIČKI RAČUNI d.o.o.): REST/JSON, korisnik + lozinka + SoftwareId.
                 Napisano po javnoj specifikaciji API v2 (manual.moj-eracun.hr).
    eposlovanje  ePoslovanje (Pondi d.o.o.): REST, API ključ. Dokumentacija API v2 (doc.eposlovanje.hr)
                 nije javno čitljiva, pa su putanje u EPOSLOVANJE_PUTANJE PRETPOSTAVKA: provjeriti na
                 testnoj okolini prije uporabe (tipka "Provjeri vezu").
    fina         FINA e-Račun: SOAP + WS-Security potpis FINA aplikacijskim certifikatom i dvosmjerni TLS.
                 Adresu servisa FINA daje kod ugovaranja (razmjena mrežnih postavki), pa se upisuje ručno.
                 Imenski prostori su iz FINA-inog primjera i treba ih potvrditi s WSDL-om koji daje FINA.

Kasa i fiskalizacija računa s kase (fiskal.py) se ovim modulom ne mijenjaju.
"""

import base64
import datetime as dt
import hashlib
import json
import os
import re
import ssl
import tempfile
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as _esc

import eracun
import fiskal
import postavke
from baza import oib_ispravan
from nabava import Greska, sada

# ---------------------------------------------------------------- postavke

POSREDNICI = (
    ("mer", "moj-eRačun"),
    ("eposlovanje", "ePoslovanje (Pondi)"),
    ("fina", "FINA e-Račun"),
)
ZADANO = {
    "posrednik": "mer",
    "okolina": "test",          # test | produkcija
    "korisnik": "",             # mer: e-mail ili ID korisnika
    "lozinka": "",              # mer (zaštićeno kao lozinka certifikata u fiskal.py)
    "software_id": "",          # mer: oznaka našeg programa koju dodjeljuje moj-eRačun
    "company_bu": "",           # mer: poslovna jedinica (nije obavezno)
    "api_kljuc": "",            # ePoslovanje (zaštićeno)
    "url": "",                  # FINA: adresa servisa (daje FINA); ePoslovanje: druga adresa API-ja ako treba
    "cert_putanja": "",         # FINA aplikacijski certifikat (.p12), nije isti kao fiskalni
    "cert_lozinka": "",
    "erp_id": "Trgovina",       # FINA ERPID
    "prostor": "",              # oznaka poslovnog prostora u broju eRačuna (zadano iz fiskalizacije)
    "uredjaj": "9",             # oznaka naplatnog uređaja za eRačune: drugačija od kase i Robe
    "proces": "P1",             # HR poslovni proces (P1 = isporuka robe i usluga)
    "mjesto": "",               # prodavatelj: mjesto i poštanski broj (za adresu na eRačunu)
    "postanski": "",
    "zadnja_provjera": "",
}
TAJNO = ("lozinka", "api_kljuc", "cert_lozinka")
NACINI_PLACANJA = (("T", "Transakcijski račun"), ("O", "Obračunsko plaćanje"), ("Z", "Ostalo"))
RAZLOZI_ODBIJANJA = (("N", "Neslaganje podataka bez utjecaja na porez"),
                     ("U", "Neslaganje podataka s utjecajem na porez"), ("O", "Ostalo"))
TIMEOUT = 30

SHEMA = """
CREATE TABLE IF NOT EXISTS eracun_ulazni (
    id         INTEGER PRIMARY KEY,
    posrednik  TEXT NOT NULL,
    okolina    TEXT NOT NULL,
    ext_id     TEXT NOT NULL,             -- oznaka dokumenta kod posrednika (ElectronicId, InvoiceID...)
    broj       TEXT NOT NULL DEFAULT '',
    oib        TEXT NOT NULL DEFAULT '',  -- dobavljač
    naziv      TEXT NOT NULL DEFAULT '',
    datum      TEXT NOT NULL DEFAULT '',
    iznos      INTEGER,                   -- centi, ako ga posrednik javi
    status     TEXT NOT NULL DEFAULT '',  -- kako ga javlja posrednik
    xml        TEXT,                      -- preuzeti eRačun (čuva se i kod nas)
    odbijen    TEXT NOT NULL DEFAULT '',  -- datum odbijanja (poslano posredniku)
    razlog     TEXT NOT NULL DEFAULT '',
    dohvacen   TEXT NOT NULL,
    UNIQUE (posrednik, okolina, ext_id)
);
CREATE TABLE IF NOT EXISTS eracun_izlazni (
    id             INTEGER PRIMARY KEY,
    okolina        TEXT NOT NULL,
    godina         INTEGER NOT NULL,
    broj           INTEGER NOT NULL,
    oznaka         TEXT NOT NULL,           -- broj-prostor-uređaj
    kupac_id       INTEGER NOT NULL,
    mjesec         TEXT NOT NULL DEFAULT '',
    datum          TEXT NOT NULL,
    vrijeme        TEXT NOT NULL,
    dospijece      TEXT NOT NULL DEFAULT '',
    iznos          INTEGER NOT NULL,        -- ukupno s PDV-om, centi
    narudzbe       TEXT NOT NULL DEFAULT '[]',
    xml            TEXT NOT NULL,
    posrednik      TEXT NOT NULL,
    ext_id         TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'priprema',  -- priprema | poslan | greska | <status posrednika>
    poruka         TEXT NOT NULL DEFAULT '',
    placeno_datum  TEXT NOT NULL DEFAULT '',
    placeno_iznos  INTEGER,
    placeno_nacin  TEXT NOT NULL DEFAULT '',
    radnik         TEXT NOT NULL DEFAULT '',
    UNIQUE (okolina, godina, broj)
);
"""


def pripremi(con):
    con.executescript(SHEMA)


def _v(con, k):
    return postavke.vrijednost(con, "eposrednik." + k, ZADANO[k])


def sve_postavke(con):
    return {k: _v(con, k) for k in ZADANO}


def za_prikaz(con):
    """Postavke za ekran: lozinke i ključevi se ne vraćaju, samo je li upisano."""
    p = sve_postavke(con)
    for k in TAJNO:
        p[k] = "upisano" if p[k] else ""
    f = fiskal.sve_postavke(con)
    p["prodavatelj"] = {"oib": f["oib"], "naziv": f["naziv_tvrtke"], "adresa": f["adresa"],
                        "prostor": f["prostor"], "u_pdv": f["u_pdv"]}
    p["posrednici"] = [{"kljuc": k, "naziv": n, "provjereno": IZVEDBE[k].provjereno} for k, n in POSREDNICI]
    p["nacini"] = NACINI_PLACANJA
    p["razlozi"] = RAZLOZI_ODBIJANJA
    return p


def spremi_postavke(con, p):
    stare = sve_postavke(con)
    nove = {}
    for k in ZADANO:
        if k not in p or k == "zadnja_provjera":
            continue
        v = str(p[k] if p[k] is not None else "").strip()
        if k in TAJNO:
            if v in ("", "upisano"):
                continue  # prazno polje = lozinka ostaje kakva je bila
            v = fiskal._zastiti(v)
        nove[k] = v[:500]
    if "posrednik" in nove and nove["posrednik"] not in IZVEDBE:
        raise Greska("Nepoznat posrednik.")
    if "okolina" in nove and nove["okolina"] not in ("test", "produkcija"):
        raise Greska("Okolina je test ili produkcija.")
    if nove.get("okolina") == "produkcija" and stare["okolina"] != "produkcija" and p.get("potvrda") != "PRODUKCIJA":
        raise Greska("Za pravu razmjenu eRačuna upišite riječ PRODUKCIJA. eRačuni u produkciji su pravi "
                     "računi koje posrednik fiskalizira.")
    if "uredjaj" in nove and not re.fullmatch(r"\d{1,4}", nove["uredjaj"] or ""):
        raise Greska("Oznaka naplatnog uređaja je broj (npr. 9).")
    if nove.get("prostor") and not re.fullmatch(r"[0-9A-Za-z]{1,20}", nove["prostor"]):
        raise Greska("Oznaka poslovnog prostora smije imati samo slova i brojke.")
    if "url" in nove and nove["url"] and not nove["url"].startswith("https://"):
        raise Greska("Adresa servisa mora počinjati s https://.")
    with con:
        for k, v in nove.items():
            postavke.postavi(con, "eposrednik." + k, v)
    return za_prikaz(con)


def spremi_certifikat(con, ime, sadrzaj_b64):
    """FINA aplikacijski certifikat (.p12) u mapu certifikati/ (pored fiskalnog)."""
    ime = os.path.basename(str(ime or "")).strip()
    if not ime.lower().endswith((".p12", ".pfx")):
        raise Greska("Odaberite .p12 ili .pfx datoteku certifikata.")
    try:
        podaci = base64.b64decode(sadrzaj_b64 or "", validate=True)
    except Exception:
        raise Greska("Datoteka certifikata nije ispravno poslana.")
    if not podaci or len(podaci) > 100_000:
        raise Greska("Datoteka certifikata je prazna ili prevelika.")
    os.makedirs(fiskal.CERT_MAPA, exist_ok=True)
    put = os.path.join(fiskal.CERT_MAPA, "eracun_" + re.sub(r"[^\w.\-]", "_", ime))
    with open(put, "wb") as f:
        f.write(podaci)
    postavke.postavi(con, "eposrednik.cert_putanja", put)
    con.commit()
    return put


def _prostor(con, pst):
    return pst["prostor"] or fiskal.sve_postavke(con)["prostor"]


# ---------------------------------------------------------------- zajedničko sučelje

class Posrednik:
    """Što svaki posrednik mora znati. Datumi su 'YYYY-MM-DD', iznosi u centima."""
    kljuc = ""
    naziv = ""
    provjereno = False   # True = napisano po javnoj specifikaciji; False = putanje treba potvrditi na testu

    def __init__(self, pst, oib):
        self.pst = pst
        self.oib = oib
        self.test = pst["okolina"] != "produkcija"

    def provjeri(self):
        """Prijava radi? Vraća kratku poruku ili baca Greska."""
        raise NotImplementedError

    def ulazni(self, od, do):
        """[{ext_id, broj, oib, naziv, datum, iznos, status}] ulaznih eRačuna u razdoblju."""
        raise NotImplementedError

    def preuzmi(self, ext_id):
        """XML ulaznog eRačuna (UBL 2.1)."""
        raise NotImplementedError

    def posalji(self, xml, oib_kupca, oznaka):
        """Šalje izlazni eRačun. Vraća {ext_id, status, poruka}."""
        raise NotImplementedError

    def stanje(self, ext_id, oznaka):
        """{status, poruka} izlaznog eRačuna (dostava i fiskalizacija)."""
        raise NotImplementedError

    def placeno(self, ext_id, datum, iznos, nacin, racun, godina=None):
        """Naplata izdanog eRačuna (eIzvještavanje)."""
        raise NotImplementedError

    def odbij(self, ext_id, datum, vrsta, opis, racun):
        """Odbijanje ulaznog eRačuna (eIzvještavanje)."""
        raise NotImplementedError

    def registriran(self, oib):
        """Je li primatelj u adresaru (AMS)? True/False, None ako posrednik to ne nudi."""
        return None


def _http(url, tijelo=None, zaglavlja=None, metoda="POST", context=None):
    """(status, tekst). Greška veze -> Greska s razumljivom porukom."""
    z = {"Accept": "application/json"}
    z.update(zaglavlja or {})
    podaci = None
    if tijelo is not None:
        podaci = tijelo if isinstance(tijelo, bytes) else json.dumps(tijelo, ensure_ascii=False).encode("utf-8")
        z.setdefault("Content-Type", "application/json; charset=utf-8")
    req = urllib.request.Request(url, data=podaci, method=metoda, headers=z)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=context) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except ssl.SSLError as e:
        raise Greska("Sigurna veza s posrednikom nije uspjela (%s)." % getattr(e, "reason", e))
    except (urllib.error.URLError, OSError) as e:
        raise Greska("Nema veze s posrednikom (%s). Provjerite internet." % getattr(e, "reason", e))


def _poruka_greske(tekst):
    """Tekst greške iz JSON odgovora (message, errors, Message...) ili početak teksta."""
    try:
        j = json.loads(tekst)
    except ValueError:
        return re.sub(r"<[^>]+>", " ", re.sub(r"\s+", " ", tekst or "")).strip()[:300] or "bez opisa"
    if isinstance(j, dict):
        if isinstance(j.get("errors"), dict):
            return "; ".join("%s: %s" % (k, " ".join(v) if isinstance(v, list) else v) for k, v in j["errors"].items())
        for k in ("message", "Message", "greska", "error", "title", "detail"):
            if j.get(k):
                return str(j[k])[:300]
    return str(j)[:300]


def _datum(x):
    return str(x or "")[:10]


def _centi(x):
    try:
        return int(round(float(x) * 100))
    except (TypeError, ValueError):
        return None


def _eur(c):
    return "%.2f" % (c / 100.0)


# ---------------------------------------------------------------- moj-eRačun

class MojERacun(Posrednik):
    """moj-eRačun API v2: https://manual.moj-eracun.hr/documentation/api-specification-2/"""
    kljuc = "mer"
    naziv = "moj-eRačun"
    provjereno = True
    PROD = "https://www.moj-eracun.hr"
    DEMO = "https://demo.moj-eracun.hr"

    def _baza(self):
        return self.pst["url"].rstrip("/") if self.pst["url"] else (self.DEMO if self.test else self.PROD)

    def _prijava(self):
        p = self.pst
        if not (p["korisnik"] and p["lozinka"] and p["software_id"]):
            raise Greska("Za moj-eRačun upišite korisnika, lozinku i SoftwareId (dobiva se od moj-eRačuna: "
                         "integracije@moj-eracun.hr).")
        d = {"Username": p["korisnik"], "Password": fiskal._otkrij(p["lozinka"]),
             "CompanyId": self.oib, "SoftwareId": p["software_id"]}
        if p["company_bu"]:
            d["CompanyBu"] = p["company_bu"]
        return d

    def _post(self, put, polja=None, ok_prazno=False):
        tijelo = self._prijava()
        tijelo.update(polja or {})
        status, tekst = _http(self._baza() + put, tijelo)
        if status >= 400:
            raise Greska("moj-eRačun (HTTP %d): %s" % (status, _poruka_greske(tekst)))
        if not tekst.strip():
            if ok_prazno:
                return None
            raise Greska("moj-eRačun je vratio prazan odgovor.")
        try:
            return json.loads(tekst)
        except ValueError:
            return tekst

    def provjeri(self):
        danas = dt.date.today().isoformat()
        r = self._post("/apis/v2/queryInbox", {"From": danas, "To": danas})
        n = len(r) if isinstance(r, list) else 0
        return "Prijava u moj-eRačun radi (%s okolina). Danas ulaznih: %d." % ("demo" if self.test else "prava", n)

    def ulazni(self, od, do):
        r = self._post("/apis/v2/queryInbox", {"From": od, "To": do})
        out = []
        for d in r if isinstance(r, list) else []:
            out.append({"ext_id": str(d.get("ElectronicId")), "broj": d.get("DocumentNr") or "",
                        "oib": re.sub(r"\D", "", str(d.get("SenderBusinessNumber") or ""))[-11:],
                        "naziv": d.get("SenderBusinessName") or "",
                        "datum": _datum(d.get("Sent") or d.get("Delivered") or d.get("Updated")),
                        "iznos": _centi(d.get("Amount")) if d.get("Amount") is not None else None,
                        "status": d.get("StatusName") or str(d.get("StatusId") or "")})
        return out

    def preuzmi(self, ext_id):
        r = self._post("/apis/v2/receive", {"ElectronicId": int(ext_id)})
        if isinstance(r, str):
            return r
        if isinstance(r, dict):  # za svaki slučaj: XML upakiran u JSON
            for k in ("File", "Document", "Xml", "xml"):
                if r.get(k):
                    return r[k]
        raise Greska("moj-eRačun nije vratio XML eRačuna.")

    def posalji(self, xml, oib_kupca, oznaka):
        r = self._post("/apis/v2/send", {"File": xml})
        if not isinstance(r, dict) or not r.get("ElectronicId"):
            raise Greska("moj-eRačun nije potvrdio slanje: %s" % str(r)[:200])
        return {"ext_id": str(r["ElectronicId"]), "status": r.get("StatusName") or "U obradi",
                "poruka": "Primatelj: %s" % (r.get("RecipientBusinessName") or "")}

    def stanje(self, ext_id, oznaka):
        r = self._post("/apis/v2/queryOutbox", {"ElectronicId": int(ext_id)})
        st = r[0] if isinstance(r, list) and r else {}
        status = st.get("StatusName") or str(st.get("StatusId") or "nepoznato")
        poruka = ""
        try:
            f = self._post("/api/fiscalization/statusOutbox", {"ElectronicId": int(ext_id), "MessageType": 0})
            for d in f if isinstance(f, list) else []:
                m = d.get("messages") or {}
                m = m[0] if isinstance(m, list) and m else m
                if m:
                    opis = {0: "uspjela", 1: "NIJE uspjela", 2: "čeka"}.get(m.get("status"), str(m.get("status")))
                    poruka = "Fiskalizacija %s%s" % (opis, (": %s %s" % (m.get("errorCode") or "",
                                                                         m.get("errorCodeDescription") or ""))
                                                     if m.get("status") == 1 else "")
        except Greska as e:
            poruka = "Stanje fiskalizacije nije dohvaćeno (%s)." % e
        return {"status": status, "poruka": poruka}

    def placeno(self, ext_id, datum, iznos, nacin, racun, godina=None):
        self._post("/api/fiscalization/markPaid", {"ElectronicId": int(ext_id), "PaymentDate": datum,
                                                    "PaymentAmount": float(_eur(iznos)), "PaymentMethod": nacin},
                   ok_prazno=True)

    def odbij(self, ext_id, datum, vrsta, opis, racun):
        self._post("/api/fiscalization/reject", {"ElectronicId": int(ext_id), "RejectionDate": datum,
                                                  "RejectionReasonType": vrsta, "RejectionReasonDescription": opis},
                   ok_prazno=True)

    def registriran(self, oib):
        tijelo = self._prijava()
        tijelo.update({"IdentifierType": "0", "Identifiervalue": oib})
        status, tekst = _http(self._baza() + "/api/mps/check", tijelo)
        if status == 200:
            return True
        if status in (400, 404):
            return False
        raise Greska("moj-eRačun (HTTP %d): %s" % (status, _poruka_greske(tekst)))


# ---------------------------------------------------------------- ePoslovanje

# PRETPOSTAVKA: dokumentacija API v2 (doc.eposlovanje.hr) se ne može pročitati bez preglednika/prijave.
# Poznato je: REST, API ključ, adresa https://eracun.eposlovanje.hr/apis/v2 (postavke za Pantheon u
# njihovim čestim pitanjima), testna okolina test.eposlovanje.hr. Putanje ispod treba potvrditi.
EPOSLOVANJE_PUTANJE = {
    "provjera": ("GET", "/document/incoming?from={od}&to={do}"),
    "ulazni": ("GET", "/document/incoming?from={od}&to={do}"),
    "preuzmi": ("GET", "/document/incoming/{id}/xml"),
    "posalji": ("POST", "/document/send"),
    "stanje": ("GET", "/document/outgoing/{id}"),
    "placeno": ("POST", "/document/outgoing/{id}/payment"),
    "odbij": ("POST", "/document/incoming/{id}/reject"),
    "registriran": ("GET", "/ams/{oib}"),
}


class EPoslovanje(Posrednik):
    kljuc = "eposlovanje"
    naziv = "ePoslovanje"
    provjereno = False
    PROD = "https://eracun.eposlovanje.hr/apis/v2"
    TEST = "https://test.eposlovanje.hr/apis/v2"

    def _zahtjev(self, sto, tijelo=None, **arg):
        if not self.pst["api_kljuc"]:
            raise Greska("Za ePoslovanje upišite API ključ (dobiva se nakon registracije na eposlovanje.hr; "
                         "za probu na test.eposlovanje.hr).")
        metoda, put = EPOSLOVANJE_PUTANJE[sto]
        baza = self.pst["url"].rstrip("/") if self.pst["url"] else (self.TEST if self.test else self.PROD)
        url = baza + put.format(**{k: urllib.request.quote(str(v)) for k, v in arg.items()})
        status, tekst = _http(url, tijelo, {"Authorization": fiskal._otkrij(self.pst["api_kljuc"])}, metoda)
        if status == 404 and sto in ("provjera",):
            raise Greska("ePoslovanje: adresa API-ja nije pronađena (HTTP 404). Putanje API-ja v2 nisu još "
                         "potvrđene po dokumentaciji; javite nam da ih uskladimo.")
        if status in (401, 403):
            raise Greska("ePoslovanje odbija API ključ (HTTP %d). Provjerite ključ i okolinu (test/produkcija)."
                         % status)
        if status >= 400:
            raise Greska("ePoslovanje (HTTP %d): %s" % (status, _poruka_greske(tekst)))
        try:
            return json.loads(tekst) if tekst.strip() else None
        except ValueError:
            return tekst

    def provjeri(self):
        danas = dt.date.today().isoformat()
        self._zahtjev("provjera", od=danas, do=danas)
        return "Prijava u ePoslovanje radi (%s okolina)." % ("testna" if self.test else "prava")

    @staticmethod
    def _polje(d, *imena):
        for i in imena:
            for k in (i, i[:1].upper() + i[1:]):
                if d.get(k) not in (None, ""):
                    return d[k]
        return ""

    def ulazni(self, od, do):
        r = self._zahtjev("ulazni", od=od, do=do)
        if isinstance(r, dict):
            r = r.get("documents") or r.get("items") or r.get("data") or []
        out = []
        for d in r or []:
            iznos = self._polje(d, "amount", "totalAmount", "payableAmount")
            out.append({"ext_id": str(self._polje(d, "id", "documentId", "electronicId")),
                        "broj": str(self._polje(d, "documentNumber", "number", "invoiceNumber")),
                        "oib": re.sub(r"\D", "", str(self._polje(d, "senderOib", "supplierOib", "senderId")))[-11:],
                        "naziv": str(self._polje(d, "senderName", "supplierName")),
                        "datum": _datum(self._polje(d, "issueDate", "date", "created")),
                        "iznos": _centi(iznos) if iznos != "" else None,
                        "status": str(self._polje(d, "status", "statusName"))})
        return out

    def preuzmi(self, ext_id):
        r = self._zahtjev("preuzmi", id=ext_id)
        if isinstance(r, dict):
            r = self._polje(r, "xml", "document", "content")
            if r and not r.lstrip().startswith("<"):
                r = base64.b64decode(r).decode("utf-8")
        if not r:
            raise Greska("ePoslovanje nije vratio XML eRačuna.")
        return r

    def posalji(self, xml, oib_kupca, oznaka):
        r = self._zahtjev("posalji", {"document": xml, "receiverOib": oib_kupca}) or {}
        ext = str(self._polje(r, "id", "documentId", "electronicId")) if isinstance(r, dict) else ""
        if not ext:
            raise Greska("ePoslovanje nije potvrdio slanje: %s" % str(r)[:200])
        return {"ext_id": ext, "status": str(self._polje(r, "status") or "U obradi"), "poruka": ""}

    def stanje(self, ext_id, oznaka):
        r = self._zahtjev("stanje", id=ext_id) or {}
        return {"status": str(self._polje(r, "status", "statusName") or "nepoznato"),
                "poruka": str(self._polje(r, "fiscalizationStatus", "message"))}

    def placeno(self, ext_id, datum, iznos, nacin, racun, godina=None):
        self._zahtjev("placeno", {"paymentDate": datum, "paymentAmount": float(_eur(iznos)), "paymentMethod": nacin},
                      id=ext_id)

    def odbij(self, ext_id, datum, vrsta, opis, racun):
        self._zahtjev("odbij", {"rejectionDate": datum, "reasonType": vrsta, "reasonDescription": opis}, id=ext_id)


# ---------------------------------------------------------------- FINA

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
DS = "http://www.w3.org/2000/09/xmldsig#"
X509 = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-x509-token-profile-1.0#X509v3"
B64 = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"
# Imenski prostori po FINA-inom primjeru (v0.1); potvrditi s WSDL-om iz FINA-ine dokumentacije.
FINA_KOMP = "http://fina.hr/eracun/b2b/invoicewebservicecomponents/v0.1"
FINA_OP = "http://fina.hr/eracun/b2b/%s/v0.1"
HR_SPEC = ("urn:cen.eu:en16931:2017#compliant#urn:mfin.gov.hr:cius-2025:1.0"
           "#conformant#urn:mfin.gov.hr:ext-2025:1.0")
FINA_RAZLOZI = {"N": "NOT_VAT_REASON", "U": "VAT_REASON", "O": "OTHER_REASON"}


class Fina(Posrednik):
    kljuc = "fina"
    naziv = "FINA e-Račun"
    provjereno = False

    def _cert(self):
        p = self.pst
        if not p["cert_putanja"]:
            raise Greska("Za FINA e-Račun odaberite FINA aplikacijski certifikat (.p12). Za test: demo certifikat.")
        return fiskal._ucitaj({"cert_putanja": p["cert_putanja"], "cert_lozinka": p["cert_lozinka"]})

    def _url(self):
        if not self.pst["url"]:
            raise Greska("Upišite adresu FINA servisa (FINA je daje kod ugovaranja, uz demo certifikat za test).")
        return self.pst["url"]

    def _ssl(self, key, cert):
        """Dvosmjerni TLS: predstavljamo se istim aplikacijskim certifikatom."""
        from cryptography.hazmat.primitives import serialization
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        fd, put = tempfile.mkstemp(suffix=".pem")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
                f.write(cert.public_bytes(serialization.Encoding.PEM))
            ctx.load_cert_chain(put)
        finally:
            try:
                os.remove(put)
            except OSError:
                pass
        if os.path.isdir(fiskal.CERT_MAPA):  # FINA demo CA i sl. u mapi certifikati
            for f in os.listdir(fiskal.CERT_MAPA):
                if f.lower().endswith((".pem", ".crt", ".cer")):
                    try:
                        ctx.load_verify_locations(cafile=os.path.join(fiskal.CERT_MAPA, f))
                    except (ssl.SSLError, OSError):
                        pass
        return ctx

    def _zaglavlje(self, kupac, vrsta):
        oznaka = "Buyer" if kupac else "Supplier"
        dodatno = "" if kupac else "<iwsc:ERPID>%s</iwsc:ERPID>" % _esc(self.pst["erp_id"] or "Trgovina")
        return ("<iwsc:Header%s><iwsc:MessageID>%s</iwsc:MessageID><iwsc:%sID>9934:%s</iwsc:%sID>%s"
                "<iwsc:MessageType>%d</iwsc:MessageType></iwsc:Header%s>"
                % (oznaka, uuid.uuid4(), oznaka, self.oib, oznaka, dodatno, vrsta, oznaka))

    def omotnica(self, operacija, sadrzaj, key, cert):
        """SOAP s WS-Security: potpisani Timestamp i Body (exc-c14n, RSA-SHA256, SHA-256).
        Oba elementa gradimo u kanonskom obliku, pa je sažetak izravno nad tim tekstom."""
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        sad = dt.datetime.now(dt.timezone.utc)
        ts = ('<wsu:Timestamp xmlns:wsu="%s" wsu:Id="TS-1"><wsu:Created>%s</wsu:Created><wsu:Expires>%s'
              '</wsu:Expires></wsu:Timestamp>' % (WSU, sad.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                 (sad + dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")))
        ns = FINA_OP % operacija
        body = ('<soapenv:Body xmlns:soapenv="%s" xmlns:wsu="%s" wsu:Id="Body-1">'
                '<ns:%sMsg xmlns:ns="%s">%s</ns:%sMsg></soapenv:Body>'
                % (SOAP_NS, WSU, operacija, ns, _iwsc_na_vrhu(sadrzaj), operacija))

        def ref(uri, tekst):
            d = base64.b64encode(hashlib.sha256(tekst.encode("utf-8")).digest()).decode()
            return ('<ds:Reference URI="#%s"><ds:Transforms><ds:Transform Algorithm="http://www.w3.org/2001/10/'
                    'xml-exc-c14n#"></ds:Transform></ds:Transforms><ds:DigestMethod Algorithm="http://www.w3.org/'
                    '2001/04/xmlenc#sha256"></ds:DigestMethod><ds:DigestValue>%s</ds:DigestValue></ds:Reference>'
                    % (uri, d))
        si = ('<ds:SignedInfo xmlns:ds="%s"><ds:CanonicalizationMethod Algorithm="http://www.w3.org/2001/10/'
              'xml-exc-c14n#"></ds:CanonicalizationMethod><ds:SignatureMethod Algorithm="http://www.w3.org/2001/04/'
              'xmldsig-more#rsa-sha256"></ds:SignatureMethod>%s%s</ds:SignedInfo>'
              % (DS, ref("TS-1", ts), ref("Body-1", body)))
        sv = base64.b64encode(key.sign(si.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())).decode()
        der = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
        sec = ('<wsse:Security xmlns:wsse="%s" xmlns:wsu="%s" soapenv:mustUnderstand="1">'
               '<wsse:BinarySecurityToken EncodingType="%s" ValueType="%s" wsu:Id="X509-1">%s</wsse:BinarySecurityToken>'
               '%s<ds:Signature xmlns:ds="%s">%s<ds:SignatureValue>%s</ds:SignatureValue><ds:KeyInfo>'
               '<wsse:SecurityTokenReference><wsse:Reference URI="#X509-1" ValueType="%s"></wsse:Reference>'
               '</wsse:SecurityTokenReference></ds:KeyInfo></ds:Signature></wsse:Security>'
               % (WSSE, WSU, B64, X509, der, ts, DS, si, sv, X509))
        return ('<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope xmlns:soapenv="%s"><soapenv:Header>%s'
                '</soapenv:Header>%s</soapenv:Envelope>' % (SOAP_NS, sec, body))

    def _poziv(self, operacija, sadrzaj):
        key, cert = self._cert()
        xml = self.omotnica(operacija, sadrzaj, key, cert)
        status, tekst = _http(self._url(), xml.encode("utf-8"),
                              {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": operacija,
                               "Accept": "text/xml"}, context=self._ssl(key, cert))
        if "Envelope" not in tekst:
            raise Greska("FINA (HTTP %d): %s" % (status, _poruka_greske(tekst)))
        try:
            root = ET.fromstring(tekst.encode("utf-8"))
        except ET.ParseError:
            raise Greska("FINA je vratila neispravan XML.")
        greska = root.find(".//{%s}Fault" % SOAP_NS)
        if greska is not None:
            raise Greska("FINA: %s" % " ".join(t.strip() for t in greska.itertext() if t.strip())[:300])
        ack = _lokalno(root, "AckStatusCode")
        if ack and ack != "10":
            raise Greska("FINA odbija poruku (%s %s)." % (ack, _lokalno(root, "AckStatusText")))
        kod = _lokalno(root, "ErrorCode")
        if kod:
            raise Greska("FINA: %s %s" % (kod, _lokalno(root, "ErrorMessage")))
        return root

    def provjeri(self):
        self._poziv("Echo", self._zaglavlje(False, 9999) + "<iwsc:Data><iwsc:EchoData><iwsc:Echo>Trgovina"
                    "</iwsc:Echo></iwsc:EchoData></iwsc:Data>")
        return "Veza s FINA e-Račun servisom radi (certifikat prihvaćen)."

    def ulazni(self, od, do):
        root = self._poziv("GetB2BIncomingInvoiceList", self._zaglavlje(True, 9101) +
                           "<iwsc:Data><iwsc:B2BIncomingInvoiceList><iwsc:Filter><iwsc:DateRange><iwsc:From>%s"
                           "</iwsc:From><iwsc:To>%s</iwsc:To></iwsc:DateRange></iwsc:Filter>"
                           "</iwsc:B2BIncomingInvoiceList></iwsc:Data>" % (od, do))
        out = []
        for el in root.iter():
            if el.tag.split("}")[-1] != "IncomingInvoice" and el.tag.split("}")[-1] != "B2BIncomingInvoice":
                continue
            ext = _lokalno(el, "InvoiceID")
            if not ext:
                continue
            out.append({"ext_id": ext, "broj": _lokalno(el, "SupplierInvoiceID") or ext,
                        "oib": re.sub(r"\D", "", _lokalno(el, "SupplierID"))[-11:],
                        "naziv": _lokalno(el, "SupplierName") or _lokalno(el, "SupplierCompanyName"),
                        "datum": _datum(_lokalno(el, "InvoiceTimestamp") or _lokalno(el, "IssueDate")),
                        "iznos": _centi(_lokalno(el, "Amount") or _lokalno(el, "TotalAmount")) if (
                            _lokalno(el, "Amount") or _lokalno(el, "TotalAmount")) else None,
                        "status": _lokalno(el, "StatusCode")})
        return out

    def preuzmi(self, ext_id):
        root = self._poziv("GetB2BIncomingInvoice", self._zaglavlje(True, 9103) +
                           "<iwsc:Data><iwsc:B2BIncomingInvoice><iwsc:InvoiceID>%s</iwsc:InvoiceID>"
                           "</iwsc:B2BIncomingInvoice></iwsc:Data>" % _esc(str(ext_id)))
        b = _lokalno(root, "InvoiceEnvelope")
        if not b:
            raise Greska("FINA nije vratila XML eRačuna (možda je odobrenje ili samo PDF).")
        return base64.b64decode(b).decode("utf-8") if not b.lstrip().startswith("<") else b

    def posalji(self, xml, oib_kupca, oznaka):
        root = self._poziv("SendB2BOutgoingInvoice", self._zaglavlje(False, 9001) +
                           "<iwsc:Data><iwsc:B2BOutgoingInvoiceEnvelope><iwsc:XMLStandard>UBL</iwsc:XMLStandard>"
                           "<iwsc:SpecificationIdentifier>%s</iwsc:SpecificationIdentifier>"
                           "<iwsc:SupplierInvoiceID>%s</iwsc:SupplierInvoiceID><iwsc:BuyerID>9934:%s</iwsc:BuyerID>"
                           "<iwsc:InvoiceEnvelope>%s</iwsc:InvoiceEnvelope></iwsc:B2BOutgoingInvoiceEnvelope>"
                           "</iwsc:Data>" % (_esc(HR_SPEC), _esc(oznaka), oib_kupca,
                                             base64.b64encode(xml.encode("utf-8")).decode()))
        ext = _lokalno(root, "InvoiceID")
        if not ext:
            raise Greska("FINA nije potvrdila slanje.")
        return {"ext_id": ext, "status": "Poslan", "poruka": _lokalno(root, "InvoiceTimestamp")}

    def stanje(self, ext_id, oznaka):
        root = self._poziv("GetB2BOutgoingInvoiceStatus", self._zaglavlje(False, 9011) +
                           "<iwsc:Data><iwsc:B2BOutgoingInvoiceStatus><iwsc:SupplierInvoiceID>%s"
                           "</iwsc:SupplierInvoiceID><iwsc:InvoiceYear>%s</iwsc:InvoiceYear>"
                           "</iwsc:B2BOutgoingInvoiceStatus></iwsc:Data>" % (_esc(oznaka), dt.date.today().year))
        return {"status": _lokalno(root, "StatusCode") or "nepoznato", "poruka": _lokalno(root, "StatusText")}

    def placeno(self, ext_id, datum, iznos, nacin, racun, godina=None):
        self._poziv("ChangeB2BOutgoingInvoiceStatus", self._zaglavlje(False, 109) +
                    "<iwsc:Data><iwsc:B2BOutgoingInvoiceStatus><iwsc:InvoiceID>%s</iwsc:InvoiceID>"
                    "<iwsc:SupplierInvoiceID>%s</iwsc:SupplierInvoiceID><iwsc:InvoiceYear>%s</iwsc:InvoiceYear>"
                    "<iwsc:InvoiceStatus><iwsc:StatusCode>PAYMENT_RECEIVED</iwsc:StatusCode>"
                    "<iwsc:PayedDate>%s</iwsc:PayedDate><iwsc:PayedAmount>%s</iwsc:PayedAmount>"
                    "<iwsc:PaymentMeansCode>%s</iwsc:PaymentMeansCode></iwsc:InvoiceStatus>"
                    "</iwsc:B2BOutgoingInvoiceStatus></iwsc:Data>"
                    % (_esc(ext_id), _esc(racun), godina or datum[:4], datum, _eur(iznos), nacin))

    def odbij(self, ext_id, datum, vrsta, opis, racun):
        self._poziv("ChangeB2BIncomingInvoiceStatus", self._zaglavlje(True, 107) +
                    "<iwsc:Data><iwsc:B2BIncomingInvoiceStatus><iwsc:InvoiceID>%s</iwsc:InvoiceID>"
                    "<iwsc:InvoiceStatus><iwsc:StatusCode>REJECTED</iwsc:StatusCode><iwsc:CodeReason>%s"
                    "</iwsc:CodeReason><iwsc:Note>%s</iwsc:Note></iwsc:InvoiceStatus>"
                    "</iwsc:B2BIncomingInvoiceStatus></iwsc:Data>"
                    % (_esc(ext_id), FINA_RAZLOZI[vrsta], _esc(opis[:500])))


def _iwsc_na_vrhu(sadrzaj):
    """exc-c14n stavlja xmlns:iwsc na svaki najviši iwsc element (tamo se prvi put koristi), pa ga
    tako i pišemo: onda je naš tekst već kanonski i sažetak je točan."""
    out, dubina, poz = [], 0, 0
    for m in re.finditer(r"<(/?)([\w:]+)>", sadrzaj):
        out.append(sadrzaj[poz:m.start()])
        if m.group(1):
            dubina -= 1
            out.append(m.group(0))
        else:
            out.append('<%s xmlns:iwsc="%s">' % (m.group(2), FINA_KOMP) if dubina == 0 else m.group(0))
            dubina += 1
        poz = m.end()
    out.append(sadrzaj[poz:])
    return "".join(out)


def _lokalno(el, ime):
    """Tekst prvog potomka s tim lokalnim imenom (bez obzira na imenski prostor)."""
    for x in el.iter():
        if x.tag.split("}")[-1] == ime and x.text and x.text.strip():
            return x.text.strip()
    return ""


IZVEDBE = {"mer": MojERacun, "eposlovanje": EPoslovanje, "fina": Fina}


def izvedba(con):
    pst = sve_postavke(con)
    oib = fiskal.sve_postavke(con)["oib"]
    if not oib_ispravan(oib):
        raise Greska("Upišite OIB trgovine (Dodaci, Fiskalizacija: OIB i naziv tvrtke). Posredniku se "
                     "predstavljamo tim OIB-om.")
    return IZVEDBE[pst["posrednik"]](pst, oib), pst


# ---------------------------------------------------------------- radnje: provjera i ulazni

def provjeri(con):
    p, pst = izvedba(con)
    poruka = p.provjeri()
    postavke.postavi(con, "eposrednik.zadnja_provjera", sada())
    con.commit()
    if not p.provjereno:
        poruka += (" Napomena: veza s ovim posrednikom napisana je bez javne dokumentacije, pa prije prave "
                   "uporabe probajte primanje i slanje na testnoj okolini.")
    return {"poruka": poruka}


def dohvati_ulazne(con, od=None, do=None):
    """Popis ulaznih eRačuna s posrednika (zadnjih 30 dana) upiše se kod nas; XML se preuzme odmah."""
    p, pst = izvedba(con)
    do = do or dt.date.today().isoformat()
    od = od or (dt.date.fromisoformat(do) - dt.timedelta(days=30)).isoformat()
    for x in (od, do):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", x):
            raise Greska("Neispravan datum.")
    novi = 0
    greske = []
    for d in p.ulazni(od, do):
        if not d["ext_id"]:
            continue
        r = con.execute("SELECT id, xml FROM eracun_ulazni WHERE posrednik = ? AND okolina = ? AND ext_id = ?",
                        (pst["posrednik"], pst["okolina"], d["ext_id"])).fetchone()
        if r is None:
            con.execute("""INSERT INTO eracun_ulazni (posrednik, okolina, ext_id, broj, oib, naziv, datum, iznos,
                           status, dohvacen) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (pst["posrednik"], pst["okolina"], d["ext_id"], d["broj"][:100], d["oib"], d["naziv"][:200],
                         d["datum"], d["iznos"], d["status"][:60], sada()))
            novi += 1
        else:
            con.execute("UPDATE eracun_ulazni SET status = ? WHERE id = ?", (d["status"][:60], r["id"]))
        con.commit()
    for r in con.execute("SELECT id, ext_id FROM eracun_ulazni WHERE posrednik = ? AND okolina = ? AND xml IS NULL "
                         "LIMIT 50", (pst["posrednik"], pst["okolina"])).fetchall():
        try:
            _spremi_xml(con, r["id"], p.preuzmi(r["ext_id"]))
        except Greska as e:
            greske.append(str(e))
    return {"novih": novi, "greske": greske[:5], "ulazni": ulazni(con)}


def _spremi_xml(con, uid, xml):
    if not xml or len(xml) > 5_000_000:
        raise Greska("eRačun je prazan ili prevelik.")
    sazetak = {}
    try:
        e = eracun.procitaj(con, xml)
        sazetak = {"broj": e["broj"], "datum": e["datum"], "oib": e["dobavljac"]["oib"],
                   "naziv": e["dobavljac"]["naziv"], "iznos": int(round(e["ukupno"] * 100))}
    except Greska:
        pass  # npr. odobrenje (CreditNote): XML se čuva, primka se radi ručno
    with con:
        con.execute("UPDATE eracun_ulazni SET xml = ? WHERE id = ?", (xml, uid))
        for k, v in sazetak.items():
            if v not in ("", None):
                con.execute("UPDATE eracun_ulazni SET %s = ? WHERE id = ?" % k, (v, uid))


def ulazni(con, granica=200):
    pst = sve_postavke(con)
    out = []
    for r in con.execute("""SELECT id, posrednik, ext_id, broj, oib, naziv, datum, iznos, status, odbijen, razlog,
                                   xml IS NOT NULL AS ima_xml
                            FROM eracun_ulazni WHERE okolina = ? ORDER BY datum DESC, id DESC LIMIT ?""",
                         (pst["okolina"], granica)):
        d = dict(r)
        d["primka"] = ulazni_primka(con, d)
        out.append(d)
    return out


def u_primku(con, uid):
    """Prijedlog primke iz preuzetog eRačuna (isti kao kod ručnog učitavanja XML-a)."""
    r = con.execute("SELECT * FROM eracun_ulazni WHERE id = ?", (uid,)).fetchone()
    if not r:
        raise Greska("eRačun ne postoji.")
    if not r["xml"]:
        raise Greska("eRačun još nije preuzet s posrednika. Kliknite Dohvati.")
    if r["odbijen"]:
        raise Greska("Ovaj eRačun je odbijen.")
    return eracun.procitaj(con, r["xml"])


def xml_ulaznog(con, uid):
    r = con.execute("SELECT xml, broj FROM eracun_ulazni WHERE id = ?", (uid,)).fetchone()
    if not r or not r["xml"]:
        raise Greska("eRačun nije preuzet.")
    return r["xml"], r["broj"]


def odbij(con, uid, p):
    vrsta = str(p.get("vrsta") or "")
    opis = str(p.get("opis") or "").strip()
    if vrsta not in dict(RAZLOZI_ODBIJANJA):
        raise Greska("Odaberite razlog odbijanja.")
    if len(opis) < 3:
        raise Greska("Upišite kratko zašto se eRačun odbija (vidi ga dobavljač i Porezna).")
    r = con.execute("SELECT * FROM eracun_ulazni WHERE id = ?", (uid,)).fetchone()
    if not r:
        raise Greska("eRačun ne postoji.")
    if r["odbijen"]:
        raise Greska("eRačun je već odbijen.")
    if ulazni_primka(con, r):
        raise Greska("Za ovaj eRačun već postoji primka %s. Prvo je stornirajte u Nabavi." % ulazni_primka(con, r))
    pz, pst = izvedba(con)
    if r["posrednik"] != pst["posrednik"] or r["okolina"] != pst["okolina"]:
        raise Greska("eRačun je stigao preko drugog posrednika ili okoline.")
    danas = dt.date.today().isoformat()
    pz.odbij(r["ext_id"], danas, vrsta, opis, r["broj"])
    with con:
        con.execute("UPDATE eracun_ulazni SET odbijen = ?, razlog = ? WHERE id = ?",
                    (danas, "%s: %s" % (vrsta, opis[:300]), uid))
    return {"poruka": "eRačun %s je odbijen. Posrednik to javlja Poreznoj (rok: do 20. u sljedećem mjesecu)."
                      % r["broj"]}


def ulazni_primka(con, r):
    p = con.execute("""SELECT d.godina, d.broj FROM dokumenti d JOIN partneri p ON p.id = d.partner_id
                       WHERE d.vrsta = 'primka' AND d.storniran = 0 AND d.storno_od IS NULL
                         AND p.oib = ? AND p.oib != '' AND d.broj_dobavljaca = ? LIMIT 1""",
                    (r["oib"], r["broj"])).fetchone()
    return "P-%d-%d" % (p[0], p[1]) if p else ""


# ---------------------------------------------------------------- izlazni eRačun (UBL 2.1, HR CIUS 2025)

UBL = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
CAC = eracun.NS["cac"]
CBC = eracun.NS["cbc"]
JM_UBL = {"kg": "KGM", "g": "GRM", "l": "LTR", "kom": "H87", "m": "MTR", "pak": "XPK", "kut": "XBX",
          "gajba": "XCR"}


def _netto(bruto_c, stopa):
    return int(round(bruto_c / (1 + stopa / 100.0)))


def pripremi_racun(con, kid, mjesec, radnik=""):
    """Iz zbirnog pregleda kupca (Kupci na tvrtku) složi eRačun. Ne šalje se još: vraća pregled."""
    import kupci_tvrtke
    pst = sve_postavke(con)
    f = fiskal.sve_postavke(con)
    z = kupci_tvrtke.zbirni(con, kid, mjesec)
    k = z["kupac"]
    if not z["bez_racuna"]:
        raise Greska("Za taj mjesec nema isporučenih narudžbi bez računa.")
    if not oib_ispravan(k.get("oib") or ""):
        raise Greska("Kupac %s nema ispravan OIB. eRačun se šalje na OIB." % k["naziv"])
    if not oib_ispravan(f["oib"]) or not f["naziv_tvrtke"] or not f["adresa"]:
        raise Greska("Upišite OIB, naziv i adresu trgovine (Dodaci, Fiskalizacija).")
    if f["u_pdv"] != "1":
        raise Greska("Slanje eRačuna za trgovinu izvan sustava PDV-a još nije podržano (oznake oslobođenja).")
    prostor = _prostor(con, pst)
    if not prostor:
        raise Greska("Upišite oznaku poslovnog prostora (postavke eRačuna ili fiskalizacije).")
    iban = postavke.vrijednost(con, "uplata.iban", "")
    bez_kpd = []
    stavke = []
    for s in z["stavke"]:
        a = con.execute("SELECT kpd, barkod FROM artikli WHERE id = ?", (s["artikl_id"],)).fetchone()
        kpd = (a["kpd"] if a else "") or ""
        if not eracun.kpd_ispravan(kpd):
            bez_kpd.append(s["naziv"])
        stavke.append(dict(s, kpd=kpd, barkod=(a["barkod"] if a else "") or ""))
    if bez_kpd:
        raise Greska("Artikli bez ispravne KPD šifre (6 znamenki, npr. 01.13.12): %s. Upišite je na artiklu."
                     % ", ".join(bez_kpd[:10]))
    sad = dt.datetime.now()
    godina = sad.year
    n = con.execute("SELECT COALESCE(MAX(broj), 0) + 1 FROM eracun_izlazni WHERE okolina = ? AND godina = ?",
                    (pst["okolina"], godina)).fetchone()[0]
    oznaka = "%d-%s-%s" % (n, prostor, pst["uredjaj"])
    rok = int(k.get("rok_placanja") or 0) or 30
    dospijece = (sad.date() + dt.timedelta(days=min(rok, 60))).isoformat()
    xml, ukupno, napomene = ubl_racun(
        oznaka=oznaka, datum=sad.date().isoformat(), vrijeme=sad.strftime("%H:%M:%S"), dospijece=dospijece,
        proces=pst["proces"] or "P1",
        prodavatelj={"oib": f["oib"], "naziv": f["naziv_tvrtke"], "adresa": f["adresa"], "mjesto": pst["mjesto"],
                     "postanski": pst["postanski"], "operater": f["operater"] or radnik or "Prodavač",
                     "oib_operatera": f["oib_operatera"] or f["oib"], "iban": iban},
        kupac={"oib": k["oib"], "naziv": k["naziv"], "adresa": k.get("adresa") or "", "mjesto": k.get("mjesto") or ""},
        stavke=stavke, napomena="Zbirni račun za isporuke %s (%s)" % (
            mjesec, ", ".join(nz["oznaka"] for nz in z["bez_racuna"])[:400]))
    if ukupno != z["ukupno"]:
        napomene.append("Ukupno na eRačunu (%s €) razlikuje se od zbirnog pregleda (%s €) zbog zaokruživanja PDV-a "
                        "po stopi (EN 16931)." % (_eur(ukupno).replace(".", ","), _eur(z["ukupno"]).replace(".", ",")))
    return {"oznaka": oznaka, "godina": godina, "broj": n, "datum": sad.date().isoformat(),
            "vrijeme": sad.strftime("%H:%M:%S"), "dospijece": dospijece, "ukupno": ukupno, "xml": xml,
            "kupac": {"id": k["id"], "naziv": k["naziv"], "oib": k["oib"]}, "mjesec": mjesec,
            "narudzbe": [nz["id"] for nz in z["bez_racuna"]], "napomene": napomene,
            "okolina": pst["okolina"], "posrednik": pst["posrednik"]}


def ubl_racun(oznaka, datum, vrijeme, dospijece, proces, prodavatelj, kupac, stavke, napomena=""):
    """UBL 2.1 Invoice po EN 16931 s hrvatskim proširenjem (CIUS 2025). Stavke: cijena i iznos s PDV-om
    u centima (kao u narudžbama); eRačun ima cijene bez PDV-a, PDV po stopi na zbroj osnovica."""
    ET.register_namespace("", UBL)
    ET.register_namespace("cac", CAC)
    ET.register_namespace("cbc", CBC)
    napomene = []

    def el(roditelj, ime, tekst=None, **atr):
        ns = CBC if ime.startswith("cbc:") else CAC
        x = ET.SubElement(roditelj, "{%s}%s" % (ns, ime.split(":")[1]), {k: v for k, v in atr.items()})
        if tekst is not None:
            x.text = str(tekst)
        return x

    def iznos(roditelj, ime, c):
        return el(roditelj, ime, _eur(c), currencyID="EUR")

    def stranka(roditelj, s, prodaje):
        party = el(roditelj, "cac:Party")
        el(party, "cbc:EndpointID", s["oib"], schemeID="9934")
        adr = el(party, "cac:PostalAddress")
        if s.get("adresa"):
            el(adr, "cbc:StreetName", s["adresa"])
        if s.get("mjesto"):
            el(adr, "cbc:CityName", s["mjesto"])
        if s.get("postanski"):
            el(adr, "cbc:PostalZone", s["postanski"])
        el(el(adr, "cac:Country"), "cbc:IdentificationCode", "HR")
        pts = el(party, "cac:PartyTaxScheme")
        el(pts, "cbc:CompanyID", "HR" + s["oib"])
        el(el(pts, "cac:TaxScheme"), "cbc:ID", "VAT")
        ple = el(party, "cac:PartyLegalEntity")
        el(ple, "cbc:RegistrationName", s["naziv"])
        el(ple, "cbc:CompanyID", s["oib"])
        return party

    root = ET.Element("{%s}Invoice" % UBL)
    el(root, "cbc:CustomizationID", HR_SPEC)
    el(root, "cbc:ProfileID", proces)
    el(root, "cbc:ID", oznaka)
    el(root, "cbc:IssueDate", datum)
    el(root, "cbc:IssueTime", vrijeme)
    el(root, "cbc:DueDate", dospijece)
    el(root, "cbc:InvoiceTypeCode", "380")
    if napomena:
        el(root, "cbc:Note", napomena)
    el(root, "cbc:DocumentCurrencyCode", "EUR")
    sup = el(root, "cac:AccountingSupplierParty")
    stranka(sup, prodavatelj, True)
    kontakt = el(sup, "cac:SellerContact")  # HR: oznaka i OIB operatera
    el(kontakt, "cbc:ID", prodavatelj["oib_operatera"])
    el(kontakt, "cbc:Name", prodavatelj["operater"])
    stranka(el(root, "cac:AccountingCustomerParty"), kupac, False)
    pm = el(root, "cac:PaymentMeans")
    el(pm, "cbc:PaymentMeansCode", "30")
    el(pm, "cbc:PaymentID", "HR99")
    if prodavatelj.get("iban"):
        el(el(pm, "cac:PayeeFinancialAccount"), "cbc:ID", prodavatelj["iban"])
    else:
        napomene.append("Nema IBAN-a trgovine (Postavke, Barkod za plaćanje): kupac neće vidjeti račun za uplatu.")

    linije, po_stopi = [], {}
    for i, s in enumerate(stavke, 1):
        stopa = float(s["pdv_stopa"])
        neto = _netto(int(s["iznos"]), stopa)
        po_stopi[stopa] = po_stopi.get(stopa, 0) + neto
        linije.append((i, s, stopa, neto))
    zbroj_neto = sum(po_stopi.values())
    pdv = {st: int(round(o * st / 100.0)) for st, o in po_stopi.items()}
    zbroj_pdv = sum(pdv.values())
    tt = el(root, "cac:TaxTotal")
    iznos(tt, "cbc:TaxAmount", zbroj_pdv)
    for st in sorted(po_stopi):
        sub = el(tt, "cac:TaxSubtotal")
        iznos(sub, "cbc:TaxableAmount", po_stopi[st])
        iznos(sub, "cbc:TaxAmount", pdv[st])
        cat = el(sub, "cac:TaxCategory")
        el(cat, "cbc:ID", "S" if st > 0 else "Z")
        el(cat, "cbc:Percent", ("%g" % st))
        el(el(cat, "cac:TaxScheme"), "cbc:ID", "VAT")
    lmt = el(root, "cac:LegalMonetaryTotal")
    iznos(lmt, "cbc:LineExtensionAmount", zbroj_neto)
    iznos(lmt, "cbc:TaxExclusiveAmount", zbroj_neto)
    iznos(lmt, "cbc:TaxInclusiveAmount", zbroj_neto + zbroj_pdv)
    iznos(lmt, "cbc:PayableAmount", zbroj_neto + zbroj_pdv)
    for i, s, stopa, neto in linije:
        ln = el(root, "cac:InvoiceLine")
        el(ln, "cbc:ID", i)
        kol = float(s["kolicina"])
        el(ln, "cbc:InvoicedQuantity", ("%.3f" % kol).rstrip("0").rstrip("."), unitCode=JM_UBL.get(str(s["jm"]).lower(), "H87"))
        iznos(ln, "cbc:LineExtensionAmount", neto)
        item = el(ln, "cac:Item")
        el(item, "cbc:Name", s["naziv"])
        if s.get("barkod") and re.fullmatch(r"\d{8}|\d{12,14}", s["barkod"]):
            el(el(item, "cac:StandardItemIdentification"), "cbc:ID", s["barkod"], schemeID="0160")
        cc = el(item, "cac:CommodityClassification")
        el(cc, "cbc:ItemClassificationCode", s["kpd"], listID="CG")
        cat = el(item, "cac:ClassifiedTaxCategory")
        el(cat, "cbc:ID", "S" if stopa > 0 else "Z")
        el(cat, "cbc:Percent", ("%g" % stopa))
        el(el(cat, "cac:TaxScheme"), "cbc:ID", "VAT")
        cijena = el(ln, "cac:Price")
        jed = neto / kol / 100.0 if kol else 0
        el(cijena, "cbc:PriceAmount", ("%.6f" % jed).rstrip("0").rstrip(".") or "0", currencyID="EUR")
    xml = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml, zbroj_neto + zbroj_pdv, napomene


def posalji_racun(con, kid, mjesec, radnik=""):
    """Složi, spremi i pošalje eRačun; narudžbama upiše broj eRačuna (više ne ulaze u zbirni)."""
    pz, pst = izvedba(con)
    r = pripremi_racun(con, kid, mjesec, radnik)
    k = r["kupac"]
    reg = None
    try:
        reg = pz.registriran(k["oib"])
    except Greska:
        pass
    if reg is False:
        raise Greska("Kupac %s (OIB %s) nije u adresaru eRačuna (AMS). eRačun mu se ne može dostaviti; "
                     "provjerite OIB ili izdajte račun na drugi način." % (k["naziv"], k["oib"]))
    with con:
        iid = con.execute(
            """INSERT INTO eracun_izlazni (okolina, godina, broj, oznaka, kupac_id, mjesec, datum, vrijeme, dospijece,
               iznos, narudzbe, xml, posrednik, radnik) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pst["okolina"], r["godina"], r["broj"], r["oznaka"], k["id"], mjesec, r["datum"], r["vrijeme"],
             r["dospijece"], r["ukupno"], json.dumps(r["narudzbe"]), r["xml"], pst["posrednik"], radnik)).lastrowid
    if pst["okolina"] == "produkcija":  # narudžbe dobiju broj eRačuna i više ne ulaze u zbirni pregled
        import kupci_tvrtke
        try:
            kupci_tvrtke.upisi_racun(con, k["id"], {"racun_roba": "eRačun " + r["oznaka"], "narudzbe": r["narudzbe"]})
        except Exception:
            with con:
                con.execute("DELETE FROM eracun_izlazni WHERE id = ?", (iid,))
            raise
    try:
        o = pz.posalji(r["xml"], k["oib"], r["oznaka"])
    except Greska as e:
        with con:
            con.execute("UPDATE eracun_izlazni SET status = 'greska', poruka = ? WHERE id = ?", (str(e)[:500], iid))
        raise Greska("eRačun %s je spremljen, ali slanje nije uspjelo: %s Pokušajte ponovno (Pošalji ponovno)."
                     % (r["oznaka"], e))
    with con:
        con.execute("UPDATE eracun_izlazni SET ext_id = ?, status = ?, poruka = ? WHERE id = ?",
                    (o["ext_id"], (o["status"] or "poslan")[:60], (o["poruka"] or "")[:500], iid))
    return {"id": iid, "oznaka": r["oznaka"], "status": o["status"], "napomene": r["napomene"],
            "poruka": "eRačun %s poslan preko posrednika %s%s." % (
                r["oznaka"], IZVEDBE[pst["posrednik"]].naziv,
                "" if pst["okolina"] == "produkcija" else " (TEST: narudžbe ostaju bez računa)")}


def posalji_ponovno(con, iid):
    pz, pst = izvedba(con)
    r = con.execute("SELECT i.*, k.oib FROM eracun_izlazni i JOIN kupci k ON k.id = i.kupac_id WHERE i.id = ?",
                    (iid,)).fetchone()
    if not r:
        raise Greska("eRačun ne postoji.")
    if r["ext_id"]:
        raise Greska("eRačun je već poslan.")
    if r["okolina"] != pst["okolina"] or r["posrednik"] != pst["posrednik"]:
        raise Greska("eRačun je pripremljen za drugog posrednika ili okolinu.")
    try:
        o = pz.posalji(r["xml"], r["oib"], r["oznaka"])
    except Greska as e:
        with con:
            con.execute("UPDATE eracun_izlazni SET status = 'greska', poruka = ? WHERE id = ?", (str(e)[:500], iid))
        raise
    with con:
        con.execute("UPDATE eracun_izlazni SET ext_id = ?, status = ?, poruka = ? WHERE id = ?",
                    (o["ext_id"], (o["status"] or "poslan")[:60], (o["poruka"] or "")[:500], iid))
    return {"poruka": "eRačun %s poslan." % r["oznaka"]}


def osvjezi(con, iid):
    pz, pst = izvedba(con)
    r = con.execute("SELECT * FROM eracun_izlazni WHERE id = ?", (iid,)).fetchone()
    if not r or not r["ext_id"]:
        raise Greska("eRačun nije poslan.")
    s = pz.stanje(r["ext_id"], r["oznaka"])
    with con:
        con.execute("UPDATE eracun_izlazni SET status = ?, poruka = ? WHERE id = ?",
                    (s["status"][:60], (s["poruka"] or "")[:500], iid))
    return s


def placeno(con, iid, p):
    r = con.execute("SELECT * FROM eracun_izlazni WHERE id = ?", (iid,)).fetchone()
    if not r or not r["ext_id"]:
        raise Greska("eRačun nije poslan.")
    datum = str(p.get("datum") or dt.date.today().isoformat())
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", datum) or datum > dt.date.today().isoformat() or datum < r["datum"]:
        raise Greska("Datum plaćanja mora biti između datuma računa i danas.")
    nacin = str(p.get("nacin") or "T")
    if nacin not in dict(NACINI_PLACANJA):
        raise Greska("Način plaćanja je T, O ili Z.")
    s = str(p.get("iznos") or "").replace(",", ".").strip()
    try:
        iznos = int(round(float(s) * 100)) if s else r["iznos"]
    except ValueError:
        raise Greska("Neispravan iznos.")
    if iznos <= 0 or iznos > r["iznos"]:
        raise Greska("Iznos mora biti veći od 0 i najviše iznos računa.")
    pz, pst = izvedba(con)
    pz.placeno(r["ext_id"], datum, iznos, nacin, r["oznaka"], r["godina"])
    with con:
        con.execute("UPDATE eracun_izlazni SET placeno_datum = ?, placeno_iznos = ?, placeno_nacin = ? WHERE id = ?",
                    (datum, iznos, nacin, iid))
    return {"poruka": "Naplata eRačuna %s javljena posredniku (eIzvještavanje)." % r["oznaka"]}


def izlazni(con, granica=200):
    pst = sve_postavke(con)
    return [dict(r) for r in con.execute(
        """SELECT i.id, i.oznaka, i.datum, i.dospijece, i.iznos, i.mjesec, i.status, i.poruka, i.ext_id, i.posrednik,
                  i.placeno_datum, i.placeno_iznos, i.placeno_nacin, k.naziv AS kupac
           FROM eracun_izlazni i LEFT JOIN kupci k ON k.id = i.kupac_id
           WHERE i.okolina = ? ORDER BY i.godina DESC, i.broj DESC LIMIT ?""", (pst["okolina"], granica))]


def xml_izlaznog(con, iid):
    r = con.execute("SELECT xml, oznaka FROM eracun_izlazni WHERE id = ?", (iid,)).fetchone()
    if not r:
        raise Greska("eRačun ne postoji.")
    return r["xml"], r["oznaka"]


def stanje(con):
    kupci = [dict(id=r["id"], naziv=r["naziv"], oib=r["oib"]) for r in con.execute(
        "SELECT id, naziv, oib FROM kupci WHERE aktivan = 1 AND oib != '' ORDER BY naziv")]
    return {"postavke": za_prikaz(con), "ulazni": ulazni(con), "izlazni": izlazni(con), "kupci": kupci}
