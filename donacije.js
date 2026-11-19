// Donacije hrane (donacije.py): gumb u Nabavi, popis, godišnji zbroj, nova donacija i zapisnik.
// Učitava se iza glavne skripte u kasa.html i koristi njezine pomoćne funkcije (api, esc, eur, toast, ZAL...).
(function () {
  const otpis = document.getElementById('n-novi-otpis');
  if (!otpis) return;
  const b = document.createElement('button');
  b.className = 'btn'; b.id = 'n-donacije'; b.hidden = true; b.textContent = 'Donacije hrane';
  b.title = 'Donirana hrana (smanjuje zalihu kao otpis), zapisnik i godišnji zbroj';
  otpis.after(b);
  const desno = () => document.getElementById('n-desno');
  const ispis = id => window.open('/ispis_donacija/' + id + '#ispis', 'ispisdon', 'width=900,height=900');

  const orig = window.primijeniModule;
  window.primijeniModule = function () { orig(); b.hidden = !MODULI.donacije; };

  async function pregled(godina) {
    let P;
    try { P = await api('/api/donacije' + (godina ? '?godina=' + godina : '')); } catch (e) { toast(e.message, true); return; }
    const H = P.prijedlog_prihoda, pr = H.roba_neto + (MODULI.fiskalizacija ? H.kasa_neto : 0);
    const boja = P.razina === 'crveno' ? 'minus' : P.razina === 'zuto' ? 'upoz' : '';
    desno().innerHTML = `<h2 style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">Donacije hrane ${P.godina}` +
      `<span><button class="btn" id="dn-prosla">◀ ${P.godina - 1}</button> <button class="btn prim" id="dn-nova">Nova donacija</button></span></h2>` +
      `<p class="upoz">${esc(P.upozorenje)}</p>` +
      '<table class="t">' +
      `<tr><td>Donirano ${P.godina} (nabavna vrijednost, broj donacija: ${P.broj})</td><td class="r"><b>${eur(P.zbroj)} €</b></td></tr>` +
      `<tr><td>Prihod ${P.prosla} (upisuje vlasnik, bez PDV-a)</td><td class="r">${P.prihod ? eur(P.prihod) + ' €' : '<span class="minus">nije upisan</span>'}</td></tr>` +
      `<tr><td>Granica 2 %</td><td class="r">${P.granica ? eur(P.granica) + ' €' : '—'}</td></tr>` +
      (P.posto_granice !== null ? `<tr><td>Iskorišteno</td><td class="r ${boja}"><b>${String(P.posto_granice).replace('.', ',')} %</b></td></tr>` : '') + '</table>' +
      (P.razina === 'crveno' ? '<p class="minus"><b>Granica od 2 % je prekoračena.</b> Za donacije iznad granice potvrdite s knjigovođom (PDV).</p>'
        : P.razina === 'zuto' ? '<p class="upoz">Iskorišteno je više od 80 % granice.</p>' : '') +
      `<div class="red" style="flex-wrap:wrap;margin-top:8px"><label>Prihod ${P.prosla} € <input id="dn-prihod" inputmode="decimal" style="width:130px" value="${P.prihod ? eur(P.prihod) : ''}"></label>` +
      '<button class="btn" id="dn-prihod-spremi">Spremi prihod</button></div>' +
      `<p class="napomena">Pomoć (promet ${P.prosla} iz naše baze, bez PDV-a): Roba ${eur(H.roba_neto)} €, kasa ${eur(H.kasa_neto)} €` +
      (pr ? ` <a href="#" id="dn-prijedlog">upiši ${eur(pr)} €</a>` : '') + '. Točan prihod daje knjigovođa.</p>' +
      '<h3>Donacije</h3>' + (P.donacije.length ? '<table class="t"><tr><th>Broj</th><th>Datum</th><th>Primatelj</th><th class="r">Nabavna</th><th></th></tr>' +
        P.donacije.map(d => `<tr${d.storniran ? ' style="text-decoration:line-through;color:var(--muted)"' : ''}><td><a href="#" data-dn="${d.id}">${esc(d.oznaka)}</a></td>` +
          `<td>${datumHr(d.datum)}</td><td>${esc(d.primatelj)}${d.posrednik ? '' : ' <span class="minus" title="Nije označen kao registrirani posrednik">!</span>'}</td>` +
          `<td class="r">${eur(d.neto)}</td><td class="r"><button class="btn" data-isp="${d.id}" style="padding:2px 8px">Zapisnik</button></td></tr>`).join('') + '</table>'
        : '<p class="napomena">Nema donacija u ovoj godini.</p>');
    document.getElementById('dn-prosla').onclick = () => pregled(P.godina - 1);
    document.getElementById('dn-nova').onclick = () => nova(P.primatelji);
    document.getElementById('dn-prihod-spremi').onclick = async () => {
      try { await api('/api/donacije/prihod', {godina: P.prosla, prihod: document.getElementById('dn-prihod').value}); toast('Prihod spremljen'); pregled(P.godina); }
      catch (e) { toast(e.message, true); }
    };
    const pp = document.getElementById('dn-prijedlog');
    if (pp) pp.onclick = e => { e.preventDefault(); document.getElementById('dn-prihod').value = eur(pr); };
    desno().querySelectorAll('[data-isp]').forEach(x => x.onclick = () => ispis(x.dataset.isp));
    desno().querySelectorAll('[data-dn]').forEach(x => x.onclick = e => { e.preventDefault(); detalj(+x.dataset.dn); });
  }

  async function detalj(id) {
    let D;
    try { D = await api('/api/donacija/' + id); } catch (e) { toast(e.message, true); return; }
    desno().innerHTML = `<h2>Donacija ${esc(D.oznaka)}${D.storniran ? ' (stornirana)' : ''}</h2>` +
      `<p>${datumHr(D.datum)} · <b>${esc(D.primatelj)}</b>, OIB ${esc(D.oib)} · registrirani posrednik: <b>${D.posrednik ? 'da' : 'NE'}</b>` +
      `${D.napomena ? '<br>' + esc(D.napomena) : ''}<br><span class="napomena">Zaliha je smanjena otpisom ${esc(D.otpis)} (Nabava).</span></p>` +
      '<table class="t"><tr><th>Artikl</th><th class="r">Količina</th><th class="r">Nab. cijena</th><th class="r">Iznos</th></tr>' +
      D.stavke.map(s => `<tr><td>${esc(s.naziv)}</td><td class="r">${kolTxt(s.kolicina)} ${esc(s.jm)}</td><td class="r">${eur(s.cijena)}</td><td class="r">${eur(s.iznos)}</td></tr>`).join('') +
      `<tfoot><tr><td colspan="3">Ukupno po nabavnoj</td><td class="r">${eur(D.neto)}</td></tr></tfoot></table>` +
      '<div class="red" style="margin-top:12px"><button class="btn prim" id="dn-ispis">Zapisnik (ispis)</button><button class="btn" id="dn-natrag">Natrag</button>' +
      (D.storniran ? '' : '<button class="btn opasno" id="dn-storno">Storniraj</button>') + '</div>';
    document.getElementById('dn-ispis').onclick = () => ispis(id);
    document.getElementById('dn-natrag').onclick = () => pregled(+D.datum.slice(0, 4));
    const sb = document.getElementById('dn-storno');
    if (sb) sb.onclick = async () => {
      if (!sb.dataset.potvrda) { sb.dataset.potvrda = 1; sb.textContent = 'Sigurno stornirati? Kliknite opet'; return; }
      try { await api('/api/storno_dok/' + D.dokument_id, {}); toast('Donacija stornirana (otpis storniran)'); await ucitajZalihu(); await ucitajDokumente(); detalj(id); }
      catch (e) { toast(e.message, true); }
    };
  }

  function nova(primatelji) {
    const st = [];
    desno().innerHTML = '<h2>Nova donacija hrane</h2>' +
      '<p class="napomena">Donacija smanjuje zalihu kao otpis, po prosječnoj nabavnoj cijeni.</p>' +
      '<div class="polja" style="display:flex;gap:10px;flex-wrap:wrap">' +
      '<label>Primatelj <input id="dn-prim" list="dn-prim-lista" autocomplete="off" style="width:220px"></label>' +
      '<datalist id="dn-prim-lista">' + primatelji.map(p => `<option value="${esc(p.primatelj)}">`).join('') + '</datalist>' +
      '<label>OIB primatelja <input id="dn-oib" inputmode="numeric" maxlength="11" style="width:130px"></label>' +
      '<label style="align-self:end"><input type="checkbox" id="dn-posr"> registrirani posrednik</label>' +
      `<label>Datum <input type="date" id="dn-datum" value="${danas()}" max="${danas()}"></label>` +
      '<label>Napomena <input id="dn-nap" placeholder="npr. istječe rok" style="width:200px"></label></div>' +
      '<div class="trazi-dok"><input id="dn-trazi" placeholder="Dodaj artikl: šifra ili naziv, pa Enter" autocomplete="off"><ul class="rez card" id="dn-rez" hidden></ul></div>' +
      '<table class="t" id="dn-stavke"></table>' +
      '<div class="red"><button class="btn prim" id="dn-spremi">Spremi i ispiši zapisnik</button><button class="btn" id="dn-odustani">Odustani</button></div>';
    const $i = id => document.getElementById(id);
    $i('dn-prim').oninput = () => {
      const p = primatelji.find(x => x.primatelj === $i('dn-prim').value);
      if (p) { $i('dn-oib').value = p.oib; $i('dn-posr').checked = !!p.posrednik; }
    };
    const crtaj = () => {
      $i('dn-stavke').innerHTML = '<tr><th>Artikl</th><th class="r">Količina</th><th class="r">Na zalihi</th><th class="r">Nabavna</th><th class="r">Iznos</th><th></th></tr>' +
        (st.length ? st.map((s, k) => `<tr><td>${esc(s.a.naziv)}</td><td class="r"><input class="dkol" data-k="${k}" value="${esc(s.kol)}" inputmode="decimal" style="width:70px"> ${esc(s.a.jm)}</td>` +
          `<td class="r">${kolTxt(s.a.zaliha)}</td><td class="r">${s.a.nabavna !== undefined ? eur(s.a.nabavna) : ''}</td><td class="r" id="dn-izn${k}"></td>` +
          `<td><button class="btn x" data-k="${k}">×</button></td></tr>`).join('') : '<tr><td colspan="6" class="napomena">Dodajte artikle preko polja iznad.</td></tr>');
      const zbroj = () => st.forEach((s, k) => { const el = $i('dn-izn' + k); const q = parseFloat(String(s.kol).replace(',', '.'));
        if (el && s.a.nabavna !== undefined) el.textContent = q > 0 ? eur(Math.round(q * s.a.nabavna)) : ''; });
      $i('dn-stavke').querySelectorAll('.dkol').forEach(inp => {
        inp.oninput = () => { st[+inp.dataset.k].kol = inp.value; zbroj(); };
        inp.onkeydown = e => { if (e.key === 'Enter') $i('dn-trazi').focus(); };
      });
      $i('dn-stavke').querySelectorAll('button.x').forEach(x => x.onclick = () => { st.splice(+x.dataset.k, 1); crtaj(); });
      zbroj();
    };
    let rez = [], i = 0;
    const crtajRez = () => { const ul = $i('dn-rez'); ul.hidden = !rez.length;
      ul.innerHTML = rez.map((a, k) => `<li data-k="${k}" class="${k === i ? 'sel' : ''}"><span class="s">${esc(a.sifra)}</span><span class="n">${esc(a.naziv)}</span><span class="c">${kolTxt(a.zaliha)} ${esc(a.jm)}</span></li>`).join(''); };
    const dodaj = a => { st.push({a, kol: ''}); $i('dn-trazi').value = ''; rez = []; crtajRez(); crtaj();
      const inps = $i('dn-stavke').querySelectorAll('.dkol'); inps[inps.length - 1].focus(); };
    $i('dn-trazi').oninput = e => { const v = e.target.value.trim(), q = norm(v); i = 0;
      rez = !q ? [] : ZAL.filter(a => a.sifra === v).concat(ZAL.filter(a => a.sifra !== v && norm(a.naziv).includes(q))).slice(0, 10); crtajRez(); };
    $i('dn-trazi').onkeydown = e => {
      if (e.key === 'ArrowDown') { i = Math.min(i + 1, rez.length - 1); crtajRez(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { i = Math.max(i - 1, 0); crtajRez(); e.preventDefault(); }
      else if (e.key === 'Enter' && rez[i]) dodaj(rez[i]);
    };
    $i('dn-rez').onclick = e => { const li = e.target.closest('li'); if (li) dodaj(rez[+li.dataset.k]); };
    $i('dn-odustani').onclick = () => pregled();
    $i('dn-spremi').onclick = async () => {
      const btn = $i('dn-spremi'); btn.disabled = true;
      try {
        const r = await api('/api/donacija', {primatelj: $i('dn-prim').value, oib: $i('dn-oib').value.trim(), posrednik: $i('dn-posr').checked,
          datum: $i('dn-datum').value, napomena: $i('dn-nap').value,
          stavke: st.map(s => ({artikl_id: s.a.id, kolicina: String(s.kol).replace(',', '.')}))});
        toast('Donacija spremljena, zaliha smanjena');
        ispis(r.id);
        await ucitajZalihu(); await ucitajDokumente(); detalj(r.id);
      } catch (e) { toast(e.message, true); btn.disabled = false; }
    };
    crtaj();
    if (!ZAL.length) ucitajZalihu();
    $i('dn-prim').focus();
  }

  b.onclick = () => pregled();
})();
