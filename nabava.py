# -*- coding: utf-8 -*-
"""
Nabava i zaliha: primke, otpisi (kalo, kvar), povrati dobavljaču i inventura.

Pravilo: od kad se primke vode ovdje, upisuju se SAMO ovdje (ne i u Robu),
inače se zaliha udvostruči. Prodaja se i dalje broji iz Robe.
"""

import datetime as dt

import math

from baza import ZALIHA_SQL, centi, oib_ispravan, pdv_na_osnovicu, vrijednosti, zaokruzi

OZNAKE = {"primka": "P", "otpis": "O", "inventura": "I", "povrat": "V"}
S_DOBAVLJACEM = ("primka", "povrat")


class Greska(Exception):
    pass


def sada():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def oznaka(d):
    return "%s-%d-%d" % (OZNAKE[d["vrsta"]], d["godina"], d["broj"])


# ---------------------------------------------------------------- Čitanje

def zaliha(con):
    """Svi aktivni artikli sa stvarnom zalihom, nabavnom i zadnjom nabavnom cijenom."""
    rows = con.execute(
        """SELECT a.id, a.sifra, a.naziv, a.jm, a.cijena, a.pdv_stopa, a.usluga, a.nabavna,
                  a.min_zaliha, a.podrijetlo, a.klasa, a.barkod, a.sidrena, a.sidrena_datum, a.akcija_prije,
                  a.neto_kolicina, a.neto_jm, round(%s, 3) AS zaliha,
                  (SELECT s.cijena FROM dok_stavke s JOIN dokumenti d ON d.id = s.dokument_id
                   WHERE s.artikl_id = a.id AND d.vrsta = 'primka' AND s.kolicina > 0
                     AND d.storniran = 0
                   ORDER BY d.datum DESC, d.id DESC LIMIT 1) AS zadnja_nasa,
                  (SELECT s.nabavna * 1.0 / NULLIF(s.kolicina, 0) FROM roba_stavke s
                   WHERE s.artikl = a.roba_sifra AND s.status = 1 AND s.kolicina > 0
                   ORDER BY s.datum DESC, s.sifra DESC LIMIT 1) AS zadnja_roba
           FROM artikli a WHERE a.aktivan = 1 ORDER BY a.naziv""" % ZALIHA_SQL).fetchall()
    zadnje = {}  # artikl -> (cijena s računa, rabat) sa zadnje naše primke
    for s in con.execute(
            """SELECT s.artikl_id, s.fakturna, s.rabat FROM dok_stavke s
               JOIN dokumenti d ON d.id = s.dokument_id
               WHERE d.vrsta = 'primka' AND s.kolicina > 0 AND d.storniran = 0
               ORDER BY d.datum, d.id"""):
        zadnje[s["artikl_id"]] = (s["fakturna"], s["rabat"])
    out = []
    for r in rows:
        d = dict(r)
        z = d.pop("zadnja_nasa")
        zr = d.pop("zadnja_roba")
        d["zadnja_nabavna"] = z if z is not None else (zaokruzi(zr) if zr else d["nabavna"])
        f = zadnje.get(d["id"])
        d["zadnja_fakturna"], d["zadnji_rabat"] = f if f and f[0] else (d["zadnja_nabavna"], 0)
        d.update(vrijednosti(d["zaliha"], d["nabavna"], d["cijena"], d["pdv_stopa"]))
        out.append(d)
    return out


def partneri(con, svi=False):
    sql = "SELECT * FROM partneri" + ("" if svi else " WHERE aktivan = 1") + " ORDER BY naziv"
    return [dict(r) for r in con.execute(sql)]


def spremi_partner(con, p):
    """Novi ili izmijenjeni dobavljač. Vraća id."""
    naziv = " ".join(str(p.get("naziv") or "").split())[:120]
    if not naziv:
        raise Greska("Upišite naziv.")
    oib = str(p.get("oib") or "").strip()
    if oib and not oib_ispravan(oib):
        raise Greska("OIB nije ispravan (11 znamenki, kontrolna znamenka se ne slaže).")
    polja = {k: str(p.get(k) or "").strip()[:200]
             for k in ("adresa", "mjesto", "telefon", "email", "napomena")}
    aktivan = 0 if p.get("aktivan") in (0, False, "0") else 1
    dupli = con.execute("SELECT id FROM partneri WHERE oib = ? AND oib != '' AND id != ?",
                        (oib, int(p.get("id") or 0))).fetchone()
    if dupli:
        raise Greska("Dobavljač s tim OIB-om već postoji.")
    with con:
        if p.get("id"):
            pid = int(p["id"])
            if not con.execute("SELECT 1 FROM partneri WHERE id = ?", (pid,)).fetchone():
                raise Greska("Dobavljač ne postoji.")
            con.execute(
                """UPDATE partneri SET naziv = ?, oib = ?, adresa = ?, mjesto = ?, telefon = ?,
                   email = ?, napomena = ?, aktivan = ?, uredjen = 1 WHERE id = ?""",
                (naziv, oib, polja["adresa"], polja["mjesto"], polja["telefon"], polja["email"],
                 polja["napomena"], aktivan, pid))
            return pid
        return con.execute(
            """INSERT INTO partneri (naziv, oib, adresa, mjesto, telefon, email, napomena,
                                     aktivan, uredjen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (naziv, oib, polja["adresa"], polja["mjesto"], polja["telefon"], polja["email"],
             polja["napomena"], aktivan)).lastrowid


def predlozena_mpc(nabavna, stopa, marza):
    """MPC (centi) uz željenu maržu na nabavnu bez PDV-a, zaokružena na ,x9."""
    if not (marza > 0 and nabavna > 0):
        return 0
    p = nabavna * (1 + marza / 100.0) * (1 + stopa / 100.0)
    return int(math.ceil((p + 1) / 10.0 - 1e-9)) * 10 - 1  # 1e-9: float šum ne smije dići cijenu


def uredi_artikl(con, aid, p):
    """Ono što vodimo mi, a ne Roba: usluga (bez zalihe), minimalna zaliha,
    zemlja podrijetla, klasa i sidrena cijena (za etikete)."""
    try:
        min_z = round(float(p.get("min_zaliha") or 0), 3)
    except (TypeError, ValueError):
        raise Greska("Neispravna minimalna zaliha.")
    if min_z < 0:
        raise Greska("Minimalna zaliha ne može biti negativna.")
    if "sidrena" in p:
        try:
            sidrena = centi(float(str(p.get("sidrena") or "").replace(",", ".")))
            sid_datum = dt.date.fromisoformat(str(p.get("sidrena_datum") or "")).isoformat()
        except (TypeError, ValueError):
            raise Greska("Upišite sidrenu cijenu i njezin datum.")
        if sidrena <= 0:
            raise Greska("Sidrena cijena mora biti veća od nule.")
    pakiranje = None
    if "neto_jm" in p:
        nj = str(p.get("neto_jm") or "")
        if nj not in ("", "-", "kg", "l"):
            raise Greska("Jedinica pakiranja mora biti kg ili l.")
        try:
            nk = round(float(str(p.get("neto_kolicina") or 0).replace(",", ".")), 3) if nj in ("kg", "l") else 0
        except ValueError:
            raise Greska("Neispravna neto količina.")
        if nj in ("kg", "l") and not 0 < nk <= 1000:
            raise Greska("Upišite neto količinu pakiranja (npr. 0,2 kg ili 0,5 l).")
        pakiranje = (nk, nj)
    akcija = None
    if "akcija_prije" in p:
        try:
            v = str(p.get("akcija_prije") or "").strip().replace(",", ".")
            akcija = centi(float(v)) if v else 0
        except ValueError:
            raise Greska("Neispravna cijena prije akcije.")
        if akcija < 0:
            raise Greska("Neispravna cijena prije akcije.")
    with con:
        if akcija == 0:  # akcija je gotova
            con.execute("UPDATE artikli SET akcija_prije = 0, akcija_cijena = 0, akcija_od = '' WHERE id = ?",
                        (aid,))
        elif akcija is not None:
            con.execute("UPDATE artikli SET akcija_prije = ? WHERE id = ?", (akcija, aid))
        if "sidrena" in p:
            con.execute("UPDATE artikli SET sidrena = ?, sidrena_datum = ?, sidrena_izvor = 'rucno' WHERE id = ?",
                        (sidrena, sid_datum, aid))
        if pakiranje:
            con.execute("UPDATE artikli SET neto_kolicina = ?, neto_jm = ? WHERE id = ?", pakiranje + (aid,))
        n = con.execute(
            "UPDATE artikli SET usluga = ?, min_zaliha = ?, podrijetlo = ?, klasa = ? WHERE id = ?",
            (1 if p.get("usluga") else 0, min_z, str(p.get("podrijetlo") or "").strip()[:40],
             str(p.get("klasa") or "").strip()[:20], aid)).rowcount
    if not n:
        raise Greska("Artikl ne postoji.")


def prijedlog(con, dana_prodaje=28, za_dana=3):
    """Prijedlog narudžbe: prosječna dnevna prodaja (iz Robe) u zadnjih N dana,
    i koliko treba naručiti da zaliha traje za_dana dana plus minimalna zaliha.
    Grupirano po zadnjem dobavljaču artikla."""
    dana_prodaje = max(1, min(int(dana_prodaje), 365))
    za_dana = max(0.5, min(float(za_dana), 60))
    # razdoblje završava zadnjim danom za koji imamo račune iz Robe
    zadnji = con.execute(
        "SELECT MAX(datum) FROM roba_dokumenti WHERE status IN (2, 3)").fetchone()[0]
    do = zadnji or dt.date.today().isoformat()
    od = (dt.date.fromisoformat(do) - dt.timedelta(days=dana_prodaje - 1)).isoformat()
    prodaja = {r[0]: r[1] for r in con.execute(
        """SELECT artikl, SUM(kolicina) FROM roba_stavke
           WHERE status IN (2, 3) AND datum BETWEEN ? AND ? GROUP BY artikl""", (od, do))}
    # zadnji dobavljač: naša primka, inače Robina
    dob = {}
    for r in con.execute(
            """SELECT a.id, d.partner FROM roba_stavke s JOIN artikli a ON a.roba_sifra = s.artikl
               JOIN roba_dokumenti d ON d.godina = s.godina AND d.sifra = s.link
               WHERE s.status = 1 AND d.brisan = 0 ORDER BY s.datum, s.sifra"""):
        dob[r[0]] = " ".join(r[1].split())
    for r in con.execute(
            """SELECT s.artikl_id, p.naziv FROM dok_stavke s JOIN dokumenti d ON d.id = s.dokument_id
               JOIN partneri p ON p.id = d.partner_id
               WHERE d.vrsta = 'primka' AND d.storniran = 0 AND d.storno_od IS NULL
               ORDER BY d.datum, d.id"""):
        dob[r[0]] = r[1]
    dobid = {" ".join(r["naziv"].split()): r["id"] for r in con.execute("SELECT id, naziv FROM partneri")}
    # otvorene narudžbe kupaca s rokom u razdoblju za koje se naručuje
    import narudzbe
    import postavke
    rez = narudzbe.rezervirano(
        con, (dt.date.today() + dt.timedelta(days=math.ceil(za_dana))).isoformat()) \
        if postavke.ukljucen(con, "narudzbe") else {}
    stavke = []
    for a in zaliha(con):
        if a["usluga"]:
            continue
        roba_sifra = con.execute("SELECT roba_sifra FROM artikli WHERE id = ?",
                                 (a["id"],)).fetchone()[0]
        dnevno = (prodaja.get(roba_sifra) or 0) / float(dana_prodaje)
        rezerv = rez.get(a["id"], 0)
        potrebno = dnevno * za_dana + a["min_zaliha"] + rezerv - max(a["zaliha"], 0)
        if potrebno <= 0 or (dnevno == 0 and a["min_zaliha"] == 0 and rezerv == 0):
            continue
        kom = a["jm"].lower() not in ("kg", "l", "m", "m2", "m3")
        kolicina = math.ceil(potrebno) if kom else math.ceil(potrebno * 10) / 10.0
        stavke.append({
            "id": a["id"], "sifra": a["sifra"], "naziv": a["naziv"], "jm": a["jm"],
            "zaliha": a["zaliha"], "min_zaliha": a["min_zaliha"], "dnevno": round(dnevno, 3),
            "narudzbe": round(rezerv, 3),
            "dana_zalihe": round(a["zaliha"] / dnevno, 1) if dnevno > 0 else None,
            "kolicina": kolicina, "zadnja_nabavna": a["zadnja_nabavna"],
            "zadnja_fakturna": a["zadnja_fakturna"], "zadnji_rabat": a["zadnji_rabat"],
            "dobavljac": dob.get(a["id"], ""), "partner_id": dobid.get(dob.get(a["id"], "")),
        })
    stavke.sort(key=lambda s: (s["dobavljac"] == "", s["dobavljac"], s["naziv"]))
    return {"od": od, "do": do, "dana_prodaje": dana_prodaje, "za_dana": za_dana, "stavke": stavke}


def dokumenti(con, mjesec, vrsta=None):
    sql = """SELECT d.*, p.naziv AS partner,
                    (SELECT COUNT(*) FROM dok_stavke s WHERE s.dokument_id = d.id) AS stavki
             FROM dokumenti d LEFT JOIN partneri p ON p.id = d.partner_id
             WHERE substr(d.datum, 1, 7) = ?"""
    args = [mjesec]
    if vrsta:
        sql += " AND d.vrsta = ?"
        args.append(vrsta)
    out = []
    for r in con.execute(sql + " ORDER BY d.datum DESC, d.id DESC", args):
        d = dict(r)
        d["oznaka"] = oznaka(r)
        out.append(d)
    return out


def dokument(con, did):
    r = con.execute(
        """SELECT d.*, p.naziv AS partner, p.oib AS partner_oib, o.vrsta AS o_vrsta,
                  o.godina AS o_godina, o.broj AS o_broj
           FROM dokumenti d LEFT JOIN partneri p ON p.id = d.partner_id
           LEFT JOIN dokumenti o ON o.id = d.storno_od WHERE d.id = ?""", (did,)).fetchone()
    if not r:
        raise Greska("Dokument ne postoji.")
    d = dict(r)
    d["oznaka"] = oznaka(r)
    d["o_oznaka"] = ("%s-%d-%d" % (OZNAKE[r["o_vrsta"]], r["o_godina"], r["o_broj"])
                     if r["storno_od"] else "")
    d["stavke"] = [dict(s) for s in con.execute(
        "SELECT * FROM dok_stavke WHERE dokument_id = ? ORDER BY id", (did,))]
    return d


# ---------------------------------------------------------------- Zapis

def _broj(con, vrsta, godina):
    return con.execute(
        "SELECT COALESCE(MAX(broj), 0) + 1 FROM dokumenti WHERE vrsta = ? AND godina = ?",
        (vrsta, godina)).fetchone()[0]


def _stanje(con, artikl_id):
    return con.execute("SELECT %s FROM artikli a WHERE a.id = ?" % ZALIHA_SQL,
                       (artikl_id,)).fetchone()[0]


def _datum(s):
    try:
        return dt.date.fromisoformat(str(s)).isoformat()
    except ValueError:
        raise Greska("Neispravan datum.")


def spremi(con, podaci):
    """Sprema primku, otpis, povrat ili inventuru. Vraća id dokumenta."""
    vrsta = podaci.get("vrsta")
    if vrsta not in OZNAKE:
        raise Greska("Nepoznata vrsta dokumenta.")
    stavke = podaci.get("stavke") or []
    if not stavke:
        raise Greska("Dokument nema stavki.")
    if vrsta in ("otpis", "povrat") and not str(podaci.get("napomena") or "").strip():
        # razlog je dio zapisnika (dokaz za knjigovodstvo i PDV)
        raise Greska("Upišite razlog (npr. pokvareno, isušeno, lom).")
    datum = _datum(podaci.get("datum") or dt.date.today().isoformat())
    if datum > dt.date.today().isoformat():
        raise Greska("Datum je u budućnosti.")
    if datum < (dt.date.today() - dt.timedelta(days=400)).isoformat():
        raise Greska("Datum je stariji od godine dana. Provjerite datum.")
    con.execute("BEGIN IMMEDIATE")
    try:
        partner_id = None
        if vrsta in S_DOBAVLJACEM:
            try:
                partner_id = int(podaci.get("partner_id"))
            except (TypeError, ValueError):
                raise Greska("Odaberite dobavljača.")
            if not con.execute("SELECT 1 FROM partneri WHERE id = ?", (partner_id,)).fetchone():
                raise Greska("Nepoznat dobavljač.")
        redovi = []
        for s in stavke:
            try:
                a = con.execute("SELECT * FROM artikli WHERE id = ?", (int(s["artikl_id"]),)).fetchone()
            except (TypeError, ValueError, KeyError):
                a = None
            if not a:
                raise Greska("Artikl ne postoji.")
            if a["usluga"]:
                raise Greska("%s je usluga i nema zalihu." % a["naziv"])
            try:
                kol = round(float(s["kolicina"]), 3)
            except (TypeError, ValueError, KeyError):
                raise Greska("Neispravna količina za %s." % a["naziv"])
            if not math.isfinite(kol) or abs(kol) > 1000000:
                raise Greska("Neispravna količina za %s." % a["naziv"])
            if vrsta == "inventura":
                if kol < 0:
                    raise Greska("Prebrojana količina ne može biti negativna (%s)." % a["naziv"])
                # upisano je stvarno (prebrojano) stanje; spremamo razliku
                kol = round(kol - _stanje(con, a["id"]), 3)
                if kol == 0:
                    continue
                cijena = a["nabavna"]
            else:
                if kol <= 0 or kol > 1000000:
                    raise Greska("Neispravna količina za %s." % a["naziv"])
                if vrsta == "primka":
                    try:
                        fakturna = float(s["cijena"])
                        rabat = float(s.get("rabat") or 0)
                    except (TypeError, ValueError, KeyError):
                        raise Greska("Upišite nabavnu cijenu za %s." % a["naziv"])
                    if not (math.isfinite(fakturna) and math.isfinite(rabat)):
                        raise Greska("Neispravna cijena za %s." % a["naziv"])
                    if fakturna < 0 or fakturna > 1000000:
                        raise Greska("Nabavna cijena mora biti između 0 i 1.000.000 EUR.")
                    if not 0 <= rabat < 100:
                        raise Greska("Rabat mora biti između 0 i 100 %.")
                    # nabavna = cijena s računa - rabat; isti izračun kao na ekranu (u centima)
                    fakt_c = centi(fakturna)
                    netto = fakt_c * (1 - rabat / 100.0)
                    redovi.append((a, kol, zaokruzi(netto), zaokruzi(kol * netto), fakt_c, rabat))
                    continue
                elif vrsta == "povrat":
                    # nabavna bez PDV-a po kojoj dobavljač odobrava; prazno = prosječna nabavna
                    try:
                        c = s.get("cijena")
                        cijena = a["nabavna"] if c in (None, "") else centi(float(c))
                    except (TypeError, ValueError, OverflowError):
                        raise Greska("Neispravna cijena za %s." % a["naziv"])
                    if cijena < 0:
                        raise Greska("Cijena ne može biti negativna.")
                else:
                    cijena = a["nabavna"]
            redovi.append((a, kol, cijena, zaokruzi(kol * cijena), cijena, 0.0))
        if not redovi:
            raise Greska("Nema razlika: prebrojano stanje jednako je stanju u programu.")

        neto = sum(r[3] for r in redovi)
        # PDV po stopi na zbroj osnovica (kao rekapitulacija na računu dobavljača i eRačun),
        # a ne po stavci, da se ne skupljaju razlike od po pola centa
        po_stopi = {}
        for r in redovi:
            po_stopi[r[0]["pdv_stopa"]] = po_stopi.get(r[0]["pdv_stopa"], 0) + r[3]
        pdv = (sum(pdv_na_osnovicu(v, k) for k, v in po_stopi.items())
               if vrsta in S_DOBAVLJACEM else 0)
        godina = int(datum[:4])
        cur = con.execute(
            """INSERT INTO dokumenti (vrsta, godina, broj, datum, vrijeme, partner_id,
                                      broj_dobavljaca, neto, pdv, napomena)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (vrsta, godina, _broj(con, vrsta, godina), datum, sada(), partner_id,
             str(podaci.get("broj_dobavljaca") or "").strip()[:60], neto, pdv,
             str(podaci.get("napomena") or "").strip()[:300]))
        did = cur.lastrowid
        for a, kol, cijena, iznos, fakturna, rabat in redovi:
            nabavna_prije = None
            if vrsta == "primka":
                nabavna_prije = con.execute("SELECT nabavna FROM artikli WHERE id = ?", (a["id"],)).fetchone()[0]
                # nova prosječna nabavna (ponderirana sa zalihom prije ulaza)
                prije = _stanje(con, a["id"])
                if prije > 0 and nabavna_prije > 0:
                    nova = zaokruzi((prije * nabavna_prije + kol * cijena) / (prije + kol))
                else:
                    nova = cijena
                con.execute("UPDATE artikli SET nabavna = ?, nabavna_nasa = 1 WHERE id = ?",
                            (nova, a["id"]))
            con.execute(
                """INSERT INTO dok_stavke (dokument_id, artikl_id, naziv, jm, kolicina, cijena,
                                           iznos, pdv_stopa, mpc, fakturna, rabat, nabavna_prije)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (did, a["id"], a["naziv"], a["jm"], kol, cijena, iznos, a["pdv_stopa"],
                 a["cijena"], fakturna, rabat, nabavna_prije))
        con.execute("COMMIT")
        return did
    except Exception:
        con.execute("ROLLBACK")
        raise


def _vrati_nabavnu(con, did):
    """Storno primke: prosječna nabavna se vraća kao da primke nije bilo
    (izračun unatrag iz zalihe; ako ne ide, nabavna zapamćena prije primke)."""
    for s in con.execute("SELECT * FROM dok_stavke WHERE dokument_id = ?", (did,)).fetchall():
        a = con.execute("SELECT nabavna FROM artikli WHERE id = ?", (s["artikl_id"],)).fetchone()
        if not a:
            continue
        stanje = _stanje(con, s["artikl_id"])
        ostaje = stanje - s["kolicina"]
        nova = None
        if ostaje > 0.0005:
            v = (stanje * a["nabavna"] - s["kolicina"] * s["cijena"]) / ostaje
            if v > 0:
                nova = zaokruzi(v)
        if nova is None:
            nova = s["nabavna_prije"]
        if nova is not None and nova > 0:
            con.execute("UPDATE artikli SET nabavna = ? WHERE id = ?", (nova, s["artikl_id"]))


def storniraj(con, did):
    con.execute("BEGIN IMMEDIATE")
    try:
        d = con.execute("SELECT * FROM dokumenti WHERE id = ?", (did,)).fetchone()
        if not d:
            raise Greska("Dokument ne postoji.")
        if d["storniran"]:
            raise Greska("Dokument je već storniran.")
        if d["storno_od"]:
            raise Greska("Storno se ne može stornirati.")
        if con.execute("SELECT name FROM sqlite_master WHERE name = 'dob_placanja'").fetchone() and \
                con.execute("SELECT 1 FROM dob_placanja WHERE dokument_id = ? AND ponisteno = ''", (did,)).fetchone():
            raise Greska("Primka je (djelomično) plaćena. Prvo poništite plaćanja (Nabava, Dugovanja), pa stornirajte.")
        if d["vrsta"] == "primka":
            _vrati_nabavnu(con, did)
        danas = dt.date.today().isoformat()
        godina = int(danas[:4])
        cur = con.execute(
            """INSERT INTO dokumenti (vrsta, godina, broj, datum, vrijeme, partner_id,
                                      broj_dobavljaca, neto, pdv, storno_od, napomena)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (d["vrsta"], godina, _broj(con, d["vrsta"], godina), danas, sada(), d["partner_id"],
             d["broj_dobavljaca"], -d["neto"], -d["pdv"], did, "Storno " + oznaka(d)))
        novi = cur.lastrowid
        con.execute(
            """INSERT INTO dok_stavke (dokument_id, artikl_id, naziv, jm, kolicina, cijena,
                                       iznos, pdv_stopa, mpc, fakturna, rabat)
               SELECT ?, artikl_id, naziv, jm, -kolicina, cijena, -iznos, pdv_stopa, mpc,
                      fakturna, rabat
               FROM dok_stavke WHERE dokument_id = ? ORDER BY id""", (novi, did))
        con.execute("UPDATE dokumenti SET storniran = 1 WHERE id = ?", (did,))
        con.execute("COMMIT")
        return novi
    except Exception:
        con.execute("ROLLBACK")
        raise
