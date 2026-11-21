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

Druga mapa (po želji, 0.4.4): ista kopija ide i na drugo mjesto (npr. USB + OneDrive), svako
mjesto se provjerava i bilježi zasebno. Kopija je "danas gotova" tek kad uspije na svim mjestima.

Modul "kopija_dojava" (0.4.4): kad kopije nema ili nije uspjela, crvena traka na kasi, crveno u
Kontroli i jednom dnevno e-mail vlasniku (SMTP postavke večernjeg sažetka).
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
MJESTA = {"1": "Prva mapa", "2": "Druga mapa"}

_lock = threading.Lock()
_sat_radi = False


def pripremi(con):
    con.executescript(SHEMA)
    if "mjesto" not in [r[1] for r in con.execute("PRAGMA table_info(kopija_van_log)")]:
        con.execute("ALTER TABLE kopija_van_log ADD COLUMN mjesto TEXT NOT NULL DEFAULT '1'")
        con.commit()


def sada():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- postavke

def _env_mapa():
    return os.environ.get("TRGOVINA_KOPIJA_MAPA", "").strip()


def _prosiri(m):
    return os.path.expanduser(os.path.expandvars(m.strip())) if m and m.strip() else ""


def mapa(con):
    """Ciljna mapa (proširene %VARIJABLE%), ili '' ako nije zadana."""
    return _prosiri(postavke.vrijednost(con, "kopija_van.mapa", "") or _env_mapa())


def mapa2(con):
    """Druga ciljna mapa ('' ako nije zadana)."""
    return _prosiri(postavke.vrijednost(con, "kopija_van.mapa2", ""))


def mape(con):
    """[(mjesto, putanja)] za sve upisane mape; prva je uvijek tu (i kad je prazna)."""
    out = [("1", mapa(con))]
    m2 = mapa2(con)
    if m2:
        out.append(("2", m2))
    return out


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
    m2 = " ".join(str(p.get("mapa2") or "").split())[:400] if p.get("mapa2") is not None else None
    if m2 is not None:
        if m2 and not os.path.isabs(_prosiri(m2)):
            raise Greska("Druga mapa: upišite cijelu putanju, npr. %USERPROFILE%\\OneDrive\\Trgovina.")
        if m2 and os.path.normcase(os.path.normpath(_prosiri(m2))) == os.path.normcase(os.path.normpath(
                _prosiri(m if m is not None else (postavke.vrijednost(con, "kopija_van.mapa", "") or _env_mapa())) or ".")):
            raise Greska("Druga mapa mora biti drugačija od prve (npr. prva na USB-u, druga u OneDriveu).")
        postavke.postavi(con, "kopija_van.mapa2", m2)
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


def _zapisi(con, nacin, datoteka, velicina, ok, poruka, mjesto="1"):
    with con:
        con.execute("INSERT INTO kopija_van_log (vrijeme, nacin, datoteka, velicina, ok, poruka, mjesto) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (sada(), nacin, datoteka, velicina, 1 if ok else 0, poruka[:500], mjesto))


def _u_mapu(tmp, cilj, ocekivano, n):
    """Kopira privremenu kopiju u mapu i provjeri je. Vraća (ok, datoteka, velicina, poruka)."""
    if not cilj:
        raise Greska("Mapa za kopiju izvan računala nije upisana (Postavke, Kopija izvan računala).")
    if not os.path.isdir(cilj):
        raise Greska("Mapa %s ne postoji ili nije dostupna (USB nije utaknut? OneDrive nije pokrenut?)." % cilj)
    ime = "kopija_trgovina_%s.db" % dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    datoteka = os.path.join(cilj, ime)
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
    ok, poruka = provjeri_datoteku(datoteka, ocekivano)
    if ok:
        obrisano = _pocisti(cilj, n)
        if obrisano:
            poruka += "; obrisano starih kopija: %d" % obrisano
    return ok, datoteka, velicina, poruka


def napravi(nacin="gumb", db_path=DB_PATH):
    """Napravi i provjeri kopiju izvan računala u svaku upisanu mapu.
    Vraća dict s rezultatom (i kad ne uspije): ok = uspjelo na svim mjestima."""
    if not _lock.acquire(timeout=600):
        return {"ok": False, "poruka": "Kopija se već radi.", "rezultati": []}
    con = spoji(db_path)
    tmp = None
    rezultati = []
    try:
        ciljevi = mape(con)
        n = zadrzi(con)
        try:
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
        except (OSError, sqlite3.Error) as e:
            for mjesto, _ in ciljevi:
                _zapisi(con, nacin, "", 0, False, "Kopija baze nije uspjela: %s" % e, mjesto)
            return {"ok": False, "datoteka": "", "velicina": 0, "poruka": "Kopija baze nije uspjela: %s" % e,
                    "rezultati": []}
        # 2. i 3. kopiranje u svaku mapu i provjera tamo
        for mjesto, cilj in ciljevi:
            try:
                ok, datoteka, velicina, poruka = _u_mapu(tmp, cilj, ocekivano, n)
            except (Greska, OSError, sqlite3.Error) as e:
                ok, datoteka, velicina, poruka = False, "", 0, str(e)
            _zapisi(con, nacin, datoteka, velicina, ok, poruka, mjesto)
            rezultati.append({"mjesto": mjesto, "naziv": MJESTA[mjesto], "ok": ok, "datoteka": datoteka,
                              "velicina": velicina, "poruka": poruka})
        prvi = rezultati[0]
        sve_ok = all(r["ok"] for r in rezultati)
        poruka = prvi["poruka"] if len(rezultati) == 1 else " | ".join(
            "%s: %s" % (r["naziv"], r["poruka"]) for r in rezultati)
        return {"ok": sve_ok, "datoteka": prvi["datoteka"], "velicina": prvi["velicina"], "poruka": poruka,
                "rezultati": rezultati}
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        con.close()
        _lock.release()


def _provjeri_mapu(cilj):
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


def provjeri(con):
    """Gumb "Provjeri": je li svaka mapa dostupna i upisiva, i je li zadnja kopija u njoj ispravna."""
    ciljevi = mape(con)
    if len(ciljevi) == 1:
        return _provjeri_mapu(ciljevi[0][1])
    out = []
    for mjesto, cilj in ciljevi:
        try:
            r = _provjeri_mapu(cilj)
        except Greska as e:
            r = {"ok": False, "poruka": str(e)}
        out.append(dict(r, mjesto=mjesto, naziv=MJESTA[mjesto]))
    return {"ok": all(r["ok"] for r in out), "rezultati": out,
            "poruka": " | ".join("%s: %s" % (r["naziv"], r["poruka"]) for r in out)}


# ---------------------------------------------------------------- stanje i upozorenje

def _zadnja(con, samo_ok, mjesto=None):
    uvjeti, arg = [], []
    if samo_ok:
        uvjeti.append("ok = 1")
    if mjesto:
        uvjeti.append("mjesto = ?")
        arg.append(mjesto)
    r = con.execute("SELECT * FROM kopija_van_log %s ORDER BY id DESC LIMIT 1"
                    % (("WHERE " + " AND ".join(uvjeti)) if uvjeti else ""), arg).fetchone()
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
    cilj2 = mapa2(con)
    return {"mapa": postavke.vrijednost(con, "kopija_van.mapa", ""), "mapa_env": _env_mapa(), "cilj": cilj,
            "dostupna": bool(cilj) and os.path.isdir(cilj), "zadrzi": zadrzi(con),
            "mapa2": postavke.vrijednost(con, "kopija_van.mapa2", ""), "cilj2": cilj2,
            "dostupna2": bool(cilj2) and os.path.isdir(cilj2),
            "zadnja_ok": _zadnja(con, True, "1"), "zadnji_pokusaj": _zadnja(con, False, "1"),
            "zadnja_ok2": _zadnja(con, True, "2") if cilj2 else None,
            "dojava": postavke.ukljucen(con, "kopija_dojava"),
            "log": [dict(r) for r in con.execute("SELECT * FROM kopija_van_log ORDER BY id DESC LIMIT 15")],
            "kopije": kopije, "upozorenje": upozorenje(con)}


def _upozorenje_mjesta(con, mjesto, cilj, sad):
    ok = _zadnja(con, True, mjesto)
    zadnji = _zadnja(con, False, mjesto)
    granica = (sad - dt.timedelta(days=DANA_CRVENO)).strftime("%Y-%m-%d %H:%M:%S")
    kad = ("Zadnja ispravna kopija: %s." % ok["vrijeme"][:16]) if ok else "Ispravne kopije izvan računala još nema."
    pred = "Kopija izvan računala" if mjesto == "1" else "Druga kopija izvan računala"
    if not cilj:
        return {"razina": "crveno", "naslov": "%s: mapa nije upisana" % pred,
                "opis": "Upišite mapu na USB disku ili u OneDrive / Google Drive (Postavke, Kopija izvan računala)."}
    if not os.path.isdir(cilj):
        return {"razina": "crveno", "naslov": "%s: mapa nije dostupna" % pred,
                "opis": "%s ne postoji. Utaknite USB disk (ili pokrenite OneDrive / Google Drive), "
                        "pa u Postavkama kliknite Napravi kopiju sada. %s" % (cilj, kad)}
    if not ok or ok["vrijeme"] < granica:
        return {"razina": "crveno", "naslov": "Nema %s zadnja %d dana" % (
                    "kopije izvan računala" if mjesto == "1" else "druge kopije izvan računala", DANA_CRVENO),
                "opis": "%s %sKliknite Postavke, Napravi kopiju sada." % (
                    kad, ("Zadnji pokušaj nije uspio: %s. " % zadnji["poruka"].rstrip(".")) if zadnji and not zadnji["ok"] else "")}
    if zadnji and not zadnji["ok"]:
        return {"razina": "zuto", "naslov": "Zadnja %s nije uspjela" % (
                    "kopija izvan računala" if mjesto == "1" else "druga kopija izvan računala"),
                "opis": "%s (%s). %s Provjerite USB / OneDrive i kliknite Napravi kopiju sada."
                        % (zadnji["poruka"].rstrip("."), zadnji["vrijeme"][:16], kad)}
    return None


def upozorenje(con, sad=None):
    """Za karticu Danas: None, ili {razina: crveno/zuto, naslov, opis} (najgore od svih mapa)."""
    sad = sad or dt.datetime.now()
    sva = [u for u in (_upozorenje_mjesta(con, m, c, sad) for m, c in mape(con)) if u]
    if not sva:
        return None
    sva.sort(key=lambda u: u["razina"] != "crveno")
    u = dict(sva[0])
    if len(sva) > 1:
        u["opis"] += " Također: %s." % "; ".join(x["naslov"] for x in sva[1:])
    return u


# ---------------------------------------------------------------- dojava (modul kopija_dojava)

def _dojava(con):
    return postavke.ukljucen(con, "kopija_van") and postavke.ukljucen(con, "kopija_dojava")


def traka(con):
    """Crvena traka na kasi: None ili {razina, tekst}. Samo crveno (žuto se vidi na Danas)."""
    if not _dojava(con):
        return None
    u = upozorenje(con)
    if not u or u["razina"] != "crveno":
        return None
    return {"razina": "crveno", "tekst": "%s. %s" % (u["naslov"], u["opis"])}


def kontrola(con):
    """Provjera za karticu Kontrola (i večernji sažetak), format kao kontrola._p."""
    if not _dojava(con):
        return []
    u = upozorenje(con)
    if not u:
        z = _zadnja(con, True, "1")
        return [{"razina": "ok", "naslov": "Kopija izvan računala",
                 "opis": "Zadnja ispravna kopija: %s." % (z["vrijeme"][:16] if z else "-"),
                 "ukupno": 0, "stavke": [], "stupci": []}]
    return [{"razina": u["razina"], "naslov": u["naslov"], "opis": u["opis"], "ukupno": 0, "stavke": [], "stupci": []}]


def dojavi_mailom(con, sad=None):
    """Jednom dnevno e-mail vlasniku kad je kopija izvan računala crvena.
    Koristi SMTP postavke večernjeg sažetka; bez njih ne radi ništa. Vraća True ako je poslano."""
    sad = sad or dt.datetime.now()
    try:
        if not _dojava(con):
            return False
        import sazetak
        u = upozorenje(con, sad)
        if not u or u["razina"] != "crveno" or not sazetak.smtp_upisan(con):
            return False
        za = postavke.vrijednost(con, "sazetak.za", "")
        if not za or postavke.vrijednost(con, "kopija_van.mail_poslan", "") == sad.date().isoformat():
            return False
        from email.message import EmailMessage
        from email.utils import formatdate, make_msgid
        msg = EmailMessage()
        msg["Subject"] = "Kasa: %s" % u["naslov"]
        msg["To"] = za
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        msg.set_content(u["opis"] + "\n\nBez kopije izvan računala kvar diska, krađa ili požar znače gubitak svih "
                        "podataka programa. Ova poruka stiže najviše jednom dnevno dok se kopija ne napravi.\n")
        # i kad slanje ne uspije, danas se više ne pokušava (ne svakih 5 minuta)
        postavke.postavi(con, "kopija_van.mail_poslan", sad.date().isoformat())
        sazetak.posalji_poruku(con, msg)
        postavke.postavi(con, "kopija_van.mail_greska", "")
        return True
    except Exception as e:
        try:
            postavke.postavi(con, "kopija_van.mail_greska", "%s %s" % (sad.strftime("%Y-%m-%d %H:%M"), e))
        except Exception:
            pass
        return False


# ---------------------------------------------------------------- raspored

def _ukljucen():
    """(modul uključen, danas već uspjelo na svim mapama)."""
    con = spoji()
    try:
        danas = dt.date.today().isoformat()
        return postavke.ukljucen(con, "kopija_van"), all(con.execute(
            "SELECT 1 FROM kopija_van_log WHERE ok = 1 AND vrijeme >= ? AND mjesto = ?",
            (danas, m)).fetchone() is not None for m, _ in mape(con))
    finally:
        con.close()


def _provjeri_dojavu():
    con = spoji()
    try:
        dojavi_mailom(con)
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
        try:
            _provjeri_dojavu()
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
