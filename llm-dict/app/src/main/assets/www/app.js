(function () {
'use strict';
var D = window.DICT, E = D.e, CH = D.ch;
var view = document.getElementById('view'), bar = document.getElementById('bar'), tabs = document.getElementById('tabs'), toastEl = document.getElementById('toast');
var ICON = {
  back: '<svg viewBox="0 0 24 24"><path d="M15 5l-7 7 7 7"/></svg>',
  star: '<svg viewBox="0 0 24 24"><path d="M12 3.8l2.5 5.2 5.7.8-4.1 4 1 5.7-5.1-2.7-5.1 2.7 1-5.7-4.1-4 5.7-.8z"/></svg>',
  search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/></svg>',
  x: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>'
};
var mem = {};
var store = {
  get: function (k, d) { try { var v = localStorage.getItem('dict.' + k); return v == null ? (k in mem ? mem[k] : d) : JSON.parse(v); } catch (e) { return k in mem ? mem[k] : d; } },
  set: function (k, v) { mem[k] = v; try { localStorage.setItem('dict.' + k, JSON.stringify(v)); } catch (e) {} }
};
var S = { q: '', showN: 30, tq: '', fs: store.get('fs', 1), theme: store.get('theme', 'auto'), fav: store.get('fav', []), recent: store.get('recent', []) };
var FS = [['小', 15], ['标准', 17], ['大', 19], ['特大', 21]];
function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
function norm(s) { try { s = s.normalize('NFKC'); } catch (e) {} return s.toLowerCase(); }
function chLabel(c) { return /^\d+$/.test(c.n) ? '第 ' + c.n + ' 章' : '附录 ' + c.n; }
function splitNo(t) { var m = /^([0-9A-G]+(?:\.\d+)+)\s+(.*)$/.exec(t); return m ? [m[1], m[2]] : ['', t]; }
function toast(msg) { toastEl.textContent = msg; toastEl.classList.add('show'); clearTimeout(toast.t); toast.t = setTimeout(function () { toastEl.classList.remove('show'); }, 1400); }

/* 每条的检索字段 */
var NO = [], NAME = [], NAMEL = [], SECN = [], SECL = [], CHL = [], YL = [], FIRST = [], dup = {};
E.forEach(function (e, i) {
  var p = splitNo(e[0]), c = CH[e[1]], s = c.secs[e[2]], sp = splitNo(s ? s.t : '');
  NO.push(p[0]); NAME.push(p[1]); NAMEL.push(norm(p[1])); SECN.push([sp[0], sp[1]]); SECL.push(sp[1] === p[1] ? '' : norm(sp[1])); CHL.push(norm(c.t)); YL.push(norm(e[4]));
  FIRST.push(s && s.e[0] === i); dup[p[1]] = (dup[p[1]] || 0) + 1;
});
function shown(i) { var n = NAME[i]; return (dup[n] > 1 || n.length <= 3) && SECN[i][1] && SECN[i][1] !== n ? SECN[i][1] + '：' + n : n; }
var FSEC = E.map(function (e, i) { var c = CH[e[1]]; for (var k = 0; k < c.secs.length; k++) if (c.secs[k].k === 'kp' && c.secs[k].e.length) return k === e[2]; return false; });
var keyToId = {};
function keyOf(i) { return CH[E[i][1]].n + '|' + E[i][0]; }
for (var i0 = 0; i0 < E.length; i0++) { var k0 = keyOf(i0); if (!(k0 in keyToId)) keyToId[k0] = i0; }

function hl(text, terms) {
  if (!terms || !terms.length) return esc(text);
  var low = norm(text), marks = [];
  if (low.length !== text.length) return esc(text);
  terms.forEach(function (t) { var p = 0, i; while (t && (i = low.indexOf(t, p)) >= 0) { marks.push([i, i + t.length]); p = i + t.length; } });
  if (!marks.length) return esc(text);
  marks.sort(function (a, b) { return a[0] - b[0]; });
  var out = '', pos = 0;
  marks.forEach(function (m) { if (m[0] < pos) { if (m[1] > pos) { out += '<mark>' + esc(text.slice(pos, m[1])) + '</mark>'; pos = m[1]; } return; } out += esc(text.slice(pos, m[0])) + '<mark>' + esc(text.slice(m[0], m[1])) + '</mark>'; pos = m[1]; });
  return out + esc(text.slice(pos));
}
function applyTheme() {
  var dark = S.theme === 'dark' || (S.theme === 'auto' && window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  try { if (window.AndroidBridge) AndroidBridge.setDark(dark); } catch (e) {}
}
function applyFs() { document.documentElement.style.setProperty('--fs', FS[S.fs][1] + 'px'); }
try { var mq = matchMedia('(prefers-color-scheme: dark)'); if (mq.addEventListener) mq.addEventListener('change', applyTheme); else mq.addListener(applyTheme); } catch (e) {}
applyTheme(); applyFs();

/* 正文按章加载；全文检索用的纯文本从正文现算 */
var loaded = window.__PRE || {}, waiters = {}, TXT = new Array(E.length), txtDone = 0;
function plainOf(rec) {
  var parts = [];
  rec.b.forEach(function (b) { var v = b[1]; if (typeof v === 'string') parts.push(v); else for (var k in v) parts.push(typeof v[k] === 'string' ? v[k] : v[k].join(' ')); });
  if (rec.su) parts.push(rec.su); if (rec.sc) parts.push(rec.sc); (rec.xp || []).forEach(function (r) { parts.push(r.join(' ')); });
  return parts.join(' ').replace(/<[^>]+>/g, ' ').replace(/&gt;/g, '>').replace(/&lt;/g, '<').replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/\s+/g, ' ');
}
function indexCh(n) { var recs = loaded[n]; if (!recs || recs.__t) return; for (var id in recs) { TXT[id] = norm(plainOf(recs[id])); } Object.defineProperty(recs, '__t', { value: 1 }); txtDone++; }
window.DICT_LOAD = function (n, recs) { loaded[n] = recs; (waiters[n] || []).forEach(function (f) { f(recs); }); delete waiters[n]; };
function loadCh(n, cb) {
  if (loaded[n]) return cb(loaded[n]);
  if (waiters[n]) return waiters[n].push(cb);
  waiters[n] = [cb];
  var s = document.createElement('script'); s.src = 'data/c_' + n + '.js';
  s.onerror = function () { var w = waiters[n] || []; delete waiters[n]; w.forEach(function (f) { f(null); }); };
  document.head.appendChild(s);
}
var warmed = false;
(function warm(k) { if (k >= CH.length) { warmed = true; if (curTab === 'home' && top().r === 'home' && S.q) updateResults(); return; } if (!CH[k].secs.some(function (s) { return s.e.length; })) return warm(k + 1); loadCh(CH[k].n, function () { indexCh(CH[k].n); setTimeout(function () { warm(k + 1); }, window.__PRE ? 0 : 12); }); })(0);

/* ---------- 搜索 ---------- */
var SYN = [['显卡', ['gpu', 'nvidia-smi']], ['文件夹', ['目录']], ['删掉', ['删除', 'rm']], ['删', ['删除', 'rm']], ['跑', ['运行']], ['断线', ['断开连接', 'nohup', 'tmux']], ['掉线', ['断开连接']], ['崩了', ['崩溃']], ['挂了', ['崩溃']], ['崩', ['崩溃']],
  ['装包', ['pip install', 'uv add']], ['杀进程', ['终止进程', 'kill']], ['连服务器', ['ssh']], ['传文件', ['scp', 'rsync']], ['上传', ['scp', 'rsync']], ['改文件名', ['mv', '移动']], ['改名', ['mv', '移动']], ['新建', ['创建', 'mkdir']],
  ['解压', ['tar', '归档']], ['爆显存', ['oom', '显存不足']], ['显存不够', ['oom', '显存不足']], ['out of memory', ['oom']], ['checkout', ['switch', 'restore']], ['vscode', ['vs code']], ['一直重复', ['重复惩罚']], ['复读', ['重复惩罚']],
  ['看日志', ['tail', 'less']], ['撤回', ['撤销', '回退']], ['提交错了', ['撤销']], ['卡住', ['终止进程', 'ctrl + c']], ['卡死', ['终止进程']], ['画图', ['matplotlib']], ['没有权限', ['permission denied', 'chmod']], ['权限不够', ['permission denied', 'sudo']],
  ['不下降', ['损失曲线']], ['续训', ['检查点']], ['残差链接', ['残差连接']], ['乱码', ['字符编码', 'utf-8']], ['没报错', ['stderr', '2>&1']], ['退出', ['退出', ':q']], ['保存模型', ['检查点', 'state_dict']], ['继续', ['继续运行', 'nohup']]];
var SYNS = SYN.slice().sort(function (a, b) { return b[0].length - a[0].length; }), SYNK = {}; SYN.forEach(function (s) { SYNK[s[0]] = 1; });
var STOPQ = /怎么办|怎么样|怎么|如何|为什么|为啥|什么是|是什么|有什么|一下|可以|吗|呢|啊|了$/g;
var TA = D.terms.map(function (t) {   // 术语的各种叫法
  var en = [], zh = [];
  t[0].split(/\s*[\/／]\s*/).forEach(function (x) { var m = /^(.*?)\s*\((.*)\)\s*$/.exec(x); if (m) { en.push(norm(m[1])); en.push(norm(m[2])); } else en.push(norm(x)); });
  t[1].split(/\s*[\/／]\s*/).forEach(function (x) { zh.push(norm(x.replace(/\s*[（(].*$/, ''))); });
  return [en.filter(Boolean), zh.filter(Boolean)];
});
var ALIAS = {};
TA.forEach(function (a, i) { a[0].concat(a[1]).forEach(function (x) { (ALIAS[x] = ALIAS[x] || []).push(i); }); });
function isAscii(w) { return /^[\x21-\x7e ]+$/.test(w); }
function isWord(w) { return /^[a-z][a-z0-9]*$/.test(w); }
/* 2=整词或词头命中 1=夹在别的英文词中间 0=没有 */
function hit(text, w, word) {
  var p = text.indexOf(w); if (p < 0) return 0; if (!word) return 2;
  while (p >= 0) { var b = p > 0 ? text.charCodeAt(p - 1) : 32, okb = !(b >= 97 && b <= 122) && !(b >= 48 && b <= 57); if (okb) { var a = text.charCodeAt(p + w.length); if (!(a >= 97 && a <= 122) || w.length >= 4) return 2; } p = text.indexOf(w, p + 1); }
  return 1;
}
var OPTQ = /^(--?[A-Za-z0-9+][A-Za-z0-9_\-]*|-)$/;
function parseQ(q) {
  var opts = [], rest = [];
  String(q).replace(/[\uFF01-\uFF5E]/g, function (c) { return String.fromCharCode(c.charCodeAt(0) - 0xFEE0); }).replace(/\u3000/g, ' ').trim().split(/\s+/).forEach(function (t, k, arr) { if (OPTQ.test(t) && (t !== '-' || arr.length > 1) || (t === '--' && arr.length > 1)) opts.push(t); else if (t) rest.push(t); });
  var raw = norm(rest.join(' ')).trim(), groups = [], nums = [];
  raw.split(/\s+/).forEach(function (tok) {
    if (!tok) return;
    (tok.match(/[一-鿿]+|[^一-鿿]+/g) || []).forEach(function (w) {
      if (/^\d+(\.\d+)+$/.test(w)) { nums.push(w); return; }
      if (/[一-鿿]/.test(w)) { var w0 = w; w = w.replace(STOPQ, ''); if (!w && w0.length > 2) return; if (!w) w = w0; }
      groups.push({ w: w, alts: [w] });
    });
  });
  // 同义说法：一个词里认出几个说法，就拆成几个条件（都要满足）
  var whole = raw.replace(/\s+/g, ' '), G = [];
  groups.forEach(function (g) {
    var rem = g.w, parts = [];
    if (/[\u4e00-\u9fff]/.test(g.w) || SYNK[g.w]) SYNS.forEach(function (s) { if (s[0].length === 1 && g.w.length > 3 && g.w.indexOf(s[0]) !== 0) return; if (rem.indexOf(s[0]) >= 0) { parts.push({ w: s[0], alts: [s[0]].concat(s[1]) }); rem = rem.replace(s[0], ' '); } });
    if (!parts.length) { G.push(g); return; }
    rem.split(' ').forEach(function (r) { if (r.length >= 2) parts.push({ w: r, alts: [r] }); });
    parts.forEach(function (p) { G.push(p); });
  });
  groups = G;
  // 术语的中英互查；算式里补上空格的写法
  groups.forEach(function (g) {
    var ids = ALIAS[g.w] || [];
    if (!ids.length && isWord(g.w) && g.w.length >= 5) { for (var k in ALIAS) if (k.indexOf(g.w) === 0 && isWord(k)) { ids = ALIAS[k]; break; } }
    ids.slice(0, 3).forEach(function (ti) { TA[ti][1].concat(TA[ti][0]).slice(0, 3).forEach(function (a) { if (g.alts.indexOf(a) < 0) g.alts.push(a); }); });
    if (/\d[+\-*\/=]\d/.test(g.w)) g.alts.push(g.w.replace(/([+\-*\/=])/g, ' $1 '));
  });
  if (groups.length > 1 && ALIAS[whole]) { var g2 = { w: whole, alts: [whole], wholeOnly: 1 }; ALIAS[whole].slice(0, 2).forEach(function (ti) { TA[ti][1].concat(TA[ti][0]).slice(0, 3).forEach(function (a) { if (g2.alts.indexOf(a) < 0) g2.alts.push(a); }); }); groups = [g2]; }
  return { raw: raw, groups: groups, nums: nums, opts: opts };
}
function scoreAlt(i, w, word) {
  var k, cov, sc = 0;
  if ((k = hit(NAMEL[i], w, word))) { cov = Math.min(1, w.length / Math.max(1, NAMEL[i].length)); sc = k === 2 ? 45 + 30 * cov : 15; }
  else if (SECL[i] && (k = hit(SECL[i], w, word))) { cov = Math.min(1, w.length / SECL[i].length); sc = k === 2 ? 28 + 22 * cov + (FIRST[i] ? 4 : 0) : 10; }
  else if ((k = hit(YL[i], w, word))) sc = k === 2 ? 25 : 8;
  else {
    var x = TXT[i], p = x ? x.indexOf(w) : -1;
    if (p >= 0) { k = word ? hit(x, w, true) : 2; if (k === 2) { var cnt = 1; while (cnt < 6 && (p = x.indexOf(w, p + w.length)) >= 0) cnt++; sc = 6 + 2 * cnt; } else sc = 3; return [sc, 2]; }
    if (hit(CHL[i], w, word) === 2) return [9, 0];
    return null;
  }
  if (sc >= 28 && hit(CHL[i], w, word) === 2) sc += 8 + (FSEC[i] ? 22 : 0) + (FSEC[i] && FIRST[i] ? 6 : 0);
  return [sc, 0];
}
var EO = {}; (function () { for (var k in D.eo) EO[k] = ' ' + D.eo[k] + ' '; })();
/* 词条里有没有这个选项：40=原样出现 25=合写在一串字母里（-lt 里的 -t） 0=没有 */
function optIn(i, o) {
  var s = EO[i]; if (!s) return o === '-' ? 1 : 0;
  if (s.indexOf(' ' + o + ' ') >= 0) return 40;
  if (/^-[A-Za-z0-9]$/.test(o) && new RegExp(' -[A-Za-z]*' + o.charAt(1) + '[A-Za-z]* ').test(s.replace(/ -[A-Za-z]{6,} /g, ' '))) return 25;
  return 0;
}
function runSearch(P) {
  var out = [], G = P.groups, i, j, a, O = P.opts || [];
  for (i = 0; i < E.length; i++) {
    if (E[i][3] !== 'kp') continue;
    var total = 0, where = 0, ok = true;
    if (P.nums.length) {
      for (j = 0; j < P.nums.length; j++) { var nq = P.nums[j], no = NO[i]; if (no === nq) total += 200; else if (no.indexOf(nq + '.') === 0) total += 150; else if (SECN[i][0] === nq) total += 120; else { ok = false; break; } }
      if (!ok) continue;
    }
    for (j = 0; j < O.length && ok; j++) { var os = optIn(i, O[j]); if (!os) ok = false; else total += os; }
    for (j = 0; j < G.length && ok; j++) {
      var best = null;
      for (a = 0; a < G[j].alts.length; a++) { var w = G[j].alts[a], r = scoreAlt(i, w, isWord(w)); if (r) { var v = r[0] * (a ? 0.9 : 1); if (!best || v > best[0]) best = [v, r[1]]; } }
      if (!best) ok = false; else { total += best[0]; if (best[1] > where) where = best[1]; }
    }
    if (ok && total > 0) out.push([total, i, where]);
  }
  out.sort(function (x, y) { return y[0] - x[0] || x[1] - y[1]; });
  return out;
}
function lev(a, b, max) { if (Math.abs(a.length - b.length) > max) return 9; var prev = [], i, j; for (j = 0; j <= b.length; j++) prev[j] = j; for (i = 1; i <= a.length; i++) { var cur = [i], mn = i; for (j = 1; j <= b.length; j++) { cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a.charCodeAt(i - 1) === b.charCodeAt(j - 1) ? 0 : 1)); if (cur[j] < mn) mn = cur[j]; } if (mn > max) return 9; prev = cur; } return prev[b.length]; }
var VOCAB = null;
function vocab() { if (VOCAB) return VOCAB; var set = {}; NAMEL.concat(SECL).forEach(function (t) { (t.match(/[a-z][a-z0-9]{2,}/g) || []).forEach(function (w) { set[w] = 1; }); }); Object.keys(ALIAS).forEach(function (w) { if (isWord(w) && w.length > 2) set[w] = 1; }); return (VOCAB = Object.keys(set)); }
function search(q) {
  var P = parseQ(q), res = { terms: [], ents: [], near: false, ts: [] };
  res.P = P;
  if (!P.groups.length && !P.nums.length && !P.opts.length) return res;
  P.groups.forEach(function (g) { g.alts.forEach(function (a) { res.ts.push(a); }); }); P.nums.forEach(function (n) { res.ts.push(n); }); P.opts.forEach(function (o) { res.ts.push(o.toLowerCase()); });
  // 术语
  var whole = P.raw.replace(/\s+/g, ' ');
  TA.forEach(function (a, i) {
    var all = a[0].concat(a[1]), sc = 0;
    if (P.opts.length) return; if (whole && all.indexOf(whole) >= 0) sc = 100; else if (P.groups.length && !P.nums.length) { sc = 40; for (var j = 0; j < P.groups.length; j++) { var w = P.groups[j].w, h = 0; all.forEach(function (x) { if (x === w) h = Math.max(h, 3); else if (hit(x, w, isWord(w)) === 2) h = Math.max(h, 2); }); if (!h) { sc = 0; break; } sc += h * 5; } }
    if (sc > 0) res.terms.push([sc, i]);
  });
  res.terms.sort(function (x, y) { return y[0] - x[0] || x[1] - y[1]; });
  res.ents = runSearch(P);
  if (res.ents.length || P.nums.length || P.opts.length) return res;
  // 放宽 1：去掉同义词替换掉的那部分以外的字、英文拼错
  var changed = false, G2 = P.groups.map(function (g) {
    var w = g.w;
    if (isWord(w) && w.length >= 4) { var best = null, bd = 9, mx = w.length >= 7 ? 2 : 1; vocab().forEach(function (v) { var d = lev(w, v, mx); if (d < bd || (d === bd && best && v.length < best.length)) { bd = d; best = v; } }); if (best && bd <= mx) { changed = true; return { w: best, alts: [best] }; } }
    var m = /^([a-z]+)(\d+)$/.exec(w); if (m) { changed = true; return { w: m[1], alts: [m[1]] }; }
    return g;
  });
  if (changed) { res.ents = runSearch({ groups: G2, nums: [] }); if (res.ents.length) { res.near = true; res.ts = G2.map(function (g) { return g.w; }); return res; } }
  // 放宽 2：中文长词拆成两个字两个字地找，命中一半以上就算
  var grams = [];
  P.groups.forEach(function (g) { g.alts.forEach(function (w, ai) { if (/[一-鿿]/.test(w)) { if (w.length <= 2) grams.push(w); else for (var k = 0; k < w.length - 1; k++) grams.push(w.substr(k, 2)); } else if (w.length >= 2) grams.push(w); }); });
  grams = grams.filter(function (x, k) { return grams.indexOf(x) === k; });
  if (grams.length >= 2 || (grams.length === 1 && grams[0].length >= 2)) {
    var need = Math.max(1, Math.floor(grams.length / 2)), out = [];
    for (var i = 0; i < E.length; i++) {
      if (E[i][3] !== 'kp') continue; var n = 0, sc = 0;
      for (var k2 = 0; k2 < grams.length; k2++) { var w2 = grams[k2]; if (NAMEL[i].indexOf(w2) >= 0) { n++; sc += 12; } else if (SECL[i].indexOf(w2) >= 0) { n++; sc += 8; } else if (YL[i].indexOf(w2) >= 0) { n++; sc += 5; } else if (TXT[i] && TXT[i].indexOf(w2) >= 0) { n++; sc += 2; } }
      if (n >= need) out.push([sc + n * 10, i, 1]);
    }
    out.sort(function (x, y) { return y[0] - x[0] || x[1] - y[1]; });
    if (out.length) { res.ents = out.slice(0, 40); res.near = true; res.ts = grams; }
  }
  return res;
}
window.__search = search;
function snippet(i, ts) {
  var t = TXT[i]; if (!t) return E[i][4];
  var best = -1; for (var j = 0; j < ts.length; j++) { if (NAMEL[i].indexOf(ts[j]) >= 0 || YL[i].indexOf(ts[j]) >= 0) continue; var p = t.indexOf(ts[j]); if (p >= 0 && (best < 0 || p < best)) best = p; }
  if (best < 0) return E[i][4];
  var st = Math.max(0, best - 30);
  return (st > 0 ? '…' : '') + t.slice(st, st + 110) + (st + 110 < t.length ? '…' : '');
}
function entRow(i, ts, where) {
  var e = E[i], c = CH[e[1]], d = where === 2 ? snippet(i, ts) : e[4], sn = SECN[i][1];
  return '<button class="row" data-go="e/' + i + '"><span class="t">' + (NO[i] ? '<span class="no">' + hl(NO[i], ts) + '</span>' : '') + hl(shown(i), ts) + '</span>' + (d ? '<span class="d">' + hl(d, ts) + '</span>' : '') +
    '<span class="m">' + chLabel(c) + '　' + esc(sn && sn !== NAME[i] && shown(i) === NAME[i] ? sn : c.t) + '</span></button>';
}
function termRow(i, ts, badge) {
  var t = D.terms[i], to = t[5] >= 0 ? t[5] : -1;
  return '<' + (to >= 0 ? 'button' : 'div') + ' class="row term"' + (to >= 0 ? ' data-go="e/' + to + '"' : '') + '><span class="t">' + hl(t[0], ts) + '<span class="zh">' + hl(t[1], ts) + '</span>' + (badge ? '<span class="badge">术语</span>' : '') + '</span><span class="d">' + t[2] + '</span><span class="m">' + esc(t[3]) + (to >= 0 ? '<span class="go">看讲解 ›</span>' : '') + '</span></' + (to >= 0 ? 'button' : 'div') + '>';
}
/* ---------- 命令和选项 ---------- */
var CMDI = {}; D.cmds.forEach(function (c, i) { CMDI[c[0]] = i; });
function optOf(ci, o) { var L = D.cmds[ci][2]; for (var k = 0; k < L.length; k++) if (L[k][0] === o) return L[k][1]; return null; }
function optRow(cmd, o, mean, extra) { return '<button class="row opt" data-go="cmd/' + encodeURIComponent(cmd) + '"><span class="t"><code>' + esc(cmd + ' ' + o) + '</code></span><span class="d">' + esc(mean) + '</span>' + (extra ? '<span class="m">' + extra + '</span>' : '') + '</button>'; }
function cmdCandidates(P) {   // 查询里写了命令名的话，只看那个命令
  var w = P.raw; if (!w) return null;
  var out = []; D.cmds.forEach(function (c, i) { if (c[0] === w) out.push(i); });
  if (!out.length) { var f = P.groups.length ? P.groups[0].w : ''; D.cmds.forEach(function (c, i) { if (c[0] === f || c[0].split(' ')[0] === f) out.push(i); }); }
  return out.length ? out : null;
}
function optCards(P) {
  var html = '', cand = cmdCandidates(P), all = cand || D.cmds.map(function (c, i) { return i; });
  P.opts.forEach(function (o) {
    var rows = [], n = 0;
    all.forEach(function (ci) { var m = optOf(ci, o); if (m) { rows.push(optRow(D.cmds[ci][0], o, m, esc(D.cmds[ci][1]))); n++; } });
    // 一串字母合写：拆开，一个字母一个字母解释
    if (!n && /^-[A-Za-z]{2,}$/.test(o)) {
      all.forEach(function (ci) {
        var letters = o.slice(1).split(''), ms = letters.map(function (x) { return optOf(ci, '-' + x); });
        if (D.cmds[ci][0] !== '[' && ms.every(Boolean) && letters.every(function (x, k) { return letters.indexOf(x) === k; })) { rows.push('<div class="row opt"><span class="t"><code>' + esc(D.cmds[ci][0] + ' ' + o) + '</code>　是 ' + letters.length + ' 个选项合在一起写</span>' + letters.map(function (x, k) { return '<span class="d"><code>-' + x + '</code>　' + esc(ms[k]) + '</span>'; }).join('') + '<span class="m"><button class="lnk" data-go="cmd/' + encodeURIComponent(D.cmds[ci][0]) + '">' + esc(D.cmds[ci][0]) + ' 的全部选项</button></span></div>'); n++; }
      });
    }
    D.tbo.forEach(function (t) { if (t[0] === o && (!cand || cand.some(function (ci) { return D.cmds[ci][0].split(' ')[0] === t[1].split(' ')[0]; }))) { rows.push('<button class="row opt" data-go="e/' + t[3] + '"><span class="t"><code>' + esc(t[1] + ' ' + o) + '</code></span><span class="d">' + esc(t[2]) + '</span><span class="m">教材 ' + esc(E[t[3]][0]) + '</span></button>'); n++; } });
    // 大小写不同的提醒
    var other = [], alt = o.length === 2 ? (o.charAt(1) === o.charAt(1).toLowerCase() ? '-' + o.charAt(1).toUpperCase() : '-' + o.charAt(1).toLowerCase()) : null;
    if (alt && alt !== o) all.forEach(function (ci) { var m = optOf(ci, alt); if (m && other.length < 4) other.push(optRow(D.cmds[ci][0], alt, m, '')); });
    html += '<div class="count">选项 <code>' + esc(o) + '</code>' + (cand ? '' : '：' + n + ' 个命令里有') + '</div>';
    html += n ? '<div class="rows">' + foldRows(rows, 8) + '</div>' : '<p class="note">速查表里没有收这个选项。可以在终端里敲 <code>命令 --help</code> 看它的说明。</p>';
    if (other.length) html += '<div class="count">注意大小写：<code>' + esc(alt) + '</code> 是另一个选项</div><div class="rows">' + other.join('') + '</div>';
  });
  return html;
}
function foldRows(rows, n) { if (rows.length <= n + 1) return rows.join(''); return rows.slice(0, n).join('') + '<div hidden>' + rows.slice(n).join('') + '</div><button class="tog" style="width:auto;margin:10px 18px" data-act="tog" data-t="再看 ' + (rows.length - n) + ' 个">再看 ' + (rows.length - n) + ' 个</button>'; }
function cmdCard(ci) {
  var c = D.cmds[ci];
  return '<div class="count">命令</div><div class="rows"><button class="row opt" data-go="cmd/' + encodeURIComponent(c[0]) + '"><span class="t"><code>' + esc(c[0]) + '</code></span><span class="d">' + esc(c[1]) + '</span><span class="m">' + c[2].length + ' 个选项<span class="go">看全部 ›</span></span></button></div>';
}
function pageCmds(isBack) {
  setBar({ back: stacks[curTab].length > 1, title: '<b>命令选项速查</b>' });
  show('<p class="sub" style="padding-top:14px">' + D.cmds.length + ' 个命令。这部分是补充内容，不是教材原文；按 Linux 上的常见用法写的。也可以直接在“查词”里搜 <code>-t</code>、<code>ls -lt</code>、<code>tar -czvf</code>。</p><div class="rows">' + D.cmds.map(function (c) { return '<button class="row opt" data-go="cmd/' + encodeURIComponent(c[0]) + '"><span class="t"><code>' + esc(c[0]) + '</code></span><span class="d" style="-webkit-line-clamp:1">' + esc(c[1]) + '</span></button>'; }).join('') + '</div>', isBack);
}
function pageCmd(isBack, name) {
  var ci = CMDI[name]; if (ci == null) return nav('cmds', true);
  var c = D.cmds[ci];
  setBar({ back: true, title: '命令选项速查', aa: true });
  var html = '<article class="entry"><h1 class="hw"><code style="background:none;padding:0">' + esc(c[0]) + '</code></h1><div class="gist">' + esc(c[1]) + '</div>';
  if (c[3] >= 0) html += '<button class="pitlink" style="background:var(--blue-bg);color:var(--blue)" data-go="e/' + c[3] + '"><span>教材里的讲解：' + esc(E[c[3]][0]) + '</span><span>›</span></button>';
  html += c[2].length ? '<table class="opts" style="margin-top:18px"><tbody>' + c[2].map(function (o) { return '<tr><td><code>' + esc(o[0]) + '</code></td><td>' + esc(o[1]) + '</td></tr>'; }).join('') + '</tbody></table>' : '<p class="note">这个命令平时基本不带选项。</p>';
  var tb = D.tbo.filter(function (t) { return t[1].split(' ')[0] === c[0].split(' ')[0]; });
  if (tb.length) html += '<div class="sh" style="position:static;margin-top:26px">教材里还出现过</div><table class="opts"><tbody>' + tb.map(function (t) { return '<tr><td><code>' + esc(t[0]) + '</code></td><td>' + esc(t[2]) + '</td></tr>'; }).join('') + '</tbody></table>';
  show(html + '<p class="note" style="margin-top:18px">补充内容，不是教材原文。不同系统上个别选项会有出入，拿不准时敲 <code>' + esc(c[0].split(' ')[0]) + ' --help</code> 核对。</p></article>', isBack);
}
function chByN(n) { for (var i = 0; i < CH.length; i++) if (CH[i].n === String(n)) return CH[i]; return null; }

/* ---------- 页面栈：每个底部标签各有一摞，切换时各自记住位置 ---------- */
var stacks = { home: [{ r: 'home', s: 0 }], course: [{ r: 'course', s: 0 }], toc: [{ r: 'toc', s: 0 }], me: [{ r: 'me', s: 0 }] }, curTab = 'home';
function top() { var st = stacks[curTab]; return st[st.length - 1]; }
function nav(r, replace) { var st = stacks[curTab]; top().s = view.scrollTop; if (replace) st[st.length - 1] = { r: r, s: 0 }; else st.push({ r: r, s: 0 }); render(false); }
function back() { var st = stacks[curTab]; if (st.length > 1) { st.pop(); render(true); return true; } return false; }
function switchTab(name, reset) { top().s = view.scrollTop; if (name === curTab || reset) { stacks[name] = [{ r: name, s: 0 }]; curTab = name; render(false); } else { curTab = name; render(true); } }
function setBar(o) {
  if (!o) { bar.innerHTML = ''; return; }
  bar.innerHTML = '<div class="bar-row">' + (o.back ? '<button class="ib" data-act="back" aria-label="返回">' + ICON.back + '</button>' : '<span style="width:14px"></span>') +
    '<div class="bar-t">' + o.title + '</div>' + (o.aa ? '<button class="ib aa" data-act="aa" aria-label="调整字号">Aa</button>' : '') +
    (o.star != null ? '<button class="ib' + (isFav(o.star) ? ' on' : '') + '" data-act="star" data-id="' + o.star + '" aria-label="收藏">' + ICON.star + '</button>' : '') + '</div>';
}
function show(html, isBack) { view.innerHTML = '<div class="page">' + html + '</div>'; view.scrollTop = isBack ? top().s : 0; }
function render(isBack) {
  [].forEach.call(tabs.children, function (b) { b.classList.toggle('on', b.getAttribute('data-tab') === curTab); });
  var r = top().r, p = r.split('/');
  if (p[0] === 'home') pageHome(isBack);
  else if (p[0] === 'toc') pageToc(isBack, p[1]);
  else if (p[0] === 'terms') pageTerms(isBack);
  else if (p[0] === 'me') pageMe(isBack);
  else if (p[0] === 'mine') pageMine(isBack);
  else if (p[0] === 'course') pageCourse(isBack);
  else if (p[0] === 'cc') pageCourseCh(isBack, p[1]);
  else if (p[0] === 'l') pageLesson(isBack, p[1]);
  else if (p[0] === 'rv') pageReview(isBack);
  else if (p[0] === 'fx') pageFinal(isBack);
  else if (p[0] === 'x') pageExam(isBack, p[1]);
  else if (p[0] === 'cmds') pageCmds(isBack);
  else if (p[0] === 'cmd') pageCmd(isBack, decodeURIComponent(r.slice(4)));
  else if (p[0] === 'c') pageChapter(isBack, p[1], p[2]);
  else if (p[0] === 'e') pageEntry(isBack, +p[1]);
}

/* ---------- 查词 ---------- */
function pageHome(isBack) {
  setBar(null);
  show('<div class="brand" id="brand">大模型字典</div>' +
    '<div class="search"><label class="sbox">' + ICON.search + '<input id="q" type="search" enterkeyhint="search" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="输入知识点、命令或术语" value="' + esc(S.q) + '"><button class="clr" data-act="clr" aria-label="清空">' + ICON.x + '</button></label></div><div id="res"></div>', false);
  updateResults();
  if (isBack) view.scrollTop = top().s;
  var q = document.getElementById('q');
  q.addEventListener('input', function () { S.q = q.value; S.showN = 30; clearTimeout(pageHome.t); pageHome.t = setTimeout(function () { updateResults(); view.scrollTop = 0; }, 40); });
  q.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') q.blur(); });
}
function recentIds() { return S.recent.map(function (k) { return keyToId[k]; }).filter(function (x) { return x != null; }); }
function updateResults() {
  var res = document.getElementById('res'); if (!res) return;
  var q = S.q.trim();
  document.getElementById('brand').hidden = !!q; view.querySelector('.clr').hidden = !S.q;
  if (!q) {
    var rec = recentIds().slice(0, 6), html = '';
    if (rec.length) { var c0 = CH[E[rec[0]][1]]; html += '<div class="cap">接着看</div><div class="tiles"><button class="wide2" data-tabgo="toc" data-go="c/' + c0.n + '">' + chLabel(c0) + '　' + esc(c0.t) + '</button></div>'; }
    html += '<div class="cap">常用</div><div class="tiles"><button data-tabgo="toc" data-go="cmds">命令选项速查<small>搜 -t、ls -lt 也行</small></button><button data-tabgo="toc" data-go="terms">术语表<small>' + D.terms.length + ' 个词</small></button><button class="v wide2" data-tabgo="me" data-go="mine">我常错的 ' + D.mine.length + ' 条<small style="color:inherit;font-weight:400">来自你的学习笔记</small></button></div>';
    if (rec.length) html += '<div class="cap">最近查过</div><div class="rows">' + rec.map(function (i) { return entRow(i, [], 0); }).join('') + '</div>';
    html += '<div class="cap">按部分翻</div><div class="tiles">' + D.parts.map(function (p, i) { return '<button data-tabgo="toc" data-go="toc/' + i + '">' + esc(p[1]) + '<small>第' + p[0] + '部分</small></button>'; }).join('') + '</div>';
    res.innerHTML = html; return;
  }
  var m = /^第?\s*(\d+)\s*章$/.exec(norm(q)), html2 = '';
  if (m && chByN(m[1])) { res.innerHTML = '<div class="count">章节</div>' + chRow(chByN(m[1])); return; }
  var r = search(q), ts = r.ts, P = r.P, pre = '';
  if (P && P.opts.length) pre = optCards(P);
  else if (P && P.raw && CMDI[P.raw] != null) pre = cmdCard(CMDI[P.raw]);
  var exact = r.terms.filter(function (x) { return x[0] >= 100; }).slice(0, 1), part = r.terms.filter(function (x) { return x[0] < 100; }).slice(0, 2);
  if (pre && !r.ents.length) { res.innerHTML = pre; return; }
  if (!r.terms.length && !r.ents.length) { res.innerHTML = '<div class="empty"><b>没有查到“' + esc(q) + '”</b>少打几个字，或者换个说法再试。' + (!warmed ? '正文还在准备，稍等会自动再搜一次。' : '') + '<button class="btn" data-tab="toc">去目录里翻</button></div>'; return; }
  html2 += pre;
  html2 += r.near ? '<div class="near">没有完全一样的，下面是最接近的 ' + r.ents.length + ' 个</div>' : '<div class="count">' + (pre ? '教材里用到的地方：' : '') + r.ents.length + ' 个知识点</div>';
  html2 += '<div class="rows">' + exact.map(function (x) { return termRow(x[1], ts, true); }).join('') + r.ents.slice(0, S.showN).map(function (x) { return entRow(x[1], ts, x[2]); }).join('') +
    (r.ents.length <= S.showN ? part.map(function (x) { return termRow(x[1], ts, true); }).join('') : '') + '</div>';
  if (r.ents.length > S.showN) html2 += '<button class="btn" data-act="more">再显示 ' + Math.min(50, r.ents.length - S.showN) + ' 个</button>';
  res.innerHTML = html2;
}

/* ---------- 目录 ---------- */
function kpCount(c) { var n = 0; c.secs.forEach(function (s) { if (s.k === 'kp') n += s.e.length; }); return n; }
function chRow(c) { return '<button class="ch" data-go="c/' + c.n + '"><span class="n">' + esc(c.n) + '</span><span class="t">' + esc(c.t) + '</span><span class="c">' + kpCount(c) + ' 条</span></button>'; }
function pageToc(isBack, part) {
  setBar({ title: '<b>目录</b>' });
  var html = '<div class="chipbar">' + D.parts.map(function (p, i) { return '<button data-act="jump" data-to="part-' + i + '">' + p[0] + '</button>'; }).join('') + '<button data-act="jump" data-to="part-x">附录</button></div>';
  D.parts.forEach(function (p, i) {
    var rows = ''; for (var n = p[2]; n <= p[3]; n++) { var c = chByN(n); if (c) rows += chRow(c); }
    if (rows) html += '<div class="part" id="part-' + i + '"><b>第' + p[0] + '部分</b>' + esc(p[1]) + '</div>' + rows;
  });
  html += '<div class="part" id="part-x"><b>附录</b>速查</div><button class="ch" data-go="terms"><span class="n">A</span><span class="t">术语表</span><span class="c">' + D.terms.length + ' 个</span></button><button class="ch" data-go="cmds"><span class="n">›</span><span class="t">命令选项速查</span><span class="c">' + D.cmds.length + ' 个命令</span></button>' + CH.filter(function (c) { return !/^\d+$/.test(c.n) && c.n !== 'A'; }).map(chRow).join('');
  show(html, isBack);
  if (!isBack && part != null) jumpTo('part-' + part);
}
function jumpTo(id) { var el = document.getElementById(id); if (!el) return; var cb = view.querySelector('.chipbar'), off = cb ? cb.offsetHeight : 0, sr = view.querySelector('.search'); if (sr && !cb) off = sr.offsetHeight; var prev = el.style.position; el.style.position = 'static'; view.scrollTop = el.offsetTop - off + 1; el.style.position = prev; }
function pageChapter(isBack, n, si) {
  var c = chByN(n); if (!c) return switchTab('toc', true);
  setBar({ back: true, title: chLabel(c) });
  var html = '<h1 class="ct">' + esc(c.t) + '</h1>' + (c.pre ? '<p class="sub">要先学过：' + esc(c.pre) + '</p>' : ''), pit = '', secs = '';
  c.secs.forEach(function (s, idx) {
    if (s.k === 'pitsec' && s.e.length) pit = '<button class="pitlink" data-go="e/' + s.e[0] + '"><span>本章常见误区</span><span>›</span></button>';
    else if (s.k === 'kp' && s.e.length) secs += '<div class="sh" id="sec-' + idx + '">' + esc(s.t) + '</div><div class="rows">' + s.e.map(function (i) {
      var e = E[i]; return '<button class="row" data-go="e/' + i + '"><span class="t">' + (e[0] === s.t ? '本节概览' : (NO[i] ? '<span class="no">' + NO[i] + '</span>' : '') + esc(NAME[i])) + '</span>' + (e[4] ? '<span class="d" style="-webkit-line-clamp:2">' + esc(e[4]) + '</span>' : '') + '</button>';
    }).join('') + '</div>';
  });
  show(html + pit + secs, isBack);
  if (!isBack && si != null) jumpTo('sec-' + si);
}

/* ---------- 词条 ---------- */
function fix(html) {
  html = html.replace(/<div class="tw">(<table>[\s\S]*?<\/table>)<\/div>/g, function (_, t) { var head = /<tr[^>]*>([\s\S]*?)<\/tr>/.exec(t), cols = head ? (head[1].match(/<t[hd]/g) || []).length : 0; return '<div class="tw' + (cols >= 4 ? ' wide' : '') + '">' + t + '</div>'; });
  return html.replace(/<pre><code>([\s\S]*?)<\/code><\/pre>/g, function (_, code) {
    var out = [], inStr = false, sh = false;
    code.split('\n').forEach(function (ln) {
      var q3 = (ln.match(/&quot;&quot;&quot;|"""|'''|&#39;&#39;&#39;/g) || []).length, was = inStr; if (q3 % 2) inStr = !inStr;
      if (!was && !inStr && !q3) {
        var m = /^(\s*)(.*?\S)\s{2,}(#(?!!).*)$/.exec(ln);
        if (m && m[2].charAt(0) !== '#') { out.push((/^\$ /.test(m[2]) ? '' : m[1]) + '<span class="cm">' + m[3] + '</span>'); ln = m[1] + m[2]; }
        else ln = ln.replace(/^(\s*)(#(?!!).*)$/, '$1<span class="cm">$2</span>');
      }
      if (/^\$ /.test(ln)) { sh = true; ln = '<span class="pr">$ </span>' + ln.slice(2); }
      out.push(ln);
    });
    return '<div class="code' + (sh ? ' sh' : '') + '"><button class="cp" data-act="copy">复制</button><pre><code>' + out.join('\n') + '</code></pre></div>';
  });
}
function plainLen(h) { return h.replace(/<[^>]+>/g, '').length; }
function quizHtml(q) { return '<div class="item quiz"><div class="qq">' + fix(q.q) + '</div><button class="ans" data-act="ans">看答案</button><div class="aa" hidden>' + fix(q.a) + '</div></div>'; }
function exHtml(x) { return '<div class="item">' + (x.l ? '<div class="lead">' + x.l + '</div>' : '') + fix(x.h) + '</div>'; }
function pitHtml(p) { var must = p.g === '必须掌握'; return '<div class="item pit' + (must ? ' must' : '') + '">' + (must ? '<span class="src">必须掌握</span>' : '') + fix(p.h) + '</div>'; }
function rowPit(c, hd) { hd = hd && hd.length >= 3 ? hd : ['现象', '原因', '解决']; return '<div class="item pit"><div class="sym">' + c[0] + '</div><div class="fx"><span class="k">' + esc(hd[1]) + '</span>' + c[1] + '</div><div class="fx"><span class="k">' + esc(hd[2]) + '</span>' + c[2] + '</div>' + (c[3] ? '<div class="say">一句人话：' + c[3] + '</div>' : '') + '</div>'; }
function mineHtml(m, links) {
  return '<div class="item mine"><span class="src">你自己错过</span><b>' + m.w + '</b>' + (m.x ? '<div>错的样子：' + m.x + '</div>' : '') + '<div>怎么防：' + m.f + '</div>' +
    (links && m.e && m.e.length ? '<div class="from">' + m.e.map(function (i) { return '<button class="lnk" data-go="e/' + i + '">' + esc(E[i][0]) + '</button>'; }).join('<br>') + '</div>' : '') + '</div>';
}
function fold(items, n, word) {
  if (items.length <= n + 1) return items.join('');
  var label = '再看 ' + (items.length - n) + ' ' + word;
  return items.slice(0, n).join('') + '<div class="item" hidden>' + items.slice(n).join('') + '</div><button class="tog" data-act="tog" data-t="' + label + '">' + label + '</button>';
}
function sense(cls, ch, name, body) { return '<section class="sense ' + cls + '"><div class="hd"><span class="seal">' + ch + '</span>' + name + '</div><div class="body">' + body + '</div></section>'; }
function pageEntry(isBack, id) {
  var e = E[id]; if (!e) return switchTab('home', true);
  var c = CH[e[1]];
  setBar({ back: true, title: chLabel(c) + '　' + esc(c.t), aa: true, star: e[3] === 'kp' ? id : null });
  var want = top();
  loadCh(c.n, function (recs) {
    if (top() !== want) return;
    if (!recs || !recs[id]) { show('<div class="empty"><b>这一条没有打开</b>返回再点一次。</div>', false); return; }
    show('<article class="entry">' + entryHtml(id, e, c, recs[id], recs) + '</article>', isBack);
    if (e[3] === 'kp') { var key = keyOf(id); S.recent = [key].concat(S.recent.filter(function (k) { return k !== key; })).slice(0, 30); store.set('recent', S.recent); }
  });
}
function entryHtml(id, e, c, rec, recs) {
  var sec = c.secs[e[2]], B = rec.b, html = '';
  var g = { y: [], e: [], p: [], q: [], m: [] }, body = [];
  B.forEach(function (b) { if (b[0] === 'h') body.push('<h4>' + esc(b[1]) + '</h4>'); else if (b[0] === 't') body.push(fix(b[1])); else if (g[b[0]]) g[b[0]].push(b[1]); });
  if (e[3] === 'pitsec') {
    return '<h1 class="hw">' + esc(c.t) + '：常见误区</h1><p class="sub">' + g.m.length + ' 条。先对“现象”，对上了再看原因和解决办法。</p><div style="margin-top:16px">' + g.m.map(function (m) { return rowPit(m.c, m.hd); }).join('') + '</div>' + (body.length ? '<div class="sense">' + body.join('') + '</div>' : '');
  }
  html += '<div class="crumb">' + (sec && sec.t !== e[0] ? '<button class="lnk" style="text-decoration:none" data-go="c/' + c.n + '/' + e[2] + '">' + esc(sec.t) + '</button>' : '') + '</div>';
  html += '<h1 class="hw">' + (NO[id] ? '<span class="no">' + NO[id] + '</span>' : '') + esc(NAME[id]) + '</h1>';
  var yi = g.y.map(function (y) { return '<div class="gist">' + fix(y) + '</div>'; }).join(''), bodyHtml = body.join('');
  if (bodyHtml) {
    if (!yi || plainLen(bodyHtml) < 220) yi += '<div style="margin-top:8px">' + bodyHtml + '</div>';
    else yi += '<div class="item" hidden>' + bodyHtml + '</div><button class="tog" data-act="tog" data-t="展开讲解">展开讲解</button>';
  }
  if (yi) html += sense('s-yi', '义', '要点', yi);
  var ex = g.e.map(exHtml).concat(g.q.map(quizHtml)), exNote = '';
  if (!ex.length && rec.se != null && recs[rec.se]) {
    var pick = null; recs[rec.se].b.forEach(function (b) { if (!pick && b[0] === 'e') pick = b; }); recs[rec.se].b.forEach(function (b) { if (!pick && b[0] === 'q') pick = b; });
    if (pick) { ex.push(pick[0] === 'e' ? exHtml(pick[1]) : quizHtml(pick[1])); exNote = '<div class="from">这条教材没给例子，借的是旁边 <button class="lnk" data-go="e/' + rec.se + '">' + esc(E[rec.se][0]) + '</button> 的</div>'; }
  }
  if (!ex.length && rec.sc) ex.push('<div class="item"><span class="badge" style="margin:0 0 6px">补充</span><div>' + fix(rec.sc) + '</div></div>');
  if (ex.length) html += sense('s-ex', '例', '案例', fold(ex, g.e.length ? Math.min(2, g.e.length) : 1, '个例子') + exNote);
  var mine = (rec.mi || []).map(function (i) { return mineHtml(D.mine[i], false); }).join('');
  var pits = g.p.map(pitHtml);
  (rec.xp || []).forEach(function (r) { pits.push(rowPit(r)); });
  if (rec.su) pits.push('<div class="item pit"><span class="src">补充</span>' + rec.su + '</div>');
  var own = pits.length > 0;
  if (!own && rec.sp) rec.sp.forEach(function (sid) {
    var s = recs[sid], pp = null; if (!s) return; s.b.forEach(function (b) { if (!pp && b[0] === 'p') pp = b[1]; });
    if (pp) pits.push('<div class="item pit">' + fix(pp.h) + '<div class="from">这条教材没单独写易错点，这是旁边 <button class="lnk" data-go="e/' + sid + '">' + esc(E[sid][0]) + '</button> 里的</div></div>');
  });
  var extra = '';
  if (!pits.length && !mine && rec.cp) { var ps = null; c.secs.forEach(function (s) { if (s.k === 'pitsec' && s.e.length) ps = s.e[0]; }); if (ps != null) extra = '<button class="pitlink" style="margin-top:0" data-go="e/' + ps + '"><span>这条教材没单独写，看本章常见误区</span><span>›</span></button>'; }
  if (pits.length || mine || extra) html += sense('s-pit', '误', '容易出错', mine + (mine && pits.length ? '<div style="height:14px"></div>' : '') + fold(pits, 3, '条易错点') + extra);
  var pv = null, nx = null, k;
  for (k = id - 1; k >= 0 && pv == null; k--) if (E[k][3] === 'kp') pv = k;
  for (k = id + 1; k < E.length && nx == null; k++) if (E[k][3] === 'kp') nx = k;
  function lab(i, w) { return E[i][1] !== e[1] ? w.charAt(0) + '一章' : w; }
  html += '<div class="pn">' + (pv != null ? '<button class="prev" data-go="e/' + pv + '" data-rep="1"><small>' + lab(pv, '上一条') + '</small>' + esc(E[pv][0]) + '</button>' : '') + (nx != null ? '<button class="next" data-go="e/' + nx + '" data-rep="1"><small>' + lab(nx, '下一条') + '</small>' + esc(E[nx][0]) + '</button>' : '') + '</div>';
  return html;
}

/* ---------- 课程（正文来自完整的白话版教材） ---------- */
var CDONE = store.get('cdone', {}), CQ = store.get('cq2', {}), DAY = 86400000, TERSE = store.get('terse', false);
var KC = window.__KPRE || {}, kwait = {};
window.DICT_COURSE = function (n, d) { KC[n] = d; (kwait[n] || []).forEach(function (f) { f(d); }); delete kwait[n]; };
function loadK(n, cb) {
  if (KC[n]) return cb(KC[n]); if (kwait[n]) return kwait[n].push(cb); kwait[n] = [cb];
  var s = document.createElement('script'); s.src = 'data/k_' + n + '.js';
  s.onerror = function () { var w = kwait[n] || []; delete kwait[n]; w.forEach(function (f) { f(null); }); };
  document.head.appendChild(s);
}
var LESSONS = [], LIDX = {};
D.course.forEach(function (c) { c.lessons.forEach(function (l, k) { LIDX[l.no] = LESSONS.length; LESSONS.push({ c: c, l: l, k: k }); }); });
function courseCh(n) { for (var i = 0; i < D.course.length; i++) if (D.course[i].n === n) return D.course[i]; return null; }
var MINE_BY = {}; D.mine.forEach(function (m, i) { (m.e || []).forEach(function (id) { (MINE_BY[id] = MINE_BY[id] || []).push(i); }); });
function lessonData(L) { var d = KC[L.c.n]; return d ? d.lessons[L.k] : null; }
function nextLesson() { for (var i = 0; i < LESSONS.length; i++) if (!CDONE[LESSONS[i].l.no]) return LESSONS[i]; return null; }
function lessonQuizzes(L) {
  var out = [], d = lessonData(L), n = 0; if (!d) return out;
  function scan(bl, name) { bl.forEach(function (b) { if (b[0] === 'q') { out.push({ k: L.l.no + '#' + n, q: b[1], no: L.l.no, name: name }); n++; } }); }
  scan(d.lead, L.l.t); d.pts.forEach(function (p) { scan(p.b, p.t); });
  return out;
}
/* 到期该复习的题：只从学完的课里出；一周内问过的不出；连续答对 3 次的不再出 */
function dueQuizzes() {
  var now = Date.now(), out = [];
  LESSONS.forEach(function (L) { var t0 = CDONE[L.l.no]; if (!t0) return; for (var i = 0; i < (L.l.q || 0); i++) { var k = L.l.no + '#' + i, s = CQ[k]; if (s && (s.s >= 3 || now - s.t < 7 * DAY)) continue; if (!s && now - t0 < DAY / 2) continue; out.push({ k: k, L: L, i: i }); } });
  return out;
}
function shuffle(a) { for (var i = a.length - 1; i > 0; i--) { var j = Math.floor(Math.random() * (i + 1)), t = a[i]; a[i] = a[j]; a[j] = t; } return a; }
function cquiz(q, src) {
  return '<div class="item quiz" data-k="' + esc(q.k) + '">' + (src ? '<div class="from" style="margin:0 0 6px">' + esc(q.no.replace(/-.*/, ' 章') + '　' + q.name) + '</div>' : '<span class="src" style="color:var(--green);font-size:13px;font-weight:700">想一想</span>') + '<div class="qq">' + fix(q.q.q) + '</div><button class="ans" data-act="ans">看答案</button><div class="aa" hidden>' + fix(q.q.a) +
    '<div class="grade"><button data-act="grade" data-v="1">我答对了</button><button data-act="grade" data-v="0">我答错了</button></div></div></div>';
}
function bar2(done, total) { return '<span class="pbar"><i style="width:' + (total ? Math.round(done / total * 100) : 0) + '%"></i></span>'; }
function lno(l) { return l.kind === 'kp' ? l.no : ''; }
function pageCourse(isBack) {
  setBar({ title: '<b>课程</b>' });
  var nx = nextLesson(), due = dueQuizzes().length, html = '';
  if (nx) html += '<button class="cta" data-go="l/' + nx.l.no + '"><small>' + (Object.keys(CDONE).length ? '接着学' : '从这里开始') + '</small>' + (lno(nx.l) ? lno(nx.l) + '　' : '第 ' + nx.c.n + ' 章　') + esc(nx.l.t) + '</button>';
  else html += '<p class="note" style="padding-top:16px">这几章都学完了。</p>';
  if (due) html += '<button class="cta alt" data-go="rv"><small>开新课之前</small>先复习 ' + Math.min(5, due) + ' 道题</button>';
  var X = D.exams, fdone = X.final.filter(function (p) { return (EX[p.id] || {}).done; }).length;
  html += '<button class="row" data-go="fx"><span class="t"><span class="no">结业</span>密封卷 ' + X.ft + ' 套</span><span class="m">' + (LESSONS.every(function (L) { return CDONE[L.l.no]; }) ? '做完 ' + fdone + ' 套' : '学完第 91 章才开启') + '</span></button>';
  var lastP = -1;
  D.course.forEach(function (c) {
    var d = c.lessons.filter(function (l) { return CDONE[l.no]; }).length, pi = -1;
    D.parts.forEach(function (p, k) { if (+c.n >= p[2] && +c.n <= p[3]) pi = k; });
    if (pi !== lastP && pi >= 0) { lastP = pi; html += '<div class="sh" style="position:static">第' + D.parts[pi][0] + '篇　' + esc(D.parts[pi][1]) + '</div>'; }
    html += '<button class="row" data-go="cc/' + c.n + '"><span class="t"><span class="no">第 ' + c.n + ' 章</span>' + esc(c.t) + '</span><span class="m">' + d + ' / ' + c.lessons.length + ' 课' + bar2(d, c.lessons.length) + '</span></button>';
  });
  html += '<p class="note" style="margin-top:16px">全书 ' + D.course.length + ' 章，共 ' + LESSONS.length + ' 课，正文是完整的白话版教材。课里的“想一想”先自己答再看答案，然后选答对还是答错；学过的题隔几天会再考，连续答对 3 次就不再出。紫色的“补充”不是教材原文，是给第一次学的人加的提示，目前只有第 2 到 5 章有。每章学完会开启这一章的阶段密封卷。</p>';
  show('<div class="rows0">' + html + '</div>', isBack);
}
function pageCourseCh(isBack, n) {
  var c = null; D.course.forEach(function (x) { if (x.n === n) c = x; }); if (!c) return nav('course', true);
  setBar({ back: true, title: '第 ' + c.n + ' 章' });
  var html = '<h1 class="ct">' + esc(c.t) + '</h1><p class="note" style="margin-top:6px">' + esc(c.intro) + '</p>' + (c.gl && c.gl.length ? '<div class="item gl"><div class="glb" hidden>' + c.gl.map(function (g) { return '<p><b>' + esc(g[0]) + '</b>　' + esc(g[1]) + '</p>'; }).join('') + '</div><button class="tog" data-act="tog" data-t="这一章会碰到的词（' + c.gl.length + ' 个，每个一句话）">这一章会碰到的词（' + c.gl.length + ' 个，每个一句话）</button></div>' : '') + '<div class="sh" style="position:static">这一章的地图</div><div class="tree">';
  c.lessons.forEach(function (l) {
    html += '<button class="lz' + (CDONE[l.no] ? ' done' : '') + '" data-go="l/' + l.no + '">' + (lno(l) ? '<span class="no">' + l.no + '</span>' : '') + '<span class="lt">' + esc(l.t) + '</span><span class="ck">' + (CDONE[l.no] ? '学过了' : l.p.length + ' 个点') + '</span></button>' +
      (l.kind === 'kp' || l.kind === 'lab' ? '<ul>' + l.p.map(function (p) { return '<li><b>' + esc(p[0] === l.t ? '概览' : p[0]) + '</b>' + (p[1] ? '：' + esc(p[1].length > 46 ? p[1].slice(0, 46) + '…' : p[1]) : '') + '</li>'; }).join('') + '</ul>' : '<ul></ul>');
  });
  html += '</div>';
  var sp = D.exams.stage[c.n] || [];
  if (sp.length) html += '<div class="sh" style="position:static">阶段密封卷</div><p class="note">学完这一章才开启，做完一套才开下一套。每套只能做一次。</p>' + sp.map(function (p) { return xRow(p.id); }).join('');
  show(html, isBack);
}
function blocksHtml(bl, st) {
  return bl.map(function (b) {
    var v = b[1];
    if (b[0] === 'k') return '<div class="gist">' + fix(v) + '</div>';
    if (b[0] === 't') return '<div class="body full">' + fix(v) + '</div>';
    if (b[0] === 'a') return '<div class="bridge full"><span class="src2">打个比方</span>' + fix(v) + '</div>';
    if (b[0] === 's') return '<div class="full"><div class="item" hidden>' + fix(v) + '</div><button class="tog" data-act="tog" data-t="这段可以先跳过，想看再展开">这段可以先跳过，想看再展开</button></div>';
    if (b[0] === 'w') return '<div class="item pit must"><span class="src">注意</span>' + fix(v) + '</div>';
    if (b[0] === 'm') return '<div class="item pit must mm"><span class="src">必须掌握</span>' + fix(v) + '</div>';
    if (b[0] === 'n') return '<div class="item sup"><span class="src">补充</span>' + fix(v) + '</div>';
    if (b[0] === 'r') return rowPit(v);
    if (b[0] === 'q') { var q = { k: st.no + '#' + st.n, q: v, no: st.no }; st.n++; return cquiz(q, false); }
    return '';
  }).join('');
}
function pageLesson(isBack, no) {
  var L = LESSONS[LIDX[no]]; if (!L) return nav('course', true);
  setBar({ back: true, title: '第 ' + L.c.n + ' 章　' + esc(L.c.t), aa: true });
  var want = top();
  loadK(L.c.n, function (kd) {
    if (top() !== want) return;
    var d = kd ? kd.lessons[L.k] : null;
    if (!d) { show('<div class="empty"><b>这一课没有打开</b>返回再点一次。</div>', false); return; }
    var st = { no: no, n: 0 }, html = '<article class="entry lesson' + (TERSE ? ' terse' : '') + '"><div class="crumb">' + esc(lno(L.l) || '第 ' + L.c.n + ' 章') + '</div><h1 class="hw">' + esc(d.t) + '</h1>' + (d.intro ? '<p class="why">' + esc(d.intro) + '</p>' : '');
    html += '<div class="seg" style="margin-top:14px;width:max-content"><button data-act="terse" data-v="0" class="' + (TERSE ? '' : 'on') + '">完整讲解</button><button data-act="terse" data-v="1" class="' + (TERSE ? 'on' : '') + '">只看要点</button></div>';
    if (d.pts.length > 1) html += '<div class="toc2"><div class="cap" style="margin:0 0 4px">这一课有 ' + d.pts.length + ' 部分</div>' + d.pts.map(function (p, k) { return '<button data-act="jump" data-to="pt-' + k + '">' + (k + 1) + '. ' + esc(p.t) + '</button>'; }).join('') + '</div>';
    if (d.lead.length) html += '<div style="margin-top:18px">' + blocksHtml(d.lead, st) + '</div>';
    d.pts.forEach(function (p, k) {
      html += '<section class="pt" id="pt-' + k + '">' + (d.pts.length > 1 || p.t !== d.t ? '<div class="pth"><span class="pk">' + (k + 1) + ' / ' + d.pts.length + (p.no ? '　' + p.no : '') + '</span><h2>' + esc(p.t) + '</h2></div>' : '<div style="height:14px"></div>') + blocksHtml(p.b, st);
      (MINE_BY[p.d] || []).forEach(function (i) { html += mineHtml(D.mine[i], false); });
      if (p.d >= 0) html += '<div class="from"><button class="lnk" data-go="e/' + p.d + '">在字典里看这一条的速查</button></div>';
      html += '</section>';
    });
    html += '<div id="fin">' + finHtml(L) + '</div></article>';
    show(html, isBack);
  });
}
function finHtml(L) {
  var i = LIDX[L.l.no], nx = LESSONS[i + 1], due = dueQuizzes().length;
  function nm(x) { return (lno(x.l) ? lno(x.l) + '　' : (x.c !== L.c ? '第 ' + x.c.n + ' 章　' : '')) + esc(x.l.t); }
  if (!CDONE[L.l.no]) return '<button class="cta" data-act="done" data-no="' + L.l.no + '"><small>都看懂了，“想一想”也答过了</small>学完这一课</button>';
  return '<p class="note" style="margin-top:24px">这一课已经学过了。</p>' + (due ? '<button class="cta alt" data-go="rv"><small>开新课之前</small>先复习 ' + Math.min(5, due) + ' 道题</button>' : '') + (nx ? '<button class="cta" data-go="l/' + nx.l.no + '" data-rep="1"><small>下一课</small>' + nm(nx) + '</button>' : '<button class="cta" data-go="course" data-rep="1"><small>这几章都学完了</small>回到课程</button>');
}
function pageReview(isBack) {
  setBar({ back: true, title: '<b>复习</b>', aa: true });
  var ks = shuffle(dueQuizzes()).slice(0, 5), want = top(), need = {}, left = 0;
  ks.forEach(function (k) { if (!KC[k.L.c.n] && !need[k.L.c.n]) { need[k.L.c.n] = 1; left++; } });
  if (left) { show('<div class="empty">正在准备题目……</div>', false); Object.keys(need).forEach(function (n) { loadK(n, function () { if (--left === 0 && top() === want) pageReview(isBack); }); }); return; }
  var qs = ks.map(function (k) { return lessonQuizzes(k.L)[k.i]; }).filter(Boolean);
  if (!qs.length) { show('<div class="empty"><b>现在没有到期要复习的题</b>学完新课、或者过几天再来。</div><button class="cta" data-go="course" data-rep="1">回到课程</button>', false); return; }
  show('<article class="entry lesson"><p class="why" style="margin-top:16px">' + qs.length + ' 道题，新旧章节混着出。先自己答，再点开看答案，然后老实选“答对了”还是“答错了”。答错的过几天会再出。</p>' + qs.map(function (q) { return cquiz(q, true); }).join('') + '<button class="cta" data-go="course" data-rep="1">答完了，回到课程</button></article>', isBack);
}

/* ---------- 密封卷：平时不出现；学完才开；做完一套才开下一套；每套只做一次 ---------- */
var EX = store.get('ex', {}), XD = window.__XPRE || null, xwait = null;
window.DICT_EXAM = function (d) { XD = d; var w = xwait || []; xwait = null; w.forEach(function (f) { f(); }); };
function loadX(cb) {
  if (XD) return cb(); if (xwait) return xwait.push(cb); xwait = [cb];
  var s = document.createElement('script'); s.src = 'data/x.js'; s.onerror = function () { var w = xwait || []; xwait = null; w.forEach(function (f) { f(); }); }; document.head.appendChild(s);
}
function xPaper(id) { try { return JSON.parse(decodeURIComponent(escape(atob(XD[id])))); } catch (e) { return null; } }
function xInfo(id) {
  var X = D.exams, r = null, AB = 'ABCDEF';
  Object.keys(X.stage).forEach(function (ch) { X.stage[ch].forEach(function (p, j) {
    if (p.id !== id) return; var c = courseCh(ch), left = c ? c.lessons.filter(function (l) { return !CDONE[l.no]; }).length : 1;
    r = { p: p, name: '第 ' + ch + ' 章　阶段卷 ' + AB[j], open: true, why: '', next: X.stage[ch][j + 1], home: 'cc/' + ch };
    if (left) { r.open = false; r.why = '学完第 ' + ch + ' 章才开启，还差 ' + left + ' 课'; }
    else if (j && !(EX[X.stage[ch][j - 1].id] || {}).done) { r.open = false; r.why = '做完阶段卷 ' + AB[j - 1] + ' 才开启'; }
  }); });
  X.final.forEach(function (p, j) {
    if (p.id !== id) return; var left = LESSONS.filter(function (L) { return !CDONE[L.l.no]; }).length;
    r = { p: p, name: '结业卷　第 ' + (j + 1) + ' 套', open: true, why: '', next: X.final[j + 1], home: 'fx' };
    if (left) { r.open = false; r.why = '学完第 91 章才开启，还差 ' + left + ' 课'; }
    else if (j && !(EX[X.final[j - 1].id] || {}).done) { r.open = false; r.why = '做完第 ' + j + ' 套才开启'; }
  });
  if (r && EX[id]) r.open = true;
  return r;
}
function xRow(id) {
  var f = xInfo(id), st = EX[id]; if (!f) return '';
  if (!f.open) return '<div class="row xlock"><span class="t">' + esc(f.name) + '</span><span class="m">未开启　' + esc(f.why) + '</span></div>';
  return '<button class="row" data-go="x/' + id + '"><span class="t">' + esc(f.name) + '</span><span class="m">' + (st && st.done ? '已交卷　' + st.s + ' / ' + st.n : st ? '做到一半，接着做' : '已开启，还没拆封') + '</span></button>';
}
function pageFinal(isBack) {
  setBar({ back: true, title: '<b>结业密封卷</b>' });
  var X = D.exams, html = '<p class="note" style="padding-top:16px">一共 ' + X.ft + ' 套，是全书的收尾测试。第 91 章学完才开启第 1 套，做完一套才开下一套，每套只能做一次。卷子里的题，课文、复习题和字典里都不会出现。</p>';
  if (X.final.length < X.ft) html += '<p class="note">现在已经出好 ' + X.final.length + ' 套' + (X.final.length ? '' : '：题目还没有写进这一版') + '。</p>';
  for (var j = 0; j < X.ft; j++) html += X.final[j] ? xRow(X.final[j].id) : '<div class="row xlock"><span class="t">结业卷　第 ' + (j + 1) + ' 套</span><span class="m">未开启</span></div>';
  show('<div class="rows0">' + html + '</div>', isBack);
}
function xSubHtml(st, n) {
  var un = 0; for (var i = 0; i < n; i++) if (st.a[i] == null) un++;
  return '<button class="cta" data-act="xsub"><small>' + (un ? '还有 ' + un + ' 题没答' : n + ' 题都答了') + '</small>交卷</button>';
}
function pageExam(isBack, id) {
  var f = xInfo(id); if (!f) return nav('course', true);
  setBar({ back: true, title: esc(f.name), aa: true });
  var want = top();
  loadX(function () {
    if (top() !== want) return;
    var qs = XD ? xPaper(id) : null, st = EX[id];
    if (!qs) { show('<div class="empty"><b>卷子没有打开</b>返回再点一次。</div>', false); return; }
    if (!st && !f.open) { show('<div class="empty"><b>这套卷还没开启</b>' + esc(f.why) + '。</div>', false); return; }
    var html = '<article class="entry lesson exam" data-x="' + id + '">';
    if (!st) {
      html += '<h1 class="hw">' + esc(f.name) + '</h1><p class="why">' + qs.length + ' 道选择题。这是一套没见过的题，用来检查你是不是真的学会了。</p>' +
        '<ul class="xrule"><li>拆封以后，这套卷就算用掉了，只能做这一次。</li><li>不翻课文，不查字典，不问别人。</li><li>中途可以退出，答过的会留着，回来接着做。</li><li>交卷以后才能看到对错和讲解。</li></ul>' +
        '<button class="cta" data-act="xopen"><small>准备好了再点</small>拆封，开始做</button>';
    } else if (!st.done) {
      html += '<p class="why" style="margin-top:16px">' + qs.length + ' 道题，每题选一个。交卷前都可以改。</p>' + qs.map(function (q, i) {
        return '<div class="xq" id="xq-' + i + '"><div class="xn">第 ' + (i + 1) + ' 题</div><div class="qq">' + fix(q.q) + '</div>' + q.o.map(function (o, k) { return '<button class="xo' + (st.a[i] === k ? ' on' : '') + '" data-act="xpick" data-q="' + i + '" data-o="' + k + '"><b>' + 'ABCD'[k] + '</b><span>' + o + '</span></button>'; }).join('') + '</div>';
      }).join('') + '<div id="xsub">' + xSubHtml(st, qs.length) + '</div>';
    } else {
      var wrong = {};
      html += '<h1 class="hw">' + esc(f.name) + '</h1><div class="xscore"><b>' + st.s + '</b> / ' + st.n + '<span>答对的题数</span></div>' + qs.map(function (q, i) {
        var me = st.a[i], ok = me === q.c; if (!ok && !q.x) wrong[q.r] = 1;
        return '<div class="xq ' + (q.x ? 'void' : ok ? 'ok' : 'no') + '"><div class="xn">第 ' + (i + 1) + ' 题　' + (q.x ? '作废，不计分：' + esc(q.x) : ok ? '答对了' : '答错了') + '</div><div class="qq">' + fix(q.q) + '</div>' +
          '<div class="xcmp"><div class="' + (ok ? 'r' : 'w') + '"><small>你选的</small>' + (me == null ? '没有作答' : 'ABCD'[me] + '　' + q.o[me]) + '</div>' + (ok ? '' : '<div class="r"><small>正确答案</small>' + 'ABCD'[q.c] + '　' + q.o[q.c] + '</div>') + '</div>' +
          '<div class="xw">' + q.w + '</div><div class="from"><button class="lnk" data-go="l/' + q.r + '">出自 ' + q.r + '，回去看这一课</button></div></div>';
      }).join('');
      var wl = Object.keys(wrong);
      html += (wl.length ? '<p class="note" style="margin-top:20px">答错的题出自：' + wl.join('、') + '。同一课里错了不止一次的，说明这一课还没掌握，建议整课重学，不只是看这道题的讲解。</p>' : '<p class="note" style="margin-top:20px">全对。</p>') +
        (f.next ? '<button class="cta" data-go="x/' + f.next.id + '" data-rep="1"><small>下一套已经开启</small>去看下一套</button>' : '') + '<button class="cta alt" data-go="' + f.home + '" data-rep="1">回去</button>';
    }
    show(html + '</article>', isBack);
  });
}

/* ---------- 术语表 ---------- */
function pageTerms(isBack) {
  setBar({ back: stacks[curTab].length > 1, title: '<b>术语表</b>' });
  show('<div class="search"><label class="sbox">' + ICON.search + '<input id="tq" type="search" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="输入英文或中文" value="' + esc(S.tq) + '"><button class="clr" data-act="tclr" aria-label="清空"' + (S.tq ? '' : ' hidden') + '>' + ICON.x + '</button></label>' +
    '<div class="chipbar" style="position:static;border:0;padding-bottom:0">' + D.tg.map(function (g, i) { return '<button data-act="jump" data-to="tg-' + i + '">' + esc(g.replace(/^A\.\d+\s*/, '')) + '</button>'; }).join('') + '</div></div><div id="tl"></div>', false);
  drawTerms(); if (isBack) view.scrollTop = top().s;
  var q = document.getElementById('tq');
  q.addEventListener('input', function () { S.tq = q.value; view.querySelector('.clr').hidden = !S.tq; clearTimeout(pageTerms.t); pageTerms.t = setTimeout(function () { drawTerms(); view.scrollTop = 0; }, 60); });
  q.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') q.blur(); });
}
function drawTerms() {
  var el = document.getElementById('tl'); if (!el) return;
  var ts = norm(S.tq).split(/\s+/).filter(Boolean), html = '', lastG = -1, n = 0;
  D.terms.forEach(function (t, i) {
    if (ts.length) { var L = norm(t[0] + ' ' + t[1]); for (var j = 0; j < ts.length; j++) if (L.indexOf(ts[j]) < 0) return; }
    if (t[4] !== lastG) { html += (lastG >= 0 ? '</div>' : '') + '<div class="sh" style="position:static" id="tg-' + t[4] + '">' + esc(D.tg[t[4]].replace(/^A\.\d+\s*/, '')) + '</div><div class="rows">'; lastG = t[4]; }
    html += termRow(i, ts, false); n++;
  });
  el.innerHTML = n ? html + '</div>' : '<div class="empty"><b>术语表里没有“' + esc(S.tq) + '”</b><button class="btn" data-act="find" data-q="' + esc(S.tq) + '">去知识点里查</button></div>';
}

/* ---------- 我的 ---------- */
function isFav(id) { return S.fav.indexOf(keyOf(id)) >= 0; }
function pageMe(isBack) {
  setBar({ title: '<b>我的</b>' });
  var favs = S.fav.map(function (k) { return keyToId[k]; }).filter(function (x) { return x != null; }), st = D.stat;
  var html = '<div class="sh">收藏</div>' + (favs.length ? '<div class="rows">' + favs.map(function (i) { return entRow(i, [], 0); }).join('') + '</div>' : '<p class="note">还没有收藏。打开一个知识点，点右上角的星星。</p>');
  html += '<div class="sh">我常错的</div><div class="rows"><button class="row" data-go="mine"><span class="t">我的易错清单</span><span class="d">' + D.mine.length + ' 条，来自学习笔记。查到相关知识点时会排在最前面提醒。</span></button></div>';
  html += '<div class="sh">显示</div><div class="set"><span>字号</span><div class="seg">' + FS.map(function (f, i) { return '<button data-act="fs" data-i="' + i + '" class="' + (S.fs === i ? 'on' : '') + '">' + f[0] + '</button>'; }).join('') + '</div></div>' +
    '<div class="set"><span>外观</span><div class="seg">' + [['auto', '跟随系统'], ['light', '浅色'], ['dark', '深色']].map(function (t) { return '<button data-act="theme" data-t="' + t[0] + '" class="' + (S.theme === t[0] ? 'on' : '') + '">' + t[1] + '</button>'; }).join('') + '</div></div>';
  html += '<div class="sh">内容从哪来</div><p class="note">全部来自《从零到大模型研发》白话版教材，共 ' + st.kp + ' 个知识点、' + D.terms.length + ' 个术语，离线可用。另有 ' + D.cmds.length + ' 个命令的选项速查，是补充内容。只收知识点：面试题、章节小结、小练习、学习方法和求职类内容没有放进来。</p>' +
    '<p class="note">案例：' + st.case_own + ' 条是教材自带的代码或表格，' + st.case_quiz + ' 条用教材的自测题当例子，' + (st.case_sup || 0) + ' 条是按教材要点补写的（标着“补充”），' + st.case_sib + ' 条借的是旁边知识点的例子。</p>' +
    '<p class="note">容易出错的内容：' + (st.pit_own + st.pit_table) + ' 条是教材原文；' + st.pit_sup + ' 条（第 2 到 6 章和附录 C）是按教材补写的，标着“补充”；' + st.pit_sib + ' 条显示的是旁边知识点的易错点，页面上会注明；其余 ' + st.pit_ch + ' 条教材没有单独写，给的是本章常见误区的入口。</p>';
  show(html, isBack);
}
function pageMine(isBack) {
  setBar({ back: true, title: '<b>我的易错清单</b>', aa: true });
  show('<article class="entry"><p class="sub" style="padding-top:14px">' + D.mine.length + ' 条，来自学习笔记。</p>' + D.mine.map(function (m) { return mineHtml(m, true); }).join('') + '</article>', isBack);
}

/* ---------- 事件 ---------- */
function copyText(t) {
  try { if (window.AndroidBridge) { AndroidBridge.copy(t); return true; } } catch (e) {}
  try { var ta = document.createElement('textarea'); ta.value = t; ta.style.cssText = 'position:fixed;opacity:0'; document.body.appendChild(ta); ta.select(); var ok = document.execCommand('copy'); ta.remove(); if (ok) return true; } catch (e2) {}
  try { navigator.clipboard.writeText(t); return true; } catch (e3) { return false; }
}
document.addEventListener('click', function (ev) {
  var t = ev.target.closest('[data-go],[data-act],[data-tab]'); if (!t) return;
  if (document.activeElement && document.activeElement.blur && !t.closest('.sbox')) document.activeElement.blur();
  if (t.hasAttribute('data-tab')) {
    var name = t.getAttribute('data-tab');
    if (name === curTab && stacks[name].length === 1) { view.scrollTop = 0; if (name === 'home') { var qi = document.getElementById('q'); if (qi) { qi.focus(); qi.select(); } } return; }
    return switchTab(name, false);
  }
  if (t.hasAttribute('data-go')) {
    var tg = t.getAttribute('data-tabgo');
    if (tg && tg !== curTab) { top().s = view.scrollTop; curTab = tg; stacks[tg] = [{ r: tg, s: 0 }]; if (t.getAttribute('data-go').indexOf('toc/') === 0) stacks[tg] = [{ r: t.getAttribute('data-go'), s: 0 }]; else stacks[tg].push({ r: t.getAttribute('data-go'), s: 0 }); return render(false); }
    return nav(t.getAttribute('data-go'), t.hasAttribute('data-rep'));
  }
  var act = t.getAttribute('data-act');
  if (act === 'back') { if (!back()) switchTab('home', true); }
  else if (act === 'aa') { S.fs = (S.fs + 1) % FS.length; store.set('fs', S.fs); applyFs(); toast('字号：' + FS[S.fs][0] + '（再点继续换）'); }
  else if (act === 'fs') { S.fs = +t.getAttribute('data-i'); store.set('fs', S.fs); applyFs(); [].forEach.call(t.parentNode.children, function (b) { b.classList.toggle('on', b === t); }); }
  else if (act === 'theme') { S.theme = t.getAttribute('data-t'); store.set('theme', S.theme); applyTheme(); [].forEach.call(t.parentNode.children, function (b) { b.classList.toggle('on', b === t); }); }
  else if (act === 'star') { var id = +t.getAttribute('data-id'), k = keyOf(id), on = S.fav.indexOf(k) < 0; S.fav = on ? [k].concat(S.fav) : S.fav.filter(function (x) { return x !== k; }); store.set('fav', S.fav); t.classList.toggle('on', on); toast(on ? '已收藏' : '已取消收藏'); }
  else if (act === 'ans') { var a = t.parentNode.querySelector('.aa'); a.hidden = !a.hidden; t.textContent = a.hidden ? '看答案' : '收起答案'; }
  else if (act === 'grade') { var qz = t.closest('.quiz'), k2 = qz.getAttribute('data-k'), okv = t.getAttribute('data-v') === '1', st2 = CQ[k2] || { s: 0, t: 0 }; CQ[k2] = { s: okv ? st2.s + 1 : 0, t: Date.now() }; store.set('cq2', CQ); t.parentNode.innerHTML = '<span class="from">' + (okv ? (CQ[k2].s >= 3 ? '连续答对 3 次，这道题以后不再出了。' : '记下了：答对。过几天再考你一次。') : '记下了：答错。回上面把这个知识点再看一遍，过几天还会再出。') + '</span>'; }
  else if (act === 'terse') { TERSE = t.getAttribute('data-v') === '1'; store.set('terse', TERSE); var art = view.querySelector('.lesson'); if (art) art.classList.toggle('terse', TERSE); [].forEach.call(t.parentNode.children, function (b) { b.classList.toggle('on', b === t); }); }
  else if (act === 'done') { var no2 = t.getAttribute('data-no'); CDONE[no2] = Date.now(); store.set('cdone', CDONE); document.getElementById('fin').innerHTML = finHtml(LESSONS[LIDX[no2]]); toast('已记为学过'); }
  else if (act === 'xopen') { var xid = t.closest('.exam').getAttribute('data-x'), xf = xInfo(xid); if (xf && xf.open && !EX[xid]) { EX[xid] = { t0: Date.now(), a: [], n: xf.p.n }; store.set('ex', EX); } nav('x/' + xid, true); }
  else if (act === 'xpick') { var xid2 = t.closest('.exam').getAttribute('data-x'), xs = EX[xid2]; if (xs && !xs.done) { xs.a[+t.getAttribute('data-q')] = +t.getAttribute('data-o'); store.set('ex', EX); [].forEach.call(t.parentNode.querySelectorAll('.xo'), function (b) { b.classList.toggle('on', b === t); }); document.getElementById('xsub').innerHTML = xSubHtml(xs, xs.n); } }
  else if (act === 'xsub') { var xs2 = EX[t.closest('.exam').getAttribute('data-x')], un2 = 0; for (var xi = 0; xi < xs2.n; xi++) if (xs2.a[xi] == null) un2++; document.getElementById('xsub').innerHTML = '<p class="note" style="margin-top:20px"><b>先别急着交。</b>' + (un2 ? '还有 ' + un2 + ' 题没答。' : '') + '交卷以后不能改，也不能重做。从第 1 题起再检查一遍：题目问的是什么，自己选的是不是想选的那个。</p><button class="cta alt" data-act="xchk">回去检查</button><button class="cta" data-act="xsub2">检查过了，交卷</button>'; }
  else if (act === 'xchk') { var xs3 = EX[t.closest('.exam').getAttribute('data-x')], first = 0; for (var xj = 0; xj < xs3.n; xj++) if (xs3.a[xj] == null) { first = xj; break; } document.getElementById('xsub').innerHTML = xSubHtml(xs3, xs3.n); jumpTo('xq-' + first); }
  else if (act === 'xsub2') { var xid3 = t.closest('.exam').getAttribute('data-x'), xs4 = EX[xid3], xq = xPaper(xid3), sc = 0, tot = 0; if (xq && xs4 && !xs4.done) { xq.forEach(function (q, i) { if (q.x) return; tot++; if (xs4.a[i] === q.c) sc++; }); xs4.done = Date.now(); xs4.s = sc; xs4.n = tot; store.set('ex', EX); } nav('x/' + xid3, true); }
  else if (act === 'tog') { var box = t.previousElementSibling; box.hidden = !box.hidden; t.textContent = box.hidden ? t.getAttribute('data-t') : '收起'; }
  else if (act === 'copy') { var lines = t.parentNode.querySelector('pre').textContent.split('\n'), sh = t.parentNode.classList.contains('sh'); if (sh) lines = lines.filter(function (l) { return !/^\s*#/.test(l); }).map(function (l) { return l.replace(/^\$ /, ''); }); toast(copyText(lines.join('\n')) ? '已复制' : '复制不了，长按代码手动选'); }
  else if (act === 'clr') { S.q = ''; var q = document.getElementById('q'); q.value = ''; updateResults(); q.focus(); }
  else if (act === 'tclr') { S.tq = ''; var tq = document.getElementById('tq'); tq.value = ''; t.hidden = true; drawTerms(); tq.focus(); }
  else if (act === 'more') { S.showN += 50; updateResults(); }
  else if (act === 'find') { S.q = t.getAttribute('data-q'); S.showN = 30; switchTab('home', true); }
  else if (act === 'jump') jumpTo(t.getAttribute('data-to'));
});
view.addEventListener('touchmove', function () { var a = document.activeElement; if (a && a.tagName === 'INPUT') a.blur(); }, { passive: true });
var baseH = window.innerHeight;
function onResize() { var hh = window.visualViewport ? visualViewport.height : window.innerHeight; if (hh > baseH) baseH = hh; document.body.classList.toggle('kb', hh < baseH - 140); }
if (window.visualViewport) visualViewport.addEventListener('resize', onResize); else window.addEventListener('resize', onResize);
/* 给安卓返回键用：返回 true 表示自己处理了 */
window.__back = function () { if (back()) return true; if (curTab !== 'home') { switchTab('home', false); return true; } if (S.q) { S.q = ''; render(false); return true; } return false; };
window.__course = function () { return { done: CDONE, q: CQ, due: dueQuizzes().length, ex: EX }; };
window.__nav = nav; window.__top = function () { return top().r; }; window.__tab = function () { return curTab; }; window.__ready = function () { return warmed; };
render(false);
})();
