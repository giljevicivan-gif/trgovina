# -*- coding: utf-8 -*-
"""
HACCP upis s mobitela.

Na računalu (HACCP -> Upis s mobitela) uključi se mali poslužitelj za mobitel
(port 8768) i pokaže adresu i PIN. Na mobitelu spojenom na isti WiFi otvori
se ta adresa, upiše PIN i ime, i upisuju se temperature, čišćenje i prijem
robe. Isto kao na računalu: ista pravila i iste provjere (haccp.py).

Sigurnost: glavni program i dalje sluša samo na ovom računalu (127.0.0.1).
Poslužitelj za mobitel zna SAMO HACCP upis (ne vidi cijene, promet ni
artikle), traži PIN, a poslije 10 krivih PIN-ova se zaključa do novog PIN-a.
Poništavanje zapisa radi se samo na računalu. Uključen ostaje i nakon
ponovnog pokretanja programa, dok se ne isključi.
"""

import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import haccp
import postavke
from baza import SCRIPT_DIR, spoji
from inventura import lokalna_adresa
from nabava import Greska

PORT = int(os.environ.get("TRGOVINA_HACCP_PORT") or 8768)
MAX_KRIVIH_PINOVA = 10

_server = None
_lock = threading.Lock()
_tokeni = {}          # token -> (pin u trenutku prijave, ime)
_krivi = {"n": 0}


def _postavka(con, k, zadano=""):
    r = con.execute("SELECT vrijednost FROM postavke WHERE kljuc = ?", ("haccp." + k,)).fetchone()
    return r[0] if r else zadano


def _postavi(con, k, v):
    with con:
        con.execute("INSERT OR REPLACE INTO postavke (kljuc, vrijednost) VALUES (?, ?)", ("haccp." + k, v))


def adresa():
    return "http://%s:%d" % (lokalna_adresa(), PORT)


def stanje(con):
    ukljucen = _postavka(con, "mobitel") == "1"
    return {"ukljucen": ukljucen, "radi": _server is not None,
            "adresa": adresa() if ukljucen else "", "pin": _postavka(con, "pin") if ukljucen else "",
            "zakljucano": _krivi["n"] >= MAX_KRIVIH_PINOVA, "spojeni": sorted({t[1] for t in _tokeni.values()})}


def ukljuci(con, novi_pin=False):
    """Uključi upis s mobitela (i novi PIN ako se traži). Vraća grešku mreže ili ''."""
    if novi_pin or not _postavka(con, "pin"):
        _postavi(con, "pin", "%06d" % secrets.randbelow(1000000))
        _tokeni.clear()
        _krivi["n"] = 0
    _postavi(con, "mobitel", "1")
    return pokreni()


def iskljuci(con):
    _postavi(con, "mobitel", "0")
    zaustavi()


def pokreni():
    global _server
    with _lock:
        if _server:
            return ""
        try:
            _server = ThreadingHTTPServer(("0.0.0.0", PORT), HaccpMobitelHandler)
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
        _tokeni.clear()


def pri_pokretanju(con):
    """Iz main(): ako je bilo uključeno, uključi opet. Vraća poruku za konzolu ili ''."""
    if _postavka(con, "mobitel") == "1" and postavke.ukljucen(con, "haccp"):
        return pokreni() or adresa()
    return ""


def _stranica():
    with open(os.path.join(SCRIPT_DIR, "haccp_mobitel.html"), encoding="utf-8") as f:
        return f.read()


def _pregled(con):
    """Samo ono što treba za upis: uređaji, plan, današnji zapisi."""
    p = haccp.pregled(con)
    return {"datum": p["datum"],
            "uredjaji": [{"id": u["id"], "naziv": u["naziv"], "min_t": u["min_t"], "max_t": u["max_t"],
                          "danas": [{"vrijeme": t["vrijeme"], "temperatura": t["temperatura"], "tko": t["tko"],
                                     "ponisteno": bool(t["ponisteno"])} for t in u["danas"]]}
                         for u in p["uredjaji"] if u["aktivan"]],
            "plan": [{"id": x["id"], "podrucje": x["podrucje"], "ucestalost": x["ucestalost"],
                      "nacin": x["nacin"], "napravljeno": x["napravljeno"]} for x in p["plan"]],
            "prijem": [{"vrijeme": x["vrijeme"], "dobavljac": x["dobavljac"], "roba": x["roba"],
                        "prihvaceno": x["prihvaceno"], "ponisteno": bool(x["ponisteno"])}
                       for x in p["prijem"] if x["vrijeme"][:10] == p["datum"]],
            "dobavljaci": [r[0] for r in con.execute(
                "SELECT naziv FROM partneri WHERE naziv <> '' ORDER BY naziv")]}


class HaccpMobitelHandler(BaseHTTPRequestHandler):
    """Zna SAMO HACCP upis. Sve ostalo je 404."""

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

    def _dozvoljeno(self, con):
        """None ako je sve u redu, inače (kod, poruka)."""
        if not postavke.ukljucen(con, "haccp") or _postavka(con, "mobitel") != "1":
            return 403, "HACCP upis s mobitela je isključen na računalu."
        return None

    def _prijavljen(self, con):
        t = _tokeni.get(self.headers.get("X-Token", ""))
        if not t or t[0] != _postavka(con, "pin"):
            return None
        return t

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/m"):
            return self._send(200, _stranica(), "text/html; charset=utf-8")
        if u.path != "/m/api/pregled":
            return self._send(404, {"greska": "Nema."})
        con = spoji()
        try:
            x = self._dozvoljeno(con)
            if x:
                return self._send(x[0], {"greska": x[1]})
            if not self._prijavljen(con):
                return self._send(401, {"greska": "Prijavite se PIN-om."})
            return self._send(200, _pregled(con))
        finally:
            con.close()

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        if n > 10000:
            return self._send(413, {"greska": "Prevelik zahtjev."})
        try:
            p = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(p, dict):
                raise ValueError
        except ValueError:
            return self._send(400, {"greska": "Neispravan zahtjev."})
        con = spoji()
        con.isolation_level = None
        try:
            x = self._dozvoljeno(con)
            if x:
                return self._send(x[0], {"greska": x[1]})
            if u.path == "/m/api/prijava":
                if _krivi["n"] >= MAX_KRIVIH_PINOVA:
                    return self._send(403, {"greska": "Previše krivih PIN-ova. Na računalu kliknite Novi PIN."})
                pin = _postavka(con, "pin")
                if not pin or not secrets.compare_digest(str(p.get("pin") or "").strip(), pin):
                    _krivi["n"] += 1
                    return self._send(403, {"greska": "Krivi PIN."})
                ime = " ".join(str(p.get("ime") or "").split())[:40]
                if not ime:
                    return self._send(400, {"greska": "Upišite ime (tko upisuje)."})
                token = secrets.token_hex(16)
                _tokeni[token] = (pin, ime)
                return self._send(200, {"token": token, "ime": ime})
            t = self._prijavljen(con)
            if not t:
                return self._send(401, {"greska": "Prijavite se PIN-om."})
            fn = {"/m/api/temperatura": haccp.upisi_temperaturu, "/m/api/ciscenje": haccp.upisi_ciscenje,
                  "/m/api/prijem": haccp.upisi_prijem}.get(u.path)
            if not fn:
                return self._send(404, {"greska": "Nema."})
            p = {k: v for k, v in p.items() if k != "vrijeme"}  # s mobitela uvijek "sada"
            p["tko"] = t[1]
            fn(con, p)
            return self._send(200, {"ok": True})
        except Greska as e:
            return self._send(400, {"greska": str(e)})
        except (TypeError, ValueError):
            return self._send(400, {"greska": "Neispravan zahtjev."})
        finally:
            con.close()
