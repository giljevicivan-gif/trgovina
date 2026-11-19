# -*- coding: utf-8 -*-
"""
Dodatni podaci iz novije Robe: sidrena cijena, zapisnici o promjeni cijena
(ZPC) i KPD šifre. Sve se samo čita iz Robe; što se od toga koristi,
uključuje se u Postavkama.

- Sidrena cijena (artikli DCIJENA / DDATUM): Roba je sama računa po Odluci
  o isticanju dodatne cijene (NN 101/2026). Kad je modul "roba_sidrena"
  uključen, naša etiketa koristi Robinu, osim kad je cijena ručno upisana kod nas.
- ZPC / ZPCSTAV: zapisnik o promjeni cijena (stara i nova MPC po artiklu,
  količina na zalihi). Daje točan datum promjene, pa je i najniža cijena u
  30 dana prije akcije točnija nego samo iz računa.
- KPD 2025 (artikli KPD, šifrarnik KPD2025.DBF): klasifikacija proizvoda koja
  je obvezna na eRačunu (Fiskalizacija 2.0). Upisuje se u Robi, jer Roba izdaje
  eRačun; ovdje samo provjeravamo koji artikli prodani tvrtkama je nemaju.
"""

import datetime as dt

from baza import centi

SHEMA = """
CREATE TABLE IF NOT EXISTS roba_zpc (
    godina   INTEGER NOT NULL,
    sifra    INTEGER NOT NULL,
    broj     INTEGER NOT NULL,
    datum    TEXT NOT NULL,
    stari    INTEGER NOT NULL,     -- stara vrijednost zalihe po MPC, centi
    novi     INTEGER NOT NULL,
    PRIMARY KEY (godina, sifra)
);
CREATE TABLE IF NOT EXISTS roba_zpc_stavke (
    godina    INTEGER NOT NULL,
    link      INTEGER NOT NULL,    -- roba_zpc.sifra
    datum     TEXT NOT NULL,
    artikl    INTEGER NOT NULL,    -- Robina šifra artikla
    kolicina  REAL NOT NULL,
    pdv_stopa REAL NOT NULL,
    stara     INTEGER NOT NULL,    -- MPC prije, centi
    nova      INTEGER NOT NULL     -- MPC poslije, centi
);
CREATE INDEX IF NOT EXISTS ix_zpc_art ON roba_zpc_stavke(artikl, datum);
CREATE TABLE IF NOT EXISTS kpd_sifre (
    sifra  TEXT PRIMARY KEY,       -- 6 znamenki, kako ih Roba pamti (npr. 464923)
    oznaka TEXT NOT NULL,          -- npr. G46.49.23
    naziv  TEXT NOT NULL
);
"""


def pripremi(con):
    con.executescript(SHEMA)
    stupci = {r[1] for r in con.execute("PRAGMA table_info(artikli)")}
    if "kpd" not in stupci:
        con.execute("ALTER TABLE artikli ADD COLUMN kpd TEXT NOT NULL DEFAULT ''")
        con.execute("ALTER TABLE artikli ADD COLUMN roba_sidrena INTEGER NOT NULL DEFAULT 0")
        con.execute("ALTER TABLE artikli ADD COLUMN roba_sidrena_datum TEXT NOT NULL DEFAULT ''")
        # '' = izračunao naš program, 'roba' = preuzeto iz Robe, 'rucno' = upisano kod nas
        con.execute("ALTER TABLE artikli ADD COLUMN sidrena_izvor TEXT NOT NULL DEFAULT ''")
        con.commit()


def _datum(v):
    if not v:
        return ""
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()[:10]
    return str(v)[:10]


def uvezi(con, godine, citaj, zadnja, ispis=print):
    """Poziva se iz uvoz.uvezi unutar iste transakcije."""
    # --- KPD i sidrena iz artikala zadnje godine
    n_kpd = n_sid = 0
    for a in citaj(zadnja, "artikli.dbf"):
        kpd = str(a.get("KPD") or "").strip()
        dc, dd = a.get("DCIJENA"), _datum(a.get("DDATUM"))
        if not kpd and not (dc and dd):
            continue
        con.execute("UPDATE artikli SET kpd = ?, roba_sidrena = ?, roba_sidrena_datum = ? WHERE roba_sifra = ?",
                    (kpd, centi(dc) if dc and dd else 0, dd if dc else "", a["SIFRA"]))
        n_kpd += bool(kpd)
        n_sid += bool(dc and dd)
    if n_kpd or n_sid:
        ispis("Iz Robe: KPD šifra za %d artikala, sidrena cijena za %d." % (n_kpd, n_sid))
    sif = citaj(zadnja, "kpd2025.dbf")
    if sif:
        con.execute("DELETE FROM kpd_sifre")
        con.executemany("INSERT OR REPLACE INTO kpd_sifre VALUES (?, ?, ?)",
                        [(k["RAZINA6"].strip(), k.get("PSIFRA", "").strip(), k.get("HRV6", "").strip())
                         for k in sif if k.get("RAZINA6")])
    # --- zapisnici o promjeni cijena, sve godine
    con.execute("DELETE FROM roba_zpc")
    con.execute("DELETE FROM roba_zpc_stavke")
    n = 0
    for god, folder in godine:
        z = citaj(folder, "zpc.dbf")
        con.executemany("INSERT OR REPLACE INTO roba_zpc VALUES (?, ?, ?, ?, ?, ?)",
                        [(god, r["SIFRA"], int(r.get("BROJ") or 0), _datum(r.get("DATUM")),
                          centi(r.get("SIZNOS")), centi(r.get("NIZNOS"))) for r in z])
        st = citaj(folder, "zpcstav.dbf")
        con.executemany("INSERT INTO roba_zpc_stavke VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        [(god, s["LINK"], _datum(s.get("DATUM")), s["ARTIKAL"], s.get("KOLICINA") or 0.0,
                          s.get("PDV_I") or 0.0, centi(s.get("SMPC")), centi(s.get("NMPC"))) for s in st])
        n += len(z)
    if n:
        ispis("Zapisnika o promjeni cijena: %d." % n)


def primijeni_sidrenu(con):
    """Robina sidrena cijena na artikle (ne dira ručno upisane). Vraća broj promijenjenih."""
    return con.execute(
        """UPDATE artikli SET sidrena = roba_sidrena, sidrena_datum = roba_sidrena_datum, sidrena_izvor = 'roba'
           WHERE roba_sidrena > 0 AND roba_sidrena_datum <> '' AND sidrena_izvor <> 'rucno'
           AND (sidrena <> roba_sidrena OR sidrena_datum <> roba_sidrena_datum OR sidrena_izvor <> 'roba')"""
    ).rowcount


def razlike_sidrene(con):
    """Artikli kojima se naša sidrena cijena razlikuje od Robine."""
    return [dict(r) for r in con.execute(
        """SELECT id, naziv, sidrena, sidrena_datum, roba_sidrena, roba_sidrena_datum, sidrena_izvor
           FROM artikli WHERE aktivan = 1 AND roba_sidrena > 0 AND sidrena_datum <> ''
           AND (sidrena <> roba_sidrena OR sidrena_datum <> roba_sidrena_datum) ORDER BY naziv""")]


# ---------------------------------------------------------------- ZPC

def zpc_popis(con, godina=None):
    sql = """SELECT z.*, COUNT(s.artikl) AS stavki FROM roba_zpc z
             LEFT JOIN roba_zpc_stavke s ON s.godina = z.godina AND s.link = z.sifra"""
    args = []
    if godina:
        sql += " WHERE substr(z.datum, 1, 4) = ?"
        args.append(str(godina))
    return [dict(r) for r in con.execute(sql + " GROUP BY z.godina, z.sifra ORDER BY z.datum DESC, z.broj DESC",
                                         args)]


def zpc(con, godina, sifra):
    z = con.execute("SELECT * FROM roba_zpc WHERE godina = ? AND sifra = ?", (godina, sifra)).fetchone()
    if not z:
        from nabava import Greska
        raise Greska("Zapisnik ne postoji.")
    d = dict(z)
    d["stavke"] = [dict(r) for r in con.execute(
        """SELECT s.*, COALESCE(a.naziv, '?') AS naziv, COALESCE(a.jm, '') AS jm, a.sifra AS nasa_sifra
           FROM roba_zpc_stavke s LEFT JOIN artikli a ON a.roba_sifra = s.artikl
           WHERE s.godina = ? AND s.link = ? ORDER BY naziv""", (godina, sifra))]
    return d


def povijest(con, roba_sifra):
    """Promjene cijene artikla iz zapisnika, od najnovije."""
    if roba_sifra is None:
        return []
    return [dict(r) for r in con.execute(
        """SELECT s.datum, s.stara, s.nova, s.kolicina, z.broj FROM roba_zpc_stavke s
           JOIN roba_zpc z ON z.godina = s.godina AND z.sifra = s.link
           WHERE s.artikl = ? ORDER BY s.datum DESC, z.broj DESC""", (roba_sifra,))]


def cijene_u_razdoblju(con, roba_sifra, od, do):
    """MPC koje su vrijedile između od i do prema zapisnicima (za najnižu cijenu u 30 dana)."""
    out = []
    for r in con.execute("SELECT datum, stara, nova FROM roba_zpc_stavke WHERE artikl = ? AND datum <= ?",
                         (roba_sifra, do)):
        if r["datum"] >= od:
            out += [c for c in (r["stara"], r["nova"]) if c > 0]   # i stara je vrijedila u razdoblju
    zadnja_prije = con.execute(
        "SELECT nova FROM roba_zpc_stavke WHERE artikl = ? AND datum < ? ORDER BY datum DESC LIMIT 1",
        (roba_sifra, od)).fetchone()
    if zadnja_prije and zadnja_prije[0] > 0:
        out.append(zadnja_prije[0])
    return out


# ---------------------------------------------------------------- KPD

def bez_kpd(con, dana=365):
    """Artikli prodani tvrtkama (račun s kupcem u Robi) bez KPD šifre ili s nepoznatom šifrom."""
    od = (dt.date.today() - dt.timedelta(days=dana)).isoformat()
    poznate = {r[0] for r in con.execute("SELECT sifra FROM kpd_sifre")}
    out = []
    for r in con.execute(
            """SELECT a.id, a.naziv, a.kpd, COUNT(DISTINCT d.sifra) AS racuna FROM roba_stavke s
               JOIN roba_dokumenti d ON d.godina = s.godina AND d.sifra = s.link
               JOIN artikli a ON a.roba_sifra = s.artikl
               WHERE d.status IN (2, 3) AND d.datum >= ? AND d.partner <> ''
               AND upper(d.partner) NOT LIKE 'MALOPRODAJA%' AND a.aktivan = 1
               GROUP BY a.id ORDER BY racuna DESC, a.naziv""" , (od,)):
        k = r["kpd"]
        if not k:
            out.append({"naziv": r["naziv"], "kpd": "", "racuna": r["racuna"], "problem": "nema KPD"})
        elif poznate and k not in poznate:
            out.append({"naziv": r["naziv"], "kpd": k, "racuna": r["racuna"], "problem": "nepoznata šifra"})
    return out


def kpd_naziv(con, sifra):
    r = con.execute("SELECT oznaka, naziv FROM kpd_sifre WHERE sifra = ?", (sifra,)).fetchone()
    return (r["oznaka"] + " " + r["naziv"]) if r else ""
