# -*- coding: utf-8 -*-
"""
Izravna veza kase s vagom (modul "vaga_veza"): kasa sama očita težinu.

Samo standardna biblioteka (bez pyserial):
  - COM port na Windowsu preko ctypes/kernel32 (CreateFileW, SetCommState, ReadFile...),
  - TCP (IP:port) za mrežne vage i serijsko-mrežne pretvarače (npr. Moxa NPort, USR-TCP232),
    koji u načinu "TCP server" prosljeđuju bajtove s RS-232 bez izmjene.

Protokoli (predlošci u PROTOKOLI, sve se može prilagoditi u postavkama):
  kontinuirano  vaga sama stalno šalje retke, npr. "ST,GS,+  1.250kg" (CAS "stream", većina
                industrijskih indikatora). Bez oznake ST/US težina mora biti ista u dva retka zaredom.
  toledo8217    Mettler Toledo 8217 (i vage koje ga oponašaju): kasa šalje "W",
                vaga vraća STX "01.250" CR ili STX "?" <status> CR (kretanje/minus/preopterećenje).
                Izvor: opis protokola 8217, https://zipstore.ru/auxpage_data_protocol_8217_mettler_toledo_1c
                (7 bitova, parna parnost, 1 stop bit; 1200-19200 bauda; bit 0 statusa = vaga se miče).
  nci           NCI / Weigh-Tronix / Avery Berkel (SCP-01): kasa šalje "W" CR, vaga vraća
                LF "  1.250kg" CR LF <status> CR ETX; status bajt 1 bit 0 = kretanje.
                Izvor: NCI Interface Specification, https://amigopos.com/downloads/nci-protocol.pdf
                i https://cdn.chipkin.com/assets/uploads/imports/resources/Weigh-Tronix NCI Interface Specification.pdf
  cas_ecr       ECR protokol s rukovanjem ENQ/ACK/DC1 (CAS vage za kase, "Type 5"/ECR i njegove kopije):
                kasa ENQ (05h), vaga ACK (06h), kasa DC1 (11h), vaga SOH STX <S|U><predznak>"99.999"kg BCC ETX EOT.
                Izvor: opis protokola vage za kasu (Novitus), https://novitus.pl/file/get/821
                (9600 8N1; S = stabilno 500 ms, U = nestabilno, "F" na svim mjestima = preopterećenje).
  prilagodjeno  vlastiti upit (npr. "$", "\\x05") i regularni izraz s grupom (?P<kg>...).
Dibal, Bizerba i dr. obično imaju u izborniku vage "protokol za kasu"; najčešće se može odabrati
jedan od gornjih (8217 ili CAS ECR) ili kontinuirano slanje. Provjerite u priručniku vage.

Kod za COM port nije isproban na stvarnom Windowsu i vagi (pisan prema dokumentaciji WinAPI-ja).
"""

import json
import os
import re
import socket
import threading
import time

from nabava import Greska

ROK = 2.0  # sekunde za cijelo očitanje (spajanje + upit + odgovor)

STX, ETX, EOT, ENQ, ACK, NAK, SOH, DC1 = b"\x02", b"\x03", b"\x04", b"\x05", b"\x06", b"\x15", b"\x01", b"\x11"

PROTOKOLI = {
    "kontinuirano": {"naziv": "Kontinuirano (vaga stalno šalje, npr. ST,GS,+ 1.250kg)",
                     "upit": "", "brzina": 9600, "paritet": "N", "bitovi": 8, "stop": 1},
    "toledo8217": {"naziv": "Mettler Toledo 8217 (upit W)",
                   "upit": "W", "brzina": 9600, "paritet": "E", "bitovi": 7, "stop": 1},
    "nci": {"naziv": "NCI / Weigh-Tronix / Avery Berkel (upit W + CR)",
            "upit": "W\\r", "brzina": 9600, "paritet": "E", "bitovi": 7, "stop": 1},
    "cas_ecr": {"naziv": "CAS ECR (ENQ, ACK, DC1)",
                "upit": "\\x05", "brzina": 9600, "paritet": "N", "bitovi": 8, "stop": 1},
    "prilagodjeno": {"naziv": "Prilagođeno (vlastiti upit i izraz)",
                     "upit": "", "brzina": 9600, "paritet": "N", "bitovi": 8, "stop": 1},
}

ZADANO = {"veza": "com", "port": "COM1", "adresa": "", "protokol": "kontinuirano",
          "brzina": 9600, "paritet": "N", "bitovi": 8, "stop": 1, "upit": "", "izraz": ""}

KLJUC = "vaga.postavke"
_brava = threading.Lock()  # COM port smije otvoriti samo jedan zahtjev odjednom


# ---------------------------------------------------------------- postavke

def postavke(con):
    import postavke as P
    try:
        p = json.loads(P.vrijednost(con, KLJUC, "{}"))
    except ValueError:
        p = {}
    return provjeri_postavke(dict(ZADANO, **{k: v for k, v in p.items() if k in ZADANO}))


def provjeri_postavke(p):
    p = dict(ZADANO, **{k: v for k, v in (p or {}).items() if k in ZADANO})
    if p["veza"] not in ("com", "tcp"):
        raise Greska("Veza mora biti COM ili TCP.")
    if p["protokol"] not in PROTOKOLI:
        raise Greska("Nepoznat protokol vage.")
    p["port"] = str(p["port"] or "").strip().upper()
    if p["veza"] == "com" and not re.fullmatch(r"COM\d{1,3}", p["port"]):
        raise Greska("COM port upišite kao COM1, COM3...")
    p["adresa"] = str(p["adresa"] or "").strip()
    if p["veza"] == "tcp":
        _adresa(p["adresa"])
    p["brzina"] = int(p["brzina"])
    if p["brzina"] not in (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200):
        raise Greska("Brzina (baud) nije podržana.")
    p["paritet"] = str(p["paritet"]).upper()[:1]
    if p["paritet"] not in ("N", "E", "O"):
        raise Greska("Paritet: N (nema), E (parni) ili O (neparni).")
    p["bitovi"], p["stop"] = int(p["bitovi"]), int(p["stop"])
    if p["bitovi"] not in (7, 8) or p["stop"] not in (1, 2):
        raise Greska("Bitovi podataka 7 ili 8, stop bitovi 1 ili 2.")
    p["upit"] = str(p["upit"] or "")[:40]
    p["izraz"] = str(p["izraz"] or "")[:300]
    if p["protokol"] == "prilagodjeno":
        upit_bajtovi(p["upit"])
        try:
            re.compile(p["izraz"] or r"(?P<kg>\d+[.,]\d+)")
        except re.error as e:
            raise Greska("Neispravan regularni izraz: %s" % e)
    return p


def spremi_postavke(con, p):
    import postavke as P
    p = provjeri_postavke(p)
    P.postavi(con, KLJUC, json.dumps(p, ensure_ascii=False))
    return p


def opis_protokola():
    return [dict(kljuc=k, **v) for k, v in PROTOKOLI.items()]


def _adresa(s):
    m = re.fullmatch(r"\s*([A-Za-z0-9.\-]+):(\d{1,5})\s*", s or "")
    if not m or not 0 < int(m.group(2)) < 65536:
        raise Greska("Adresu vage upišite kao IP:port, npr. 192.168.1.50:4001.")
    return m.group(1), int(m.group(2))


def upit_bajtovi(s):
    r"""Tekst upita u bajtove: \r \n \t \xHH i <ENQ>, <STX>, <CR>, <LF>..."""
    imena = {"NUL": 0, "SOH": 1, "STX": 2, "ETX": 3, "EOT": 4, "ENQ": 5, "ACK": 6, "CR": 13, "LF": 10,
             "DC1": 17, "DC2": 18, "NAK": 21, "ESC": 27}
    s = re.sub(r"<([A-Z0-9]{2,3})>", lambda m: "\\x%02x" % imena[m.group(1)] if m.group(1) in imena else m.group(0), s)
    try:
        return s.encode("latin-1").decode("unicode_escape").encode("latin-1")
    except (UnicodeError, ValueError):
        raise Greska("Neispravan upit vagi (dozvoljeno: tekst, \\r, \\n, \\xHH, <ENQ>).")


# ---------------------------------------------------------------- prikaz sirovih bajtova

_KRATICE = {0: "NUL", 1: "SOH", 2: "STX", 3: "ETX", 4: "EOT", 5: "ENQ", 6: "ACK", 10: "LF", 13: "CR",
            17: "DC1", 18: "DC2", 21: "NAK", 23: "ETB", 27: "ESC"}


def sirovo(b):
    b = bytes(b[-256:])
    tekst = "".join(chr(c) if 32 <= c < 127 else "<%s>" % _KRATICE.get(c, "%02X" % c) for c in b)
    return {"hex": b.hex(" ").upper(), "tekst": tekst, "bajtova": len(b)}


# ---------------------------------------------------------------- veze

class TcpVeza:
    def __init__(self, adresa, rok):
        host, port = _adresa(adresa)
        try:
            self.s = socket.create_connection((host, port), timeout=max(0.2, rok))
        except OSError as e:
            raise Greska("Nema veze s vagom na %s:%d (%s)." % (host, port, e.strerror or e))

    def posalji(self, b):
        self.s.sendall(b)

    def citaj(self, do):
        """Bajtovi koji stignu do trenutka 'do' (vraća čim nešto stigne; b'' ako ništa)."""
        ostalo = do - time.monotonic()
        if ostalo <= 0:
            return b""
        self.s.settimeout(min(ostalo, 0.2))
        try:
            b = self.s.recv(4096)
        except socket.timeout:
            return b""
        if not b:
            raise Greska("Vaga je prekinula vezu.")
        return b

    def zatvori(self):
        try:
            self.s.close()
        except OSError:
            pass


if os.name == "nt":  # pragma: no cover  (Windows; nije isprobano na stvarnoj vagi)
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class DCB(ctypes.Structure):
        # raspored kao u winbase.h (sizeof = 28)
        _fields_ = [
            ("DCBlength", wintypes.DWORD),
            ("BaudRate", wintypes.DWORD),
            ("fBinary", wintypes.DWORD, 1),
            ("fParity", wintypes.DWORD, 1),
            ("fOutxCtsFlow", wintypes.DWORD, 1),
            ("fOutxDsrFlow", wintypes.DWORD, 1),
            ("fDtrControl", wintypes.DWORD, 2),
            ("fDsrSensitivity", wintypes.DWORD, 1),
            ("fTXContinueOnXoff", wintypes.DWORD, 1),
            ("fOutX", wintypes.DWORD, 1),
            ("fInX", wintypes.DWORD, 1),
            ("fErrorChar", wintypes.DWORD, 1),
            ("fNull", wintypes.DWORD, 1),
            ("fRtsControl", wintypes.DWORD, 2),
            ("fAbortOnError", wintypes.DWORD, 1),
            ("fDummy2", wintypes.DWORD, 17),
            ("wReserved", wintypes.WORD),
            ("XonLim", wintypes.WORD),
            ("XoffLim", wintypes.WORD),
            ("ByteSize", wintypes.BYTE),
            ("Parity", wintypes.BYTE),
            ("StopBits", wintypes.BYTE),
            ("XonChar", ctypes.c_char),
            ("XoffChar", ctypes.c_char),
            ("ErrorChar", ctypes.c_char),
            ("EofChar", ctypes.c_char),
            ("EvtChar", ctypes.c_char),
            ("wReserved1", wintypes.WORD),
        ]

    class COMMTIMEOUTS(ctypes.Structure):
        _fields_ = [("ReadIntervalTimeout", wintypes.DWORD),
                    ("ReadTotalTimeoutMultiplier", wintypes.DWORD),
                    ("ReadTotalTimeoutConstant", wintypes.DWORD),
                    ("WriteTotalTimeoutMultiplier", wintypes.DWORD),
                    ("WriteTotalTimeoutConstant", wintypes.DWORD)]

    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.GetCommState.argtypes = [wintypes.HANDLE, ctypes.POINTER(DCB)]
    _k32.GetCommState.restype = wintypes.BOOL
    _k32.SetCommState.argtypes = [wintypes.HANDLE, ctypes.POINTER(DCB)]
    _k32.SetCommState.restype = wintypes.BOOL
    _k32.SetCommTimeouts.argtypes = [wintypes.HANDLE, ctypes.POINTER(COMMTIMEOUTS)]
    _k32.SetCommTimeouts.restype = wintypes.BOOL
    _k32.PurgeComm.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.PurgeComm.restype = wintypes.BOOL
    _k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _k32.ReadFile.restype = wintypes.BOOL
    _k32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _k32.WriteFile.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL

    _GENERIC_RW = 0x80000000 | 0x40000000
    _OPEN_EXISTING = 3
    _INVALID = ctypes.c_void_p(-1).value
    _MAXDWORD = 0xFFFFFFFF

    def _win_greska(sto):
        kod = ctypes.get_last_error()
        poruke = {2: "port ne postoji", 5: "port je zauzet (otvoren u drugom programu?)", 31: "uređaj ne radi"}
        return Greska("Vaga: %s (%s, kod %d)." % (sto, poruke.get(kod, ctypes.FormatError(kod).strip()), kod))

    class ComVeza:
        def __init__(self, p, rok):
            self.h = None
            h = _k32.CreateFileW("\\\\.\\" + p["port"], _GENERIC_RW, 0, None, _OPEN_EXISTING, 0, None)
            if h is None or h == _INVALID:
                raise _win_greska("ne mogu otvoriti %s" % p["port"])
            self.h = h
            try:
                dcb = DCB()
                dcb.DCBlength = ctypes.sizeof(DCB)
                if not _k32.GetCommState(h, ctypes.byref(dcb)):
                    raise _win_greska("GetCommState")
                dcb.BaudRate = p["brzina"]
                dcb.ByteSize = p["bitovi"]
                dcb.Parity = {"N": 0, "O": 1, "E": 2}[p["paritet"]]
                dcb.StopBits = {1: 0, 2: 2}[p["stop"]]
                dcb.fBinary = 1
                dcb.fParity = 0 if p["paritet"] == "N" else 1
                dcb.fOutxCtsFlow = dcb.fOutxDsrFlow = dcb.fDsrSensitivity = 0
                dcb.fOutX = dcb.fInX = dcb.fErrorChar = dcb.fNull = dcb.fAbortOnError = 0
                dcb.fDtrControl = 1   # DTR_CONTROL_ENABLE (neke vage traže DTR)
                dcb.fRtsControl = 1   # RTS_CONTROL_ENABLE
                if not _k32.SetCommState(h, ctypes.byref(dcb)):
                    raise _win_greska("SetCommState (brzina/paritet)")
                # ReadFile vraća odmah ako ima bajtova, inače čeka najviše 100 ms na prvi
                t = COMMTIMEOUTS(_MAXDWORD, _MAXDWORD, 100, 0, 500)
                if not _k32.SetCommTimeouts(h, ctypes.byref(t)):
                    raise _win_greska("SetCommTimeouts")
                _k32.PurgeComm(h, 0x0001 | 0x0002 | 0x0004 | 0x0008)  # TXABORT RXABORT TXCLEAR RXCLEAR
            except Exception:
                self.zatvori()
                raise

        def posalji(self, b):
            n = wintypes.DWORD(0)
            buf = ctypes.create_string_buffer(b, len(b))
            if not _k32.WriteFile(self.h, buf, len(b), ctypes.byref(n), None) or n.value != len(b):
                raise _win_greska("slanje upita")

        def citaj(self, do):
            if do - time.monotonic() <= 0:
                return b""
            buf = ctypes.create_string_buffer(4096)
            n = wintypes.DWORD(0)
            if not _k32.ReadFile(self.h, buf, 4096, ctypes.byref(n), None):
                raise _win_greska("čitanje")
            return buf.raw[:n.value]

        def zatvori(self):
            if self.h is not None:
                _k32.CloseHandle(self.h)
                self.h = None
else:
    ComVeza = None


def _otvori(p, rok):
    if p["veza"] == "tcp":
        return TcpVeza(p["adresa"], rok)
    if ComVeza is None:
        raise Greska("COM port radi samo na Windowsu; ovdje upišite TCP (IP:port).")
    return ComVeza(p, rok)


# ---------------------------------------------------------------- tumačenje odgovora

def _broj(t):
    return float(t.replace(",", ".").replace(" ", ""))


def _u_kg(v, jed):
    jed = (jed or "kg").lower()
    if jed == "g":
        return v / 1000
    if jed in ("lb", "lbs"):
        raise Greska("Vaga šalje funte (lb); prebacite je na kg.")
    return v


_RED = re.compile(r"(?P<pred>[+-])?\s*(?P<broj>\d+(?:[.,]\d+)?)\s*(?P<jed>kg|g|lb)?", re.I)


def tumaci_kontinuirano(red):
    """Jedan redak, npr. 'ST,GS,+  1.250kg' ili 'US,NT,-0.005 kg' ili '   1.250'.
    Vraća (kg, stabilno: True/False/None) ili None ako redak nije težina."""
    gore = red.upper()
    if re.search(r"\bOL\b|OVER|\^\^", gore):
        raise Greska("Vaga je preopterećena (OL).")
    stab = None
    if re.search(r"\b(US|MO)\b", gore):
        stab = False
    elif re.search(r"\bST\b", gore):
        stab = True
    zadnji = None
    for m in _RED.finditer(red):  # zadnji broj u retku je težina (prije su npr. broj vage ili tara)
        if m.group("jed") or "." in m.group("broj") or "," in m.group("broj"):
            zadnji = m
    if not zadnji:
        return None
    v = _u_kg(_broj(zadnji.group("broj")), zadnji.group("jed"))
    if zadnji.group("pred") == "-":
        v = -v
    return v, stab


def _toledo(b):
    m = re.search(rb"\x02([^\x02\r]*)\r", b)
    if not m:
        return None
    t = m.group(1)
    if t.startswith(b"?"):
        st = t[1] if len(t) > 1 else 0
        if st & 0x02:
            raise Greska("Vaga je preopterećena.")
        if st & 0x04:
            raise Greska("Vaga je ispod nule.")
        return None, False  # bit 0: vaga se miče
    m2 = re.fullmatch(rb"\s*(\d+\.\d+)\s*N?\s*", t)
    if not m2:
        raise Greska("Vaga (8217) je vratila neočekivan odgovor.")
    return float(m2.group(1)), True


def _nci(b):
    m = re.search(rb"\n([^\r\n]*)\r\n([^\r\n]*)\r\x03", b)
    if not m:
        return None
    tez, st = m.group(1).decode("latin-1"), m.group(2)
    if "^" in tez:
        raise Greska("Vaga je preopterećena.")
    if "_" in tez or "-" * 3 in tez:
        raise Greska("Vaga je ispod nule ili treba nulirati.")
    if st[:1] == b"S" and len(st) >= 3:  # neke vage ispred statusa šalju 'S'
        st = st[1:]
    if not st:
        raise Greska("Vaga (NCI) nije poslala status.")
    r = tumaci_kontinuirano(tez)
    if not r:
        raise Greska("Vaga (NCI) je vratila neočekivan odgovor.")
    if len(st) > 1 and st[1] & 0x02:
        raise Greska("Vaga je preopterećena.")
    return r[0], not (st[0] & 0x01)


def _cas_ecr(b):
    m = re.search(rb"\x01\x02(.)(.)(.{6})(..)(.)\x03\x04", b, re.S)
    if not m:
        return None
    stanje, pred, tez, jed = m.group(1), m.group(2), m.group(3), m.group(4)
    if b"F" in tez or pred == b"F":
        raise Greska("Vaga je preopterećena.")
    try:
        v = _u_kg(float(tez.decode("latin-1").replace(" ", "") or "0"), jed.decode("latin-1").strip())
    except ValueError:
        raise Greska("Vaga (CAS ECR) je vratila neočekivan odgovor.")
    # BCC (XOR podataka) se ne provjerava: opisi se razlikuju koje bajtove uključuje
    return (-v if pred == b"-" else v), stanje == b"S"


def _prilagodjeno(b, izraz):
    t = b.decode("latin-1")
    m = re.search(izraz or r"(?P<kg>[+-]?\d+[.,]\d+)", t)
    if not m:
        return None
    g = m.groupdict()
    try:
        if g.get("g") is not None:  # grupa (?P<g>...) = težina u gramima
            v = _broj(g["g"]) / 1000
        else:
            v = _broj(g["kg"] if g.get("kg") is not None else m.group(1) if m.groups() else m.group(0))
    except (ValueError, TypeError, AttributeError):
        raise Greska("Izraz je pronašao nešto što nije broj; provjerite grupu (?P<kg>...).")
    stab = g.get("stabilno")
    return v, (True if stab is None else stab.strip().upper() in ("S", "ST", "1", "STABLE"))


# ---------------------------------------------------------------- očitanje

def ocitaj(p, rok=ROK):
    """Očita vagu. Vraća {kg, stabilno, sirovo}; Greska ako vaga ne odgovori ili je težina nula/minus.
    Nestabilna težina vraća stabilno=False (kasa je ne upisuje)."""
    p = provjeri_postavke(p)
    sve = bytearray()
    try:
        r = _ocitaj(p, rok, sve)
    except Greska as e:
        e.sirovo = sirovo(sve)
        raise
    kg, stab = r
    if kg is not None:
        kg = round(kg + (1e-9 if kg >= 0 else -1e-9), 3)
    if stab and kg is not None and kg <= 0:
        g = Greska("Vaga pokazuje %s kg (nula ili minus). Stavite robu na vagu." % str(kg).replace(".", ","))
        g.sirovo = sirovo(sve)
        raise g
    return {"kg": kg, "stabilno": bool(stab), "sirovo": sirovo(sve)}


def _ocitaj(p, rok, sve):
    if not _brava.acquire(timeout=rok):
        raise Greska("Vaga je zauzeta (drugo očitanje u tijeku).")
    try:
        do = time.monotonic() + rok
        v = _otvori(p, rok)
        try:
            return _razgovor(v, p, do, sve)
        finally:
            v.zatvori()
    finally:
        _brava.release()


def _citaj(v, p, do, sve):
    b = v.citaj(do)
    if p["bitovi"] == 7:
        b = bytes(c & 0x7F for c in b)
    sve.extend(b)
    if len(sve) > 65536:
        del sve[:-4096]
    return b


def _razgovor(v, p, do, sve):
    prot = p["protokol"]
    if prot == "kontinuirano" or (prot == "prilagodjeno" and not p["upit"]):
        return _kontinuirano(v, p, do, sve)
    if prot == "cas_ecr":
        v.posalji(ENQ)
        while ACK not in sve:
            if NAK in sve or time.monotonic() >= do:
                break
            _citaj(v, p, do, sve)
        if ACK not in sve:
            raise Greska("Vaga nije potvrdila upit (nema ACK na ENQ)." if not sve else
                         "Vaga je odbila upit (nema ACK). Je li na vagi odabran ECR protokol?")
        v.posalji(DC1)
        tumac = _cas_ecr
    elif prot == "toledo8217":
        v.posalji(b"W")
        tumac = _toledo
    elif prot == "nci":
        v.posalji(b"W\r")
        tumac = _nci
    else:
        v.posalji(upit_bajtovi(p["upit"]))
        tumac = lambda b: _prilagodjeno(b, p["izraz"])
    while time.monotonic() < do:
        _citaj(v, p, do, sve)
        r = tumac(bytes(sve))
        if r:
            return r
    raise Greska("Vaga nije odgovorila u %d s." % ROK if not sve else
                 "Vaga je odgovorila nešto što ne razumijem (provjerite protokol i brzinu).")


def _kontinuirano(v, p, do, sve):
    """Čita retke dok ne dobije mirnu težinu; bez oznake ST/US traži dva ista retka zaredom."""
    ostatak, prethodni, zadnji = b"", None, None
    while time.monotonic() < do:
        b = _citaj(v, p, do, sve)
        if not b:
            continue
        dijelovi = re.split(rb"[\r\n\x03]+", ostatak + b)
        ostatak = dijelovi.pop()
        for d in dijelovi:
            red = d.replace(b"\x02", b"").decode("latin-1").strip()
            if not red:
                continue
            if p["protokol"] == "prilagodjeno":
                r = _prilagodjeno(d, p["izraz"])
            else:
                r = tumaci_kontinuirano(red)
            if not r:
                continue
            kg, stab = r
            if stab is None:  # vaga ne šalje oznaku mirnoće
                stab = prethodni is not None and abs(prethodni - kg) < 1e-9
                prethodni = kg
            zadnji = (kg, stab)
            if stab:
                return zadnji
    if zadnji:
        return zadnji
    raise Greska("Vaga nije poslala težinu u %d s." % ROK if not sve else
                 "Od vage stižu podaci koje ne razumijem (provjerite protokol, brzinu i paritet).")
