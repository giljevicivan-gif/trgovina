# -*- coding: utf-8 -*-
"""Test modula eRačun preko posrednika (eracun_posrednik.py).

Pokretanje (iz mape programa):  python -m unittest discover -s testovi -v
Radi na privremenoj bazi i s lažnim poslužiteljem moj-eRačuna na 127.0.0.1: ništa se ne šalje van.
"""

import base64
import datetime as dt
import hashlib
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import baza  # noqa: E402
import eracun  # noqa: E402
import eracun_posrednik as ep  # noqa: E402
import postavke  # noqa: E402
from nabava import Greska  # noqa: E402


def oib(pocetak):
    for z in range(10):
        o = pocetak + str(z)
        if baza.oib_ispravan(o):
            return o


OIB_MI = oib("1234567890")
OIB_KUPAC = oib("9876543210")
OIB_DOB = oib("5555555555")


class LazniMer(http.server.BaseHTTPRequestHandler):
    zahtjevi = []
    ulazni_xml = ""

    def log_message(self, *a):
        pass

    def do_POST(self):
        tijelo = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
        LazniMer.zahtjevi.append((self.path, tijelo))
        if tijelo.get("Password") != "tajna" and tijelo.get("SoftwareId"):
            return self._odg(401, {"message": "Korisničko ime i lozinka nisu ispravni"})
        if self.path == "/apis/v2/queryInbox":
            return self._odg(200, [{"ElectronicId": 777, "DocumentNr": "15-1-1", "SenderBusinessNumber": OIB_DOB,
                                    "SenderBusinessName": "Dobavljač d.o.o.", "Sent": "2026-11-20T10:00:00",
                                    "StatusId": 40, "StatusName": "Dostavljen", "Amount": 12.5}])
        if self.path == "/apis/v2/receive":
            return self._odg(200, LazniMer.ulazni_xml, xml=True)
        if self.path == "/apis/v2/send":
            return self._odg(200, {"ElectronicId": 4242, "StatusId": 20, "StatusName": "U validaciji",
                                   "RecipientBusinessName": "Kupac d.o.o."})
        if self.path == "/apis/v2/queryOutbox":
            return self._odg(200, [{"ElectronicId": 4242, "StatusId": 40, "StatusName": "Dostavljen"}])
        if self.path == "/api/fiscalization/statusOutbox":
            return self._odg(200, [{"electronicID": 4242, "messages": [{"status": 0, "messageType": 0}]}])
        if self.path in ("/api/fiscalization/markPaid", "/api/fiscalization/reject", "/api/mps/check"):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._odg(404, {"message": "nema"})

    def _odg(self, kod, tijelo, xml=False):
        b = tijelo.encode("utf-8") if xml else json.dumps(tijelo).encode("utf-8")
        self.send_response(kod)
        self.send_header("Content-Type", "application/xml" if xml else "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class Posrednik(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LazniMer)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.con = baza.spoji(os.path.join(self.dir, "t.db"))
        c = self.con
        for k, v in (("fiskal.oib", OIB_MI), ("fiskal.naziv_tvrtke", "Trgovina d.o.o."), ("fiskal.adresa", "Ulica 1"),
                     ("fiskal.prostor", "PP1"), ("eposrednik.korisnik", "a@b.hr"),
                     ("eposrednik.lozinka", "b64:" + base64.b64encode(b"tajna").decode()),
                     ("eposrednik.software_id", "Test-001"), ("uplata.iban", "HR1210010051863000160"),
                     ("eposrednik.url", "http://127.0.0.1:%d" % self.srv.server_address[1])):
            postavke.postavi(c, k, v)
        c.execute("INSERT INTO artikli (id, sifra, naziv, jm, cijena, pdv_stopa, barkod) VALUES "
                  "(1, '1', 'Jabuka', 'kg', 199, 5, '3850000000017'), (2, '2', 'Sok', 'Kom', 249, 25, '')")
        c.execute("UPDATE artikli SET kpd = '01.24.10'")
        c.execute("INSERT INTO kupci (id, naziv, oib, adresa, mjesto, rok_placanja) VALUES "
                  "(1, 'Kupac d.o.o.', ?, 'Trg 2', 'Split', 15)", (OIB_KUPAC,))
        mj = dt.date.today().replace(day=1).isoformat()
        c.execute("INSERT INTO narudzbe (id, godina, broj, kupac_id, vrijeme, rok, status) VALUES "
                  "(1, 2026, 1, 1, ?, ?, 'isporucena'), (2, 2026, 2, 1, ?, ?, 'isporucena')", (mj, mj, mj, mj))
        c.execute("INSERT INTO narudzba_stavke (narudzba_id, artikl_id, naziv, jm, kolicina, cijena, iznos, pdv_stopa) VALUES "
                  "(1, 1, 'Jabuka', 'kg', 3.25, 199, 647, 5), (2, 1, 'Jabuka', 'kg', 1.5, 199, 299, 5),"
                  "(2, 2, 'Sok', 'Kom', 3, 249, 747, 25)")
        c.commit()
        self.mjesec = mj[:7]
        LazniMer.zahtjevi.clear()

    def zadnji(self, put):
        return [t for p, t in LazniMer.zahtjevi if p == put][-1]

    def test_modul_zadano_iskljucen(self):
        self.assertFalse(postavke.ukljucen(self.con, "eracun_posrednik"))

    def test_racun_je_ispravan_ubl(self):
        r = ep.pripremi_racun(self.con, 1, self.mjesec)
        self.assertEqual(r["oznaka"], "1-PP1-9")
        root = ET.fromstring(r["xml"].encode("utf-8"))
        ns = eracun.NS
        t = lambda p: root.find(p, ns).text  # noqa: E731
        self.assertEqual(t("cbc:CustomizationID"), ep.HR_SPEC)
        self.assertEqual(root.find("cac:AccountingCustomerParty/cac:Party/cbc:EndpointID", ns).text, OIB_KUPAC)
        linije = root.findall("cac:InvoiceLine", ns)
        self.assertEqual(len(linije), 2)  # jabuka zbrojena iz dvije narudžbe
        zbroj = sum(round(float(l.find("cbc:LineExtensionAmount", ns).text) * 100) for l in linije)
        self.assertEqual(zbroj, round(float(t("cac:LegalMonetaryTotal/cbc:LineExtensionAmount")) * 100))
        pdv = sum(round(float(s.find("cbc:TaxAmount", ns).text) * 100) for s in root.findall("cac:TaxTotal/cac:TaxSubtotal", ns))
        self.assertEqual(pdv, round(float(t("cac:TaxTotal/cbc:TaxAmount")) * 100))
        for s in root.findall("cac:TaxTotal/cac:TaxSubtotal", ns):  # BR-S-09: PDV = osnovica * stopa
            o = round(float(s.find("cbc:TaxableAmount", ns).text) * 100)
            st = float(s.find("cac:TaxCategory/cbc:Percent", ns).text)
            self.assertEqual(round(float(s.find("cbc:TaxAmount", ns).text) * 100), round(o * st / 100))
        self.assertEqual(round(float(t("cac:LegalMonetaryTotal/cbc:PayableAmount")) * 100), zbroj + pdv)
        self.assertLessEqual(abs((zbroj + pdv) - (647 + 299 + 747)), 2)
        for l in linije:
            self.assertEqual(l.find("cac:Item/cac:CommodityClassification/cbc:ItemClassificationCode", ns).get("listID"), "CG")
        # naš vlastiti uvoz (primka iz eRačuna) mora pročitati ono što šaljemo
        e = eracun.procitaj(self.con, r["xml"])
        self.assertEqual(e["dobavljac"]["oib"], OIB_MI)
        self.assertEqual(len(e["stavke"]), 2)

    def test_bez_kpd_ne_salje(self):
        self.con.execute("UPDATE artikli SET kpd = '01.24' WHERE id = 2")
        with self.assertRaises(Greska):
            ep.pripremi_racun(self.con, 1, self.mjesec)

    def test_slanje_naplata_test_okolina(self):
        r = ep.posalji_racun(self.con, 1, self.mjesec, "Ana")
        poslano = self.zadnji("/apis/v2/send")
        self.assertEqual(poslano["CompanyId"], OIB_MI)
        self.assertEqual(poslano["SoftwareId"], "Test-001")
        self.assertTrue(poslano["File"].startswith("<?xml"))
        red = self.con.execute("SELECT * FROM eracun_izlazni WHERE id = ?", (r["id"],)).fetchone()
        self.assertEqual(red["ext_id"], "4242")
        # na testu narudžbe ostaju bez računa
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM narudzbe WHERE racun_roba = ''").fetchone()[0], 2)
        s = ep.osvjezi(self.con, r["id"])
        self.assertEqual(s["status"], "Dostavljen")
        self.assertIn("uspjela", s["poruka"])
        ep.placeno(self.con, r["id"], {"datum": dt.date.today().isoformat(), "nacin": "T"})
        m = self.zadnji("/api/fiscalization/markPaid")
        self.assertEqual((m["ElectronicId"], m["PaymentMethod"]), (4242, "T"))
        self.assertAlmostEqual(m["PaymentAmount"], red["iznos"] / 100.0)
        with self.assertRaises(Greska):  # iznos veći od računa
            ep.placeno(self.con, r["id"], {"iznos": "99999"})

    def test_produkcija_upisuje_broj_na_narudzbe(self):
        postavke.postavi(self.con, "eposrednik.okolina", "produkcija")
        self.con.commit()
        r = ep.posalji_racun(self.con, 1, self.mjesec)
        racuni = {x[0] for x in self.con.execute("SELECT racun_roba FROM narudzbe")}
        self.assertEqual(racuni, {"eRačun " + r["oznaka"]})
        with self.assertRaises(Greska):  # drugi put: nema narudžbi bez računa
            ep.pripremi_racun(self.con, 1, self.mjesec)

    def test_ulazni_primka_i_odbijanje(self):
        self.con.execute("INSERT INTO partneri (naziv, oib) VALUES ('Dobavljač d.o.o.', ?)", (OIB_DOB,))
        xml, _, _ = ep.ubl_racun(
            "15-1-1", "2026-11-20", "10:00:00", "2026-12-01", "P1",
            {"oib": OIB_DOB, "naziv": "Dobavljač d.o.o.", "adresa": "Put 3", "mjesto": "Zagreb", "postanski": "10000",
             "operater": "Ivo", "oib_operatera": OIB_DOB, "iban": ""},
            {"oib": OIB_MI, "naziv": "Trgovina d.o.o.", "adresa": "Ulica 1", "mjesto": ""},
            [{"naziv": "Jabuka", "jm": "kg", "kolicina": 10, "iznos": 1250, "pdv_stopa": 5, "kpd": "01.24.10",
              "barkod": "3850000000017"}])
        LazniMer.ulazni_xml = xml
        r = ep.dohvati_ulazne(self.con)
        self.assertEqual(r["novih"], 1)
        self.assertEqual(r["greske"], [])
        u = r["ulazni"][0]
        self.assertTrue(u["ima_xml"])
        e = ep.u_primku(self.con, u["id"])
        self.assertEqual(e["stavke"][0]["artikl_id"], 1)  # po barkodu
        self.assertEqual(ep.dohvati_ulazne(self.con)["novih"], 0)  # drugi put ništa novo
        with self.assertRaises(Greska):
            ep.odbij(self.con, u["id"], {"vrsta": "X", "opis": "krivo"})
        ep.odbij(self.con, u["id"], {"vrsta": "U", "opis": "Kriva stopa PDV-a"})
        o = self.zadnji("/api/fiscalization/reject")
        self.assertEqual((o["ElectronicId"], o["RejectionReasonType"]), (777, "U"))
        with self.assertRaises(Greska):
            ep.u_primku(self.con, u["id"])

    def test_kriva_lozinka(self):
        postavke.postavi(self.con, "eposrednik.lozinka", "b64:" + base64.b64encode(b"kriva").decode())
        self.con.commit()
        with self.assertRaises(Greska) as g:
            ep.provjeri(self.con)
        self.assertIn("nisu ispravni", str(g.exception))

    def test_lozinka_se_ne_vraca(self):
        p = ep.za_prikaz(self.con)
        self.assertEqual(p["lozinka"], "upisano")
        ep.spremi_postavke(self.con, {"lozinka": "upisano", "korisnik": "x@y.hr"})
        self.assertEqual(ep._v(self.con, "lozinka"), "b64:" + base64.b64encode(b"tajna").decode())
        with self.assertRaises(Greska):
            ep.spremi_postavke(self.con, {"okolina": "produkcija"})


class FinaPotpis(unittest.TestCase):
    """WS-Security potpis FINA poruke: sažeci i potpis provjereni neovisno (lxml exc-c14n)."""

    def test_potpis(self):
        try:
            from lxml import etree
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
            from cryptography.x509.oid import NameOID
        except ImportError:
            self.skipTest("nema lxml/cryptography")
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ime = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TEST")])
        cert = (x509.CertificateBuilder().subject_name(ime).issuer_name(ime).public_key(key.public_key())
                .serial_number(1).not_valid_before(dt.datetime(2026, 1, 1)).not_valid_after(dt.datetime(2030, 1, 1))
                .sign(key, hashes.SHA256()))
        f = ep.Fina({"okolina": "test", "url": "", "erp_id": "Trgovina", "cert_putanja": "", "cert_lozinka": ""}, OIB_MI)
        xml = f.omotnica("GetB2BIncomingInvoiceList", f._zaglavlje(True, 9101) + "<iwsc:Data></iwsc:Data>", key, cert)
        doc = etree.fromstring(xml.encode("utf-8"))
        ns = {"ds": ep.DS, "wsu": ep.WSU}
        for ref in doc.iterfind(".//ds:Reference", ns):
            uri = ref.get("URI")[1:]
            el = doc.xpath("//*[@wsu:Id=$i]", namespaces=ns, i=uri)[0]
            c14n = etree.tostring(el, method="c14n", exclusive=True)
            self.assertEqual(base64.b64encode(hashlib.sha256(c14n).digest()).decode(),
                             ref.find("ds:DigestValue", ns).text, uri)
        si = etree.tostring(doc.find(".//ds:SignedInfo", ns), method="c14n", exclusive=True)
        sv = base64.b64decode(doc.find(".//ds:SignatureValue", ns).text)
        key.public_key().verify(sv, si, padding.PKCS1v15(), hashes.SHA256())  # baci iznimku ako ne valja


if __name__ == "__main__":
    unittest.main()
