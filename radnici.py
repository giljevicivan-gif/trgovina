# -*- coding: utf-8 -*-
"""
Radnici s PIN-om (modul "radnici").

Kad je modul uključen, program se otvara tek kad radnik odabere svoje ime i upiše PIN.
Vlasnik vidi i smije sve. Radniku se daju ovlasti:
    nabava     primke, otpisi, povrati, dobavljači, dugovanja
    zaliha     kartica Zaliha (artikli, akcije, inventura, etikete)
    cijene     nabavne cijene, marža i zarada (bez toga se iz odgovora brišu)
    izvjestaji Izvještaji, Kontrola i paket za knjigovođu
    storno     storno računa na kasi za vježbu
    postavke   Postavke, radnici i e-mail
Bez ovlasti radnik i dalje radi kasu, račune, blagajnu, narudžbe, HACCP, sniženja i gajbe.

Svaka promjena (POST) upisuje se u dnevnik radnji: tko, kada, što.
PIN se ne sprema, nego samo njegov sažetak (PBKDF2), a nakon 5 krivih PIN-ova
radnik čeka minutu. Prijava traje dok se ne odjavi ili dok se program ne zatvori.
"""

import datetime as dt
import re
import hashlib
import secrets
import threading
import time

import postavke
from baza import oib_ispravan
from nabava import Greska

OVLASTI = (
    ("nabava", "Nabava (primke, otpisi, dobavljači)"),
    ("zaliha", "Zaliha (artikli, akcije, inventura)"),
    ("cijene", "Vidi nabavne cijene i maržu"),
    ("izvjestaji", "Izvještaji i Kontrola"),
    ("storno", "Storno računa"),
    ("postavke", "Postavke i radnici"),
)
SVE = [k for k, _ in OVLASTI]

# Putanje koje traže ovlast (početak putanje). GET i POST.
PUTANJE = (
    ("/api/dokument", "nabava"), ("/api/storno_dok/", "nabava"), ("/api/partner", "nabava"),
    ("/api/prijedlog", "nabava"), ("/api/usporedba", "nabava"), ("/api/dugovanja", "nabava"),
    ("/api/zaliha", "zaliha"), ("/api/artikl/", "zaliha"), ("/api/akcije", "zaliha"), ("/api/inv", "zaliha"),
    ("/api/rokovi", "zaliha"), ("/api/kalo", "zaliha"), ("/api/zpc", "zaliha"), ("/ispis_popisna", "zaliha"),
    ("/api/izvjestaj", "izvjestaji"), ("/api/kontrola", "izvjestaji"), ("/ispis_knjigovodja", "izvjestaji"),
    ("/knjigovodja_", "izvjestaji"),
    ("/ispis_dok/", "cijene"), ("/ispis_zaliha", "cijene"), ("/ispis_knjigovodja", "cijene"),
    ("/knjigovodja_", "cijene"), ("/api/izvjestaj", "cijene"), ("/api/kontrola", "cijene"),
    ("/api/usporedba", "cijene"), ("/api/lanci", "cijene"),
    ("/api/storno/", "storno"), ("/ispis_popisna", "cijene"),
    ("/api/eracun", "nabava"), ("/api/kpd", "zaliha"),
    ("/api/eposrednik", "nabava"), ("/eposrednik_xml", "nabava"), ("/api/eposrednik/postavke", "postavke"),
    ("/api/eposrednik/certifikat", "postavke"), ("/api/eposrednik/provjeri", "postavke"),
    ("/api/eposrednik/izlazni", "postavke"),
    ("/api/postavke", "postavke"), ("/api/fiskal", "postavke"), ("/api/radnici", "postavke"), ("/api/sazetak", "postavke"),
    ("/api/povrat_amb/vrsta", "postavke"), ("/api/zaslon/mreza", "postavke"),
    ("/api/vaga/", "postavke"),
    ("/api/tisak/postavke", "postavke"), ("/api/tisak/pisaci", "postavke"), ("/api/tisak/probni", "postavke"),
    ("/api/tisak/otvori_ladicu", "postavke"), ("/api/tisak/tekst", "postavke"),
    ("/api/uplata", "postavke"), ("/api/dob_mail", "nabava"), ("/ispis_narudzba_dob", "nabava"),
    ("/api/kopija_van", "postavke"), ("/api/donacij", "nabava"), ("/ispis_donacija", "nabava"),
    ("/api/uvoz_artikala", "postavke"),
)
# GET koji radnik bez ovlasti ipak smije (Zaliha i Nabava ih koriste i drugdje)
GET_SLOBODNO = ("/api/postavke", "/api/zaliha", "/api/partneri", "/api/kontrola")

# Ključevi s nabavnim cijenama i zaradom; radniku bez ovlasti "cijene" brišu se iz JSON-a.
SKRIVENO = {"nabavna", "nabavna_pdv", "nabavna_nasa", "zadnja_nabavna", "zadnja_fakturna", "zadnji_rabat",
            "fakturna", "marza", "zarada", "prosjek", "neto_s_nabavnom", "nab_netto", "nab_pdv",
            "spaseno_nabavna", "ispod_nabavne", "vrijednost", "vrijednost_razlike", "nabava", "gubitak"}

SHEMA = """
CREATE TABLE IF NOT EXISTS radnici (
    id       INTEGER PRIMARY KEY,
    ime      TEXT NOT NULL,
    pin      TEXT NOT NULL,              -- sol$sažetak (PBKDF2-SHA256)
    vlasnik  INTEGER NOT NULL DEFAULT 0,
    ovlasti  TEXT NOT NULL DEFAULT '',   -- zarezom odvojeno, vidi OVLASTI
    aktivan  INTEGER NOT NULL DEFAULT 1,
    dodan    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS radnje (
    id         INTEGER PRIMARY KEY,
    vrijeme    TEXT NOT NULL,
    radnik_id  INTEGER,
    radnik     TEXT NOT NULL,
    putanja    TEXT NOT NULL,
    opis       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_radnje_vrijeme ON radnje(vrijeme);
"""

KOLACIC = "trgovina_radnik"
_prijave = {}      # token -> (radnik_id, zadnja aktivnost)
_krivi = {}        # radnik_id -> (broj krivih, vrijeme zadnjeg)
_lock = threading.Lock()


def sada():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def pripremi(con):
    con.executescript(SHEMA)
    if "oib" not in {r[1] for r in con.execute("PRAGMA table_info(radnici)")}:
        # OIB radnika ide na fiskalni račun kao OIB operatera (inače OIB iz postavki fiskalizacije)
        con.execute("ALTER TABLE radnici ADD COLUMN oib TEXT NOT NULL DEFAULT ''")
        con.commit()


def _sazetak(pin, sol=None):
    sol = sol or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), bytes.fromhex(sol), 100000).hex()
    return "%s$%s" % (sol, h)


def _provjeri_pin(pin, spremljeno):
    sol, _ = spremljeno.split("$", 1)
    return secrets.compare_digest(_sazetak(pin, sol), spremljeno)


def _pin(p):
    pin = str(p.get("pin") or "").strip()
    if not pin.isdigit() or not 4 <= len(pin) <= 8:
        raise Greska("PIN mora imati 4 do 8 brojki.")
    return pin


def _ovlasti(r):
    return SVE[:] if r["vlasnik"] else [o for o in (r["ovlasti"] or "").split(",") if o in SVE]


def _javno(r):
    return {"id": r["id"], "ime": r["ime"], "vlasnik": bool(r["vlasnik"]), "ovlasti": _ovlasti(r),
            "aktivan": bool(r["aktivan"])}


def popis(con, svi=False):
    sql = "SELECT * FROM radnici" + ("" if svi else " WHERE aktivan = 1") + " ORDER BY vlasnik DESC, ime"
    return [dict(_javno(r), oib=r["oib"]) if svi else _javno(r) for r in con.execute(sql)]


def ima_vlasnika(con):
    return bool(con.execute("SELECT 1 FROM radnici WHERE vlasnik = 1 AND aktivan = 1").fetchone())


def spremi(con, p):
    """Novi radnik ili izmjena (ime, PIN, ovlasti, aktivan). Prazan PIN kod izmjene ga ne mijenja."""
    rid = int(p.get("id") or 0)
    ime = " ".join(str(p.get("ime") or "").split())[:40]
    if not ime:
        raise Greska("Upišite ime radnika.")
    vlasnik = 1 if p.get("vlasnik") in (True, 1, "1", "true") else 0
    ovlasti = ",".join(o for o in (p.get("ovlasti") or []) if o in SVE)
    aktivan = 0 if p.get("aktivan") in (False, 0, "0", "false") else 1
    oib = re.sub(r"\s", "", str(p.get("oib") or ""))
    if oib and not oib_ispravan(oib):
        raise Greska("OIB radnika %s nije ispravan." % ime)
    with con:
        if rid:
            r = con.execute("SELECT * FROM radnici WHERE id = ?", (rid,)).fetchone()
            if not r:
                raise Greska("Radnik ne postoji.")
            if r["vlasnik"] and (not vlasnik or not aktivan) and postavke.ukljucen(con, "radnici") and \
                    con.execute("SELECT COUNT(*) FROM radnici WHERE vlasnik = 1 AND aktivan = 1").fetchone()[0] <= 1:
                raise Greska("Mora ostati barem jedan vlasnik (inače nitko ne može u Postavke).")
            con.execute("UPDATE radnici SET ime = ?, vlasnik = ?, ovlasti = ?, aktivan = ?, oib = ? WHERE id = ?",
                        (ime, vlasnik, ovlasti, aktivan, oib, rid))
            if str(p.get("pin") or "").strip():
                con.execute("UPDATE radnici SET pin = ? WHERE id = ?", (_sazetak(_pin(p)), rid))
            if not aktivan:
                odjavi_radnika(rid)
        else:
            rid = con.execute("INSERT INTO radnici (ime, pin, vlasnik, ovlasti, dodan, oib) VALUES (?, ?, ?, ?, ?, ?)",
                              (ime, _sazetak(_pin(p)), vlasnik, ovlasti, sada(), oib)).lastrowid
    return rid


def prijavi(con, p):
    """Vraća token prijave ili diže Grešku."""
    try:
        rid = int(p.get("id"))
    except (TypeError, ValueError):
        raise Greska("Odaberite radnika.")
    r = con.execute("SELECT * FROM radnici WHERE id = ? AND aktivan = 1", (rid,)).fetchone()
    if not r:
        raise Greska("Radnik ne postoji.")
    with _lock:
        n, kad = _krivi.get(rid, (0, 0))
        if n >= 5 and time.time() - kad < 60:
            raise Greska("Previše krivih PIN-ova. Pričekajte minutu.")
    if not _provjeri_pin(str(p.get("pin") or "").strip(), r["pin"]):
        with _lock:
            n, kad = _krivi.get(rid, (0, 0))
            _krivi[rid] = (1 if time.time() - kad > 600 else n + 1, time.time())
        raise Greska("Krivi PIN.")
    token = secrets.token_urlsafe(24)
    with _lock:
        _krivi.pop(rid, None)
        _prijave[token] = (rid, time.time())
    zapisi(con, dict(r), "/api/radnici/prijava", "Prijava")
    return token


def odjavi(token):
    with _lock:
        _prijave.pop(token, None)


def odjavi_radnika(rid):
    with _lock:
        for t in [t for t, v in _prijave.items() if v[0] == rid]:
            _prijave.pop(t, None)


def iz_kolacica(zaglavlje):
    for dio in (zaglavlje or "").split(";"):
        k, _, v = dio.strip().partition("=")
        if k == KOLACIC:
            return v
    return ""


def tko(con, token):
    """Prijavljeni radnik (dict) ili None. Pazi na automatsku odjavu nakon neaktivnosti."""
    if not token:
        return None
    try:
        minuta = float(postavke.vrijednost(con, "radnici.odjava_min", "0") or 0)
    except ValueError:
        minuta = 0
    with _lock:
        v = _prijave.get(token)
        if not v:
            return None
        if minuta > 0 and time.time() - v[1] > minuta * 60:
            _prijave.pop(token, None)
            return None
        _prijave[token] = (v[0], time.time())
    r = con.execute("SELECT * FROM radnici WHERE id = ? AND aktivan = 1", (v[0],)).fetchone()
    return dict(_javno(r), oib=r["oib"]) if r else None


def smije(radnik, ovlast):
    return radnik is None or radnik["vlasnik"] or ovlast in radnik["ovlasti"]


def potrebne_ovlasti(putanja, metoda):
    if metoda == "GET" and putanja in GET_SLOBODNO:
        return []
    return sorted({o for poc, o in PUTANJE if putanja.startswith(poc)})


def skrij(obj):
    """Briše nabavne cijene i zaradu iz odgovora (rekurzivno)."""
    if isinstance(obj, dict):
        return {k: skrij(v) for k, v in obj.items() if k not in SKRIVENO}
    if isinstance(obj, list):
        return [skrij(v) for v in obj]
    return obj


# ---------------------------------------------------------------- dnevnik radnji

OPISI = (
    ("/api/naplati", "Račun na kasi (vježba)"), ("/api/storno/", "Storno računa"),
    ("/api/dokument", "Dokument nabave (primka, otpis, povrat, inventura)"), ("/api/storno_dok/", "Storno dokumenta nabave"),
    ("/api/smjena/otvori", "Otvorena smjena"), ("/api/smjena/promet", "Polog/isplata u blagajni"),
    ("/api/smjena/zatvori", "Zaključena smjena"), ("/api/smjena/brojanje", "Brzo brojanje ladice"), ("/api/uvoz", "Osvježeno iz Robe"),
    ("/api/postavke", "Promijenjene postavke"), ("/api/artikl/", "Promijenjen artikl"),
    ("/api/partner", "Dobavljač"), ("/api/akcije/", "Akcija"), ("/api/inv/", "Inventura"),
    ("/api/narudzba", "Narudžba kupca"), ("/api/kupac", "Kupac"), ("/api/ponuda", "Ponuda"),
    ("/api/haccp/", "HACCP upis"), ("/api/rokovi/", "Rokovi trajanja"), ("/api/bonovi/", "Poklon bon"),
    ("/api/vjernost/", "Kartica vjernosti"), ("/api/dugovanja/", "Dugovanja"), ("/api/kopija", "Sigurnosna kopija"),
    ("/api/snizenje/", "Sniženje"), ("/api/ambalaza/", "Gajbe i ambalaža"), ("/api/lanci/", "Cijene lanaca"),
    ("/api/radnici/", "Radnici"), ("/api/sazetak/", "E-mail sažetak"),
    ("/api/povrat_amb/", "Povratna naknada za ambalažu"), ("/api/zaslon/", "Zaslon za kupca"),
    ("/api/tisak/ladica", "Ladica (gotovina)"), ("/api/tisak/otvori_ladicu", "Ladica otvorena ručno"),
    ("/api/tisak/", "Termalni pisač"),
    ("/api/uplata", "Postavke barkoda za plaćanje"), ("/api/dob_mail/posalji", "Narudžba dobavljaču e-mailom"),
    ("/api/kopija_van/", "Kopija izvan računala"), ("/api/donacij", "Donacija hrane"),
    ("/api/eposrednik/postavke", "Postavke eRačuna (posrednik)"), ("/api/eposrednik/izlazni", "Izlazni eRačun"),
    ("/api/eposrednik/", "Ulazni eRačuni (posrednik)"),
)


def opis_radnje(con, putanja, podaci, odgovor):
    naziv = next((t for poc, t in OPISI if putanja.startswith(poc)), putanja)
    dodatak = ""
    rid = odgovor.get("id") if isinstance(odgovor, dict) else None
    try:
        if putanja in ("/api/naplati",) or putanja.startswith("/api/storno/"):
            r = con.execute("SELECT godina, broj, ukupno FROM racuni WHERE id = ?", (rid,)).fetchone()
            if r:
                dodatak = "K-%d-%d, %.2f €" % (r[0], r[1], r[2] / 100.0)
        elif putanja == "/api/dokument" or putanja.startswith("/api/storno_dok/"):
            import nabava
            r = con.execute("SELECT * FROM dokumenti WHERE id = ?", (rid,)).fetchone()
            if r:
                dodatak = "%s %s" % (r["vrsta"], nabava.oznaka(r))
        elif putanja == "/api/smjena/promet":
            dodatak = "%s %s %s" % (podaci.get("vrsta") or "polog", podaci.get("iznos") or "", podaci.get("opis") or "")
        elif putanja in ("/api/smjena/zatvori", "/api/smjena/brojanje"):
            dodatak = "prebrojano %s" % (podaci.get("prebrojano") or "")
            if putanja == "/api/smjena/brojanje" and isinstance(odgovor, dict) and "razlika" in odgovor:
                dodatak += ", razlika %.2f €" % (odgovor["razlika"] / 100.0)
        elif putanja == "/api/postavke":
            dodatak = ", ".join("%s=%s" % (k, "da" if v else "ne") for k, v in sorted(podaci.items())
                                if isinstance(v, bool))
        elif putanja.startswith("/api/artikl/"):
            dodatak = ", ".join("%s=%s" % (k, v) for k, v in sorted(podaci.items()))
        elif putanja == "/api/radnici/spremi":
            dodatak = "%s%s" % (podaci.get("ime") or "", ", novi PIN" if podaci.get("pin") else "")
        elif putanja.startswith("/api/snizenje/") and isinstance(odgovor, dict) and odgovor.get("opis"):
            dodatak = odgovor["opis"]
    except Exception:  # dnevnik nikad ne smije srušiti radnju
        pass
    return (naziv + (": " + dodatak if dodatak else ""))[:300]


def zapisi(con, radnik, putanja, opis):
    with con:
        con.execute("INSERT INTO radnje (vrijeme, radnik_id, radnik, putanja, opis) VALUES (?, ?, ?, ?, ?)",
                    (sada(), radnik["id"] if radnik else None, radnik["ime"] if radnik else "", putanja, opis))


def radnje(con, p):
    od = str(p.get("od") or (dt.date.today() - dt.timedelta(days=7)).isoformat())[:10]
    do = str(p.get("do") or dt.date.today().isoformat())[:10]
    sql = "SELECT * FROM radnje WHERE substr(vrijeme, 1, 10) BETWEEN ? AND ?"
    args = [od, do]
    if p.get("radnik"):
        sql += " AND radnik_id = ?"
        args.append(int(p["radnik"]))
    return {"od": od, "do": do, "radnje": [dict(r) for r in con.execute(sql + " ORDER BY id DESC LIMIT 1000", args)]}


def postavke_radnika(con):
    return {"odjava_min": postavke.vrijednost(con, "radnici.odjava_min", "0"), "ovlasti": OVLASTI}


def spremi_postavke(con, p):
    v = str(p.get("odjava_min") or "0").strip()
    if not v.isdigit() or int(v) > 600:
        raise Greska("Automatska odjava: broj minuta 0 do 600 (0 = nikad).")
    postavke.postavi(con, "radnici.odjava_min", str(int(v)))
    return {"ok": True}

