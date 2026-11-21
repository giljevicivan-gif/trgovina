# -*- coding: utf-8 -*-
"""
Postavke: koji su dodatni moduli uključeni.

Program koriste različite trgovine, pa se ono što ne treba svima uključuje
ili isključuje ovdje. Zakonske obveze koje vrijede za svaku trgovinu na malo
(sidrena cijena, najniža cijena u 30 dana kod akcije, cijena za jedinicu mjere,
"OVO NIJE FISKALIZIRANI RAČUN") nisu modul i ne mogu se isključiti.
Isključen modul ne briše podatke; kad se ponovno uključi, sve je tu.
"""

from nabava import Greska

# kljuc, naziv, opis, uključen u novoj instalaciji
MODULI = (
    ("narudzbe", "Narudžbe kupaca", "Narudžbe i otpremnice za restorane, škole i sl.", True),
    ("kupci_tvrtke", "Kupci na tvrtku i cjenik po kupcu",
     "Rabat i ugovorene cijene po kupcu, zbirni pregled isporuka za račun u Robi (radi uz narudžbe).", False),
    ("inventura_mobitel", "Inventura s mobitela", "Brojanje robe s mobitela preko WiFi-ja.", True),
    ("akcije", "Prijedlog akcija", "Akcije za robu koja stoji (pravilo najniže cijene u 30 dana vrijedi uvijek).", True),
    ("usporedba", "Usporedba dobavljača", "Tko je za koji artikl jeftiniji.", True),
    ("voce_povrce", "Voće i povrće", "Zemlja podrijetla i klasa na etiketama i u Kontroli (Pravilnik NN 156/2025).", True),
    ("haccp", "HACCP dnevnik", "Temperature hladnjaka, čišćenje i prijem robe, s ispisom za sanitarnu inspekciju.", False),
    ("rokovi", "Rokovi trajanja i kalo", "Upozorenje prije isteka roka na pakiranoj robi i pregled kala (gubitka) po artiklu.", False),
    ("bonovi", "Poklon bonovi", "Evidencija bonova: broj, iznos, rok, koliko je ostalo. Bon se prodaje i naplaćuje u Robi.", False),
    ("vjernost", "Kartica vjernosti", "Bodovi za kupnje i nagrada popust, uz privolu člana (GDPR).", False),
    ("roba_sidrena", "Sidrena cijena iz Robe",
     "Kad je Roba ima (novija verzija), etiketa koristi Robinu sidrenu cijenu; ručno upisana kod nas ostaje.", False),
    ("zpc", "Zapisnici o promjeni cijena iz Robe",
     "Povijest cijena po artiklu i točnija najniža cijena u 30 dana kod akcije.", False),
    ("kpd", "KPD šifre (eRačun)",
     "KPD šifra na artiklu (upis kod nas, prijedlog od sličnih artikala, popis artikala bez šifre) i provjera "
     "da artikli prodani tvrtkama imaju šifru. Šifre potvrdite s knjigovođom.", False),
    ("r1_kasa", "R1 račun na kasi (kupac tvrtka)",
     "Na kasi gumb R1: naziv, adresa i OIB tvrtke na računu. Poreznoj ide isti fiskalni račun. "
     "Potvrdite s knjigovođom da je za tvrtku koja plaća na kasi to dovoljno.", False),
    ("eracun_uvoz", "Primka iz eRačuna dobavljača",
     "Učitajte XML eRačuna (UBL 2.1) i program sam popuni primku: dobavljač, broj, stavke i cijene. "
     "Ništa se ne šalje Poreznoj.", False),
    ("ponude", "Ponude i broj narudžbenice",
     "Ponude kupcima (pretvaraju se u narudžbu) i broj narudžbenice kupca na otpremnici (radi uz narudžbe).", False),
    ("dugovanja", "Računi dobavljača i dugovanja", "Dospijeće i plaćanje računa dobavljača (primke), tko koliko duguje i što kasni.", False),
    ("prijedlog_dan", "Prijedlog narudžbe po danima",
     "Prijedlog narudžbe gleda koliko se prodaje koji dan u tjednu, praznike i (ako upišete mjesto) vremensku prognozu.", False),
    ("snizenje", "Brzo sniženje robe koja vene",
     "Popust jednim klikom za robu koja se kvari, etiketa sa sniženjem i koliko je spašeno od otpisa.", False),
    ("ambalaza", "Gajbe i povratna ambalaža",
     "Koliko gajbi i paleta duguješ kojem dobavljaču i koliko je vraćeno, s potvrdom za vozača.", False),
    ("lanci", "Usporedba s cijenama lanaca",
     "Vaše MPC prema javnim cjenicima lanaca (Konzum, Lidl, Plodine, Spar...). Treba internet.", False),
    ("brzo_placanje", "Brže plaćanje na kasi",
     "F5 = gotovina, F6 = kartica (samo na kartici Kasa) i gumbi Točno / 10 / 20 / 50 € za primljeni iznos.", False),
    ("kg_upozorenje", "Upozorenje za veliku težinu",
     "Pita za potvrdu kad se na kasi upiše više od 50 kg (npr. 1250 upisano u gramima umjesto 1,25).", True),
    ("podijeljeno_placanje", "Plaćanje dijelom gotovinom, dijelom karticom",
     "Na kasi gumb 'Gotovina + kartica'. Fiskalno se šalje kao način plaćanja 'O' (ostalo): potvrdite s knjigovođom.", False),
    ("povrat_ambalaze", "Povrat ambalaže na kasi (povratna naknada)",
     "Gumb na kasi: kupac vrati prazne boce i limenke, isplati mu se povratna naknada (0,10 € po komadu, "
     "iznos po vrsti se može promijeniti) iz ladice uz potvrdu (nije račun). Upisuje se kao isplata u Blagajni; "
     "pregled po danu i mjesecu za Fond (CSV). Potvrdite s knjigovođom i Fondom kako se vodi i potražuje "
     "isplaćena naknada.", False),
    ("povrat_tetrapak", "Tetrapak u povratnoj naknadi (od 1. 1. 2027.)",
     "U Povratu ambalaže dodaje vrstu Tetrapak (višeslojni karton, 0,10 € po komadu). Isplata je moguća tek od "
     "1. 1. 2027.; u Pregledu za Fond tetrapak je zasebno. Radi uz modul Povrat ambalaže.", False),
    ("zaslon_kupca", "Zaslon za kupca",
     "Drugi prozor (drugi monitor) ili tablet u WiFi mreži kupcu pokazuje stavke, ukupno i koliko mu se vraća.", False),
    ("vaga_veza", "Vaga spojena s kasom",
     "Kasa sama očita težinu s vage (COM port ili mreža): gumb Vaga (F4) kod artikla na kg. "
     "Postavke veze i probno očitanje ispod popisa modula.", False),
    ("termo_pisac", "Izravni ispis na termalni pisač",
     "Račun ide ravno na termalni pisač 80/58 mm (ESC/POS: Windows pisač, USB bez drivera, mreža ili COM) bez prozora za ispis, "
     "s QR kodom, rezanjem papira i otvaranjem ladice kod gotovine. Kad pisač ne radi, otvara se obični ispis.", False),
    ("uvoz_artikala", "Uvoz artikala druge trgovine",
     "Jednokratno preuzimanje artikala (šifra, naziv, barkod, cijena, PDV, KPD) iz Robe druge trgovine ili .zip kopije. "
     "Prvo pregled, upisuju se samo novi artikli, postojeći se ne mijenjaju. Panel ispod popisa modula.", False),
    ("qr_uplata", "Barkod za plaćanje na otpremnici",
     "Na otpremnici i zbirnom pregledu za tvrtke 2D barkod (HUB-3) koji kupac skenira mobilnim bankarstvom. "
     "Treba IBAN (Postavke). Plaćanje po otpremnici prije računa potvrdite s knjigovođom.", False),
    ("narudzba_mail", "Narudžba dobavljaču e-mailom",
     "U Prijedlogu narudžbe gumb za slanje narudžbe dobavljaču e-mailom (SMTP iz večernjeg sažetka).", False),
    ("kopija_van", "Kopija baze izvan računala",
     "Jednom dnevno (i kod zaključka smjene) provjerena kopija baze na USB disk ili u OneDrive / Google Drive mapu, "
     "po želji i u drugu mapu (npr. USB i OneDrive). Na kartici Danas crveno upozorenje ako kopije nema 2 dana.", False),
    ("kopija_dojava", "Dojava kad kopija izostane",
     "Kad kopija izvan računala ne uspije ili je nema 2 dana: crvena traka na kasi, crveno u Kontroli i jednom dnevno "
     "e-mail vlasniku (SMTP iz večernjeg sažetka). Radi uz modul Kopija baze izvan računala.", False),
    ("donacije", "Donacije hrane",
     "Evidencija donirane hrane (smanjuje zalihu kao otpis), zapisnik za potpis i godišnji zbroj prema 2 % prihoda. "
     "Potvrdite s knjigovođom.", False),
    ("radnici", "Radnici s PIN-om",
     "Svaki radnik ulazi svojim PIN-om, vidi se tko je što napravio, a radnik bez ovlasti ne vidi nabavne cijene ni maržu.", False),
    ("fiskalizacija", "Fiskalizacija računa (kasa)",
     "Kasa šalje račune Poreznoj (FINA certifikat). Prvo na testu s demo certifikatom; prava fiskalizacija tek uz potvrdu.", False),
    ("sazetak", "Večernji sažetak na e-mail",
     "Kod zaključka smjene (ili u zadano vrijeme) stiže e-mail s prometom, blagajnom i što treba naručiti. Treba internet.", False),
    ("sazetak_stanje", "Sažetak: stanje kase i kopije",
     "U večernji e-mail dodaje odjeljak STANJE: fiskalni računi danas i koliko ih je bez JIR-a, kopija izvan računala, "
     "sat računala i manjak u ladici zadnjih 7 dana. Radi uz Večernji sažetak.", False),
    ("kasa_upozorenja", "Rok i minimalna zaliha na kasi",
     "Kod dodavanja artikla u košaricu: zabrana i potvrda za istekao rok \"upotrijebiti do\", upozorenje za "
     "\"najbolje upotrijebiti do\" i rok koji uskoro ističe (modul Rokovi), i poruka kad zaliha padne ispod "
     "minimalne.", False),
)
ZADANO = {m[0]: m[3] for m in MODULI}

SHEMA = """
CREATE TABLE IF NOT EXISTS postavke (
    kljuc      TEXT PRIMARY KEY,
    vrijednost TEXT NOT NULL
);
"""


def pripremi(con):
    con.executescript(SHEMA)


def moduli(con):
    """{kljuc: True/False} za sve module."""
    spremljeno = {r[0]: r[1] for r in con.execute(
        "SELECT kljuc, vrijednost FROM postavke WHERE kljuc LIKE 'modul.%'")}
    return {k: spremljeno.get("modul." + k, "1" if z else "0") == "1" for k, z in ZADANO.items()}


def ukljucen(con, kljuc):
    return moduli(con).get(kljuc, False)


def popis(con):
    m = moduli(con)
    return [{"kljuc": k, "naziv": n, "opis": o, "ukljucen": m[k]} for k, n, o, _ in MODULI]


def spremi(con, p):
    """p = {kljuc: bool}. Nepoznati ključevi se odbijaju."""
    for k in p:
        if k not in ZADANO:
            raise Greska("Nepoznat modul: %s." % k)
    with con:
        for k, v in p.items():
            con.execute("INSERT OR REPLACE INTO postavke (kljuc, vrijednost) VALUES (?, ?)",
                        ("modul." + k, "1" if v in (True, 1, "1", "true") else "0"))


def provjeri(con, kljuc):
    """Greška ako je modul isključen (za API)."""
    if not ukljucen(con, kljuc):
        naziv = next(n for k, n, _, _ in MODULI if k == kljuc)
        raise Greska("Modul \"%s\" je isključen (gumb Postavke gore desno)." % naziv)


def vrijednost(con, kljuc, zadano=""):
    r = con.execute("SELECT vrijednost FROM postavke WHERE kljuc = ?", (kljuc,)).fetchone()
    return r[0] if r else zadano


def postavi(con, kljuc, v):
    with con:
        con.execute("INSERT OR REPLACE INTO postavke (kljuc, vrijednost) VALUES (?, ?)", (kljuc, str(v)))
