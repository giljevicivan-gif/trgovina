# -*- coding: utf-8 -*-
"""
Usporedba s cijenama trgovačkih lanaca (modul "lanci").

Lanci (Konzum, Lidl, Plodine, Spar, Tommy, Studenac, Kaufland, Eurospin...) moraju
svaki dan javno objaviti cjenike svih prodavaonica (Odluka o objavi cjenika, NN 75/2025
i NN 101/2026). Projekt cijene.dev (Senko Rašić) ih skuplja i svaki dan objavi
jedan ZIP sa svim cjenicima, bez prijave: https://api.cijene.dev/v0/list

"Preuzmi cijene" skine zadnji ZIP (oko 80 MB), iz njega uzme samo prodavaonice
u vašem mjestu (ako lanac tamo nema prodavaonicu, uzme sve njegove u RH) i spremi
najnižu i prosječnu cijenu svakog proizvoda u lanci.db pokraj trgovina.db.
lanci.db se ne kopira u sigurnosne kopije (može se uvijek ponovno preuzeti).

Povezivanje s našim artiklima:
- automatski po barkodu (pakirana roba);
- za rinfuzno voće i povrće (nema barkod) ručno: kod artikla "Poveži" i odaberete
  proizvod lanca (npr. "Banane, kg"). Veza se pamti u trgovina.db.
Za artikle na kg/l uspoređuje se cijena za kg/l (cijena za jedinicu mjere iz cjenika).
"""

import csv
import datetime as dt
import io
import json
import os
import sqlite3
import tempfile
import threading
import unicodedata
import urllib.request
import zipfile

import postavke
from baza import DB_PATH
from nabava import Greska

IZVOR = "https://api.cijene.dev"
LANCI_DB = os.environ.get("TRGOVINA_LANCI_DB") or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "lanci.db")
SKUPLJI = 10   # % kad smo "skuplji" (crveno)

SHEMA_VEZE = """
CREATE TABLE IF NOT EXISTS lanci_veze (
    artikl_id  INTEGER NOT NULL REFERENCES artikli(id),
    lanac      TEXT NOT NULL,
    sifra      TEXT NOT NULL,     -- product_id u cjeniku lanca
    naziv      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (artikl_id, lanac)
);
"""

SHEMA_CIJENE = """
CREATE TABLE IF NOT EXISTS cijene (
    lanac       TEXT NOT NULL,
    sifra       TEXT NOT NULL,
    barkod      TEXT NOT NULL DEFAULT '',
    naziv       TEXT NOT NULL,
    trazi       TEXT NOT NULL,        -- naziv malim slovima bez kvačica, za traženje
    marka       TEXT NOT NULL DEFAULT '',
    kategorija  TEXT NOT NULL DEFAULT '',
    jm          TEXT NOT NULL DEFAULT '',
    kolicina    TEXT NOT NULL DEFAULT '',
    cijena_min  INTEGER,              -- centi, najniža MPC u odabranim prodavaonicama
    cijena_pros INTEGER,
    akcija_min  INTEGER,              -- najniža posebna (akcijska) cijena, ako je ima
    jed_min     INTEGER,              -- najniža cijena za jedinicu mjere (kg, l)
    najniza_30  INTEGER,
    trgovina    INTEGER NOT NULL DEFAULT 0,   -- u koliko prodavaonica
    cijela_hr   INTEGER NOT NULL DEFAULT 0,   -- 1 = nema prodavaonice u mjestu, uzete sve
    PRIMARY KEY (lanac, sifra)
);
CREATE INDEX IF NOT EXISTS ix_cijene_barkod ON cijene(barkod);
CREATE TABLE IF NOT EXISTS info (kljuc TEXT PRIMARY KEY, vrijednost TEXT NOT NULL);
"""

_stanje = {"radi": False, "poruka": "", "greska": "", "gotovo": ""}
_lock = threading.Lock()


def pripremi(con):
    con.executescript(SHEMA_VEZE)


def norm(s):
    s = unicodedata.normalize("NFD", str(s or "").lower().replace("đ", "d"))
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).split())


def _baza():
    c = sqlite3.connect(LANCI_DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SHEMA_CIJENE)
    return c


def _cijena(v):
    v = str(v or "").strip().replace(",", ".")
    if not v:
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return int(round(f * 100)) if f > 0 else None


def postavke_modula(con):
    return {"mjesto": postavke.vrijednost(con, "lanci.mjesto", ""),
            "lanci": postavke.vrijednost(con, "lanci.lanci", ""),
            "izvor": postavke.vrijednost(con, "lanci.izvor", IZVOR)}


def spremi_postavke(con, p):
    postavke.postavi(con, "lanci.mjesto", str(p.get("mjesto") or "").strip()[:60])
    postavke.postavi(con, "lanci.lanci", ",".join(norm(x) for x in str(p.get("lanci") or "").split(",") if x.strip()))
    izvor = str(p.get("izvor") or IZVOR).strip().rstrip("/")
    if not izvor.startswith("https://"):
        raise Greska("Izvor mora počinjati s https://")
    postavke.postavi(con, "lanci.izvor", izvor)
    return {"ok": True}


# ---------------------------------------------------------------- uvoz ZIP-a

def uvezi_zip(putanja, mjesto="", samo_lanci=(), poruka=lambda t: None):
    """Pročita ZIP s cjenicima (format cijene.dev: <lanac>/stores.csv, products.csv, prices.csv)
    i upiše sažetak po proizvodu u lanci.db. Vraća {lanac: broj proizvoda}."""
    mj = norm(mjesto)
    zf = zipfile.ZipFile(putanja)
    mape = {}
    for ime in zf.namelist():
        dijelovi = ime.replace("\\", "/").split("/")
        if len(dijelovi) >= 2 and dijelovi[-1] in ("stores.csv", "products.csv", "prices.csv"):
            mape.setdefault(dijelovi[-2], {})[dijelovi[-1]] = ime
    if not mape:
        raise Greska("U ZIP-u nema cjenika (stores.csv, products.csv, prices.csv).")
    datum = ""
    if "archive-info.txt" in zf.namelist():
        datum = zf.read("archive-info.txt").decode("utf-8", "replace")[:200]

    def citaj(ime):
        return csv.DictReader(io.TextIOWrapper(zf.open(ime), encoding="utf-8-sig", errors="replace", newline=""))

    out = {}
    c = _baza()
    try:
        for lanac, f in sorted(mape.items()):
            if samo_lanci and norm(lanac) not in samo_lanci:
                continue
            if not all(k in f for k in ("stores.csv", "products.csv", "prices.csv")):
                continue
            poruka("%s: prodavaonice..." % lanac)
            sve, u_mjestu = set(), set()
            for r in citaj(f["stores.csv"]):
                sve.add(r.get("store_id"))
                if mj and norm(r.get("city")) == mj:
                    u_mjestu.add(r.get("store_id"))
            trgovine = u_mjestu or sve
            cijela_hr = 0 if u_mjestu or not mj else 1
            poruka("%s: cijene (%d prodavaonica)..." % (lanac, len(trgovine)))
            agr = {}
            for r in citaj(f["prices.csv"]):
                if r.get("store_id") not in trgovine:
                    continue
                pid = r.get("product_id")
                cij = _cijena(r.get("price"))
                if pid is None or cij is None:
                    continue
                a = agr.get(pid)
                if a is None:
                    a = agr[pid] = [cij, 0, 0, None, None, None]
                a[0] = min(a[0], cij)
                a[1] += cij
                a[2] += 1
                for i, k in ((3, "special_price"), (4, "unit_price"), (5, "best_price_30")):
                    v = _cijena(r.get(k))
                    if v is not None:
                        a[i] = v if a[i] is None else min(a[i], v)
            poruka("%s: proizvodi..." % lanac)
            redovi = []
            for r in citaj(f["products.csv"]):
                a = agr.get(r.get("product_id"))
                if not a:
                    continue
                naziv = " ".join(str(r.get("name") or "").split())[:200]
                barkod = "".join(ch for ch in str(r.get("barcode") or "") if ch.isdigit())
                redovi.append((lanac, r["product_id"], barkod, naziv, norm(naziv), str(r.get("brand") or "")[:60],
                               str(r.get("category") or "")[:80], str(r.get("unit") or "")[:20],
                               str(r.get("quantity") or "")[:20], a[0], int(round(a[1] / a[2])), a[3], a[4], a[5],
                               a[2], cijela_hr))
            with c:
                c.execute("DELETE FROM cijene WHERE lanac = ?", (lanac,))
                c.executemany("INSERT OR REPLACE INTO cijene VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              redovi)
            out[lanac] = len(redovi)
        with c:
            c.execute("INSERT OR REPLACE INTO info VALUES ('uvezeno', ?)",
                      (dt.datetime.now().strftime("%Y-%m-%d %H:%M"),))
            c.execute("INSERT OR REPLACE INTO info VALUES ('mjesto', ?)", (mjesto,))
            c.execute("INSERT OR REPLACE INTO info VALUES ('arhiva', ?)", (datum or os.path.basename(putanja),))
            c.execute("INSERT OR REPLACE INTO info VALUES ('lanci', ?)", (json.dumps(out),))
    finally:
        c.close()
        zf.close()
    return out


def _preuzmi(izvor, mjesto, samo, datoteka=None):
    def poruka(t):
        with _lock:
            _stanje["poruka"] = t
    tmp = None
    try:
        if datoteka:
            put = datoteka
        else:
            poruka("Tražim zadnji cjenik...")
            with urllib.request.urlopen(urllib.request.Request(izvor + "/v0/list", headers={"User-Agent": "trgovina/1"}),
                                        timeout=30) as r:
                lista = json.loads(r.read().decode("utf-8"))
            arh = sorted(lista.get("archives") or [], key=lambda a: a.get("date") or "")
            if not arh:
                raise Greska("Izvor nema cjenika.")
            zadnja = arh[-1]
            fd, tmp = tempfile.mkstemp(suffix=".zip", dir=os.path.dirname(LANCI_DB))
            os.close(fd)
            with urllib.request.urlopen(urllib.request.Request(zadnja["url"], headers={"User-Agent": "trgovina/1"}),
                                        timeout=60) as r, open(tmp, "wb") as f:
                ukupno, n = int(zadnja.get("size") or 0), 0
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
                    n += len(b)
                    poruka("Preuzimam cjenike od %s: %d MB%s" % (zadnja.get("date"), n >> 20,
                                                                ("/%d MB" % (ukupno >> 20)) if ukupno else ""))
            put = tmp
        rez = uvezi_zip(put, mjesto, samo, poruka)
        with _lock:
            _stanje.update(greska="", gotovo=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                           poruka="Učitano: " + ", ".join("%s %d" % kv for kv in sorted(rez.items())))
    except Exception as e:
        with _lock:
            _stanje.update(greska="Preuzimanje nije uspjelo: %s" % (getattr(e, "reason", None) or e), poruka="")
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
        with _lock:
            _stanje["radi"] = False


def preuzmi(con, datoteka=None):
    """Pokreće preuzimanje u pozadini (ili uvoz ZIP-a s diska ako je zadana datoteka)."""
    p = postavke_modula(con)
    with _lock:
        if _stanje["radi"]:
            raise Greska("Preuzimanje je već u tijeku.")
        _stanje.update(radi=True, greska="", poruka="Počinjem...")
    samo = tuple(x for x in p["lanci"].split(",") if x)
    t = threading.Thread(target=_preuzmi, args=(p["izvor"], p["mjesto"], samo, datoteka), daemon=True)
    t.start()
    return t


def stanje():
    with _lock:
        s = dict(_stanje)
    if os.path.exists(LANCI_DB):
        c = _baza()
        try:
            s["info"] = {r[0]: r[1] for r in c.execute("SELECT * FROM info")}
        finally:
            c.close()
    else:
        s["info"] = {}
    return s


# ---------------------------------------------------------------- usporedba

def trazi(q, lanac=""):
    rijeci = norm(q).split()
    if not rijeci or not os.path.exists(LANCI_DB):
        return []
    sql = "SELECT * FROM cijene WHERE " + " AND ".join("trazi LIKE ?" for _ in rijeci)
    args = ["%" + r + "%" for r in rijeci]
    if lanac:
        sql += " AND lanac = ?"
        args.append(lanac)
    c = _baza()
    try:
        return [dict(r) for r in c.execute(sql + " ORDER BY lanac, cijena_min LIMIT 80", args)]
    finally:
        c.close()


def povezi(con, p):
    try:
        aid = int(p.get("artikl_id"))
    except (TypeError, ValueError):
        raise Greska("Odaberite artikl.")
    lanac, sifra = str(p.get("lanac") or "").strip(), str(p.get("sifra") or "").strip()
    if not lanac:
        raise Greska("Nepoznat lanac.")
    with con:
        if not sifra:  # prazno = makni vezu
            con.execute("DELETE FROM lanci_veze WHERE artikl_id = ? AND lanac = ?", (aid, lanac))
        else:
            con.execute("INSERT OR REPLACE INTO lanci_veze VALUES (?, ?, ?, ?)",
                        (aid, lanac, sifra, str(p.get("naziv") or "")[:200]))
    return {"ok": True}


def _usporedivo(a, r):
    """Cijena lanca usporediva s našom MPC (centi) i oznaka koja je cijena uzeta."""
    kg = a["jm"].lower() in ("kg", "l")
    if kg and r["jed_min"]:
        return r["jed_min"], "za %s" % a["jm"].lower()
    if kg and norm(r["jm"]) in ("kg", "l") and str(r["kolicina"]).strip() in ("", "1", "1.0", "1,0", "1.00"):
        return r["cijena_min"], "za %s" % a["jm"].lower()
    if kg:
        return None, ""
    return r["cijena_min"], "kom"


def usporedba(con, sve=False):
    if not os.path.exists(LANCI_DB):
        return {"stavke": [], "lanci": [], "info": {}, "skupljih": 0, "jeftinijih": 0}
    veze = {}
    for r in con.execute("SELECT * FROM lanci_veze"):
        veze.setdefault(r["artikl_id"], {})[r["lanac"]] = r["sifra"]
    artikli = [dict(r) for r in con.execute(
        "SELECT id, sifra, naziv, jm, cijena, barkod FROM artikli WHERE aktivan = 1 AND usluga = 0 ORDER BY naziv")]
    c = _baza()
    try:
        lanci = [r[0] for r in c.execute("SELECT DISTINCT lanac FROM cijene ORDER BY lanac")]
        info = {r[0]: r[1] for r in c.execute("SELECT * FROM info")}
        stavke, skupljih, jeftinijih = [], 0, 0
        for a in artikli:
            nadeno = {}
            bk = "".join(ch for ch in a["barkod"] if ch.isdigit())
            if len(bk) >= 8 and not bk.startswith("2"):  # 2xxxx su interni kodovi vage
                for r in c.execute("SELECT * FROM cijene WHERE barkod = ?", (bk,)):
                    nadeno[r["lanac"]] = (r, "barkod")
            for lanac, sifra in veze.get(a["id"], {}).items():
                r = c.execute("SELECT * FROM cijene WHERE lanac = ? AND sifra = ?", (lanac, sifra)).fetchone()
                nadeno[lanac] = (r, "ručno") if r else (None, "ručno, nema u zadnjem cjeniku")
            if not nadeno and not sve:
                continue
            po_lancu = {}
            cij = []
            for lanac, (r, kako) in nadeno.items():
                if not r:
                    po_lancu[lanac] = {"kako": kako}
                    continue
                v, oznaka = _usporedivo(a, r)
                akc = r["akcija_min"] if r["akcija_min"] and (not v or r["akcija_min"] < r["cijena_min"]) else None
                po_lancu[lanac] = {"cijena": v, "oznaka": oznaka, "naziv": r["naziv"], "akcija": akc if oznaka == "kom" else None,
                                   "trgovina": r["trgovina"], "cijela_hr": r["cijela_hr"], "kako": kako}
                if v:
                    cij.append(v)
            d = dict(a, lanci=po_lancu)
            if cij and a["cijena"]:
                najniza = min(cij)
                d["najniza_lanac"] = najniza
                d["razlika"] = round((a["cijena"] - najniza) * 100.0 / najniza, 1)
                if d["razlika"] > SKUPLJI:
                    skupljih += 1
                elif d["razlika"] < 0:
                    jeftinijih += 1
            stavke.append(d)
        return {"stavke": stavke, "lanci": lanci, "info": info, "skupljih": skupljih, "jeftinijih": jeftinijih,
                "prag": SKUPLJI}
    finally:
        c.close()
