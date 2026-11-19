// Dodatni moduli (dodaci.py): kartica "Dodaci", prijava radnika PIN-om, prijedlog po danima.
// Učitava se iza glavne skripte u kasa.html i koristi njezine pomoćne funkcije (api, esc, eur, toast...).
(function () {
  const DOD = {stanje: null};
  const nizi = s => String(s).replace('.', ',');
  const broj = s => parseFloat(String(s).replace(',', '.'));

  // ---------------------------------------------------------------- stil i kostur
  const css = document.createElement('style');
  css.textContent = `
#prijava { position: fixed; inset: 0; background: var(--bg); z-index: 50; display: flex; align-items: center; justify-content: center; }
#prijava .card { width: 360px; max-width: calc(100% - 32px); text-align: center; }
#prijava .imena { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin: 12px 0; }
#prijava .imena button.on { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); font-weight: 600; }
#prijava input { font-size: 28px; width: 180px; text-align: center; letter-spacing: 6px; padding: 8px; border: 1px solid var(--line); border-radius: 8px; }
#prijava .tipke { display: grid; grid-template-columns: repeat(3, 72px); gap: 8px; justify-content: center; margin: 12px 0; }
#prijava .tipke button { font-size: 22px; padding: 12px 0; }
#radnik-znak { font-size: 13px; color: var(--muted); white-space: nowrap; }
#v-dodaci .card { margin-bottom: 16px; }
#v-dodaci .card > h2 { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }
#v-dodaci .polja { display: flex; gap: 10px; flex-wrap: wrap; align-items: end; margin: 8px 0; }
#v-dodaci .polja label { display: flex; flex-direction: column; font-size: 13px; color: var(--muted); gap: 3px; }
#v-dodaci .polja input, #v-dodaci .polja select { padding: 7px 9px; border: 1px solid var(--line); border-radius: 8px; color: var(--ink); background: var(--panel); }
#v-dodaci .plus { color: var(--accent); } #v-dodaci .mali { padding: 3px 9px; font-size: 13px; }
#v-dodaci pre { white-space: pre-wrap; background: var(--bg); padding: 10px; border-radius: 8px; font-size: 13px; max-height: 420px; overflow: auto; }
.d-dani { display: flex; gap: 6px; flex-wrap: wrap; margin: 6px 0 12px; }
.d-dani span { background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 4px 8px; font-size: 13px; }
.d-dani span.nula { color: var(--muted); text-decoration: line-through; } .d-dani span.vise { border-color: var(--accent); }
`;
  document.head.appendChild(css);

  const nav = document.querySelector('.tabs');
  const tabBtn = document.createElement('button');
  tabBtn.dataset.v = 'dodaci'; tabBtn.textContent = 'Dodaci'; tabBtn.hidden = true;
  nav.insertBefore(tabBtn, nav.querySelector('[data-v=kontrola]'));
  const view = document.createElement('section');
  view.className = 'view'; view.id = 'v-dodaci';
  view.innerHTML = '<div id="d-snizenje" class="card" hidden></div><div id="d-ambalaza" class="card" hidden></div>' +
    '<div id="d-lanci" class="card" hidden></div><div id="d-prijedlog" class="card" hidden></div>' +
    '<div id="d-radnici" class="card" hidden></div><div id="d-sazetak" class="card" hidden></div><div id="d-fiskal" class="card" hidden></div>';
  document.querySelector('main').appendChild(view);
  tabBtn.addEventListener('click', () => {
    document.querySelectorAll('.tabs button').forEach(x => x.classList.toggle('on', x === tabBtn));
    document.querySelectorAll('.view').forEach(v => v.classList.toggle('on', v === view));
    ucitajDodatke();
  });
  const znak = document.createElement('span');
  znak.id = 'radnik-znak'; znak.hidden = true;
  document.querySelector('.top').insertBefore(znak, $('#znacka'));

  // ---------------------------------------------------------------- prijava radnika
  const origFetch = window.fetch.bind(window);
  window.fetch = async function (url, opt) {
    // uključivanje radnika bez vlasnika: prvo upiši vlasnika
    if (url === '/api/postavke' && opt && opt.method === 'POST') {
      try {
        const b = JSON.parse(opt.body);
        if (b.radnici && DOD.stanje && !DOD.stanje.ima_vlasnika) {
          const ok = await prviVlasnik();
          if (!ok) return new Response(JSON.stringify({greska: 'Radnici nisu uključeni: vlasnik nije upisan.'}), {status: 400});
        }
      } catch (e) { /* nije JSON */ }
    }
    const r = await origFetch(url, opt);
    if (r.status === 401 && String(url).startsWith('/api/')) {
      try { const j = await r.clone().json(); if (j.prijava) prikaziPrijavu(); } catch (e) { /* */ }
    }
    return r;
  };

  async function osvjeziStanje() {
    try { DOD.stanje = await api('/api/dodaci/stanje'); } catch (e) { return; }
    const S = DOD.stanje;
    if (S.prijava) prikaziPrijavu();
    const r = S.radnik;
    znak.hidden = !r;
    if (r) {
      znak.innerHTML = `${esc(r.ime)}${r.vlasnik ? ' (vlasnik)' : ''} · <a href="#" id="odjava">Odjava</a>`;
      $('#odjava').onclick = async e => { e.preventDefault(); await origFetch('/api/radnici/odjava', {method: 'POST', body: '{}'}); location.reload(); };
    }
    primijeniOvlasti();
  }
  const smije = o => { const r = DOD.stanje && DOD.stanje.radnik; return !r || r.vlasnik || r.ovlasti.includes(o); };

  function primijeniOvlasti() {
    if (!DOD.stanje) return;
    const m = DOD.stanje.moduli || {};
    const tab = v => document.querySelector(`.tabs button[data-v=${v}]`);
    if (m.radnici && DOD.stanje.radnik) {
      if (!smije('nabava')) tab('nabava').hidden = true;
      if (!smije('zaliha')) tab('zaliha').hidden = true;
      if (!smije('izvjestaji')) { tab('izvjestaji').hidden = true; tab('kontrola').hidden = true; }
      $('#postavke-gumb').hidden = !smije('postavke');
    } else $('#postavke-gumb').hidden = false;
    tabBtn.hidden = !(m.snizenje || m.ambalaza || (m.lanci && smije('cijene')) || (m.prijedlog_dan && smije('nabava')) ||
                      (smije('postavke') && (m.radnici || m.sazetak || m.fiskalizacija)));
    const on = document.querySelector('.tabs button.on');
    if (on && on.hidden) tab('danas').click();
  }
  // glavna skripta zove primijeniModule nakon svake promjene postavki
  const origModuli = window.primijeniModule;
  window.primijeniModule = function () { origModuli(); osvjeziStanje(); };

  let odabraniRadnik = null;
  function prikaziPrijavu() {
    if ($('#prijava')) return;
    const S = DOD.stanje || {radnici: []};
    const box = document.createElement('div');
    box.id = 'prijava';
    box.innerHTML = '<div class="card"><h2>Prijava</h2><p class="napomena">Odaberite svoje ime i upišite PIN.</p>' +
      '<div class="imena">' + (S.radnici || []).map(r => `<button class="btn" data-r="${r.id}">${esc(r.ime)}</button>`).join('') + '</div>' +
      '<input id="pr-pin" type="password" inputmode="numeric" autocomplete="off" maxlength="8">' +
      '<div class="tipke">' + [1, 2, 3, 4, 5, 6, 7, 8, 9, '⌫', 0, 'OK'].map(t => `<button class="btn" data-t="${t}">${t}</button>`).join('') + '</div>' +
      '<p class="minus" id="pr-greska"></p></div>';
    document.body.appendChild(box);
    const odaberi = id => { odabraniRadnik = id; box.querySelectorAll('[data-r]').forEach(b => b.classList.toggle('on', +b.dataset.r === id)); $('#pr-pin').focus(); };
    box.querySelectorAll('[data-r]').forEach(b => b.onclick = () => odaberi(+b.dataset.r));
    if ((S.radnici || []).length === 1) odaberi(S.radnici[0].id);
    const posalji = async () => {
      if (!odabraniRadnik) { $('#pr-greska').textContent = 'Odaberite ime.'; return; }
      try {
        await api('/api/radnici/prijava', {id: odabraniRadnik, pin: $('#pr-pin').value});
        location.reload();
      } catch (e) { $('#pr-greska').textContent = e.message; $('#pr-pin').value = ''; }
    };
    box.querySelectorAll('[data-t]').forEach(b => b.onclick = () => {
      const t = b.dataset.t, i = $('#pr-pin');
      if (t === 'OK') posalji(); else if (t === '⌫') i.value = i.value.slice(0, -1); else if (i.value.length < 8) i.value += t;
    });
    $('#pr-pin').addEventListener('keydown', e => { if (e.key === 'Enter') posalji(); });
    if (!(S.radnici || []).length) osvjeziStanje().then(() => { box.remove(); if (DOD.stanje && DOD.stanje.prijava) prikaziPrijavu(); });
  }

  function prviVlasnik() {
    return new Promise(rijesi => {
      const box = document.createElement('div');
      box.id = 'prijava';
      box.innerHTML = '<div class="card"><h2>Prvo upišite vlasnika</h2><p class="napomena">Vlasnik vidi i smije sve, a radnike i njihove ovlasti dodaje u kartici Dodaci. ' +
        'PIN zapamtite: bez njega se program ne može otvoriti.</p>' +
        '<p><input id="pv-ime" placeholder="Ime" style="letter-spacing:0;font-size:18px;width:240px"></p>' +
        '<p><input id="pv-pin" type="password" inputmode="numeric" placeholder="PIN (4-8 brojki)" maxlength="8" style="font-size:18px;letter-spacing:2px;width:240px"></p>' +
        '<p><input id="pv-pin2" type="password" inputmode="numeric" placeholder="Ponovite PIN" maxlength="8" style="font-size:18px;letter-spacing:2px;width:240px"></p>' +
        '<p class="minus" id="pv-greska"></p><div class="red" style="justify-content:center"><button class="btn prim" id="pv-da">Spremi</button><button class="btn" id="pv-ne">Odustani</button></div></div>';
      document.body.appendChild(box);
      $('#pv-ne').onclick = () => { box.remove(); rijesi(false); };
      $('#pv-da').onclick = async () => {
        if ($('#pv-pin').value !== $('#pv-pin2').value) { $('#pv-greska').textContent = 'PIN-ovi nisu isti.'; return; }
        try {
          await api('/api/radnici/prvi', {ime: $('#pv-ime').value, pin: $('#pv-pin').value});
          DOD.stanje.ima_vlasnika = true; box.remove(); rijesi(true);
        } catch (e) { $('#pv-greska').textContent = e.message; }
      };
      $('#pv-ime').focus();
    });
  }

  // ---------------------------------------------------------------- kartica Dodaci
  function ucitajDodatke() {
    const m = (DOD.stanje && DOD.stanje.moduli) || MODULI;
    const sekcije = [['snizenje', m.snizenje, ucitajSnizenje], ['ambalaza', m.ambalaza, ucitajAmbalazu],
      ['lanci', m.lanci && smije('cijene'), ucitajLance], ['prijedlog', m.prijedlog_dan && smije('nabava'), ucitajPrijedlogPostavke],
      ['radnici', m.radnici && smije('postavke'), ucitajRadnike], ['sazetak', m.sazetak && smije('postavke'), ucitajSazetak],
      ['fiskal', m.fiskalizacija && smije('postavke'), ucitajFiskal]];
    sekcije.forEach(([id, ukljucen, fn]) => { $('#d-' + id).hidden = !ukljucen; if (ukljucen) fn(); });
  }

  // ---- brzo sniženje
  let SN = null;
  async function ucitajSnizenje() {
    try { SN = await api('/api/snizenje'); } catch (e) { toast(e.message, true); return; }
    const box = $('#d-snizenje'), U = SN.ukupno;
    const traje = SN.snizenja.filter(s => s.u_tijeku), bilo = SN.snizenja.filter(s => !s.u_tijeku);
    const red = s => `<tr><td>${esc(s.naziv)}</td><td>-${nizi(s.postotak)} %</td><td class="r">${eur(s.najniza_30)} → <b>${eur(s.cijena)}</b></td>` +
      `<td>${datumHr(s.od.slice(0, 10))} ${s.od.slice(11, 16)}–${(s.zavrseno || s.do).slice(11, 16)}</td>` +
      `<td class="r">${kolTxt(s.prodano)} ${esc(s.jm)} · ${eur(s.prihod)} €${s.nepotpuno ? ' <span class="napomena" title="Podaci iz Robe nisu osvježeni do kraja sniženja">(još)</span>' : ''}</td>` +
      `<td class="r">${s.otpisano ? kolTxt(s.otpisano) + ' ' + esc(s.jm) : ''}</td>` +
      `<td class="r"><button class="btn mali" data-sn-et="${s.id}">Etiketa</button>${s.u_tijeku ? ` <button class="btn mali opasno" data-sn-kraj="${s.id}">Završi</button>` : ''}</td></tr>`;
    const zaglavlje = '<tr><th>Artikl</th><th>Popust</th><th class="r">Najniža 30 d → snižena</th><th>Vrijeme</th><th class="r">Prodano po sniženju</th><th class="r">Otpisano</th><th></th></tr>';
    box.innerHTML = '<h2>Brzo sniženje robe koja vene</h2>' +
      '<p class="napomena">Postotak se računa od najniže cijene u 30 dana, a snižena cijena ne može ispod nabavne s PDV-om. ' +
      '<b>Sniženu cijenu naplatite u Robi</b> (popust na stavci ili nova MPC), jer istaknuta cijena mora biti ista kao naplaćena.</p>' +
      (traje.length ? '<h3>Traje sada</h3><table class="t">' + zaglavlje + traje.map(red).join('') + '</table>' : '') +
      '<h3>Novo sniženje</h3><div class="polja"><label>Traži artikl<input id="sn-trazi" placeholder="npr. banana" autocomplete="off"></label>' +
      '<label>Do (prazno = danas do zatvaranja)<input id="sn-do" type="datetime-local"></label>' +
      '<label>Koliko robe (nije obvezno)<input id="sn-kol" inputmode="decimal" style="width:100px"></label></div>' +
      '<div class="scroll" style="max-height:300px"><table class="t" id="sn-kand"></table></div><div id="sn-potvrda"></div>' +
      (bilo.length ? `<h3>Zadnjih 30 dana</h3><p>Prodano po sniženju: <b>${eur(U.prihod)} €</b>` +
        (U.spaseno_nabavna !== undefined ? ` · roba koja bi inače išla u otpis vrijedila je <b class="plus">${eur(U.spaseno_nabavna)} €</b> po nabavnoj` : '') +
        (U.otpisano_nabavna ? ` · ipak otpisano za ${eur(U.otpisano_nabavna)} €` : '') + '.</p>' +
        '<div class="scroll" style="max-height:320px"><table class="t">' + zaglavlje + bilo.map(red).join('') + '</table></div>' : '');
    const crtajKand = () => {
      const q = norm($('#sn-trazi').value.trim());
      const lista = SN.kandidati.filter(a => !q || norm(a.naziv).includes(q) || a.sifra === q).slice(0, 60);
      $('#sn-kand').innerHTML = '<tr><th>Artikl</th><th class="r">Zaliha</th><th class="r" title="Za koliko dana se proda zaliha (prodaja zadnjih 14 dana)">Dosta za</th><th class="r">MPC</th><th class="r">Sniženje</th></tr>' +
        lista.map(a => `<tr><td>${esc(a.naziv)}</td><td class="r">${kolTxt(a.zaliha)} ${esc(a.jm)}</td><td class="r">${a.dana_zalihe === null ? '' : nizi(a.dana_zalihe) + ' d'}</td>` +
          `<td class="r">${eur(a.cijena)}</td><td class="r">${SN.postoci.map(p => `<button class="btn mali" data-sn-a="${a.id}" data-p="${p}">-${p} %</button>`).join(' ')}</td></tr>`).join('');
      $('#sn-kand').querySelectorAll('[data-sn-a]').forEach(b => b.onclick = () => potvrdi(+b.dataset.snA, +b.dataset.p));
    };
    $('#sn-trazi').oninput = crtajKand; crtajKand();
    box.querySelectorAll('[data-sn-et]').forEach(b => b.onclick = () => window.open('/ispis_snizenje/' + b.dataset.snEt + '#ispis', 'snizenje', 'width=700,height=700'));
    box.querySelectorAll('[data-sn-kraj]').forEach(b => b.onclick = async () => {
      try { await api('/api/snizenje/zavrsi', {id: +b.dataset.snKraj}); toast('Sniženje završeno. Vratite cijenu u Robi.'); ucitajSnizenje(); } catch (e) { toast(e.message, true); }
    });
  }
  async function potvrdi(id, posto) {
    let I;
    try { I = await api(`/api/snizenje/izracun?id=${id}&posto=${posto}`); } catch (e) { toast(e.message, true); return; }
    const box = $('#sn-potvrda');
    box.innerHTML = `<div class="card" style="margin-top:12px;border-color:var(--accent)"><p><b>${esc(I.naziv)}</b>: najniža cijena u 30 dana ${eur(I.najniza_30)} €, -<input id="sn-posto" value="${nizi(I.postotak)}" style="width:60px"> % = <b>${eur(I.cijena)} €</b> / ${esc(I.jm)}</p>` +
      (I.ispod_nabavne ? `<p class="minus">To je ispod nabavne cijene s PDV-om. Najviše možete sniziti ${I.najvise_posto} %; inače otpis ili donacija.</p>` : `<p class="napomena">Najviše možete sniziti ${I.najvise_posto} % (do nabavne s PDV-om).</p>`) +
      '<div class="red"><button class="btn prim" id="sn-da">Pokreni i ispiši etiketu</button><button class="btn" id="sn-preracunaj">Preračunaj</button><button class="btn" id="sn-ne">Odustani</button></div></div>';
    $('#sn-ne').onclick = () => box.innerHTML = '';
    $('#sn-preracunaj').onclick = () => potvrdi(id, broj($('#sn-posto').value));
    $('#sn-da').onclick = async () => {
      try {
        const r = await api('/api/snizenje/pokreni', {artikl_id: id, postotak: broj($('#sn-posto').value), do: $('#sn-do').value, kolicina: $('#sn-kol').value});
        window.open('/ispis_snizenje/' + r.id + '#ispis', 'snizenje', 'width=700,height=700');
        toast('Sniženje pokrenuto: ' + r.opis + '. Naplatite tu cijenu u Robi.');
        ucitajSnizenje();
      } catch (e) { toast(e.message, true); }
    };
    box.scrollIntoView({block: 'nearest'});
  }

  // ---- gajbe i ambalaža
  async function ucitajAmbalazu() {
    let A, P;
    try { [A, P] = await Promise.all([api('/api/ambalaza'), api('/api/partneri')]); } catch (e) { toast(e.message, true); return; }
    const box = $('#d-ambalaza'), vrste = A.vrste.filter(v => v.aktivan);
    const dug = A.stanje.filter(d => d.komada);
    box.innerHTML = '<h2>Gajbe i povratna ambalaža <button class="btn mali" id="am-vrste-gumb">Vrste ambalaže</button></h2>' +
      '<div id="am-vrste" hidden></div>' +
      (dug.length ? '<table class="t"><tr><th>Dobavljač</th><th>Kod nas (treba vratiti)</th><th class="r">Vrijednost</th></tr>' + dug.map(d =>
        `<tr><td>${esc(d.partner)}</td><td>${d.vrste.filter(v => v.saldo).map(v => `${esc(v.vrsta)}: <b class="${v.saldo < 0 ? 'minus' : ''}">${v.saldo}</b>`).join(' · ')}</td>` +
        `<td class="r">${d.vrijednost ? eur(d.vrijednost) + ' €' : ''}</td></tr>`).join('') + '</table>' +
        '<p class="napomena">Minus znači da ste vratili više nego što je upisano da je stiglo (provjerite upise).</p>' : '<p class="napomena">Nema ambalaže koju dugujete.</p>') +
      '<h3>Upis (stiglo s robom / vraćeno vozaču)</h3><div class="polja">' +
      `<label>Dobavljač<select id="am-partner"><option value="">-</option>${P.map(p => `<option value="${p.id}">${esc(p.naziv)}</option>`).join('')}</select></label>` +
      `<label>Datum<input type="date" id="am-datum" value="${danas()}"></label><label>Napomena<input id="am-nap" style="width:220px"></label></div>` +
      '<table class="t" style="max-width:560px"><tr><th>Ambalaža</th><th class="r">Stiglo kom</th><th class="r">Vraćeno kom</th></tr>' +
      vrste.map(v => `<tr><td>${esc(v.naziv)}</td><td class="r"><input data-am-p="${v.id}" inputmode="numeric"></td><td class="r"><input data-am-v="${v.id}" inputmode="numeric"></td></tr>`).join('') + '</table>' +
      '<div class="red" style="margin-top:8px"><button class="btn prim" id="am-spremi">Spremi i ispiši potvrdu</button></div>' +
      '<h3>Zadnjih 90 dana</h3><div class="scroll" style="max-height:300px"><table class="t"><tr><th>Datum</th><th>Dobavljač</th><th>Ambalaža</th><th class="r">Stiglo</th><th class="r">Vraćeno</th><th>Napomena</th><th></th></tr>' +
      A.promet.map(m => `<tr${m.ponisten ? ' style="opacity:.5;text-decoration:line-through"' : ''}><td>${datumHr(m.datum)}</td><td>${esc(m.partner)}</td><td>${esc(m.vrsta)}</td>` +
        `<td class="r">${m.primljeno || ''}</td><td class="r">${m.vraceno || ''}</td><td>${esc(m.napomena)}${m.radnik ? ' <span class="napomena">· ' + esc(m.radnik) + '</span>' : ''}${m.ponisten ? ' <span class="napomena">poništeno ' + esc(m.ponisten) + '</span>' : ''}</td>` +
        `<td class="r">${m.ponisten ? '' : `<button class="btn mali" data-am-ispis="${m.id}">Potvrda</button> <button class="btn mali opasno" data-am-pon="${m.id}">Poništi</button>`}</td></tr>`).join('') + '</table></div>';
    $('#am-spremi').onclick = async () => {
      const stavke = vrste.map(v => ({vrsta_id: v.id, primljeno: box.querySelector(`[data-am-p="${v.id}"]`).value, vraceno: box.querySelector(`[data-am-v="${v.id}"]`).value}));
      try {
        const r = await api('/api/ambalaza/upisi', {partner_id: $('#am-partner').value, datum: $('#am-datum').value, napomena: $('#am-nap').value, stavke});
        window.open('/ispis_ambalaza?ids=' + r.ids.join(',') + '#ispis', 'ambalaza', 'width=800,height=800');
        toast('Upisano'); ucitajAmbalazu();
      } catch (e) { toast(e.message, true); }
    };
    box.querySelectorAll('[data-am-ispis]').forEach(b => b.onclick = () => window.open('/ispis_ambalaza?ids=' + b.dataset.amIspis, 'ambalaza', 'width=800,height=800'));
    box.querySelectorAll('[data-am-pon]').forEach(b => b.onclick = async () => {
      const razlog = prompt('Razlog poništavanja upisa:');
      if (!razlog) return;
      try { await api('/api/ambalaza/ponisti', {id: +b.dataset.amPon, razlog}); ucitajAmbalazu(); } catch (e) { toast(e.message, true); }
    });
    $('#am-vrste-gumb').onclick = () => {
      const v = $('#am-vrste'); v.hidden = !v.hidden; if (v.hidden) return;
      v.innerHTML = '<table class="t" style="max-width:560px"><tr><th>Naziv</th><th class="r">Polog €/kom</th><th>Aktivna</th><th></th></tr>' +
        A.vrste.map(x => `<tr data-vr="${x.id}"><td><input class="vr-n" value="${esc(x.naziv)}" style="width:200px;text-align:left"></td><td class="r"><input class="vr-v" value="${x.vrijednost ? eur(x.vrijednost) : ''}"></td>` +
          `<td><input type="checkbox" class="vr-a" ${x.aktivan ? 'checked' : ''} style="width:auto"></td><td><button class="btn mali">Spremi</button></td></tr>`).join('') +
        '<tr data-vr="0"><td><input class="vr-n" placeholder="Nova, npr. Gajba plava" style="width:200px;text-align:left"></td><td class="r"><input class="vr-v"></td><td></td><td><button class="btn mali">Dodaj</button></td></tr></table>';
      v.querySelectorAll('[data-vr]').forEach(r => r.querySelector('button').onclick = async () => {
        const a = r.querySelector('.vr-a');
        try { await api('/api/ambalaza/vrsta', {id: +r.dataset.vr, naziv: r.querySelector('.vr-n').value, vrijednost: r.querySelector('.vr-v').value, aktivan: a ? a.checked : true}); toast('Spremljeno'); ucitajAmbalazu(); } catch (e) { toast(e.message, true); }
      });
    };
  }

  // ---- cijene lanaca
  let LA = null, laTimer = null;
  async function ucitajLance() {
    const sve = $('#la-sve') && $('#la-sve').checked ? 1 : 0, samoSkuplji = $('#la-skuplji') && $('#la-skuplji').checked;
    try { LA = await api('/api/lanci?sve=' + sve); } catch (e) { toast(e.message, true); return; }
    const box = $('#d-lanci'), S = LA.stanje, I = S.info || {}, P = LA.postavke;
    const st = LA.stavke.filter(s => !samoSkuplji || s.razlika > LA.prag);
    box.innerHTML = '<h2>Usporedba s cijenama lanaca</h2>' +
      '<p class="napomena">Javni cjenici lanaca (obveza objave svaki dan), skupljeni na cijene.dev. Uspoređuje se najniža cijena u prodavaonicama u vašem mjestu; ' +
      'pakirana roba se povezuje sama po barkodu, a voće i povrće povežite jednom ručno (gumb Poveži). Za artikle na kg uspoređuje se cijena za kg.</p>' +
      `<div class="polja"><label>Mjesto (grad prodavaonica)<input id="la-mjesto" value="${esc(P.mjesto)}" placeholder="npr. Dubrovnik"></label>` +
      `<label>Samo lanci (prazno = svi)<input id="la-lanci" value="${esc(P.lanci)}" placeholder="npr. konzum, lidl, plodine" style="width:240px"></label>` +
      '<button class="btn" id="la-spremi">Spremi</button><button class="btn prim" id="la-preuzmi">Preuzmi cijene</button></div>' +
      `<p id="la-status" class="${S.greska ? 'minus' : 'napomena'}">${S.radi ? esc(S.poruka) : S.greska ? esc(S.greska) : I.uvezeno ? 'Zadnje preuzeto ' + esc(I.uvezeno) + (I.mjesto ? ' za ' + esc(I.mjesto) : ' (cijela Hrvatska)') + '. ' + esc(S.poruka || '') : 'Cijene još nisu preuzete (oko 80 MB s interneta, par minuta).'}</p>` +
      (LA.lanci.length ? `<p>Skuplji od najjeftinijeg lanca više od ${LA.prag} %: <b class="minus">${LA.skupljih}</b> · jeftiniji: <b class="plus">${LA.jeftinijih}</b></p>` +
        '<div class="red"><label><input type="checkbox" id="la-skuplji"' + (samoSkuplji ? ' checked' : '') + '> samo gdje smo skuplji</label>' +
        '<label><input type="checkbox" id="la-sve"' + (sve ? ' checked' : '') + '> prikaži i nepovezane artikle (za povezivanje)</label></div>' +
        '<div class="scroll" style="max-height:520px"><table class="t"><tr><th>Artikl</th><th class="r">Naša MPC</th>' + LA.lanci.map(l => `<th class="r">${esc(l)}</th>`).join('') + '<th class="r">Razlika</th><th></th></tr>' +
        st.map(s => `<tr><td>${esc(s.naziv)} <span class="napomena">${esc(s.jm)}</span></td><td class="r">${eur(s.cijena)}</td>` +
          LA.lanci.map(l => { const x = s.lanci[l]; if (!x) return '<td></td>'; if (!x.cijena) return `<td class="r napomena" title="${esc(x.kako)}">-</td>`;
            return `<td class="r" title="${esc(x.naziv)} (${esc(x.kako)}, ${x.trgovina} prod.${x.cijela_hr ? ', cijela HR' : ''})">${eur(x.cijena)}${x.akcija ? ' <span class="plus" title="akcija">(' + eur(x.akcija) + ')</span>' : ''}${x.cijela_hr ? '*' : ''}</td>`; }).join('') +
          `<td class="r ${s.razlika > LA.prag ? 'minus' : s.razlika < 0 ? 'plus' : ''}">${s.razlika === undefined ? '' : (s.razlika > 0 ? '+' : '') + nizi(s.razlika) + ' %'}</td>` +
          `<td><button class="btn mali" data-la-pov="${s.id}">Poveži</button></td></tr>`).join('') + '</table></div>' +
        '<p class="napomena">* lanac nema prodavaonicu u vašem mjestu, uzeta je najniža cijena u Hrvatskoj. U zagradi je akcijska cijena.</p>' : '') +
      '<div id="la-poveznica"></div>';
    $('#la-spremi').onclick = async () => { try { await api('/api/lanci/postavke', {mjesto: $('#la-mjesto').value, lanci: $('#la-lanci').value}); toast('Spremljeno; kliknite Preuzmi cijene'); } catch (e) { toast(e.message, true); } };
    $('#la-preuzmi').onclick = async () => {
      try { await api('/api/lanci/postavke', {mjesto: $('#la-mjesto').value, lanci: $('#la-lanci').value}); await api('/api/lanci/preuzmi', {}); pratiPreuzimanje(); } catch (e) { toast(e.message, true); }
    };
    if ($('#la-sve')) { $('#la-sve').onchange = ucitajLance; $('#la-skuplji').onchange = ucitajLance; }
    box.querySelectorAll('[data-la-pov]').forEach(b => b.onclick = () => povezi(LA.stavke.find(s => s.id === +b.dataset.laPov)));
    if (S.radi) pratiPreuzimanje();
  }
  function pratiPreuzimanje() {
    clearTimeout(laTimer);
    laTimer = setTimeout(async () => {
      let S;
      try { S = (await api('/api/lanci?sve=0')).stanje; } catch (e) { return; }
      if (!$('#la-status')) return;
      $('#la-status').textContent = S.radi ? S.poruka : (S.greska || S.poruka);
      $('#la-status').className = S.greska ? 'minus' : 'napomena';
      if (S.radi) pratiPreuzimanje(); else ucitajLance();
    }, 1500);
  }
  async function povezi(a) {
    const box = $('#la-poveznica');
    const prva = a.naziv.split(/\s+/)[0];
    box.innerHTML = `<div class="card" style="border-color:var(--accent)"><h3>Poveži: ${esc(a.naziv)} (${esc(a.jm)}, ${eur(a.cijena)} €)</h3>` +
      `<div class="polja"><label>Traži u cjenicima<input id="la-q" value="${esc(prva)}"></label><button class="btn" id="la-trazi">Traži</button><button class="btn" id="la-zatvori">Zatvori</button></div><div id="la-rez"></div></div>`;
    const trazi = async () => {
      let R;
      try { R = await api('/api/lanci/trazi?q=' + encodeURIComponent($('#la-q').value)); } catch (e) { toast(e.message, true); return; }
      const ima = a.lanci || {};
      $('#la-rez').innerHTML = R.rezultati.length ? '<div class="scroll" style="max-height:360px"><table class="t"><tr><th>Lanac</th><th>Proizvod</th><th class="r">Cijena</th><th class="r">Za kg/l</th><th></th></tr>' +
        R.rezultati.map((r, i) => `<tr><td>${esc(r.lanac)}</td><td>${esc(r.naziv)} <span class="napomena">${esc(r.marka || '')} ${esc(r.kolicina || '')} ${esc(r.jm || '')}</span></td>` +
          `<td class="r">${r.cijena_min ? eur(r.cijena_min) : ''}</td><td class="r">${r.jed_min ? eur(r.jed_min) : ''}</td>` +
          `<td><button class="btn mali" data-i="${i}">${ima[r.lanac] && ima[r.lanac].naziv === r.naziv ? 'Povezano' : 'Odaberi'}</button></td></tr>`).join('') + '</table></div>' +
        '<p class="napomena">Za svaki lanac odaberite jedan proizvod. Ručna veza zamjenjuje onu po barkodu.</p>' +
        Object.keys(ima).map(l => `<button class="btn mali" data-makni="${esc(l)}">Makni vezu: ${esc(l)}</button>`).join(' ') : '<p class="napomena">Ništa nije nađeno.</p>';
      $('#la-rez').querySelectorAll('[data-i]').forEach(b => b.onclick = async () => {
        const r = R.rezultati[+b.dataset.i];
        try { await api('/api/lanci/povezi', {artikl_id: a.id, lanac: r.lanac, sifra: r.sifra, naziv: r.naziv}); b.textContent = 'Povezano'; toast('Povezano: ' + r.lanac); } catch (e) { toast(e.message, true); }
      });
      $('#la-rez').querySelectorAll('[data-makni]').forEach(b => b.onclick = async () => {
        try { await api('/api/lanci/povezi', {artikl_id: a.id, lanac: b.dataset.makni, sifra: ''}); b.remove(); } catch (e) { toast(e.message, true); }
      });
    };
    $('#la-trazi').onclick = trazi; $('#la-q').onkeydown = e => { if (e.key === 'Enter') trazi(); };
    $('#la-zatvori').onclick = () => { box.innerHTML = ''; ucitajLance(); };
    trazi(); box.scrollIntoView({block: 'nearest'});
  }

  // ---- prijedlog po danima: postavke mjesta i prikaz dana u Nabavi
  async function ucitajPrijedlogPostavke() {
    let P;
    try { P = await api('/api/prijedlog_dan/postavke'); } catch (e) { toast(e.message, true); return; }
    const box = $('#d-prijedlog');
    box.innerHTML = '<h2>Prijedlog narudžbe po danima</h2><p class="napomena">Prijedlog u Nabavi sada gleda dan u tjednu, blagdane i (ako upišete mjesto) vremensku prognozu. ' +
      'Za prognozu upišite koordinate trgovine (Google Karte: desni klik na trgovinu). Prognoza dolazi s open-meteo.com, besplatno i bez prijave.</p>' +
      `<div class="polja"><label>Mjesto (za vas)<input id="pd-mjesto" value="${esc(P.mjesto)}" placeholder="Dubrovnik"></label>` +
      `<label>Širina<input id="pd-lat" value="${esc(nizi(P.lat))}" placeholder="42,65" style="width:90px"></label><label>Dužina<input id="pd-lon" value="${esc(nizi(P.lon))}" placeholder="18,09" style="width:90px"></label>` +
      '<button class="btn" id="pd-spremi">Spremi</button></div>';
    $('#pd-spremi').onclick = async () => { try { await api('/api/prijedlog_dan/postavke', {mjesto: $('#pd-mjesto').value, lat: $('#pd-lat').value, lon: $('#pd-lon').value}); toast('Spremljeno'); } catch (e) { toast(e.message, true); } };
  }
  const origPrijedlog = window.prikaziPrijedlog;
  window.prikaziPrijedlog = async function () {
    await origPrijedlog();
    if (!PRIJ || !PRIJ.po_danima || !$('#n-desno')) return;
    const nap = $('#n-desno').querySelector('p.napomena');
    const F = PRIJ.faktori, div = document.createElement('div');
    div.innerHTML = '<p class="napomena">Po danima: prodaja tog dana u tjednu (zadnjih 8 tjedana), blagdani' + (PRIJ.vrijeme ? ' i vremenska prognoza' : '') + '. Za koje dane se naručuje:</p>' +
      '<div class="d-dani">' + PRIJ.dani.map(d => `<span class="${d.faktor === 0 ? 'nula' : d.faktor > 1 ? 'vise' : ''}" title="${esc(d.razlog)}">${d.dan} ${datumHr(d.datum).slice(0, -5)}` +
        `${d.tmax !== null && d.tmax !== undefined ? ' · ' + Math.round(d.tmax) + '°' : ''}${d.oborine ? ' · ' + nizi(Math.round(d.oborine)) + ' mm' : ''}${d.razlog ? ' · ' + esc(d.razlog) : ''}${d.tezina < 1 ? ' (pola)' : ''}</span>`).join('') + '</div>' +
      (PRIJ.greska_vremena ? `<p class="napomena">${esc(PRIJ.greska_vremena)}</p>` : '') +
      `<p class="napomena">Iz povijesti:${F.prije_blagdana !== 1 ? ' dan prije blagdana ×' + nizi(F.prije_blagdana) : ' dan prije blagdana se ne prodaje više'}${F.n_vruce >= 5 ? ', vruće ×' + nizi(F.vruce) : ''}${F.n_kisa >= 5 ? ', kiša ×' + nizi(F.kisa) : ''}` +
      `${F.blagdana_u_povijesti ? '; na blagdane trgovina ' + (F.blagdana_radila ? 'radi' : 'ne radi') : ''}. "Prodaja/dan" je prosjek za te dane.</p>`;
    if (nap) nap.after(div);
  };

  // ---- radnici
  async function ucitajRadnike() {
    let R;
    try { R = await api('/api/radnici'); } catch (e) { toast(e.message, true); return; }
    const box = $('#d-radnici');
    const ovl = R.ovlasti;
    const red = r => `<tr data-rd="${r.id}"><td><input class="rd-ime" value="${esc(r.ime)}" style="width:140px;text-align:left"></td>` +
      `<td><input class="rd-pin" type="password" inputmode="numeric" maxlength="8" placeholder="${r.id ? 'ne mijenja' : 'PIN'}" style="width:100px"></td>` +
      `<td><input class="rd-oib" inputmode="numeric" maxlength="11" value="${esc(r.oib || '')}" placeholder="OIB (za fiskalni račun)" title="OIB radnika ide na fiskalni račun kao OIB operatera. Prazno = OIB iz postavki fiskalizacije." style="width:120px"></td>` +
      `<td><label><input type="checkbox" class="rd-vl" ${r.vlasnik ? 'checked' : ''} style="width:auto"> vlasnik</label></td>` +
      `<td>${ovl.map(([k, n]) => `<label style="white-space:nowrap;margin-right:8px" title="${esc(n)}"><input type="checkbox" class="rd-o" value="${k}" ${r.ovlasti.includes(k) ? 'checked' : ''} style="width:auto"> ${esc(n.split(' (')[0])}</label>`).join(' ')}</td>` +
      `<td>${r.id ? `<label><input type="checkbox" class="rd-ak" ${r.aktivan ? 'checked' : ''} style="width:auto"> aktivan</label>` : ''}</td>` +
      `<td><button class="btn mali">${r.id ? 'Spremi' : 'Dodaj'}</button></td></tr>`;
    box.innerHTML = '<h2>Radnici i ovlasti</h2><p class="napomena">Vlasnik smije sve. Radnik bez ovlasti radi kasu, račune, blagajnu, narudžbe, HACCP, sniženja i gajbe, ' +
      'ali ne vidi nabavne cijene ni maržu. Svaka promjena se upisuje u dnevnik ispod.</p>' +
      '<div class="scroll" style="max-height:none"><table class="t"><tr><th>Ime</th><th>PIN</th><th>OIB</th><th></th><th>Ovlasti</th><th></th><th></th></tr>' +
      R.radnici.map(red).join('') + red({id: 0, ime: '', vlasnik: false, ovlasti: [], aktivan: true}) + '</table></div>' +
      `<div class="polja"><label>Automatska odjava nakon (minuta, 0 = nikad)<input id="rd-odjava" value="${esc(R.odjava_min)}" style="width:80px"></label><button class="btn" id="rd-odjava-spremi">Spremi</button></div>` +
      '<h3>Dnevnik radnji</h3><div class="polja">' +
      `<label>Od<input type="date" id="rd-od" value="${new Date(Date.now() - 7 * 864e5).toISOString().slice(0, 10)}"></label><label>Do<input type="date" id="rd-do" value="${danas()}"></label>` +
      `<label>Radnik<select id="rd-tko"><option value="">svi</option>${R.radnici.map(r => `<option value="${r.id}">${esc(r.ime)}</option>`).join('')}</select></label>` +
      '<button class="btn" id="rd-prikazi">Prikaži</button></div><div id="rd-dnevnik"></div>';
    box.querySelectorAll('[data-rd]').forEach(r => r.querySelector('button').onclick = async () => {
      const ak = r.querySelector('.rd-ak');
      try {
        await api('/api/radnici/spremi', {id: +r.dataset.rd, ime: r.querySelector('.rd-ime').value, pin: r.querySelector('.rd-pin').value, oib: r.querySelector('.rd-oib').value,
          vlasnik: r.querySelector('.rd-vl').checked, ovlasti: [...r.querySelectorAll('.rd-o:checked')].map(c => c.value), aktivan: ak ? ak.checked : true});
        toast('Spremljeno'); ucitajRadnike(); osvjeziStanje();
      } catch (e) { toast(e.message, true); }
    });
    $('#rd-odjava-spremi').onclick = async () => { try { await api('/api/radnici/postavke', {odjava_min: $('#rd-odjava').value}); toast('Spremljeno'); } catch (e) { toast(e.message, true); } };
    const dnevnik = async () => {
      let D;
      try { D = await api(`/api/radnici/radnje?od=${$('#rd-od').value}&do=${$('#rd-do').value}&radnik=${$('#rd-tko').value}`); } catch (e) { toast(e.message, true); return; }
      $('#rd-dnevnik').innerHTML = D.radnje.length ? '<div class="scroll" style="max-height:400px"><table class="t"><tr><th>Vrijeme</th><th>Radnik</th><th>Radnja</th></tr>' +
        D.radnje.map(x => `<tr><td>${datumHr(x.vrijeme.slice(0, 10))} ${x.vrijeme.slice(11, 16)}</td><td>${esc(x.radnik || '-')}</td><td>${esc(x.opis)}</td></tr>`).join('') + '</table></div>' :
        '<p class="napomena">Nema upisa u tom razdoblju.</p>';
    };
    $('#rd-prikazi').onclick = dnevnik; dnevnik();
  }

  // ---- e-mail sažetak
  async function ucitajSazetak() {
    let Z;
    try { Z = await api('/api/sazetak'); } catch (e) { toast(e.message, true); return; }
    const box = $('#d-sazetak'), P = Z.postavke;
    const polje = (k, n, extra) => `<label>${n}<input id="sz-${k}" value="${esc(P[k] || '')}" ${extra || ''}></label>`;
    box.innerHTML = '<h2>Večernji sažetak na e-mail</h2><p class="napomena">Šalje se s vašeg e-mail računa. Za Gmail: poslužitelj smtp.gmail.com, port 587, ' +
      'a umjesto lozinke "lozinka za aplikacije" (Google račun → Sigurnost → Potvrda u 2 koraka → Lozinke za aplikacije). Lozinka ostaje samo na ovom računalu.</p>' +
      '<div class="polja">' + polje('posluzitelj', 'SMTP poslužitelj', 'placeholder="smtp.gmail.com"') + polje('port', 'Port', 'style="width:70px"') +
      `<label>Sigurnost<select id="sz-sigurnost"><option value="starttls">STARTTLS (587)</option><option value="ssl" ${P.sigurnost === 'ssl' ? 'selected' : ''}>SSL (465)</option></select></label>` +
      polje('korisnik', 'Korisnik (e-mail)') +
      `<label>Lozinka<input id="sz-lozinka" type="password" autocomplete="new-password" placeholder="${P.lozinka_spremljena ? 'spremljena (ne mijenja se)' : ''}"></label></div>` +
      '<div class="polja">' + polje('od', 'Pošiljatelj (prazno = korisnik)') + polje('za', 'Kome (više adresa odvojite zarezom)', 'style="width:300px"') + '</div>' +
      `<div class="polja"><label style="flex-direction:row;align-items:center;gap:6px;color:var(--ink)"><input type="checkbox" id="sz-kod" ${P.kod_zakljucka ? 'checked' : ''}> Pošalji kad se zaključi smjena</label>` +
      polje('vrijeme', 'i/ili svaki dan u (npr. 21:30)', 'style="width:90px"') + '</div>' +
      '<div class="red"><button class="btn prim" id="sz-spremi">Spremi</button><button class="btn" id="sz-probni">Pošalji probni e-mail</button>' +
      (P.lozinka_spremljena ? '<button class="btn opasno" id="sz-obrisi">Obriši lozinku</button>' : '') + '</div>' +
      (P.zadnje ? `<p class="napomena">Zadnji poslan: ${esc(P.zadnje)}</p>` : '') + (P.zadnja_greska ? `<p class="minus">Zadnja greška: ${esc(P.zadnja_greska)}</p>` : '') +
      `<h3>Kako izgleda (danas)</h3><p><b>${esc(Z.naslov)}</b></p><pre>${esc(Z.tekst)}</pre>`;
    const podaci = () => ({posluzitelj: $('#sz-posluzitelj').value, port: $('#sz-port').value, sigurnost: $('#sz-sigurnost').value,
      korisnik: $('#sz-korisnik').value, lozinka: $('#sz-lozinka').value, od: $('#sz-od').value, za: $('#sz-za').value,
      kod_zakljucka: $('#sz-kod').checked, vrijeme: $('#sz-vrijeme').value});
    $('#sz-spremi').onclick = async () => { try { await api('/api/sazetak/postavke', podaci()); toast('Spremljeno'); ucitajSazetak(); } catch (e) { toast(e.message, true); } };
    $('#sz-probni').onclick = async () => {
      $('#sz-probni').disabled = true;
      try { await api('/api/sazetak/postavke', podaci()); const r = await api('/api/sazetak/probni', {}); toast('Poslano: ' + r.naslov); } catch (e) { toast(e.message, true); }
      ucitajSazetak();
    };
    if ($('#sz-obrisi')) $('#sz-obrisi').onclick = async () => { try { await api('/api/sazetak/postavke', Object.assign(podaci(), {lozinka: '', obrisi_lozinku: true})); ucitajSazetak(); } catch (e) { toast(e.message, true); } };
  }

  // ---- fiskalizacija (fiskal.py)
  async function ucitajFiskal() {
    let F;
    try { F = await api('/api/fiskal'); } catch (e) { toast(e.message, true); return; }
    const box = $('#d-fiskal'), P = F.postavke, C = F.certifikat;
    const polje = (k, n, extra) => `<label>${n}<input id="fk-${k}" value="${esc(P[k] || '')}" ${extra || ''}></label>`;
    const kv = (k, n) => `<label style="flex-direction:row;align-items:center;gap:6px;color:var(--ink)"><input type="checkbox" id="fk-${k}" ${P[k] === '1' ? 'checked' : ''}> ${n}</label>`;
    const test = P.okolina === 'test';
    let h = `<h2>Fiskalizacija <span class="badge">${test ? 'TEST (demo)' : 'PRODUKCIJA'}</span></h2>` +
      (test ? '<p class="napomena">Test: računi iz kase idu na testni poslužitelj Porezne s demo certifikatom i NE vrijede. Prava prodaja i dalje ide kroz Robu.</p>'
            : '<p class="minus"><b>Prava fiskalizacija je uključena.</b> Svaki račun iz kase je pravi fiskalni račun.</p>') +
      `<p class="napomena">Poslužitelj: ${esc(F.url)}</p>`;
    if (!F.cryptography) h += '<p class="minus">Nedostaje Python paket <b>cryptography</b> (za certifikat i potpis). <button class="btn prim" id="fk-instaliraj">Instaliraj (treba internet)</button></p>';
    h += (F.upozorenja || []).map(u => `<p class="minus">${esc(u)}</p>`).join('');
    h += '<h3>1. Certifikat</h3>';
    if (C && !C.greska) h += `<p>${C.demo ? '<b>DEMO</b> certifikat' : 'Certifikat'}: ${esc(C.subjekt)}<br><span class="napomena">Izdavatelj: ${esc(C.izdavatelj)} · vrijedi do ${C.vrijedi_do}${C.oib ? ' · OIB u certifikatu ' + C.oib : ''}</span></p>`;
    else if (C && C.greska) h += `<p class="minus">${esc(C.greska)}</p>`;
    else h += '<p class="napomena">Certifikat još nije učitan.</p>';
    h += '<div class="polja"><label>Datoteka .p12 / .pfx<input type="file" id="fk-datoteka" accept=".p12,.pfx"></label>' +
      `<label>Lozinka certifikata<input id="fk-lozinka" type="password" autocomplete="new-password" placeholder="${P.lozinka_upisana ? 'upisana (ne mijenja se)' : ''}"></label>` +
      polje('cert_putanja', 'ili putanja do datoteke', 'style="width:320px"') + '</div>';
    h += '<h3>2. Podaci obveznika</h3><div class="polja">' + polje('oib', 'OIB obveznika', 'style="width:130px" inputmode="numeric"') +
      polje('prostor', 'Oznaka poslovnog prostora', 'style="width:120px" placeholder="npr. PJ1"') +
      polje('uredjaj', 'Oznaka naplatnog uređaja', 'style="width:80px" inputmode="numeric"') +
      polje('oib_operatera', 'OIB operatera (prazno = OIB obveznika)', 'style="width:130px" inputmode="numeric"') +
      polje('operater', 'Oznaka operatera na računu', 'style="width:140px"') + '</div>' +
      '<div class="polja">' + polje('naziv_tvrtke', 'Naziv na računu (obrt / tvrtka)', 'style="width:260px"') +
      `<label>Adresa (po retcima)<textarea id="fk-adresa" rows="2" style="width:260px">${esc(P.adresa || '')}</textarea></label>` +
      kv('u_pdv', 'U sustavu PDV-a') + '</div>' +
      '<p class="napomena">Oznaku prostora i uređaja birate sami (prostor mora biti prijavljen u ePoreznoj prije prave fiskalizacije). Uzmite <b>drugi broj uređaja nego u Robi</b> (npr. 2), da se brojevi računa ne preklapaju. Brojevi idu od 1 svake godine, posebno za test i produkciju.</p>';
    h += '<h3>3. Načini plaćanja</h3><table class="t"><tr><th>Način u kasi</th><th>Za Poreznu</th></tr>' +
      F.placanja.map(p => `<tr><td>${esc(p.naziv)}</td><td><select data-pl="${p.id}"><option value="">automatski (${p.nacin})</option>` +
        F.nacini.map(([k, n]) => `<option value="${k}" ${p.rucno === k ? 'selected' : ''}>${k} = ${n}</option>`).join('') + '</select></td></tr>').join('') + '</table>';
    h += '<h3>4. Veza</h3><div class="polja">' + polje('timeout', 'Čekanje odgovora (s)', 'style="width:70px"') +
      polje('ca_putanja', 'Dodatni CA certifikat poslužitelja (.pem/.cer, nije obavezno)', 'style="width:320px"') +
      (test ? kv('tls_bez_provjere', 'Ne provjeravaj certifikat poslužitelja (samo test)') : '') + '</div>';
    h += '<div class="red"><button class="btn prim" id="fk-spremi">Spremi</button><button class="btn" id="fk-echo">Provjeri vezu</button>' +
      (test ? '<button class="btn" id="fk-provjera">Probni račun (provjera certifikata)</button>' : '') + '</div><p id="fk-rez" class="napomena"></p>';
    h += '<h3>Računi bez JIR-a</h3>' + (F.neposlani.length ?
      '<table class="t"><tr><th>Račun</th><th>Vrijeme</th><th class="r">Iznos</th><th>Pokušaja</th><th>Greška</th></tr>' +
      F.neposlani.map(n => `<tr><td>${esc(n.oznaka)}${n.okolina === 'test' ? ' T' : ''}</td><td>${esc(n.vrijeme.slice(0, 16))}</td><td class="r">${eur(n.ukupno)}</td><td>${n.pokusaja}</td><td>${esc(n.poruka)}</td></tr>`).join('') +
      '</table><div class="red"><button class="btn prim" id="fk-ponovi">Pošalji ponovno sve</button></div>'
      : '<p class="napomena">Nema, svi računi imaju JIR. Računi bez JIR-a (nema interneta) šalju se sami svake 2 minute.</p>');
    if (F.zadnji.length) h += '<h3>Zadnji fiskalni računi</h3><table class="t"><tr><th>Račun</th><th>Vrijeme</th><th class="r">Iznos</th><th>JIR</th></tr>' +
      F.zadnji.map(n => `<tr><td>${esc(n.oznaka)}${n.okolina === 'test' ? ' T' : ''}</td><td>${esc(n.vrijeme.slice(0, 16))}</td><td class="r">${eur(n.ukupno)}</td><td style="font-size:12px">${n.jir ? esc(n.jir) : '<b class="minus">nema</b>'}</td></tr>`).join('') + '</table>';
    h += '<h3>Prava fiskalizacija</h3>' + (test ?
      '<p class="napomena">Tek kad test prođe i kad imate pravi certifikat za fiskalizaciju, prijavljen poslovni prostor i dogovor s knjigovođom. Upišite riječ PRODUKCIJA i kliknite gumb.</p>' +
      '<div class="polja"><label>Potvrda<input id="fk-potvrda" style="width:130px"></label><button class="btn opasno" id="fk-prod">Prebaci na pravu fiskalizaciju</button></div>'
      : '<div class="red"><button class="btn" id="fk-natest">Vrati na test</button></div>');
    box.innerHTML = h;

    const rez = (t, err) => { const el = $('#fk-rez'); el.textContent = t; el.className = err ? 'minus' : 'napomena'; };
    const podaci = () => {
      const o = {};
      ['oib', 'prostor', 'uredjaj', 'oib_operatera', 'operater', 'naziv_tvrtke', 'adresa', 'cert_putanja', 'ca_putanja', 'timeout'].forEach(k => o[k] = $('#fk-' + k).value);
      o.u_pdv = $('#fk-u_pdv').checked;
      if ($('#fk-tls_bez_provjere')) o.tls_bez_provjere = $('#fk-tls_bez_provjere').checked;
      if ($('#fk-lozinka').value) o.cert_lozinka = $('#fk-lozinka').value;
      return o;
    };
    const spremi = async () => {
      const dat = $('#fk-datoteka').files[0];
      if (dat) {
        const b64 = await new Promise((ok, ne) => { const r = new FileReader(); r.onload = () => ok(String(r.result).split(',')[1]); r.onerror = ne; r.readAsDataURL(dat); });
        await api('/api/fiskal/certifikat', {ime: dat.name, sadrzaj: b64});
        const o = podaci(); delete o.cert_putanja;  // putanju je postavio upload
        await api('/api/fiskal/postavke', o);
      } else await api('/api/fiskal/postavke', podaci());
      const nac = {}; box.querySelectorAll('[data-pl]').forEach(s => nac[s.dataset.pl] = s.value);
      await api('/api/fiskal/nacini', nac);
    };
    const gumb = (id, fn) => { const b = $('#' + id); if (!b) return; b.onclick = async () => {
      b.disabled = true; rez('Radim…');
      try { const t = await fn(); await ucitajFiskal(); if (t) rez(t); }
      catch (e) { rez(e.message, true); b.disabled = false; }
    }; };
    gumb('fk-spremi', async () => { await spremi(); toast('Spremljeno'); return 'Spremljeno.'; });
    gumb('fk-echo', async () => { await spremi(); return (await api('/api/fiskal/echo', {})).poruka; });
    gumb('fk-provjera', async () => { await spremi(); return (await api('/api/fiskal/provjera', {})).poruka; });
    gumb('fk-ponovi', async () => { const x = await api('/api/fiskal/posalji', {}); ucitajKontrolu(); return `Poslano ${x.poslano}, ostalo ${x.ostalo}.`; });
    gumb('fk-instaliraj', async () => (await api('/api/fiskal/instaliraj', {})).poruka);
    gumb('fk-prod', async () => { await spremi(); await api('/api/fiskal/postavke', {okolina: 'produkcija', potvrda: $('#fk-potvrda').value}); await osvjeziArtikle(); return 'Prava fiskalizacija je uključena.'; });
    gumb('fk-natest', async () => { await api('/api/fiskal/postavke', {okolina: 'test'}); await osvjeziArtikle(); return 'Vraćeno na test.'; });
  }

  // ---------------------------------------------------------------- Danas: sniženja i gajbe
  const origDanas = window.ucitajDanas;
  window.ucitajDanas = async function () {
    await origDanas();
    const m = (DOD.stanje && DOD.stanje.moduli) || MODULI;
    if (!(m.snizenje || m.ambalaza)) return;
    let D;
    try { D = await api('/api/dodaci/danas'); } catch (e) { return; }
    const grid = $('#j-sadrzaj').querySelector('.i-grid');
    if (!grid) return;
    const kartica = (naslov, tijelo) => { const k = document.createElement('div'); k.className = 'card j-k';
      k.innerHTML = `<h2 style="display:flex;justify-content:space-between;gap:8px">${naslov}<button class="btn">Dodaci</button></h2>${tijelo}`;
      k.querySelector('button').onclick = () => tabBtn.click(); grid.appendChild(k); };
    if (D.snizenja) kartica('Sniženja u tijeku', D.snizenja.length ? '<table class="t">' + D.snizenja.map(s => `<tr><td>${esc(s.naziv)}</td><td>-${nizi(s.postotak)} %</td><td class="r">${eur(s.cijena)} € do ${s.do.slice(11, 16)}</td></tr>`).join('') + '</table>' :
      '<p class="napomena">Nema. Roba koja vene? Snizite je jednim klikom.</p>');
    if (D.ambalaza && D.ambalaza.length) kartica('Gajbe za vratiti', '<table class="t">' + D.ambalaza.map(a => `<tr><td>${esc(a.partner)}</td><td class="r">${a.vrste.map(v => esc(v.vrsta) + ' ' + v.saldo).join(', ')}</td></tr>`).join('') + '</table>');
  };

  osvjeziStanje();
})();
