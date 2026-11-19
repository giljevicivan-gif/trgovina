"""Pokretač kase za instaliranu verziju (Setup).

Pokreće se s pythonw.exe (bez crnog prozora); poruke idu u datoteku kasa.log u mapi podataka.
Postavke (mapa Robe, naziv, mapa podataka, port) čita iz postavke.cmd koji piše instalacija.

    pythonw pokreni.py                      pokreni kasu (ako već radi, samo otvori preglednik)
    pythonw pokreni.py --zatvori            zatvori kasu koja radi
    pythonw pokreni.py --nakon-nadogradnje  pričekaj da se stara verzija ugasi, pa pokreni (bez preglednika)
"""
import os
import re
import secrets
import socket
import sys
import time
import traceback
import urllib.request

OVDJE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(OVDJE)  # mapa instalacije (u njoj su python\, program\, postavke.cmd)


def procitaj_postavke():
    """set "IME=vrijednost" retci iz postavke.cmd (REM retci se preskaču)."""
    put = os.path.join(APP, "postavke.cmd")
    if not os.path.exists(put):
        return
    with open(put, encoding="utf-8-sig", errors="replace") as f:
        for red in f:
            m = re.match(r'\s*set\s+"?([A-Za-z_][A-Za-z0-9_]*)=(.*?)"?\s*$', red, re.I)
            if m and not os.environ.get(m.group(1)):
                os.environ[m.group(1)] = m.group(2)


def pripremi_okolinu():
    procitaj_postavke()
    podaci = os.environ.get("TRGOVINA_PODACI") or r"C:\TrgovinaPodaci"
    os.environ["TRGOVINA_PODACI"] = podaci
    os.makedirs(podaci, exist_ok=True)
    os.environ.setdefault("TRGOVINA_DB", os.path.join(podaci, "trgovina.db"))
    os.environ.setdefault("TRGOVINA_KOPIJE", os.path.join(podaci, "sigurnosne_kopije"))
    os.environ.setdefault("TRGOVINA_CERT_MAPA", os.path.join(podaci, "certifikati"))
    os.environ.setdefault("TRGOVINA_PORT", "8790")
    os.chdir(podaci)
    return podaci


def poruka(tekst, greska=False):
    print(tekst)
    if os.name == "nt" and sys.stdout is not sys.__stdout__:  # bez prozora: pokaži okvir s porukom
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, tekst, "Kasa", 0x10 if greska else 0x40)
        except Exception:
            pass


def port():
    return int(os.environ.get("TRGOVINA_PORT") or 8790)


def zauzet(p):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", p)) == 0
    finally:
        s.close()


def nasa_kasa(p):
    """Je li na portu naša kasa (a ne neki drugi program)?"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/verzija.txt" % p, timeout=3) as r:
            return r.read(200).decode("utf-8", "replace").startswith("trgovina")
    except Exception:
        return False


def kljuc_datoteka():
    return os.path.join(os.environ["TRGOVINA_PODACI"], "kasa_zatvori.kljuc")


def zatvori():
    """Zatvori kasu koja radi (ključ je u mapi podataka, pa to može samo ovo računalo)."""
    p = port()
    if not zauzet(p):
        return poruka("Kasa nije pokrenuta.")
    try:
        with open(kljuc_datoteka(), encoding="utf-8") as f:
            k = f.read().strip()
        req = urllib.request.Request("http://127.0.0.1:%d/zatvori" % p, data=k.encode(), method="POST")
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        return poruka("Kasa se nije dala zatvoriti (%s)." % e, True)
    for _ in range(20):
        if not zauzet(p):
            return poruka("Kasa je zatvorena.")
        time.sleep(0.5)
    poruka("Kasa se još zatvara.")


def preusmjeri_ispis(podaci):
    """Bez prozora (pythonw) nema konzole: ispis ide u kasa.log (najviše ~1 MB, stari se čuva kao .1)."""
    if sys.stdout is not None:
        return
    log = os.path.join(podaci, "kasa.log")
    try:
        if os.path.getsize(log) > 1_000_000:
            os.replace(log, log + ".1")
    except OSError:
        pass
    f = open(log, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = f
    print("\n===== %s =====" % time.strftime("%d.%m.%Y %H:%M:%S"))


def main():
    podaci = pripremi_okolinu()
    preusmjeri_ispis(podaci)
    if "--zatvori" in sys.argv:
        return zatvori()
    p = port()
    if "--nakon-nadogradnje" in sys.argv:
        os.environ["TRGOVINA_BEZ_PREGLEDNIKA"] = "1"  # stranica se sama osvježi
        for _ in range(60):
            if not zauzet(p):
                break
            time.sleep(0.5)
    if zauzet(p):
        if nasa_kasa(p):
            import webbrowser
            webbrowser.open("http://127.0.0.1:%d" % p)
            return
        return poruka("Port %d već koristi neki DRUGI program na ovom računalu.\n\n"
                      "Ponovno pokrenite Setup kase ili u datoteci postavke.cmd (mapa programa) "
                      "promijenite TRGOVINA_PORT, npr. na %d." % (p, p + 1), True)

    with open(kljuc_datoteka(), "w", encoding="utf-8") as f:
        f.write(secrets.token_hex(16))

    argv = [os.path.join(OVDJE, "kasa.py")]
    roba = (os.environ.get("ROBA") or "").strip()
    if roba:
        from uvoz import godine_u_mapi
        try:
            ima = os.path.isdir(roba) and godine_u_mapi(roba)
        except Exception:
            ima = False
        if ima:
            argv.append(roba)
        else:
            poruka("Mapa programa Roba \"%s\" ne postoji ili u njoj nema godišnjih mapa.\n\n"
                   "Kasa se pokreće bez uvoza iz Robe. Mapu promijenite ponovnim pokretanjem Setupa." % roba)
    sys.argv = argv
    try:
        import kasa
        kasa.main()
    except SystemExit as e:
        if e.code not in (None, 0):
            poruka("Kasa se zaustavila: %s" % e.code, True)
    except Exception as e:
        traceback.print_exc()
        poruka("Kasa se nije mogla pokrenuti:\n%s\n\nDetalji su u datoteci %s"
               % (e, os.path.join(podaci, "kasa.log")), True)


if __name__ == "__main__":
    main()
