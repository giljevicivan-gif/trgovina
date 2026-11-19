# -*- coding: utf-8 -*-
"""
Poklon bonovi i kartica vjernosti (dva modula, svaki se uključuje posebno).

POKLON BONOVI
Bon se prodaje i iskorištava preko Robe (fiskalizirani račun), a ovdje se vodi
evidencija: broj, iznos, rok, koliko je ostalo. Tako se ne može iskoristiti
dvaput ni nakon roka.
- Bon za bilo koju robu u trgovini (voće 5 %, ostalo 25 %) je VIŠENAMJENSKI
  vrijednosni kupon (Zakon o PDV-u, NN 115/2016): kod prodaje bona nema PDV-a,
  PDV se obračunava kad se bonom plati roba. U Robi: prodaja bona kao stavka
  bez PDV-a (KPD 66.19.99 za eRačun), a kod kupnje bon kao način plaćanja.
  Točan način upisa u Robu potvrdite s knjigovođom.
- Program ne izdaje račun; ispis bona nosi oznaku da nije račun.

KARTICA VJERNOSTI
Bodovi za kupnje, nagrada popust. Popust se daje na računu u Robi.
- Osobni podaci (GDPR): upisuje se samo što treba (ime, telefon ili e-mail
  nije obavezno), uz privolu člana s datumom. Na zahtjev člana podaci se
  brišu (ostaju samo bodovi bez imena).
"""

import datetime as dt
import secrets

import postavke
from baza import zaokruzi
from nabava import Greska, sada

SHEMA = """
CREATE TABLE IF NOT EXISTS bonovi (
    id          INTEGER PRIMARY KEY,
    kod         TEXT NOT NULL UNIQUE,
    iznos       INTEGER NOT NULL,             -- centi
    izdan       TEXT NOT NULL,                -- datum
    vrijedi_do  TEXT NOT NULL,
    racun_roba  TEXT NOT NULL DEFAULT '',     -- račun iz Robe kojim je bon prodan
    napomena    TEXT NOT NULL DEFAULT '',
    ponisten    TEXT NOT NULL DEFAULT '',     -- razlog; prazno = vrijedi
    upisano     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bon_iskoristenja (
    id          INTEGER PRIMARY KEY,
    bon_id      INTEGER NOT NULL REFERENCES bonovi(id),
    vrijeme     TEXT NOT NULL,
    iznos       INTEGER NOT NULL,
    racun_roba  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS vjernost_clanovi (
    id          INTEGER PRIMARY KEY,
    kartica     TEXT NOT NULL UNIQUE,
    ime         TEXT NOT NULL DEFAULT '',
    kontakt     TEXT NOT NULL DEFAULT '',     -- telefon ili e-mail, nije obavezno
    privola     TEXT NOT NULL,                -- datum privole za obradu podataka
    upisano     TEXT NOT NULL,
    obrisan     TEXT NOT NULL DEFAULT ''      -- datum brisanja osobnih podataka
);
CREATE TABLE IF NOT EXISTS vjernost_bodovi (
    id          INTEGER PRIMARY KEY,
    clan_id     INTEGER NOT NULL REFERENCES vjernost_clanovi(id),
    vrijeme     TEXT NOT NULL,
    vrsta       TEXT NOT NULL CHECK (vrsta IN ('kupnja', 'nagrada', 'ispravak')),
    iznos       INTEGER NOT NULL DEFAULT 0,   -- iznos računa ili popusta, centi
    bodovi      INTEGER NOT NULL,
    racun_roba  TEXT NOT NULL DEFAULT '',
    napomena    TEXT NOT NULL DEFAULT ''
);
"""

ROK_BONA_DANA = 365


def pripremi(con):
    con.executescript(SHEMA)


def _centi(v, poruka="Neispravan iznos."):
    try:
        c = zaokruzi(float(str(v or "").replace(" ", "").replace(",", ".")) * 100)
    except ValueError:
        raise Greska(poruka)
    return c


def _tekst(p, k, n=80):
    return " ".join(str(p.get(k) or "").split())[:n]


# ---------------------------------------------------------------- Bonovi

def _stanje_bona(con, b):
    d = dict(b)
    d["iskoristeno"] = con.execute("SELECT COALESCE(SUM(iznos), 0) FROM bon_iskoristenja WHERE bon_id = ?",
                                   (b["id"],)).fetchone()[0]
    d["ostalo"] = b["iznos"] - d["iskoristeno"]
    danas = dt.date.today().isoformat()
    d["stanje"] = ("poništen" if b["ponisten"] else "iskorišten" if d["ostalo"] <= 0 else
                   "istekao" if b["vrijedi_do"] < danas else "vrijedi")
    return d


def izdaj_bon(con, p):
    iznos = _centi(p.get("iznos"))
    if not 100 <= iznos <= 100000:
        raise Greska("Iznos bona 1 do 1.000 EUR.")
    danas = dt.date.today()
    try:
        do = dt.date.fromisoformat(str(p.get("vrijedi_do") or "")) if p.get("vrijedi_do") else \
            danas + dt.timedelta(days=ROK_BONA_DANA)
    except ValueError:
        raise Greska("Neispravan datum.")
    if do <= danas:
        raise Greska("Bon mora vrijediti barem do sutra.")
    racun = _tekst(p, "racun_roba", 60)
    for _ in range(20):
        kod = "B%d-%s" % (danas.year % 100, "".join(secrets.choice("ACDEFHJKMNPRTUVWXY3479") for _ in range(6)))
        if not con.execute("SELECT 1 FROM bonovi WHERE kod = ?", (kod,)).fetchone():
            break
    with con:
        bid = con.execute(
            """INSERT INTO bonovi (kod, iznos, izdan, vrijedi_do, racun_roba, napomena, upisano)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (kod, iznos, danas.isoformat(), do.isoformat(), racun, _tekst(p, "napomena"), sada())).lastrowid
    return bon(con, bid)


def bon(con, bid=None, kod=None):
    if kod is not None:
        b = con.execute("SELECT * FROM bonovi WHERE kod = ?", (str(kod).strip().upper(),)).fetchone()
    else:
        b = con.execute("SELECT * FROM bonovi WHERE id = ?", (bid,)).fetchone()
    if not b:
        raise Greska("Nema bona s tim brojem.")
    d = _stanje_bona(con, b)
    d["iskoristenja"] = [dict(r) for r in con.execute(
        "SELECT * FROM bon_iskoristenja WHERE bon_id = ? ORDER BY vrijeme", (b["id"],))]
    return d


def iskoristi_bon(con, p):
    racun = _tekst(p, "racun_roba", 60)
    if not racun:
        raise Greska("Upišite broj računa iz Robe na kojem je plaćeno bonom.")
    iznos = _centi(p.get("iznos"))
    con.execute("BEGIN IMMEDIATE")
    try:
        b = bon(con, kod=p.get("kod"))
        if b["stanje"] != "vrijedi":
            raise Greska("Bon %s: %s." % (b["kod"], b["stanje"]))
        if not 0 < iznos <= b["ostalo"]:
            raise Greska("Na bonu je ostalo %s EUR." % ("%.2f" % (b["ostalo"] / 100.0)).replace(".", ","))
        con.execute("INSERT INTO bon_iskoristenja (bon_id, vrijeme, iznos, racun_roba) VALUES (?, ?, ?, ?)",
                    (b["id"], sada(), iznos, racun))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return bon(con, b["id"])


def uredi_bon(con, p):
    """Upis broja računa prodaje, produljenje roka ili poništenje (uz razlog)."""
    b = bon(con, int(p.get("id") or 0))
    with con:
        if "racun_roba" in p and _tekst(p, "racun_roba", 60) != b["racun_roba"]:
            if b["racun_roba"]:
                raise Greska("Broj računa prodaje bona je već upisan (%s) i ne mijenja se." % b["racun_roba"])
            con.execute("UPDATE bonovi SET racun_roba = ? WHERE id = ?", (_tekst(p, "racun_roba", 60), b["id"]))
        if p.get("vrijedi_do"):
            try:
                do = dt.date.fromisoformat(str(p["vrijedi_do"])).isoformat()
            except ValueError:
                raise Greska("Neispravan datum.")
            if do < b["vrijedi_do"]:
                raise Greska("Rok bona se može samo produljiti.")
            con.execute("UPDATE bonovi SET vrijedi_do = ? WHERE id = ?", (do, b["id"]))
        if p.get("ponisti"):
            razlog = _tekst(p, "razlog", 120)
            if not razlog:
                raise Greska("Upišite razlog poništenja.")
            if b["iskoristeno"]:
                raise Greska("Bon je već djelomično iskorišten i ne može se poništiti.")
            con.execute("UPDATE bonovi SET ponisten = ? WHERE id = ?", (razlog, b["id"]))
    return bon(con, b["id"])


def popis_bonova(con, svi=False):
    out = [_stanje_bona(con, b) for b in con.execute("SELECT * FROM bonovi ORDER BY id DESC LIMIT 300")]
    return out if svi else [b for b in out if b["stanje"] in ("vrijedi", "istekao") and b["ostalo"] > 0][:100] + \
        [b for b in out if b["stanje"] not in ("vrijedi", "istekao")][:20]


def upozorenja_bonova(con):
    danas = dt.date.today()
    za30 = (danas + dt.timedelta(days=30)).isoformat()
    sve = [_stanje_bona(con, b) for b in con.execute("SELECT * FROM bonovi WHERE ponisten = ''")]
    return {"bez_racuna": [b for b in sve if not b["racun_roba"]],
            "istice": [b for b in sve if b["stanje"] == "vrijedi" and b["vrijedi_do"] <= za30],
            "u_opticaju": sum(b["ostalo"] for b in sve if b["stanje"] == "vrijedi")}


# ---------------------------------------------------------------- Vjernost

def pravila(con):
    g = postavke.vrijednost
    return {"bodova_po_euru": float(g(con, "vjernost.bodova_po_euru", "1")),
            "prag": int(g(con, "vjernost.prag", "100")),
            "nagrada": int(g(con, "vjernost.nagrada", "500"))}   # centi


def spremi_pravila(con, p):
    try:
        bpe = round(float(str(p.get("bodova_po_euru")).replace(",", ".")), 2)
        prag = int(p.get("prag"))
        nagrada = _centi(p.get("nagrada"))
    except (TypeError, ValueError):
        raise Greska("Neispravna pravila.")
    if not 0 < bpe <= 100 or not 1 <= prag <= 100000 or not 0 < nagrada <= 10000:
        raise Greska("Neispravna pravila (nagrada do 100 EUR).")
    for k, v in (("bodova_po_euru", bpe), ("prag", prag), ("nagrada", nagrada)):
        postavke.postavi(con, "vjernost." + k, v)
    return pravila(con)


def _clan(con, cid=None, kartica=None):
    if kartica is not None:
        c = con.execute("SELECT * FROM vjernost_clanovi WHERE kartica = ?", (str(kartica).strip(),)).fetchone()
    else:
        c = con.execute("SELECT * FROM vjernost_clanovi WHERE id = ?", (cid,)).fetchone()
    if not c:
        raise Greska("Nema člana s tom karticom.")
    d = dict(c)
    d["bodovi"] = con.execute("SELECT COALESCE(SUM(bodovi), 0) FROM vjernost_bodovi WHERE clan_id = ?",
                              (c["id"],)).fetchone()[0]
    return d


def clan(con, cid=None, kartica=None):
    d = _clan(con, cid, kartica)
    d["povijest"] = [dict(r) for r in con.execute(
        "SELECT * FROM vjernost_bodovi WHERE clan_id = ? ORDER BY vrijeme DESC LIMIT 50", (d["id"],))]
    d["pravila"] = pravila(con)
    return d


def novi_clan(con, p):
    if not p.get("privola"):
        raise Greska("Član mora dati privolu za obradu podataka (potpis na pristupnici).")
    kartica = _tekst(p, "kartica", 30)
    if not kartica:
        n = con.execute("SELECT COUNT(*) FROM vjernost_clanovi").fetchone()[0] + 1
        kartica = "V%05d" % n
        while con.execute("SELECT 1 FROM vjernost_clanovi WHERE kartica = ?", (kartica,)).fetchone():
            n += 1
            kartica = "V%05d" % n
    elif con.execute("SELECT 1 FROM vjernost_clanovi WHERE kartica = ?", (kartica,)).fetchone():
        raise Greska("Kartica s tim brojem već postoji.")
    with con:
        cid = con.execute(
            "INSERT INTO vjernost_clanovi (kartica, ime, kontakt, privola, upisano) VALUES (?, ?, ?, ?, ?)",
            (kartica, _tekst(p, "ime", 60), _tekst(p, "kontakt", 60), dt.date.today().isoformat(),
             sada())).lastrowid
    return clan(con, cid)


def obrisi_podatke(con, cid):
    """GDPR: brisanje osobnih podataka na zahtjev člana; bodovi ostaju bez imena."""
    c = _clan(con, cid)
    with con:
        con.execute("UPDATE vjernost_clanovi SET ime = '', kontakt = '', obrisan = ? WHERE id = ?",
                    (dt.date.today().isoformat(), c["id"]))


def kupnja(con, p):
    racun = _tekst(p, "racun_roba", 60)
    if not racun:
        raise Greska("Upišite broj računa iz Robe.")
    iznos = _centi(p.get("iznos"))
    if not 0 < iznos <= 1000000:
        raise Greska("Neispravan iznos računa.")
    c = _clan(con, kartica=p.get("kartica"))
    if c["obrisan"]:
        raise Greska("Članstvo je ukinuto.")
    if con.execute("SELECT 1 FROM vjernost_bodovi WHERE racun_roba = ? AND vrsta = 'kupnja'", (racun,)).fetchone():
        raise Greska("Za račun %s bodovi su već upisani." % racun)
    bod = int(iznos / 100.0 * pravila(con)["bodova_po_euru"] + 1e-9)
    with con:
        con.execute("""INSERT INTO vjernost_bodovi (clan_id, vrijeme, vrsta, iznos, bodovi, racun_roba)
                       VALUES (?, ?, 'kupnja', ?, ?, ?)""", (c["id"], sada(), iznos, bod, racun))
    return clan(con, c["id"])


def nagrada(con, p):
    racun = _tekst(p, "racun_roba", 60)
    if not racun:
        raise Greska("Upišite broj računa iz Robe na kojem je dan popust.")
    pr = pravila(con)
    con.execute("BEGIN IMMEDIATE")
    try:
        c = _clan(con, kartica=p.get("kartica"))
        if c["obrisan"]:
            raise Greska("Član je obrisan (podaci izbrisani na njegov zahtjev).")
        if c["bodovi"] < pr["prag"]:
            raise Greska("Član ima %d bodova, za nagradu treba %d." % (c["bodovi"], pr["prag"]))
        con.execute("""INSERT INTO vjernost_bodovi (clan_id, vrijeme, vrsta, iznos, bodovi, racun_roba)
                       VALUES (?, ?, 'nagrada', ?, ?, ?)""", (c["id"], sada(), pr["nagrada"], -pr["prag"], racun))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return clan(con, c["id"])


def clanovi(con):
    return [dict(r) for r in con.execute(
        """SELECT c.*, COALESCE((SELECT SUM(bodovi) FROM vjernost_bodovi b WHERE b.clan_id = c.id), 0) AS bodovi
           FROM vjernost_clanovi c ORDER BY c.id DESC LIMIT 500""")]
