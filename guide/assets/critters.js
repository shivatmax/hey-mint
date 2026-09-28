/* The critters, for the web: the same eight species as mint/ui/critters.py (same shapes, colours
   and signature moves), drawn as SVG, living beside a big Mint and a toy box.
   <div data-playground> gets filled in; everything in it reacts to the pointer and to clicks. */
(() => {
  const stage = document.querySelector('[data-playground]');
  if (!stage) return;
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const NS = 'http://www.w3.org/2000/svg';
  const H = 64, Y = y => H - y;                                   // mint/ui/critters.py draws y-up
  const DARK = [.13, .10, .16], PINK = [1, .55, .66], WHITE = [1, 1, 1];
  const mix = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);
  const css = c => `rgb(${c.map(v => Math.round(v * 255)).join(',')})`;
  const hex = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16) / 255);

  const CREW = [
    {name: 'Astra', s: 'owl', c: '#8B7CFF', job: 'researches'},
    {name: 'Luna', s: 'bun', c: '#2EC4B6', job: 'writes code'},
    {name: 'Sage', s: 'kit', c: '#FFB547', job: 'writes reports'},
    {name: 'Codex', s: 'sprout', c: '#10A37F', job: 'builds websites'},
    {name: 'Mochi', s: 'cat', c: '#FF6FB5', job: 'naps a lot'},
    {name: 'Pip', s: 'chick', c: '#FFD166', job: 'cheers you on'},
    {name: 'Ember', s: 'drake', c: '#FF7A6B', job: 'keeps it warm'},
    {name: 'Teddy', s: 'bear', c: '#C08457', job: 'gives hugs'},
  ];
  const SIGNATURE = {owl: 'hoo hoo!', bun: 'thump thump', sprout: 'whirr!', kit: 'chases its tail', cat: 'streeetch',
    chick: 'peck peck', drake: 'rawr!', bear: 'bear hug!'};
  const HELLO = ['hi!', 'hehe', 'yay!', 'hello!', 'boop', '♪'];

  // ---------- drawing ----------
  function el(tag, attrs = {}, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    parent?.appendChild(e);
    return e;
  }
  // A path in critters.py's terms: ["m",x,y] ["l",x,y] ["q",cx,cy,x,y] ["z"], y-up.
  const P = steps => steps.map(([k, ...v]) => k === 'z' ? 'Z' : k.toUpperCase() + v.map((n, i) => i % 2 ? Y(n) : n).join(',')).join(' ');
  const ell = (cx, cy, w, h, attrs, parent) => el('ellipse', {cx, cy: Y(cy), rx: w / 2, ry: h / 2, ...attrs}, parent);
  const pivot = (g, x, y) => { g.style.transformOrigin = `${x}px ${Y(y)}px`; return g; };

  function drawCritter(sp, color) {
    const rgb = hex(color), fur = css(mix(rgb, WHITE, .22)), edge = css(mix(rgb, DARK, .45)),
      belly = css(mix(rgb, WHITE, .68)), inner = css(mix(PINK, mix(rgb, WHITE, .22), .25));
    const svg = el('svg', {viewBox: '-4 6 64 62', class: 'critter-svg', 'aria-hidden': 'true'});
    const body = pivot(el('g', {class: 'c-body'}, svg), 28, 0);
    const back = el('g', {}, body);
    const parts = {ears: [], tail: [], wings: [], leaves: []};
    const shape = (d, fill, stroke, w, parent) => el('path', {d, fill: fill || 'none', stroke: stroke || 'none', 'stroke-width': w || 0,
      'stroke-linejoin': 'round', 'stroke-linecap': 'round'}, parent);
    const ear = (pts, innerPts, pv) => {
      const g = pivot(el('g', {}, back), ...pv);
      shape(P([['m', ...pts[0]], ...pts.slice(1).map(p => ['l', ...p]), ['z']]), fur, edge, 1.2, g);
      if (innerPts) shape(P([['m', ...innerPts[0]], ...innerPts.slice(1).map(p => ['l', ...p]), ['z']]), inner, null, 0, g);
      parts.ears.push(g);
    };
    let front = [];
    if (sp === 'kit' || sp === 'cat') {
      const tip = sp === 'kit' ? 45 : 42;
      ear([[17, 27], [14, tip], [25, 31]], [[18.5, 29.5], [16.2, tip - 6], [22.5, 31]], [20, 29]);
      ear([[39, 27], [42, tip], [31, 31]], [[37.5, 29.5], [39.8, tip - 6], [33.5, 31]], [36, 29]);
    } else if (sp === 'bun') {
      for (const [cx, lean] of [[22.5, -1], [33.5, 1]]) {
        const g = pivot(el('g', {}, back), cx, 28);
        g.style.rotate = `${lean * .12}rad`;
        ell(cx, 39, 9, 24, {fill: fur, stroke: edge, 'stroke-width': 1.2}, g);
        ell(cx, 38.5, 4.4, 17, {fill: inner}, g);
        parts.ears.push(g);
      }
    } else if (sp === 'owl') {
      ear([[15, 26], [14, 38], [23, 31]], null, [18, 29]);
      ear([[41, 26], [42, 38], [33, 31]], null, [38, 29]);
    } else if (sp === 'bear') {
      for (const cx of [17, 39]) {
        const g = pivot(el('g', {}, back), cx, 28);
        ell(cx, 30.5, 11, 11, {fill: fur, stroke: edge, 'stroke-width': 1.2}, g);
        ell(cx, 30.5, 5, 5, {fill: inner}, g);
        parts.ears.push(g);
      }
    } else if (sp === 'drake') {
      for (const pts of [[[20, 31], [17.5, 40], [24.5, 32.5]], [[36, 31], [38.5, 40], [31.5, 32.5]]])
        shape(P([['m', ...pts[0]], ['l', ...pts[1]], ['l', ...pts[2]], ['z']]), css([1, .93, .78]), edge, 1, back);
      for (const s of [-1, 1]) {
        const x0 = 28 + s * 11, g = pivot(el('g', {}, back), x0, 19);
        shape(P([['m', x0, 22], ['q', x0 + s * 12, 32, x0 + s * 15, 26], ['q', x0 + s * 11, 22, x0 + s * 13, 17],
          ['q', x0 + s * 7, 18, x0, 16], ['z']]), css(mix(mix(rgb, WHITE, .22), mix(rgb, WHITE, .68), .4)), edge, 1.1, g);
        parts.wings.push(g);
      }
      front.push(g => shape(P([['m', 25, 32], ['l', 28, 36], ['l', 31, 32], ['z']]), belly, edge, .9, g));
    }
    // Tails, behind the body.
    if (sp === 'kit') {
      const g = pivot(el('g', {}, back), 41, 10);
      shape(P([['m', 40, 8], ['q', 55, 5, 53, 20], ['q', 52, 28, 45, 25], ['q', 47, 15, 40, 13], ['z']]), fur, edge, 1.2, g);
      ell(50.5, 22.5, 6, 6, {fill: '#fff'}, g);
      parts.tail.push(g);
    } else if (sp === 'cat') {
      const g = pivot(el('g', {}, back), 41, 8), d = P([['m', 40, 7], ['q', 51, 6, 49, 18], ['q', 48, 25, 53, 26]]);
      shape(d, null, edge, 3.6, g); shape(d, null, fur, 2.2, g);
      parts.tail.push(g);
    } else if (sp === 'bun' || sp === 'bear') {
      const r = sp === 'bun' ? 8 : 6;
      ell(43.5, 9, r, r, {fill: sp === 'bun' ? '#fff' : fur, stroke: edge, 'stroke-width': 1}, back);
    } else if (sp === 'drake') {
      const g = pivot(el('g', {}, back), 41, 9);
      shape(P([['m', 40, 6], ['q', 50, 4, 54, 9], ['l', 51, 10], ['l', 53, 14], ['q', 47, 12, 41, 13], ['z']]), fur, edge, 1.1, g);
      parts.tail.push(g);
    }
    // Feet, body, belly.
    const foot = sp === 'chick' ? css([1, .62, .2]) : css(mix(rgb, DARK, .25));
    for (const x of [21, 35]) ell(x, 3.2, 10, 6, {fill: foot, stroke: edge, 'stroke-width': 1}, body);
    const tall = sp === 'drake' ? 30 : 28, wide = sp === 'chick' || sp === 'bear' ? 32 : 30;
    ell(28, 4 + tall / 2, wide, tall, {fill: fur, stroke: edge, 'stroke-width': 1.3}, body);
    ell(28, 12.5, 18, 13, {fill: belly}, body);
    for (const f of front) f(body);
    if (sp === 'chick') {
      for (const [dx, h] of [[-3, 5], [0, 7], [3, 5]]) ell(28 + dx, 32 + h / 2, 3.2, h, {fill: fur, stroke: edge, 'stroke-width': .9}, body);
      for (const s of [-1, 1]) {
        const g = pivot(el('g', {}, body), 28 + s * 14, 19);
        ell(28 + s * 15.5, 15, 7, 12, {fill: css(mix(mix(rgb, WHITE, .22), mix(rgb, DARK, .45), .2)), stroke: edge, 'stroke-width': 1}, g);
        parts.wings.push(g);
      }
    } else if (sp === 'sprout') {
      shape(P([['m', 28, 31], ['l', 28, 36]]), null, css([.2, .5, .25]), 1.4, body);
      for (const s of [-1, 1]) {
        const g = pivot(el('g', {}, body), 28, 36);
        shape(P([['m', 28, 36], ['q', 28 + s * 4, 45, 28 + s * 13, 44], ['q', 28 + s * 9, 35, 28, 36], ['z']]),
          css([.42, .82, .42]), css([.2, .5, .25]), 1, g);
        parts.leaves.push(g);
      }
    } else if (sp === 'owl') {
      for (const s of [-1, 1]) {
        const g = pivot(el('g', {}, body), 28 + s * 14, 19);
        ell(28 + s * 15, 14, 7, 14, {fill: css(mix(mix(rgb, WHITE, .22), mix(rgb, DARK, .45), .25)), stroke: edge, 'stroke-width': 1}, g);
        parts.wings.push(g);
      }
    }
    // Face.
    for (const x of [17.5, 38.5]) ell(x, 16.5, 6, 3.4, {fill: css(PINK), opacity: .6}, body);
    const eyes = el('g', {class: 'c-eyes'}, body);
    for (const x of [22.5, 33.5]) {
      const g = pivot(el('g', {class: 'c-eye'}, eyes), x, 22);
      if (sp === 'owl') ell(x, 22, 10, 10, {fill: '#fff', stroke: edge, 'stroke-width': .8}, g);
      ell(x, 22, sp === 'owl' ? 5.6 : 5.2, sp === 'owl' ? 6.2 : 6.6, {fill: css(DARK)}, g);
      ell(x + 1.3, 23.8, 2.2, 2.2, {fill: '#fff'}, g);
    }
    shape(P([['m', 20, 21], ['q', 22.5, 25.5, 25, 21], ['m', 31, 21], ['q', 33.5, 25.5, 36, 21]]), null, css(DARK), 1.7, body)
      .classList.add('c-happy');
    let spiral = [];
    for (const x of [22.5, 33.5]) spiral.push(['m', x + 2.4, 22], ['q', x + 2.4, 24.6, x, 24.6], ['q', x - 2.6, 24.6, x - 2.6, 22],
      ['q', x - 2.6, 19.8, x, 19.8], ['q', x + 1.4, 19.8, x + 1.4, 22]);
    shape(P(spiral), null, css(DARK), 1.2, body).classList.add('c-dizzy');
    if (sp === 'owl' || sp === 'chick')
      shape(P([['m', 26, 18.5], ['l', 30, 18.5], ['l', 28, 15.4], ['z']]), css([1, .66, .2]), css(mix([1, .66, .2], DARK, .4)), .8, body);
    const mouth = shape('', null, css(DARK), 1.3, body);
    mouth.classList.add('c-mouth');
    if (sp === 'cat') shape(P([['m', 16, 17.5], ['l', 9, 19.5], ['m', 16, 16], ['l', 9, 15], ['m', 40, 17.5], ['l', 47, 19.5],
      ['m', 40, 16], ['l', 47, 15]]), null, edge, .8, body);
    return {svg, body, eyes, mouth, parts};
  }

  const MOUTH = {
    normal: [P([['m', 25, 17], ['q', 28, 14, 31, 17]]), null],
    happy: [P([['m', 24.5, 17], ['q', 28, 11.5, 31.5, 17], ['z']]), '#9E2940'],
    surprised: ['M28,47.4 m-1.7,0 a1.7,2 0 1,0 3.4,0 a1.7,2 0 1,0 -3.4,0', '#9E2940'],
    sad: [P([['m', 25, 14], ['q', 28, 17, 31, 14]]), null],
    dizzy: [P([['m', 24.5, 15], ['q', 26.2, 17, 28, 15], ['q', 29.8, 13, 31.5, 15]]), null],
  };

  // ---------- one critter ----------
  class Critter {
    constructor(info, parent) {
      Object.assign(this, info);
      this.el = document.createElement('button');
      this.el.className = 'critter';
      this.el.type = 'button';
      this.el.setAttribute('aria-label', `${info.name}, who ${info.job}. Click for a trick.`);
      this.drawing = drawCritter(info.s, info.c);
      this.el.appendChild(this.drawing.svg);
      this.tag = document.createElement('span');
      this.tag.className = 'c-tag';
      this.tag.innerHTML = `<b>${info.name}</b> ${info.job}`;
      this.el.appendChild(this.tag);
      parent.appendChild(this.el);
      this.pokes = [];
      this.mood('normal');
      this.drawing.body.style.animationDelay = (-Math.random() * 3).toFixed(2) + 's';
      this.el.addEventListener('pointerenter', () => { if (!this.busy) { this.hop(5, 320); this.mood('happy', 900); } });
      this.el.addEventListener('click', () => this.poked());
      this.el.addEventListener('dblclick', () => this.flip());
    }
    mood(name, ms) {
      const [d, fill] = MOUTH[name] || MOUTH.normal, svg = this.drawing.svg;
      this.drawing.mouth.setAttribute('d', d);
      this.drawing.mouth.setAttribute('fill', fill || 'none');
      svg.classList.toggle('happy', name === 'happy');
      svg.classList.toggle('dizzy', name === 'dizzy');
      svg.classList.toggle('beaky', (this.s === 'owl' || this.s === 'chick') && name === 'normal');
      clearTimeout(this._moodT);
      if (ms) this._moodT = setTimeout(() => this.mood('normal'), ms);
    }
    anim(target, frames, ms, opts = {}) {
      if (reduce || !target) return null;
      return target.animate(frames, {duration: ms, easing: 'cubic-bezier(.3,.7,.3,1)', composite: 'add', ...opts});
    }
    hop(h = 14, ms = 450, delay = 0) {
      this.anim(this.el, [{transform: 'translateY(0)'}, {transform: `translateY(${-h}px)`, offset: .45}, {transform: 'translateY(0)'}],
        ms, {delay, easing: 'ease-out', composite: 'replace'});
      this.anim(this.drawing.body, [{scale: '1 1'}, {scale: '.94 1.08', offset: .3}, {scale: '1 1', offset: .75}, {scale: '1.12 .86', offset: .88}, {scale: '1 1'}],
        ms + 120, {delay, composite: 'replace'});
    }
    say(text, ms = 1600) {
      const b = document.createElement('span');
      b.className = 'c-say'; b.textContent = text;
      this.el.appendChild(b); this.el.classList.add('saying');
      clearTimeout(this._sayT);
      setTimeout(() => b.remove(), ms);
      this._sayT = setTimeout(() => this.el.classList.remove('saying'), ms);
    }
    float(chars, n = 3, color) {
      for (let i = 0; i < n; i++) {
        const p = document.createElement('span');
        p.className = 'c-float'; p.textContent = chars[i % chars.length];
        p.style.color = color || this.c;
        p.style.left = (30 + Math.random() * 40) + '%';
        p.style.setProperty('--dx', (Math.random() * 40 - 20) + 'px');
        p.style.animationDelay = (i * .14) + 's';
        this.el.appendChild(p);
        setTimeout(() => p.remove(), 1600 + i * 140);
      }
    }
    blink() { this.anim(this.drawing.eyes, [{scale: '1 1'}, {scale: '1 .1'}, {scale: '1 1'}], 180, {composite: 'replace'}); }
    fidget() {
      const {ears, tail, wings, leaves} = this.drawing.parts;
      ears.forEach(e => this.anim(e, [{rotate: '0rad'}, {rotate: '-.25rad'}, {rotate: '.1rad'}, {rotate: '0rad'}], 500));
      tail.forEach(t => this.anim(t, [{rotate: '0rad'}, {rotate: '-.35rad'}, {rotate: '.2rad'}, {rotate: '0rad'}], 900));
      wings.forEach((w, i) => this.anim(w, [{rotate: '0rad'}, {rotate: `${i ? -.5 : .5}rad`}, {rotate: '0rad'}], 500));
      leaves.forEach(l => this.anim(l, [{rotate: '0rad'}, {rotate: '-.2rad'}, {rotate: '.2rad'}, {rotate: '0rad'}], 1000));
    }
    flap(ms = 800) {
      this.drawing.parts.wings.forEach((w, i) => this.anim(w, [{rotate: '0rad'}, {rotate: `${i ? -.6 : .6}rad`}, {rotate: '0rad'}],
        160, {iterations: Math.round(ms / 160)}));
    }
    signature() {
      const {body, parts} = this.drawing;
      if (this.s === 'owl') {
        this.anim(body, [{rotate: '0rad'}, {rotate: '-.32rad', offset: .15}, {rotate: '-.32rad', offset: .4}, {rotate: '.32rad', offset: .55},
          {rotate: '.32rad', offset: .85}, {rotate: '0rad'}], 1100);
        setTimeout(() => this.blink(), 450); this.float(['♪', '♫'], 2);
      } else if (this.s === 'bun') {
        if (parts.ears[1]) this.anim(parts.ears[1], [{rotate: '0rad'}, {rotate: '.9rad', offset: .2}, {rotate: '.9rad', offset: .7}, {rotate: '0rad'}], 1000);
        this.hop(8, 260); this.hop(8, 260, 360);
      } else if (this.s === 'sprout') {
        parts.leaves.forEach(l => this.anim(l, [0, -.6, .6, -.6, .6, 0].map(r => ({rotate: r + 'rad'})), 900));
        this.hop(30, 900); this.float(['🍃', '✦'], 4, '#4CB85C');
      } else if (this.s === 'kit') {
        this.anim(this.drawing.svg, [{transform: 'rotateY(0)'}, {transform: 'rotateY(720deg)'}], 900, {composite: 'replace'});
        parts.tail.forEach(t => this.anim(t, [0, -.5, .4, -.5, 0].map(r => ({rotate: r + 'rad'})), 900));
        this.hop(10, 900);
      } else if (this.s === 'cat') {
        this.anim(body, [{scale: '1 1'}, {scale: '1.32 .74', offset: .3}, {scale: '1.32 .74', offset: .75}, {scale: '1 1'}], 1100, {composite: 'replace'});
        setTimeout(() => this.float(['♥'], 2, '#FF4F7B'), 900);
      } else if (this.s === 'chick') {
        this.anim(body, [0, -.35, 0, -.35, 0, -.35, 0].map(r => ({rotate: r + 'rad'})), 900);
        this.flap(900);
      } else if (this.s === 'drake') {
        this.flap(700);
        const f = document.createElement('span'); f.className = 'c-flame'; f.textContent = '🔥';
        this.el.appendChild(f); setTimeout(() => f.remove(), 800);
      } else {
        this.anim(body, [0, .12, -.12, .12, -.12, 0].map(r => ({rotate: r + 'rad'})), 600);
        this.hop(12, 500); setTimeout(() => this.float(['♥', '♥'], 2, '#FF4F7B'), 400);
      }
      return SIGNATURE[this.s];
    }
    poked() {
      const now = Date.now();
      this.pokes = this.pokes.filter(t => now - t < 1800).concat(now);
      if (this.pokes.length >= 5) {
        this.pokes = []; this.mood('dizzy', 1800);
        this.anim(this.drawing.body, [0, .2, -.2, .2, -.2, 0].map(r => ({rotate: r + 'rad'})), 900);
        this.say('dizzy…'); return;
      }
      this.mood('happy', 1400);
      if (this.pokes.length === 1) { this.say(this.signature()); }
      else { this.hop(12 + this.pokes.length * 3, 420); this.float(['♥'], 1, '#FF4F7B'); this.say(HELLO[Math.random() * HELLO.length | 0], 900); }
    }
    flip() {
      this.mood('happy', 1400);
      this.anim(this.el, [{transform: 'translateY(0) rotate(0)'}, {transform: 'translateY(-34px) rotate(-180deg)', offset: .5},
        {transform: 'translateY(0) rotate(-360deg)'}], 750, {composite: 'replace', easing: 'ease-in-out'});
      this.float(['✦', '★', '✧'], 4, '#FFC83D');
    }
    look(dx, dy) {
      const d = Math.hypot(dx, dy) || 1, k = Math.min(1, d / 300) * 1.4;
      this.drawing.eyes.style.translate = `${(dx / d * k).toFixed(2)}px ${(dy / d * k).toFixed(2)}px`;
    }
  }

  // ---------- the stage: Mint in the middle, the crew hiding in the gift box ----------
  // Where each critter stands once out: feet at (x% of the width, y% of the height), around Mint.
  const SPOTS = [[15, 31], [6, 60], [16, 88], [31, 99], [69, 99], [84, 88], [94, 60], [85, 31]];
  const mint = stage.querySelector('.big-orb');
  const box = document.createElement('button');
  box.type = 'button'; box.className = 'toybox';
  box.setAttribute('aria-label', 'The gift box. The crew is inside: click to let them out.');
  box.innerHTML = `<span class="peek-clip"><span class="peeker"></span></span>
    <svg viewBox="0 0 60 64" aria-hidden="true">
    <ellipse cx="30" cy="61" rx="22" ry="2.8" fill="rgba(40,50,90,.13)"/>
    <rect x="10" y="25" width="40" height="33" rx="4" fill="#A7EDD6" stroke="#5FB39A" stroke-width="1.6"/>
    <rect x="26.5" y="25" width="7" height="33" fill="#F06C9B"/>
    <circle class="bx-eye" cx="20" cy="41" r="1.8" fill="#16202E"/><circle class="bx-eye" cx="40" cy="41" r="1.8" fill="#16202E"/>
    <path d="M18 46 q2 2 4 0" stroke="#FF8FB8" stroke-width="1.6" fill="none" stroke-linecap="round"/>
    <g class="lid"><rect x="7" y="14" width="46" height="11" rx="3" fill="#BDF5E3" stroke="#5FB39A" stroke-width="1.6"/>
      <rect x="26.5" y="14" width="7" height="11" fill="#F06C9B"/>
      <path d="M30 14 C22 3 13 8 21 13 Z M30 14 C38 3 47 8 39 13 Z" fill="#F7A3C0" stroke="#D65384" stroke-width="1.4" stroke-linejoin="round"/></g></svg>
    <span class="box-hint">click me!</span>`;
  stage.appendChild(box);
  const lid = box.querySelector('.lid'), peeker = box.querySelector('.peeker'), hint = box.querySelector('.box-hint');
  const note = document.querySelector('.play-hint');
  const setNote = text => { if (note) note.textContent = text; };
  const crew = CREW.map((info, i) => {
    const c = new Critter(info, stage);
    const [x, y] = SPOTS[i];
    c.el.style.setProperty('--x', x); c.el.style.setProperty('--y', y);
    if (x < 25) c.el.classList.add('edge-l'); else if (x > 75) c.el.classList.add('edge-r');
    c.el.classList.add('inside');
    c.peek = drawCritter(info.s, info.c);            // its head, for peeking out of the box
    c.peek.svg.classList.add('peek-svg');
    peeker.appendChild(c.peek.svg);
    return c;
  });
  let state = 'in', busy = false;
  const wait = ms => new Promise(r => setTimeout(r, reduce ? 0 : ms));
  const emote = name => mint ? window.PageMint?.emote(name, mint) : window.PageMint?.emote(name);
  const say = text => window.PageMint?.say(text);
  const play = (el, frames, ms, opts = {}) => reduce ? null : el.animate(frames, {duration: ms, easing: 'cubic-bezier(.3,.7,.3,1)', fill: 'both', ...opts});
  const lidTo = (open, ms = 260) => play(lid, [{transform: lid.style.transform || 'none'},
    {transform: open === 'wide' ? 'translate(-6px,-26px) rotate(-38deg)' : open ? 'translate(-2px,-5px) rotate(-24deg)' : 'none'}],
    ms, {easing: open ? 'cubic-bezier(.3,1.5,.5,1)' : 'cubic-bezier(.5,0,.3,1.4)'})
    ?.finished.then(() => { lid.style.transform = open === 'wide' ? 'translate(-6px,-26px) rotate(-38deg)' : open ? 'translate(-2px,-5px) rotate(-24deg)' : ''; });

  addEventListener('pointermove', e => {
    for (const c of crew) {
      if (state !== 'out') break;
      const r = c.el.getBoundingClientRect();
      c.look(e.clientX - (r.left + r.width / 2), e.clientY - (r.top + r.height * .45));
    }
  }, {passive: true});
  if (!reduce) setInterval(() => {
    if (state !== 'out') return;
    const c = crew[Math.random() * crew.length | 0];
    Math.random() < .5 ? c.blink() : c.fidget();
  }, 1300);

  // While they are inside, one of them keeps peeking out.
  let peekAnims = [];
  const track = a => { if (a) peekAnims.push(a); return a; };
  function stopPeeking() {
    for (const a of peekAnims) a.cancel();
    for (const a of [...peeker.getAnimations(), ...lid.getAnimations()]) a.cancel();
    peekAnims = []; busy = false;
  }
  async function peekOnce() {
    if (state !== 'in' || busy || reduce) return;
    const c = crew[Math.random() * crew.length | 0];
    for (const other of crew) other.peek.svg.classList.toggle('on', other === c);
    busy = true;
    try {
      track(play(lid, [{transform: 'none'}, {transform: 'translate(-2px,-5px) rotate(-24deg)'}], 260, {easing: 'cubic-bezier(.3,1.5,.5,1)'}));
      await wait(120);
      if (state !== 'in') return;
      await track(play(peeker, [{transform: 'translateY(100%)'}, {transform: 'translateY(40%)'}], 380, {easing: 'cubic-bezier(.3,1.6,.5,1)'}))?.finished;
      const glance = Math.random() < .5 ? 1 : -1;
      track(play(c.peek.eyes, [{translate: '0 0'}, {translate: `${glance * 1.6}px 0`, offset: .25}, {translate: `${glance * 1.6}px 0`, offset: .45},
        {translate: `${-glance * 1.6}px 0`, offset: .7}, {translate: '0 0'}], 1100, {fill: 'none'}));
      if (Math.random() < .45) { const f = document.createElement('span'); f.className = 'box-float'; f.textContent = ['?', '!', '♪', '…', '♥'][Math.random() * 5 | 0];
        f.style.color = c.c; box.appendChild(f); setTimeout(() => f.remove(), 1300); }
      await wait(1150);
      if (state !== 'in') return;
      await track(play(peeker, [{transform: 'translateY(40%)'}, {transform: 'translateY(100%)'}], 230, {easing: 'ease-in'}))?.finished;
      await track(play(lid, [{transform: 'translate(-2px,-5px) rotate(-24deg)'}, {transform: 'none'}], 220, {easing: 'cubic-bezier(.5,0,.3,1.4)'}))?.finished;
      if (Math.random() < .35) play(box, [{rotate: '0deg'}, {rotate: '-4deg'}, {rotate: '4deg'}, {rotate: '-2deg'}, {rotate: '0deg'}], 420, {fill: 'none'});
      for (const a of peekAnims) a.cancel();
      peekAnims = [];
    } catch (e) { /* interrupted by a click */ }
    finally { if (state === 'in') busy = false; }
  }
  (function loop() { peekOnce(); setTimeout(loop, 1700 + Math.random() * 1600); })();

  function offsetToBox(c) {
    const b = box.getBoundingClientRect(), r = c.el.getBoundingClientRect();
    return [b.left + b.width / 2 - (r.left + r.width / 2), b.top + b.height * .35 - (r.top + r.height * .6)];
  }
  function sparkle(n = 10) {
    for (let i = 0; i < n; i++) {
      const s = document.createElement('span'); s.className = 'box-spark'; s.textContent = ['✦', '✧', '★', '•'][i % 4];
      s.style.color = ['#FFC83D', '#FF8FB8', '#5FE0CF', '#A99CFF'][i % 4];
      const a = -Math.PI * (.1 + .8 * Math.random()), d = 40 + Math.random() * 60;
      s.style.setProperty('--dx', Math.cos(a) * d + 'px'); s.style.setProperty('--dy', Math.sin(a) * d + 'px');
      box.appendChild(s); setTimeout(() => s.remove(), 900);
    }
  }
  async function letOut() {
    if (state !== 'in') return;
    state = 'moving'; hint.remove();
    stopPeeking();
    stage.classList.add('open');
    play(box, [{scale: '1 1'}, {scale: '1.12 .84', offset: .35}, {scale: '.94 1.08', offset: .7}, {scale: '1 1'}], 420, {fill: 'none'});
    lidTo('wide', 380); sparkle(12); emote('surprised');
    crew.forEach((c, i) => {
      const [dx, dy] = offsetToBox(c), dir = c.el.style.getPropertyValue('--x') < 50 ? 1 : -1, lift = 110 + Math.random() * 50;
      setTimeout(() => {
        c.el.classList.remove('inside');
        const fly = play(c.el, [
          {transform: `translate(${dx}px,${dy}px) scale(.2) rotate(0deg)`, opacity: 0},
          {transform: `translate(${dx * .9}px,${dy * .9 - 30}px) scale(.55) rotate(${-dir * 60}deg)`, opacity: 1, offset: .14},
          {transform: `translate(${dx * .45}px,${dy * .45 - lift}px) scale(1.08) rotate(${-dir * 220}deg)`, offset: .55},
          {transform: 'translate(0,0) scale(1) rotate(' + (-dir * 360) + 'deg)'}], 820, {easing: 'cubic-bezier(.25,.6,.35,1)', fill: 'none'});
        (fly ? fly.finished : Promise.resolve()).then(() => {
          c.hop(0, 10); c.mood('happy', 1300);
          play(c.drawing.body, [{scale: '1 1'}, {scale: '1.18 .8', offset: .3}, {scale: '.94 1.06', offset: .65}, {scale: '1 1'}], 420, {fill: 'none', composite: 'replace'});
          c.say(c.name, 900);
        });
      }, i * 120);
    });
    await wait(crew.length * 120 + 600);
    lidTo(false, 380);
    await wait(300);
    emote('laugh'); say('Say hi to the crew! Click the box to send them home.');
    box.setAttribute('aria-label', 'The gift box. Click to send the crew back in.');
    setNote('Hover a critter to meet it, click for its trick. Click the box to send them home.');
    state = 'out';
  }
  async function callBack() {
    if (state !== 'out') return;
    state = 'moving'; emote('wave');
    lidTo('wide', 320);
    [...crew].reverse().forEach((c, i) => setTimeout(() => {
      const [dx, dy] = offsetToBox(c), dir = c.el.style.getPropertyValue('--x') < 50 ? 1 : -1;
      const fly = play(c.el, [{transform: 'translate(0,0) scale(1) rotate(0deg)', opacity: 1},
        {transform: `translate(${dx * .5}px,${dy * .5 - 90}px) scale(.9) rotate(${dir * 180}deg)`, offset: .5},
        {transform: `translate(${dx}px,${dy}px) scale(.2) rotate(${dir * 360}deg)`, opacity: 0}], 620, {easing: 'cubic-bezier(.4,0,.6,1)', fill: 'none'});
      (fly ? fly.finished : Promise.resolve()).then(() => c.el.classList.add('inside'));
    }, i * 90));
    await wait(crew.length * 90 + 700);
    await lidTo(false, 320);
    play(box, [{scale: '1 1'}, {scale: '1.08 .9'}, {scale: '1 1'}], 300, {fill: 'none'});
    stage.classList.remove('open');
    box.setAttribute('aria-label', 'The gift box. The crew is inside: click to let them out.');
    setNote('Poke Mint. The crew is in the gift box: click it to let them out.');
    state = 'in';
  }
  box.addEventListener('click', () => state === 'in' ? letOut() : state === 'out' ? callBack() : null);

  // The show: a stadium wave, a solo each, a dance, a bow. Mint claps along.
  let showing = false;
  async function show() {
    if (showing) return;
    if (state === 'in') await letOut();
    while (state !== 'out') await wait(100);
    showing = true;
    say('Showtime!'); emote('smile');
    for (let k = 0; k < 2; k++) crew.forEach((c, i) => setTimeout(() => { c.mood('happy', 700); c.hop(22, 420); }, (k * crew.length + i) * 90));
    await wait(crew.length * 180 + 500);
    for (const c of crew) { c.say(c.signature(), 1300); c.mood('happy', 1200); await wait(900); }
    emote('dance');
    crew.forEach((c, i) => {
      c.anim(c.drawing.body, [{rotate: '-.18rad'}, {rotate: '.18rad'}], 380, {iterations: 8, direction: 'alternate', delay: i * 40});
      c.float(['♪', '♫'], 2);
    });
    await wait(3000);
    emote('clap');
    crew.forEach(c => { c.mood('happy', 1500); c.anim(c.drawing.body, [{scale: '1 1'}, {scale: '1.05 .8', offset: .5}, {scale: '1 1'}], 700, {composite: 'replace'}); });
    await wait(600);
    crew.forEach((c, i) => setTimeout(() => c.float(['✦', '★'], 3, '#FFC83D'), i * 60));
    say('Ta-da! 🎉');
    await wait(900);
    showing = false;
  }
  document.querySelectorAll('[data-show]').forEach(b => b.addEventListener('click', show));
  document.querySelectorAll('[data-big-emote]').forEach(b => b.addEventListener('click', () => {
    emote(b.dataset.bigEmote);
    if (state !== 'out') return;
    if (['love', 'laugh', 'dance'].includes(b.dataset.bigEmote)) crew.forEach((c, i) => setTimeout(() => { c.mood('happy', 1200); c.hop(8, 360); }, i * 60));
    if (b.dataset.bigEmote === 'surprised') crew.forEach(c => c.mood('surprised', 1200));
    if (b.dataset.bigEmote === 'sleepy') crew.forEach(c => c.mood('sad', 1600));
  }));
})();
