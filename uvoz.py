# -*- coding: utf-8 -*-
"""
Uvoz podataka iz programa Roba u našu bazu (trgovina.db).

Robine datoteke se SAMO ČITAJU. Uvoz se smije ponavljati koliko god treba:
- artikli i načini plaćanja se ažuriraju prema Robi (Roba je za sada glavna
  za cijene i zalihu); naši vlastiti artikli ostaju netaknuti,
- povijest dokumenata iz Robe se svaki put učita ispočetka,
- naši računi s kase se nikad ne diraju.

Pokretanje:
    python uvoz.py "C:\\Roba"
"""

import datetime as dt
import os
import sys

import cijene
import postavke
import roba_dodatno
from baza import centi, spoji
from dbf import find_file, read_dbf


def godine_u_mapi(root):
    out = []
    for f in sorted(os.listdir(root)):
        p = os.path.join(root, f)
        if f.isdigit() and len(f) == 4 and os.path.isdir(p) and find_file(p, "mat.dbf"):
            out.append((int(f), p))
    return out


def citaj(folder, ime):
    p = find_file(folder, ime)
    return read_dbf(p) if p else []


def naziv_placanja(opis):
    opis = opis.strip()
    return (opis.capitalize() if opis.isupper() else opis) or "Ostalo"


def uvezi(root, con, ispis=print):
    godine = godine_u_mapi(root)
    if not godine:
        raise SystemExit("U mapi %s nema godišnjih mapa (npr. 2026) s datotekom mat.dbf." % root)
    zadnja_god, zadnja = godine[-1]
    ispis("Mapa Roba: %s, godine: %s" % (root, ", ".join(str(g) for g, _ in godine)))

    with con:
        # --- stope PDV-a i načini plaćanja (iz zadnje godine)
        stope = {p["SIFRA"]: p["STOPA"] for p in citaj(zadnja, "porez.dbf")}
        for v in citaj(zadnja, "vrste.dbf"):
            con.execute(
                "INSERT INTO nacini_placanja (roba_sifra, naziv) VALUES (?, ?) "
                "ON CONFLICT(roba_sifra) DO UPDATE SET naziv = excluded.naziv",
                (v["SIFRA"], naziv_placanja(v["OPIS"])))

        # --- artikli (osim kad su zamijenjeni artiklima druge trgovine, vidi uvoz_artikala.py)
        n = 0
        bez_artikala = postavke.vrijednost(con, "uvoz.bez_artikala", "0") == "1"
        if bez_artikala:
            ispis("Artikli iz Robe se ne uvoze (zamijenjeni u Postavkama, Uvoz artikala druge trgovine).")
        for a in ([] if bez_artikala else citaj(zadnja, "artikli.dbf")):
            barkod = a.get("BARCODE", "").strip()
            if not barkod.strip("0"):
                barkod = ""
            stopa = stope.get(a.get("PDV"), a.get("PDV_I", 25.0))
            con.execute(
                """INSERT INTO artikli (roba_sifra, sifra, barkod, naziv, jm, cijena,
                                        pdv_stopa, zaliha, nabavna, aktivan)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(roba_sifra) DO UPDATE SET
                       sifra = excluded.sifra, barkod = excluded.barkod,
                       naziv = excluded.naziv, jm = excluded.jm,
                       cijena = excluded.cijena, pdv_stopa = excluded.pdv_stopa,
                       zaliha = excluded.zaliha,
                       nabavna = CASE WHEN artikli.nabavna_nasa = 1 THEN artikli.nabavna
                                      ELSE excluded.nabavna END,
                       aktivan = excluded.aktivan""",
                (a["SIFRA"], a.get("MSIFRA", "").strip() or str(a["SIFRA"]), barkod,
                 a["NAZIV"].strip(), a.get("JM", "Kom").strip() or "Kom",
                 centi(a.get("CIJENA")), stopa, round(a.get("KOLICINA", 0.0), 3),
                 centi(a.get("PROS")), 0 if a.get("NEAKTIVAN") else 1))
            n += 1
        ispis("Artikli: %d" % n)

        # --- dobavljači
        for d in citaj(zadnja, "dobav.dbf"):
            if d["SIFRA"] == 999999:  # "MALOPRODAJA", nije dobavljač
                continue
            oib = d.get("OIB") or ""
            if isinstance(oib, float):
                oib = "%011d" % oib if oib else ""
            con.execute(
                "INSERT INTO partneri (roba_sifra, naziv, oib) VALUES (?, ?, ?) "
                "ON CONFLICT(roba_sifra) DO UPDATE SET naziv = excluded.naziv, oib = excluded.oib "
                "WHERE partneri.uredjen = 0",
                (d["SIFRA"], " ".join(d["NAZIV"].split()), str(oib).strip()))

        # --- povijest dokumenata, sve godine
        con.execute("DELETE FROM roba_stavke")
        con.execute("DELETE FROM roba_dokumenti")
        con.execute("DELETE FROM roba_placanja")
        for god, folder in godine:
            partneri = {d["SIFRA"]: d.get("NAZIV", "").strip() for d in citaj(folder, "dobav.dbf")}
            dok = citaj(folder, "mat.dbf")
            con.executemany(
                "INSERT INTO roba_dokumenti VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(god, m["SIFRA"], m.get("DOKUMENT", "").strip(), int(m.get("STATUS") or 0),
                  m.get("DATUM"),
                  m["DATVRI"].strftime("%Y-%m-%d %H:%M:%S") if m.get("DATVRI") else None,
                  centi(m.get("NETO")), centi(m.get("POREZ")), centi(m.get("IZNOS")),
                  partneri.get(m.get("DOBAVLJAC"), m.get("XXXX", "").strip()),
                  m.get("VRSTAPL"), 1 if m.get("BRISAN") else 0,
                  m.get("FJIR", "").strip())
                 for m in dok])
            st = citaj(folder, "mstavke.dbf")
            con.executemany(
                """INSERT INTO roba_stavke (godina, sifra, link, status, datum, artikl, naziv, jm,
                   kolicina, iznos, neto, nabavna, pdv_stopa, cijena, porez)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(god, s["SIFRA"], s.get("LINK") or 0, int(s.get("STATUS") or 0), s.get("DATUM"),
                  s.get("ARTIKAL"), s.get("XXXX", "").strip(), s.get("JM", "").strip(),
                  s.get("KOLICINA", 0.0), centi(s.get("IZNOS")), centi(s.get("VPC_V")),
                  centi(s.get("NABAVNAV")), stope.get(s.get("PDVLINK"), float(s.get("PDV") or 0)),
                  centi(s.get("VPCPDV")), centi(s.get("PDV_IV")))
                 for s in st])
            # plaćanja: račun plaćen dijelom gotovinom, dijelom karticom ima više redova
            con.executemany(
                "INSERT INTO roba_placanja (godina, link, placanje, iznos) VALUES (?,?,?,?)",
                [(god, p.get("LINK") or 0, p.get("LINKVR"), centi(p.get("IZNOS")))
                 for p in citaj(folder, "mplacanje.dbf")])
            ispis("%d: dokumenata %d, stavki %d" % (god, len(dok), len(st)))

        # --- KPD, Robina sidrena cijena i zapisnici o promjeni cijena (novija Roba)
        roba_dodatno.uvezi(con, godine, citaj, zadnja, ispis)
        if postavke.ukljucen(con, "roba_sidrena"):
            n_s = roba_dodatno.primijeni_sidrenu(con)
            if n_s:
                ispis("Sidrena cijena preuzeta iz Robe za %d artikala." % n_s)

        # --- upozorenje: primke upisane i u Robi nakon što ih vodimo ovdje
        prva = con.execute("SELECT MIN(datum) FROM dokumenti WHERE vrsta = 'primka'").fetchone()[0]
        if prva:
            dupl = con.execute(
                "SELECT COUNT(*) FROM roba_dokumenti WHERE status = 1 AND brisan = 0 AND datum >= ?",
                (prva,)).fetchone()[0]
            if dupl:
                ispis("UPOZORENJE: u Robi postoji %d primki od %s, a primke vodimo ovdje. "
                      "Zaliha za te artikle može biti dvostruka." % (dupl, prva))

        # --- sidrena cijena za nove artikle (dan kad su ušli u ponudu), vidi cijene.py
        nove = cijene.popuni(con)
        if nove:
            ispis("Sidrena cijena upisana za %d artikala." % nove)

        opis = "artikala %d, godine %s" % (n, ", ".join(str(g) for g, _ in godine))
        con.execute("INSERT INTO uvozi (vrijeme, mapa, opis) VALUES (?, ?, ?)",
                    (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), root, opis))
    return opis


def provjera(con, ispis=print):
    """Usporedba: brojke iz uvezene povijesti, da se mogu provjeriti u Robi."""
    ispis("\nProvjera (usporedite s Robom):")
    ispis("  Mjesec    Računa      Promet")
    for r in con.execute(
            """SELECT substr(datum,1,7) AS mj, COUNT(*) AS n, SUM(iznos) AS iznos
               FROM roba_dokumenti WHERE status = 3 GROUP BY mj ORDER BY mj"""):
        ispis("  %s  %6d  %10.2f EUR" % (r["mj"], r["n"], r["iznos"] / 100))
    r = con.execute("SELECT COUNT(*) n, SUM(zaliha * nabavna) v FROM artikli "
                    "WHERE zaliha > 0").fetchone()
    ispis("  Artikala na zalihi: %d, nabavna vrijednost zalihe: %.2f EUR"
          % (r["n"], (r["v"] or 0) / 100))
    minus = con.execute("SELECT COUNT(*) FROM artikli WHERE zaliha < 0").fetchone()[0]
    if minus:
        ispis("  Artikala u minusu: %d" % minus)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    root = sys.argv[1]
    con = spoji()
    uvezi(root, con)
    provjera(con)
    print("\nGotovo. Baza: trgovina.db")


if __name__ == "__main__":
    main()
