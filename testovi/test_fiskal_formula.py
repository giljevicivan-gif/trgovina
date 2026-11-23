# -*- coding: utf-8 -*-
"""Trajni test: naš program nikad ne smije izdati račun s greškom 166 Porezne
("ukupan iznos na računu nije ispravan prema formuli", tolerancija 0,01 EUR).

Pokretanje (iz mape programa):  python -m unittest discover -s testovi -v
Test radi na privremenoj bazi, ne dira trgovina.db ni certifikat.
"""

import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import baza  # noqa: E402
import fiskal  # noqa: E402
import kasa  # noqa: E402
import postavke  # noqa: E402

STOPE = (5.0, 13.0, 25.0, 0.0)
CIJENE = (99, 149, 199, 249, 286, 349, 449, 499, 777, 1499, 12990, 10, 1)
KOLICINE_KG = (0.001, 0.118, 0.216, 0.384, 0.512, 0.638, 0.999, 1.284, 2.5, 7.333)


def _slucajne_stavke(rnd, n=None):
    out = []
    for _ in range(n or rnd.randint(1, 30)):
        kol = rnd.choice(KOLICINE_KG + (1, 2, 3, round(rnd.uniform(0.001, 9.999), 3)))
        pop = rnd.choice((0, 0, 0, 5, 10, 15, 33.3, 50, 99, 100))
        out.append({"naziv": "x", "iznos": baza.zaokruzi(rnd.choice(CIJENE) * kol * (1 - pop / 100.0)),
                    "pdv_stopa": rnd.choice(STOPE)})
    return out


class FormulaBezBaze(unittest.TestCase):
    """_pdv_stavke + provjeri_formulu na stotinama tisuća slučajnih računa."""

    def test_slucajni_racuni(self):
        rnd = random.Random(166)
        for i in range(50000):
            st = _slucajne_stavke(rnd)
            if rnd.random() < 0.2:  # storno
                for s in st:
                    s["iznos"] = -s["iznos"]
            uk = sum(s["iznos"] for s in st)
            pdv = fiskal._pdv_stavke(st, True)
            # Porezna: |ukupno - zbroj(osnovica + PDV)| <= 0,01; mi tražimo točno
            self.assertEqual(sum(o + p for _, o, p in pdv), uk, st)
            for stopa, osn, p in pdv:
                bruto = sum(s["iznos"] for s in st if s["pdv_stopa"] == stopa)
                self.assertEqual(osn + p, bruto)
            fiskal.provjeri_formulu(uk, st, pdv, True)

    def test_racuni_iz_robe_koji_su_imali_166(self):
        # Stvarni računi iz Robe s greškom 166 (2026.): kod nas prolaze točno.
        rn50 = [(286, 5.0), (100, 5.0), (250, 5.0)]          # Robin zbroj 6,38, ukupno 6,36
        rn111 = [(283, 5.0), (299, 5.0), (234, 5.0), (79, 25.0), (194, 5.0), (198, 5.0)]
        for r in (rn50, rn111):
            st = [{"naziv": "x", "iznos": i, "pdv_stopa": s} for i, s in r]
            uk = sum(i for i, _ in r)
            pdv = fiskal._pdv_stavke(st, True)
            self.assertEqual(sum(o + p for _, o, p in pdv), uk)
            fiskal.provjeri_formulu(uk, st, pdv, True)

    def test_kriva_osnovica_se_odbija(self):
        st = [{"naziv": "PUPČAR", "iznos": 286, "pdv_stopa": 5.0}]
        # kako je slala Roba: osnovica 2,73 + PDV 0,14 = 2,87 za 2,86
        with self.assertRaises(fiskal.FormulaGreska):
            fiskal.provjeri_formulu(286, st, [(5.0, 273, 14)], True)

    def test_krivi_pdv_se_odbija(self):
        st = [{"naziv": "a", "iznos": 1000, "pdv_stopa": 25.0}]
        with self.assertRaises(fiskal.FormulaGreska):  # zbroj štima, ali PDV nije 25 %
            fiskal.provjeri_formulu(1000, st, [(25.0, 900, 100)], True)

    def test_zbroj_stavki_mora_biti_ukupno(self):
        st = [{"naziv": "a", "iznos": 100, "pdv_stopa": 5.0}, {"naziv": "b", "iznos": 50, "pdv_stopa": 25.0}]
        for krivo in (149, 151, 0):
            with self.assertRaises(fiskal.FormulaGreska):
                fiskal.provjeri_formulu(krivo, st, None, True)

    def test_stopa_viska_ili_manjka(self):
        st = [{"naziv": "a", "iznos": 105, "pdv_stopa": 5.0}]
        with self.assertRaises(fiskal.FormulaGreska):
            fiskal.provjeri_formulu(105, st, [(5.0, 100, 5), (25.0, 0, 0)], True)

    def test_placanja_moraju_biti_ukupno(self):
        st = [{"naziv": "a", "iznos": 1000, "pdv_stopa": 5.0}]
        fiskal.provjeri_formulu(1000, st, None, True, [600, 400])
        with self.assertRaises(fiskal.FormulaGreska):
            fiskal.provjeri_formulu(1000, st, None, True, [600, 399])

    def test_iznos_mora_biti_cijeli_cent(self):
        with self.assertRaises(fiskal.FormulaGreska):
            fiskal.provjeri_formulu(100.5, [{"naziv": "a", "iznos": 100.5, "pdv_stopa": 5.0}], None, True)

    def test_racun_xml_odbija_krivi_racun(self):
        f = {"oib": "12345678903", "u_pdv": 1, "dat_vrijeme": "23.11.2026T10:00:00", "broj": 1,
             "prostor": "P1", "uredjaj": "2", "ukupno": 287, "nacin": "G", "oib_operatera": "12345678903",
             "zki": "0" * 32, "nak_dost": 0}
        with self.assertRaises(fiskal.FormulaGreska):
            fiskal.racun_xml(f, [{"naziv": "a", "iznos": 286, "pdv_stopa": 5.0}])
        f["ukupno"] = 286
        x = fiskal.racun_xml(f, [{"naziv": "a", "iznos": 286, "pdv_stopa": 5.0}])
        self.assertIn("<tns:Osnovica>2.72</tns:Osnovica><tns:Iznos>0.14</tns:Iznos>", x)
        self.assertIn("<tns:IznosUkupno>2.86</tns:IznosUkupno>", x)


class NaplataNaKasi(unittest.TestCase):
    """Cijeli put kroz kasu: kg artikli, popusti, više stopa, podijeljeno plaćanje, storno."""

    def setUp(self):
        self.mapa = tempfile.mkdtemp()
        self.con = baza.spoji(os.path.join(self.mapa, "test.db"))
        postavke.spremi(self.con, {"podijeljeno_placanje": True})
        with self.con:
            self.con.execute("INSERT INTO nacini_placanja (id, naziv) VALUES (1, 'Gotovina'), (2, 'Kartica')")
            rnd = random.Random(5)
            for i in range(1, 41):
                self.con.execute(
                    "INSERT INTO artikli (id, sifra, naziv, jm, cijena, pdv_stopa) VALUES (?, ?, ?, ?, ?, ?)",
                    (i, str(i), "Artikl %d" % i, "Kg" if i % 3 else "Kom", rnd.choice(CIJENE), STOPE[i % 4]))

    def tearDown(self):
        self.con.close()

    def _provjera(self, c, rid):
        r = c.execute("SELECT ukupno FROM racuni WHERE id = ?", (rid,)).fetchone()
        fiskal._provjeri_racun(c, rid, r[0], 1)  # isto što radi fiskal.pri_naplati prije izdavanja

    def test_naplate(self):
        rnd = random.Random(2026)
        for n in range(400):
            stavke = [{"artikl_id": rnd.randint(1, 40),
                       "kolicina": rnd.choice(KOLICINE_KG + (1, 2, 5)),
                       "popust": rnd.choice((0, 0, 10, 15, 33.3, 50))} for _ in range(rnd.randint(1, 15))]
            podaci = {"stavke": stavke, "nacin_placanja_id": 1}
            rid = kasa.naplati(self.con, podaci, self._provjera)
            uk = self.con.execute("SELECT ukupno FROM racuni WHERE id = ?", (rid,)).fetchone()[0]
            if n % 3 == 0 and uk > 1:  # isti račun, podijeljeno plaćanje gotovina + kartica
                dio = rnd.randint(1, uk - 1)
                podaci["dijelovi"] = [{"nacin_placanja_id": 1, "iznos": dio},
                                      {"nacin_placanja_id": 2, "iznos": uk - dio}]
                rid = kasa.naplati(self.con, podaci, self._provjera)
            if n % 7 == 0:
                kasa.storniraj(self.con, rid, self._provjera)
        self.assertGreater(self.con.execute("SELECT COUNT(*) FROM racuni").fetchone()[0], 400)

    def test_krivi_racun_se_ne_izdaje(self):
        # Ako bi provjera javila grešku, naplata se poništava: nema računa u bazi.
        prije = self.con.execute("SELECT COUNT(*) FROM racuni").fetchone()[0]

        def pokvari_pa_provjeri(c, rid):
            c.execute("UPDATE racuni SET ukupno = ukupno + 2 WHERE id = ?", (rid,))
            self._provjera(c, rid)
        with self.assertRaises(fiskal.FormulaGreska):
            kasa.naplati(self.con, {"stavke": [{"artikl_id": 1, "kolicina": 0.638}], "nacin_placanja_id": 1},
                         pokvari_pa_provjeri)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM racuni").fetchone()[0], prije)


if __name__ == "__main__":
    unittest.main()
