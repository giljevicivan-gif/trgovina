// Uvoz artikala druge trgovine (uvoz_artikala.py): kartica u panelu Postavke.
// Učitava se iza glavne skripte u kasa.html i koristi njezine pomoćne funkcije (api, esc, eur, toast, MODULI, osvjeziArtikle).
(function () {
  const box = document.getElementById('postavke');
  if (!box) return;
  const $id = id => document.getElementById(id);
  const br = n => Number(n).toLocaleString('hr-HR');
  const datumHr = iso => iso ? iso.slice(8, 10) + '.' + iso.slice(5, 7) + '.' + iso.slice(0, 4) + '.' : '';
  let zadnji = null;  // zadnji pregled (za gumb Uvezi)

  function forma() {
    const el = $id('ua-kartica');
    let putanja = '', izbor = 'prodani_zaliha';
    try { putanja = localStorage.getItem('ua-putanja') || ''; } catch (e) {}
    el.innerHTML = '<h2 style="margin-top:0">Uvoz artikala druge trgovine</h2>' +
      '<p class="napomena">Za trgovinu koja prelazi na ovaj program: artikli se jednom preuzmu iz njezine Robe. ' +
      'Robine datoteke se samo čitaju. Upisuju se samo novi artikli, postojeći se ne mijenjaju, a prije upisa se napravi sigurnosna kopija baze.</p>' +
      '<label style="display:block">Mapa Robe, mapa godine ili .zip kopija' +
      '<input id="ua-putanja" style="width:100%" placeholder="npr. C:\\Roba\\2025 ili C:\\Users\\...\\Downloads\\2025.zip" value="' + esc(putanja) + '"></label>' +
      '<div class="red" style="flex-wrap:wrap;gap:8px;margin-top:6px"><label>Koji artikli <select id="ua-izbor">' +
      [['prodani_zaliha', 'Prodavani u toj godini ili imaju zalihu'], ['zaliha', 'Samo artikli sa zalihom'], ['svi', 'Svi artikli']]
        .map(([k, n]) => '<option value="' + k + '"' + (k === izbor ? ' selected' : '') + '>' + n + '</option>').join('') + '</select></label>' +
      '<label style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="ua-zaliha"> preuzmi i količine na zalihi</label></div>' +
      '<div style="margin-top:6px"><label style="display:flex;gap:6px;align-items:center"><input type="radio" name="ua-nacin" value="dodaj" checked> Dodaj nove artikle (moji ostaju)</label>' +
      '<label style="display:flex;gap:6px;align-items:center"><input type="radio" name="ua-nacin" value="zamijeni"> Zamijeni moje artikle ovima (moji se brišu)</label></div>' +
      '<div class="red"><button class="btn" id="ua-pregled">Pregled</button></div><div id="ua-rez"></div><div id="ua-stanje"></div>';
    $id('ua-pregled').onclick = pregled;
    stanje();
  }

  async function stanje() {
    let S;
    try { S = await api('/api/uvoz_artikala'); } catch (e) { return; }
    const el = $id('ua-stanje');
    if (!el || !S.bez_artikala) return;
    el.innerHTML = '<p class="upoz">Artikli iz vaše Robe se više ne uvoze (zamijenjeni su). Prodaja i dokumenti iz Robe se i dalje čitaju.' +
      ' <button class="btn" id="ua-vrati">Opet uvozi artikle iz Robe</button></p>';
    $id('ua-vrati').onclick = async () => {
      try { await api('/api/uvoz_artikala/vrati', {}); toast('Artikli iz Robe se opet uvoze kod sljedećeg osvježavanja'); el.innerHTML = ''; }
      catch (e) { toast(e.message, true); }
    };
  }

  const podaci = () => ({putanja: $id('ua-putanja').value, izbor: $id('ua-izbor').value, zaliha: $id('ua-zaliha').checked,
    zamijeni: (document.querySelector('input[name=ua-nacin]:checked') || {}).value === 'zamijeni'});

  async function pregled() {
    const rez = $id('ua-rez'), b = $id('ua-pregled');
    b.disabled = true; rez.innerHTML = '<p class="napomena">Čitam Robu… (velika godina može trajati pola minute)</p>';
    let P;
    try { P = await api('/api/uvoz_artikala/pregled', podaci()); }
    catch (e) { rez.innerHTML = '<p class="minus">' + esc(e.message) + '</p>'; b.disabled = false; return; }
    b.disabled = false; zadnji = P;
    try { localStorage.setItem('ua-putanja', $id('ua-putanja').value); } catch (e) {}
    const T = P.trgovina, R = P.razlozi;
    const tablica = (redovi, razlog) => '<div style="overflow-x:auto"><table class="t" style="font-size:13px"><tr><th>Šifra</th><th>Naziv</th><th>Barkod</th>' +
      (razlog ? '<th>Zašto preskočen</th>' : '<th>JM</th><th class="r">MPC</th><th class="r">PDV</th>' + (P.zaliha ? '<th class="r">Zaliha</th>' : '')) + '</tr>' +
      redovi.map(s => '<tr><td>' + esc(s.sifra) + '</td><td>' + esc(s.naziv) + '</td><td>' + esc(s.barkod) + '</td>' +
        (razlog ? '<td>' + esc(s.razlog) + '</td>'
          : '<td>' + esc(s.jm) + '</td><td class="r" style="white-space:nowrap">' + eur(s.cijena) + '</td><td class="r" style="white-space:nowrap">' + s.pdv_stopa + '&nbsp;%</td>' +
            (P.zaliha ? '<td class="r">' + String(s.zaliha).replace('.', ',') + '</td>' : '')) + '</tr>').join('') + '</table></div>';
    rez.innerHTML =
      '<p><b>' + esc(T.naziv || 'Trgovina bez naziva u vulpes.ini') + '</b>' + (T.mjesto ? ', ' + esc(T.mjesto) : '') +
      (T.oib ? ', OIB ' + esc(T.oib) : '') + '<br><span class="napomena">Izvor: ' + esc(P.izvor) +
      (P.zadnja_promjena ? ', zadnja promjena artikla ' + datumHr(P.zadnja_promjena) : '.') +
      ' Ako su podaci stari, cijene provjerite u Robi te trgovine.</span></p>' +
      P.upozorenja.map(u => '<p class="minus">' + esc(u) + '</p>').join('') +
      '<table class="t"><tr><td>Artikala u toj Robi</td><td class="r">' + br(P.ukupno_u_robi) + '</td></tr>' +
      (P.izvan_izbora ? '<tr><td>Izvan odabira (nisu prodavani i nemaju zalihu)</td><td class="r">' + br(P.izvan_izbora) + '</td></tr>' : '') +
      '<tr><td><b>Novih za upis</b></td><td class="r"><b>' + br(P.novi) + '</b></td></tr>' +
      (R.sifra ? '<tr><td>Preskočeno: šifra već postoji u ovoj bazi</td><td class="r">' + br(R.sifra) + '</td></tr>' : '') +
      (R.barkod ? '<tr><td>Preskočeno: barkod već postoji u ovoj bazi</td><td class="r">' + br(R.barkod) + '</td></tr>' : '') +
      (R.bez_cijene ? '<tr><td>Preskočeno: bez naziva ili cijene</td><td class="r">' + br(R.bez_cijene) + '</td></tr>' : '') +
      (R.neaktivan ? '<tr><td>Preskočeno: neaktivni u Robi</td><td class="r">' + br(R.neaktivan) + '</td></tr>' : '') +
      '</table>' +
      '<p class="napomena">U ovoj bazi sada: ' + br(P.baza.artikala) + ' artikala. ' +
      (P.veza ? 'Artikli zadržavaju Robinu šifru, pa kasnije spajanje Robe te trgovine osvježava iste artikle.'
              : 'Artikli ulaze kao vaši vlastiti (bez veze s Robom), da ih uvoz vaše Robe nikad ne prepiše.') +
      (P.zaliha ? ' Zaliha se preuzima (' + br(P.sa_zalihom) + ' artikala s količinom); napravite inventuru prije prodaje.'
                : ' Zaliha ostaje 0 dok ne napravite inventuru ili primku.') + '</p>' +
      (P.novi ? '<details><summary>Prvih ' + P.primjer_novi.length + ' novih</summary>' + tablica(P.primjer_novi, false) + '</details>' : '') +
      (P.preskoceni ? '<details><summary>Preskočeni (prvih ' + P.primjer_preskoceni.length + ')</summary>' + tablica(P.primjer_preskoceni, true) + '</details>' : '') +
      (P.stari ? '<p class="minus"><b>Zamjena:</b> od vaših ' + br(P.stari.ukupno) + ' artikala briše se ' + br(P.stari.brisanje) +
        (P.stari.skrivanje ? ', a ' + br(P.stari.skrivanje) + ' ima povijest (računi, primke, inventure…) pa se samo skrivaju, da stari dokumenti ostanu ispravni' : '') +
        '. Računi, dokumenti, postavke i radnici ostaju. Prije toga se napravi sigurnosna kopija baze.' +
        (P.roba_mapa ? ' Artikli iz vaše Robe se poslije više neće uvoziti (može se vratiti ovdje).' : '') + '</p>' +
        '<label style="display:flex;gap:8px;align-items:flex-start;margin:8px 0"><input type="checkbox" id="ua-brisanje" style="margin-top:3px">' +
        '<span>Da, obriši moje artikle i stavi ove.</span></label>' : '') +
      (P.potvrda_potrebna ? '<label style="display:flex;gap:8px;align-items:flex-start;margin:8px 0"><input type="checkbox" id="ua-potvrda" style="margin-top:3px">' +
        '<span>Pročitao sam upozorenje i ipak želim artikle u <b>ovu</b> bazu.</span></label>' : '') +
      (P.novi ? '<div class="red"><button class="btn prim" id="ua-uvezi">' + (P.stari ? 'Zamijeni artikle (' + br(P.novi) + ' novih)' : 'Uvezi ' + br(P.novi) + ' novih artikala') + '</button></div>' : '');
    const u = $id('ua-uvezi');
    if (u) u.onclick = uvezi;
  }

  async function uvezi() {
    const b = $id('ua-uvezi');
    const p = Object.assign(podaci(), {potvrda: !!($id('ua-potvrda') && $id('ua-potvrda').checked),
      potvrda_brisanja: !!($id('ua-brisanje') && $id('ua-brisanje').checked)});
    if (zadnji && zadnji.potvrda_potrebna && !p.potvrda) { toast('Prvo označite kvačicu ispod upozorenja.', true); return; }
    if (zadnji && zadnji.stari && !p.potvrda_brisanja) { toast('Prvo označite kvačicu za brisanje svojih artikala.', true); return; }
    if (zadnji && !!zadnji.stari !== p.zamijeni) { toast('Promijenili ste način: kliknite ponovno Pregled.', true); return; }
    b.disabled = true; b.textContent = 'Upisujem…';
    try {
      const r = await api('/api/uvoz_artikala/uvezi', p);
      $id('ua-rez').innerHTML = '<p><b>Upisano ' + br(r.upisano) + ' novih artikala.</b> Preskočeno ' + br(r.preskoceno) + '.' +
        (r.obrisano || r.skriveno ? ' Vaših artikala obrisano ' + br(r.obrisano) + ', skriveno ' + br(r.skriveno) + '.' : '') + '</p>' +
        '<p class="napomena">Sigurnosna kopija prije uvoza: ' + esc(r.kopija) + '</p>';
      toast('Upisano ' + r.upisano + ' artikala'); stanje();
      if (typeof osvjeziArtikle === 'function') osvjeziArtikle();
    } catch (e) { toast(e.message, true); b.disabled = false; b.textContent = 'Uvezi'; }
  }

  // panel Postavke se crta iznova kod svakog otvaranja: dodaj karticu kad se pojavi
  new MutationObserver(() => {
    if (!MODULI.uvoz_artikala || box.hidden || $id('ua-kartica')) return;
    const spremi = $id('p-spremi');
    if (!spremi) return;
    const k = document.createElement('div');
    k.id = 'ua-kartica';
    k.style.cssText = 'border-top:1px solid var(--line);margin-top:12px;padding-top:8px';
    spremi.closest('.red').before(k);
    forma();
  }).observe(box, {childList: true, attributes: true, attributeFilter: ['hidden']});
})();
