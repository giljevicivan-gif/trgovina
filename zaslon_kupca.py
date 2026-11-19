# -*- coding: utf-8 -*-
"""
Zaslon za kupca (modul "zaslon_kupca").

Drugi prozor preglednika (drugi monitor okrenut kupcu) ili tablet u lokalnoj
mreži pokazuje stavke u košarici, ukupno i nakon naplate "Hvala! Vratiti: x €".

Kako radi (jednostavno, bez websocketa): kasa na svaku promjenu košarice
pošalje stanje (POST /api/zaslon), a stranica /zaslon svakih pola sekunde
pita GET /api/zaslon. Stanje je samo u memoriji programa.

Glavni program sluša samo na ovom računalu (127.0.0.1). Po želji (zadano
isključeno) uključi se i mali poslužitelj za tablet u mreži (port 8769): zna
SAMO prikaz zaslona (stranicu i trenutnu košaricu), ništa se ne može upisati,
nema artikala, cijena iz šifrarnika ni prometa.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import postavke
from baza import SCRIPT_DIR, spoji
from inventura import lokalna_adresa

PORT = int(os.environ.get("TRGOVINA_ZASLON_PORT") or 8769)
NAZIV = os.environ.get("TRGOVINA_NAZIV", "Trgovina")
HVALA_SEKUNDI = 8          # koliko dugo stoji "Hvala! Vratiti..." nakon naplate
MAX_STAVKI = 300

_lock = threading.Lock()
_stanje = {"stanje": "prazno", "stavke": [], "ukupno": 0, "vratiti": None, "t": 0.0, "n": 0}
_server = None
_modul = {"ukljucen": False, "provjereno": 0.0}


# ---------------------------------------------------------------- stanje

def _tekst(v, n=80):
    return " ".join(str(v if v is not None else "").split())[:n]


def _cijeli(v):
    try:
        x = int(round(float(v)))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(-100000000, min(100000000, x))


def postavi(p):
    """Stanje s kase. p = {stanje: 'kosarica'|'placeno', stavke: [{naziv, kol, jm, cijena, popust, iznos}],
    ukupno, vratiti}. Sve se očisti i skrati; zaslon prikazuje samo tekst."""
    stanje = p.get("stanje") if p.get("stanje") in ("kosarica", "placeno") else "kosarica"
    stavke = []
    for s in (p.get("stavke") or [])[:MAX_STAVKI]:
        if not isinstance(s, dict):
            continue
        stavke.append({"naziv": _tekst(s.get("naziv")), "kol": _tekst(s.get("kol"), 12), "jm": _tekst(s.get("jm"), 8),
                       "cijena": _cijeli(s.get("cijena")), "popust": _tekst(s.get("popust"), 8),
                       "iznos": _cijeli(s.get("iznos"))})
    vratiti = p.get("vratiti")
    with _lock:
        _stanje.update({"stanje": stanje if (stavke or stanje == "placeno") else "prazno",
                        "stavke": stavke if stanje == "kosarica" else [],
                        "ukupno": _cijeli(p.get("ukupno")),
                        "vratiti": _cijeli(vratiti) if vratiti not in (None, "") else None,
                        "t": time.time(), "n": _stanje["n"] + 1})


def procitaj():
    with _lock:
        d = dict(_stanje)
    if d["stanje"] == "placeno" and time.time() - d["t"] > HVALA_SEKUNDI:
        d.update(stanje="prazno", ukupno=0, vratiti=None)
    d.pop("t", None)
    d["naziv"] = NAZIV
    return d


def modul_ukljucen():
    """Za česta pitanja zaslona (2 u sekundi) bez otvaranja baze svaki put."""
    if time.time() - _modul["provjereno"] > 5:
        con = spoji()
        try:
            _modul["ukljucen"] = postavke.ukljucen(con, "zaslon_kupca")
        finally:
            con.close()
        _modul["provjereno"] = time.time()
    return _modul["ukljucen"]


def osvjezi_modul(con):
    _modul["ukljucen"] = postavke.ukljucen(con, "zaslon_kupca")
    _modul["provjereno"] = time.time()
    if not _modul["ukljucen"]:
        postavi({"stavke": []})


def stranica():
    with open(os.path.join(SCRIPT_DIR, "zaslon_kupca.html"), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- tablet u mreži

def adresa():
    return "http://%s:%d" % (lokalna_adresa(), PORT)


def stanje_mreze(con):
    ukljucen = postavke.vrijednost(con, "zaslon.mreza") == "1"
    return {"mreza": ukljucen, "radi": _server is not None, "adresa": adresa() if ukljucen else "",
            "port": PORT}


def spremi_mrezu(con, p):
    """{mreza: true/false} -> uključi/isključi poslužitelj za tablet. Vraća stanje (+ greška mreže)."""
    ukljuci = p.get("mreza") in (True, 1, "1", "true")
    postavke.postavi(con, "zaslon.mreza", "1" if ukljuci else "0")
    greska = pokreni() if ukljuci else (zaustavi() or "")
    return dict(stanje_mreze(con), greska_mreze=greska)


def pokreni():
    global _server
    with _lock:
        if _server:
            return ""
        try:
            _server = ThreadingHTTPServer(("0.0.0.0", PORT), ZaslonHandler)
        except OSError as e:
            return "Ne mogu otvoriti port %d: %s" % (PORT, e)
        threading.Thread(target=_server.serve_forever, daemon=True).start()
        return ""


def zaustavi():
    global _server
    with _lock:
        if _server:
            srv, _server = _server, None
            threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()), daemon=True).start()


def pri_pokretanju(con):
    """Iz kasa.main() i nakon spremanja Postavki. Vraća poruku za konzolu ili ''."""
    osvjezi_modul(con)
    if postavke.vrijednost(con, "zaslon.mreza") == "1" and _modul["ukljucen"]:
        return pokreni() or adresa()
    zaustavi()
    return ""


class ZaslonHandler(BaseHTTPRequestHandler):
    """Tablet u mreži: samo čitanje. Stranica zaslona i trenutna košarica; sve ostalo 404."""

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

    def do_GET(self):
        put = urlparse(self.path).path
        if not modul_ukljucen():
            return self._send(403, {"greska": "Zaslon za kupca je isključen na računalu."})
        if put in ("/", "/zaslon"):
            return self._send(200, stranica(), "text/html; charset=utf-8")
        if put == "/api/zaslon":
            return self._send(200, procitaj())
        return self._send(404, {"greska": "Nema."})

    def do_POST(self):
        return self._send(405, {"greska": "Samo prikaz."})
