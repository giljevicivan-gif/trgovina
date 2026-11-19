# -*- coding: utf-8 -*-
"""
Kasa - naš program za trgovinu, prva verzija.

VAŽNO: ovaj program NE izdaje fiskalizirane račune. Svaka prodaja se naplaćuje i
fiskalizira u programu Roba (Zakon o fiskalizaciji, NN 89/2025, čl. 13 i 14).
Naša kasa služi samo za vježbu (isključuje se s TRGOVINA_KASA=0 u pokreni.bat),
a njezin ispis nosi oznaku "OVO NIJE FISKALIZIRANI RAČUN" (čl. 29) i ne daje se kupcu.
Iznimka: modul "fiskalizacija" (fiskal.py, zadano isključen) šalje račune Poreznoj,
prvo na testnu okolinu s demo certifikatom; prava fiskalizacija samo uz izričitu potvrdu.

Pokretanje:
    python kasa.py                 (baza trgovina.db pokraj skripte)
    python kasa.py "C:\\Roba"       (prvo uveze svježe podatke iz Robe)

Kartice: Kasa, Računi, Pregled dana, Nabava (primke, otpisi), Zaliha (s inventurom),
Izvještaji (izvjestaji.py).
Nabava i zaliha su u nabava.py.

Otvara se preglednik na http://127.0.0.1:8766
Treba samo Python 3.8+ (za fiskalizaciju još paket cryptography).
"""

import datetime as dt
import json
import os
import re
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import akcije
import blagajna
import danas as jutro
import haccp
import haccp_mobitel
import kupci_tvrtke
import rokovi
import bonovi
import dugovanja
import fiskal
import ponude
import roba_dodatno
import cijene
import eracun
import vaga
import dodaci
import inventura
import izvjestaji
import kontrola
import knjigovodja
import kopija
import kopija_van
import donacije
import usporedba
import nabava
import nadogradnja
import narudzbe
import narudzba_mail
import uplata
import postavke
import povrat_ambalaze
import zaslon_kupca
import tisak
from baza import DB_PATH, ZALIHA_SQL, pdv_iz_bruto, spoji, zaokruzi
from nabava import Greska

PORT = int(os.environ.get("TRGOVINA_PORT") or 8766)
ROBA_MAPA = None  # mapa programa Roba (iz naredbenog retka), za "Osvježi iz Robe"
NAZIV_TRGOVINE = os.environ.get("TRGOVINA_NAZIV", "Trgovina")
# Kasa za vježbu (nefiskalna); TRGOVINA_KASA=0 je potpuno sakrije
KASA_VJEZBA = os.environ.get("TRGOVINA_KASA", "").strip() != "0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def sada():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- Podaci

def artikli(con):
    rows = con.execute(
        """SELECT a.id, a.sifra, a.barkod, a.naziv, a.jm, a.cijena, a.pdv_stopa, a.usluga,
                  round(%s, 3) AS zaliha
           FROM artikli a WHERE a.aktivan = 1 ORDER BY a.naziv""" % ZALIHA_SQL).fetchall()
    return [dict(r) for r in rows]


def najprodavaniji(con, n=24):
    """Brzi gumbi: najprodavaniji artikli zadnjih 60 dana (Roba + naša kasa)."""
    od = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    rows = con.execute(
        """SELECT a.id, COUNT(*) AS n FROM (
               SELECT a.id FROM roba_stavke s JOIN artikli a ON a.roba_sifra = s.artikl
               WHERE s.status = 3 AND s.datum >= ?
               UNION ALL
               SELECT s.artikl_id FROM racun_stavke s JOIN racuni r ON r.id = s.racun_id
               WHERE r.vrijeme >= ?
           ) a JOIN artikli x ON x.id = a.id AND x.aktivan = 1
           GROUP BY a.id ORDER BY n DESC LIMIT ?""", (od, od, n)).fetchall()
    if len(rows) < n:
        # podaci iz Robe mogu biti stari; onda uzmi najprodavanije ikad
        rows = con.execute(
            """SELECT a.id, COUNT(*) AS n FROM roba_stavke s
               JOIN artikli a ON a.roba_sifra = s.artikl AND a.aktivan = 1
               WHERE s.status = 3 GROUP BY a.id ORDER BY n DESC LIMIT ?""", (n,)).fetchall()
    return [r["id"] for r in rows]


def placanja(con):
    return [dict(r) for r in con.execute(
        "SELECT id, naziv FROM nacini_placanja WHERE aktivan = 1 ORDER BY roba_sifra, id")]


def racun(con, rid):
    r = con.execute(
        """SELECT r.*, p.naziv AS placanje, o.godina AS o_godina, o.broj AS o_broj
           FROM racuni r JOIN nacini_placanja p ON p.id = r.nacin_placanja_id
           LEFT JOIN racuni o ON o.id = r.storno_od WHERE r.id = ?""", (rid,)).fetchone()
    if not r:
        raise Greska("Račun ne postoji.")
    out = dict(r)
    out["oznaka"] = oznaka(r)
    out["placanje"] = opis_placanja(con, rid, r["placanje"])
    out["kupac"] = eracun.kupac_za_ispis(con, r["kupac_id"])
    out["stavke"] = [dict(s) for s in con.execute(
        "SELECT * FROM racun_stavke WHERE racun_id = ? ORDER BY id", (rid,))]
    pdv = {}
    for s in out["stavke"]:
        pdv[s["pdv_stopa"]] = pdv.get(s["pdv_stopa"], 0) + s["iznos"]
    # PDV storna je točno suprotan PDV-u izvornog računa (zaokruživanje od nule)
    out["pdv"] = []
    for k, v in sorted(pdv.items()):
        p = pdv_iz_bruto(abs(v), k) * (1 if v >= 0 else -1)
        out["pdv"].append({"stopa": k, "osnovica": v - p, "pdv": p})
    out["fiskal"] = fiskal.za_ispis(con, rid)
    if out["fiskal"] and r["storno_od"]:
        o = fiskal.zapis(con, r["storno_od"])
        out["fiskal"]["storno_od"] = fiskal.oznaka(o) if o else ""
    return out


def oznaka(r):
    return "K-%d-%d" % (r["godina"], r["broj"])


def racuni_dana(con, datum):
    rows = con.execute(
        """SELECT r.id, r.godina, r.broj, r.vrijeme, r.ukupno, r.storniran, r.storno_od,
                  p.naziv AS placanje,
                  (SELECT COUNT(*) FROM racun_stavke s WHERE s.racun_id = r.id) AS stavki
           FROM racuni r JOIN nacini_placanja p ON p.id = r.nacin_placanja_id
           WHERE substr(r.vrijeme, 1, 10) = ? ORDER BY r.id DESC""", (datum,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["oznaka"] = oznaka(r)
        d["placanje"] = opis_placanja(con, r["id"], r["placanje"])
        out.append(d)
    return fiskal.dodaj_u_popis(con, out)


def pregled_dana(con, datum):
    po_placanju = [dict(r) for r in con.execute(
        """SELECT p.naziv, SUM(r.storno_od IS NULL AND r.storniran = 0) AS n, SUM(r.ukupno) AS iznos
           FROM racuni r JOIN nacini_placanja p ON p.id = r.nacin_placanja_id
           WHERE substr(r.vrijeme, 1, 10) = ? GROUP BY p.id ORDER BY iznos DESC""", (datum,))]
    # PDV dana = zbroj PDV-a s pojedinih računa (kako piše na računima), a ne PDV na
    # dnevni zbroj, koji se od toga može razlikovati za koji cent
    po_stopi = {}
    for s in con.execute(
            """SELECT s.pdv_stopa, SUM(s.iznos) AS iznos FROM racun_stavke s
               JOIN racuni r ON r.id = s.racun_id
               WHERE substr(r.vrijeme, 1, 10) = ? GROUP BY r.id, s.pdv_stopa""", (datum,)):
        z = po_stopi.setdefault(s["pdv_stopa"], [0, 0])
        z[0] += s["iznos"]
        z[1] += pdv_iz_bruto(s["iznos"], s["pdv_stopa"])
    pdv = [{"stopa": k, "osnovica": v[0] - v[1], "pdv": v[1], "iznos": v[0]}
           for k, v in sorted(po_stopi.items())]
    zarada = con.execute(
        """SELECT SUM(s.iznos - s.iznos * s.pdv_stopa / (100 + s.pdv_stopa)
                      - s.nabavna * s.kolicina) FROM racun_stavke s
           JOIN racuni r ON r.id = s.racun_id
           WHERE substr(r.vrijeme, 1, 10) = ? AND s.nabavna > 0""", (datum,)).fetchone()[0]
    ukupno = sum(p["iznos"] for p in po_placanju)
    broj = con.execute(
        """SELECT COUNT(*) FROM racuni WHERE substr(vrijeme, 1, 10) = ?
           AND storno_od IS NULL AND storniran = 0""", (datum,)).fetchone()[0]
    return {"datum": datum, "ukupno": ukupno, "racuna": broj,
            "po_placanju": po_placanju, "pdv": pdv, "zarada": zaokruzi(zarada)}


# ---------------------------------------------------------------- Zapis

def _sljedeci_broj(con, godina):
    return con.execute("SELECT COALESCE(MAX(broj), 0) + 1 FROM racuni WHERE godina = ?",
                       (godina,)).fetchone()[0]


def _fiskal_posalji(con, rid):
    """Račun je već spremljen: greška pri slanju ne smije izgledati kao neuspjela naplata."""
    try:
        return fiskal.posalji(con, rid)
    except Exception as e:
        traceback.print_exc()
        return {"status": "ceka", "poruka": "Slanje nije uspjelo (%s), šalje se ponovno samo." % e, "jir": ""}


_NAPLATE = {}  # oznaka naplate iz preglednika -> id računa (dok kasa radi)
_NAPLATE_BRAVA = threading.Lock()


def _dijelovi_placanja(con, dijelovi, ukupno):
    """[(nacin_id, iznos_centi), ...] za račun plaćen s više načina; [] = jedan način."""
    if not dijelovi:
        return []
    if not postavke.ukljucen(con, "podijeljeno_placanje"):
        raise Greska("Plaćanje s više načina nije uključeno (Postavke).")
    out = {}
    try:
        for d in dijelovi:
            nid, izn = int(d["nacin_placanja_id"]), int(d["iznos"])
            if izn < 0 or not con.execute("SELECT 1 FROM nacini_placanja WHERE id = ?", (nid,)).fetchone():
                raise ValueError
            if izn:
                out[nid] = out.get(nid, 0) + izn
    except (TypeError, ValueError, KeyError):
        raise Greska("Neispravni dijelovi plaćanja.")
    if sum(out.values()) != ukupno:
        raise Greska("Dijelovi plaćanja (%s) nisu jednaki iznosu računa (%s)." % (eur(sum(out.values())), eur(ukupno)))
    return list(out.items()) if len(out) > 1 else []


def opis_placanja(con, rid, naziv):
    """'Gotovina' ili 'Gotovina 10,00 + Kartica 5,35' za račun s više načina plaćanja."""
    dio = con.execute(
        """SELECT p.naziv, x.iznos FROM racun_placanja x JOIN nacini_placanja p ON p.id = x.nacin_placanja_id
           WHERE x.racun_id = ? ORDER BY x.id""", (rid,)).fetchall()
    return " + ".join("%s %s" % (d[0], eur(d[1])) for d in dio) if dio else naziv


def naplati(con, podaci, prije_commita=None):
    stavke = podaci.get("stavke") or []
    if not stavke:
        raise Greska("Račun nema stavki.")
    try:
        pid = int(podaci.get("nacin_placanja_id"))
    except (TypeError, ValueError):
        raise Greska("Odaberite način plaćanja.")
    con.execute("BEGIN IMMEDIATE")  # jedan po jedan, da broj računa bude bez rupa
    try:
        if not con.execute("SELECT 1 FROM nacini_placanja WHERE id = ?", (pid,)).fetchone():
            raise Greska("Nepoznat način plaćanja.")
        redovi = []
        for s in stavke:
            a = con.execute("SELECT * FROM artikli WHERE id = ?", (int(s["artikl_id"]),)).fetchone()
            if not a:
                raise Greska("Artikl ne postoji.")
            try:
                kol = round(float(s["kolicina"]), 3)
                popust = float(s.get("popust") or 0)
            except (TypeError, ValueError, KeyError):
                raise Greska("Neispravna količina ili popust za %s." % a["naziv"])
            if not 0 < kol <= 100000:  # odbija i NaN
                raise Greska("Neispravna količina za %s." % a["naziv"])
            if not 0 <= popust <= 100:
                raise Greska("Popust mora biti između 0 i 100 %.")
            iznos = zaokruzi(a["cijena"] * kol * (1 - popust / 100.0))
            redovi.append((a, kol, popust, iznos))
        ukupno = sum(r[3] for r in redovi)
        dijelovi = _dijelovi_placanja(con, podaci.get("dijelovi"), ukupno)
        kupac_id = None
        if podaci.get("kupac_id"):  # R1 račun: kupac tvrtka
            postavke.provjeri(con, "r1_kasa")
            kupac_id = eracun.provjeri_kupca(con, podaci["kupac_id"])
        if dijelovi:
            pid = dijelovi[0][0]  # glavni način (prvi dio); svi dijelovi su u racun_placanja
        vrijeme = sada()
        godina = int(vrijeme[:4])
        broj = _sljedeci_broj(con, godina)
        cur = con.execute(
            """INSERT INTO racuni (godina, broj, vrijeme, ukupno, nacin_placanja_id, napomena, kupac_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (godina, broj, vrijeme, ukupno, pid, str(podaci.get("napomena") or "")[:200], kupac_id))
        rid = cur.lastrowid
        for nid, izn in dijelovi:
            con.execute("INSERT INTO racun_placanja (racun_id, nacin_placanja_id, iznos) VALUES (?, ?, ?)",
                        (rid, nid, izn))
        for a, kol, popust, iznos in redovi:
            con.execute(
                """INSERT INTO racun_stavke (racun_id, artikl_id, naziv, jm, kolicina, cijena,
                                             popust, iznos, pdv_stopa, nabavna)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rid, a["id"], a["naziv"], a["jm"], kol, a["cijena"], popust, iznos,
                 a["pdv_stopa"], a["nabavna"]))
        if prije_commita:
            prije_commita(con, rid)  # fiskalizacija: broj i ZKI u istoj transakciji
        con.execute("COMMIT")
        return rid
    except Exception:
        con.execute("ROLLBACK")
        raise


def storniraj(con, rid, prije_commita=None):
    con.execute("BEGIN IMMEDIATE")
    try:
        r = con.execute("SELECT * FROM racuni WHERE id = ?", (rid,)).fetchone()
        if not r:
            raise Greska("Račun ne postoji.")
        if r["storniran"]:
            raise Greska("Račun je već storniran.")
        if r["storno_od"]:
            raise Greska("Storno račun se ne može stornirati.")
        vrijeme = sada()
        godina = int(vrijeme[:4])
        cur = con.execute(
            """INSERT INTO racuni (godina, broj, vrijeme, ukupno, nacin_placanja_id, storno_od,
                                   napomena, kupac_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (godina, _sljedeci_broj(con, godina), vrijeme, -r["ukupno"],
             r["nacin_placanja_id"], rid, "Storno računa %s" % oznaka(r), r["kupac_id"]))
        novi = cur.lastrowid
        con.execute("""INSERT INTO racun_placanja (racun_id, nacin_placanja_id, iznos)
                       SELECT ?, nacin_placanja_id, -iznos FROM racun_placanja WHERE racun_id = ?""", (novi, rid))
        for s in con.execute("SELECT * FROM racun_stavke WHERE racun_id = ?", (rid,)).fetchall():
            con.execute(
                """INSERT INTO racun_stavke (racun_id, artikl_id, naziv, jm, kolicina, cijena,
                                             popust, iznos, pdv_stopa, nabavna)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (novi, s["artikl_id"], s["naziv"], s["jm"], -s["kolicina"], s["cijena"],
                 s["popust"], -s["iznos"], s["pdv_stopa"], s["nabavna"]))
        con.execute("UPDATE racuni SET storniran = 1 WHERE id = ?", (rid,))
        if prije_commita:
            prije_commita(con, novi)
        con.execute("COMMIT")
        return novi
    except Exception:
        con.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------- Web

# Putanje dodatnih modula; kad je modul isključen u Postavkama, API ih odbija.
PUTANJE_MODULA = (
    ("/api/narudzb", "narudzbe"), ("/api/kupac", "narudzbe"), ("/ispis_narudzba/", "narudzbe"),
    ("/api/inv/otvori", "inventura_mobitel"), ("/api/inv/pokreni", "inventura_mobitel"),
    ("/api/akcije", "akcije"), ("/api/usporedba", "usporedba"),
    ("/api/haccp", "haccp"), ("/ispis_haccp", "haccp"),
    ("/api/kupci_", "narudzbe"), ("/api/kupci_", "kupci_tvrtke"),
    ("/ispis_zbirni", "narudzbe"), ("/ispis_zbirni", "kupci_tvrtke"),
    ("/api/rokovi", "rokovi"), ("/api/kalo", "rokovi"),
    ("/api/bon", "bonovi"), ("/ispis_bon", "bonovi"),
    ("/api/vjernost", "vjernost"), ("/ispis_pristupnica", "vjernost"),
    ("/api/dugovanja", "dugovanja"),
    ("/api/kpd", "kpd"), ("/api/r1", "r1_kasa"), ("/api/eracun", "eracun_uvoz"),
    ("/api/tisak/", "termo_pisac"),
    ("/api/ponud", "narudzbe"), ("/api/ponud", "ponude"), ("/ispis_ponuda", "narudzbe"), ("/ispis_ponuda", "ponude"),
    ("/api/zpc", "zpc"), ("/ispis_zpc", "zpc"),
    ("/api/povrat_amb", "povrat_ambalaze"), ("/ispis_povrat_amb", "povrat_ambalaze"), ("/povrat_amb", "povrat_ambalaze"),
    ("/api/zaslon", "zaslon_kupca"), ("/zaslon", "zaslon_kupca"),
    ("/api/vaga", "vaga_veza"),
    ("/api/uplata", "qr_uplata"), ("/api/dob_mail", "narudzba_mail"), ("/ispis_narudzba_dob", "narudzba_mail"),
    ("/api/kopija_van", "kopija_van"), ("/api/donacij", "donacije"), ("/ispis_donacija", "donacije"),
)


def provjeri_modul(con, putanja):
    for pocetak, modul in PUTANJE_MODULA:
        if putanja.startswith(pocetak):
            postavke.provjeri(con, modul)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        body = dodaci.pri_slanju(self, code, body)  # radnici: skrivanje nabavnih cijena, dnevnik radnji
        if not isinstance(body, bytes):
            body = (json.dumps(body, ensure_ascii=False) if ctype.startswith("application/json")
                    else body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _csv(self, ime, tekst):
        tijelo = tekst.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % ime)
        self.send_header("Content-Length", str(len(tijelo)))
        self.end_headers()
        self.wfile.write(tijelo)

    def _handle(self, fn):
        con = spoji()
        con.isolation_level = None  # transakcije vodimo sami (BEGIN IMMEDIATE)
        try:
            fn(con)
        except Greska as e:
            self._send(400, {"greska": str(e)})
        except (ValueError, TypeError, KeyError, OverflowError) as e:
            # neispravan broj, datum ili polje u zahtjevu
            self._send(400, {"greska": "Neispravan podatak u zahtjevu (%s)." % e})
        except Exception as e:  # pragma: no cover
            import traceback
            traceback.print_exc()  # u prozoru programa, za prijavu greške
            self._send(500, {"greska": "Greška u programu: %s" % e})
        finally:
            con.close()

    def _obicno(self, code, tekst):
        b = tekst.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/verzija.txt":  # pokretač provjerava je li na portu naša kasa
            return self._obicno(200, "trgovina %s" % nadogradnja.POKRENUTA)
        if u.path in ("/zaslon", "/api/zaslon", "/kasa_kupac.js"):  # zaslon za kupca: često, bez prijave i baze
            return self._zaslon_get(u.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        danas = dt.date.today().isoformat()

        def go(con):
            if dodaci.get(self, con, u.path, q):  # dodatni moduli (dodaci.py) i prijava radnika
                return
            if u.path == "/api/postavke/nadogradnja":
                return self._send(200, nadogradnja.stanje(q.get("odmah") == "1"))
            provjeri_modul(con, u.path)
            if u.path == "/":
                self._send(200, STRANICA.replace("{{NAZIV}}", NAZIV_TRGOVINE),
                           "text/html; charset=utf-8")
            elif u.path == "/api/pocetak":
                self._send(200, {"artikli": artikli(con), "placanja": placanja(con),
                                 "brzi": najprodavaniji(con), "naziv": NAZIV_TRGOVINE,
                                 "kasa": KASA_VJEZBA, "moduli": postavke.moduli(con),
                                 "fiskal": fiskal.sve_postavke(con)["okolina"]
                                 if postavke.ukljucen(con, "fiskalizacija") else ""})
            elif u.path == "/api/postavke":
                self._send(200, postavke.popis(con))
            elif u.path == "/api/haccp":
                self._send(200, haccp.pregled(con))
            elif u.path == "/api/haccp/mobitel":
                self._send(200, haccp_mobitel.stanje(con))
            elif u.path == "/api/ponude":
                self._send(200, {"ponude": ponude.popis(con, q.get("sve") == "1"),
                                 "kupci": narudzbe.kupci(con)})
            elif re.fullmatch(r"/api/ponuda/\d+", u.path):
                self._send(200, ponude.ponuda(con, int(u.path.rsplit("/", 1)[1])))
            elif re.fullmatch(r"/ispis_ponuda/\d+", u.path):
                self._send(200, ispis_narudzba_html(ponude.ponuda(con, int(u.path.rsplit("/", 1)[1])), ponuda=True),
                           "text/html; charset=utf-8")
            elif u.path == "/api/zpc":
                self._send(200, roba_dodatno.zpc_popis(con, q.get("godina")))
            elif re.fullmatch(r"/api/zpc/\d+/\d+", u.path):
                g, s = u.path.split("/")[3:5]
                self._send(200, roba_dodatno.zpc(con, int(g), int(s)))
            elif re.fullmatch(r"/ispis_zpc/\d+/\d+", u.path):
                g, s = u.path.split("/")[2:4]
                self._send(200, ispis_zpc_html(roba_dodatno.zpc(con, int(g), int(s))), "text/html; charset=utf-8")
            elif u.path in ("/kopija_van.js", "/donacije.js"):
                with open(os.path.join(SCRIPT_DIR, u.path[1:]), encoding="utf-8") as f:
                    self._send(200, f.read(), "text/javascript; charset=utf-8")
            elif u.path == "/api/kopija_van":
                self._send(200, kopija_van.stanje(con))
            elif u.path == "/api/donacije":
                self._send(200, donacije.pregled(con, q.get("godina")))
            elif re.fullmatch(r"/api/donacija/\d+", u.path):
                self._send(200, donacije.donacija(con, int(u.path.rsplit("/", 1)[1])))
            elif re.fullmatch(r"/ispis_donacija/\d+", u.path):
                self._send(200, donacije.zapisnik_html(con, int(u.path.rsplit("/", 1)[1]), NAZIV_TRGOVINE),
                           "text/html; charset=utf-8")
            elif u.path == "/api/kpd/bez":
                self._send(200, {"artikli": eracun.bez_kpd(con)})
            elif re.fullmatch(r"/api/kpd/prijedlog/\d+", u.path):
                self._send(200, eracun.prijedlog_kpd(con, int(u.path.rsplit("/", 1)[1])))
            elif u.path == "/api/povrat_amb":
                self._send(200, {"vrste": povrat_ambalaze.vrste(con, q.get("sve") == "1"),
                                 "zadnje": povrat_ambalaze.zadnje(con),
                                 "danas": povrat_ambalaze.pregled(con, danas, danas),
                                 "smjena": bool(con.execute("SELECT 1 FROM smjene WHERE zatvorena IS NULL").fetchone())})
            elif u.path == "/api/povrat_amb/pregled":
                self._send(200, povrat_ambalaze.pregled(con, q.get("od"), q.get("do")))
            elif u.path == "/povrat_amb.csv":
                p = povrat_ambalaze.pregled(con, q.get("od"), q.get("do"))
                self._csv("povratna_naknada_%s_%s.csv" % (p["od"], p["do"]),
                          povrat_ambalaze.csv_pregled(con, p["od"], p["do"]))
            elif re.fullmatch(r"/ispis_povrat_amb/\d+", u.path):
                self._send(200, povrat_ambalaze.potvrda_html(con, int(u.path.rsplit("/", 1)[1]), NAZIV_TRGOVINE),
                           "text/html; charset=utf-8")
            elif u.path == "/api/zaslon/mreza":
                self._send(200, zaslon_kupca.stanje_mreze(con))
            elif u.path == "/api/vaga":  # očitanje vage za kasu (blagajnik smije)
                self._send(200, vaga.ocitaj(vaga.postavke(con)))
            elif u.path == "/api/vaga/postavke":
                self._send(200, {"postavke": vaga.postavke(con), "protokoli": vaga.opis_protokola(),
                                 "windows": os.name == "nt"})
            elif u.path == "/api/r1/kupci":
                self._send(200, {"kupci": eracun.kupci(con)})
            elif u.path.startswith("/api/tisak/"):  # termalni pisač (tisak.py)
                tisak.get(self, con, u.path, q, racun)
            elif re.fullmatch(r"/api/artikl_info/\d+", u.path):
                a = con.execute("SELECT * FROM artikli WHERE id = ?", (int(u.path.rsplit("/", 1)[1]),)).fetchone()
                if not a:
                    raise Greska("Artikl ne postoji.")
                m = postavke.moduli(con)
                self._send(200, {"kpd": a["kpd"], "kpd_naziv": roba_dodatno.kpd_naziv(con, a["kpd"]) if a["kpd"] else "",
                                 "roba_sidrena": a["roba_sidrena"], "roba_sidrena_datum": a["roba_sidrena_datum"],
                                 "sidrena_izvor": a["sidrena_izvor"],
                                 "povijest": roba_dodatno.povijest(con, a["roba_sifra"]) if m["zpc"] else None})
            elif u.path == "/api/rokovi":
                self._send(200, {"popis": rokovi.popis(con), "vrste": rokovi.VRSTE, "zatvaranje": rokovi.ZATVARANJE})
            elif u.path == "/api/kalo":
                od, do = izvjestaji.razdoblje(q.get("od"), q.get("do"))
                norma = postavke.vrijednost(con, "kalo.norma")
                self._send(200, rokovi.kalo(con, od, do, float(norma) if norma else None))
            elif u.path == "/api/bonovi":
                self._send(200, {"bonovi": bonovi.popis_bonova(con, q.get("svi") == "1"),
                                 "upozorenja": bonovi.upozorenja_bonova(con)})
            elif u.path == "/api/bon":
                self._send(200, bonovi.bon(con, kod=q.get("kod") or ""))
            elif re.fullmatch(r"/ispis_bon/\d+", u.path):
                self._send(200, ispis_bon_html(bonovi.bon(con, int(u.path.rsplit("/", 1)[1]))),
                           "text/html; charset=utf-8")
            elif u.path == "/api/vjernost":
                self._send(200, {"clanovi": bonovi.clanovi(con), "pravila": bonovi.pravila(con)})
            elif u.path == "/api/vjernost/clan":
                self._send(200, bonovi.clan(con, kartica=q.get("kartica") or ""))
            elif re.fullmatch(r"/ispis_pristupnica/\d+", u.path):
                self._send(200, ispis_pristupnica_html(bonovi.clan(con, int(u.path.rsplit("/", 1)[1]))),
                           "text/html; charset=utf-8")
            elif u.path == "/api/dugovanja":
                self._send(200, dugovanja.pregled(con, q.get("svi") != "1"))
            elif re.fullmatch(r"/api/dugovanja/racun/\d+", u.path):
                self._send(200, dugovanja.racun(con, int(u.path.rsplit("/", 1)[1])))
            elif u.path == "/ispis_haccp":
                self._send(200, ispis_haccp_html(haccp.mjesec(con, q.get("mjesec") or danas[:7])),
                           "text/html; charset=utf-8")
            elif u.path == "/api/racuni":
                self._send(200, racuni_dana(con, dt.date.fromisoformat(q.get("datum") or danas).isoformat()))
            elif u.path == "/api/dan":
                self._send(200, pregled_dana(con, dt.date.fromisoformat(q.get("datum") or danas).isoformat()))
            elif u.path == "/api/izvjestaj":
                try:
                    self._send(200, izvjestaji.izvjestaj(con, q.get("od"), q.get("do")))
                except ValueError as e:
                    raise Greska(str(e))
            elif re.fullmatch(r"/api/najniza/\d+", u.path):
                self._send(200, {"cijena": cijene.najniza_30(con, int(u.path.rsplit("/", 1)[1]))})
            elif u.path == "/api/inv":
                p = inventura.pregled(con)
                self._send(200, {"brojanje": p, "adresa": inventura.adresa() if p else "",
                                 "radi": inventura.radi()})
            elif re.fullmatch(r"/ispis_popisna/\d+", u.path):
                p = inventura.pregled(con, int(u.path.rsplit("/", 1)[1]))
                if not p:
                    raise Greska("Brojanje ne postoji.")
                self._send(200, ispis_popisna_html(p), "text/html; charset=utf-8")
            elif u.path == "/api/narudzbe":
                self._send(200, {"narudzbe": narudzbe.popis(con, q.get("sve") != "1", q.get("mjesec")),
                                 "kupci": narudzbe.kupci(con, svi=True)})
            elif re.fullmatch(r"/api/kupci_cjenik/\d+", u.path):
                self._send(200, kupci_tvrtke.cjenik(con, int(u.path.rsplit("/", 1)[1])))
            elif re.fullmatch(r"/api/kupci_zbirni/\d+", u.path):
                self._send(200, kupci_tvrtke.zbirni(con, int(u.path.rsplit("/", 1)[1]), q.get("mjesec")))
            elif re.fullmatch(r"/ispis_zbirni/\d+", u.path):
                z = kupci_tvrtke.zbirni(con, int(u.path.rsplit("/", 1)[1]), q.get("mjesec"))
                self._send(200, uplata.za_zbirni(con, z, ispis_zbirni_html(z)), "text/html; charset=utf-8")
            elif re.fullmatch(r"/api/narudzba/\d+", u.path):
                self._send(200, narudzbe.narudzba(con, int(u.path.rsplit("/", 1)[1])))
            elif re.fullmatch(r"/ispis_narudzba/\d+", u.path):
                d = narudzbe.narudzba(con, int(u.path.rsplit("/", 1)[1]))
                h = ispis_narudzba_html(d, q.get("vrsta") == "otpremnica")
                if q.get("vrsta") == "otpremnica":
                    h = uplata.za_otpremnicu(con, d, h)
                self._send(200, h, "text/html; charset=utf-8")
            elif u.path == "/api/uplata":
                self._send(200, uplata.postavke_modula(con))
            elif re.fullmatch(r"/api/dob_mail/\d+", u.path):
                self._send(200, narudzba_mail.priprema(con, int(u.path.rsplit("/", 1)[1])))
            elif u.path == "/api/dob_mail":
                self._send(200, {"narudzbe": narudzba_mail.popis(con, 30)})
            elif re.fullmatch(r"/ispis_narudzba_dob/\d+", u.path):
                self._send(200, narudzba_mail.ispis(con, int(u.path.rsplit("/", 1)[1])), "text/html; charset=utf-8")
            elif u.path == "/api/danas":
                self._send(200, jutro.pregled(con))
            elif u.path == "/api/usporedba":
                self._send(200, usporedba.usporedba(con, q.get("dana") or 90, q.get("sve") == "1"))
            elif u.path == "/api/akcije":
                self._send(200, {"prijedlog": akcije.prijedlog(con, q.get("dana") or akcije.DANA_BEZ_PRODAJE,
                                                               q.get("popust") or akcije.POPUST),
                                 "aktivne": akcije.aktivne(con)})
            elif u.path == "/api/kontrola":
                self._send(200, kontrola.provjere(con))
            elif u.path == "/api/blagajna":
                self._send(200, {"smjena": blagajna.trenutna(con), "povijest": blagajna.popis(con),
                                 "roba": bool(ROBA_MAPA)})
            elif re.fullmatch(r"/api/smjena/\d+", u.path):
                self._send(200, blagajna.izracun(con, int(u.path.rsplit("/", 1)[1])))
            elif u.path == "/ispis_etikete":
                ids = [int(x) for x in (q.get("ids") or "").split(",") if x.isdigit()]
                self._send(200, ispis_etiketa_html([a for a in nabava.zaliha(con) if a["id"] in ids],
                                                   postavke.ukljucen(con, "voce_povrce")),
                           "text/html; charset=utf-8")
            elif u.path == "/cjenik.csv":
                self._csv("cjenik_%s.csv" % dt.datetime.now().strftime("%d.%m.%Y_%H-%M"),
                          cijene.cjenik_csv(nabava.zaliha(con)))
            elif u.path == "/ispis_knjigovodja":
                try:
                    p = knjigovodja.paket(con, q.get("od"), q.get("do"))
                except ValueError as e:
                    raise Greska(str(e))
                self._send(200, ispis_knjigovodja_html(p), "text/html; charset=utf-8")
            elif u.path in ("/knjigovodja_dokumenti.csv", "/knjigovodja_stavke.csv"):
                try:
                    od, do = izvjestaji.razdoblje(q.get("od"), q.get("do"))
                except ValueError as e:
                    raise Greska(str(e))
                fn = knjigovodja.csv_dokumenti if "dokumenti" in u.path else knjigovodja.csv_stavke
                self._csv("%s_%s_%s.csv" % (u.path[1:-4], od, do), fn(con, od, do))
            elif u.path == "/api/partneri":
                self._send(200, nabava.partneri(con, svi=True))
            elif u.path == "/api/prijedlog":
                try:
                    self._send(200, nabava.prijedlog(con, q.get("dana") or 28, q.get("za") or 3))
                except ValueError:
                    raise Greska("Neispravan broj dana.")
            elif u.path == "/api/kopije":
                self._send(200, kopija.popis())
            elif u.path == "/api/zaliha":
                self._send(200, {"artikli": nabava.zaliha(con), "partneri": nabava.partneri(con)})
            elif u.path == "/api/dokumenti":
                self._send(200, nabava.dokumenti(con, q.get("mjesec") or danas[:7],
                                                 q.get("vrsta")))
            elif re.fullmatch(r"/api/dokument/\d+", u.path):
                self._send(200, nabava.dokument(con, int(u.path.rsplit("/", 1)[1])))
            elif u.path == "/ispis_zaliha":
                self._send(200, ispis_zaliha_html(nabava.zaliha(con)), "text/html; charset=utf-8")
            elif re.fullmatch(r"/ispis_dok/\d+", u.path):
                try:
                    marza = float((q.get("marza") or "0").replace(",", "."))
                except ValueError:
                    marza = 0
                self._send(200, ispis_dok_html(nabava.dokument(con, int(u.path.rsplit("/", 1)[1])), marza),
                           "text/html; charset=utf-8")
            elif re.fullmatch(r"/api/racun/\d+", u.path):
                self._send(200, racun(con, int(u.path.rsplit("/", 1)[1])))
            elif re.fullmatch(r"/ispis/\d+", u.path):
                self._send(200, ispis_html(racun(con, int(u.path.rsplit("/", 1)[1]))),
                           "text/html; charset=utf-8")
            else:
                self._send(404, {"greska": "Nema"})
        self._handle(go)

    def _zaslon_get(self, put):
        if put == "/kasa_kupac.js":
            with open(os.path.join(SCRIPT_DIR, "kasa_kupac.js"), encoding="utf-8") as f:
                return self._send(200, f.read(), "text/javascript; charset=utf-8")
        if not zaslon_kupca.modul_ukljucen():
            return self._send(403, {"greska": "Zaslon za kupca je isključen (Postavke)."})
        if put == "/zaslon":
            return self._send(200, zaslon_kupca.stranica(), "text/html; charset=utf-8")
        return self._send(200, zaslon_kupca.procitaj())

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        if u.path == "/zatvori":  # pokreni.py --zatvori (ključ je u mapi podataka)
            kljuc = self.rfile.read(min(n, 200)).decode("utf-8", "replace").strip()
            try:
                with open(os.path.join(os.environ.get("TRGOVINA_PODACI", ""), "kasa_zatvori.kljuc"), encoding="utf-8") as f:
                    ispravan = kljuc and f.read().strip() == kljuc
            except OSError:
                ispravan = False
            if not ispravan:
                return self._obicno(403, "ne")
            self._obicno(200, "ok")
            threading.Timer(0.5, lambda: os._exit(0)).start()
            return
        try:
            podaci = json.loads(self.rfile.read(n) or b"{}", parse_constant=_bez_nan)
            if not isinstance(podaci, dict):
                raise ValueError
        except ValueError:
            return self._send(400, {"greska": "Neispravan zahtjev."})
        if u.path == "/api/zaslon":  # košarica za zaslon kupca (samo u memoriji, bez dnevnika radnji)
            if not zaslon_kupca.modul_ukljucen():
                return self._send(403, {"greska": "Zaslon za kupca je isključen."})
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._send(400, {"greska": "Neispravan zahtjev."})
            zaslon_kupca.postavi(podaci)
            return self._send(200, {"ok": True})

        def go(con):
            if dodaci.post(self, con, u.path, podaci):
                return
            if u.path == "/api/postavke/nadogradnja":
                try:
                    return self._send(200, nadogradnja.nadogradi())
                except Exception as e:
                    raise Greska("Nadogradnja nije uspjela: %s" % e)
            provjeri_modul(con, u.path)
            if u.path in ("/api/naplati",) or u.path.startswith("/api/storno/"):
                if not KASA_VJEZBA:
                    raise Greska("Kasa za vježbu je isključena. Prodaja se naplaćuje u Robi.")
            if u.path == "/api/naplati":
                # ista naplata poslana dvaput (izgubljen odgovor pa ponovni klik) ne smije dati dva računa
                oznaka = str(podaci.get("oznaka_naplate") or "")[:64]
                with _NAPLATE_BRAVA:
                    if oznaka and oznaka in _NAPLATE:
                        rid = _NAPLATE[oznaka]
                        odg = {"id": rid, "ponovljeno": True}
                        if postavke.ukljucen(con, "fiskalizacija"):
                            odg["fiskal"] = _fiskal_posalji(con, rid)
                        return self._send(200, odg)
                    if postavke.ukljucen(con, "fiskalizacija"):
                        fiskal.spreman(con)  # bez certifikata i OIB-a nema naplate
                        rid = naplati(con, podaci, lambda c, i: fiskal.pri_naplati(c, i, dodaci._ime(self), dodaci._oib(self)))
                        if oznaka:
                            _NAPLATE[oznaka] = rid
                        odg = {"id": rid, "fiskal": _fiskal_posalji(con, rid)}
                    else:
                        rid = naplati(con, podaci)
                        if oznaka:
                            _NAPLATE[oznaka] = rid
                        odg = {"id": rid}
                self._send(200, odg)
            elif u.path == "/api/uplata":
                self._send(200, uplata.spremi_postavke(con, podaci))
            elif u.path == "/api/dob_mail/pregled":
                self._send(200, narudzba_mail.pregled(con, podaci))
            elif u.path == "/api/dob_mail/posalji":
                self._send(200, narudzba_mail.posalji(con, podaci))
            elif u.path == "/api/smjena/otvori":
                self._send(200, {"id": blagajna.otvori(con, podaci)})
            elif u.path == "/api/smjena/promet":
                blagajna.promet(con, podaci)
                self._send(200, {"ok": True})
            elif u.path == "/api/smjena/zatvori":
                if ROBA_MAPA:
                    osvjezi_robu(con)
                self._send(200, {"id": blagajna.zatvori(con, podaci)})
                kopija_van.nakon_zakljucka()
            elif u.path == "/api/uvoz":
                if not ROBA_MAPA:
                    raise Greska("Mapa Robe nije zadana (pokrenite preko pokreni.bat).")
                self._send(200, {"opis": osvjezi_robu(con)})
            elif u.path == "/api/postavke":
                postavke.spremi(con, podaci)
                if not postavke.ukljucen(con, "inventura_mobitel") and inventura.radi():
                    inventura.zaustavi()  # isključen modul: mobitel se više ne može spojiti
                if postavke.ukljucen(con, "haccp"):
                    haccp_mobitel.pri_pokretanju(con)
                else:
                    haccp_mobitel.zaustavi()
                zaslon_kupca.pri_pokretanju(con)
                self._send(200, postavke.popis(con))
            elif u.path.startswith(("/api/rokovi/", "/api/kalo/", "/api/bonovi/", "/api/vjernost/",
                                    "/api/dugovanja/")):
                self._send(200, post_dodatni(con, u.path, podaci))
            elif u.path == "/api/haccp/mobitel":
                akcija = podaci.get("akcija")
                if akcija == "iskljuci":
                    haccp_mobitel.iskljuci(con)
                    greska = ""
                elif akcija in ("ukljuci", "novi_pin"):
                    greska = haccp_mobitel.ukljuci(con, novi_pin=akcija == "novi_pin")
                else:
                    raise Greska("Nepoznato.")
                self._send(200, dict(haccp_mobitel.stanje(con), greska_mreze=greska))
            elif u.path.startswith("/api/haccp/"):
                fn = {"temperatura": haccp.upisi_temperaturu, "ciscenje": haccp.upisi_ciscenje,
                      "prijem": haccp.upisi_prijem, "uredjaj": haccp.spremi_uredjaj,
                      "plan": haccp.spremi_plan, "ponisti": haccp.ponisti}.get(u.path[11:])
                if not fn:
                    raise Greska("Nepoznato.")
                self._send(200, {"id": fn(con, podaci)})
            elif u.path == "/api/akcije/pokreni":
                self._send(200, {"upis": akcije.pokreni(con, podaci.get("stavke") or [])})
            elif u.path == "/api/akcije/zavrsi":
                akcije.zavrsi(con, int(podaci.get("id") or 0))
                self._send(200, {"ok": True})
            elif u.path == "/api/inv/otvori":
                inventura.otvori(con)
                self._send(200, {"greska_mreze": inventura.pokreni()})
            elif u.path == "/api/inv/pokreni":
                self._send(200, {"greska_mreze": inventura.pokreni()})
            elif u.path == "/api/inv/zatvori":
                self._send(200, inventura.zatvori(con, podaci.get("napomena") or ""))
            elif u.path == "/api/inv/odustani":
                inventura.odustani(con)
                self._send(200, {"ok": True})
            elif u.path == "/api/inv/ponisti":
                s = inventura.trenutna(con)
                if not s:
                    raise Greska("Nema otvorenog brojanja.")
                inventura.ponisti(con, s["id"], podaci.get("id"))
                self._send(200, {"ok": True})
            elif u.path == "/api/kupac":
                self._send(200, {"id": narudzbe.spremi_kupca(con, podaci)})
            elif re.fullmatch(r"/api/kupci_(uvjeti|cijena|racun)/\d+", u.path):
                kid, sto = int(u.path.rsplit("/", 1)[1]), u.path.split("/")[2][6:]
                if sto == "uvjeti":
                    kupci_tvrtke.spremi_uvjete(con, kid, podaci)
                elif sto == "cijena":
                    kupci_tvrtke.spremi_cijenu(con, kid, podaci)
                else:
                    kupci_tvrtke.upisi_racun(con, kid, podaci)
                self._send(200, kupci_tvrtke.cjenik(con, kid))
            elif u.path == "/api/narudzba":
                self._send(200, {"id": narudzbe.spremi(con, podaci)})
            elif u.path == "/api/ponuda":
                self._send(200, {"id": ponude.spremi(con, podaci)})
            elif re.fullmatch(r"/api/ponuda/\d+/odbij", u.path):
                ponude.odbij(con, int(u.path.split("/")[3]))
                self._send(200, {"ok": True})
            elif re.fullmatch(r"/api/ponuda/\d+/narudzba", u.path):
                self._send(200, {"id": ponude.u_narudzbu(con, int(u.path.split("/")[3]), podaci)})
            elif re.fullmatch(r"/api/narudzba/\d+/status", u.path):
                narudzbe.status(con, int(u.path.split("/")[3]), podaci)
                self._send(200, {"ok": True})
            elif re.fullmatch(r"/api/kpd/\d+", u.path):
                eracun.spremi_kpd(con, int(u.path.rsplit("/", 1)[1]), podaci.get("kpd"))
                self._send(200, {"ok": True})
            elif u.path == "/api/povrat_amb/isplati":
                self._send(200, povrat_ambalaze.isplati(con, podaci, dodaci._ime(self)))
            elif u.path == "/api/povrat_amb/vrsta":
                self._send(200, {"id": povrat_ambalaze.spremi_vrstu(con, podaci)})
            elif u.path == "/api/povrat_amb/ponisti":
                self._send(200, povrat_ambalaze.ponisti(con, podaci.get("id")))
            elif u.path == "/api/zaslon/mreza":
                self._send(200, zaslon_kupca.spremi_mrezu(con, podaci))
            elif u.path == "/api/vaga/postavke":
                self._send(200, {"postavke": vaga.spremi_postavke(con, podaci)})
            elif u.path == "/api/vaga/test":  # probno očitanje: uvijek vraća i sirove bajtove
                try:
                    self._send(200, vaga.ocitaj(podaci))
                except Greska as e:
                    self._send(200, {"greska": str(e), "sirovo": getattr(e, "sirovo", None)})
            elif u.path == "/api/r1/kupac":
                self._send(200, {"id": eracun.novi_kupac(con, podaci)})
            elif u.path.startswith("/api/tisak/"):  # termalni pisač (tisak.py), poslije naplate
                tisak.post(self, con, u.path, podaci, racun)
            elif u.path == "/api/kopija_van/postavke":
                self._send(200, kopija_van.spremi_postavke(con, podaci))
            elif u.path == "/api/kopija_van/napravi":
                r = kopija_van.napravi("gumb")
                self._send(200 if r["ok"] else 400, r if r["ok"] else dict(r, greska=r["poruka"]))
            elif u.path == "/api/kopija_van/provjeri":
                self._send(200, kopija_van.provjeri(con))
            elif u.path == "/api/donacija":
                self._send(200, {"id": donacije.spremi(con, podaci, dodaci._ime(self))})
            elif u.path == "/api/donacije/prihod":
                self._send(200, donacije.spremi_prihod(con, podaci))
            elif u.path == "/api/eracun/procitaj":
                self._send(200, eracun.procitaj(con, podaci.get("xml") or ""))
            elif u.path == "/api/eracun/veze":
                self._send(200, {"spremljeno": eracun.spremi_veze(con, podaci)})
            elif u.path == "/api/partner":
                self._send(200, {"id": nabava.spremi_partner(con, podaci)})
            elif re.fullmatch(r"/api/artikl/\d+", u.path):
                nabava.uredi_artikl(con, int(u.path.rsplit("/", 1)[1]), podaci)
                self._send(200, {"ok": True})
            elif u.path == "/api/kopija":
                self._send(200, kopija.napravi())
            elif u.path == "/api/dokument":
                self._send(200, {"id": nabava.spremi(con, podaci)})
            elif re.fullmatch(r"/api/storno_dok/\d+", u.path):
                self._send(200, {"id": nabava.storniraj(con, int(u.path.rsplit("/", 1)[1]))})
            elif re.fullmatch(r"/api/storno/\d+", u.path):
                rid = int(u.path.rsplit("/", 1)[1])
                hook = fiskal_storno(con, rid, dodaci._ime(self), dodaci._oib(self))
                novi = storniraj(con, rid, hook)
                self._send(200, {"id": novi, "fiskal": _fiskal_posalji(con, novi) if hook else None})
            else:
                self._send(404, {"greska": "Nema"})
        self._handle(go)


def fiskal_storno(con, rid, operater="", oib_operatera=""):
    """Storno fiskalnog računa je novi fiskalni račun s minusom, u istoj okolini (test/produkcija).
    Vraća funkciju za storniraj() ili None (račun za vježbu ili s testa, storno samo kod nas)."""
    f = fiskal.zapis(con, rid)
    ukljucen = postavke.ukljucen(con, "fiskalizacija")
    okolina = fiskal.sve_postavke(con)["okolina"] if ukljucen else ""
    if f and f["okolina"] == "produkcija" and okolina != "produkcija":
        raise Greska("Račun je fiskaliziran u produkciji; storno se radi samo uz uključenu pravu fiskalizaciju.")
    if f and f["okolina"] == okolina:
        fiskal.spreman(con)
        return lambda c, i: fiskal.pri_naplati(c, i, operater, oib_operatera)
    return None


def _bez_nan(x):
    raise ValueError("NaN/Infinity")


def eur(c):
    s = "%.2f" % (c / 100.0)
    return s.replace(".", ",")


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def kupac_html(r):
    """R1: kupac tvrtka na računu (samo kad je odabran)."""
    k = r.get("kupac")
    if not k:
        return ""
    mjesto = ", ".join(x for x in (k["adresa"], k["mjesto"]) if x)
    return "<p><b>R1</b><br>Kupac: %s<br>%sOIB kupca: %s</p>" % (
        esc(k["naziv"]), (esc(mjesto) + "<br>") if mjesto else "", esc(k["oib"]))


def ispis_html(r):
    """Ispis za termalni printer 80 mm (ili bilo koji, preko dijaloga za ispis)."""
    red = []
    for s in r["stavke"]:
        kol = ("%.3f" % s["kolicina"]).rstrip("0").rstrip(".").replace(".", ",")
        pop = (" -%g%%" % s["popust"]).replace(".", ",") if s["popust"] else ""
        red.append("<tr><td colspan=2>%s</td></tr><tr><td>%s %s x %s%s</td><td class=r>%s</td></tr>"
                   % (esc(s["naziv"]), kol, esc(s["jm"]), eur(s["cijena"]), pop, eur(s["iznos"])))
    pdv = "".join("<tr><td>PDV %g%% na %s</td><td class=r>%s</td></tr>"
                  % (p["stopa"], eur(p["osnovica"]), eur(p["pdv"])) for p in r["pdv"])
    storno = ("<p class=c><b>STORNO računa K-%d-%d</b></p>" % (r["o_godina"], r["o_broj"])
              if r["storno_od"] else "")
    f = r.get("fiskal")
    if f:
        return ispis_fiskalni_html(r, f, "".join(red), pdv)
    return """<!doctype html><html lang=hr><head><meta charset=utf-8><title>%s</title>
<style>
@page { size: 80mm auto; margin: 3mm; }
body { font: 12px/1.35 'Courier New', monospace; width: 72mm; margin: 0 auto; color: #000; }
table { width: 100%%; border-collapse: collapse; } td { vertical-align: top; padding: 0; }
.r { text-align: right; white-space: nowrap; } .c { text-align: center; }
.tot td { font-size: 15px; font-weight: bold; border-top: 1px dashed #000; padding-top: 4px; }
.upoz { border: 2px solid #000; padding: 4px; text-align: center; font-weight: bold; margin: 6px 0; }
hr { border: 0; border-top: 1px dashed #000; }
@media screen { body { margin: 20px auto; } .gumbi { text-align: center; margin: 12px 0; } }
@media print { .gumbi { display: none; } }
</style></head><body>
<p class=c><b>%s</b></p>
<div class=upoz>OVO NIJE FISKALIZIRANI RAČUN<br>VJEŽBA - ne izdaje se kupcu</div>
%s
<p>Dokument: %s<br>Vrijeme: %s<br>Plaćanje: %s</p>%s
<hr><table>%s</table>
<table><tr class=tot><td>UKUPNO EUR</td><td class=r>%s</td></tr></table>
<hr><table>%s</table>
<div class=upoz>OVO NIJE FISKALIZIRANI RAČUN</div>
<div class=gumbi><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button></div>
<script>if (location.hash === '#ispis') { addEventListener('load', () => print()); }</script>
</body></html>""" % (esc(r["oznaka"]), esc(NAZIV_TRGOVINE), storno, esc(r["oznaka"]),
                     esc(dt.datetime.strptime(r["vrijeme"], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y. %H:%M:%S")),
                     esc(r["placanje"]), kupac_html(r), "".join(red), eur(r["ukupno"]), pdv)


def ispis_fiskalni_html(r, f, stavke, pdv):
    """Fiskalni račun (test ili pravi): podaci obveznika, broj, operater, ZKI, JIR i QR kod."""
    test = f["okolina"] == "test"
    upoz = ("<div class=upoz>OVO NIJE FISKALIZIRANI RAČUN<br>TEST FISKALIZACIJA (DEMO), NIJE VAŽEĆI RAČUN</div>"
            if test else "")
    if not f["jir"]:
        upoz += "<div class=upoz>Račun još nije potvrđen u Poreznoj (bez JIR-a).<br>Šalje se naknadno.</div>"
    storno = ("<p class=c><b>STORNO računa %s</b></p>" % esc(f.get("storno_od") or "K-%s-%s" % (r["o_godina"], r["o_broj"]))
              if r["storno_od"] else "")
    adresa = "<br>".join(esc(x) for x in f["adresa"].splitlines() if x.strip())
    d = dt.datetime.strptime(r["vrijeme"], "%Y-%m-%d %H:%M:%S")
    return """<!doctype html><html lang=hr><head><meta charset=utf-8><title>Račun %s</title>
<style>
@page { size: 80mm auto; margin: 3mm; }
body { font: 12px/1.35 'Courier New', monospace; width: 72mm; margin: 0 auto; color: #000; }
table { width: 100%%; border-collapse: collapse; } td { vertical-align: top; padding: 0; }
.r { text-align: right; white-space: nowrap; } .c { text-align: center; }
.tot td { font-size: 15px; font-weight: bold; border-top: 1px dashed #000; padding-top: 4px; }
.upoz { border: 2px solid #000; padding: 4px; text-align: center; font-weight: bold; margin: 6px 0; }
.sitno { font-size: 10px; word-break: break-all; }
hr { border: 0; border-top: 1px dashed #000; }
@media screen { body { margin: 20px auto; } .gumbi { text-align: center; margin: 12px 0; } }
@media print { .gumbi { display: none; } }
</style></head><body>
<p class=c><b>%s</b><br>%s%s</p>
%s%s
<p>Račun br. %s<br>Datum: %s<br>Operater: %s<br>Plaćanje: %s<br>Interni broj: %s</p>%s
<hr><table>%s</table>
<table><tr class=tot><td>UKUPNO EUR</td><td class=r>%s</td></tr></table>
<hr><table>%s</table>
%s
%s
<div class=gumbi><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button></div>
<script>if (location.hash === '#ispis') { addEventListener('load', () => print()); }</script>
</body></html>""" % (
        esc(f["oznaka"]), esc(f["naziv_tvrtke"] or NAZIV_TRGOVINE), (adresa + "<br>") if adresa else "",
        "OIB: " + esc(f["oib"]), upoz, storno, esc(f["oznaka"]), d.strftime("%d.%m.%Y. %H:%M:%S"),
        esc(f["operater"] or "-"), esc(r["placanje"]), esc(r["oznaka"]), kupac_html(r),
        stavke, eur(r["ukupno"]), pdv if f["u_pdv"] else "<tr><td>Obveznik nije u sustavu PDV-a.</td></tr>",
        fiskal.ispis_dio(f), upoz if test else "")


NASLOVI = {"primka": "PRIMKA", "otpis": "ZAPISNIK O OTPISU ROBE (kalo, kvar, lom)",
           "inventura": "INVENTURNA RAZLIKA",
           "povrat": "POVRAT ROBE DOBAVLJAČU"}


def ispis_popisna_html(p):
    """Popisna lista inventure: knjižno stanje, popisano, razlika; potpisuje komisija."""
    def kol(k):
        return ("%.3f" % k).rstrip("0").rstrip(".").replace(".", ",")
    red = "".join(
        "<tr><td>%d</td><td>%s</td><td>%s</td><td class=r>%s</td><td class=r>%s</td><td class=r>%s</td>"
        "<td class=r>%s</td><td class=r>%s</td></tr>"
        % (i, esc(s["naziv"]), esc(s["jm"]), kol(s["knjizno"]), kol(s["prebrojano"]), kol(s["razlika"]),
           eur(s["nabavna"]), eur(s["vrijednost_razlike"]))
        for i, s in enumerate(p["stavke"], 1))
    stanje = ("Zaključeno %s" % esc(p["zatvorena"])) if p["zatvorena"] else "BROJANJE JOŠ TRAJE"
    return """<!doctype html><html lang=hr><head><meta charset=utf-8><title>Popisna lista</title>
<style>
@page { size: A4; margin: 12mm; }
body { font: 12px/1.35 Arial, sans-serif; color: #000; max-width: 190mm; margin: 0 auto; }
h1 { font-size: 18px; margin: 0 0 4px; } .m { color: #444; }
table { width: 100%%; border-collapse: collapse; margin-top: 12px; }
th, td { padding: 4px 5px; border-bottom: 1px solid #bbb; text-align: left; }
th { border-bottom: 2px solid #000; font-size: 11px; } .r { text-align: right; white-space: nowrap; }
.tot { width: 50%%; margin-left: auto; } .b td { font-weight: bold; }
.pot { display: flex; justify-content: space-between; margin-top: 50px; }
.pot div { flex: 1; margin: 0 4%%; border-top: 1px solid #000; padding-top: 4px; text-align: center; }
@media screen { body { margin: 24px auto; } } @media print { .gumbi { display: none; } }
</style></head><body>
<p class=m>%s</p>
<h1>POPISNA LISTA (inventura)</h1>
<p>Brojanje otvoreno: %s · %s<br>Popisano artikala: %d. Artikli koji nisu brojani nisu mijenjani.</p>
<table><tr><th>#</th><th>Artikl</th><th>JM</th><th class=r>Stanje u programu</th><th class=r>Popisano</th>
<th class=r>Razlika</th><th class=r>Nab. cijena</th><th class=r>Vrijednost razlike</th></tr>%s</table>
<table class=tot><tr><td>Manjak po nabavnoj cijeni</td><td class=r>%s</td></tr>
<tr><td>Višak po nabavnoj cijeni</td><td class=r>%s</td></tr>
<tr class=b><td>Ukupna razlika</td><td class=r>%s</td></tr></table>
<div class=pot><div>Član komisije</div><div>Član komisije</div><div>Odgovorna osoba</div></div>
<p class=gumbi><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button></p>
</body></html>""" % (esc(NAZIV_TRGOVINE), esc(p["otvorena"]), stanje, len(p["stavke"]), red,
                     eur(p["manjak"]), eur(p["visak"]), eur(p["manjak"] + p["visak"]))


def ispis_knjigovodja_html(p):
    """A4 paket za knjigovođu: rekapitulacija, ulazni PDV, popis dokumenata, promet iz Robe."""
    def hr(iso):
        return knjigovodja._hr(iso)

    def stopa(s):
        return ("%g %%" % s).replace(".", ",")
    rek = "".join("<tr><td>%s</td><td class=r>%d</td><td class=r>%s</td><td class=r>%s</td></tr>"
                  % (esc(r["naslov"]), r["broj"], eur(r["neto"]), eur(r["pdv"])) for r in p["rekap"])
    ul = "".join("<tr><td>%s</td><td class=r>%s</td><td class=r>%s</td></tr>"
                 % (stopa(r["stopa"]), eur(r["osnovica"]), eur(r["pdv"])) for r in p["pdv_ulazni"]) \
        or "<tr><td colspan=3>Nema primki.</td></tr>"
    if p["roba_primke"]["broj"]:
        ul += ("<tr><td>Primke upisane u Robi, bez međuskladišnica, minus povratnice dobavljaču (%d)</td><td class=r>%s</td><td class=r>%s</td></tr>"
               % (p["roba_primke"]["broj"], eur(p["roba_primke"]["neto"]), eur(p["roba_primke"]["pdv"])))
    popisi = []
    for v, naslov in knjigovodja.VRSTE:
        dd = [d for d in p["dokumenti"] if d["vrsta"] == v]
        if not dd:
            continue
        red = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class=r>%s</td><td class=r>%s</td>"
            "<td class=r>%s</td><td>%s</td></tr>"
            % (esc(d["oznaka"]), hr(d["datum"]), esc(d["partner"]), esc(d["broj_dobavljaca"]),
               eur(d["neto"]), eur(d["pdv"]), eur(d["neto"] + d["pdv"]), esc(d["napomena"]))
            for d in dd)
        popisi.append("<h2>%s</h2><table><tr><th>Dokument</th><th>Datum</th><th>Dobavljač</th>"
                      "<th>Račun dobavljača</th><th class=r>Bez PDV</th><th class=r>PDV</th>"
                      "<th class=r>Ukupno</th><th>Napomena</th></tr>%s"
                      "<tr class=b><td colspan=4>Ukupno</td><td class=r>%s</td><td class=r>%s</td>"
                      "<td class=r>%s</td><td></td></tr></table>"
                      % (esc(naslov), red, eur(sum(d["neto"] for d in dd)), eur(sum(d["pdv"] for d in dd)),
                         eur(sum(d["neto"] + d["pdv"] for d in dd))))
    pr = "".join("<tr><td>%s</td><td class=r>%s</td><td class=r>%s</td><td class=r>%s</td><td class=r>%s</td></tr>"
                 % (stopa(r["stopa"]), eur(r["osnovica"]), eur(r["pdv"]), eur(r["ukupno"]), eur(r["razlika"]))
                 for r in p["promet"]) or "<tr><td colspan=5>Nema računa.</td></tr>"
    isp = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td class=r>%s</td><td>%s</td></tr>"
                  % (esc(n["oznaka"]), hr(n["rok"]), esc(n["kupac"]), eur(n["ukupno"]),
                     esc(n["racun_roba"]) or "<b>NEMA BROJA RAČUNA</b>") for n in p["isporuke"])
    isp = ("<h2>Isporučene narudžbe kupaca</h2><p class=m>Za svaku isporuku račun je izdan i "
           "fiskaliziran u Robi; ovdje je samo njegov broj.</p><table><tr><th>Narudžba</th>"
           "<th>Isporuka</th><th>Kupac</th><th class=r>Iznos</th><th>Račun u Robi</th></tr>%s</table>"
           % isp) if isp else ""
    return """<!doctype html><html lang=hr><head><meta charset=utf-8><title>Za knjigovođu %s - %s</title>
<style>
@page { size: A4; margin: 12mm; }
body { font: 11px/1.35 Arial, sans-serif; color: #000; max-width: 190mm; margin: 0 auto; }
h1 { font-size: 18px; margin: 0 0 4px; } h2 { font-size: 13px; margin: 18px 0 4px; } .m { color: #444; }
table { width: 100%%; border-collapse: collapse; }
th, td { padding: 3px 5px; border-bottom: 1px solid #bbb; text-align: left; vertical-align: top; }
th { border-bottom: 2px solid #000; font-size: 10px; } .r { text-align: right; white-space: nowrap; }
.b td { font-weight: bold; border-top: 1px solid #000; } .dva { display: flex; gap: 6%%; } .dva > div { flex: 1; }
.pot { display: flex; justify-content: space-between; margin-top: 40px; }
.pot div { width: 40%%; border-top: 1px solid #000; padding-top: 4px; text-align: center; }
@media screen { body { margin: 24px auto; } } @media print { .gumbi { display: none; } }
</style></head><body>
<p class=m>%s</p>
<h1>Dokumenti za knjigovodstvo: %s - %s</h1>
<p class=m>Naši skladišni dokumenti (primke, povrati, otpisi, inventura). Stornirani dokumenti
ostaju na popisu, a storno ih poništava (negativni iznosi).</p>
<div class=dva><div><h2>Rekapitulacija</h2><table><tr><th>Vrsta</th><th class=r>Broj</th>
<th class=r>Nabavna bez PDV</th><th class=r>PDV</th></tr>%s</table></div>
<div><h2>Ulazni PDV (primke umanjene za povrate)</h2><table><tr><th>Stopa</th><th class=r>Osnovica</th>
<th class=r>PDV</th></tr>%s</table></div></div>
%s
<h2>Prodaja iz Robe (informativno)</h2>
<p class=m>Službeni podaci o prodaji su fiskalizirani računi u programu Roba. Računa: %d%s.</p>
<table><tr><th>Stopa PDV</th><th class=r>Osnovica</th><th class=r>PDV</th><th class=r>Ukupno</th>
<th class=r>Razlika zaokruživanja</th></tr>%s</table>
<p class=m>Osnovica i PDV su zbroj stavki kako ih je Roba fiskalizirala (PDV po stavci). Zato
osnovica + PDV može od ukupnog odstupati za cent po računu. Kod računa R1 PDV je obračunat
na osnovicu po stopi, kao na samom računu.</p>
%s
<div class=pot><div>Sastavio</div><div>Primio (knjigovođa)</div></div>
<p class=gumbi><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button></p>
</body></html>""" % (hr(p["od"]), hr(p["do"]), esc(NAZIV_TRGOVINE), hr(p["od"]), hr(p["do"]), rek, ul,
                     "".join(popisi) or "<p>U ovom razdoblju nema naših dokumenata.</p>",
                     p["racuna"], (", od toga storno: %d" % p["storno_racuna"]) if p["storno_racuna"] else "",
                     pr, isp)


def ispis_narudzba_html(d, otpremnica=False, ponuda=False):
    """A4 narudžba ili otpremnica. Nije račun: račun se izdaje i fiskalizira u Robi."""
    def kol(k):
        return ("%.3f" % k).rstrip("0").rstrip(".").replace(".", ",")
    red = []
    for i, s in enumerate(d["stavke"], 1):
        celije = [str(i), esc(s["naziv"]), esc(s["jm"]), kol(s["kolicina"])]
        if not otpremnica:
            celije += [eur(s["cijena"]), ("%g %%" % s["popust"]).replace(".", ",") if s["popust"] else "",
                       eur(s["iznos"])]
        red.append("<tr>" + "".join("<td%s>%s</td>" % ("" if k in (1, 2) else " class=r", c)
                                    for k, c in enumerate(celije)) + "</tr>")
    zag = ["#", "Artikl / usluga", "JM", "Količina"] + ([] if otpremnica else ["Cijena", "Popust", "Iznos"])
    zag = "".join("<th%s>%s</th>" % ("" if k in (1, 2) else " class=r", z) for k, z in enumerate(zag))
    tot = "" if otpremnica else (
        "".join("<tr><td>od toga PDV %g %% na %s</td><td class=r>%s</td></tr>"
                % (p["stopa"], eur(p["osnovica"]), eur(p["pdv"])) for p in d["pdv"]) +
        "<tr class=b><td>Ukupno s PDV-om (informativno)</td><td class=r>%s</td></tr>" % eur(d["ukupno"]))
    kupac = [esc(d["kupac"])]
    if d["kupac_oib"]:
        kupac.append("OIB " + esc(d["kupac_oib"]))
    adresa = ", ".join(esc(x) for x in (d["kupac_adresa"], d["kupac_mjesto"]) if x)
    if adresa:
        kupac.append(adresa)
    if d["kupac_telefon"]:
        kupac.append("tel. " + esc(d["kupac_telefon"]))
    naslov = "PONUDA" if ponuda else "OTPREMNICA" if otpremnica else "NARUDŽBA"
    pot = ("<div>Izdao</div><div>Robu preuzeo (ime i potpis)</div>" if otpremnica
           else "<div>Zaprimio</div><div>Kupac</div>")
    return """<!doctype html><html lang=hr><head><meta charset=utf-8><title>%s %s</title>
<style>
@page { size: A4; margin: 15mm; }
body { font: 13px/1.4 Arial, sans-serif; color: #000; max-width: 190mm; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; } .m { color: #444; }
table { width: 100%%; border-collapse: collapse; margin-top: 14px; }
th, td { padding: 5px 6px; border-bottom: 1px solid #bbb; text-align: left; }
th { border-bottom: 2px solid #000; font-size: 12px; } .r { text-align: right; white-space: nowrap; }
.tot { width: 55%%; margin-left: auto; } .b td { font-weight: bold; border-top: 2px solid #000; }
.nije { border: 2px solid #000; padding: 6px 10px; font-weight: bold; margin-top: 14px; }
.pot { display: flex; justify-content: space-between; margin-top: 50px; }
.pot div { width: 40%%; border-top: 1px solid #000; padding-top: 4px; text-align: center; }
@media screen { body { margin: 24px auto; } } @media print { .gumbi { display: none; } }
</style></head><body>
<p class=m>%s</p>
<h1>%s %s</h1>
<p>Kupac: <b>%s</b><br>%s%s%s%s</p>
<table><tr>%s</tr>%s</table>
<table class=tot>%s</table>
<div class=nije>OVO NIJE RAČUN. Fiskalizirani račun izdaje se iz programa Roba%s.</div>
<div class=pot>%s</div>
<p class=gumbi><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button></p>
<script>if (location.hash === '#ispis') { addEventListener('load', () => print()); }</script>
</body></html>""" % (
        naslov, esc(d["oznaka"]), esc(NAZIV_TRGOVINE), naslov, esc(d["oznaka"]), "<br>".join(kupac[:1]) +
        ("<br>" + "<br>".join(kupac[1:]) if kupac[1:] else ""),
        ("Datum: %s<br>Ponuda vrijedi do: %s" % (esc(cijene.datum_hr(d["datum"])), esc(cijene.datum_hr(d["vrijedi_do"])))
         if ponuda else "Datum isporuke: " + esc(cijene.datum_hr(d["rok"]))),
        "<br>Broj narudžbenice kupca: <b>%s</b>" % esc(d["narudzbenica"]) if d.get("narudzbenica") else "",
        "<br>Napomena: %s" % esc(d["napomena"]) if d["napomena"] else "",
        "<br>Račun iz Robe: %s" % esc(d["racun_roba"]) if d.get("racun_roba") else "",
        zag, "".join(red), tot,
        " (broj %s)" % esc(d["racun_roba"]) if d.get("racun_roba") else "", pot)


def ispis_zpc_html(z):
    """Prijepis Robinog zapisnika o promjeni cijena (za knjigovođu i inspekciju)."""
    def kol(k):
        return ("%.3f" % k).rstrip("0").rstrip(".").replace(".", ",")
    red = "".join(
        "<tr><td>%d</td><td>%s</td><td class=r>%s %s</td><td class=r>%g %%</td><td class=r>%s</td><td class=r>%s</td>"
        "<td class=r>%s</td><td class=r>%s</td></tr>"
        % (i, esc(s["naziv"]), kol(s["kolicina"]), esc(s["jm"]), s["pdv_stopa"], eur(s["stara"]), eur(s["nova"]),
           eur(zaokruzi(s["kolicina"] * s["stara"])), eur(zaokruzi(s["kolicina"] * s["nova"])))
        for i, s in enumerate(z["stavke"], 1))
    return """<!doctype html><html lang=hr><head><meta charset=utf-8><title>ZPC %s</title>
<style>
@page { size: A4; margin: 15mm; }
body { font: 12px/1.4 Arial, sans-serif; color: #000; max-width: 190mm; margin: 0 auto; }
h1 { font-size: 18px; margin: 0 0 4px; } .m { color: #444; }
table { width: 100%%; border-collapse: collapse; margin-top: 12px; }
th, td { padding: 4px 5px; border-bottom: 1px solid #bbb; text-align: left; }
th { border-bottom: 2px solid #000; font-size: 11px; } .r { text-align: right; white-space: nowrap; }
.pot { display: flex; justify-content: space-between; margin-top: 50px; }
.pot div { width: 40%%; border-top: 1px solid #000; padding-top: 4px; text-align: center; }
@media print { .gumbi { display: none; } }
</style></head><body>
<p class=m>%s</p>
<h1>Zapisnik o promjeni cijena br. %d</h1>
<p>Datum promjene: <b>%s</b>. Prijepis iz programa Roba.</p>
<table><tr><th>#</th><th>Artikl</th><th class=r>Zaliha</th><th class=r>PDV</th><th class=r>Stara MPC</th>
<th class=r>Nova MPC</th><th class=r>Stara vrijednost</th><th class=r>Nova vrijednost</th></tr>%s
<tr><td colspan=6><b>Ukupno</b></td><td class=r><b>%s</b></td><td class=r><b>%s</b></td></tr></table>
<div class=pot><div>Sastavio</div><div>Odgovorna osoba</div></div>
<p class=gumbi><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button></p>
<script>if (location.hash === '#ispis') { addEventListener('load', () => print()); }</script>
</body></html>""" % (z["broj"], esc(NAZIV_TRGOVINE), z["broj"], esc(cijene.datum_hr(z["datum"])), red,
                     eur(z["stari"]), eur(z["novi"]))


def post_dodatni(con, put, p):
    """POST za module rokovi, bonovi, vjernost i dugovanja."""
    if put == "/api/kalo/norma":
        v = str(p.get("norma") or "").strip().replace(",", ".")
        if v:
            try:
                n = float(v)
            except ValueError:
                raise Greska("Neispravan postotak.")
            if not 0 < n <= 50:
                raise Greska("Kalo 0 do 50 %.")
            v = "%g" % n
        postavke.postavi(con, "kalo.norma", v)
        return {"ok": True}
    fn = {
        "/api/rokovi/upisi": rokovi.upisi, "/api/rokovi/zatvori": rokovi.zatvori,
        "/api/bonovi/izdaj": bonovi.izdaj_bon, "/api/bonovi/iskoristi": bonovi.iskoristi_bon,
        "/api/bonovi/uredi": bonovi.uredi_bon,
        "/api/vjernost/novi": bonovi.novi_clan, "/api/vjernost/kupnja": bonovi.kupnja,
        "/api/vjernost/nagrada": bonovi.nagrada, "/api/vjernost/pravila": bonovi.spremi_pravila,
        "/api/vjernost/obrisi": lambda c, x: bonovi.obrisi_podatke(c, int(x.get("id") or 0)),
        "/api/dugovanja/plati": dugovanja.plati, "/api/dugovanja/dospijece": dugovanja.spremi_dospijece,
        "/api/dugovanja/rok": dugovanja.spremi_rok, "/api/dugovanja/ponisti": dugovanja.ponisti_placanje,
        "/api/dugovanja/pocetak": lambda c, x: dugovanja.spremi_pocetak(c, x.get("od")),
    }.get(put)
    if not fn:
        raise Greska("Nepoznato.")
    r = fn(con, p)
    return r if isinstance(r, dict) else {"id": r}


def ispis_bon_html(b):
    """Poklon bon za ispis. Nije račun: bon se prodaje preko Robe."""
    return """<!doctype html><html lang=hr><head><meta charset=utf-8><title>Poklon bon %s</title>
<style>
@page { size: A4; margin: 15mm; }
body { font: 14px/1.4 Arial, sans-serif; color: #000; margin: 0 auto; max-width: 190mm; }
.bon { border: 3px double #000; border-radius: 10px; padding: 18px 22px; width: 120mm; margin: 10mm auto; text-align: center; }
.bon h1 { font-size: 26px; margin: 4px 0 8px; letter-spacing: 2px; } .iznos { font-size: 34px; font-weight: bold; margin: 8px 0; }
.kod { font: bold 20px monospace; letter-spacing: 3px; border: 1px dashed #000; display: inline-block; padding: 4px 12px; }
.m { color: #333; font-size: 12px; } @media print { .gumbi { display: none; } }
</style></head><body>
<div class=bon><div class=m>%s</div><h1>POKLON BON</h1><div class=iznos>%s EUR</div>
<div class=kod>%s</div><p>Vrijedi do <b>%s</b> za kupnju u trgovini.<br>Može se iskoristiti u više kupnji do iznosa bona.</p>
<p class=m>Izdan %s Ovo nije račun.</p></div>
<p class=gumbi style="text-align:center"><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button></p>
<script>if (location.hash === '#ispis') { addEventListener('load', () => print()); }</script>
</body></html>""" % (esc(b["kod"]), esc(NAZIV_TRGOVINE), eur(b["iznos"]), esc(b["kod"]),
                     esc(cijene.datum_hr(b["vrijedi_do"])), esc(cijene.datum_hr(b["izdan"])))


def ispis_pristupnica_html(c):
    """Pristupnica s privolom za karticu vjernosti (GDPR čl. 6 i 7)."""
    return """<!doctype html><html lang=hr><head><meta charset=utf-8><title>Pristupnica %s</title>
<style>
@page { size: A4; margin: 18mm; }
body { font: 13px/1.5 Arial, sans-serif; color: #000; max-width: 175mm; margin: 0 auto; }
h1 { font-size: 20px; } .pot { display: flex; justify-content: space-between; margin-top: 50px; }
.pot div { width: 40%%; border-top: 1px solid #000; padding-top: 4px; text-align: center; }
@media print { .gumbi { display: none; } }
</style></head><body>
<p>%s</p>
<h1>Pristupnica: kartica vjernosti</h1>
<p>Broj kartice: <b>%s</b><br>Ime: %s<br>Telefon / e-mail: %s<br>Datum: %s</p>
<h2 style="font-size:15px">Privola za obradu osobnih podataka</h2>
<p>Pristajem da trgovina, kao voditelj obrade, obrađuje moje ime i kontakt (ako sam ga dao/la) samo radi
vođenja kartice vjernosti: upisa bodova za kupnje i dodjele nagrada. Podaci se ne dijele s drugima i ne
koriste za druge svrhe. Privolu mogu povući u bilo kojem trenutku; tada se moji osobni podaci brišu.
Imam pravo na uvid u podatke, ispravak i brisanje, i pravo prigovora Agenciji za zaštitu osobnih podataka
(azop.hr). Podaci se čuvaju dok sam član.</p>
<div class=pot><div>Potpis člana</div><div>Za trgovinu</div></div>
<p class=gumbi><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button></p>
<script>if (location.hash === '#ispis') { addEventListener('load', () => print()); }</script>
</body></html>""" % (esc(c["kartica"]), esc(NAZIV_TRGOVINE), esc(c["kartica"]), esc(c["ime"]) or "_______________",
                     esc(c["kontakt"]) or "_______________", esc(cijene.datum_hr(c["privola"])))


def ispis_zbirni_html(z):
    """Podloga za zbirni račun u Robi: isporuke kupca u mjesecu bez računa. Nije račun."""
    def kol(k):
        return ("%.3f" % k).rstrip("0").rstrip(".").replace(".", ",")
    k = z["kupac"]
    red = "".join(
        "<tr><td>%d</td><td>%s</td><td>%s</td><td class=r>%s</td><td class=r>%s</td><td class=r>%s</td>"
        "<td class=r>%g %%</td><td class=r>%s</td></tr>"
        % (i, esc(s["naziv"]), esc(s["jm"]), kol(s["kolicina"]), eur(s["cijena"]),
           ("%g %%" % s["popust"]).replace(".", ",") if s["popust"] else "", s["pdv_stopa"], eur(s["iznos"]))
        for i, s in enumerate(z["stavke"], 1))
    tot = "".join("<tr><td>od toga PDV %g %% na %s</td><td class=r>%s</td></tr>"
                  % (p["stopa"], eur(p["osnovica"]), eur(p["pdv"])) for p in z["pdv"])
    otp = ", ".join("%s (%s%s)" % (esc(n["oznaka"]), esc(cijene.datum_hr(n["rok"])),
                                   ", narudžbenica " + esc(n["narudzbenica"]) if n.get("narudzbenica") else "")
                    for n in z["bez_racuna"])
    kupac = esc(k["naziv"]) + ("<br>OIB " + esc(k["oib"]) if k["oib"] else "") + (
        "<br>" + esc(", ".join(x for x in (k["adresa"], k["mjesto"]) if x)) if k["adresa"] or k["mjesto"] else "")
    return """<!doctype html><html lang=hr><head><meta charset=utf-8><title>Zbirni pregled %s</title>
<style>
@page { size: A4; margin: 15mm; }
body { font: 13px/1.4 Arial, sans-serif; color: #000; max-width: 190mm; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; } .m { color: #444; }
table { width: 100%%; border-collapse: collapse; margin-top: 14px; }
th, td { padding: 5px 6px; border-bottom: 1px solid #bbb; text-align: left; }
th { border-bottom: 2px solid #000; font-size: 12px; } .r { text-align: right; white-space: nowrap; }
.tot { width: 55%%; margin-left: auto; } .b td { font-weight: bold; border-top: 2px solid #000; }
.nije { border: 2px solid #000; padding: 6px 10px; font-weight: bold; margin-top: 14px; }
@media screen { body { margin: 24px auto; } } @media print { .gumbi { display: none; } }
</style></head><body>
<p class=m>%s</p>
<h1>Zbirni pregled isporuka za %s</h1>
<p>Kupac: <b>%s</b>%s</p>
<p class=m>Otpremnice bez računa: %s</p>
<table><tr><th>#</th><th>Artikl / usluga</th><th>JM</th><th class=r>Količina</th><th class=r>Cijena</th>
<th class=r>Popust</th><th class=r>PDV</th><th class=r>Iznos</th></tr>%s</table>
<table class=tot>%s<tr class=b><td>Ukupno s PDV-om</td><td class=r>%s</td></tr></table>
<div class=nije>OVO NIJE RAČUN. Podloga za zbirni račun koji se izdaje i fiskalizira u programu Roba
(tvrtki R1 / eRačun). Broj tog računa upišite u program (Narudžbe, Kupci, Zbirni pregled).</div>
<p class=gumbi><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button></p>
<script>if (location.hash === '#ispis') { addEventListener('load', () => print()); }</script>
</body></html>""" % (
        esc(k["naziv"]), esc(NAZIV_TRGOVINE), esc(z["mjesec"][5:] + "." + z["mjesec"][:4] + "."), kupac,
        "<br>Rok plaćanja: %d dana" % k["rok_placanja"] if k["rok_placanja"] else "",
        otp or "nema", red or "<tr><td colspan=8>Nema isporuka bez računa.</td></tr>", tot, eur(z["ukupno"]))


POTPISI = {
    "povrat": "<div>Predao</div><div>Preuzeo (dobavljač)</div>",
    # otpis: zapisnik potpisuje komisija koja je utvrdila kvar i uništenje robe
    "otpis": "<div>Član komisije</div><div>Član komisije</div><div>Odgovorna osoba</div>",
    "inventura": "<div>Član komisije</div><div>Član komisije</div><div>Odgovorna osoba</div>",
}


def ispis_dok_html(d, marza=0):
    """A4 ispis primke, otpisa, povrata ili inventurne razlike."""
    def kol(k):
        return ("%.3f" % k).rstrip("0").rstrip(".").replace(".", ",")
    primka = d["vrsta"] == "primka"
    red, promjene = [], []
    for i, s in enumerate(d["stavke"], 1):
        vpc = s["mpc"] / (1 + s["pdv_stopa"] / 100.0)
        ruc = ("%.1f %%" % ((vpc - s["cijena"]) / s["cijena"] * 100)).replace(".", ",") \
            if primka and s["cijena"] > 0 else ""
        celije = [str(i), esc(s["naziv"]), esc(s["jm"]), kol(s["kolicina"])]
        if primka:
            celije += [eur(s["fakturna"] or s["cijena"]), ("%g %%" % s["rabat"]).replace(".", ",")]
        celije += [eur(s["cijena"]), eur(s["iznos"])]
        if primka:
            celije += ["%g %%" % s["pdv_stopa"], eur(s["mpc"]), ruc]
            if marza > 0:
                p = nabava.predlozena_mpc(s["cijena"], s["pdv_stopa"], marza)
                celije.append(eur(p) if p else "")
                if p and p != s["mpc"] and s["kolicina"] > 0:
                    promjene.append("<tr><td>%s</td><td class=r>%s</td><td class=r><b>%s</b></td></tr>"
                                    % (esc(s["naziv"]), eur(s["mpc"]), eur(p)))
        red.append("<tr>" + "".join("<td%s>%s</td>" % ("" if k in (1, 2) else " class=r", c)
                                    for k, c in enumerate(celije)) + "</tr>")
    zag = ["#", "Artikl", "JM", "Količina"]
    if primka:
        zag += ["Cijena s računa", "Rabat"]
    zag += ["Nab. cijena", "Iznos"]
    if primka:
        zag += ["PDV", "MPC", "RUC"]
        if marza > 0:
            zag.append("Predl. MPC")
    zag = "".join("<th%s>%s</th>" % ("" if k in (1, 2) else " class=r", z) for k, z in enumerate(zag))
    tot = ("<tr><td>Nabavna vrijednost bez PDV-a</td><td class=r>%s</td></tr>"
           "<tr><td>PDV</td><td class=r>%s</td></tr>"
           "<tr class=b><td>%s</td><td class=r>%s</td></tr>"
           % (eur(d["neto"]), eur(d["pdv"]),
              "Ukupno za platiti" if primka else "Ukupno (dobavljač odobrava)",
              eur(d["neto"] + d["pdv"])) if d["vrsta"] in nabava.S_DOBAVLJACEM else
           "<tr class=b><td>Vrijednost po nabavnoj cijeni</td><td class=r>%s</td></tr>" % eur(d["neto"]))
    info = []
    if d["partner"]:
        info.append("Dobavljač: <b>%s</b>%s" % (esc(d["partner"]),
                    " (OIB %s)" % esc(d["partner_oib"]) if d["partner_oib"] else ""))
    if d["broj_dobavljaca"]:
        info.append("%s: %s" % ("Odnosi se na račun" if d["vrsta"] == "povrat" else "Račun dobavljača",
                                esc(d["broj_dobavljaca"])))
    if d["storno_od"]:
        info.append("<b>STORNO dokumenta %s</b>" % esc(d["o_oznaka"]))
    if d["napomena"]:
        info.append("%s: %s" % ("Razlog" if d["vrsta"] in ("otpis", "povrat") else "Napomena",
                                esc(d["napomena"])))
    if d["vrsta"] == "povrat":
        info.append("Ovo nije račun ni odobrenje. Za vraćenu robu dobavljač izdaje odobrenje (eRačun).")
    if d["vrsta"] == "otpis":
        info.append("Komisija je utvrdila da roba nije za prodaju i da je uništena / zbrinuta.")
    return """<!doctype html><html lang=hr><head><meta charset=utf-8><title>%s</title>
<style>
@page { size: A4; margin: 15mm; }
body { font: 13px/1.4 Arial, sans-serif; color: #000; max-width: 190mm; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; } .m { color: #444; }
table { width: 100%%; border-collapse: collapse; margin-top: 14px; }
th, td { padding: 5px 6px; border-bottom: 1px solid #bbb; text-align: left; }
th { border-bottom: 2px solid #000; font-size: 12px; } .r { text-align: right; white-space: nowrap; }
.tot { width: 50%%; margin-left: auto; } .b td { font-weight: bold; border-top: 2px solid #000; }
.pot { display: flex; justify-content: space-between; margin-top: 50px; }
.pot div { flex: 1; margin: 0 4%%; border-top: 1px solid #000; padding-top: 4px; text-align: center; }
@media screen { body { margin: 24px auto; } } @media print { .gumbi { display: none; } }
</style></head><body>
<p class=m>%s</p>
<h1>%s %s</h1>
<p>Datum: %s &nbsp; · &nbsp; Upisano: %s<br>%s</p>
<table><tr>%s</tr>%s</table>
<table class=tot>%s</table>%s
<div class=pot>%s</div>
<p class=gumbi><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button></p>
<script>if (location.hash === '#ispis') { addEventListener('load', () => print()); }</script>
</body></html>""" % (esc(d["oznaka"]), esc(NAZIV_TRGOVINE), NASLOVI[d["vrsta"]], esc(d["oznaka"]),
                     esc(cijene.datum_hr(d["datum"])), esc(d["vrijeme"]), "<br>".join(info), zag, "".join(red), tot,
                     ("<h2 style='font-size:15px;margin-top:24px'>Promjene MPC za upis u Robu "
                      "(marža %s %%)</h2><table class=tot style='margin-left:0'><tr><th>Artikl</th>"
                      "<th class=r>MPC sada</th><th class=r>Nova MPC</th></tr>%s</table>"
                      % (("%g" % marza).replace(".", ","), "".join(promjene))) if promjene else "",
                     POTPISI.get(d["vrsta"], "<div>Zaprimio / sastavio</div><div>Odobrio</div>"))


def ispis_zaliha_html(artikli):
    """A4 (položeno) stanje zalihe za knjigovodstvo: vrijednost po nabavnoj i prodajnoj."""
    def kol(k):
        return ("%.3f" % k).rstrip("0").rstrip(".").replace(".", ",")
    art = [a for a in artikli if abs(a["zaliha"]) > 0.0005]
    zbroj = {k: sum(a[k] for a in art) for k in ("nab_netto", "nab_pdv", "pro_netto", "pro_pdv")}
    red = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td class=r>%s</td><td class=r>%s</td>"
        "<td class=r>%s</td><td class=r>%s</td><td class=r>%g %%</td><td class=r>%s</td>"
        "<td class=r>%s</td><td class=r>%s</td></tr>"
        % (esc(a["sifra"]), esc(a["naziv"]), esc(a["jm"]), kol(a["zaliha"]), eur(a["nabavna"]),
           eur(a["nab_netto"]), eur(a["nab_pdv"]), a["pdv_stopa"], eur(a["cijena"]),
           eur(a["pro_netto"]), eur(a["pro_pdv"])) for a in art)
    minus = sum(1 for a in art if a["zaliha"] < 0)
    return """<!doctype html><html lang=hr><head><meta charset=utf-8><title>Stanje zalihe</title>
<style>
@page { size: A4 landscape; margin: 12mm; }
body { font: 11px/1.35 Arial, sans-serif; color: #000; margin: 0 auto; max-width: 273mm; }
h1 { font-size: 18px; margin: 0 0 4px; } .m { color: #444; }
table { width: 100%%; border-collapse: collapse; margin-top: 10px; }
th, td { padding: 3px 5px; border-bottom: 1px solid #ccc; text-align: left; }
th { border-bottom: 2px solid #000; } .r { text-align: right; white-space: nowrap; }
tfoot td { font-weight: bold; border-top: 2px solid #000; border-bottom: 0; }
thead { display: table-header-group; } tr { page-break-inside: avoid; }
@media screen { body { margin: 20px auto; } } @media print { .gumbi { display: none; } }
</style></head><body>
<p class=m>%s</p><h1>STANJE ZALIHE na dan %s</h1>
<p class=m>Nabavna cijena = prosječna nabavna bez PDV-a (cijena s računa dobavljača umanjena za rabat).
Prodajna = MPC iz Robe.%s</p>
<table><thead><tr><th>Šifra</th><th>Naziv</th><th>JM</th><th class=r>Količina</th>
<th class=r>Nab. cijena</th><th class=r>Netto nabavna</th><th class=r>PDV nabavna</th>
<th class=r>Stopa</th><th class=r>MPC</th><th class=r>Netto prodajna</th><th class=r>PDV na prodajnu</th>
</tr></thead><tbody>%s</tbody>
<tfoot><tr><td colspan=5>UKUPNO (%d artikala)</td><td class=r>%s</td><td class=r>%s</td><td></td><td></td>
<td class=r>%s</td><td class=r>%s</td></tr></tfoot></table>
<p class=gumbi><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button></p>
<script>if (location.hash === '#ispis') { addEventListener('load', () => print()); }</script>
</body></html>""" % (esc(NAZIV_TRGOVINE), dt.date.today().strftime("%d.%m.%Y."),
                     " Pozor: %d artikala ima negativnu zalihu, napravite inventuru." % minus
                     if minus else "", red, len(art), eur(zbroj["nab_netto"]), eur(zbroj["nab_pdv"]),
                     eur(zbroj["pro_netto"]), eur(zbroj["pro_pdv"]))


UVOZ_LOCK = threading.Lock()


def osvjezi_robu(con):
    """Ponovno pročita Robine datoteke (samo čitanje) u našu bazu."""
    from uvoz import uvezi
    with UVOZ_LOCK:
        con.isolation_level = ""  # uvoz koristi "with con" transakciju
        try:
            return uvezi(ROBA_MAPA, con, ispis=lambda *a: None)
        finally:
            con.isolation_level = None


def ispis_etiketa_html(artikli, voce_povrce=True):
    """A4 etikete za police: naziv, cijena, jedinica, zemlja podrijetla, klasa.
    Za rinfuzno voće i povrće zemlja podrijetla je obvezna i mora biti barem
    jednako velika kao cijena (Pravilnik o tržišnim standardima, NN 156/2025, čl. 9).
    "AKCIJA" i najniža cijena u 30 dana pišu samo ako je MPC iz Robe stvarno niža od nje
    (Zakon o zaštiti potrošača, NN 59/2026), da etiketa nikad ne obećava lažno sniženje."""
    et = []
    for a in artikli:
        jm = a["jm"].lower()
        jed = "EUR/kg" if jm == "kg" else "EUR/" + esc(a["jm"])
        podrijetlo = esc(a["podrijetlo"]) if a["podrijetlo"] and voce_povrce else "&nbsp;"
        # cijena za jedinicu mjere za pakiranu robu na komad (Pravilnik NN 105/2026, čl. 8)
        uz_cijenu = jed
        if jm not in ("kg", "l") and a.get("neto_jm") in ("kg", "l") and a.get("neto_kolicina"):
            uz_cijenu += "<br>Cijena za %s: %s EUR" % (a["neto_jm"], eur(cijene.cijena_za_jedinicu(a)))
        et.append("""<div class=e><div class=n>%s</div><div class=c><b>%s</b> <span>%s</span></div>
<div class=s>%s%s</div><div class=p>%s</div><div class=k>%s%s</div></div>"""
                  % (esc(a["naziv"]), eur(a["cijena"]), uz_cijenu,
                     ("<b>AKCIJA</b> · Najniža cijena u 30 dana prije akcije: %s %s<br>"
                      % (eur(a["akcija_prije"]), jed)) if 0 < a["cijena"] < a["akcija_prije"] else "",
                     ("Bazna cijena (%s): %s %s" % (cijene.datum_hr(a["sidrena_datum"]), eur(a["sidrena"]), jed))
                     if a["sidrena_datum"] else "&nbsp;",
                     ("<small>Podrijetlo</small>" if a["podrijetlo"] and voce_povrce else "") + podrijetlo,
                     ("Klasa: %s · " % esc(a["klasa"])) if a["klasa"] and voce_povrce else "",
                     "šifra %s" % esc(a["sifra"])))
    bez = sum(1 for a in artikli if not a["podrijetlo"] and not a["usluga"]) if voce_povrce else 0
    bez_sid = sum(1 for a in artikli if not a["sidrena_datum"] and not a["usluga"])
    return """<!doctype html><html lang=hr><head><meta charset=utf-8><title>Etikete</title>
<style>
@page { size: A4; margin: 8mm; }
body { font-family: Arial, sans-serif; margin: 0; color: #000; }
.g { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0; }
.e { border: 1px dashed #999; height: 46mm; padding: 4mm; box-sizing: border-box; display: flex;
     flex-direction: column; justify-content: space-between; page-break-inside: avoid; }
.n { font-size: 16pt; font-weight: bold; line-height: 1.1; text-transform: uppercase; }
.n { font-size: 14pt; }
.c b { font-size: 26pt; } .c span { font-size: 11pt; }
/* podrijetlo barem jednako veliko kao znamenke cijene (NN 156/2025, čl. 9 st. 4) */
.p { font-size: 26pt; font-weight: bold; line-height: 1; }
.p small { display: block; font-size: 8pt; font-weight: normal; }
.k { font-size: 8pt; color: #333; }
/* bazna (prije: sidrena) cijena, od 17. 11. 2026. (NN 132/2026): jasno i čitko uz MPC */
.s { font-size: 10pt; }
.upoz { background: #fdf0dc; padding: 8px; margin: 8px 0; font-size: 13px; }
@media print { .gumbi, .upoz { display: none; } }
</style></head><body>
%s<p class=gumbi><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button>
 &nbsp; %d etiketa, 3 u redu, izrežite po isprekidanoj crti.</p>
<div class=g>%s</div></body></html>""" % (
        ("<p class=upoz>%d artikala nema upisanu zemlju podrijetla. Za rinfuzno voće i povrće "
         "ona je obvezna (Zaliha → klik na naziv artikla).</p>" % bez if bez else "") +
        ("<p class=upoz>%d artikala nema sidrenu cijenu (obvezna od 17. 11. 2026.). "
         "Upišite je u Zaliha → klik na naziv artikla.</p>" % bez_sid if bez_sid else ""),
        len(et), "".join(et))


def ispis_haccp_html(m):
    """A4 HACCP evidencije za mjesec: temperature po uređaju, čišćenje, prijem robe.
    Poništeni zapisi ostaju vidljivi (prekriženi) s razlogom."""
    def dvr(v):
        return "%s.%s. %s" % (v[8:10], v[5:7], v[11:16])

    def stupanj(t):
        return ("%.1f" % t).replace(".", ",") if t is not None else ""

    def red(celije, pon):
        st = " class=pon" if pon else ""
        return "<tr%s>%s</tr>" % (st, "".join("<td>%s</td>" % c for c in celije))
    dijelovi = []
    uredjaji = []
    for t in m["temperature"]:
        if t["uredjaj"] not in uredjaji:
            uredjaji.append(t["uredjaj"])
    for naziv in uredjaji:
        tt = [t for t in m["temperature"] if t["uredjaj"] == naziv]
        redovi = "".join(red([dvr(t["vrijeme"]), "<b>%s</b>" % stupanj(t["temperatura"])
                              + (" !" if not t["min_t"] <= t["temperatura"] <= t["max_t"] else ""),
                              "%s do %s" % (stupanj(t["min_t"]), stupanj(t["max_t"])), esc(t["tko"]),
                              esc(t["mjera"]) + ((" PONIŠTENO: " + esc(t["ponisteno"])) if t["ponisteno"] else "")],
                             t["ponisteno"]) for t in tt)
        dijelovi.append("<h2>Temperatura: %s</h2><table><tr><th>Vrijeme</th><th>°C</th><th>Granice °C</th>"
                        "<th>Mjerio</th><th>Korektivna mjera / napomena</th></tr>%s</table>" % (esc(naziv), redovi))
    if not uredjaji:
        dijelovi.append("<h2>Temperature</h2><p>Nema zapisa u ovom mjesecu.</p>")
    plan = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                   % (esc(p["podrucje"]), esc(p["ucestalost"].replace("mjesecno", "mjesečno")), esc(p["nacin"]),
                      esc(p["sredstvo"])) for p in m["plan"])
    dijelovi.append("<h2>Plan čišćenja</h2><table><tr><th>Što</th><th>Koliko često</th><th>Način</th>"
                    "<th>Sredstvo</th></tr>%s</table>" % (plan or "<tr><td colspan=4>Plan nije upisan.</td></tr>"))
    cis = "".join(red([dvr(c["vrijeme"]), esc(c["podrucje"]), esc(c["nacin"]), esc(c["sredstvo"]), esc(c["tko"]),
                       ("PONIŠTENO: " + esc(c["ponisteno"])) if c["ponisteno"] else ""], c["ponisteno"])
                  for c in m["ciscenje"])
    dijelovi.append("<h2>Evidencija čišćenja, pranja i dezinfekcije</h2><table><tr><th>Vrijeme</th><th>Što</th>"
                    "<th>Način</th><th>Sredstvo</th><th>Tko</th><th>Napomena</th></tr>%s</table>"
                    % (cis or "<tr><td colspan=6>Nema zapisa.</td></tr>"))
    pri = "".join(red([dvr(p["vrijeme"]), esc(p["dobavljac"]), esc(p["dokument"]), esc(p["roba"]),
                       stupanj(p["temperatura"]), "prihvaćeno" if p["prihvaceno"] else "<b>ODBIJENO</b>",
                       esc(p["napomena"]) + ((" PONIŠTENO: " + esc(p["ponisteno"])) if p["ponisteno"] else ""),
                       esc(p["tko"])], p["ponisteno"]) for p in m["prijem"])
    dijelovi.append("<h2>Prijem robe</h2><table><tr><th>Vrijeme</th><th>Dobavljač</th><th>Dokument</th><th>Roba</th>"
                    "<th>°C</th><th>Ocjena</th><th>Napomena</th><th>Zaprimio</th></tr>%s</table>"
                    % (pri or "<tr><td colspan=8>Nema zapisa.</td></tr>"))
    return """<!doctype html><html lang=hr><head><meta charset=utf-8><title>HACCP %s</title>
<style>
@page { size: A4; margin: 12mm; }
body { font: 11px/1.35 Arial, sans-serif; color: #000; max-width: 190mm; margin: 0 auto; }
h1 { font-size: 17px; margin: 0 0 4px; } h2 { font-size: 13px; margin: 16px 0 4px; } .m { color: #444; }
table { width: 100%%; border-collapse: collapse; }
th, td { padding: 3px 5px; border-bottom: 1px solid #bbb; text-align: left; vertical-align: top; }
th { border-bottom: 2px solid #000; font-size: 10px; }
tr.pon td { text-decoration: line-through; color: #666; }
.pot { display: flex; justify-content: space-between; margin-top: 36px; }
.pot div { width: 40%%; border-top: 1px solid #000; padding-top: 4px; text-align: center; }
@media screen { body { margin: 24px auto; } } @media print { .gumbi { display: none; } }
</style></head><body>
<p class=m>%s</p>
<h1>HACCP evidencije za %s.%s.</h1>
<p class=m>Zapisi se ne mijenjaju; pogrešan unos je poništen uz razlog i ostaje prekrižen. "!" = izvan granica.</p>
%s
<div class=pot><div>Odgovorna osoba</div><div>Datum pregleda</div></div>
<p class=gumbi><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button></p>
</body></html>""" % (esc(m["mjesec"]), esc(NAZIV_TRGOVINE), m["mjesec"][5:7], m["mjesec"][:4], "".join(dijelovi))


def ucitaj_stranicu():
    with open(os.path.join(SCRIPT_DIR, "kasa.html"), encoding="utf-8") as f:
        return dodaci.stranica(f.read())


STRANICA = ""


def main():
    global STRANICA, ROBA_MAPA
    STRANICA = ucitaj_stranicu()
    if len(sys.argv) > 1:
        ROBA_MAPA = sys.argv[1]
    # kopija prije spoji(), jer spoji() nadograđuje bazu na novu verziju
    if os.path.exists(DB_PATH):
        for k in kopija.napravi():
            print("Sigurnosna kopija: %s" % (k["datoteka"] or "GREŠKA u %s: %s" % (k["mapa"], k["greska"])))
    con = spoji()
    if inventura.trenutna(con) and postavke.ukljucen(con, "inventura_mobitel"):  # ostalo otvoreno od prije
        greska = inventura.pokreni()
        print("Inventura s mobitela: %s" % (greska or inventura.adresa()))
    dodaci.pri_pokretanju(con)
    kopija_van.pri_pokretanju()
    poruka = haccp_mobitel.pri_pokretanju(con)
    if poruka:
        print("HACCP s mobitela: %s" % poruka)
    poruka = zaslon_kupca.pri_pokretanju(con)
    if poruka:
        print("Zaslon za kupca (tablet): %s" % poruka)
    if len(sys.argv) > 1:
        from uvoz import uvezi
        print("Uvoz iz Robe...")
        uvezi(sys.argv[1], con)
    if not con.execute("SELECT COUNT(*) FROM artikli").fetchone()[0]:
        print("Baza nema artikala. Pokrenite:  python kasa.py \"C:\\Roba\"  (putanja do Robe)")
    if not con.execute("SELECT COUNT(*) FROM nacini_placanja").fetchone()[0]:
        con.execute("INSERT INTO nacini_placanja (naziv) VALUES ('Gotovina'), ('Kartica')")
        con.commit()
    con.close()
    ThreadingHTTPServer.allow_reuse_address = os.name != "nt"  # na Windowsu bi dopustilo dvije kase na istom portu
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = "http://127.0.0.1:%d" % PORT
    print("Kasa radi na %s  (zatvorite ovaj prozor za kraj)" % url)
    if not os.environ.get("TRGOVINA_BEZ_PREGLEDNIKA"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
