# -*- coding: utf-8 -*-
"""
Baza podataka našeg programa (SQLite, jedna datoteka trgovina.db).

Iznosi se čuvaju u centima (cijeli broj), da nema grešaka zaokruživanja.
Količine se čuvaju kao decimalni broj (npr. 0.592 kg).
"""

import math
import os
import sqlite3

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("TRGOVINA_DB") or os.path.join(SCRIPT_DIR, "trgovina.db")

SHEMA = """
CREATE TABLE IF NOT EXISTS artikli (
    id            INTEGER PRIMARY KEY,
    roba_sifra    INTEGER UNIQUE,          -- SIFRA iz Robe (artikli.dbf), NULL za nove
    sifra         TEXT NOT NULL,           -- kratka šifra za kasu (Robin MSIFRA)
    barkod        TEXT NOT NULL DEFAULT '',
    naziv         TEXT NOT NULL,
    jm            TEXT NOT NULL DEFAULT 'Kom',
    cijena        INTEGER NOT NULL,        -- MPC s PDV-om, u centima
    pdv_stopa     REAL NOT NULL,           -- npr. 5.0, 13.0, 25.0
    usluga        INTEGER NOT NULL DEFAULT 0,  -- 1 = usluga, ne vodi se zaliha
    zaliha        REAL NOT NULL DEFAULT 0,
    nabavna       INTEGER NOT NULL DEFAULT 0,  -- prosječna nabavna, u centima
    aktivan       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_artikli_sifra ON artikli(sifra);
CREATE INDEX IF NOT EXISTS ix_artikli_barkod ON artikli(barkod);

CREATE TABLE IF NOT EXISTS nacini_placanja (
    id          INTEGER PRIMARY KEY,
    roba_sifra  INTEGER UNIQUE,
    naziv       TEXT NOT NULL,
    aktivan     INTEGER NOT NULL DEFAULT 1
);

-- Naši (interni, NEFISKALNI) računi s kase.
-- Izdani račun se nikad ne mijenja ni briše: ispravak ide kroz storno.
CREATE TABLE IF NOT EXISTS racuni (
    id                 INTEGER PRIMARY KEY,
    godina             INTEGER NOT NULL,
    broj               INTEGER NOT NULL,       -- redni broj u godini, bez rupa
    vrijeme            TEXT NOT NULL,          -- 'YYYY-MM-DD HH:MM:SS'
    ukupno             INTEGER NOT NULL,       -- u centima, s PDV-om
    nacin_placanja_id  INTEGER NOT NULL REFERENCES nacini_placanja(id),
    storno_od          INTEGER REFERENCES racuni(id),  -- ovaj račun stornira taj
    storniran          INTEGER NOT NULL DEFAULT 0,     -- ovaj račun je storniran
    napomena           TEXT NOT NULL DEFAULT '',
    UNIQUE (godina, broj)
);

-- Račun plaćen s više načina (npr. dio gotovinom, dio karticom); jedan način = nema redaka.
CREATE TABLE IF NOT EXISTS racun_placanja (
    id                 INTEGER PRIMARY KEY,
    racun_id           INTEGER NOT NULL REFERENCES racuni(id),
    nacin_placanja_id  INTEGER NOT NULL REFERENCES nacini_placanja(id),
    iznos              INTEGER NOT NULL        -- u centima
);
CREATE INDEX IF NOT EXISTS ix_racun_placanja ON racun_placanja(racun_id);

CREATE TABLE IF NOT EXISTS racun_stavke (
    id          INTEGER PRIMARY KEY,
    racun_id    INTEGER NOT NULL REFERENCES racuni(id),
    artikl_id   INTEGER REFERENCES artikli(id),
    naziv       TEXT NOT NULL,
    jm          TEXT NOT NULL,
    kolicina    REAL NOT NULL,
    cijena      INTEGER NOT NULL,     -- jedinična MPC u trenutku prodaje
    popust      REAL NOT NULL DEFAULT 0,  -- postotak
    iznos       INTEGER NOT NULL,     -- s PDV-om, nakon popusta
    pdv_stopa   REAL NOT NULL,
    nabavna     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_stavke_racun ON racun_stavke(racun_id);

-- Povijest iz Robe (samo za izvještaje, puni je uvoz).
CREATE TABLE IF NOT EXISTS roba_dokumenti (
    godina      INTEGER NOT NULL,
    sifra       INTEGER NOT NULL,     -- mat.SIFRA
    dokument    TEXT NOT NULL,
    status      INTEGER NOT NULL,     -- 3 račun, 1 primka/otkup, 10 kalo/otpis
    datum       TEXT,
    vrijeme     TEXT,
    neto        INTEGER NOT NULL,
    porez       INTEGER NOT NULL,
    iznos       INTEGER NOT NULL,
    partner     TEXT NOT NULL DEFAULT '',
    placanje    INTEGER,
    brisan      INTEGER NOT NULL DEFAULT 0,
    jir         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (godina, sifra)
);
CREATE TABLE IF NOT EXISTS roba_stavke (
    godina      INTEGER NOT NULL,
    sifra       INTEGER NOT NULL,
    link        INTEGER NOT NULL,     -- -> roba_dokumenti.sifra
    status      INTEGER NOT NULL,
    datum       TEXT,
    artikl      INTEGER,              -- -> artikli.roba_sifra
    naziv       TEXT NOT NULL,
    jm          TEXT NOT NULL,
    kolicina    REAL NOT NULL,
    iznos       INTEGER NOT NULL,     -- maloprodajni iznos
    neto        INTEGER NOT NULL,
    nabavna     INTEGER NOT NULL,
    pdv_stopa   REAL NOT NULL,
    PRIMARY KEY (godina, sifra)
);
CREATE INDEX IF NOT EXISTS ix_roba_stavke_link ON roba_stavke(godina, link);
CREATE INDEX IF NOT EXISTS ix_roba_stavke_art ON roba_stavke(artikl, status, datum);
CREATE INDEX IF NOT EXISTS ix_roba_dok_status ON roba_dokumenti(status, datum);
-- Plaćanja računa iz Robe (mplacanje.dbf). Račun plaćen dijelom gotovinom, dijelom karticom
-- ima u zaglavlju placanje = -2, a dijelovi su ovdje.
CREATE TABLE IF NOT EXISTS roba_placanja (
    godina      INTEGER NOT NULL,
    link        INTEGER NOT NULL,     -- -> roba_dokumenti.sifra
    placanje    INTEGER,              -- -> nacini_placanja.roba_sifra
    iznos       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_roba_plac ON roba_placanja(godina, link);

-- Dobavljači (iz Robe, dobav.dbf) i novi.
CREATE TABLE IF NOT EXISTS partneri (
    id          INTEGER PRIMARY KEY,
    roba_sifra  INTEGER UNIQUE,
    naziv       TEXT NOT NULL,
    oib         TEXT NOT NULL DEFAULT '',
    aktivan     INTEGER NOT NULL DEFAULT 1
);

-- Naši skladišni dokumenti: primka (ulaz), otpis (kalo, kvar), inventura (ispravak),
-- povrat (roba vraćena dobavljaču).
-- Kao i računi: izdani dokument se ne mijenja, ispravak ide kroz storno.
CREATE TABLE IF NOT EXISTS dokumenti (
    id               INTEGER PRIMARY KEY,
    vrsta            TEXT NOT NULL CHECK (vrsta IN ('primka', 'otpis', 'inventura', 'povrat')),
    godina           INTEGER NOT NULL,
    broj             INTEGER NOT NULL,
    datum            TEXT NOT NULL,          -- datum dokumenta (npr. datum računa dobavljača)
    vrijeme          TEXT NOT NULL,          -- kad je upisan
    partner_id       INTEGER REFERENCES partneri(id),
    broj_dobavljaca  TEXT NOT NULL DEFAULT '',
    neto             INTEGER NOT NULL DEFAULT 0,   -- nabavna vrijednost bez PDV-a, centi
    pdv              INTEGER NOT NULL DEFAULT 0,   -- ulazni PDV (primka, povrat), centi
    storno_od        INTEGER REFERENCES dokumenti(id),
    storniran        INTEGER NOT NULL DEFAULT 0,
    napomena         TEXT NOT NULL DEFAULT '',
    UNIQUE (vrsta, godina, broj)
);

-- kolicina: primka, otpis i povrat pozitivno (otpis i povrat se oduzimaju), inventura s predznakom
-- (višak +, manjak -). Storno ima suprotan predznak.
CREATE TABLE IF NOT EXISTS dok_stavke (
    id           INTEGER PRIMARY KEY,
    dokument_id  INTEGER NOT NULL REFERENCES dokumenti(id),
    artikl_id    INTEGER NOT NULL REFERENCES artikli(id),
    naziv        TEXT NOT NULL,
    jm           TEXT NOT NULL,
    kolicina     REAL NOT NULL,
    cijena       INTEGER NOT NULL,     -- nabavna jedinična bez PDV-a, centi
    iznos        INTEGER NOT NULL,     -- kolicina * cijena, centi
    pdv_stopa    REAL NOT NULL,
    mpc          INTEGER NOT NULL DEFAULT 0, -- MPC u trenutku unosa, za maržu
    fakturna     INTEGER NOT NULL DEFAULT 0, -- cijena s računa dobavljača prije rabata
    rabat        REAL NOT NULL DEFAULT 0     -- rabat dobavljača u %
);
CREATE INDEX IF NOT EXISTS ix_dok_stavke_dok ON dok_stavke(dokument_id);
CREATE INDEX IF NOT EXISTS ix_dok_stavke_art ON dok_stavke(artikl_id);

-- Blagajna: smjena od otvaranja do zaključka, s gotovinom u ladici.
CREATE TABLE IF NOT EXISTS smjene (
    id           INTEGER PRIMARY KEY,
    otvorena     TEXT NOT NULL,
    zatvorena    TEXT,
    pocetno      INTEGER NOT NULL,          -- gotovina u ladici na početku, centi
    ocekivano    INTEGER,                   -- izračun kod zaključka
    prebrojano   INTEGER,                   -- stvarno prebrojano kod zaključka
    gotovina     INTEGER,                   -- gotovinska prodaja (iz Robe) u smjeni
    kartice      INTEGER,                   -- ostala plaćanja (informativno)
    racuna       INTEGER,
    napomena     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS blagajna_promet (
    id         INTEGER PRIMARY KEY,
    smjena_id  INTEGER NOT NULL REFERENCES smjene(id),
    vrijeme    TEXT NOT NULL,
    iznos      INTEGER NOT NULL,   -- + polog (npr. sitniš), - isplata (npr. plaćen dobavljač)
    opis       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS uvozi (
    id          INTEGER PRIMARY KEY,
    vrijeme     TEXT NOT NULL,
    mapa        TEXT NOT NULL,
    opis        TEXT NOT NULL
);
"""


class Veza(sqlite3.Connection):
    """Kasa radi s isolation_level=None (transakcije vodi sama). Tada "with con:" ne bi
    otvorio transakciju i svaka bi naredba bila spremljena odmah, pa greška na pola ne bi
    vratila ništa. Ovdje "with con:" uvijek otvara transakciju (BEGIN IMMEDIATE)."""

    def __enter__(self):
        if not hasattr(self, "_vanjske"):
            self._vanjske = []
        # "with con:" unutar već otvorene transakcije (npr. BEGIN IMMEDIATE u naplati)
        # ne smije je spremiti na pola; spremit će je (ili poništiti) vanjski dio
        self._vanjske.append(self.isolation_level is None and self.in_transaction)
        if self.isolation_level is None and not self.in_transaction:
            self.execute("BEGIN IMMEDIATE")
        return super().__enter__()

    def __exit__(self, tip, vrijednost, trag):
        if self._vanjske.pop():
            return False
        return super().__exit__(tip, vrijednost, trag)


def spoji(path=DB_PATH):
    con = sqlite3.connect(path, timeout=10, factory=Veza)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SHEMA)
    _nadogradi(con)
    import inventura  # moduli drže svoje tablice
    import narudzbe
    import akcije
    import haccp
    import postavke
    import kupci_tvrtke
    import rokovi
    import bonovi
    import dugovanja
    import roba_dodatno
    import ponude
    roba_dodatno.pripremi(con)
    narudzbe.pripremi(con)
    ponude.pripremi(con)
    inventura.pripremi(con)
    akcije.pripremi(con)
    postavke.pripremi(con)
    haccp.pripremi(con)
    kupci_tvrtke.pripremi(con)
    rokovi.pripremi(con)
    bonovi.pripremi(con)
    dugovanja.pripremi(con)
    import eracun
    eracun.pripremi(con)
    import kopija_van
    import donacije
    kopija_van.pripremi(con)
    donacije.pripremi(con)
    return con


def _nadogradi(con):
    """Dodaje stupce koji su nastali nakon prve verzije (stara baza ostaje)."""
    stupci = {r[1] for r in con.execute("PRAGMA table_info(artikli)")}
    if "nabavna_nasa" not in stupci:
        # 1 = prosječnu nabavnu računamo mi (iz naših primki), uvoz je ne dira
        con.execute("ALTER TABLE artikli ADD COLUMN nabavna_nasa INTEGER NOT NULL DEFAULT 0")
        con.commit()
    stupci = {r[1] for r in con.execute("PRAGMA table_info(dok_stavke)")}
    if "rabat" not in stupci:
        con.execute("ALTER TABLE dok_stavke ADD COLUMN fakturna INTEGER NOT NULL DEFAULT 0")
        con.execute("ALTER TABLE dok_stavke ADD COLUMN rabat REAL NOT NULL DEFAULT 0")
        con.commit()
    if "nabavna_prije" not in stupci:
        # prosječna nabavna prije primke, da je storno primke može vratiti
        con.execute("ALTER TABLE dok_stavke ADD COLUMN nabavna_prije INTEGER")
        con.commit()
    stupci = {r[1] for r in con.execute("PRAGMA table_info(artikli)")}
    if "min_zaliha" not in stupci:
        # minimalna zaliha za "pri kraju" i prijedlog narudžbe; uvoz je ne dira
        con.execute("ALTER TABLE artikli ADD COLUMN min_zaliha REAL NOT NULL DEFAULT 0")
        con.commit()
    stupci = {r[1] for r in con.execute("PRAGMA table_info(artikli)")}
    if "podrijetlo" not in stupci:
        # za etikete: zemlja podrijetla (obvezna za rinfuzno voće i povrće) i klasa
        con.execute("ALTER TABLE artikli ADD COLUMN podrijetlo TEXT NOT NULL DEFAULT ''")
        con.execute("ALTER TABLE artikli ADD COLUMN klasa TEXT NOT NULL DEFAULT ''")
        con.commit()
    stupci = {r[1] for r in con.execute("PRAGMA table_info(partneri)")}
    if "uredjen" not in stupci:
        for stupac in ("adresa", "mjesto", "telefon", "email", "napomena"):
            con.execute("ALTER TABLE partneri ADD COLUMN %s TEXT NOT NULL DEFAULT ''" % stupac)
        # 1 = uređen kod nas, uvoz iz Robe ga više ne prepisuje
        con.execute("ALTER TABLE partneri ADD COLUMN uredjen INTEGER NOT NULL DEFAULT 0")
        con.commit()
    stupci = {r[1] for r in con.execute("PRAGMA table_info(roba_stavke)")}
    if "cijena" not in stupci:
        # jedinična MPC s PDV-om prije rabata (mstavke VPCPDV); puni je sljedeći uvoz
        con.execute("ALTER TABLE roba_stavke ADD COLUMN cijena INTEGER NOT NULL DEFAULT 0")
        con.commit()
    stupci = {r[1] for r in con.execute("PRAGMA table_info(artikli)")}
    if "neto_kolicina" not in stupci:
        # neto količina pakiranja za cijenu za jedinicu mjere (Pravilnik NN 105/2026, čl. 8);
        # neto_jm: 'kg', 'l', ili '-' = ne treba (voće i povrće na komad ili vezicu, ispod 50 g)
        con.execute("ALTER TABLE artikli ADD COLUMN neto_kolicina REAL NOT NULL DEFAULT 0")
        con.execute("ALTER TABLE artikli ADD COLUMN neto_jm TEXT NOT NULL DEFAULT ''")
        con.commit()
    stupci = {r[1] for r in con.execute("PRAGMA table_info(roba_stavke)")}
    if "porez" not in stupci:
        # PDV stavke kako ga je Roba izračunala i fiskalizirala (mstavke PDV_IV); puni ga uvoz
        con.execute("ALTER TABLE roba_stavke ADD COLUMN porez INTEGER")
        con.commit()
    stupci = {r[1] for r in con.execute("PRAGMA table_info(artikli)")}
    if "sidrena" not in stupci:
        # sidrena (dodatna) cijena za etikete, od 17. 11. 2026. (vidi cijene.py); uvoz je ne dira
        con.execute("ALTER TABLE artikli ADD COLUMN sidrena INTEGER NOT NULL DEFAULT 0")
        con.execute("ALTER TABLE artikli ADD COLUMN sidrena_datum TEXT NOT NULL DEFAULT ''")
        con.commit()
    stupci = {r[1] for r in con.execute("PRAGMA table_info(artikli)")}
    if "akcija_prije" not in stupci:
        # akcija: uz sniženu cijenu ističe se najniža cijena u 30 dana prije (Zakon o zaštiti
        # potrošača); 0 = nije na akciji
        con.execute("ALTER TABLE artikli ADD COLUMN akcija_prije INTEGER NOT NULL DEFAULT 0")
        con.commit()
    sql = con.execute("SELECT sql FROM sqlite_master WHERE name = 'dokumenti'").fetchone()[0]
    if "'povrat'" not in sql:
        _dokumenti_s_povratom(con)


def _dokumenti_s_povratom(con):
    """Nova vrsta dokumenta 'povrat': SQLite ne može promijeniti CHECK, pa se tablica
    prepisuje (postupak iz SQLite dokumentacije, sve u jednoj transakciji)."""
    nova = SHEMA[SHEMA.index("CREATE TABLE IF NOT EXISTS dokumenti ("):]
    nova = nova[:nova.index(");") + 2].replace(
        "CREATE TABLE IF NOT EXISTS dokumenti (", "CREATE TABLE dokumenti_novi (")
    stara_razina = con.isolation_level
    con.commit()
    con.isolation_level = None
    con.execute("PRAGMA foreign_keys = OFF")
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(nova)
        con.execute("INSERT INTO dokumenti_novi SELECT id, vrsta, godina, broj, datum, vrijeme, "
                    "partner_id, broj_dobavljaca, neto, pdv, storno_od, storniran, napomena "
                    "FROM dokumenti")
        con.execute("DROP TABLE dokumenti")
        con.execute("ALTER TABLE dokumenti_novi RENAME TO dokumenti")
        if con.execute("PRAGMA foreign_key_check").fetchone():
            raise sqlite3.IntegrityError("foreign_key_check nakon prepisivanja tablice dokumenti")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.execute("PRAGMA foreign_keys = ON")
        con.isolation_level = stara_razina


def oib_ispravan(oib):
    """Kontrolna znamenka OIB-a (ISO 7064, MOD 11,10)."""
    if len(oib) != 11 or not oib.isdigit():
        return False
    a = 10
    for z in oib[:10]:
        a = (a + int(z)) % 10 or 10
        a = (a * 2) % 11
    k = 11 - a
    return (0 if k == 10 else k) == int(oib[10])


def vrijednosti(zaliha, nabavna, mpc, stopa):
    """Vrijednost zalihe za knjigovodstvo, u centima:
    netto i PDV po nabavnoj cijeni, netto i PDV po prodajnoj (MPC sadrži PDV)."""
    nab_netto = zaokruzi(zaliha * nabavna)
    pro_bruto = zaokruzi(zaliha * mpc)
    pro_pdv = pdv_iz_bruto(pro_bruto, stopa)
    return {"nab_netto": nab_netto, "nab_pdv": pdv_na_osnovicu(nab_netto, stopa),
            "pro_netto": pro_bruto - pro_pdv, "pro_pdv": pro_pdv}


# Stvarna zaliha = stanje iz Robe kod zadnjeg uvoza + naše primke - otpisi - povrati +/- inventure.
# Prodaja se broji preko Robe (kupac dobiva Robin račun), pa naša kasa zalihu ne mijenja.
# Plaćanja računa iz Robe, jedan red po načinu plaćanja: za račun plaćen na više načina
# (placanje = -2) dijelovi iz roba_placanja, inače način i iznos iz zaglavlja.
ROBA_PLACANJA_SQL = """(SELECT d.godina, d.sifra, d.status, d.datum, d.vrijeme, d.brisan,
       d.iznos AS iznos_racuna, COALESCE(p.placanje, d.placanje) AS placanje,
       COALESCE(p.iznos, d.iznos) AS iznos
   FROM roba_dokumenti d LEFT JOIN roba_placanja p
     ON d.placanje = -2 AND p.godina = d.godina AND p.link = d.sifra)"""

# Ulazni dokumenti iz Robe (nabava, pretporez): primke (status 1) bez međuskladišnica i
# povratnice dobavljaču (status 4) s minusom. Međuskladišnica je prijenos između vlastitih
# skladišta ili pogona (npr. "USKLADIŠNICA" od Norinčice), nije nabava i nema pretporeza.
ROBA_ULAZ_SQL = """(SELECT datum, partner, dokument, CASE status WHEN 1 THEN 1 ELSE 0 END AS primka,
       CASE status WHEN 4 THEN -neto ELSE neto END AS neto,
       CASE status WHEN 4 THEN -porez ELSE porez END AS porez
   FROM roba_dokumenti WHERE brisan = 0 AND (status = 4 OR (status = 1
     AND dokument NOT LIKE '%SKLAD%' AND dokument NOT LIKE '%USKL%'
     AND dokument NOT LIKE '%SKALD%')))"""

ZALIHA_SQL = """(a.zaliha + COALESCE((
    SELECT SUM(CASE WHEN d.vrsta IN ('otpis', 'povrat') THEN -s.kolicina ELSE s.kolicina END)
    FROM dok_stavke s JOIN dokumenti d ON d.id = s.dokument_id
    WHERE s.artikl_id = a.id), 0))"""


def zaokruzi(x):
    """Zaokruživanje na cijeli cent, pola uvijek od nule (matematički, kao u knjigovodstvu).
    Pythonov round() zaokružuje pola na parni broj (2,5 -> 2), a float zapis 1,005 je
    zapravo 1,00499..., pa obje stvari mogu dati cent razlike. Mala tolerancija to ispravlja."""
    x = x or 0.0
    if not math.isfinite(x):
        raise ValueError("neispravan broj")
    return int(math.floor(abs(x) + 0.5 + 1e-7)) * (1 if x >= 0 else -1)


def centi(x):
    """Euri (float) -> centi (int), zaokruženo na najbliži cent."""
    return zaokruzi((x or 0.0) * 100)


def pdv_iz_bruto(iznos_c, stopa):
    """PDV sadržan u bruto iznosu (centi)."""
    return zaokruzi(iznos_c * stopa / (100.0 + stopa))


def pdv_na_osnovicu(osnovica_c, stopa):
    """PDV na osnovicu (centi). Računa se na zbroj osnovica po stopi, ne po stavci
    (tako i eRačun, norma EN 16931, pravilo BR-S-09)."""
    return zaokruzi(osnovica_c * stopa / 100.0)
