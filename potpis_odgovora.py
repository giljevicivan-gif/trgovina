# -*- coding: utf-8 -*-
"""
Provjera potpisa na odgovoru Porezne (CIS) za fiskalizaciju računa.

Porezna potpisuje svaki odgovor (RacunOdgovor) svojim certifikatom "fiskalcis"
i traži da kasa taj potpis provjeri (obavijest od 4.8.2026: certifikat je
zamijenjen 8.9.2026.). Ovdje se provjerava:
  1. sažetak (DigestValue) potpisanog dijela odgovora: da nitko nije promijenio JIR,
  2. potpis (SignatureValue) javnim ključem iz certifikata u odgovoru,
  3. da je certifikat izdan Poreznoj ("fiskalcis..."), od FINA-e i da vrijedi.
Ako u mapi certifikati postoji FINA CA certifikat (isti koji treba za provjeru
TLS veze), provjeri se i da ga je ta CA potpisala.

Kanonizacija XML-a (C14N 1.0 i Exclusive C14N 1.0) napisana je ovdje, bez
dodatnih paketa: odgovor se čita expatom bez obrade imenskih prostora pa
prefiksi ostaju kakvi jesu (o njima ovisi potpis).

Rezultat: ("ok" | "neispravan" | "upozorenje" | "nije_provjeren", opis, podaci o certifikatu).
"neispravan" znači da potpis dokazano ne odgovara (odgovor je promijenjen ili
nije od Porezne) i JIR se ne prihvaća; "upozorenje" da je potpis Porezne
ispravan, ali s certifikatom nešto nije u redu (istekao, CA iz mape ne odgovara);
"nije_provjeren" da provjera nije bila moguća (npr. odgovor bez potpisa ili
nepoznat algoritam). Kod "upozorenje" i "nije_provjeren" JIR se prihvaća, a
Kontrola to javi: zbog greške u provjeri ne smiju stati svi računi.
"""

import base64
import datetime as dt
import hashlib
import os
import xml.parsers.expat

DS = "http://www.w3.org/2000/09/xmldsig#"
C14N = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
C14N_K = C14N + "#WithComments"
EXC = "http://www.w3.org/2001/10/xml-exc-c14n#"
EXC_K = EXC + "WithComments"
ENVELOPED = DS + "enveloped-signature"
SAZETCI = {
    DS + "sha1": hashlib.sha1,
    "http://www.w3.org/2001/04/xmlenc#sha256": hashlib.sha256,
    "http://www.w3.org/2001/04/xmldsig-more#sha384": hashlib.sha384,
    "http://www.w3.org/2001/04/xmlenc#sha512": hashlib.sha512,
}
POTPISI = {
    DS + "rsa-sha1": "SHA1",
    "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256": "SHA256",
    "http://www.w3.org/2001/04/xmldsig-more#rsa-sha384": "SHA384",
    "http://www.w3.org/2001/04/xmldsig-more#rsa-sha512": "SHA512",
}
XML_NS = "http://www.w3.org/XML/1998/namespace"


class Cvor:
    __slots__ = ("ime", "atr", "djeca", "roditelj")

    def __init__(self, ime, atr, roditelj):
        self.ime, self.atr, self.djeca, self.roditelj = ime, atr, [], roditelj

    # -- imenski prostori
    def deklaracije(self):
        """{prefiks: uri} deklarirano na ovom elementu ('' = zadani)."""
        out = {}
        for k, v in self.atr:
            if k == "xmlns":
                out[""] = v
            elif k.startswith("xmlns:"):
                out[k[6:]] = v
        return out

    def u_dosegu(self):
        """Svi imenski prostori u dosegu ovog elementa."""
        lanac, c = [], self
        while c is not None:
            lanac.append(c)
            c = c.roditelj
        ns = {}
        for c in reversed(lanac):
            ns.update(c.deklaracije())
        return ns

    def lokalno(self):
        return self.ime.split(":", 1)[-1]

    def prefiks(self):
        return self.ime.split(":", 1)[0] if ":" in self.ime else ""

    def uri(self):
        return self.u_dosegu().get(self.prefiks(), "")

    def elementi(self):
        return [d for d in self.djeca if isinstance(d, Cvor)]

    def tekst(self):
        return "".join(d for d in self.djeca if isinstance(d, str))

    def svi(self):
        yield self
        for d in self.elementi():
            yield from d.svi()


def procitaj(tekst):
    """Stablo iz XML teksta (bajtovi ili str). Komentari i PI se ne čuvaju (CIS ih ne šalje)."""
    if isinstance(tekst, str):
        tekst = tekst.encode("utf-8")
    p = xml.parsers.expat.ParserCreate()  # bez imenskih prostora: prefiksi ostaju
    p.ordered_attributes = True
    stog, korijen = [], []

    def pocetak(ime, atr):
        c = Cvor(ime, list(zip(atr[0::2], atr[1::2])), stog[-1] if stog else None)
        if stog:
            stog[-1].djeca.append(c)
        else:
            korijen.append(c)
        stog.append(c)

    def kraj(ime):
        stog.pop()

    def znakovi(t):
        if stog:
            if stog[-1].djeca and isinstance(stog[-1].djeca[-1], str):
                stog[-1].djeca[-1] += t
            else:
                stog[-1].djeca.append(t)

    p.StartElementHandler, p.EndElementHandler, p.CharacterDataHandler = pocetak, kraj, znakovi
    p.Parse(tekst, True)
    return korijen[0]


def _esc_tekst(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", "&#xD;")


def _esc_atr(t):
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
            .replace("\t", "&#x9;").replace("\n", "&#xA;").replace("\r", "&#xD;"))


def kanoniziraj(cvor, iskljucivi=False, izostavi=None, prefiksi=()):
    """C14N 1.0 (inkluzivni) ili Exclusive C14N 1.0 podstabla s korijenom u cvor,
    bez elementa izostavi (enveloped-signature). Vraća bajtove UTF-8."""
    dijelovi = []
    xml_atr = {}
    if not iskljucivi:  # inkluzivni C14N nasljeđuje xml:* atribute predaka
        c = cvor.roditelj
        lanac = []
        while c is not None:
            lanac.append(c)
            c = c.roditelj
        for c in reversed(lanac):
            for k, v in c.atr:
                if k.startswith("xml:"):
                    xml_atr[k] = v

    def element(c, ns_gore, ns_ispisano, prvi):
        ns = dict(ns_gore)
        ns.update(c.deklaracije())
        if iskljucivi:
            potrebni = {c.prefiks()} | {k.split(":", 1)[0] for k, _ in c.atr
                                        if ":" in k and not k.startswith("xmlns") and not k.startswith("xml:")}
            potrebni |= set(prefiksi)
            dekl = {p: ns.get(p, "") for p in potrebni if p != "xml"}
            dekl = {p: u for p, u in dekl.items() if ns_ispisano.get(p, "") != u and (u or p == "")}
            if "" in dekl and dekl[""] == "" and ns_ispisano.get("", "") == "":
                del dekl[""]
        else:
            dekl = {p: u for p, u in ns.items() if p != "xml" and ns_ispisano.get(p, "") != u}
            if dekl.get("", None) == "" and ns_ispisano.get("", "") == "":
                del dekl[""]
        ispisano = dict(ns_ispisano)
        ispisano.update(dekl)
        atr = [(k, v) for k, v in c.atr if k != "xmlns" and not k.startswith("xmlns:")]
        if prvi and xml_atr:
            vlastiti = {k for k, _ in atr}
            atr += [(k, v) for k, v in xml_atr.items() if k not in vlastiti]

        def kljuc(kv):
            k = kv[0]
            if ":" in k:
                p, lok = k.split(":", 1)
                return (XML_NS if p == "xml" else ns.get(p, ""), lok)
            return ("", k)
        ns_dio = "".join(' xmlns="%s"' % _esc_atr(u) if p == "" else ' xmlns:%s="%s"' % (p, _esc_atr(u))
                         for p, u in sorted(dekl.items()))
        atr_dio = "".join(' %s="%s"' % (k, _esc_atr(v)) for k, v in sorted(atr, key=kljuc))
        dijelovi.append("<%s%s%s>" % (c.ime, ns_dio, atr_dio))
        for d in c.djeca:
            if isinstance(d, str):
                dijelovi.append(_esc_tekst(d))
            elif d is not izostavi:
                element(d, ns, ispisano, False)
        dijelovi.append("</%s>" % c.ime)

    ns_iznad = cvor.roditelj.u_dosegu() if cvor.roditelj is not None else {}
    element(cvor, ns_iznad, {}, True)
    return "".join(dijelovi).encode("utf-8")


def _dijete(c, lokalno):
    for d in c.elementi():
        if d.lokalno() == lokalno:
            return d
    return None


def _algoritam(c):
    return dict(c.atr).get("Algorithm", "") if c is not None else ""


def _c14n_iz(c):
    """(iskljucivi, prefiksi) za element CanonicalizationMethod / Transform."""
    alg = _algoritam(c)
    if alg in (C14N, C14N_K):
        return False, ()
    if alg in (EXC, EXC_K):
        inc = next((d for d in c.elementi() if d.lokalno() == "InclusiveNamespaces"), None)
        pref = dict(inc.atr).get("PrefixList", "").split() if inc is not None else []
        return True, tuple("" if p == "#default" else p for p in pref)
    return None


def _ca_certifikati(mapa):
    """FINA CA certifikati iz mape certifikati (.cer/.crt/.pem), ako ih ima."""
    from cryptography import x509
    out = []
    if not mapa or not os.path.isdir(mapa):
        return out
    for ime in os.listdir(mapa):
        if not ime.lower().endswith((".cer", ".crt", ".pem")):
            continue
        try:
            with open(os.path.join(mapa, ime), "rb") as f:
                b = f.read()
            if b"-----BEGIN" in b:
                out += x509.load_pem_x509_certificates(b)
            else:
                out.append(x509.load_der_x509_certificate(b))
        except Exception:
            continue
    return out


def _ime(cert_ime, oid):
    try:
        v = cert_ime.get_attributes_for_oid(oid)
        return v[0].value if v else ""
    except Exception:
        return ""


def provjeri(tekst, ca_mapa=None, sada=None):
    """Provjeri potpis odgovora. Vraća (stanje, opis, podaci_o_certifikatu)."""
    try:
        korijen = procitaj(tekst)
    except Exception as e:
        return "nije_provjeren", "Odgovor se ne može pročitati (%s)." % e, {}
    potpis = next((c for c in korijen.svi() if c.lokalno() == "Signature" and c.uri() == DS), None)
    if potpis is None:
        return "nije_provjeren", "Odgovor Porezne nema potpis.", {}
    try:
        from cryptography import x509
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        return "nije_provjeren", "Nedostaje paket cryptography.", {}

    si = _dijete(potpis, "SignedInfo")
    sv = _dijete(potpis, "SignatureValue")
    ki = _dijete(potpis, "KeyInfo")
    if si is None or sv is None:
        return "nije_provjeren", "Potpis nema SignedInfo ili SignatureValue.", {}
    c14n_si = _c14n_iz(_dijete(si, "CanonicalizationMethod"))
    alg_potpisa = POTPISI.get(_algoritam(_dijete(si, "SignatureMethod")))
    if c14n_si is None or not alg_potpisa:
        return "nije_provjeren", "Nepoznat algoritam potpisa (%s)." % _algoritam(_dijete(si, "SignatureMethod")), {}

    # certifikat iz KeyInfo
    cert = None
    if ki is not None:
        for c in ki.svi():
            if c.lokalno() == "X509Certificate" and c.tekst().strip():
                try:
                    cert = x509.load_der_x509_certificate(base64.b64decode("".join(c.tekst().split())))
                    break
                except Exception:
                    return "nije_provjeren", "Certifikat u odgovoru se ne može pročitati.", {}
    if cert is None:
        return "nije_provjeren", "Odgovor nema certifikat Porezne.", {}
    try:
        do = cert.not_valid_after_utc.replace(tzinfo=None)
        od = cert.not_valid_before_utc.replace(tzinfo=None)
    except AttributeError:  # starija inačica paketa cryptography
        do, od = cert.not_valid_after, cert.not_valid_before
    cn = _ime(cert.subject, NameOID.COMMON_NAME)
    izdavatelj = _ime(cert.issuer, NameOID.COMMON_NAME) or _ime(cert.issuer, NameOID.ORGANIZATION_NAME)
    podaci = {"cn": cn, "izdavatelj": izdavatelj, "vrijedi_od": od.strftime("%d.%m.%Y."),
              "vrijedi_do": do.strftime("%d.%m.%Y."), "otisak": cert.fingerprint(hashes.SHA256()).hex()[:16],
              "isteka_iso": do.strftime("%Y-%m-%d")}

    # 1. sažeci svih Reference elemenata
    reference = [d for d in si.elementi() if d.lokalno() == "Reference"]
    if not reference:
        return "nije_provjeren", "Potpis nema Reference.", podaci
    for ref in reference:
        uri = dict(ref.atr).get("URI", "")
        if uri.startswith("#"):
            cilj = next((c for c in korijen.svi() if dict(c.atr).get("Id") == uri[1:]), None)
        elif uri == "":
            cilj = korijen
        else:
            return "nije_provjeren", "Nepodržana referenca %s." % uri, podaci
        if cilj is None:
            return "neispravan", "Potpisani dio odgovora (%s) ne postoji." % uri, podaci
        iskljucivi, prefiksi, izostavi = False, (), None
        tr = _dijete(ref, "Transforms")
        for t in (tr.elementi() if tr is not None else []):
            if _algoritam(t) == ENVELOPED:
                izostavi = potpis
            else:
                c = _c14n_iz(t)
                if c is None:
                    return "nije_provjeren", "Nepodržana transformacija %s." % _algoritam(t), podaci
                iskljucivi, prefiksi = c
        fn = SAZETCI.get(_algoritam(_dijete(ref, "DigestMethod")))
        dv = _dijete(ref, "DigestValue")
        if not fn or dv is None:
            return "nije_provjeren", "Nepoznat algoritam sažetka.", podaci
        izracunato = base64.b64encode(fn(kanoniziraj(cilj, iskljucivi, izostavi, prefiksi)).digest()).decode()
        if izracunato != "".join(dv.tekst().split()):
            return "neispravan", "Sažetak odgovora ne odgovara potpisu: odgovor je promijenjen.", podaci

    # 2. potpis nad SignedInfo
    kljuc = cert.public_key()
    if not isinstance(kljuc, rsa.RSAPublicKey):
        return "nije_provjeren", "Certifikat Porezne nema RSA ključ.", podaci
    try:
        kljuc.verify(base64.b64decode("".join(sv.tekst().split())), kanoniziraj(si, *c14n_si[:1], None, c14n_si[1]),
                     padding.PKCS1v15(), getattr(hashes, alg_potpisa)())
    except InvalidSignature:
        return "neispravan", "Potpis odgovora nije ispravan.", podaci
    except Exception as e:
        return "nije_provjeren", "Potpis se ne može provjeriti (%s)." % e, podaci

    # 3. čiji je certifikat
    if not cn.lower().startswith("fiskalcis"):
        return "neispravan", "Odgovor nije potpisala Porezna (certifikat: %s)." % (cn or "?"), podaci
    if "fina" not in izdavatelj.lower():
        return "neispravan", "Certifikat Porezne nije izdala FINA (%s)." % izdavatelj, podaci
    # potpis je ispravan i od Porezne; dalje su samo upozorenja (JIR se prihvaća)
    sada = sada or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    if not (od - dt.timedelta(days=1) <= sada <= do + dt.timedelta(days=1)):
        return "upozorenje", "Certifikat Porezne ne vrijedi (vrijedi %s - %s)." % (
            podaci["vrijedi_od"], podaci["vrijedi_do"]), podaci
    ca = [c for c in _ca_certifikati(ca_mapa) if c.subject == cert.issuer]
    if ca:
        podaci["lanac"] = False
        for c in ca:
            try:
                c.public_key().verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(),
                                      cert.signature_hash_algorithm)
                podaci["lanac"] = True
                break
            except Exception:
                continue
        if not podaci["lanac"]:
            return "upozorenje", ("Certifikat Porezne nije potpisala %s iz mape certifikati (možda je u mapi "
                                  "stari CA certifikat)." % izdavatelj), podaci
    return "ok", "Potpis Porezne je ispravan.", podaci
