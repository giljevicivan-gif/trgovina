# -*- coding: utf-8 -*-
"""
Usporedba nabavnih cijena po dobavljačima.

Za svaki artikl koji je u razdoblju dolazio od više dobavljača: zadnja,
prosječna i najniža jedinična nabavna cijena bez PDV-a kod svakoga.
Izvori: primke iz Robe (prije nego što smo ih počeli voditi ovdje) i naše
primke (bez storniranih). Cijena nije sve: kvaliteta, klasa i kalo se
razlikuju, a cijene voća i povrća se mijenjaju sa sezonom (zato datum).
"""

import datetime as dt

from nabava import Greska


def _ime(s):
    return " ".join(str(s or "").split()).upper()


def usporedba(con, dana=90, sve=False):
    try:
        dana = int(dana)
    except (TypeError, ValueError):
        raise Greska("Neispravan broj dana.")
    if not 7 <= dana <= 730:
        raise Greska("Razdoblje mora biti 7 - 730 dana.")
    zadnji = max(filter(None, [
        con.execute("SELECT MAX(datum) FROM roba_dokumenti WHERE status = 1 AND brisan = 0").fetchone()[0],
        con.execute("SELECT MAX(datum) FROM dokumenti WHERE vrsta = 'primka'").fetchone()[0]]), default=None)
    if not zadnji:
        return {"od": None, "do": None, "dana": dana, "artikli": [], "dobavljaci": []}
    od = (dt.date.fromisoformat(zadnji) - dt.timedelta(days=dana - 1)).isoformat()

    redovi = []  # (artikl_id, naziv, jm, dobavljač, datum, količina, jedinična cijena u centima)
    for r in con.execute(
            """SELECT a.id, a.naziv, a.jm, d.partner, s.datum, s.kolicina, s.nabavna
               FROM roba_stavke s
               JOIN roba_dokumenti d ON d.godina = s.godina AND d.sifra = s.link
               JOIN artikli a ON a.roba_sifra = s.artikl
               WHERE s.status = 1 AND d.brisan = 0 AND s.kolicina > 0 AND s.nabavna > 0
                 AND s.datum BETWEEN ? AND ?""", (od, zadnji)):
        redovi.append((r[0], r[1], r[2], r[3], r[4], r[5], r[6] / r[5]))
    for r in con.execute(
            """SELECT a.id, a.naziv, a.jm, p.naziv, d.datum, s.kolicina, s.cijena
               FROM dok_stavke s JOIN dokumenti d ON d.id = s.dokument_id
               JOIN artikli a ON a.id = s.artikl_id
               LEFT JOIN partneri p ON p.id = d.partner_id
               WHERE d.vrsta = 'primka' AND d.storno_od IS NULL AND d.storniran = 0
                 AND s.kolicina > 0 AND s.cijena > 0 AND d.datum BETWEEN ? AND ?""", (od, zadnji)):
        redovi.append(tuple(r))

    art = {}
    imena = {}
    for aid, naziv, jm, dob, datum, kol, cij in redovi:
        if not dob:
            continue
        k = _ime(dob)
        imena.setdefault(k, " ".join(dob.split()))
        a = art.setdefault(aid, {"id": aid, "naziv": naziv, "jm": jm, "dob": {}})
        d = a["dob"].setdefault(k, {"dobavljac": imena[k], "kolicina": 0.0, "vrijednost": 0.0,
                                    "najniza": cij, "zadnja": cij, "datum": datum, "isporuka": 0})
        d["kolicina"] += kol
        d["vrijednost"] += kol * cij
        d["najniza"] = min(d["najniza"], cij)
        d["isporuka"] += 1
        if datum >= d["datum"]:
            d["zadnja"], d["datum"] = cij, datum

    out, pobjede = [], {}
    for a in art.values():
        dob = list(a.pop("dob").values())
        if len(dob) < 2 and not sve:
            continue
        for d in dob:
            d["prosjek"] = int(round(d["vrijednost"] / d["kolicina"]))
            d["zadnja"] = int(round(d["zadnja"]))
            d["najniza"] = int(round(d["najniza"]))
            d["kolicina"] = round(d["kolicina"], 3)
            del d["vrijednost"]
        # uspoređujemo prosjek u razdoblju; jedna povoljna isporuka ne čini dobavljača jeftinijim
        dob.sort(key=lambda d: d["prosjek"])
        naj = dob[0]["prosjek"]
        for d in dob:
            d["razlika"] = round((d["prosjek"] - naj) * 100.0 / naj, 1) if naj else 0
        # koliko bi se uštedjelo da je sve kupljeno po prosjeku najjeftinijeg
        a["usteda"] = int(round(sum(max(0, d["prosjek"] - naj) * d["kolicina"] for d in dob)))
        a["dobavljaci"] = dob
        out.append(a)
        if len(dob) > 1:
            z = pobjede.setdefault(dob[0]["dobavljac"], 0)
            pobjede[dob[0]["dobavljac"]] = z + 1
    out.sort(key=lambda a: (-a["usteda"], a["naziv"]))
    return {"od": od, "do": zadnji, "dana": dana, "artikli": out,
            "dobavljaci": sorted(({"dobavljac": k, "najjeftiniji": v} for k, v in pobjede.items()),
                                 key=lambda x: -x["najjeftiniji"])}
