// The editor's live tag counter must count exactly like the server (metadata.tag_chars), and the keyword
// box must behave: runs the real functions from the dashboard script against a tiny stand-in page.
const fs = require('fs'), path = require('path');
const src = fs.readFileSync(path.join(__dirname, 'dash_c.js'), 'utf8');
const grab = (start, end) => { const a = src.indexOf(start); if (a < 0) throw new Error('not found: ' + start); const b = src.indexOf(end, a); return src.slice(a, b) };
const els = {};
const el = id => els[id] || (els[id] = { id, value: '', textContent: '', innerHTML: '', style: {}, dataset: {}, _cls: new Set(),
  classList: { add(c) { els[id]._cls.add(c) }, remove(c) { els[id]._cls.delete(c) }, toggle(c, on) { on ? els[id]._cls.add(c) : els[id]._cls.delete(c) }, contains(c) { return els[id]._cls.has(c) } } });
let CHIPS = [];
const $ = s => el(s.replace(/^#/, ''));
const $$ = s => s.startsWith('#e-chips') ? CHIPS : [];
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const when = iso => 'TIME';
const code = grab('const KSRC=', "$('#e-kw').onclick");
const api = {}; let out = [];
const ok = (c, m) => { out.push((c ? 'PASS ' : 'FAIL ') + m) };
eval(code + `
  api.tagChars = tagChars; api.kwCount = kwCount; api.kwChips = kwChips; api.kwFill = kwFill;`);
ok(api.tagChars(['a b', 'c']) === 7 && api.tagChars([]) === 0 && api.tagChars(['बटर चिकन']) === 10, 'tag counter: commas count, a tag with a space counts 2 more, Hindi letters count once each');
const cases = JSON.parse(fs.readFileSync(path.join(__dirname, 'parity_cases.json'), 'utf8'));
ok(cases.every(c => api.tagChars(c.tags) === c.n), `tag counter equals the server's count on ${cases.length} lists`);
$('#e-mg').value = Array.from({ length: 40 }, (_, i) => 'butter chicken recipe number ' + i).join(', ');
$('#e-mt').value = 'x'.repeat(101); $('#e-md').value = Array.from({ length: 16 }, (_, i) => '#tag' + i).join(' ');
api.kwCount();
ok(/too many/.test($('#e-mgn').textContent) && /16 hashtags/.test($('#e-mgn').textContent) && /too long/.test($('#e-mtn').textContent), 'warnings: tags over 500, more than 15 hashtags, title over 100');
$('#e-mg').value = 'butter chicken, shorts'; $('#e-mt').value = 'Butter Chicken Recipe'; $('#e-md').value = '#shorts';
api.kwChips([{ k: 'butter chicken', src: ['typed', 'video'], hot: true }, { k: '<img src=x onerror=alert(1)>', src: ['video'] }]);
const html = $('#e-chips').innerHTML;
ok(html.includes('&lt;img') && !html.includes('<img') && html.includes('kd t') && html.includes('kd v') && html.includes('kd h') && html.includes('typed by people'), 'chips show their sources; keyword text is escaped');
ok($('#e-mgn').textContent.includes('23 of 500') && $('#e-mtn').textContent.includes('21 of 100') && !/too/.test($('#e-mtn').textContent), 'counters: ' + $('#e-mgn').textContent + ' | ' + $('#e-mtn').textContent);
api.kwFill({ status: 'queued', meta_edited: true, kw: { at: '2026-11-22T10:00:00Z', note: 'Chosen by the system: 3 phrases.', items: [{ k: 'a b', src: ['typed'] }], hint: 'my topic' } });
ok($('#e-knote').textContent.includes('You changed the title') && $('#e-kq').value === 'my topic' && $('#e-meta').classList.contains('hide') && !$('#e-krow').classList.contains('hide'), 'hand-edited clip: says the system will not rewrite it');
api.kwFill({ status: 'queued', kw: null });
ok($('#e-knote').textContent.startsWith('No keyword research is saved') && $('#e-klegend').classList.contains('hide'), 'clip without research: says so');
api.kwFill({ status: 'uploaded', kw: { at: 'x', note: 'N.', items: [] } });
ok($('#e-krow').classList.contains('hide'), 'uploaded video: the buttons are hidden');
// Ideas -> Keyword research: the result is drawn in groups, escaped, with the two buttons
const code2 = grab('const KRG=', "$('#kr-go').onclick");
const skel = () => '';
let fake = { groups: { top: ['street food india', '<b>x</b>'], questions: ['how to make street food'], long: [], hindi: ['street food in hindi'] }, trending: ['street food tour'], note: '' };
const run = eval('(async()=>{const api=async u=>{URL_SEEN=u;return fake};' + code2 + ';await krRun();return 1})');
var URL_SEEN = '';
(async () => {
  $('#kr-q').value = 'street food india';
  await run();
  const h = $('#kr-out').innerHTML;
  ok(URL_SEEN === '/api/keywords?q=street%20food%20india' && h.includes('Most typed') && h.includes('Questions people ask') && h.includes('Hindi / Hinglish') && h.includes('trending lists')
    && !h.includes('Long phrases') && h.includes('&lt;b&gt;x') && !h.includes('<b>x</b>') && h.includes('id="kr-src"') && h.includes('id="kr-copy"'), 'research card: groups, escaped phrases, both buttons; empty groups hidden');
  ok(!/\d+\s*(searches|views|volume)/i.test(h), 'research card shows no invented numbers');
  fake = { groups: { top: [], questions: [], long: [], hindi: [] }, trending: [], note: 'Nobody seems to type this. Try fewer or simpler words.' };
  await run();
  ok($('#kr-out').innerHTML.includes('Nobody seems to type this') && !$('#kr-out').innerHTML.includes('kr-src'), 'nothing found: a plain line, no buttons');
  $('#kr-q').value = ' ';
  let msg = ''; try { await run() } catch (e) { msg = e.message }
  ok(msg.startsWith('Type a topic first'), 'empty topic: asks for a topic');
  console.log(out.join('\n'));
})();
