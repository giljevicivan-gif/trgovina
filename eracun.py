# -*- coding: utf-8 -*-
"""
Priprema za Fiskalizaciju 2.0 (tri modula, svaki u Postavkama):

  kpd          KPD šifra u našem šifrarniku: upis na artiklu (ručno upisana ostaje i kad
               Roba uveze svoju), prijedlog šifre od sličnih artikala, popis artikala bez šifre.
  r1_kasa      R1 račun na kasi: kupac tvrtka (naziv, adresa, OIB) na računu i oznaka R1.
               Poruka Poreznoj (CIS) ostaje ista: fiskalni račun nema polje za OIB kupca.
               Potvrdite s knjigovođom da je za tvrtku koja plaća na kasi to dovoljno.
  eracun_uvoz  Uvoz eRačuna dobavljača (UBL 2.1 XML) u primku: dobavljač po OIB-u, stavke po
               barkodu (EAN) ili po šifri dobavljača koju smo već jednom povezali. Ništa se ne
               šalje Poreznoj; primka se sprema tek kad je provjerite i kliknete Spremi.
"""

import re
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

from baza import oib_ispravan
from nabava import Greska

SHEMA = """
CREATE TABLE IF NOT EXISTS eracun_veze (
    partner_id  INTEGER NOT NULL,
    kljuc       TEXT NOT NULL,        -- 'e:<EAN>' ili 's:<šifra dobavljača>'
    artikl_id   INTEGER NOT NULL,
    PRIMARY KEY (partner_id, kljuc)
);
"""


def pripremi(con):
    con.executescript(SHEMA)
    stupci = {r[1] for r in con.execute("PRAGMA table_info(artikli)")}
    if "kpd" not in stupci:  # inače ga dodaje roba_dodatno
        con.execute("ALTER TABLE artikli ADD COLUMN kpd TEXT NOT NULL DEFAULT ''")
    if "kpd_rucno" not in stupci:
        # 1 = KPD upisan kod nas; uvoz iz Robe ga ne prepisuje
        con.execute("ALTER TABLE artikli ADD COLUMN kpd_rucno INTEGER NOT NULL DEFAULT 0")
    if "kupac_id" not in {r[1] for r in con.execute("PRAGMA table_info(racuni)")}:
        con.execute("ALTER TABLE racuni ADD COLUMN kupac_id INTEGER")  # R1: kupac tvrtka
    con.commit()


# ---------------------------------------------------------------- KPD

KPD_OBLIK = re.compile(r"^\d{2}(\.\d{1,2}){1,3}$")  # npr. 01.13 ili 01.13.12


def spremi_kpd(con, aid, kpd):
    kpd = str(kpd or "").strip()
    if kpd and not KPD_OBLIK.match(kpd):
        raise Greska("KPD šifra je oblika 01.13.12 (brojke odvojene točkom).")
    with con:
        if not con.execute("SELECT 1 FROM artikli WHERE id = ?", (aid,)).fetchone():
            raise Greska("Artikl ne postoji.")
        con.execute("UPDATE artikli SET kpd = ?, kpd_rucno = ? WHERE id = ?", (kpd, 1 if kpd else 0, aid))


def naziv_kpd(con, sifra):
    try:
        r = con.execute("SELECT naziv FROM kpd_sifre WHERE sifra = ?", (sifra,)).fetchone()
    except Exception:
        return ""
    return r[0] if r else ""


def prijedlog_kpd(con, aid):
    """Najčešća KPD šifra kod artikala s istom prvom riječi naziva (npr. svih 'Jabuka ...'),
    inače kod artikala iste jedinice mjere i stope PDV-a. Samo prijedlog: provjerite ga."""
    a = con.execute("SELECT * FROM artikli WHERE id = ?", (aid,)).fetchone()
    if not a:
        raise Greska("Artikl ne postoji.")
    rijec = (a["naziv"].split() or [""])[0]
    kandidati = []
    if len(rijec) >= 3:
        kandidati = con.execute(
            """SELECT kpd, COUNT(*) n FROM artikli WHERE kpd != '' AND id != ? AND naziv LIKE ?
               GROUP BY kpd ORDER BY n DESC LIMIT 3""", (aid, rijec + "%")).fetchall()
    izvor = "isti početak naziva (%s)" % rijec
    if not kandidati:
        kandidati = con.execute(
            """SELECT kpd, COUNT(*) n FROM artikli WHERE kpd != '' AND id != ? AND lower(jm) = lower(?)
               AND pdv_stopa = ? GROUP BY kpd ORDER BY n DESC LIMIT 3""",
            (aid, a["jm"], a["pdv_stopa"])).fetchall()
        izvor = "ista jedinica mjere i PDV"
    return {"prijedlozi": [{"kpd": k["kpd"], "naziv": naziv_kpd(con, k["kpd"]), "artikala": k["n"]}
                           for k in kandidati], "izvor": izvor if kandidati else ""}


def bez_kpd(con, samo_tvrtke=False, granica=500):
    """Aktivni artikli bez KPD šifre (eRačun je traži na svakoj stavci)."""
    rows = con.execute(
        """SELECT id, sifra, naziv, jm FROM artikli WHERE aktivan = 1 AND kpd = '' AND usluga = 0
           ORDER BY naziv LIMIT ?""", (granica,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- R1 na kasi

def kupci(con):
    return [dict(r) for r in con.execute(
        "SELECT id, naziv, oib, adresa, mjesto FROM kupci WHERE aktivan = 1 AND oib != '' ORDER BY naziv")]


def novi_kupac(con, p):
    naziv = " ".join(str(p.get("naziv") or "").split())[:120]
    oib = re.sub(r"\s", "", str(p.get("oib") or ""))
    if not naziv:
        raise Greska("Upišite naziv tvrtke.")
    if not oib_ispravan(oib):
        raise Greska("OIB kupca nije ispravan.")
    with con:
        r = con.execute("SELECT id FROM kupci WHERE oib = ?", (oib,)).fetchone()
        if r:
            return r[0]
        return con.execute("INSERT INTO kupci (naziv, oib, adresa, mjesto) VALUES (?, ?, ?, ?)",
                           (naziv, oib, str(p.get("adresa") or "").strip()[:200],
                            str(p.get("mjesto") or "").strip()[:100])).lastrowid


def provjeri_kupca(con, kid):
    """Za naplatu: id kupca s ispravnim OIB-om ili Greska."""
    try:
        kid = int(kid)
    except (TypeError, ValueError):
        raise Greska("Neispravan kupac za R1 račun.")
    r = con.execute("SELECT oib FROM kupci WHERE id = ?", (kid,)).fetchone()
    if not r or not oib_ispravan(r[0] or ""):
        raise Greska("Kupac za R1 račun nema ispravan OIB.")
    return kid


def kupac_za_ispis(con, kid):
    if not kid:
        return None
    r = con.execute("SELECT naziv, oib, adresa, mjesto FROM kupci WHERE id = ?", (kid,)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- uvoz UBL eRačuna

NS = {"cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
      "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"}
JM = {"KGM": "kg", "GRM": "g", "LTR": "l", "H87": "kom", "C62": "kom", "PCE": "kom", "EA": "kom",
      "XBX": "kut", "XCR": "gajba", "MTR": "m", "XPK": "pak", "XPA": "pak"}


def _d(x, default="0"):
    try:
        return Decimal((x or default).strip())
    except (InvalidOperation, AttributeError):
        raise Greska("eRačun ima neispravan broj: %r" % x)


def _t(el, put):
    x = el.find(put, NS)
    return (x.text or "").strip() if x is not None and x.text else ""


def _oib_iz(party):
    """OIB dobavljača: EndpointID (shema 9934), PartyTaxScheme 'HR…', PartyLegalEntity."""
    for put in ("cbc:EndpointID", "cac:PartyTaxScheme/cbc:CompanyID", "cac:PartyLegalEntity/cbc:CompanyID",
                "cac:PartyIdentification/cbc:ID"):
        for el in party.findall(put, NS):
            m = re.search(r"(\d{11})", el.text or "")
            if m and oib_ispravan(m.group(1)):
                return m.group(1)
    return ""


def procitaj(con, xml):
    """eRačun (UBL 2.1 Invoice) -> prijedlog primke. Ništa ne sprema."""
    if not xml or len(xml) > 5_000_000:
        raise Greska("Odaberite XML datoteku eRačuna (najviše 5 MB).")
    if "<!ENTITY" in xml[:5000]:
        raise Greska("XML s entitetima nije podržan.")
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except ET.ParseError as e:
        raise Greska("Datoteka nije ispravan XML (%s)." % e)
    ime = root.tag.split("}")[-1]
    if ime == "CreditNote":
        raise Greska("Ovo je odobrenje (CreditNote), ne račun. Upišite ga kao povrat dobavljaču.")
    if ime != "Invoice":
        raise Greska("Ovo nije eRačun u obliku UBL 2.1 (Invoice).")
    party = root.find("cac:AccountingSupplierParty/cac:Party", NS)
    if party is None:
        raise Greska("U eRačunu nema dobavljača.")
    oib = _oib_iz(party)
    naziv = (_t(party, "cac:PartyLegalEntity/cbc:RegistrationName") or _t(party, "cac:PartyName/cbc:Name"))
    p = con.execute("SELECT id, naziv FROM partneri WHERE oib = ? AND oib != ''", (oib,)).fetchone() if oib else None
    adresa = party.find("cac:PostalAddress", NS)
    dob = {"oib": oib, "naziv": naziv, "partner_id": p["id"] if p else None,
           "adresa": _t(adresa, "cbc:StreetName") if adresa is not None else "",
           "mjesto": _t(adresa, "cbc:CityName") if adresa is not None else ""}
    stavke, upozorenja = [], []
    for ln in root.findall("cac:InvoiceLine", NS):
        q_el = ln.find("cbc:InvoicedQuantity", NS)
        kol = _d(q_el.text if q_el is not None else "0")
        jm_kod = q_el.get("unitCode", "") if q_el is not None else ""
        iznos = _d(_t(ln, "cbc:LineExtensionAmount"))
        item = ln.find("cac:Item", NS)
        cijena = _d(_t(ln, "cac:Price/cbc:PriceAmount"))
        baza_q = _d(_t(ln, "cac:Price/cbc:BaseQuantity"), "1") or Decimal(1)
        jed = cijena / baza_q if baza_q else cijena
        bruto = kol * jed
        rabat = Decimal(0)
        if bruto > 0 and iznos < bruto:
            rabat = ((1 - iznos / bruto) * 100).quantize(Decimal("0.01"))
        elif bruto == 0 and kol:
            jed = iznos / kol
        ean = _t(item, "cac:StandardItemIdentification/cbc:ID")
        sifra = _t(item, "cac:SellersItemIdentification/cbc:ID")
        kpd = ""
        for c in item.findall("cac:CommodityClassification/cbc:ItemClassificationCode", NS):
            if KPD_OBLIK.match((c.text or "").strip()):
                kpd = c.text.strip()
        s = {"naziv": _t(item, "cbc:Name"), "ean": ean, "sifra_dob": sifra, "kpd": kpd,
             "kolicina": float(kol), "jm": JM.get(jm_kod, jm_kod.lower()),
             "cijena": float(jed.quantize(Decimal("0.0001"))), "rabat": float(rabat), "iznos": float(iznos),
             "pdv": float(_d(_t(item, "cac:ClassifiedTaxCategory/cbc:Percent"))), "artikl_id": None}
        s["artikl_id"] = _pronadji(con, dob["partner_id"], ean, sifra)
        if kol <= 0:
            upozorenja.append("Stavka %s ima količinu %s (povrat?): provjerite je ručno." % (s["naziv"], kol))
        stavke.append(s)
    if not stavke:
        raise Greska("U eRačunu nema stavki.")
    for ac in root.findall("cac:AllowanceCharge", NS):
        vrsta = "Popust" if _t(ac, "cbc:ChargeIndicator") == "false" else "Trošak"
        upozorenja.append("%s na cijelom računu: %s EUR (%s). Raspodijelite ga ručno (npr. rabat za sve stavke)."
                          % (vrsta, _t(ac, "cbc:Amount"), _t(ac, "cbc:AllowanceChargeReason") or "bez opisa"))
    if not oib:
        upozorenja.append("U eRačunu nije pronađen ispravan OIB dobavljača.")
    return {"broj": _t(root, "cbc:ID"), "datum": _t(root, "cbc:IssueDate"), "dobavljac": dob,
            "neto": float(_d(_t(root, "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount"))),
            "ukupno": float(_d(_t(root, "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount"))),
            "za_platiti": float(_d(_t(root, "cac:LegalMonetaryTotal/cbc:PayableAmount"))),
            "dospijece": _t(root, "cbc:DueDate") or _t(root, "cac:PaymentMeans/cbc:PaymentDueDate"),
            "stavke": stavke, "upozorenja": upozorenja}


def _pronadji(con, partner_id, ean, sifra):
    if ean:
        r = con.execute("SELECT id FROM artikli WHERE barkod = ? AND aktivan = 1", (ean,)).fetchone()
        if r:
            return r[0]
    if partner_id:
        for k in (("e:" + ean) if ean else None, ("s:" + sifra) if sifra else None):
            if k:
                r = con.execute("SELECT artikl_id FROM eracun_veze WHERE partner_id = ? AND kljuc = ?",
                                (partner_id, k)).fetchone()
                if r:
                    return r[0]
    return None


def spremi_veze(con, p):
    """Zapamti koja stavka dobavljača je koji naš artikl (za sljedeći eRačun)."""
    try:
        pid = int(p.get("partner_id"))
    except (TypeError, ValueError):
        raise Greska("Nepoznat dobavljač.")
    n = 0
    with con:
        for v in p.get("veze") or []:
            try:
                aid = int(v.get("artikl_id"))
            except (TypeError, ValueError):
                continue
            for k in (("e:" + v["ean"]) if v.get("ean") else None,
                      ("s:" + v["sifra_dob"]) if v.get("sifra_dob") else None):
                if k:
                    con.execute("INSERT OR REPLACE INTO eracun_veze (partner_id, kljuc, artikl_id) VALUES (?, ?, ?)",
                                (pid, k[:120], aid))
                    n += 1
            if v.get("kpd") and KPD_OBLIK.match(v["kpd"]):  # KPD s eRačuna, ako artikl nema svoj
                con.execute("UPDATE artikli SET kpd = ? WHERE id = ? AND kpd = ''", (v["kpd"], aid))
    return n
