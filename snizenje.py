# -*- coding: utf-8 -*-
"""
Brzo sniženje robe koja vene (modul "snizenje").

Primjer: popodne su banane prezrele. Odaberete artikl, kliknete -30 % i program
izračuna sniženu cijenu, ispiše etiketu i zapamti sniženje do kraja dana.
Kad se podaci iz Robe osvježe, vidi se koliko je prodano po sniženoj cijeni
(i koliko bi inače otišlo u otpis), a koliko je ipak otpisano.

Pravila su ista kao kod akcija (akcije.py):
- postotak se računa od NAJNIŽE cijene u 30 dana prije sniženja i ta cijena
  piše na etiketi (Zakon o zaštiti potrošača, NN 59/2026; za kvarljivu robu
  postoji blaže pravilo, ali mi uvijek koristimo strože);
- snižena cijena nikad nije ispod nabavne s PDV-om (prodaja ispod nabavne može biti
  nepošteno trgovanje); za robu koja se ne može prodati ni tada: otpis ili donacija;
- račune izdaje Roba, pa se snižena cijena naplaćuje u Robi (popust na stavci
  ili promjena MPC); istaknuta cijena mora biti ista kao naplaćena.
"""

import datetime as dt

import akcije
import cijene
from baza import ZALIHA_SQL, zaokruzi
from nabava import Greska

POSTOCI = (20, 30, 40, 50)

SHEMA = """
CREATE TABLE IF NOT EXISTS snizenja (
    id          INTEGER PRIMARY KEY,
    artikl_id   INTEGER NOT NULL REFERENCES artikli(id),
    naziv       TEXT NOT NULL,
    jm          TEXT NOT NULL,
    od          TEXT NOT NULL,              -- 'YYYY-MM-DD HH:MM:SS'
    do          TEXT NOT NULL,
    mpc         INTEGER NOT NULL,           -- MPC u trenutku sniženja
    najniza_30  INTEGER NOT NULL,           -- od nje se računa postotak, piše na etiketi
    postotak    REAL NOT NULL,
    cijena      INTEGER NOT NULL,           -- snižena MPC s PDV-om
    nabavna     INTEGER NOT NULL DEFAULT 0, -- prosječna nabavna bez PDV-a u trenutku sniženja
    pdv_stopa   REAL NOT NULL,
    zaliha      REAL NOT NULL DEFAULT 0,    -- zaliha u trenutku sniženja
    kolicina    REAL NOT NULL DEFAULT 0,    -- koliko robe ide na sniženje (procjena, 0 = ne zna se)
    razlog      TEXT NOT NULL DEFAULT '',
    zavrseno    TEXT NOT NULL DEFAULT '',   -- ručno završeno prije roka
    radnik      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_snizenja_artikl ON snizenja(artikl_id, od);
"""


def sada():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def pripremi(con):
    con.executescript(SHEMA)


def _artikl(con, aid):
    a = con.execute("SELECT a.*, round(%s, 3) AS zaliha_sada FROM artikli a WHERE a.id = ?" % ZALIHA_SQL,
                    (aid,)).fetchone()
    if not a:
        raise Greska("Artikl ne postoji.")
    return a


def izracun(con, aid, postotak):
    """Snižena cijena za artikl i postotak, s granicom nabavne cijene."""
    a = _artikl(con, aid)
    try:
        postotak = float(str(postotak).replace(",", "."))
    except ValueError:
        raise Greska("Neispravan postotak.")
    if not 0 < postotak < 100:
        raise Greska("Postotak sniženja mora biti između 0 i 100.")
    najniza = cijene.najniza_30(con, aid, s_trenutnom=True) or a["cijena"]
    cijena = zaokruzi(najniza * (1 - postotak / 100.0))
    nab = akcije._nabavna_s_pdv(a)
    najvise = int((1 - nab / float(najniza)) * 100) if nab and najniza else 99
    return {"id": a["id"], "naziv": a["naziv"], "jm": a["jm"], "mpc": a["cijena"], "najniza_30": najniza,
            "postotak": postotak, "cijena": cijena, "najvise_posto": max(0, najvise),
            "ispod_nabavne": bool(nab and cijena < nab), "zaliha": a["zaliha_sada"]}


def pokreni(con, p, radnik=""):
    try:
        aid = int(p.get("artikl_id"))
    except (TypeError, ValueError):
        raise Greska("Odaberite artikl.")
    i = izracun(con, aid, p.get("postotak"))
    a = _artikl(con, aid)
    if a["usluga"]:
        raise Greska("Usluga se ne snižava ovdje.")
    if i["cijena"] <= 0 or i["cijena"] >= i["najniza_30"]:
        raise Greska("Snižena cijena mora biti niža od najniže cijene u 30 dana (%s EUR)."
                     % cijene._eur(i["najniza_30"]))
    if i["ispod_nabavne"]:
        raise Greska("%s: -%s %% je ispod nabavne cijene s PDV-om (%s EUR). Najviše možete sniziti %d %%; "
                     "ako se ni tada ne proda, napravite otpis ili donaciju."
                     % (a["naziv"], ("%g" % i["postotak"]).replace(".", ","), cijene._eur(akcije._nabavna_s_pdv(a)), i["najvise_posto"]))
    if a["akcija_prije"]:
        raise Greska("%s je već na akciji (Zaliha, Akcije). Prvo završite akciju." % a["naziv"])
    od = sada()
    do = str(p.get("do") or "").strip()
    if do:
        try:
            do = dt.datetime.fromisoformat(do.replace("T", " ")).strftime("%Y-%m-%d %H:%M:59")
        except ValueError:
            raise Greska("Neispravno vrijeme kraja sniženja.")
    else:
        do = od[:10] + " 23:59:59"
    if do <= od:
        raise Greska("Kraj sniženja mora biti kasnije od sada.")
    if do[:10] > (dt.date.today() + dt.timedelta(days=7)).isoformat():
        raise Greska("Brzo sniženje traje najviše 7 dana; za dulje koristite Akcije.")
    try:
        kolicina = max(0.0, round(float(str(p.get("kolicina") or 0).replace(",", ".")), 3))
    except ValueError:
        raise Greska("Neispravna količina.")
    if not (kolicina == kolicina and kolicina <= 100000):  # NaN, beskonačno
        raise Greska("Neispravna količina.")
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("SELECT 1 FROM snizenja WHERE artikl_id = ? AND zavrseno = '' AND do > ?",
                       (aid, od)).fetchone():
            raise Greska("%s je već na sniženju." % a["naziv"])
        sid = con.execute(
            """INSERT INTO snizenja (artikl_id, naziv, jm, od, do, mpc, najniza_30, postotak, cijena, nabavna,
                                     pdv_stopa, zaliha, kolicina, razlog, radnik)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (aid, a["naziv"], a["jm"], od, do, a["cijena"], i["najniza_30"], i["postotak"], i["cijena"],
             a["nabavna"], a["pdv_stopa"], a["zaliha_sada"], kolicina,
             str(p.get("razlog") or "").strip()[:100], radnik or "")).lastrowid
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return {"id": sid, "opis": "%s -%s %% na %s EUR" % (a["naziv"], ("%g" % i["postotak"]).replace(".", ","), cijene._eur(i["cijena"]))}


def zavrsi(con, p):
    sid = int(p.get("id") or 0)
    with con:
        if not con.execute("UPDATE snizenja SET zavrseno = ? WHERE id = ? AND zavrseno = '' AND do > ?",
                           (sada(), sid, sada())).rowcount:
            raise Greska("Sniženje nije u tijeku.")
    return {"id": sid}


def _kraj(s):
    return s["zavrseno"] or s["do"]


def _ucinak(con, s):
    """Prodaja po sniženoj cijeni u Robi i otpisi u vrijeme sniženja (i dan poslije)."""
    kraj = _kraj(s)
    prodano = prihod = 0.0
    for r in con.execute(
            """SELECT st.kolicina, st.iznos FROM roba_stavke st
               JOIN artikli a ON a.roba_sifra = st.artikl
               JOIN roba_dokumenti d ON d.godina = st.godina AND d.sifra = st.link
               WHERE a.id = ? AND st.status IN (2, 3) AND d.brisan = 0
                 AND COALESCE(NULLIF(d.vrijeme, ''), d.datum || ' 12:00:00') BETWEEN ? AND ?""",
            (s["artikl_id"], s["od"], kraj)):
        if r["kolicina"] > 0 and r["iznos"] <= r["kolicina"] * s["cijena"] + 1:
            prodano += r["kolicina"]
            prihod += r["iznos"]
    dan_poslije = (dt.date.fromisoformat(kraj[:10]) + dt.timedelta(days=1)).isoformat()
    otpisano = con.execute(
        """SELECT COALESCE(SUM(st.kolicina), 0) FROM dok_stavke st JOIN dokumenti d ON d.id = st.dokument_id
           WHERE d.vrsta = 'otpis' AND d.storniran = 0 AND d.storno_od IS NULL AND st.artikl_id = ?
             AND d.datum BETWEEN ? AND ?""", (s["artikl_id"], s["od"][:10], dan_poslije)).fetchone()[0]
    otpisano = otpisano or 0
    zadnji = con.execute("SELECT MAX(vrijeme) FROM roba_dokumenti WHERE status IN (2, 3)").fetchone()[0] or ""
    return {"prodano": round(prodano, 3), "prihod": int(prihod), "otpisano": round(otpisano, 3),
            "spaseno_nabavna": zaokruzi(prodano * s["nabavna"]),
            "podaci_do": zadnji,
            "nepotpuno": zadnji < kraj and (zadnji[:10] < kraj[:10] or kraj[:10] >= dt.date.today().isoformat())}


def popis(con, dana=30):
    od = (dt.date.today() - dt.timedelta(days=int(dana))).isoformat()
    t = sada()
    out = []
    for s in con.execute("SELECT * FROM snizenja WHERE od >= ? ORDER BY id DESC", (od,)):
        d = dict(s)
        d["u_tijeku"] = not s["zavrseno"] and s["do"] > t
        d.update(_ucinak(con, s))
        out.append(d)
    ukupno = {"prihod": sum(x["prihod"] for x in out),
              "spaseno_nabavna": sum(x["spaseno_nabavna"] for x in out),
              "otpisano_nabavna": sum(zaokruzi(x["otpisano"] * x["nabavna"]) for x in out),
              "snizenja": len(out)}
    return {"snizenja": out, "ukupno": ukupno, "postoci": POSTOCI, "od": od}


def aktivne_cijene(con):
    """{artikl_id: snižena cijena} za sniženja u tijeku (za kasu za vježbu i sažetak)."""
    t = sada()
    return {r[0]: r[1] for r in con.execute(
        "SELECT artikl_id, cijena FROM snizenja WHERE zavrseno = '' AND od <= ? AND do > ?", (t, t))}


def kandidati(con):
    """Artikli (bez usluga i akcija) za odabir sniženja."""
    zadnji = con.execute("SELECT MAX(datum) FROM roba_dokumenti WHERE status IN (2, 3)").fetchone()[0]
    prodaja = {}
    if zadnji:
        od = (dt.date.fromisoformat(zadnji) - dt.timedelta(days=13)).isoformat()
        prodaja = {r[0]: r[1] for r in con.execute(
            """SELECT a.id, SUM(s.kolicina) / 14.0 FROM roba_stavke s JOIN artikli a ON a.roba_sifra = s.artikl
               WHERE s.status IN (2, 3) AND s.datum BETWEEN ? AND ? GROUP BY a.id""", (od, zadnji))}
    out = []
    for a in con.execute("""SELECT a.id, a.sifra, a.naziv, a.jm, a.cijena, round(%s, 3) AS zaliha
                            FROM artikli a WHERE a.aktivan = 1 AND a.usluga = 0 AND a.akcija_prije = 0
                            ORDER BY a.naziv""" % ZALIHA_SQL):
        dnevno = prodaja.get(a["id"]) or 0
        out.append({"id": a["id"], "sifra": a["sifra"], "naziv": a["naziv"], "jm": a["jm"], "cijena": a["cijena"],
                    "zaliha": a["zaliha"], "dana_zalihe": round(a["zaliha"] / dnevno, 1) if dnevno and a["zaliha"] > 0 else None})
    # zaliha iz Robe zna biti u minusu, pa se nude svi artikli; prvo oni koji su na zalihi
    out.sort(key=lambda x: x["zaliha"] <= 0)
    return out


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def etiketa_html(con, sid, naziv_trgovine, voce_povrce=True):
    s = con.execute("SELECT * FROM snizenja WHERE id = ?", (sid,)).fetchone()
    if not s:
        raise Greska("Sniženje ne postoji.")
    a = con.execute("SELECT * FROM artikli WHERE id = ?", (s["artikl_id"],)).fetchone()
    jed = "EUR/kg" if s["jm"].lower() == "kg" else "EUR/" + _esc(s["jm"])
    e = cijene._eur
    pod = _esc(a["podrijetlo"]) if a and a["podrijetlo"] and voce_povrce else ""
    kraj = dt.datetime.fromisoformat(s["do"])
    do = ("danas do %s" % kraj.strftime("%H:%M")) if s["do"][:10] == s["od"][:10] else \
        "do %s %s" % (cijene.datum_hr(s["do"][:10]), kraj.strftime("%H:%M"))
    za_jed = ""
    if a and s["jm"].lower() not in ("kg", "l") and a["neto_jm"] in ("kg", "l") and a["neto_kolicina"]:
        za_jed = "<div class=m>Cijena za %s: %s EUR</div>" % (a["neto_jm"], e(int(s["cijena"] / a["neto_kolicina"] + 0.5)))
    return """<!doctype html><html lang=hr><head><meta charset=utf-8><title>Sniženo %s</title>
<style>
@page { size: A4; margin: 10mm; }
body { font-family: Arial, sans-serif; margin: 0; color: #000; }
.e { border: 3px solid #000; width: 95mm; min-height: 62mm; padding: 5mm; box-sizing: border-box; margin: 5mm auto; }
.n { font-size: 16pt; font-weight: bold; text-transform: uppercase; }
.s { font-size: 22pt; font-weight: bold; background: #000; color: #fff; display: inline-block; padding: 1mm 4mm; margin: 2mm 0; }
.c { font-size: 34pt; font-weight: bold; } .c span { font-size: 12pt; font-weight: normal; }
.m { font-size: 10pt; } .p { font-size: 34pt; font-weight: bold; line-height: 1; }
.p small { display: block; font-size: 8pt; font-weight: normal; }
@media print { .gumbi { display: none; } }
</style></head><body>
<div class=e><div class=m>%s</div><div class=n>%s</div>
<div class=s>SNIŽENO -%s %%</div>
<div class=c>%s <span>%s</span></div>%s
<div class=m>Najniža cijena u 30 dana prije sniženja: <b>%s %s</b></div>
<div class=m>Sniženje vrijedi %s.</div>%s</div>
<p class=gumbi style="text-align:center"><button onclick="print()">Ispiši</button> <button onclick="close()">Zatvori</button></p>
<script>if (location.hash === '#ispis') { addEventListener('load', () => print()); }</script>
</body></html>""" % (_esc(s["naziv"]), _esc(naziv_trgovine), _esc(s["naziv"]), ("%g" % s["postotak"]).replace(".", ","),
                     e(s["cijena"]), jed, za_jed, e(s["najniza_30"]), jed, do,
                     ("<div class=p><small>Podrijetlo</small>%s</div>" % pod) if pod else "")
