// Kasa: povrat ambalaže (povrat_ambalaze.py) i zaslon za kupca (zaslon_kupca.py).
// Učitava se iza glavne skripte u kasa.html i koristi njezine pomoćne funkcije (api, $, esc, eur, toast, kosarica...).
// kasa.html zove: kasaKupacModuli(MODULI) iz primijeniModule(), zaslonPosalji() iz crtajKosaricu(),
// zaslonPlaceno(ukupno, vratiti) iz naplati().
(function () {
  const css = document.createElement('style');
  css.textContent = `
#kk-gumbi { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
#kk-gumbi[hidden] { display: none; }
#kk-gumbi .btn { flex: 1; padding: 8px 10px; font-size: 14px; white-space: nowrap; }
#kk-gumbi .btn.mali { flex: 0 0 auto; }
.kk-modal { position: fixed; inset: 0; background: rgba(0,0,0,.35); z-index: 40; display: flex; align-items: center; justify-content: center; }
.kk-modal > .card { width: 560px; max-width: calc(100% - 32px); max-height: 90vh; overflow: auto; }
.kk-modal h2 { margin: 0 0 6px; }
.kk-vrsta { display: grid; grid-template-columns: 1fr auto auto auto auto; gap: 8px; align-items: center; padding: 10px 0; border-bottom: 1px solid var(--line); }
.kk-vrsta b { font-size: 18px; } .kk-vrsta .napomena { display: block; }
.kk-vrsta button { width: 56px; height: 56px; font-size: 28px; font-weight: 700; border: 1px solid var(--line); border-radius: 10px; background: var(--panel); }
.kk-vrsta button.deset { font-size: 16px; }
.kk-vrsta input { width: 90px; height: 56px; font-size: 26px; text-align: center; border: 2px solid var(--accent); border-radius: 10px; }
.kk-vrsta .uk { grid-column: 1 / -1; text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; }
.kk-ukupno { display: flex; justify-content: space-between; align-items: baseline; margin: 12px 0; }
.kk-ukupno b { font-size: 34px; font-variant-numeric: tabular-nums; }
.kk-modal .gumbi { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
.kk-modal .gumbi .prim { flex: 1; font-size: 20px; padding: 12px; }
.kk-modal table.t td, .kk-modal table.t th { padding: 5px 8px; }
.kk-modal .mali { padding: 3px 8px; font-size: 12px; }
.kk-poz { padding: 6px 10px; border-radius: 8px; background: var(--warn-soft); color: var(--warn); font-size: 13px; margin: 8px 0; }
`;
  document.head.appendChild(css);

  // ---------------------------------------------------------------- gumbi na kasi
  const gumbi = document.createElement('div');
  gumbi.id = 'kk-gumbi'; gumbi.hidden = true;
  gumbi.innerHTML = '<button class="btn" id="kk-povrat" type="button" hidden>Povrat ambalaže</button>' +
    '<button class="btn" id="kk-zaslon" type="button" hidden>Otvori zaslon za kupca</button>' +
    '<button class="btn mali" id="kk-tablet" type="button" hidden title="Zaslon za kupca na tabletu u WiFi mreži">Tablet</button>';
  const opcije = document.querySelector('#v-kasa .desno .opcije');
  opcije.parentNode.insertBefore(gumbi, opcije.nextSibling);

  // kartica u Blagajni
  const karta = document.createElement('div');
  karta.className = 'card'; karta.id = 'kk-blagajna'; karta.hidden = true; karta.style.marginBottom = '16px';
  const dva = document.querySelector('#v-dan .dva');
  dva.parentNode.insertBefore(karta, dva.nextSibling);

  let M = {};
  window.kasaKupacModuli = function (m) {
    const bilo = M.zaslon_kupca;
    M = m || {};
    $('#kk-povrat').hidden = !M.povrat_ambalaze;
    $('#kk-zaslon').hidden = $('#kk-tablet').hidden = !M.zaslon_kupca;
    gumbi.hidden = !(M.povrat_ambalaze || M.zaslon_kupca);
    karta.hidden = !M.povrat_ambalaze;
    if (M.povrat_ambalaze) crtajKartu();
    if (M.zaslon_kupca && !bilo) zaslonPosalji();
  };
  $('#kk-povrat').onclick = () => otvoriPovrat();
  $('#kk-zaslon').onclick = () => {
    const w = window.open('/zaslon', 'zaslon_kupca', 'width=1024,height=600');
    if (!w) toast('Preglednik je blokirao novi prozor. Dopustite skočne prozore za kasu.', true);
    zaslonPosalji();
  };
  $('#kk-tablet').onclick = () => tablet();
  const tabDan = document.querySelector('.tabs button[data-v=dan]');
  if (tabDan) tabDan.addEventListener('click', () => { if (M.povrat_ambalaze) crtajKartu(); });

  function modal(html) {
    zatvori();
    const box = document.createElement('div');
    box.className = 'kk-modal'; box.id = 'kk-modal';
    box.innerHTML = '<div class="card">' + html + '</div>';
    box.addEventListener('keydown', e => {
      e.stopPropagation();  // F9 i ostale prečice kase ne vrijede dok je prozor otvoren
      if (e.key === 'Escape') zatvori();
    });
    box.addEventListener('click', e => { if (e.target === box) zatvori(); });
    document.body.appendChild(box);
    return box;
  }
  function zatvori() {
    const b = $('#kk-modal');
    if (b) { b.remove(); const t = $('#trazi'); if (t && $('#v-kasa').classList.contains('on')) t.focus(); }
  }
  const NAPOMENA = 'Potvrdite s knjigovođom i Fondom kako se vodi i potražuje isplaćena naknada.';
  const cijeli = v => { const n = parseInt(String(v).replace(/\D/g, ''), 10); return n > 0 ? Math.min(n, 5000) : 0; };
  const sp = (x, a, b) => x === 1 ? a : b;

  // ---------------------------------------------------------------- povrat ambalaže
  async function otvoriPovrat() {
    let P;
    try { P = await api('/api/povrat_amb'); } catch (e) { toast(e.message, true); return; }
    let ispis = true;
    try { ispis = localStorage.getItem('kk.ispis') !== '0'; } catch (e) { /* */ }
    const box = modal('<h2>Povrat ambalaže</h2><p class="napomena" style="margin:0">Kupac vraća prazne boce i limenke, ' +
      'isplaćuje mu se povratna naknada iz ladice. Ovo nije prodaja ni račun.</p>' +
      (P.vrste.length ? P.vrste.map(v => `<div class="kk-vrsta" data-v="${v.id}" data-iznos="${v.iznos}">` +
        `<span><b>${esc(v.naziv)}</b><span class="napomena">${eur(v.iznos)} € po komadu</span></span>` +
        '<button type="button" data-d="-1" title="Jedan manje">−</button><input inputmode="numeric" value="0" autocomplete="off">' +
        '<button type="button" data-d="1" title="Jedan više">+</button><button type="button" class="deset" data-d="10" title="Deset više">+10</button>' +
        '<span class="uk"></span></div>').join('')
        : '<p class="kk-poz">Nema nijedne vrste ambalaže. Dodajte je u "Vrste i iznosi".</p>') +
      '<div class="kk-ukupno"><span id="kk-kom">0 komada</span><span>Isplatiti: <b id="kk-uk">0,00</b> €</span></div>' +
      (P.smjena ? '<p class="napomena">Upisat će se kao isplata u Blagajni (otvorena smjena).</p>'
        : '<p class="kk-poz">Nema otvorene smjene u Blagajni: isplata se sprema, ali ne ulazi u blagajnu smjene.</p>') +
      `<label class="napomena"><input type="checkbox" id="kk-ispis" ${ispis ? 'checked' : ''}> Ispiši potvrdu za kupca</label>` +
      '<div class="gumbi"><button class="btn prim" id="kk-isplati" type="button" disabled>Isplati</button><button class="btn" id="kk-odustani" type="button">Odustani</button></div>' +
      '<div class="gumbi"><button class="btn mali" id="kk-pregled" type="button">Pregled za Fond (dan, mjesec, CSV)</button>' +
      '<button class="btn mali" id="kk-vrste" type="button">Vrste i iznosi</button></div>' +
      (P.zadnje.length ? '<h3 style="margin:14px 0 4px;font-size:15px">Zadnje isplate</h3><table class="t">' +
        P.zadnje.slice(0, 6).map(z => `<tr class="${z.ponisteno ? 'storn' : ''}"><td>${esc(z.oznaka)}</td><td>${datumHr(z.datum)} ${z.vrijeme.slice(11, 16)}</td>` +
          `<td class="r">${z.komada} kom</td><td class="r">${eur(z.ukupno)} €</td><td style="white-space:nowrap">` +
          `<button class="btn mali" data-isp="${z.id}" type="button">Ispiši</button> ` +
          (z.ponisteno ? '' : `<button class="btn mali opasno" data-pon="${z.id}" data-ozn="${esc(z.oznaka)}" type="button">Poništi</button>`) + '</td></tr>').join('') + '</table>' : '') +
      `<p class="napomena" style="margin-top:10px">${NAPOMENA}</p>`);
    const redovi = [...box.querySelectorAll('.kk-vrsta')];
    function zbroj() {
      let uk = 0, kom = 0;
      redovi.forEach(r => {
        const n = cijeli(r.querySelector('input').value), i = +r.dataset.iznos;
        kom += n; uk += n * i;
        r.querySelector('.uk').textContent = n ? `${n} × ${eur(i)} = ${eur(n * i)} €` : '';
      });
      $('#kk-uk').textContent = eur(uk);
      $('#kk-kom').textContent = kom + ' ' + sp(kom, 'komad', 'komada');
      $('#kk-isplati').disabled = !kom;
      return kom;
    }
    redovi.forEach(r => {
      const inp = r.querySelector('input');
      inp.addEventListener('focus', () => inp.select());
      inp.addEventListener('input', zbroj);
      r.querySelectorAll('button').forEach(b => b.onclick = () => {
        inp.value = Math.max(0, Math.min(5000, cijeli(inp.value) + (+b.dataset.d))); zbroj();
      });
    });
    box.addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.tagName === 'INPUT' && e.target.type !== 'checkbox') isplati(); });
    async function isplati() {
      if (!zbroj() || $('#kk-isplati').disabled) return;
      $('#kk-isplati').disabled = true;
      const ispisi = $('#kk-ispis').checked;
      try { localStorage.setItem('kk.ispis', ispisi ? '1' : '0'); } catch (e) { /* */ }
      try {
        const r = await api('/api/povrat_amb/isplati', {stavke: redovi.map(x => ({vrsta_id: +x.dataset.v, komada: cijeli(x.querySelector('input').value)}))});
        if (ispisi) window.open('/ispis_povrat_amb/' + r.id + '#ispis', 'ispispov', 'width=420,height=640');
        zatvori();
        toast(`Isplatiti kupcu ${eur(r.ukupno)} € (${r.oznaka})`);
        if (M.povrat_ambalaze) crtajKartu();
      } catch (e) { toast(e.message, true); $('#kk-isplati').disabled = false; }
    }
    $('#kk-isplati').onclick = isplati;
    $('#kk-odustani').onclick = zatvori;
    $('#kk-pregled').onclick = () => pregled();
    $('#kk-vrste').onclick = () => urediVrste();
    box.querySelectorAll('[data-isp]').forEach(b => b.onclick = () => window.open('/ispis_povrat_amb/' + b.dataset.isp + '#ispis', 'ispispov', 'width=420,height=640'));
    box.querySelectorAll('[data-pon]').forEach(b => b.onclick = async () => {
      if (!confirm(`Poništiti isplatu ${b.dataset.ozn}? Ako je smjena još otvorena, iznos se vraća u blagajnu.`)) return;
      try { await api('/api/povrat_amb/ponisti', {id: +b.dataset.pon}); toast('Isplata poništena'); otvoriPovrat(); crtajKartu(); }
      catch (e) { toast(e.message, true); }
    });
    zbroj();
    if (redovi.length) { const i = redovi[0].querySelector('input'); i.focus(); i.select(); }
  }

  const mjesec = (pomak) => {
    const d = new Date(); d.setDate(1); d.setMonth(d.getMonth() + pomak);
    const od = new Date(d.getFullYear(), d.getMonth(), 1), dok = new Date(d.getFullYear(), d.getMonth() + 1, 0);
    const iso = x => `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, '0')}-${String(x.getDate()).padStart(2, '0')}`;
    return [iso(od), pomak ? iso(dok) : danas()];
  };

  async function pregled(od, dok) {
    if (!od) [od, dok] = mjesec(0);
    let P;
    try { P = await api(`/api/povrat_amb/pregled?od=${od}&do=${dok}`); } catch (e) { toast(e.message, true); return; }
    const box = modal('<h2>Povratna naknada: pregled za Fond</h2>' +
      `<div class="red" style="margin:8px 0"><label>Od <input type="date" id="kk-od" value="${P.od}"></label><label>Do <input type="date" id="kk-do" value="${P.do}"></label>` +
      '<button class="btn mali" id="kk-p-danas" type="button">Danas</button><button class="btn mali" id="kk-p-mj" type="button">Ovaj mjesec</button>' +
      '<button class="btn mali" id="kk-p-pmj" type="button">Prošli mjesec</button></div>' +
      `<p>Isplata: <b>${P.isplata}</b> · komada: <b>${P.komada}</b> · isplaćeno: <b>${eur(P.ukupno)} €</b></p>` +
      '<h3 style="font-size:15px;margin:10px 0 4px">Po vrsti</h3><table class="t"><tr><th>Vrsta</th><th class="r">Po komadu</th><th class="r">Komada</th><th class="r">Iznos €</th></tr>' +
      (P.po_vrstama.length ? P.po_vrstama.map(r => `<tr><td>${esc(r.naziv)}</td><td class="r">${eur(r.iznos)}</td><td class="r">${r.komada}</td><td class="r">${eur(r.ukupno)}</td></tr>`).join('') +
        `<tr><td><b>Ukupno</b></td><td></td><td class="r"><b>${P.komada}</b></td><td class="r"><b>${eur(P.ukupno)}</b></td></tr>`
        : '<tr><td colspan="4" class="napomena">Nema isplata u tom razdoblju.</td></tr>') + '</table>' +
      (P.po_danima.length ? '<h3 style="font-size:15px;margin:12px 0 4px">Po danu</h3><div style="max-height:240px;overflow:auto"><table class="t"><tr><th>Datum</th><th>Vrsta</th><th class="r">Komada</th><th class="r">Iznos €</th></tr>' +
        P.po_danima.map(r => `<tr><td>${datumHr(r.datum)}</td><td>${esc(r.naziv)} (${eur(r.iznos)})</td><td class="r">${r.komada}</td><td class="r">${eur(r.ukupno)}</td></tr>`).join('') + '</table></div>' : '') +
      '<div class="gumbi"><button class="btn" id="kk-csv" type="button">Preuzmi CSV</button><button class="btn" id="kk-natrag" type="button">Natrag</button><button class="btn" id="kk-zatvori" type="button">Zatvori</button></div>' +
      `<p class="napomena" style="margin-top:10px">Poništene isplate se ne broje. ${NAPOMENA}</p>`);
    const opet = () => pregled($('#kk-od').value, $('#kk-do').value);
    $('#kk-od').onchange = opet; $('#kk-do').onchange = opet;
    $('#kk-p-danas').onclick = () => pregled(danas(), danas());
    $('#kk-p-mj').onclick = () => pregled(...mjesec(0));
    $('#kk-p-pmj').onclick = () => pregled(...mjesec(-1));
    $('#kk-csv').onclick = () => { location.href = `/povrat_amb.csv?od=${P.od}&do=${P.do}`; };
    $('#kk-natrag').onclick = () => otvoriPovrat();
    $('#kk-zatvori').onclick = zatvori;
    box.querySelector('.card').scrollTop = 0;
  }

  async function urediVrste() {
    let P;
    try { P = await api('/api/povrat_amb?sve=1'); } catch (e) { toast(e.message, true); return; }
    const red = v => `<tr data-id="${v ? v.id : ''}"><td><input class="kk-n" value="${v ? esc(v.naziv) : ''}" placeholder="npr. Boca/limenka" style="width:100%"></td>` +
      `<td><input class="kk-i" inputmode="decimal" value="${v ? eur(v.iznos) : '0,10'}" style="width:70px;text-align:right"></td>` +
      `<td><input type="checkbox" class="kk-a" ${!v || v.aktivan ? 'checked' : ''}></td><td><button class="btn mali kk-s" type="button">Spremi</button></td></tr>`;
    modal('<h2>Vrste ambalaže i iznos naknade</h2>' +
      '<p class="napomena">Zakonska povratna naknada je 0,10 € po komadu (od 1. 1. 2025., NN 137/2024). Ako se iznos promijeni ' +
      'ili Fond uvede posebnu vrstu, ovdje promijenite iznos ili dodajte vrstu. Stare isplate ostaju s iznosom iz svog dana.</p>' +
      '<table class="t"><tr><th>Vrsta</th><th>€ po kom</th><th>Aktivna</th><th></th></tr>' + P.vrste.map(red).join('') + red(null) + '</table>' +
      '<div class="gumbi"><button class="btn" id="kk-natrag" type="button">Natrag</button></div>');
    $('#kk-modal').querySelectorAll('.kk-s').forEach(b => b.onclick = async () => {
      const tr = b.closest('tr');
      try {
        await api('/api/povrat_amb/vrsta', {id: +tr.dataset.id || 0, naziv: tr.querySelector('.kk-n').value,
          iznos: tr.querySelector('.kk-i').value, aktivan: tr.querySelector('.kk-a').checked});
        toast('Spremljeno'); urediVrste();
      } catch (e) { toast(e.message, true); }
    });
    $('#kk-natrag').onclick = () => otvoriPovrat();
  }

  async function crtajKartu() {
    let P;
    try { P = await api('/api/povrat_amb'); } catch (e) { karta.innerHTML = ''; return; }
    const d = P.danas;
    karta.innerHTML = '<h2 style="margin-top:0">Povratna naknada za ambalažu</h2>' +
      `<p>Danas isplaćeno: <b>${eur(d.ukupno)} €</b> (${d.komada} kom, ${d.isplata} ${sp(d.isplata, 'isplata', 'isplata')})` +
      (d.po_vrstama.length ? ' · ' + d.po_vrstama.map(r => `${esc(r.naziv)}: ${r.komada}`).join(', ') : '') + '</p>' +
      '<p class="napomena">Isplate su u blagajni smjene upisane kao isplata "Povratna naknada PN-…".</p>' +
      '<div class="red" style="margin:0"><button class="btn" id="kk-k-isplata" type="button">Nova isplata</button>' +
      '<button class="btn" id="kk-k-pregled" type="button">Pregled za Fond (dan, mjesec, CSV)</button></div>';
    $('#kk-k-isplata').onclick = () => otvoriPovrat();
    $('#kk-k-pregled').onclick = () => pregled();
  }

  // ---------------------------------------------------------------- zaslon za kupca
  let tZaslon = null, placeno = false;
  function stanjeKosarice() {
    return {stanje: 'kosarica', ukupno: ukupno(), stavke: kosarica.map(s => ({
      naziv: s.a.naziv, kol: kolTxt(s.kol), jm: s.a.jm, cijena: s.a.cijena,
      popust: s.popust ? String(s.popust).replace('.', ',') : '', iznos: iznos(s)}))};
  }
  function salji(body) {
    fetch('/api/zaslon', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).catch(() => {});
  }
  window.zaslonPosalji = function () {
    if (!M.zaslon_kupca) return;
    clearTimeout(tZaslon);
    tZaslon = setTimeout(() => {
      // nakon naplate "Hvala" ostaje dok se ne počne nova košarica
      if (placeno && !kosarica.length) return;
      placeno = false;
      salji(stanjeKosarice());
    }, 150);
  };
  window.zaslonPlaceno = function (uk, vrati) {
    if (!M.zaslon_kupca) return;
    clearTimeout(tZaslon);
    placeno = true;
    salji({stanje: 'placeno', ukupno: uk, vratiti: vrati !== null && vrati >= 0 ? vrati : null});
  };

  async function tablet() {
    let S;
    try { S = await api('/api/zaslon/mreza'); } catch (e) { toast(e.message, true); return; }
    modal('<h2>Zaslon za kupca na tabletu</h2>' +
      '<p class="napomena">Na drugom monitoru: gumb "Otvori zaslon za kupca" i prozor prebacite na monitor kupca (F11 = cijeli zaslon).</p>' +
      '<p class="napomena">Tablet u istoj WiFi mreži: uključite ovdje i na tabletu otvorite adresu ispod. Tablet samo prikazuje ' +
      'košaricu (ništa ne može upisati ni vidjeti druge cijene). Ako Windows pita za vatrozid, dopustite privatnu mrežu.</p>' +
      (S.mreza ? `<div class="mob-adr"><div><span>Adresa za tablet</span><b>${esc(S.adresa)}</b></div><div><span>Stanje</span><b>${S.radi ? 'radi' : 'ne radi'}</b></div></div>`
        : `<p>Zaslon za tablet je <b>isključen</b> (port ${S.port}).</p>`) +
      '<div class="gumbi">' + (S.mreza ? '<button class="btn opasno" id="kk-mreza" data-v="0" type="button">Isključi tablet</button>'
        : '<button class="btn prim" id="kk-mreza" data-v="1" type="button">Uključi zaslon za tablet</button>') +
      '<button class="btn" id="kk-zatvori" type="button">Zatvori</button></div>');
    $('#kk-mreza').onclick = async () => {
      try {
        const r = await api('/api/zaslon/mreza', {mreza: $('#kk-mreza').dataset.v === '1'});
        if (r.greska_mreze) toast(r.greska_mreze, true);
        tablet();
      } catch (e) { toast(e.message, true); }
    };
    $('#kk-zatvori').onclick = zatvori;
  }

  if (typeof MODULI === 'object' && Object.keys(MODULI).length) kasaKupacModuli(MODULI);
})();
