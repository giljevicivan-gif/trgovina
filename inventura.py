# -*- coding: utf-8 -*-
"""
Inventura s mobitela.

Na računalu (Zaliha -> Inventura s mobitela) otvori se brojanje: program pokaže
adresu (npr. http://192.168.1.5:8767) i PIN. Na mobitelu spojenom na isti WiFi
otvori se ta adresa, upiše PIN i broji: artikl + količina. Isti artikl se može
brojati više puta (polica + skladište), količine se zbrajaju.

Na računalu se uživo vidi prebrojano, stanje u programu i razlika. Na kraju
"Zaključi" sprema inventuru (dokument I-, kao i obična inventura) samo za
prebrojane artikle, a "Popisna lista" se ispisuje i potpisuje (komisija).

Sigurnost: glavni program i dalje sluša samo na ovom računalu (127.0.0.1).
Za mobitel se otvara POSEBAN mali poslužitelj (port 8767) koji zna samo
brojanje, traži PIN i radi samo dok je brojanje otvoreno.
"""

import datetime as dt
import json
import os
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from baza import SCRIPT_DIR, ZALIHA_SQL, spoji, zaokruzi
from nabava import Greska, sada
import nabava

PORT = int(os.environ.get("TRGOVINA_MOBITEL_PORT") or 8767)
MAX_KRIVIH_PINOVA = 10

SHEMA = """
CREATE TABLE IF NOT EXISTS inv_sesije (
    id           INTEGER PRIMARY KEY,
    otvorena     TEXT NOT NULL,
    zatvorena    TEXT,
    pin          TEXT NOT NULL,
    dokument_id  INTEGER REFERENCES dokumenti(id),   -- spremljena inventura (I-)
    napomena     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS inv_brojanje (
    id          INTEGER PRIMARY KEY,
    sesija_id   INTEGER NOT NULL REFERENCES inv_sesije(id),
    artikl_id   INTEGER NOT NULL REFERENCES artikli(id),
    kolicina    REAL NOT NULL,
    vrijeme     TEXT NOT NULL,
    uredjaj     TEXT NOT NULL DEFAULT '',
    ponisteno   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_inv_brojanje ON inv_brojanje(sesija_id);
-- stanje u programu u trenutku zaključka (za popisnu listu, poslije se stanje mijenja)
CREATE TABLE IF NOT EXISTS inv_stanje (
    sesija_id  INTEGER NOT NULL REFERENCES inv_sesije(id),
    artikl_id  INTEGER NOT NULL REFERENCES artikli(id),
    knjizno    REAL NOT NULL,
    nabavna    INTEGER NOT NULL,
    PRIMARY KEY (sesija_id, artikl_id)
);
"""


def pripremi(con):
    con.executescript(SHEMA)


# ---------------------------------------------------------------- Podaci

def trenutna(con):
    return con.execute("SELECT * FROM inv_sesije WHERE zatvorena IS NULL ORDER BY id DESC LIMIT 1").fetchone()


def otvori(con):
    if trenutna(con):
        raise Greska("Brojanje je već otvoreno.")
    pin = "%06d" % secrets.randbelow(1000000)
    with con:
        return con.execute("INSERT INTO inv_sesije (otvorena, pin) VALUES (?, ?)", (sada(), pin)).lastrowid


def dodaj(con, sid, artikl_id, kolicina, uredjaj=""):
    a = con.execute("SELECT naziv, usluga FROM artikli WHERE id = ? AND aktivan = 1",
                    (int(artikl_id),)).fetchone()
    if not a:
        raise Greska("Artikl ne postoji.")
    if a["usluga"]:
        raise Greska("%s je usluga i nema zalihu." % a["naziv"])
    try:
        kol = round(float(str(kolicina).replace(",", ".")), 3)
    except (TypeError, ValueError):
        raise Greska("Neispravna količina.")
    if not 0 <= kol <= 100000:
        raise Greska("Neispravna količina.")
    with con:
        return con.execute(
            "INSERT INTO inv_brojanje (sesija_id, artikl_id, kolicina, vrijeme, uredjaj) VALUES (?, ?, ?, ?, ?)",
            (sid, int(artikl_id), kol, sada(), str(uredjaj)[:40])).lastrowid


def ponisti(con, sid, unos_id, uredjaj=None):
    sql = "UPDATE inv_brojanje SET ponisteno = 1 WHERE id = ? AND sesija_id = ?"
    args = [int(unos_id), sid]
    if uredjaj is not None:  # s mobitela se poništava samo svoje
        sql += " AND uredjaj = ?"
        args.append(uredjaj)
    with con:
        if not con.execute(sql, args).rowcount:
            raise Greska("Unos ne postoji.")


def unosi(con, sid, uredjaj=None, n=30):
    sql = """SELECT b.id, b.kolicina, b.vrijeme, b.uredjaj, b.ponisteno, a.naziv, a.jm
             FROM inv_brojanje b JOIN artikli a ON a.id = b.artikl_id WHERE b.sesija_id = ?"""
    args = [sid]
    if uredjaj is not None:
        sql += " AND b.uredjaj = ?"
        args.append(uredjaj)
    return [dict(r) for r in con.execute(sql + " ORDER BY b.id DESC LIMIT ?", args + [n])]


def pregled(con, sid=None):
    """Prebrojano po artiklu, stanje u programu i razlika (za računalo i popisnu listu)."""
    s = con.execute("SELECT * FROM inv_sesije WHERE id = ?", (sid,)).fetchone() if sid else trenutna(con)
    if not s:
        return None
    zapamceno = {r["artikl_id"]: (r["knjizno"], r["nabavna"]) for r in con.execute(
        "SELECT * FROM inv_stanje WHERE sesija_id = ?", (s["id"],))}
    stavke = []
    for r in con.execute(
            """SELECT a.id, a.sifra, a.naziv, a.jm, a.nabavna, round(%s, 3) AS knjizno,
                      SUM(b.kolicina) AS prebrojano, COUNT(*) AS unosa
               FROM inv_brojanje b JOIN artikli a ON a.id = b.artikl_id
               WHERE b.sesija_id = ? AND b.ponisteno = 0
               GROUP BY a.id ORDER BY a.naziv""" % ZALIHA_SQL, (s["id"],)):
        d = dict(r)
        if d["id"] in zapamceno:
            d["knjizno"], d["nabavna"] = zapamceno[d["id"]]
        d["prebrojano"] = round(d["prebrojano"], 3)
        d["razlika"] = round(d["prebrojano"] - d["knjizno"], 3)
        d["vrijednost_razlike"] = zaokruzi(d["razlika"] * d["nabavna"])
        stavke.append(d)
    out = dict(s)
    out["stavke"] = stavke
    out["unosi"] = unosi(con, s["id"])
    out["manjak"] = sum(x["vrijednost_razlike"] for x in stavke if x["razlika"] < 0)
    out["visak"] = sum(x["vrijednost_razlike"] for x in stavke if x["razlika"] > 0)
    return out


def zatvori(con, napomena=""):
    """Sprema inventuru (I-) za prebrojane artikle i zatvara brojanje. Vraća id dokumenta ili None."""
    s = trenutna(con)
    if not s:
        raise Greska("Nema otvorenog brojanja.")
    p = pregled(con, s["id"])
    if not p["stavke"]:
        raise Greska("Ništa nije prebrojano.")
    with con:
        con.executemany("INSERT OR REPLACE INTO inv_stanje VALUES (?, ?, ?, ?)",
                        [(s["id"], x["id"], x["knjizno"], x["nabavna"]) for x in p["stavke"]])
    did = None
    if any(x["razlika"] for x in p["stavke"]):
        did = nabava.spremi(con, {
            "vrsta": "inventura",
            "napomena": ("Inventura s mobitela " + (str(napomena).strip() or "")).strip()[:300],
            "stavke": [{"artikl_id": x["id"], "kolicina": x["prebrojano"]} for x in p["stavke"]]})
    with con:
        con.execute("UPDATE inv_sesije SET zatvorena = ?, dokument_id = ?, napomena = ? WHERE id = ?",
                    (sada(), did, str(napomena).strip()[:300], s["id"]))
    zaustavi()
    return {"sesija": s["id"], "dokument_id": did}


def odustani(con):
    s = trenutna(con)
    if not s:
        raise Greska("Nema otvorenog brojanja.")
    with con:
        con.execute("UPDATE inv_sesije SET zatvorena = ?, napomena = 'odustalo' WHERE id = ?",
                    (sada(), s["id"]))
    zaustavi()


# ---------------------------------------------------------------- Poslužitelj za mobitel

_server = None
_lock = threading.Lock()
_tokeni = {}          # token -> (sesija_id, naziv uređaja)
_krivi = {"n": 0}


def lokalna_adresa():
    """IP ovog računala u lokalnoj mreži (ne šalje ništa na internet)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def adresa():
    return "http://%s:%d" % (lokalna_adresa(), PORT)


def pokreni():
    """Pokreće poslužitelj za mobitel (ako već ne radi). Vraća grešku ili ''."""
    global _server
    with _lock:
        if _server:
            return ""
        try:
            _server = ThreadingHTTPServer(("0.0.0.0", PORT), MobitelHandler)
        except OSError as e:
            return "Ne mogu otvoriti port %d: %s" % (PORT, e)
        _tokeni.clear()
        _krivi["n"] = 0
        threading.Thread(target=_server.serve_forever, daemon=True).start()
        return ""


def zaustavi():
    global _server
    with _lock:
        if _server:
            srv, _server = _server, None
            threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()), daemon=True).start()
        _tokeni.clear()


def radi():
    return _server is not None


def _stranica():
    with open(os.path.join(SCRIPT_DIR, "mobitel.html"), encoding="utf-8") as f:
        return f.read()


class MobitelHandler(BaseHTTPRequestHandler):
    """Zna SAMO brojanje za inventuru. Sve ostalo je 404."""

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = (json.dumps(body, ensure_ascii=False) if ctype.startswith("application/json")
                    else body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _prijavljen(self, con):
        t = _tokeni.get(self.headers.get("X-Token", ""))
        if not t:
            return None
        s = trenutna(con)
        if not s or s["id"] != t[0]:
            return None
        return t

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/m"):
            return self._send(200, _stranica(), "text/html; charset=utf-8")
        if u.path not in ("/m/api/artikli", "/m/api/moji"):
            return self._send(404, {"greska": "Nema."})
        con = spoji()
        try:
            t = self._prijavljen(con)
            if not t:
                return self._send(401, {"greska": "Prijavite se PIN-om."})
            if u.path == "/m/api/artikli":
                rows = con.execute("""SELECT id, sifra, barkod, naziv, jm FROM artikli
                                      WHERE aktivan = 1 AND usluga = 0 ORDER BY naziv""")
                return self._send(200, [dict(r) for r in rows])
            return self._send(200, unosi(con, t[0], uredjaj=t[1], n=15))
        finally:
            con.close()

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        if n > 10000:
            return self._send(413, {"greska": "Prevelik zahtjev."})
        try:
            p = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"greska": "Neispravan zahtjev."})
        con = spoji()
        con.isolation_level = None
        try:
            if u.path == "/m/api/prijava":
                s = trenutna(con)
                if not s or _krivi["n"] >= MAX_KRIVIH_PINOVA:
                    return self._send(403, {"greska": "Brojanje nije otvoreno (ili previše krivih PIN-ova). "
                                                      "Otvorite ga ponovno na računalu."})
                if not secrets.compare_digest(str(p.get("pin") or "").strip(), s["pin"]):
                    _krivi["n"] += 1
                    return self._send(403, {"greska": "Krivi PIN."})
                token = secrets.token_hex(16)
                ime = " ".join(str(p.get("ime") or "").split())[:40] or "mobitel %d" % (len(_tokeni) + 1)
                _tokeni[token] = (s["id"], ime)
                return self._send(200, {"token": token, "ime": ime})
            t = self._prijavljen(con)
            if not t:
                return self._send(401, {"greska": "Prijavite se PIN-om."})
            if u.path == "/m/api/brojanje":
                dodaj(con, t[0], p.get("artikl_id"), p.get("kolicina"), t[1])
                return self._send(200, {"ok": True})
            if u.path == "/m/api/ponisti":
                ponisti(con, t[0], p.get("id"), uredjaj=t[1])
                return self._send(200, {"ok": True})
            return self._send(404, {"greska": "Nema."})
        except Greska as e:
            return self._send(400, {"greska": str(e)})
        except (TypeError, ValueError):
            return self._send(400, {"greska": "Neispravan zahtjev."})
        finally:
            con.close()
