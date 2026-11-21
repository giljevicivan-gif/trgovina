# -*- coding: utf-8 -*-
"""
Izravni ispis računa na termalni pisač 80 mm (ESC/POS) i otvaranje ladice (modul "termo_pisac").

Bez modula (ili kad pisač ne radi) kasa otvara obični prozor za ispis (/ispis/<id>), kao i prije.

Veza s pisačem (Dodaci, Termalni pisač):
    windows  pisač instaliran u Windowsima (USB): podaci idu kroz spooler u RAW obliku (winspool.drv)
    usb      USB pisač bez instaliranog drivera: ravno u uređaj kroz Windowsov ugrađeni usbprint.sys
    mreza    mrežni pisač IP:port (obično 9100)
    com      serijski port (COM3) ili USB pisač koji se javlja kao COM port

Račun se slaže kao obični tekst (isti podaci kao ispis_html / ispis_fiskalni_html u kasa.py):
podaci obveznika, broj računa, operater, stavke, ukupno, PDV, ZKI, JIR i QR kod (naredbe GS ( k).
Upozorenja ostaju ista: "OVO NIJE FISKALIZIRANI RAČUN" (vježba i test), račun bez JIR-a.

Ispis se poziva tek kad je račun spremljen (zaseban zahtjev iz preglednika), pa problem s pisačem
nikad ne poništi prodaju. Mreža, USB i COM imaju vremensko ograničenje od 5 sekundi.
"""

import datetime as dt
import os
import re
import socket
import subprocess
import threading
import unicodedata

import fiskal
import postavke
from nabava import Greska

NAZIV = os.environ.get("TRGOVINA_NAZIV", "Trgovina")
TIMEOUT = 5.0
_BRAVA = threading.Lock()  # jedan ispis odjednom (dva računa se ne smiju izmiješati)

# ---------------------------------------------------------------- ESC/POS naredbe
ESC, GS, LF = b"\x1b", b"\x1d", b"\n"
INIT = ESC + b"@"
KODNE = {  # ključ: (python kodna stranica, broj za ESC t n, naziv); broj na početku naziva = broj na probi
    "cp852": ("cp852", 18, "1. CP852, ESC t 18 (većina pisača)"),
    "cp1250": ("cp1250", 45, "2. CP1250, ESC t 45"),
    "iso2_39": ("iso8859_2", 39, "3. ISO 8859-2, ESC t 39"),
    "cp852_54": ("cp852", 54, "4. CP852, ESC t 54"),
    "cp1250_72": ("cp1250", 72, "5. CP1250, ESC t 72"),
    "cp852_5": ("cp852", 5, "6. CP852, ESC t 5"),
    "cp1250_33": ("cp1250", 33, "7. CP1250, ESC t 33"),
    "cp852_9": ("cp852", 9, "8. CP852, ESC t 9"),
    "cp852_2": ("cp852", 2, "9. CP852, ESC t 2"),
    "cp1250_17": ("cp1250", 17, "10. CP1250, ESC t 17"),
    "cp1250_30": ("cp1250", 30, "11. CP1250, ESC t 30"),
    "cp852_13": ("cp852", 13, "12. CP852, ESC t 13"),
    "cp1250_50": ("cp1250", 50, "13. CP1250, ESC t 50"),
    "cp852_32": ("cp852", 32, "14. CP852, ESC t 32"),
    "cp852_68": ("cp852", 68, "15. CP852, ESC t 68"),
    "iso2_36": ("iso8859_2", 36, "16. ISO 8859-2, ESC t 36"),
    "cp1250_18": ("cp1250", 18, "17. CP1250, ESC t 18"),
    "cp852_6": ("cp852", 6, "18. CP852, ESC t 6"),
    "ascii": ("ascii", None, "bez kvačica (č -> c)"),
}
# Kineski pisači (Xprinter, Winpos, MicroPOS...) često su u "kineskom" načinu: bajt iznad 127 s idućim
# bajtom čine kineski znak, pa č ć ž š đ izlaze kao kocke. FS . ga isključuje (Epson ga zanemari).
BEZ_KINESKOG = b"\x1c."
PRESLOVI = str.maketrans({"č": "c", "ć": "c", "š": "s", "ž": "z", "đ": "dj", "Č": "C", "Ć": "C", "Š": "S",
                          "Ž": "Z", "Đ": "Dj", "€": "EUR", "×": "x", "–": "-", "—": "-", "„": '"', "“": '"',
                          "”": '"', "‘": "'", "’": "'", "…": "...", "\u00a0": " "})


def kodiraj(tekst, kodna="cp852"):
    """Tekst u bajtove za pisač; znak koji kodna stranica nema preslovi se (č -> c), inače '?'."""
    enc = KODNE.get(kodna, KODNE["cp852"])[0]
    out = bytearray()
    for z in str(tekst):
        try:
            out += z.encode(enc)
            continue
        except UnicodeEncodeError:
            pass
        zam = z.translate(PRESLOVI)
        if zam == z:
            zam = unicodedata.normalize("NFKD", z).encode("ascii", "ignore").decode("ascii") or "?"
        out += zam.encode(enc, "replace")
    return bytes(out)


def qr_naredbe(podaci, velicina=5):
    """QR kod (model 2, ispravljanje grešaka M) naredbama GS ( k; podaci su bajtovi."""
    velicina = max(1, min(16, int(velicina)))
    n = len(podaci) + 3
    if n > 7092:
        raise Greska("Predug tekst za QR kod.")
    return (GS + b"(k\x04\x00\x31\x41\x32\x00"            # fn 165: model 2
            + GS + b"(k\x03\x00\x31\x43" + bytes([velicina])  # fn 167: veličina modula (točke)
            + GS + b"(k\x03\x00\x31\x45\x31"              # fn 169: ispravljanje grešaka M
            + GS + b"(k" + bytes([n & 0xFF, n >> 8]) + b"\x31\x50\x30" + podaci  # fn 180: spremi
            + GS + b"(k\x03\x00\x31\x51\x30")             # fn 181: ispiši


def ladica_naredba(pin=2):
    """ESC p m t1 t2: impuls 50 ms uključeno / 500 ms isključeno; pin 2 (m=0) ili pin 5 (m=1)."""
    return ESC + b"p" + (b"\x01" if str(pin) == "5" else b"\x00") + b"\x19\xfa"


REZ = GS + b"V\x42\x00"  # GS V 66 0: djelomično rezanje


# ---------------------------------------------------------------- postavke

ZADANO = {"veza": "windows", "pisac": "", "adresa": "", "com": "COM3", "usb": "", "brzina": "", "sirina": "80",
          "znakova": "", "kodna": "cp852", "rezanje": "1", "redova": "4", "ladica": "1", "ladica_pin": "2",
          "qr": "1", "qr_velicina": "5", "papir": "1"}


def _v(con, k):
    return postavke.vrijednost(con, "tisak." + k, ZADANO[k])


def postavke_modula(con):
    p = {k: _v(con, k) for k in ZADANO}
    p["znakova_sada"] = znakova(p)
    p["zadnje"] = postavke.vrijednost(con, "tisak.zadnje", "")
    p["zadnja_greska"] = postavke.vrijednost(con, "tisak.zadnja_greska", "")
    p["windows"] = os.name == "nt"
    p["kodne"] = [[k, v[2]] for k, v in KODNE.items()]
    return p


def znakova(p):
    z = str(p.get("znakova") or "").strip()
    if z.isdigit() and 24 <= int(z) <= 64:
        return int(z)
    return 32 if str(p.get("sirina")) == "58" else 42  # 42 stane na svaki 80 mm pisač (neki imaju 48)


def spremi_postavke(con, p):
    """Mijenja samo poslana polja; ostala ostaju kako su spremljena."""
    sada = {k: _v(con, k) for k in ZADANO}
    d = lambda k: str(p[k] if p.get(k) is not None else sada[k]).strip()
    da = lambda k: ("1" if p[k] in (True, 1, "1", "true") else "0") if k in p else sada[k]
    veza = d("veza")
    if veza not in ("windows", "usb", "mreza", "com"):
        raise Greska("Veza: Windows pisač, USB bez drivera, mreža ili COM port.")
    adresa = d("adresa")
    if veza == "mreza":
        _host_port(adresa)  # provjera
    com = d("com")
    if veza == "com" and not com:
        raise Greska("Upišite COM port (npr. COM3).")
    brzina = d("brzina")
    if brzina and brzina not in ("9600", "19200", "38400", "57600", "115200"):
        raise Greska("Brzina COM porta: 9600, 19200, 38400, 57600 ili 115200 (ili prazno).")
    sirina = d("sirina")
    if sirina not in ("80", "58"):
        raise Greska("Širina papira: 80 ili 58 mm.")
    zn = d("znakova")
    if zn and not (zn.isdigit() and 24 <= int(zn) <= 64):
        raise Greska("Znakova u retku: od 24 do 64 (ili prazno = 42 za 80 mm, 32 za 58 mm).")
    kodna = d("kodna")
    if kodna not in KODNE:
        raise Greska("Nepoznata kodna stranica.")
    redova = d("redova")
    if not (redova.isdigit() and int(redova) <= 15):
        raise Greska("Praznih redova prije rezanja: od 0 do 15.")
    qv = d("qr_velicina")
    if not (qv.isdigit() and 1 <= int(qv) <= 16):
        raise Greska("Veličina QR koda: od 1 do 16 (obično 5).")
    pin = d("ladica_pin")
    if pin not in ("2", "5"):
        raise Greska("Ladica: pin 2 ili pin 5.")
    vals = {"veza": veza, "pisac": d("pisac")[:200], "adresa": adresa[:100], "com": com[:60], "usb": d("usb")[:400], "brzina": brzina,
            "sirina": sirina, "znakova": zn, "kodna": kodna, "rezanje": da("rezanje"), "redova": redova,
            "ladica": da("ladica"), "ladica_pin": pin, "qr": da("qr"), "qr_velicina": qv, "papir": da("papir")}
    for k, v in vals.items():
        postavke.postavi(con, "tisak." + k, v)
    _ne_javlja.clear()
    return postavke_modula(con)


# ---------------------------------------------------------------- slaganje računa
# Račun je popis redaka: ("t", tekst, {"b": podebljano, "c": centrirano, "v": veliko}), ("qr", tekst), ("crta",)

def _eur(c):
    return ("%.2f" % (c / 100.0)).replace(".", ",")


def _kol(k):
    return ("%.3f" % k).rstrip("0").rstrip(".").replace(".", ",")


def prelomi(tekst, w):
    """Riječi u retke najviše w znakova; predugačka riječ (ZKI, JIR) se reže."""
    out = []
    for odlomak in str(tekst).splitlines() or [""]:
        red = ""
        for rijec in odlomak.split():
            while len(rijec) > w:
                if red:
                    out.append(red)
                    red = ""
                out.append(rijec[:w])
                rijec = rijec[w:]
            if not red:
                red = rijec
            elif len(red) + 1 + len(rijec) <= w:
                red += " " + rijec
            else:
                out.append(red)
                red = rijec
        out.append(red)
    return out


def lijevo_desno(lijevo, desno, w):
    """'UKUPNO EUR          12,34' točno w znakova; kad ne stane, iznos ide u novi red desno."""
    if len(lijevo) + 1 + len(desno) <= w:
        return [lijevo + " " * (w - len(lijevo) - len(desno)) + desno]
    return prelomi(lijevo, w) + [desno.rjust(w)]


def slozi_racun(r, w=48, naziv=NAZIV):
    """Redovi računa iz kasa.racun(con, rid)."""
    R = []
    t = lambda s, **o: R.append(("t", s, o))

    def okvir(*redovi):  # upozorenje između dvije crte zvjezdica, podebljano
        if not R or R[-1] != ("crta", "*"):  # dva upozorenja zaredom dijele crtu
            R.append(("crta", "*"))
        for red in redovi:
            for x in prelomi(red, w):
                t(x, b=1, c=1)
        R.append(("crta", "*"))

    f = r.get("fiskal")
    vrijeme = dt.datetime.strptime(r["vrijeme"], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y. %H:%M:%S")
    test = bool(f) and f["okolina"] == "test"
    if f:
        for x in prelomi(f["naziv_tvrtke"] or naziv, w):
            t(x, b=1, c=1)
        for red in f["adresa"].splitlines():
            if red.strip():
                for x in prelomi(red.strip(), w):
                    t(x, c=1)
        t("OIB: " + f["oib"], c=1)
        if test:
            okvir("OVO NIJE FISKALIZIRANI RAČUN", "TEST FISKALIZACIJA (DEMO), NIJE VAŽEĆI RAČUN")
        if not f["jir"]:
            okvir("Račun još nije potvrđen u Poreznoj (bez JIR-a).", "Šalje se naknadno.")
        if r["storno_od"]:
            t("STORNO računa %s" % (f.get("storno_od") or "K-%s-%s" % (r["o_godina"], r["o_broj"])), b=1, c=1)
        for x in ("Račun br. " + f["oznaka"], "Datum: " + vrijeme, "Operater: " + (f["operater"] or "-"),
                  "Plaćanje: " + r["placanje"], "Interni broj: " + r["oznaka"]):
            for y in prelomi(x, w):
                t(y)
    else:
        for x in prelomi(naziv, w):
            t(x, b=1, c=1)
        okvir("OVO NIJE FISKALIZIRANI RAČUN", "VJEŽBA - ne izdaje se kupcu")
        if r["storno_od"]:
            t("STORNO računa K-%d-%d" % (r["o_godina"], r["o_broj"]), b=1, c=1)
        for x in ("Dokument: " + r["oznaka"], "Vrijeme: " + vrijeme, "Plaćanje: " + r["placanje"]):
            for y in prelomi(x, w):
                t(y)
    k = r.get("kupac")
    if k:
        t("R1", b=1)
        mjesto = ", ".join(x for x in (k["adresa"], k["mjesto"]) if x)
        for x in ["Kupac: " + k["naziv"]] + ([mjesto] if mjesto else []) + ["OIB kupca: " + k["oib"]]:
            for y in prelomi(x, w):
                t(y)
    R.append(("crta", "-"))
    for s in r["stavke"]:
        for x in prelomi(s["naziv"], w):
            t(x)
        pop = (" -%g%%" % s["popust"]).replace(".", ",") if s["popust"] else ""
        for x in lijevo_desno("  %s %s x %s%s" % (_kol(s["kolicina"]), s["jm"], _eur(s["cijena"]), pop),
                              _eur(s["iznos"]), w):
            t(x)
    R.append(("crta", "-"))
    for x in lijevo_desno("UKUPNO EUR", _eur(r["ukupno"]), w // 2):
        t(x, b=1, v=1)
    R.append(("crta", "-"))
    if not f or f["u_pdv"]:
        for p in r["pdv"]:
            for x in lijevo_desno(("PDV %g%% na %s" % (p["stopa"], _eur(p["osnovica"]))).replace(".", ","),
                                  _eur(p["pdv"]), w):
                t(x)
    else:
        t("Obveznik nije u sustavu PDV-a.")
    if f:
        R.append(("prazno",))
        for x in prelomi("ZKI: " + f["zki"], w):
            t(x)
        for x in prelomi("JIR: " + (f["jir"] or "naknadno (nema veze s Poreznom)"), w):
            t(x)
        R.append(("qr", fiskal.qr_url(f)))
        if test:
            okvir("OVO NIJE FISKALIZIRANI RAČUN", "TEST FISKALIZACIJA (DEMO), NIJE VAŽEĆI RAČUN")
    else:
        okvir("OVO NIJE FISKALIZIRANI RAČUN")
    return R


def slozi_probni(w=48, naziv=NAZIV, kodna="cp852"):
    R = [("t", "PROBNI ISPIS - nije račun", {"b": 1, "c": 1}), ("t", naziv, {"c": 1}), ("crta", "=")]
    R += [("t", "Kvačice: č ć ž š đ  Č Ć Ž Š Đ", {}), ("t", "Kodna stranica: " + KODNE[kodna][2], {}),
          ("t", "Znakova u retku: %d" % w, {}), ("t", ("1234567890" * 7)[:w], {})]
    R += [("t", x, {}) for x in lijevo_desno("Iznos poravnan desno", "1.234,56", w)]
    R += [("t", x, {"b": 1, "v": 1}) for x in lijevo_desno("UKUPNO EUR", "12,34", w // 2)]
    R += [("t", "Vrijeme: " + dt.datetime.now().strftime("%d.%m.%Y. %H:%M:%S"), {}), ("t", "QR (probni):", {}),
          ("qr", "https://porezna.gov.hr/rn?jir=00000000-0000-0000-0000-000000000000&datv=20260101_1200&izn=1234"),
          ("crta", "=")]
    return R


def proba_kodnih(p):
    """Jedan red po kodnoj stranici, s brojem: Ivan kaže koji broj ima ispravna slova."""
    w = znakova(p)
    b = bytearray(INIT + BEZ_KINESKOG)
    b += ESC + b"a\x01" + ESC + b"E\x01" + b"PROBA HRVATSKIH SLOVA" + LF + ESC + b"E\x00" + ESC + b"a\x00"
    b += kodiraj("Dobar red: c c z s d s kvacicom ili crticom.", "ascii") + LF
    b += kodiraj("Upisite broj dobrog reda u Hrvatska slova.", "ascii") + LF
    b += b"-" * w + LF
    for k, (enc, n, naziv) in KODNE.items():
        if n is None:
            continue
        broj = naziv.split(".")[0]
        b += ESC + b"t" + bytes([n]) + ("%3s: " % broj).encode("ascii") + kodiraj("č ć ž š đ  Č Ć Ž Š Đ", k) + LF
    b += ESC + b"t\x00" + b"-" * w + LF
    b += kodiraj("Znakova u retku (sada %d): najveci broj" % w, "ascii") + LF
    b += kodiraj("ciji red stane u jedan red, s | na kraju:", "ascii") + LF
    for n in (32, 42, 48, 56, 64):
        b += (("%d " % n) + "=" * (n - 4) + "|").encode("ascii") + LF
    b += LF * int(p.get("redova") or 0)
    if p.get("rezanje", "1") == "1":
        b += REZ
    return bytes(b)


def kao_tekst(R, w=48, qr=True):
    """Pregled na ekranu (i ispis bez QR naredbi)."""
    out = []
    for e in R:
        if e[0] == "t":
            s = e[1]
            if e[2].get("v"):  # dvostruka širina: na ekranu razmaknuto slovo po slovo
                s = " ".join(s.center(w // 2) if e[2].get("c") else s).rstrip()
            elif e[2].get("c"):
                s = s.center(w).rstrip()
            out.append(s)
        elif e[0] == "crta":
            out.append(e[1] * w)
        elif e[0] == "prazno":
            out.append("")
        elif e[0] == "qr":
            out += ["[QR kod]".center(w).rstrip()] if qr else prelomi(e[1], w)
    return "\n".join(out)


def u_bajtove(R, p, ladica=False):
    """Redovi u ESC/POS bajtove prema postavkama p."""
    w = znakova(p)
    kodna = p.get("kodna") if p.get("kodna") in KODNE else "cp852"
    b = bytearray(INIT + BEZ_KINESKOG)
    if ladica:
        b += ladica_naredba(p.get("ladica_pin", "2"))
    n = KODNE[kodna][1]
    if n is not None:
        b += ESC + b"t" + bytes([n])
    for e in R:
        if e[0] == "t":
            o = e[2]
            if o.get("c"):
                b += ESC + b"a\x01"
            if o.get("b"):
                b += ESC + b"E\x01"
            if o.get("v"):
                b += GS + b"!\x11"
            b += kodiraj(e[1], kodna) + LF
            if o.get("v"):
                b += GS + b"!\x00"
            if o.get("b"):
                b += ESC + b"E\x00"
            if o.get("c"):
                b += ESC + b"a\x00"
        elif e[0] == "crta":
            b += kodiraj(e[1] * w, kodna) + LF
        elif e[0] == "prazno":
            b += LF
        elif e[0] == "qr":
            if p.get("qr", "1") == "1":
                b += ESC + b"a\x01" + qr_naredbe(e[1].encode("ascii"), p.get("qr_velicina") or 5) + LF + ESC + b"a\x00"
            else:
                for x in prelomi(e[1], w):
                    b += kodiraj(x, kodna) + LF
    b += LF * int(p.get("redova") or 0)
    if p.get("rezanje", "1") == "1":
        b += REZ
    return bytes(b)


# ---------------------------------------------------------------- slanje pisaču

def _host_port(adresa):
    m = re.fullmatch(r"\s*([A-Za-z0-9.\-]+|\[[0-9A-Fa-f:]+\])(?::(\d{1,5}))?\s*", adresa or "")
    if not m:
        raise Greska("Mrežni pisač: upišite IP adresu, npr. 192.168.1.50 ili 192.168.1.50:9100.")
    port = int(m.group(2) or 9100)
    if not 0 < port < 65536:
        raise Greska("Neispravan port mrežnog pisača.")
    return m.group(1).strip("[]"), port


def _mreza(adresa, podaci):
    host, port = _host_port(adresa)
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT) as s:
            s.settimeout(TIMEOUT)
            s.sendall(podaci)
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass
    except socket.timeout:
        raise Greska("Mrežni pisač %s:%d se ne javlja (5 s). Je li uključen i na mreži?" % (host, port))
    except OSError as e:
        raise Greska("Mrežni pisač %s:%d: %s" % (host, port, e.strerror or e))


def _com_putanja(com):
    com = com.strip()
    if os.name == "nt" and re.fullmatch(r"(?i)com\d+", com):
        return "\\\\.\\" + com.upper()  # \\.\COM3 (radi i za COM10 i više)
    return com


def _com(com, brzina, podaci):
    put = _com_putanja(com)
    if os.name == "nt" and brzina and re.fullmatch(r"(?i)com\d+", com.strip()):
        try:  # brzina i 8N1; USB pisači s virtualnim COM portom brzinu obično zanemare
            subprocess.run(["mode", "%s:" % com.strip().upper(), "BAUD=%s" % brzina, "PARITY=n", "DATA=8", "STOP=1"],
                           shell=True, capture_output=True, timeout=TIMEOUT, creationflags=0x08000000)
        except Exception:
            pass
    greska = []

    def pisi():
        try:
            with open(put, "wb", buffering=0) as fh:
                fh.write(podaci)
                fh.flush()
        except OSError as e:
            greska.append(e)

    nit = threading.Thread(target=pisi, daemon=True)
    nit.start()
    nit.join(TIMEOUT)
    if nit.is_alive():
        raise Greska("Pisač na %s ne prima podatke (5 s). Je li uključen i je li to pravi port?" % com)
    if greska:
        e = greska[0]
        raise Greska("Pisač na %s: %s" % (com, e.strerror or e))


def _windows_raw(ime, podaci, dokument="Racun"):
    """Ispis kroz Windows spooler u RAW obliku (ESC/POS ide pisaču nepromijenjen)."""
    if os.name != "nt":
        raise Greska("Ispis preko Windows pisača radi samo na Windowsima.")
    import ctypes
    from ctypes import wintypes
    ws = ctypes.WinDLL("winspool.drv", use_last_error=True)

    class DOC_INFO_1W(ctypes.Structure):
        _fields_ = [("pDocName", wintypes.LPWSTR), ("pOutputFile", wintypes.LPWSTR), ("pDatatype", wintypes.LPWSTR)]

    ws.OpenPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
    ws.OpenPrinterW.restype = wintypes.BOOL
    ws.StartDocPrinterW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(DOC_INFO_1W)]
    ws.StartDocPrinterW.restype = wintypes.DWORD
    ws.WritePrinter.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    ws.WritePrinter.restype = wintypes.BOOL
    for fn in (ws.StartPagePrinter, ws.EndPagePrinter, ws.EndDocPrinter, ws.ClosePrinter):
        fn.argtypes = [wintypes.HANDLE]
        fn.restype = wintypes.BOOL

    def greska(sto):
        kod = ctypes.get_last_error()
        return Greska("Windows pisač \"%s\": %s nije uspio (%d: %s)" % (ime, sto, kod, ctypes.FormatError(kod).strip()))

    h = wintypes.HANDLE()
    if not ws.OpenPrinterW(ime, ctypes.byref(h), None):
        raise greska("OpenPrinter")
    try:
        info = DOC_INFO_1W(dokument, None, "RAW")
        if not ws.StartDocPrinterW(h, 1, ctypes.byref(info)):
            raise greska("StartDocPrinter")
        try:
            if not ws.StartPagePrinter(h):
                raise greska("StartPagePrinter")
            buf = ctypes.create_string_buffer(podaci, len(podaci))
            poslano = 0
            while poslano < len(podaci):
                n = wintypes.DWORD(0)
                if not ws.WritePrinter(h, ctypes.byref(buf, poslano), len(podaci) - poslano, ctypes.byref(n)):
                    raise greska("WritePrinter")
                if n.value == 0:
                    raise Greska("Windows pisač \"%s\" ne prima podatke." % ime)
                poslano += n.value
            ws.EndPagePrinter(h)
        finally:
            ws.EndDocPrinter(h)
    finally:
        ws.ClosePrinter(h)


def pisaci():
    """Imena pisača instaliranih u Windowsima."""
    if os.name != "nt":
        return []
    try:
        return _enum_printers()
    except Exception:
        pass
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Printer | Select-Object -ExpandProperty Name"],
                           capture_output=True, timeout=15, creationflags=0x08000000)
        return [x.strip() for x in r.stdout.decode("utf-8", "replace").splitlines() if x.strip()]
    except Exception:
        return []


def _enum_printers():
    import ctypes
    from ctypes import wintypes
    ws = ctypes.WinDLL("winspool.drv", use_last_error=True)

    class PRINTER_INFO_4W(ctypes.Structure):
        _fields_ = [("pPrinterName", wintypes.LPWSTR), ("pServerName", wintypes.LPWSTR), ("Attributes", wintypes.DWORD)]

    ws.EnumPrintersW.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    ws.EnumPrintersW.restype = wintypes.BOOL
    zastavice = 2 | 4  # PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS
    treba, broj = wintypes.DWORD(0), wintypes.DWORD(0)
    ws.EnumPrintersW(zastavice, None, 4, None, 0, ctypes.byref(treba), ctypes.byref(broj))
    if not treba.value:
        return []
    buf = ctypes.create_string_buffer(treba.value)
    if not ws.EnumPrintersW(zastavice, None, 4, buf, treba.value, ctypes.byref(treba), ctypes.byref(broj)):
        raise OSError(ctypes.get_last_error(), "EnumPrinters")
    niz = ctypes.cast(buf, ctypes.POINTER(PRINTER_INFO_4W * broj.value)).contents
    return [x.pPrinterName for x in niz if x.pPrinterName]


# ---------------------------------------------------------------- izravno na USB (bez upravljačkog programa)
# Termalni pisači na USB-u (MicroPOS/Winpos WTP-100, Epson TM, Xprinter...) javljaju se kao "USB pisač".
# Windows za njih sam učita svoj ugrađeni upravljački program "USB Printing Support" (usbprint.sys),
# i bez ikakve instalacije: uređaj tada postoji, ali nije "pisač" u popisu pisača. Program ga traži
# kroz SetupAPI (sučelje GUID_DEVINTERFACE_USBPRINT) i piše ESC/POS bajtove ravno u uređaj (CreateFile +
# WriteFile), s vremenskim ograničenjem od 5 sekundi. Instaliran driver proizvođača ne smeta.
USBPRINT_GUID = "{28D78FAD-5A12-11D1-AE5B-0000F803A8C2}"
IOCTL_USBPRINT_GET_LPT_STATUS = 0x220030


def _winapi():
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID), ("Flags", wintypes.DWORD),
                    ("Reserved", ctypes.c_size_t)]

    class SP_DEVINFO_DATA(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", GUID), ("DevInst", wintypes.DWORD),
                    ("Reserved", ctypes.c_size_t)]

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t), ("Offset", wintypes.DWORD),
                    ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

    sa = ctypes.WinDLL("setupapi.dll", use_last_error=True)
    k = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    sa.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    sa.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    sa.SetupDiEnumDeviceInterfaces.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD,
                                               ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
    sa.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
    sa.SetupDiGetDeviceInterfaceDetailW.argtypes = [wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                                    ctypes.POINTER(SP_DEVINFO_DATA)]
    sa.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
    sa.SetupDiGetDeviceRegistryPropertyW.argtypes = [wintypes.HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD,
                                                     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, wintypes.DWORD,
                                                     ctypes.POINTER(wintypes.DWORD)]
    sa.SetupDiGetDeviceRegistryPropertyW.restype = wintypes.BOOL
    sa.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                              wintypes.DWORD, wintypes.HANDLE]
    k.CreateFileW.restype = wintypes.HANDLE
    k.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                            ctypes.POINTER(OVERLAPPED)]
    k.WriteFile.restype = wintypes.BOOL
    k.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
    k.DeviceIoControl.restype = wintypes.BOOL
    k.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD),
                                      wintypes.BOOL]
    k.GetOverlappedResult.restype = wintypes.BOOL
    k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateEventW.restype = wintypes.HANDLE
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return ctypes, wintypes, sa, k, GUID, SP_DEVICE_INTERFACE_DATA, SP_DEVINFO_DATA, OVERLAPPED


def _guid(GUID, tekst):
    import uuid
    u = uuid.UUID(tekst)
    g = GUID(u.time_low, u.time_mid, u.time_hi_version)
    for i, b in enumerate(u.bytes[8:]):
        g.Data4[i] = b
    return g


def _usb_naziv(putanja, opis):
    m = re.search(r"(?i)vid_([0-9a-f]{4})&pid_([0-9a-f]{4})", putanja or "")
    vp = " (VID %s, PID %s)" % (m.group(1).upper(), m.group(2).upper()) if m else ""
    return (opis or "USB pisač") + vp


def usb_pisaci():
    """[{"putanja", "naziv"}] za USB pisače koje Windows vidi (s drugim ili bez ikakvog drivera proizvođača)."""
    if os.name != "nt":
        return []
    ctypes, wintypes, sa, k, GUID, IFD, DID, _ = _winapi()
    g = _guid(GUID, USBPRINT_GUID)
    INVALID = wintypes.HANDLE(-1).value
    h = sa.SetupDiGetClassDevsW(ctypes.byref(g), None, None, 0x02 | 0x10)  # DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    if not h or h == INVALID:
        return []
    out = []
    try:
        i = 0
        while i < 32:
            ifd = IFD()
            ifd.cbSize = ctypes.sizeof(IFD)
            if not sa.SetupDiEnumDeviceInterfaces(h, None, ctypes.byref(g), i, ctypes.byref(ifd)):
                break
            i += 1
            treba = wintypes.DWORD(0)
            sa.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(ifd), None, 0, ctypes.byref(treba), None)
            if not treba.value:
                continue
            buf = ctypes.create_string_buffer(treba.value + 4)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize: 8 na 64-bitnom, 6 na 32-bitnom Pythonu
            ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            did = DID()
            did.cbSize = ctypes.sizeof(DID)
            if not sa.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(ifd), buf, treba.value, None, ctypes.byref(did)):
                continue
            putanja = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            opis = ""
            for prop in (0x0C, 0x00):  # SPDRP_FRIENDLYNAME, SPDRP_DEVICEDESC
                pb = ctypes.create_unicode_buffer(256)
                if sa.SetupDiGetDeviceRegistryPropertyW(h, ctypes.byref(did), prop, None, pb, ctypes.sizeof(pb), None):
                    opis = pb.value.strip()
                    if opis:
                        break
            out.append({"putanja": putanja, "naziv": _usb_naziv(putanja, opis)})
    finally:
        sa.SetupDiDestroyDeviceInfoList(h)
    return out


def _usb_odaberi(spremljeno):
    """Spremljena putanja ako je uređaj tu; inače jedini spojeni USB pisač."""
    svi = usb_pisaci()
    if not svi:
        raise Greska("Nije pronađen nijedan USB pisač. Je li pisač uključen i spojen USB kabelom? "
                     "(Ako ga Windows vidi kao COM port, odaberite vezu \"COM port\".)")
    for x in svi:
        if spremljeno and x["putanja"].lower() == spremljeno.lower():
            return x
    if len(svi) == 1:
        return svi[0]
    if spremljeno:
        # isti pisač u drugoj USB utičnici: ista VID/PID oznaka
        m = re.search(r"(?i)vid_[0-9a-f]{4}&pid_[0-9a-f]{4}", spremljeno)
        isti = [x for x in svi if m and m.group(0).lower() in x["putanja"].lower()]
        if len(isti) == 1:
            return isti[0]
    raise Greska("Spojeno je više USB pisača. U Dodaci, Termalni pisač kliknite \"Traži USB pisač\" i odaberite pravi.")


def _usb_otvori(putanja):
    ctypes, wintypes, sa, k = _winapi()[:4]
    GENERIC_RW, SHARE_RW, OPEN_EXISTING, FILE_FLAG_OVERLAPPED = 0xC0000000, 0x3, 3, 0x40000000
    h = k.CreateFileW(putanja, GENERIC_RW, SHARE_RW, None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
    if not h or h == wintypes.HANDLE(-1).value:
        kod = ctypes.get_last_error()
        if kod in (5, 32):  # pristup odbijen / zauzeto
            raise Greska("USB pisač je zauzet (drugi program ili Windows ispis ga drži). Pokušajte ponovno.")
        raise Greska("USB pisač se ne može otvoriti (%d: %s)" % (kod, ctypes.FormatError(kod).strip()))
    return h


def _usb_cekaj(k, ctypes, wintypes, h, ov, ms):
    """Čeka preklopljeni (overlapped) zahtjev; vraća broj bajtova ili None ako je isteklo vrijeme."""
    n = wintypes.DWORD(0)
    if k.WaitForSingleObject(ov.hEvent, ms) != 0:  # WAIT_OBJECT_0
        k.CancelIoEx(h, ctypes.byref(ov))
        k.GetOverlappedResult(h, ctypes.byref(ov), ctypes.byref(n), True)
        return None
    if not k.GetOverlappedResult(h, ctypes.byref(ov), ctypes.byref(n), False):
        kod = ctypes.get_last_error()
        raise Greska("USB pisač: greška kod slanja (%d: %s)" % (kod, ctypes.FormatError(kod).strip()))
    return n.value


def _usb(spremljeno, podaci):
    if os.name != "nt":
        raise Greska("Izravni USB ispis radi samo na Windowsima.")
    ctypes, wintypes, sa, k, _, _, _, OVERLAPPED = _winapi()
    uredaj = _usb_odaberi(spremljeno)
    h = _usb_otvori(uredaj["putanja"])
    ev = k.CreateEventW(None, True, False, None)
    try:
        buf = ctypes.create_string_buffer(podaci, len(podaci))
        poslano = 0
        while poslano < len(podaci):
            ov = OVERLAPPED()
            ov.hEvent = ev
            n = wintypes.DWORD(0)
            if not k.WriteFile(h, ctypes.byref(buf, poslano), len(podaci) - poslano, ctypes.byref(n), ctypes.byref(ov)):
                kod = ctypes.get_last_error()
                if kod != 997:  # ERROR_IO_PENDING
                    raise Greska("USB pisač: slanje nije uspjelo (%d: %s)" % (kod, ctypes.FormatError(kod).strip()))
            r = _usb_cekaj(k, ctypes, wintypes, h, ov, int(TIMEOUT * 1000))
            if r is None:
                raise Greska("USB pisač ne prima podatke (5 s). Je li uključen, ima li papira, je li poklopac zatvoren?")
            if r == 0:
                raise Greska("USB pisač ne prima podatke.")
            poslano += r
    finally:
        k.CloseHandle(ev)
        k.CloseHandle(h)


def _papir_usb(spremljeno):
    """Stanje papira kroz ugrađeni Windows USB upravljački program (LPT status bajt, IEEE 1284):
    "nema papira" samo kad pisač javi i "papir prazan" i "greška"; inače ne znamo (None) ili ima."""
    if os.name != "nt":
        return None
    ctypes, wintypes, sa, k, _, _, _, OVERLAPPED = _winapi()
    h = _usb_otvori(_usb_odaberi(spremljeno)["putanja"])
    ev = k.CreateEventW(None, True, False, None)
    try:
        ov = OVERLAPPED()
        ov.hEvent = ev
        st = ctypes.create_string_buffer(1)
        n = wintypes.DWORD(0)
        if not k.DeviceIoControl(h, IOCTL_USBPRINT_GET_LPT_STATUS, None, 0, st, 1, ctypes.byref(n), ctypes.byref(ov)):
            if ctypes.get_last_error() != 997:
                return None
        if not _usb_cekaj(k, ctypes, wintypes, h, ov, 1500):
            return None
        b = st.raw[0]
    finally:
        k.CloseHandle(ev)
        k.CloseHandle(h)
    if b in (0x00, 0xFF):
        return None
    PAPER_EMPTY, NOT_ERROR = 0x20, 0x08
    if b & PAPER_EMPTY and not b & NOT_ERROR:
        return {"ima": False, "pri_kraju": False}
    return {"ima": True, "pri_kraju": False}


# ---------------------------------------------------------------- stanje papira
# ESC/POS "DLE EOT 4": pisač odmah vrati jedan bajt o senzoru papira
# (bitovi 2-3 = papir pri kraju, bitovi 5-6 = nema papira). Radi za mrežni pisač
# (odgovor stiže istom vezom). USB bez drivera: LPT status bajt iz usbprint.sys. Za Windows pisač stanje se čita iz spoolera
# (PRINTER_INFO_2.Status: nema papira, offline, greška) ako ga upravljački program javlja.
# COM port se ne pita (čitanje bez vremenskog ograničenja moglo bi zaključati port).
DLE_EOT_PAPIR = b"\x10\x04\x04"
class NemaPapira(Greska):
    """Pisač javlja da nema papira (prije ili tijekom ispisa)."""


NEMA_PAPIRA = "Nestalo je papira u pisaču. Stavite novu rolu i kliknite \"Ispiši ponovno\"."


def _papir_iz_bajta(b):
    if b & 0x93 != 0x12:  # bitovi 1 i 4 su uvijek 1, bitovi 0 i 7 uvijek 0: inače to nije odgovor o papiru
        return None
    return {"ima": not (b & 0x60), "pri_kraju": bool(b & 0x0C)}


_ne_javlja = set()  # mrežni pisači koji ne odgovaraju na DLE EOT: ne pitati ih kod svakog računa


def _papir_mreza(adresa):
    host, port = _host_port(adresa)
    if (host, port) in _ne_javlja:
        return None
    try:
        with socket.create_connection((host, port), timeout=2.0) as s:
            s.settimeout(1.5)
            s.sendall(DLE_EOT_PAPIR)
            b = s.recv(1)
    except socket.timeout:
        _ne_javlja.add((host, port))
        return None
    except OSError:
        return None
    r = _papir_iz_bajta(b[0]) if b else None
    if r is None:
        _ne_javlja.add((host, port))
    return r


def _papir_windows(ime):
    if os.name != "nt" or not ime:
        return None
    import ctypes
    from ctypes import wintypes
    ws = ctypes.WinDLL("winspool.drv", use_last_error=True)
    ws.OpenPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
    ws.GetPrinterW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD)]
    ws.ClosePrinter.argtypes = [wintypes.HANDLE]
    h = wintypes.HANDLE()
    if not ws.OpenPrinterW(ime, ctypes.byref(h), None):
        return None
    try:
        treba = wintypes.DWORD(0)
        ws.GetPrinterW(h, 6, None, 0, ctypes.byref(treba))  # PRINTER_INFO_6 = samo Status
        if not treba.value:
            return None
        buf = ctypes.create_string_buffer(treba.value)
        if not ws.GetPrinterW(h, 6, buf, treba.value, ctypes.byref(treba)):
            return None
        status = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
    finally:
        ws.ClosePrinter(h)
    PAPER_OUT, PAPER_PROBLEM, OFFLINE, ERROR, NOT_AVAILABLE = 0x10, 0x40, 0x80, 0x2, 0x1000
    if status & (PAPER_OUT | PAPER_PROBLEM):
        return {"ima": False, "pri_kraju": False}
    if status & (OFFLINE | NOT_AVAILABLE):
        return {"ima": None, "pri_kraju": False, "offline": True}
    return {"ima": True, "pri_kraju": False} if not status & ERROR else None


def stanje_papira(p):
    """{"ima": True/False/None, "pri_kraju": bool} ili None ako se ne može saznati."""
    if p.get("papir", "1") != "1":
        return None
    try:
        if p["veza"] == "mreza":
            return _papir_mreza(p["adresa"])
        if p["veza"] == "windows":
            return _papir_windows(p.get("pisac"))
        if p["veza"] == "usb":
            return _papir_usb(p.get("usb"))
    except Exception:
        return None
    return None


def posalji(p, podaci, dokument="Racun"):
    """Bajtovi pisaču prema postavkama; Greska kad ne uspije."""
    if not _BRAVA.acquire(timeout=3 * TIMEOUT):
        raise Greska("Pisač je zauzet prethodnim ispisom.")
    try:
        if p["veza"] == "mreza":
            _mreza(p["adresa"], podaci)
        elif p["veza"] == "com":
            _com(p["com"], p.get("brzina"), podaci)
        elif p["veza"] == "usb":
            _usb(p.get("usb"), podaci)
        else:
            if not p.get("pisac"):
                raise Greska("Odaberite Windows pisač (Dodaci, Termalni pisač).")
            _windows_raw(p["pisac"], podaci, dokument)
    finally:
        _BRAVA.release()


def _zabiljezi(con, ok, poruka):
    t = dt.datetime.now().strftime("%d.%m.%Y. %H:%M")
    try:
        postavke.postavi(con, "tisak.zadnje" if ok else "tisak.zadnja_greska", "%s %s" % (t, poruka))
        if ok:
            postavke.postavi(con, "tisak.zadnja_greska", "")
    except Exception:
        pass


def _posalji_i_zabiljezi(con, p, podaci, opis):
    try:
        posalji(p, podaci, opis)
    except Greska as e:
        _zabiljezi(con, False, str(e))
        raise
    except Exception as e:  # npr. ctypes na neobičnom Windowsu: kasa radi dalje, ispis ide preko preglednika
        _zabiljezi(con, False, str(e))
        raise Greska("Ispis nije uspio: %s" % e)
    _zabiljezi(con, True, opis)


# ---------------------------------------------------------------- ladica i API

def gotovinom(con, rid):
    """Koliko je računa plaćeno gotovinom (centi; storno je negativan)."""
    dio = con.execute(
        """SELECT p.naziv, x.iznos FROM racun_placanja x JOIN nacini_placanja p ON p.id = x.nacin_placanja_id
           WHERE x.racun_id = ?""", (rid,)).fetchall()
    if dio:
        return sum(d[1] for d in dio if re.search(r"(?i)gotov", d[0] or ""))
    r = con.execute("""SELECT r.ukupno, p.naziv FROM racuni r JOIN nacini_placanja p ON p.id = r.nacin_placanja_id
                       WHERE r.id = ?""", (rid,)).fetchone()
    return r[0] if r and re.search(r"(?i)gotov", r[1] or "") else 0


def _smije_ladicu(con, rid):
    """Ladica se otvara samo za svježi (10 min) račun s gotovinom, ne za ponovni ispis starog računa."""
    r = con.execute("SELECT vrijeme FROM racuni WHERE id = ?", (rid,)).fetchone()
    if not r:
        raise Greska("Račun ne postoji.")
    star = (dt.datetime.now() - dt.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    return r[0] >= star and gotovinom(con, rid) != 0


def ispisi_racun(con, racun, ladica=False):
    """racun = kasa.racun(con, rid). Vraća {"ok", "ladica", "upozorenje"}.
    Prije ispisa provjeri papir (ako ga nema: Greska "papir"), a poslije javi ako je
    papir nestao usred ispisa ili je pri kraju."""
    p = postavke_modula(con)
    opis = "Račun " + ((racun.get("fiskal") or {}).get("oznaka") or racun["oznaka"])
    prije = stanje_papira(p)
    if prije and prije.get("ima") is False:
        _zabiljezi(con, False, "%s: nema papira" % opis)
        if ladica and p["ladica"] == "1" and _smije_ladicu(con, racun["id"]):
            try:  # kupac čeka ostatak: ladica se otvara i bez papira
                posalji(p, INIT + ladica_naredba(p["ladica_pin"]), "Ladica")
            except Exception:
                pass
        raise NemaPapira(NEMA_PAPIRA)
    otvori = bool(ladica) and p["ladica"] == "1" and _smije_ladicu(con, racun["id"])
    podaci = u_bajtove(slozi_racun(racun, znakova(p)), p, ladica=otvori)
    _posalji_i_zabiljezi(con, p, podaci, opis)
    out = {"ok": True, "ladica": otvori, "upozorenje": ""}
    if prije is not None:  # pisač javlja stanje: provjera i poslije ispisa
        poslije = stanje_papira(p)
        if poslije and poslije.get("ima") is False:
            _zabiljezi(con, False, "%s: papir je nestao tijekom ispisa" % opis)
            raise NemaPapira("Papir je nestao tijekom ispisa: račun možda nije cijeli. Stavite novu rolu i "
                             "kliknite \"Ispiši ponovno\".")
        if (poslije or prije).get("pri_kraju"):
            out["upozorenje"] = "Papir u pisaču je pri kraju: pripremite novu rolu."
    return out


def otvori_ladicu(con, rid=None):
    """rid: ladica nakon naplate gotovinom (blagajnik); None: gumb u postavkama (vlasnik)."""
    p = postavke_modula(con)
    if rid is not None:
        if p["ladica"] != "1" or not _smije_ladicu(con, rid):
            return {"ok": True, "ladica": False}
    _posalji_i_zabiljezi(con, p, INIT + ladica_naredba(p["ladica_pin"]), "Ladica")
    return {"ok": True, "ladica": True}


def probni(con):
    p = postavke_modula(con)
    _posalji_i_zabiljezi(con, p, u_bajtove(slozi_probni(znakova(p), NAZIV, p["kodna"]), p), "Probni ispis")
    return {"ok": True}


def proba(con):
    p = postavke_modula(con)
    _posalji_i_zabiljezi(con, p, proba_kodnih(p), "Proba hrvatskih slova")
    return {"ok": True}


def get(h, con, put, q, racun):
    """GET /api/tisak/... (racun = kasa.racun)."""
    if put == "/api/tisak/postavke":
        p = postavke_modula(con)
        zadnji = con.execute("SELECT MAX(id) FROM racuni").fetchone()[0]
        p["pregled"] = kao_tekst(slozi_racun(racun(con, zadnji), znakova(p)), znakova(p)) if zadnji else ""
        h._send(200, p)
    elif put == "/api/tisak/pisaci":
        h._send(200, {"pisaci": pisaci(), "windows": os.name == "nt"})
    elif put == "/api/tisak/usb":
        try:
            h._send(200, {"usb": usb_pisaci(), "windows": os.name == "nt"})
        except Exception as e:  # neobičan Windows: poruka umjesto pada
            raise Greska("Traženje USB pisača nije uspjelo: %s" % e)
    elif re.fullmatch(r"/api/tisak/tekst/\d+", put):
        p = postavke_modula(con)
        h._send(200, {"tekst": kao_tekst(slozi_racun(racun(con, int(put.rsplit("/", 1)[1])), znakova(p)), znakova(p))})
    else:
        raise Greska("Nepoznata putanja.")


def post(h, con, put, podaci, racun):
    """POST /api/tisak/... (racun = kasa.racun)."""
    if put == "/api/tisak/postavke":
        h._send(200, spremi_postavke(con, podaci))
    elif re.fullmatch(r"/api/tisak/racun/\d+", put):
        try:
            h._send(200, ispisi_racun(con, racun(con, int(put.rsplit("/", 1)[1])), podaci.get("ladica") in (True, 1, "1")))
        except NemaPapira as e:
            h._send(409, {"greska": str(e), "papir": True})
    elif put == "/api/tisak/ladica":
        try:
            rid = int(podaci.get("racun_id"))
        except (TypeError, ValueError):
            raise Greska("Ladica se otvara samo uz račun plaćen gotovinom.")
        h._send(200, otvori_ladicu(con, rid))
    elif put == "/api/tisak/otvori_ladicu":
        h._send(200, otvori_ladicu(con))
    elif put == "/api/tisak/proba_slova":
        h._send(200, proba(con))
    elif put == "/api/tisak/probni":
        h._send(200, probni(con))
    else:
        raise Greska("Nepoznata putanja.")
