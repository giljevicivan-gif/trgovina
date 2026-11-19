# -*- coding: utf-8 -*-
"""
Propisi o cijenama (provjereno listopad 2026.):

- Sidrena (dodatna) cijena, od 17. 11. 2026. za SVE trgovce na malo
  (Odluka o isticanju dodatne cijene, NN 101/2026; Zakon o zaštiti potrošača
  čl. 7, NN 59/2026). Uz maloprodajnu cijenu se ističe cijena istog proizvoda
  na referentni dan: za hranu, piće, kozmetiku, sredstva za čišćenje i kućanstvo
  2. 5. 2025., za ostalu robu i usluge 10. 9. 2026. Proizvod uveden kasnije:
  cijena na prvi dan u ponudi. Na etiketi stoji npr. "Cijena na 2.5.2025.: 1,99 EUR".

- Cjenik u CSV ili XML obliku: obvezan samo ako trgovina ima web stranicu
  (Odluka o objavi cjenika, NN 101/2026), objava svaki radni dan do 8:00.

Sidrenu cijenu pamtimo u artikli.sidrena (centi) i artikli.sidrena_datum.
Popunjava se sama, samo za artikle koji je još nemaju (nikad se ne prepisuje):
iz povijesti računa u Robi (najčešća cijena na zadnji dan prodaje do
referentnog dana, inače na prvi dan prodaje), a za artikl bez prodaje
trenutna MPC i današnji datum (dan kad je artikl ušao u ponudu).
"""

import csv
import datetime as dt
import io
from collections import Counter

# Za trgovinu voćem i povrćem sve je "hrana", pa vrijedi 2. 5. 2025.
REFERENTNI_DAN = "2025-05-02"


def _cijena_dana(con, roba_sifra, datum):
    """Najčešća jedinična MPC na računima iz Robe za taj dan (centi)."""
    c = Counter()
    for r in con.execute(
            """SELECT kolicina, iznos, cijena FROM roba_stavke
               WHERE status IN (2, 3) AND artikl = ? AND datum = ? AND kolicina > 0""",
            (roba_sifra, datum)):
        # stara baza bez stupca cijena (prije prvog novog uvoza): iz iznosa
        c[r["cijena"] or int(round(r["iznos"] / r["kolicina"]))] += 1
    return c.most_common(1)[0][0] if c else None


def predlozi(con, a):
    """(centi, 'YYYY-MM-DD') za jedan artikl."""
    if a["roba_sifra"] is not None:
        r = con.execute(
            """SELECT MAX(datum) FROM roba_stavke WHERE status IN (2, 3) AND artikl = ?
               AND datum <= ? AND kolicina > 0""", (a["roba_sifra"], REFERENTNI_DAN)).fetchone()
        datum = r[0] or con.execute(
            """SELECT MIN(datum) FROM roba_stavke WHERE status IN (2, 3) AND artikl = ?
               AND kolicina > 0""", (a["roba_sifra"],)).fetchone()[0]
        if datum:
            c = _cijena_dana(con, a["roba_sifra"], datum)
            if c:
                # proizvod koji je bio u ponudi na referentni dan nosi taj datum
                return c, (REFERENTNI_DAN if datum <= REFERENTNI_DAN else datum)
    return a["cijena"], dt.date.today().isoformat()


def najniza_30(con, aid, s_trenutnom=False):
    """Prijedlog za akciju: najniža cijena u zadnjih 30 dana (centi), iz računa u Robi.
    Za artikl "X AKCIJA" gleda se redovni artikl "X". 0 = nema podataka.
    30 dana se broji do zadnjeg računa u Robi (ne do zadnje prodaje artikla), jer
    stara prodaja ne govori ništa o cijeni u zadnjih 30 dana.
    s_trenutnom: prije početka akcije uračunava se i sadašnja MPC (i ona se
    primjenjuje), pa najniža nikad nije veća od cijene na polici."""
    a = con.execute("SELECT roba_sifra, naziv, cijena FROM artikli WHERE id = ?", (aid,)).fetchone()
    if not a:
        return 0
    sifra, naziv = a["roba_sifra"], " ".join(a["naziv"].split())
    if naziv.upper().endswith(" AKCIJA"):
        b = con.execute("SELECT roba_sifra FROM artikli WHERE upper(trim(naziv)) = ? AND aktivan = 1",
                        (naziv[:-7].strip().upper(),)).fetchone()
        if b:
            sifra = b["roba_sifra"]
        else:
            return 0
    if sifra is None:
        return a["cijena"] if s_trenutnom else 0
    zadnji = con.execute("SELECT MAX(datum) FROM roba_dokumenti WHERE status IN (2, 3)").fetchone()[0]
    if not zadnji:
        return a["cijena"] if s_trenutnom else 0
    od = (dt.date.fromisoformat(zadnji) - dt.timedelta(days=29)).isoformat()
    dani = [r[0] for r in con.execute(
        """SELECT DISTINCT datum FROM roba_stavke WHERE artikl = ? AND status IN (2, 3)
           AND datum BETWEEN ? AND ?""", (sifra, od, zadnji))]
    cijene = [c for c in (_cijena_dana(con, sifra, d) for d in dani) if c]
    import postavke
    if postavke.ukljucen(con, "zpc"):
        import roba_dodatno  # točni datumi promjena cijena iz Robinih zapisnika
        cijene += roba_dodatno.cijene_u_razdoblju(
            con, sifra, od, max(zadnji, dt.date.today().isoformat()))
    if s_trenutnom and a["cijena"] > 0:
        cijene.append(a["cijena"])
    return min(cijene) if cijene else 0


def popuni(con):
    """Upiše sidrenu cijenu artiklima koji je nemaju. Vraća broj upisanih."""
    n = 0
    for a in con.execute("SELECT id, roba_sifra, cijena FROM artikli "
                         "WHERE sidrena_datum = '' AND usluga = 0").fetchall():
        c, d = predlozi(con, a)
        con.execute("UPDATE artikli SET sidrena = ?, sidrena_datum = ? WHERE id = ?", (c, d, a["id"]))
        n += 1
    return n


def datum_hr(iso):
    """'2025-05-02' -> '2.5.2025.'"""
    try:
        d = dt.date.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return "%d.%d.%d." % (d.day, d.month, d.year)


def _eur(c):
    return ("%.2f" % (c / 100.0)).replace(".", ",")


def cijena_za_jedinicu(a):
    """Cijena za kg ili l pakirane robe koja se prodaje na komad (centi), 0 ako nije zadana."""
    if a.get("neto_jm") in ("kg", "l") and a.get("neto_kolicina"):
        return int(a["cijena"] / a["neto_kolicina"] + 0.5 + 1e-7)
    return 0


def cjenik_csv(artikli, naziv=""):
    """Cjenik za web stranicu (Odluka o objavi cjenika, NN 101/2026): CSV, ';', UTF-8."""
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    w.writerow(["naziv proizvoda", "šifra proizvoda", "marka proizvoda", "neto količina",
                "jedinica mjere", "maloprodajna cijena", "cijena za jedinicu mjere",
                "MPC za vrijeme posebnog oblika prodaje", "najniža cijena u posljednjih 30 dana",
                "sidrena cijena", "datum sidrene cijene", "barkod", "raspoloživost"])
    for a in artikli:
        if a["usluga"]:
            continue
        jm = a["jm"].lower()
        po_kg = jm in ("kg", "l")
        pak = a.get("neto_jm") in ("kg", "l") and a.get("neto_kolicina")
        neto = (("%g " % a["neto_kolicina"]).replace(".", ",") + a["neto_jm"]) if pak else \
            ("1 " + a["jm"] if not po_kg else "")
        za_jed = (_eur(cijena_za_jedinicu(a)) + " EUR/" + a["neto_jm"]) if pak else \
            (_eur(a["cijena"]) if po_kg else "")
        akc = bool(a.get("akcija_prije")) and a["cijena"] < a["akcija_prije"]
        w.writerow([a["naziv"], a["sifra"], "", neto,
                    a["jm"], _eur(a["cijena"]), za_jed,
                    _eur(a["cijena"]) if akc else "", _eur(a["akcija_prije"]) if akc else "", _eur(a["sidrena"]) if a["sidrena_datum"] else "",
                    datum_hr(a["sidrena_datum"]), a.get("barkod") or "",
                    "dostupno" if a["zaliha"] > 0 else "nedostupno"])
    return "﻿" + out.getvalue()
