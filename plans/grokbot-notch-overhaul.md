# Mint × Grok Bot study — notch & character overhaul plan

Sources studied (2026-10-07):
- `~/Documents/Codex/2026-10-07/cre/outputs/grokbot-cover-1080p.mp4` (53 s, 1080p60), frame by frame
- novra.design/case-studies/grokbot — full text + the 8 character-state clips
  (Follow, Annoyed, Slap, Dizzy, Error, Finished, Upload, Working) + the motion clips
  (Mac notch working, greeting/opening, widget working→finished)
- Reference frames saved in `ref/grokbot/` (open-working, closed, error, drop-zone,
  upload-progress, composer-and-window-glow, open-transition-15fps, greeting-8fps,
  states-*.jpg, widget-focus-switch)

Credit: concept by Novra (independent, not xAI); mascot/motion by Benji Taylor.
We borrow **principles and the system**, not their mascot. Mint keeps its own face.

---

## 1. What they do — the design, decoded

### 1.1 Core idea
"If the character already tells you what the agent is up to, why should it disappear
the second you leave the app?" The character **is** the status indicator. Every state
must stay readable when the UI collapses to its smallest size.

"One bot in focus. The rest in sight." Active agent gets room for state + current task;
others collapse to the minimum: **a face and the line they're working on**.

### 1.2 Layout (measured from frames, 1080p → pt ≈ ÷2 on a 2× display)
| Part | What it is |
|---|---|
| Closed island | Pure black. Left wing: main face (~28 pt) with a state-coloured radial glow bleeding into the black. Right wing: **2×2 cluster of sub-agent faces** (~11 pt each, solid hue). Nothing else. |
| Open: top bar | Left: Home (selected = grey capsule bg), Chat, "+" (new). Right: cloud, gear, globe, `100%`, battery. Icons ~14 pt, grey; selected white. Sits in the black above the cards. |
| Open: cards | Inner cards #161616-ish, ~22 pt radius, 1 px hairline white @ ~6 %. 12–16 pt gutters. |
| Focus card | Big face (~64 pt) + **3-line task wheel**: previous (dim, small) / **current (white, larger, SF-symbol icon)** / next (dim). |
| Agents card | 2×2 **agent chips**: capsule, fill = hue @ ~12 %, border = hue @ ~35 %, label in hue, solid hue face on the left, label truncated with "…". |
| Narrow mode | When the focus card needs width (error, upload, composer), agents collapse to a **vertical overlapping stack of faces** in a slim card at the right. |

Agent hues seen: teal ≈ #4FC3A1, red ≈ #E5484D, amber ≈ #F5A524, violet ≈ #7C5CFF.
Main bot: white → sky-blue vertical gradient (≈ #E6E6E6 → #79B8E6), cyan glow.

### 1.3 State language (the clever part)
State is carried by **four redundant channels**, so it reads at any size:
1. **Face gradient** — lower half tints: blue = working, red = error, white/grey = idle, green ambient = done.
2. **Status badge** at the face's top-left: animated "•••" (working, dots cycle), solid red dot (error), green dot (finished). On hover the badge **morphs into a labelled pill** ("Working") and back.
3. **Ambient light** — radial glow behind the face (closed + open), plus the card's bottom edge glowing in the state colour (error = red/orange from the corners, done/upload = green).
4. **Eyes** — no mouth at all. Pill eyes = normal; flat dashes = error/tired; half-moons = annoyed/happy; dots = looking; arcs = content.

### 1.4 Character behaviours (Rive state machine)
- **Follow** — eyes track the cursor *with a delay*; eyes slide across the sphere and
  **foreshorten** near the edge (narrower, sometimes one eye hidden behind the curve) → reads as a 3-D ball.
- **Annoyed** — after repeated pokes: squint → body swells, white, angry half-moon eyes.
- **Slap** — click: squash, eyes jerk sideways, recoil.
- **Dizzy** — third slap: the ball *rolls* — eyes travel up over the top and come back round from below (rotation of a sphere, not a 2-D spin).
- **Error** — shrink then swell (a sad pulse), flat eyes, red tint; behind it rotating rounded-triangle outlines (ghost warning glyphs) radiate red.
- **Finished** — green dot, looks up and to the right, eyes become contented arcs, gentle tilt.
- **Upload** — the circle **morphs into a rounded window/folder shape with eyes**, then back.
- **Working** — badge dots cycle; hover → badge expands to "Working" pill.
- **Greeting** — island opens with **particle streams** (sparkle columns + confetti) drifting, a warm glow, face drops in, blinks, **detached floating hands** appear and wave; particles clear from the edges inward; then badge pops and the gradient turns blue as work begins.
- States coexist: an interaction never breaks the underlying work state (mapped in a Figma flowchart, built as one state machine).

### 1.5 Motion
- **Open (~0.45 s, slight settle, no big overshoot)**: island grows from the wings; the
  **face is a shared element** — it travels and scales from the wing into the focus card;
  the 2×2 cluster faces **fly out to become the chip avatars**; chip labels **wipe in
  left→right, staggered**; cards rise from ~0.96 scale + blur; top bar fades in last.
- **Task change**: the wheel rolls up one row; the new current line reveals with a
  gradient wipe (half bright, half dim mid-transition).
- **Close**: reverse; content goes first, then the shape.
- **Focus switch** (widget): selected agent chip grows into the focus slot while the old focus shrinks into a chip.
- **Progress**: the face *is* the progress-bar thumb, riding the bar with motion blur; % on the right.
- **Agent driving an app**: the target window gets an **animated iridescent gradient border** (Apple-Intelligence-style) while the agent works in it.
- **Composer**: the island becomes an input — text, "+", mic, white send pill — agents stack stays on the right.
- Sound design (SFX + music) under every state change.

### 1.6 Beyond the notch
Widget (multi-bot / single-bot), Lock-Screen Live Activity, and **even the app icon carrying state** ("not another badge — one more place the character can tell you something changed").

---

## 2. Where Mint is today (gap analysis)

| Area | Mint now | Grok Bot | Gap |
|---|---|---|---|
| Sub-agents | Critters pop out of a toy box *beside the orb* (`critters.py`), name tags | Agents live **in the notch**: 2×2 cluster closed, chips open, stack when narrow, focus switching | **Big** — critters aren't part of the notch at all |
| Current task | Caption words in the wing (`notch.py` compact/plain) | Focus card with 3-line task wheel + icons | Big |
| State channels | Swirl colours + speed, spinner, ring pulse, `finish_badge` | Gradient + badge(+pill) + ambient glow + eyes, all redundant | Medium — no badge, no card glow, closed glow weak |
| Open transition | Spring on the island path; content fades/slides 8 pt (`_reveal`) | Shared-element face, cluster→chips, staggered wipe, blur-in | Big |
| Character interaction | Eyes look at cursor, blink, wink, yawn, hops, moods | Follow-with-lag + foreshortening, poke/slap/annoyed/dizzy | Medium |
| Error | Rain flourish, shake | Error card: title, red sub-line, retry button, red glow, ghost glyphs | Medium |
| Progress / files | Progress ring on the orb; shelf drop | Dashed drop zone, face morphs to folder, face-thumb progress bar | Medium |
| Acting in apps | Hand-off drag, computer use, nothing on the window | Iridescent border on the target window | **Big, cheap win** |
| Composer | Chat window "becomes" a notch guest | Native composer inside the island | Medium |
| Sound | `fx.click` + a few | SFX for every state | Medium |
| Dock / menu-bar | Static | State carried on the icon | Small |

Mint keeps: its swirl orb identity, sparkly eyes, ω mouth, cheeks, critter species,
moods/sunglasses, music/calendar/battery/shelf/search panes. We **add the system**
around it.

---

## 3. Plan

Each phase ships on its own, behind prefs, live-installable, with guide clips.

### Phase 0 — Design tokens + motion kit (foundation, ~1 day)
- `look.py`: notch palette (island #000, card #161618, hairline white 6 %, text
  primary/secondary/tertiary), radii (island bottom 24, card 20, chip capsule), spacing
  scale (4/8/12/16), type scale (SF Pro: 11 meta / 13 chip / 15 body / 17 current task,
  semibold for current), **agent hues** (teal/red/amber/violet/sky/pink/lime/indigo — one per critter species).
- New `mint/ui/kinetics.py`:
  - spring presets: `snappy` (stiffness 300, critical), `gentle` (150, critical),
    `bouncy` (220, damping 14) — single source for every CASpringAnimation.
  - `stagger(layers, delay=0.035)`, `wipe_in(text_layer)` (gradient mask sweep L→R),
    `blur_in(layer)` (CIGaussianBlur via `layer.filters`; needs `layerUsesCoreImageFilters`),
  - `fly(layer, from_rect, to_rect)` — **matched-geometry**: snapshot/reparent a layer
    across the old and new layout with a spring on position + bounds + cornerRadius.
- Acceptance: notch open/close re-implemented on kinetics with identical behaviour.

### Phase 1 — Agents in the notch (~2–3 days) ★ biggest visible change
- `notch_agents.py` (new): model fed by the same events critters get
  (start/thinking/tool/progress/file/ask/done/failed/stopped, `parent`).
- **Closed**: right wing shows up to 4 mini critter faces in a 2×2 (≥5 → 3 + "+n"),
  each with its hue; a face blinks/bobs when its agent emits an event; finished ones get a
  tiny ✓ then leave with a hop.
- **Open/home**: two cards — focus card (Mint or focused agent, big face + task wheel)
  and agents card (chips: hue face + "Asking research…" line, truncated).
- **Narrow**: when the focus card needs width → vertical overlapping face stack.
- **Focus switch**: click a chip → chip `fly()`s into the focus slot, the old focus shrinks into a chip.
- Critters on the desktop stage stay for orb mode; in notch mode they live here
  (pref `notch_agents`, default on).
- Task wheel content: current tool/step line from session events ("Prep workspace",
  "Copy final outputs") with SF symbols per tool kind (reuse `cute_fx.KINDS` mapping).

### Phase 2 — State language (~1–2 days)
- **Status badge** on the orb/face (orb + notch + chips): working = 3 dots cycling,
  thinking = dots breathing, error = red dot, done = green dot, listening = mic dot.
  Hover → badge morphs to a pill with the state word, springs back on exit.
- **Gradient tint**: lower-half state colour layered over the swirl (keeps Mint's swirl,
  adds readable state).
- **Ambient glow**: radial state glow behind the face in the closed wing (bigger, softer
  than today), and a bottom-edge card glow in the open state (red/orange error, green done/upload, blue working).
- **Error card**: face (flat eyes, red tint, sad pulse), title + red one-line reason +
  circular retry button (re-runs the last request); rotating ghost-triangle outlines behind.
- **Finished**: green dot, look up-right, arc eyes, then Mint's existing sunglasses beat.
- Eyes-only expression set added to `orb.py` (dash / half-moon / dot / arc) and used by moods.

### Phase 3 — Transitions (~2 days)
- Open: shape spring → face `fly()` wing→card → cluster faces fly to chip avatars →
  chips `stagger` + `wipe_in` → cards `blur_in` from 0.96 → top bar last. Target ≈ 0.45 s.
- Close: content out first (0.12 s), then shape; face flies back to the wing.
- Task change: wheel rolls one row, new line wipes in.
- **Greeting** (first launch, wake word after long sleep, new day): island opens with a
  `CAEmitterLayer` sparkle/confetti stream, warm glow, face drops in, blinks, two floating
  hands wave (new layers on the orb), particles clear edges-inward, then normal state.
- Orb↔notch switch reuses `fly()` so the orb and the notch face are the same object visually.

### Phase 4 — Character physics & play (~1–2 days)
- **Follow with lag**: eyes ease toward cursor (already) + **sphere foreshortening** —
  eye width scales by cos(angle) near the rim, far eye clips behind the curve.
- **Poke state machine** in `orb.py`/`emotes.py`: click = slap (squash, eyes jerk);
  3 quick clicks = dizzy (eyes roll over the top and back = sphere rotation); repeated
  pokes = annoyed (swell, half-moon eyes, small huff), calm down after 4 s. Never
  interrupts work state (badge + task stay correct underneath).
- Critters get the same follow/poke reactions at small scale.

### Phase 5 — Work surfaces (~2–3 days)
- **Window glow**: while Mint (computer use / hand-off / browser agent) acts on a window,
  draw a click-through overlay panel exactly on that window's frame with an animated
  conic iridescent border (blur + mask); fades out on done. Track window moves via
  `CGWindowListCopyWindowInfo` at 10 Hz. Pref `agent_window_glow`.
- **Drop zone**: dragging a file over the notch → dashed rounded border, face morphs into
  a rounded folder/window shape with eyes; drop → green glow.
- **Progress bar with face thumb**: long jobs (downloads, image generation, file moves,
  installs) show a bar in the notch where Mint's face rides as the thumb, % at right.
- **Composer in the island**: "+" tab / ⌘-shortcut turns the island into an input
  (text, attach "+", mic, white send pill), agent stack stays right; Enter sends to the
  session, island morphs back into the working focus card.

### Phase 6 — Polish (~1 day)
- **SFX set** (soft, short, pref `sounds`): open, close, task tick, done chime, error
  thud, poke boing, dizzy whirl, greeting sparkle. Generated locally, ≤ 0.3 s, -18 LUFS.
- **Dock / menu-bar face** mirrors state (badge colour + eyes) — the "icon got involved" idea.
- Settings → Looks: Agents in notch, Status badge, Window glow, Greeting, Sounds.
- Guide: new clips (agents-notch, open-transition, error-retry, window-glow, poke-dizzy,
  composer), docs + index cards, deploy; README GIFs; release.

---

## 4. Engineering notes / risks
- **Performance**: keep everything Core-Animation-driven (springs, emitters, gradient
  masks) — the Python 30 Hz tick only sets targets. Budget: < 3 % CPU idle, 60 fps open.
- `layer.filters` blur on macOS needs the hosting view's `layerUsesCoreImageFilters`;
  fall back to opacity+scale if unavailable.
- `fly()` across panels (orb window ↔ notch panel) needs a transient overlay panel at
  `kCGMainMenuWindowLevelKey+3` that carries the snapshot.
- Macs without a notch: the island floats as a pill at top-centre — same system.
- `notch.py` is 2.4 k lines and shared with the Guide session: Phase 1/2 go into new
  modules (`notch_agents.py`, `kinetics.py`, `badge.py`, `window_glow.py`) and `notch.py`
  only composes them. Coordinate installs with peers as before.
- Recordings need Mint visible to capture (share_visible currently on; MINT_CAPTURE=1 for test runs; no visible tests during calls).

## 5. Order & effort
0 → 1 → 2 → 3 → 5 (window glow first inside 5, cheapest wow) → 4 → 6.
≈ 10–14 working days total; each phase demoable on its own.
