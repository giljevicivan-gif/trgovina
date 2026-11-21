# -*- coding: utf-8 -*-
"""
Provjera sata računala.

Porezna javlja grešku 174 ("Izdani račun kasni u sustav fiskalizacije za više od
10 minuta do 2 dana") kad je vrijeme na računu više od 10 minuta prije trenutka kad
je račun stigao. Ako sat računala kasni, svaki račun izgleda zakašnjelo iako je
poslan odmah. U podacima iz Robe (2026.) sat je kasnio oko 2 sekunde dnevno:
1 minuta u siječnju, 4 minute u travnju, a u listopadu više od 10 minuta.

Razlika se mjeri iz zaglavlja Date u HTTPS odgovoru poslužitelja (Porezna kod
slanja računa, inače GitHub jednom na sat). Program sat ne mijenja sam; samo
upozori u Kontroli i u postavkama fiskalizacije.
"""

import datetime as dt
import email.utils
import os
import threading
import time
import urllib.error
import urllib.request

import postavke
from baza import spoji

URL = os.environ.get("TRGOVINA_SAT_URL") or "https://api.github.com"
SVAKIH = 3600          # sekundi između provjera u pozadini
ZUTO = 60              # sekundi razlike: upozorenje
CRVENO = 5 * 60        # sekundi razlike: Porezna uskoro javlja grešku 174 (10 min)

_stanje = {"razlika": None, "izvor": "", "kada": 0.0, "radi": False}
_lock = threading.Lock()


def razlika_iz_date(zaglavlje, primljeno=None):
    """Sekunde za koje sat računala kasni (pozitivno) ili žuri (negativno) prema
    zaglavlju HTTP Date; None ako zaglavlja nema ili nije ispravno."""
    if not zaglavlje:
        return None
    try:
        server = email.utils.parsedate_to_datetime(zaglavlje)
    except (TypeError, ValueError, IndexError):
        return None
    if server is None or server.tzinfo is None:
        return None
    lokalno = primljeno or dt.datetime.now(dt.timezone.utc)
    return (server - lokalno).total_seconds()


def zabiljezi(razlika, izvor):
    """Spremi izmjerenu razliku (u memoriji i u postavkama, da je Kontrola vidi i nakon ponovnog pokretanja)."""
    if razlika is None:
        return
    with _lock:
        prije, kada = _stanje["razlika"], _stanje["kada"]
        _stanje.update(razlika=razlika, izvor=izvor, kada=time.time())
    if prije is not None and abs(prije - razlika) < 5 and time.time() - kada < 600:
        return  # ne pisati u bazu kod svakog računa
    try:
        con = spoji()
        try:
            postavke.postavi(con, "sat.razlika", "%.0f" % razlika)
            postavke.postavi(con, "sat.izvor", izvor)
            postavke.postavi(con, "sat.provjereno", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        finally:
            con.close()
    except Exception:
        pass


def zabiljezi_odgovor(odgovor, izvor):
    """Iz HTTP odgovora (urllib) uzme zaglavlje Date i zabilježi razliku."""
    try:
        zabiljezi(razlika_iz_date(odgovor.headers.get("Date")), izvor)
    except Exception:
        pass


def izmjeri(timeout=8):
    """Jedno mjerenje prema GitHubu (isti poslužitelj kao nadogradnja). Bez interneta None."""
    try:
        z = urllib.request.Request(URL, method="HEAD", headers={"User-Agent": "trgovina-kasa"})
        with urllib.request.urlopen(z, timeout=timeout) as r:
            razlika = razlika_iz_date(r.headers.get("Date"))
    except urllib.error.HTTPError as e:  # i odgovor s greškom ima Date
        razlika = razlika_iz_date(e.headers.get("Date") if e.headers else None)
    except Exception:
        return None
    zabiljezi(razlika, "internet")
    return razlika


def pokreni():
    """Pozadinska provjera jednom na sat (prva minutu nakon pokretanja)."""
    if _stanje["radi"]:
        return
    _stanje["radi"] = True

    def petlja():
        time.sleep(60)
        while True:
            if time.time() - _stanje["kada"] > SVAKIH - 60:  # Porezna je možda već izmjerila
                izmjeri()
            time.sleep(SVAKIH)

    threading.Thread(target=petlja, daemon=True, name="sat-provjera").start()


def opis(razlika):
    s = abs(int(round(razlika)))
    koliko = ("%d min %d s" % (s // 60, s % 60)) if s >= 60 else ("%d s" % s)
    return ("Sat računala kasni %s." if razlika > 0 else "Sat računala žuri %s.") % koliko


def zadnje(con):
    """(razlika u sekundama, kada) zadnjeg mjerenja ili (None, '')."""
    v = postavke.vrijednost(con, "sat.razlika", "")
    try:
        return float(v), postavke.vrijednost(con, "sat.provjereno", "")
    except ValueError:
        return None, ""


def upozorenje(con):
    """Tekst upozorenja ili '' ako je sat točan (ili još nije provjeren)."""
    razlika, _ = zadnje(con)
    if razlika is None or abs(razlika) < ZUTO:
        return ""
    return (opis(razlika) + " Porezna uspoređuje vrijeme na računu sa svojim satom: ako sat računala kasni "
            "više od 10 minuta, svaki račun javi kao zakašnjeli (greška 174), a ako žuri, račun je \"iz budućnosti\" "
            "(greška 177). U Windowsu: Postavke, Vrijeme i jezik, "
            "Datum i vrijeme, uključite \"Automatski postavi vrijeme\" i kliknite \"Sinkroniziraj sada\".")


def kontrola(con):
    """Provjera za karticu Kontrola (format kao kontrola._p)."""
    razlika, kada = zadnje(con)
    if razlika is None or abs(razlika) < ZUTO:
        return []
    return [{"razina": "crveno" if abs(razlika) >= CRVENO else "zuto", "naslov": "Sat računala nije točan",
             "opis": upozorenje(con) + (" (provjereno %s)" % kada[:16] if kada else ""),
             "ukupno": 0, "stavke": [], "stupci": []}]
