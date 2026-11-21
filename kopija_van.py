# -*- coding: utf-8 -*-
"""
Kopija baze IZVAN računala (modul "kopija_van").

Obična sigurnosna kopija (kopija.py) ostaje u mapi pokraj programa: kad se pokvari disk,
ukrade ili izgori računalo, nestane i ona. Ovaj modul jednom dnevno sprema kopiju u mapu
koju odabere vlasnik: USB disk (npr. E:\\Trgovina) ili mapu koju sinkronizira OneDrive /
Google Drive (npr. %USERPROFILE%\\OneDrive\\Trgovina).

Postupak:
1. SQLite "backup" (ispravna slika baze i dok kasa radi) u privremenu datoteku na računalu,
   u istoj transakciji čitanja broje se retci ključnih tablica;
2. kopiranje u ciljnu mapu (.part pa preimenovanje, da ne ostane pola datoteke);
3. PROVJERA kopije u ciljnoj mapi: otvori se samo za čitanje, PRAGMA integrity_check
   (quick_check za velike baze) i usporedba broja redaka s bazom;
4. rezultat (vrijeme, datoteka, veličina, u redu / greška) ide u tablicu kopija_van_log;
5. čuva se zadnjih N kopija (postavka, zadano 30); brišu se samo NAŠE datoteke
   (kopija_trgovina_GGGG-MM-DD_HHMMSS.db), ništa drugo u mapi.

Kada: kod pokretanja programa ako danas još nema uspješne kopije, kod zaključka smjene,
svakih sat vremena ako danas još nema uspješne (npr. USB je utaknut kasnije) i na gumb.
Ako postavka mape nije upisana, koristi se TRGOVINA_KOPIJA_MAPA iz pokreni.bat (kao i dosad).
"""

import datetime as dt
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from urllib.request import pathname2url

import postavke
from baza import DB_PATH, spoji
from nabava import Greska

ZADRZI = 30
TABLICE = ("racuni", "artikli", "dokumenti", "dok_stavke")
VELIKA_BAZA = 300 * 1024 * 1024   # iznad ovoga quick_check umjesto integrity_check
UZORAK = re.compile(r"^kopija_trgovina_\d{4}-\d{2}-\d{2}_\d{6}\.db$")
DANA_CRVENO = 2

SHEMA = """
CREATE TABLE IF NOT EXISTS kopija_van_log (
    id         INTEGER PRIMARY KEY,
    vrijeme    TEXT NOT NULL,
    nacin      TEXT NOT NULL DEFAULT '',    -- pokretanje, smjena, sat, gumb
    datoteka   TEXT NOT NULL DEFAULT '',
    velicina   INTEGER NOT NULL DEFAULT 0,
    ok         INTEGER NOT NULL DEFAULT 0,
    poruka     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_kopija_van_log ON kopija_van_log(ok, vrijeme);
"""

_lock = threading.Lock()
_sat_radi = False


def pripremi(con):
    con.executescript(SHEMA)


def sada():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- postavke

def _env_mapa():
    m = os.environ.get("TRGOVINA_KOPIJA_MAPA", "").strip()
    return "" if m == "-" else m  # "-" = bez dodatne mape (Kasa_druga_trgovina.cmd)


def _prosiri(m):
    return os.path.expanduser(os.path.expandvars(m.strip())) if m and m.strip() else ""


def mapa(con):
    """Ciljna mapa (proširene %VARIJABLE%), ili '' ako nije zadana."""
    return _prosiri(postavke.vrijednost(con, "kopija_van.mapa", "") or _env_mapa())


def zadrzi(con):
    try:
        return max(3, min(int(postavke.vrijednost(con, "kopija_van.zadrzi", str(ZADRZI))), 1000))
    except ValueError:
        return ZADRZI


def spremi_postavke(con, p):
    m = " ".join(str(p.get("mapa") or "").split())[:400] if p.get("mapa") is not None else None
    if m is not None:
        if m and not os.path.isabs(_prosiri(m)):
            raise Greska("Upišite cijelu putanju mape, npr. E:\\Trgovina ili %USERPROFILE%\\OneDrive\\Trgovina.")
        postavke.postavi(con, "kopija_van.mapa", m)
    if p.get("zadrzi") not in (None, ""):
        try:
            n = int(str(p["zadrzi"]).strip())
        except ValueError:
            raise Greska("Broj kopija mora biti cijeli broj.")
        if not 3 <= n <= 1000:
            raise Greska("Čuvajte između 3 i 1000 kopija.")
        postavke.postavi(con, "kopija_van.zadrzi", n)
    return stanje(con)


# ---------------------------------------------------------------- kopija i provjera

def _ro(put):
    """Veza samo za čitanje (kopija se ne smije promijeniti provjerom)."""
    return sqlite3.connect("file:%s?mode=ro" % pathname2url(os.path.abspath(put)), uri=True)


def _broji(con):
    out = {}
    for t in TABLICE:
        try:
            out[t] = con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        except sqlite3.OperationalError:
            pass  # tablica ne postoji (vrlo stara baza)
    return out


def provjeri_datoteku(put, ocekivano=None):
    """Otvori kopiju samo za čitanje, provjeri cjelovitost i broj redaka.
    Vraća (ok, poruka)."""
    try:
        velicina = os.path.getsize(put)
        c = _ro(put)
        try:
            pragma = "quick_check" if velicina > VELIKA_BAZA else "integrity_check"
            rez = [r[0] for r in c.execute("PRAGMA %s" % pragma).fetchmany(5)]
            if rez != ["ok"]:
                return False, "%s: %s" % (pragma, "; ".join(map(str, rez)))
            broj = _broji(c)
        finally:
            c.close()
    except (OSError, sqlite3.Error) as e:
        return False, "Kopija se ne može otvoriti: %s" % e
    if not broj:
        return False, "Kopija nema tablica programa (nije baza trgovine?)."
    if ocekivano:
        razlike = ["%s %s/%s" % (t, broj.get(t), n) for t, n in ocekivano.items() if broj.get(t) != n]
        if razlike:
            return False, "Broj redaka se ne slaže (kopija/baza): " + ", ".join(razlike)
    return True, "%s u redu; retci: %s" % (pragma, ", ".join("%s %d" % kv for kv in broj.items()))


def _pocisti(cilj, n):
    kopije = sorted(f for f in os.listdir(cilj) if UZORAK.match(f))
    obrisano = 0
    for f in kopije[:-n]:
        try:
            os.remove(os.path.join(cilj, f))
            obrisano += 1
        except OSError:
            pass
        for dodatak in ("-wal", "-shm", "-journal"):
            try:
                os.remove(os.path.join(cilj, f + dodatak))
            except OSError:
                pass
    return obrisano


def _zapisi(con, nacin, datoteka, velicina, ok, poruka):
    with con:
        con.execute("INSERT INTO kopija_van_log (vrijeme, nacin, datoteka, velicina, ok, poruka) "
                    "VALUES (?, ?, ?, ?, ?, ?)", (sada(), nacin, datoteka, velicina, 1 if ok else 0, poruka[:500]))


def napravi(nacin="gumb", db_path=DB_PATH):
    """Napravi i provjeri kopiju izvan računala. Vraća dict s rezultatom (i kad ne uspije)."""
    if not _lock.acquire(timeout=600):
        return {"ok": False, "poruka": "Kopija se već radi."}
    con = spoji(db_path)
    tmp = None
    datoteka, velicina = "", 0
    try:
        cilj = mapa(con)
        if not cilj:
            raise Greska("Mapa za kopiju izvan računala nije upisana (Postavke, Kopija izvan računala).")
        if not os.path.isdir(cilj):
            raise Greska("Mapa %s ne postoji ili nije dostupna (USB nije utaknut? OneDrive nije pokrenut?)." % cilj)
        ime = "kopija_trgovina_%s.db" % dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        datoteka = os.path.join(cilj, ime)
        # 1. backup u privremenu datoteku; brojanje u istoj transakciji čitanja (ista slika baze)
        fd, tmp = tempfile.mkstemp(prefix="trgovina_kopija_", suffix=".db")
        os.close(fd)
        izvor = sqlite3.connect(db_path, timeout=30)
        try:
            izvor.isolation_level = None
            izvor.execute("BEGIN")
            ocekivano = _broji(izvor)
            dst = sqlite3.connect(tmp)
            try:
                izvor.backup(dst)
                # kopija je jedna samostalna datoteka (bez -wal/-shm uz nju na USB-u ili OneDriveu)
                dst.execute("PRAGMA journal_mode = DELETE")
            finally:
                dst.close()
            izvor.execute("COMMIT")
        finally:
            izvor.close()
        # 2. kopiranje u ciljnu mapu
        part = datoteka + ".part"
        try:
            shutil.copyfile(tmp, part)
            with open(part, "rb+") as f:
                os.fsync(f.fileno())
            os.replace(part, datoteka)
        except OSError as e:
            try:
                os.remove(part)
            except OSError:
                pass
            raise Greska("Ne mogu pisati u %s: %s" % (cilj, e))
        velicina = os.path.getsize(datoteka)
        # 3. provjera kopije u ciljnoj mapi
        ok, poruka = provjeri_datoteku(datoteka, ocekivano)
        if ok:
            obrisano = _pocisti(cilj, zadrzi(con))
            if obrisano:
                poruka += "; obrisano starih kopija: %d" % obrisano
        _zapisi(con, nacin, datoteka, velicina, ok, poruka)
        return {"ok": ok, "datoteka": datoteka, "velicina": velicina, "poruka": poruka}
    except (Greska, OSError, sqlite3.Error) as e:
        _zapisi(con, nacin, datoteka, velicina, False, str(e))
        return {"ok": False, "datoteka": datoteka, "velicina": velicina, "poruka": str(e)}
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        con.close()
        _lock.release()


def provjeri(con):
    """Gumb "Provjeri": je li mapa dostupna i upisiva, i je li zadnja kopija u njoj ispravna."""
    cilj = mapa(con)
    if not cilj:
        raise Greska("Mapa nije upisana.")
    if not os.path.isdir(cilj):
        raise Greska("Mapa %s ne postoji ili nije dostupna (USB nije utaknut? OneDrive nije pokrenut?)." % cilj)
    proba = os.path.join(cilj, ".trgovina_proba_pisanja")
    try:
        with open(proba, "w") as f:
            f.write("ok")
        os.remove(proba)
    except OSError as e:
        raise Greska("U mapu %s se ne može pisati: %s" % (cilj, e))
    kopije = sorted(f for f in os.listdir(cilj) if UZORAK.match(f))
    if not kopije:
        return {"ok": True, "poruka": "Mapa je dostupna i u nju se može pisati. Kopija još nema: kliknite Napravi kopiju sada."}
    zadnja = os.path.join(cilj, kopije[-1])
    ok, poruka = provjeri_datoteku(zadnja)
    return {"ok": ok, "datoteka": zadnja, "poruka": ("Mapa je dostupna. Zadnja kopija %s: %s" % (kopije[-1], poruka)),
            "kopija": len(kopije)}


# ---------------------------------------------------------------- stanje i upozorenje

def _zadnja(con, samo_ok):
    r = con.execute("SELECT * FROM kopija_van_log %s ORDER BY id DESC LIMIT 1"
                    % ("WHERE ok = 1" if samo_ok else "")).fetchone()
    return dict(r) if r else None


def stanje(con):
    cilj = mapa(con)
    kopije = []
    if cilj and os.path.isdir(cilj):
        try:
            for f in sorted((f for f in os.listdir(cilj) if UZORAK.match(f)), reverse=True)[:50]:
                kopije.append({"ime": f, "velicina": os.path.getsize(os.path.join(cilj, f))})
        except OSError:
            pass
    return {"mapa": postavke.vrijednost(con, "kopija_van.mapa", ""), "mapa_env": _env_mapa(), "cilj": cilj,
            "dostupna": bool(cilj) and os.path.isdir(cilj), "zadrzi": zadrzi(con),
            "zadnja_ok": _zadnja(con, True), "zadnji_pokusaj": _zadnja(con, False),
            "log": [dict(r) for r in con.execute("SELECT * FROM kopija_van_log ORDER BY id DESC LIMIT 15")],
            "kopije": kopije, "upozorenje": upozorenje(con)}


def upozorenje(con, sad=None):
    """Za karticu Danas: None, ili {razina: crveno/zuto, naslov, opis}."""
    sad = sad or dt.datetime.now()
    cilj = mapa(con)
    ok = _zadnja(con, True)
    zadnji = _zadnja(con, False)
    granica = (sad - dt.timedelta(days=DANA_CRVENO)).strftime("%Y-%m-%d %H:%M:%S")
    kad = ("Zadnja ispravna kopija: %s." % ok["vrijeme"][:16]) if ok else "Ispravne kopije izvan računala još nema."
    if not cilj:
        return {"razina": "crveno", "naslov": "Kopija izvan računala: mapa nije upisana",
                "opis": "Upišite mapu na USB disku ili u OneDrive / Google Drive (Postavke, Kopija izvan računala)."}
    if not os.path.isdir(cilj):
        return {"razina": "crveno", "naslov": "Kopija izvan računala: mapa nije dostupna",
                "opis": "%s ne postoji. Utaknite USB disk (ili pokrenite OneDrive / Google Drive), "
                        "pa u Postavkama kliknite Napravi kopiju sada. %s" % (cilj, kad)}
    if not ok or ok["vrijeme"] < granica:
        return {"razina": "crveno", "naslov": "Nema kopije izvan računala zadnja %d dana" % DANA_CRVENO,
                "opis": "%s %sKliknite Postavke, Napravi kopiju sada." % (
                    kad, ("Zadnji pokušaj nije uspio: %s. " % zadnji["poruka"].rstrip(".")) if zadnji and not zadnji["ok"] else "")}
    if zadnji and not zadnji["ok"]:
        return {"razina": "zuto", "naslov": "Zadnja kopija izvan računala nije uspjela",
                "opis": "%s (%s). %s Provjerite USB / OneDrive i kliknite Napravi kopiju sada."
                        % (zadnji["poruka"].rstrip("."), zadnji["vrijeme"][:16], kad)}
    return None


# ---------------------------------------------------------------- raspored

def _ukljucen():
    con = spoji()
    try:
        return postavke.ukljucen(con, "kopija_van"), con.execute(
            "SELECT 1 FROM kopija_van_log WHERE ok = 1 AND vrijeme >= ?",
            (dt.date.today().isoformat(),)).fetchone() is not None
    finally:
        con.close()


def u_pozadini(nacin):
    t = threading.Thread(target=lambda: napravi(nacin), daemon=True)
    t.start()
    return t


def nakon_zakljucka():
    """Zove se nakon zaključka smjene."""
    try:
        if _ukljucen()[0]:
            return u_pozadini("smjena")
    except Exception:
        pass
    return None


def _sat():
    zadnji_pokusaj = 0.0
    nacin = "pokretanje"
    while True:
        try:
            ukljucen, danas_ima = _ukljucen()
            if ukljucen and not danas_ima and time.time() - zadnji_pokusaj > 3600:
                zadnji_pokusaj = time.time()
                napravi(nacin)
                nacin = "sat"
        except Exception:
            pass
        time.sleep(300)


def pri_pokretanju():
    """Pokreće pozadinsku nit: odmah kopija ako danas nema uspješne, pa provjera svakih 5 min
    (pokušaj najviše jednom na sat dok danas ne uspije)."""
    global _sat_radi
    if _sat_radi:
        return
    _sat_radi = True
    threading.Thread(target=_sat, daemon=True).start()
