/* The guide's own Mint: a small orb in the corner that behaves like the real one.
   It follows the pointer with its eyes, announces sections, and acts out examples:
   expressions, tricks (the same paths as mint/ui/motion.py), moving on command, and
   marking words on the page (box, underline, highlight, circle, arrow). */
(() => {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];

  // ---------- the corner orb ----------
  const buddy = $('.buddy'), body = $('.buddy .body'), orb = $('.buddy .orb'), bubble = $('#bubble');
  const orbs = $$('.orb');
  let hideT;
  function say(text, who = 'Mint', ms = 4200) {
    if (!bubble) return;
    bubble.innerHTML = `<b>${who}</b>` + String(text).replace(/</g, '&lt;');
    bubble.classList.add('show');
    clearTimeout(hideT); hideT = setTimeout(() => bubble.classList.remove('show'), ms);
  }

  // Eyes follow the pointer (unless the orb is flying and looks where it goes).
  let flyingLook = null;
  function lookAt(o, dx, dy) {
    const r = o.getBoundingClientRect(), d = Math.hypot(dx, dy) || 1, k = Math.min(1, d / 400) * r.width * 0.045;
    for (const eye of o.querySelectorAll('.eye')) eye.style.transform = `translate(${dx / d * k}px,${dy / d * k}px)`;
  }
  addEventListener('pointermove', e => {
    for (const o of orbs) {
      if (o === orb && flyingLook) continue;
      const r = o.getBoundingClientRect();
      lookAt(o, e.clientX - (r.left + r.width / 2), e.clientY - (r.top + r.height / 2));
    }
  });
  if (!reduce) setInterval(() => {
    const o = orbs[Math.floor(Math.random() * orbs.length)];
    if (!o || [...o.classList].some(c => c.startsWith('e-'))) return;
    o.classList.add('blink'); setTimeout(() => o.classList.remove('blink'), 150);
  }, 2600);

  // ---------- expressions ----------
  const EMOTES = {smile: 2600, laugh: 2600, love: 3000, blush: 2400, cry: 3600, angry: 2400, surprised: 2000,
    sleepy: 3400, dizzy: 3000, cool: 3200, wave: 2600, clap: 2600, praise: 2800, dance: 4000};
  function particles(o, chars, n = 5, colors = ['#FF4F7B']) {
    for (let i = 0; i < n; i++) {
      const p = document.createElement('span');
      p.className = 'particle'; p.textContent = chars[i % chars.length];
      p.style.color = colors[i % colors.length];
      p.style.left = (30 + Math.random() * 40) + '%'; p.style.top = (Math.random() * 20 - 10) + '%';
      p.style.setProperty('--dx', (Math.random() * 60 - 30) + 'px');
      p.style.animationDelay = (i * 0.16) + 's';
      p.style.fontSize = Math.max(12, o.offsetWidth * 0.3) + 'px';
      o.appendChild(p); setTimeout(() => p.remove(), 2200 + i * 160);
    }
  }
  function tears(o) {
    for (const [i, side] of [[0, .36], [1, .6]]) for (let k = 0; k < 3; k++) {
      const t = document.createElement('span'); t.className = 'tear';
      t.style.left = (side * 100) + '%'; t.style.top = '50%'; t.style.animationDelay = (k * 0.9 + i * 0.3) + 's';
      o.appendChild(t); setTimeout(() => t.remove(), 3600);
    }
  }
  function emote(name, o = orb) {
    if (!(name in EMOTES)) return;
    for (const c of [...o.classList]) if (c.startsWith('e-')) o.classList.remove(c);
    void o.offsetWidth; o.classList.add('e-' + name);
    ({love: () => particles(o, ['♥'], 6, ['#FF4F7B', '#FF8FB8']), sleepy: () => particles(o, ['z', 'Z', 'z'], 4, ['#6D7BA8']),
      dance: () => particles(o, ['♪', '♫'], 6, ['#8B7CFF', '#2EC4B6', '#FF6FB5']), clap: () => particles(o, ['✦', '✧'], 6, ['#FFC83D', '#FF8FB8']),
      praise: () => particles(o, ['★', '👍'], 4, ['#FFC83D']), dizzy: () => particles(o, ['★', '✦'], 5, ['#FFC83D']),
      cry: () => tears(o), laugh: () => particles(o, ['ha', 'ha'], 3, ['#0E8F84']), angry: () => particles(o, ['💢'], 2)
    })[name]?.();
    clearTimeout(o._emoteT);
    o._emoteT = setTimeout(() => o.classList.remove('e-' + name), EMOTES[name]);
  }

  // ---------- flight (the same shapes as mint/ui/motion.py, in screen pixels, y down) ----------
  const E = {
    linear: t => t, inOut: t => t < .5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2,
    outQuad: t => 1 - (1 - t) ** 2, inQuad: t => t * t,
    outBack: t => 1 + 2.5 * (t - 1) ** 3 + 1.5 * (t - 1) ** 2,
    wave: (k = 2, a = .5) => t => { const e = E.inOut(t); return e + a * Math.sin(2 * Math.PI * k * e) / (2 * Math.PI * k); },
  };
  const lerp = (a, b, u) => [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u];
  const P = {
    line: (a, b) => u => lerp(a, b, u),
    quad: (a, c, b) => u => { const v = 1 - u; return [v * v * a[0] + 2 * v * u * c[0] + u * u * b[0], v * v * a[1] + 2 * v * u * c[1] + u * u * b[1]]; },
    cubic: (a, c1, c2, b) => u => { const v = 1 - u; return [0, 1].map(i => v ** 3 * a[i] + 3 * v * v * u * c1[i] + 3 * v * u * u * c2[i] + u ** 3 * b[i]); },
    hop: (a, b, h) => u => { const p = lerp(a, b, u); return [p[0], p[1] - 4 * h * u * (1 - u)]; },
    loop: (c, r, a0, turns) => u => { const a = a0 + 2 * Math.PI * turns * u; return [c[0] + r * Math.cos(a), c[1] + r * Math.sin(a)]; },
    fig8: (c, rx, ry, side) => u => { const a = 2 * Math.PI * u; return [c[0] + side * rx * Math.sin(a), c[1] + ry * Math.sin(a) * Math.cos(a)]; },
    wavy: (a, b, amp, waves) => { const dx = b[0] - a[0], dy = b[1] - a[1], L = Math.hypot(dx, dy) || 1, nx = -dy / L, ny = dx / L;
      return u => { const p = lerp(a, b, u), o = amp * Math.sin(2 * Math.PI * waves * u) * Math.sin(Math.PI * u); return [p[0] + nx * o, p[1] + ny * o]; }; },
    hold: p => () => p,
  };
  const Leg = (path, secs, ease = E.inOut, o = {}) => ({path, secs, ease, ...o});
  let pos = [0, 0], home = [0, 0], squash = 0, squashV = 0, flight = null, token = 0;

  function swoop(a, b, secs, land = .2) {
    const d = Math.hypot(b[0] - a[0], b[1] - a[1]), mid = lerp(a, b, .5), bulge = Math.min(40, 12 + d * .12);
    const ctrl = [mid[0], mid[1] - bulge];
    return Leg(P.quad(a, ctrl, b), secs || Math.min(1.1, .45 + d / 900), E.outBack, {land});
  }
  function fly(legs, done) {
    token++; const my = token;
    flight = {legs, i: 0, t0: performance.now(), done, last: pos.slice(), spin0: 0, my};
    if (reduce) { pos = legs[legs.length - 1].path(1); apply(0, 0, 0); flight = null; done?.(); return; }
    requestAnimationFrame(frame);
  }
  function apply(vx, vy, spin) {
    buddy.style.transform = `translate(${pos[0]}px,${pos[1]}px)`;
    const sp = Math.hypot(vx, vy), k = Math.min(.2, sp / 3000), lean = Math.max(-.28, Math.min(.28, vx / 1800));
    const th = Math.atan2(vy, vx), s = Math.max(-.35, Math.min(.35, squash));
    body.style.transform = `rotate(${lean + spin}rad) rotate(${th}rad) scale(${1 + k},${1 - .6 * k}) rotate(${-th}rad) scale(${1 + s},${1 - s})`;
  }
  function frame(now) {
    squashV += -.2 * squash - .16 * squashV; squash += squashV;
    const f = flight;
    if (!f) { apply(0, 0, 0); if (Math.abs(squash) > .004 || Math.abs(squashV) > .004) requestAnimationFrame(frame); return; }
    if (f.my !== token) return;
    const leg = f.legs[f.i], t = Math.min(1, (now - f.t0) / (leg.secs * 1000)), u = leg.ease(t);
    pos = leg.path(u);
    const vx = (pos[0] - f.last[0]) * 60, vy = (pos[1] - f.last[1]) * 60; f.last = pos.slice();
    const spin = f.spin0 + 2 * Math.PI * (leg.spin || 0) * u;
    apply(leg.stretch === false ? 0 : vx, leg.stretch === false ? 0 : vy, spin);
    if (leg.gaze) { flyingLook = true; const r = orb.getBoundingClientRect(); lookAt(orb, leg.gaze[0] - r.left, leg.gaze[1] - r.top); }
    else if (Math.hypot(vx, vy) > 60) { flyingLook = true; lookAt(orb, vx, vy); } else flyingLook = null;
    if (t >= 1) {
      f.spin0 = spin % (2 * Math.PI);
      if (leg.land) { squash = leg.land; squashV = 0; }
      leg.end?.();
      f.i++; f.t0 = now;
      if (f.i >= f.legs.length) { flight = null; flyingLook = null; f.done?.(); }
    }
    requestAnimationFrame(frame);
  }

  // Tricks start and end at `home`; "inward" is up and to the left (the orb sits bottom right).
  function trickLegs(name, s) {
    const ix = -1, up = -1, [sx, sy] = s, R = (a, b) => a + Math.random() * (b - a);
    switch (name) {
      case 'hops': {
        const legs = []; let at = s;
        for (let i = 0; i < 3; i++) { const n = [at[0] + ix * 44, at[1]]; legs.push(Leg(P.hop(at, n, 26), .36, E.linear, {land: .24})); at = n; }
        legs.push(Leg(P.hold(at), .5, E.linear, {end: () => emote(['smile', 'wave'][Math.random() * 2 | 0])}));
        legs.push(Leg(P.hop(at, s, 64), .85, E.inOut, {land: .3}));
        return legs;
      }
      case 'loop': {
        const top = [sx + ix * 70, sy + up * 110], r = 42, c = [top[0], top[1] + r];
        return [Leg(P.quad(s, [sx + ix * 10, sy + up * 110], top), .6), Leg(P.loop(c, r, -Math.PI / 2, ix), 1.2),
          Leg(P.quad(top, [sx + ix * 90, sy + up * 20], s), .7, E.inOut, {land: .3, end: () => emote(Math.random() < .5 ? 'dizzy' : 'laugh')})];
      }
      case 'figure8': {
        const c = [sx + ix * 90, sy + up * 90];
        return [Leg(P.quad(s, [sx, sy + up * 90], c), .55), Leg(P.fig8(c, 75, 48, ix), 2.4, E.wave(2, .5)),
          Leg(P.quad(c, [c[0], sy], s), .6, E.inOut, {land: .28, end: () => emote('smile')})];
      }
      case 'bounce': {
        const legs = [];
        for (const [h, a, b, land] of [[140, .46, .42, .34], [55, .28, .26, .24], [18, .15, .14, .14]]) {
          const pk = [sx, sy + up * h]; legs.push(Leg(P.line(s, pk), a, E.outQuad), Leg(P.line(pk, s), b, E.inQuad, {land}));
        }
        legs[legs.length - 1].end = () => emote('laugh');
        return legs;
      }
      case 'zigzag': case 'bee': {
        const t = [sx + ix * R(200, 280), sy + up * R(40, 130)];
        return [Leg(P.wavy(s, t, 20, 3), 2.0), Leg(P.hold(t), .9, E.linear, {end: () => emote('surprised')}), Leg(P.wavy(t, s, 16, 2.5), 1.8, E.inOut, {land: .24})];
      }
      case 'chase': {
        const t = [sx + ix * R(160, 240), sy + up * R(60, 150)];
        let star;
        return [Leg(P.hold(s), .02, E.linear, {end: () => { star = document.createElement('div'); star.className = 'star'; star.textContent = '★';
            const b = buddy.getBoundingClientRect(); star.style.left = (b.left + t[0] - pos[0] + 14) + 'px'; star.style.top = (b.top + t[1] - pos[1] + 12) + 'px';
            document.body.appendChild(star); }}),
          Leg(P.hold(s), .45, E.linear, {stretch: false}),
          Leg(P.cubic(s, [sx - ix * 30, sy + up * 40], [t[0] - ix * 60, t[1] + up * 60], t), .6, E.inQuad, {land: .26, end: () => { star?.remove(); particles(orb, ['★', '✦'], 6, ['#FFC83D', '#FF8FB8']); emote('laugh'); }}),
          Leg(P.hold(t), .5), Leg(P.hop(t, s, 50), .9, E.inOut, {land: .3})];
      }
      case 'peek': {
        const spot = [sx + ix * 150, sy + up * 70];
        return [Leg(P.quad(s, [sx + ix * 40, sy + up * 110], spot), .45, E.outBack), Leg(P.hold(spot), .5), Leg(P.hold(spot), .5, E.linear, {end: () => emote('surprised')}),
          Leg(P.wavy(spot, s, 8, 2), 1.5, E.inOut, {land: .22})];
      }
      default:  // spin
        return [Leg(P.hop(s, s, 46), .62, E.linear, {spin: 1, land: .3}), Leg(P.hop(s, s, 18), .36, E.linear, {spin: -1, land: .18, end: () => emote('wink' in EMOTES ? 'wink' : 'smile')})];
    }
  }
  const TRICKS = ['hops', 'loop', 'figure8', 'bounce', 'zigzag', 'chase', 'peek', 'spin'];
  function trick(name) {
    if (name === 'all') {
      let legs = [];
      for (const n of TRICKS) legs = legs.concat(trickLegs(n, home), [Leg(P.hold(home), .45)]);
      return fly(legs);
    }
    fly(trickLegs(name || TRICKS[Math.random() * TRICKS.length | 0], home));
  }
  function move(dir) {
    const step = 110, target = {up: [home[0], home[1] - step], down: [home[0], home[1] + step], left: [home[0] - step * 1.6, home[1]],
      right: [home[0] + step, home[1]], back: [0, 0], away: [home[0], home[1] - 260]}[dir] || home;
    target[0] = Math.max(-innerWidth + 120, Math.min(0, target[0])); target[1] = Math.max(-innerHeight + 120, Math.min(0, target[1]));
    const from = pos.slice(); home = target; fly([swoop(from, target)]);
  }

  // ---------- marks on the page ----------
  let marksLayer = [];
  function clearMarks() { for (const m of marksLayer) m.remove(); marksLayer = []; }
  function svgEl(tag, attrs) { const e = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; }
  function mark(target, style = 'box', note = '') {
    const el = typeof target === 'string' ? $(target) : target;
    if (!el) return;
    clearMarks();
    el.scrollIntoView({behavior: reduce ? 'auto' : 'smooth', block: 'center'});
    setTimeout(() => {
      const rs = [...el.getClientRects()];
      const r = rs.reduce((a, b) => ({left: Math.min(a.left, b.left), top: Math.min(a.top, b.top), right: Math.max(a.right, b.right), bottom: Math.max(a.bottom, b.bottom)}));
      const pad = 7, x = r.left + scrollX - pad, y = r.top + scrollY - pad, w = r.right - r.left + 2 * pad, h = r.bottom - r.top + 2 * pad;
      const svg = svgEl('svg', {class: 'mark-layer', width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight});
      let shape;
      if (style === 'box') shape = svgEl('path', {d: `M${x + 10},${y} H${x + w - 10} Q${x + w},${y} ${x + w},${y + 10} V${y + h - 10} Q${x + w},${y + h} ${x + w - 10},${y + h} H${x + 10} Q${x},${y + h} ${x},${y + h - 10} V${y + 10} Q${x},${y} ${x + 10},${y}`});
      else if (style === 'underline') shape = svgEl('path', {d: `M${x + 4},${y + h - 2} q${w / 4},4 ${w / 2},0 t${w / 2 - 8},0`});
      else if (style === 'highlight') { shape = svgEl('rect', {x: x + 2, y: y + 3, width: w - 4, height: h - 6, rx: 5, class: 'hl'}); svg.style.mixBlendMode = 'multiply'; }
      else if (style === 'circle') { const cx = x + w / 2, cy = y + h / 2, rx = w / 2 + 10, ry = h / 2 + 8;
        shape = svgEl('path', {d: `M${cx + rx},${cy} A${rx},${ry} 0 1,1 ${cx - rx},${cy + 2} A${rx + 3},${ry + 2} 0 1,1 ${cx + rx - 6},${cy - 6}`}); }
      else { const tx = x - 4, ty = y + h / 2, fx = tx - 90, fy = ty + 60;
        shape = svgEl('path', {d: `M${fx},${fy} Q${fx + 20},${ty + 4} ${tx},${ty} M${tx - 14},${ty - 8} L${tx},${ty} L${tx - 16},${ty + 7}`}); }
      svg.appendChild(shape); document.body.appendChild(svg); marksLayer.push(svg);
      if (shape.getTotalLength && style !== 'highlight') { shape.style.setProperty('--len', shape.getTotalLength()); shape.classList.add('draw'); }
      if (note) { const n = document.createElement('div'); n.className = 'mark-note'; n.textContent = note;
        n.style.left = (style === 'arrow' ? x - 150 : x) + 'px'; n.style.top = (style === 'arrow' ? y + h + 44 : y - 34) + 'px';
        document.body.appendChild(n); marksLayer.push(n); }
      // The orb swoops over beside the mark, looks at it, and goes home.
      const b = buddy.getBoundingClientRect(), bx = b.left - pos[0], by = b.top - pos[1];
      const box = (el.closest('.doc, figure, table, p') || el).getBoundingClientRect();   // park beside the block, not on its text
      const spot = [Math.min(box.right + 56, innerWidth - 80) - bx, Math.max(r.top - 30, 70) - by];
      const look = [r.left + (r.right - r.left) / 2, r.top + (r.bottom - r.top) / 2];
      const leg = swoop(pos.slice(), spot, .8, .15); leg.gaze = look;
      fly([leg, Leg(P.hold(spot), 3.2, E.linear, {gaze: look})], () => { fly([swoop(pos.slice(), home, .9, .22)]); setTimeout(clearMarks, 600); });
    }, reduce ? 0 : 450);
  }

  // ---------- "▶ try" examples ----------
  function perform(spec) {
    const [kind, a, b, c] = spec.split(':');
    if (kind === 'emote') { emote(a); say(a === 'love' ? 'Hearts for you.' : 'Like this!'); }
    else if (kind === 'trick') { trick(a); say(a === 'all' ? 'All eight, back to back!' : `A ${a}!`); }
    else if (kind === 'move') { move(a); say(a === 'back' ? 'Back where I was.' : `Moved ${a}.`); }
    else if (kind === 'mark') { mark(a, b, c || ''); say('There it is.'); }
    else if (kind === 'clear') { clearMarks(); say('Marks cleared.'); }
    else if (kind === 'say') say(a);
  }
  $$('.say q').forEach(q => {
    q.tabIndex = 0; q.setAttribute('role', 'button');
    q.title = q.dataset.try ? 'Watch the page Mint do it (and copy)' : 'Copy';
    const act = () => {
      navigator.clipboard?.writeText(q.textContent).catch(() => {});
      q.classList.add('copied'); setTimeout(() => q.classList.remove('copied'), 1200);
      if (q.dataset.try) perform(q.dataset.try);
      else say('Copied. Say it to Mint, or press ⌘J and paste.');
    };
    q.addEventListener('click', act);
    q.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); act(); } });
  });
  $$('[data-emote]').forEach(btn => btn.addEventListener('click', () => { emote(btn.dataset.emote); say(`That's “${btn.dataset.emote}”.`); }));

  // Poking the orbs: a giggle, hearts; many pokes make it dizzy.
  for (const o of orbs) {
    let pokes = [];
    o.addEventListener('click', () => {
      const now = Date.now(); pokes = pokes.filter(t => now - t < 1600).concat(now);
      if (pokes.length >= 5) { pokes = []; emote('dizzy', o); say('Whoa… dizzy.'); return; }
      o.classList.remove('poke'); void o.offsetWidth; o.classList.add('poke');
      emote(pokes.length >= 3 ? 'laugh' : 'smile', o); particles(o, ['♥'], Math.min(pokes.length + 1, 4), ['#FF4F7B', '#FF8FB8']);
      if (o === orb) say(pokes.length >= 3 ? 'Hehe, that tickles!' : 'Hi! Try a ▶ example.');
    });
  }

  // ---------- page furniture ----------
  const top = $('.top');
  addEventListener('scroll', () => top?.classList.toggle('scrolled', scrollY > 8), {passive: true});

  // Things rise into view once.
  const rio = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { e.target.classList.add('in'); rio.unobserve(e.target); } }),
    {rootMargin: '0px 0px -8% 0px'});
  $$('.rise').forEach((el, i) => { el.style.transitionDelay = (i % 4) * 70 + 'ms'; rio.observe(el); });

  // The landing player: each phrase is a tab that plays the clip of Mint doing it.
  const player = $('[data-player]');
  if (player) {
    const tabs = $$('.phrases button', player), vids = $$('.screen video', player), tags = $$('.screen .tag', player);
    let at = 0, auto = true, visible = true, raf;
    const vid = name => vids.find(v => v.dataset.v === name);
    function tick() {
      const v = vid(tabs[at].dataset.v);
      if (v && v.duration) tabs[at].style.setProperty('--p', (v.currentTime / v.duration).toFixed(4));
      raf = requestAnimationFrame(tick);
    }
    function show(i, user) {
      if (user) auto = false;
      at = (i + tabs.length) % tabs.length;
      const name = tabs[at].dataset.v;
      tabs.forEach((t, k) => { t.classList.toggle('on', k === at); t.setAttribute('aria-selected', k === at); t.style.setProperty('--p', 0); });
      tags.forEach(t => t.classList.toggle('on', t.dataset.for === name));
      vids.forEach(v => { const on = v.dataset.v === name; v.classList.toggle('on', on); if (!on) v.pause(); });
      const v = vid(name);
      if (v && visible && !reduce) { if (v.currentTime) v.currentTime = 0; v.play().catch(() => {}); }
      const row = tabs[at].closest('.phrases');                       // on phones the phrases scroll sideways
      if (row && row.scrollWidth > row.clientWidth) row.scrollTo({left: tabs[at].parentElement.offsetLeft - row.offsetLeft, behavior: reduce ? 'auto' : 'smooth'});
      if (user) say(`“${tabs[at].lastChild.textContent.trim()}”`, 'You', 2600);
    }
    vids.forEach(v => v.addEventListener('ended', () => { if (auto) show(at + 1); else { v.currentTime = 0; v.play().catch(() => {}); } }));
    tabs.forEach((t, i) => t.addEventListener('click', () => show(i, true)));
    new IntersectionObserver(es => es.forEach(e => {
      visible = e.isIntersecting;
      const v = vid(tabs[at].dataset.v);
      if (!v || reduce) return;
      visible ? v.play().catch(() => {}) : v.pause();
    }), {threshold: .3}).observe(player);
    const resume = () => { const v = vid(tabs[at].dataset.v); if (v && visible && !reduce && !document.hidden && v.paused) v.play().catch(() => {}); };
    document.addEventListener('visibilitychange', resume);
    vids.forEach(v => v.addEventListener('loadeddata', resume));
    show(0);
    if (!reduce) tick();
  }

  // Docs: the side menu follows the section you are reading.
  const links = $$('.side nav a');
  const io = new IntersectionObserver(entries => {
    for (const e of entries) if (e.isIntersecting) links.forEach(a => a.classList.toggle('on', a.getAttribute('href') === '#' + e.target.id));
  }, {rootMargin: '-30% 0px -65% 0px'});
  $$('article section[id]').forEach(s => io.observe(s));

  // Other clips play only while on screen.
  const vio = new IntersectionObserver(entries => {
    for (const e of entries) { const v = e.target; if (e.isIntersecting && !reduce) { v.preload = 'auto'; v.play().catch(() => {}); } else v.pause(); }
  }, {threshold: .25});
  // The launch film: poster + play button; plays muted with native controls, back to the poster at the end.
  const film = $('[data-film]');
  if (film) {
    const fv = $('video', film);
    // Fallback: if this browser can't stream the file (Safari wants byte ranges), play it from YouTube instead.
    const toYouTube = () => {
      if ($('iframe', film)) return;
      fv.pause(); fv.remove();
      const f = document.createElement('iframe');
      f.src = 'https://www.youtube-nocookie.com/embed/' + film.dataset.youtube + '?autoplay=1&mute=1&rel=0&playsinline=1';
      f.title = 'Meet Mint v0.3.0, the launch film'; f.allow = 'autoplay; encrypted-media; picture-in-picture; fullscreen'; f.allowFullscreen = true;
      film.appendChild(f);
    };
    const go = () => {
      film.classList.add('playing'); fv.controls = true; fv.muted = true;   // starts muted; the controls unmute it
      fv.play().catch(e => { if (e.name === 'NotSupportedError') toYouTube(); });   // AbortError = tab hidden, not a failure
      setTimeout(() => { if (fv.isConnected && document.visibilityState === 'visible' && (fv.error || fv.readyState < 3)) toYouTube(); }, 4000);
    };
    fv.addEventListener('error', () => { if (film.classList.contains('playing')) toYouTube(); });
    $('.film-play', film).addEventListener('click', go);
    fv.addEventListener('ended', () => { film.classList.remove('playing'); fv.controls = false; fv.load(); });
  }
  $$('video').filter(v => !v.closest('[data-player]') && !v.closest('[data-film]')).forEach(v => { v.muted = true; v.loop = true; v.playsInline = true; vio.observe(v); });

  // Docs search: sections and every example.
  const input = $('#find'), hits = $('#hits');
  if (input && window.MINT_INDEX) {
    input.addEventListener('input', () => {
      const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
      if (!words.length) { hits.innerHTML = ''; return; }
      const found = MINT_INDEX.filter(x => words.every(w => (x.t + ' ' + (x.w || '')).toLowerCase().includes(w))).slice(0, 8);
      hits.innerHTML = found.length ? found.map(h => h.k === 'section' ? `<a href="#${h.h}"><b>${h.t}</b></a>`
        : `<a href="#${h.h}"><b>“${h.t}”</b> · ${h.w}</a>`).join('')
        : '<div class="none">Not here yet. Just ask Mint: it tries before it says no.</div>';
    });
    input.addEventListener('keydown', e => {
      if (e.key === 'Escape') { input.value = ''; hits.innerHTML = ''; }
      if (e.key === 'Enter') hits.querySelector('a')?.click();
    });
    addEventListener('keydown', e => { if (e.key === '/' && document.activeElement !== input) { e.preventDefault(); input.focus(); } });
  }

  window.PageMint = {say, emote, trick, move, mark, clearMarks};
  setTimeout(() => say(document.body.dataset.hello || 'Hi! I\'m Mint.'), 1200);
})();
