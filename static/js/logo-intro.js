/* Landing page intro with the KL-CIIE logo (markup: templates/events/_logo_intro.html, styles: portal.css "Logo intro").
   KL's red facets fly together, the seal rolls in, CIIE drops into place, then the whole logo flies up into the header.
   Plays once per browser session (sessionStorage "kl-intro"); /?intro=1 plays it again. A tap, click, key or scroll skips it.
   People who ask for less motion never see it (base.html only adds .is-preloading when motion is fine). */
(function () {
  "use strict";
  var doc = document, root = doc.documentElement;
  var box = doc.querySelector("[data-logo-intro]");
  if (!box) return;
  if (!root.classList.contains("is-preloading")) { box.remove(); return; }
  doc.body.appendChild(box); /* straight under <body>, so nothing inside <main> can trap the fixed overlay */

  var raf = 0, finished = false;
  var skipEvents = ["pointerdown", "keydown", "wheel", "touchstart"];

  function finish() {
    if (finished) return;
    finished = true;
    cancelAnimationFrame(raf);
    try { sessionStorage.setItem("kl-intro", "1"); } catch (e) {}
    root.classList.remove("is-preloading", "li-hide-brand");
    box.remove();
    skipEvents.forEach(function (n) { window.removeEventListener(n, skip, true); });
  }
  function skip() {
    if (finished || box.classList.contains("is-leaving")) return;
    root.classList.remove("li-hide-brand");
    box.classList.add("is-leaving");
    setTimeout(finish, 320);
  }
  /* Never leave the landing page locked behind the intro: any error simply ends it. */
  function fail(err) {
    if (window.console) console.error("logo intro:", err);
    finish();
  }

  /* On a very slow connection the page has kept people waiting already, so skip straight to it. */
  if (performance.now() > 4000) { finish(); return; }
  try { play(); } catch (err) { fail(err); }

  function play() {
    var svg = box.querySelector(".li-svg"), panel = box.querySelector(".li-panel"), hint = box.querySelector(".li-hint");
    var $ = function (s) { return svg.querySelector(s); };
    var $$ = function (s) { return Array.prototype.slice.call(svg.querySelectorAll(s)); };
    var bottom = $(".li-bottom"), sealG = $(".li-seal-g"), seal = $(".li-seal"), base = $(".li-kl-base"), shine = $(".li-shine");
    var facets = $$(".li-f"), letters = $$(".li-ch"), deemed = $(".li-deemed"), sub = $(".li-sub");

    var W = window.innerWidth, H = window.innerHeight;
    var stacked = H > W * 1.05; /* phones held upright: CIIE sits under KL so both stay big */

    /* ---------- easing ---------- */
    var ease = {
      outCubic: function (t) { return 1 - Math.pow(1 - t, 3); },
      inOutCubic: function (t) { return t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; },
      inOutQuart: function (t) { return t < .5 ? 8 * t * t * t * t : 1 - Math.pow(-2 * t + 2, 4) / 2; },
      outExpo: function (t) { return t === 1 ? 1 : 1 - Math.pow(2, -10 * t); },
      outBack: function (t) { var c = 1.3, c3 = c + 1; return 1 + c3 * Math.pow(t - 1, 3) + c * Math.pow(t - 1, 2); }
    };
    var lerp = function (a, b, t) { return a + (b - a) * t; };
    var seed = 7; /* the same "random" scatter on every visit */
    var rand = function () { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };

    /* ---------- a tiny timeline: tweens and one-off calls, driven by requestAnimationFrame ---------- */
    var tweens = [], calls = [], total = 0, t0 = 0;
    function tween(at, dur, fn, ez) { tweens.push({ at: at, dur: dur, fn: fn, ez: ez || ease.outCubic, done: false }); total = Math.max(total, at + dur); }
    function call(at, fn) { calls.push({ at: at, fn: fn, done: false }); total = Math.max(total, at); }
    function tick() {
      if (finished) return;
      var now = performance.now(); /* not the rAF timestamp, which can lag behind when the page is busy loading */
      if (!t0) { t0 = now; box.setAttribute("data-playing", ""); } /* first frame on screen: the CSS failsafe counts from here */
      var t = now - t0;
      try {
        calls.forEach(function (c) { if (!c.done && t >= c.at) { c.done = true; c.fn(); } });
        tweens.forEach(function (w) {
          if (w.done || t < w.at) return;
          var p = w.dur ? Math.min(1, (t - w.at) / w.dur) : 1;
          w.fn(w.ez(p), p);
          if (p >= 1) w.done = true;
        });
      } catch (err) { fail(err); return; }
      if (t >= total) { finish(); return; }
      raf = requestAnimationFrame(tick);
    }

    /* ---------- per-element transform state (SVG user units, rotation about the element's own centre) ---------- */
    function st(el) {
      if (!el._li) { var b = el.getBBox(); el._li = { tx: 0, ty: 0, r: 0, s: 1, cx: b.x + b.width / 2, cy: b.y + b.height / 2 }; }
      return el._li;
    }
    function put(el, o) {
      var k = st(el);
      for (var key in o) if (key !== "o") k[key] = o[key];
      el.setAttribute("transform", "translate(" + (k.tx + k.cx).toFixed(2) + " " + (k.ty + k.cy).toFixed(2) + ") rotate(" + k.r.toFixed(2) +
        ") scale(" + k.s.toFixed(4) + ") translate(" + (-k.cx).toFixed(2) + " " + (-k.cy).toFixed(2) + ")");
      if ("o" in o) el.style.opacity = o.o;
    }
    var fade = function (el, o) { if (el) el.style.opacity = o; };

    /* ---------- the camera: centre (cx, cy) in logo units and s = screen pixels per unit ---------- */
    var cam = { cx: 800, cy: 274, s: 1 };
    function view(cx, cy, s) {
      cam.cx = cx; cam.cy = cy; cam.s = s;
      var w = W / s, h = H / s;
      svg.setAttribute("viewBox", (cx - w / 2).toFixed(3) + " " + (cy - h / 2).toFixed(3) + " " + w.toFixed(3) + " " + h.toFixed(3));
    }
    /* Move the camera so that one anchor point (by default the target's centre) travels in a straight line on screen
       while the zoom changes evenly; moving the centre in logo units instead makes the logo swing off on a curve. */
    function camTo(at, dur, to, ez, anchor) {
      var from = null;
      tween(at, dur, function (e) {
        if (!from) {
          var a = anchor || { x: to.cx, y: to.cy };
          from = {
            ls: Math.log(cam.s), ax: a.x, ay: a.y,
            x0: W / 2 + (a.x - cam.cx) * cam.s, y0: H / 2 + (a.y - cam.cy) * cam.s,
            x1: W / 2 + (a.x - to.cx) * to.s, y1: H / 2 + (a.y - to.cy) * to.s
          };
        }
        var s = Math.exp(lerp(from.ls, Math.log(to.s), e));
        view(from.ax - (lerp(from.x0, from.x1, e) - W / 2) / s, from.ay - (lerp(from.y0, from.y1, e) - H / 2) / s, s);
      }, ez);
    }
    function union(els) {
      var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      els.forEach(function (el) {
        var b = el.getBBox();
        x0 = Math.min(x0, b.x); y0 = Math.min(y0, b.y); x1 = Math.max(x1, b.x + b.width); y1 = Math.max(y1, b.y + b.height);
      });
      return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
    }

    /* ---------- layout: one row (the official lockup) or stacked on tall screens ---------- */
    var topBox = union([seal, base, deemed]);
    var botBox0 = union(letters.concat([sub]));
    var shift = { x: 0, y: 0 };
    if (stacked) {
      shift.x = (topBox.x + topBox.w / 2) - (botBox0.x + botBox0.w / 2);
      shift.y = topBox.y + topBox.h + 46 - botBox0.y;
    }
    var setShift = function (x, y) { bottom.setAttribute("transform", "translate(" + x.toFixed(2) + " " + y.toFixed(2) + ")"); };
    setShift(shift.x, shift.y);
    var botBox = { x: botBox0.x + shift.x, y: botBox0.y + shift.y, w: botBox0.w, h: botBox0.h };
    var all = {
      x: Math.min(topBox.x, botBox.x), y: Math.min(topBox.y, botBox.y),
      w: Math.max(topBox.x + topBox.w, botBox.x + botBox.w) - Math.min(topBox.x, botBox.x),
      h: Math.max(topBox.y + topBox.h, botBox.y + botBox.h) - Math.min(topBox.y, botBox.y)
    };
    var fitS = Math.min(W * (stacked ? .86 : .88) / all.w, H * (stacked ? .62 : .7) / all.h, 1.05);
    var fit = { cx: all.x + all.w / 2, cy: all.y + all.h / 2, s: fitS };
    var klBox = union([base]);
    var panelRGB = (getComputedStyle(panel).backgroundColor.match(/\d+/g) || [0, 48, 73]).slice(0, 3).join(",");
    var panelAlpha = function (a) { panel.style.backgroundColor = "rgba(" + panelRGB + "," + a + ")"; };

    /* ---------- 1. KL's facets fly in from all sides and lock together ---------- */
    root.classList.add("li-hide-brand"); /* the header logo waits until the big one lands on it */
    var klc = { x: klBox.x + klBox.w / 2, y: klBox.y + klBox.h / 2 };
    view(klc.x, klc.y, Math.min(W * .72 / klBox.w, H * .55 / klBox.h, 2.4));
    fade(base, 0); fade(shine, 0); fade(deemed, 0); fade(sub, 0);
    var order = facets.map(function (f, i) { return i; }).sort(function () { return rand() - .5; });
    order.forEach(function (fi, n) {
      var f = facets[fi], k = st(f);
      var a = Math.atan2(k.cy - klc.y, k.cx - klc.x) + (rand() - .5) * 1.1;
      var d = 650 + rand() * 520;
      var from = { tx: Math.cos(a) * d, ty: Math.sin(a) * d, r: (rand() < .5 ? -1 : 1) * (140 + rand() * 220), s: .35 + rand() * .5 };
      put(f, { tx: from.tx, ty: from.ty, r: from.r, s: from.s, o: 0 });
      tween(90 + n * 42, 880, function (e, p) {
        put(f, { tx: from.tx * (1 - e), ty: from.ty * (1 - e), r: from.r * (1 - ease.outCubic(p)), s: lerp(from.s, 1, ease.outCubic(p)), o: Math.min(1, p * 3.5) });
      }, ease.outExpo);
    });
    var landed = 90 + facets.length * 42 + 880;
    call(landed - 40, function () { fade(base, 1); }); /* the solid KL under the facets hides any hairline seams */
    tween(landed, 640, function (e) { shine.setAttribute("x", lerp(klBox.x - 160, klBox.x + klBox.w + 20, e).toFixed(1)); fade(shine, Math.sin(Math.PI * e)); }, ease.inOutCubic);

    /* ---------- 2. the seal rolls in from the left ---------- */
    var roll = -1150;
    put(sealG, { tx: roll, r: roll / (Math.PI * seal.getBBox().height) * 360, o: 1 });
    tween(860, 1150, function (e) { put(sealG, { tx: roll * (1 - e), r: roll * (1 - e) / (Math.PI * seal.getBBox().height) * 360 }); }, ease.outCubic);

    /* ---------- 3. pull back to the whole logo; CIIE drops in, then the two text lines ---------- */
    camTo(1080, 1050, fit, ease.inOutCubic);
    letters.forEach(function (l, i) {
      put(l, { ty: -360 });
      tween(1320 + i * 90, 720, function (e) { put(l, { ty: -360 * (1 - e) }); }, ease.outBack);
    });
    [deemed, sub].forEach(function (el, i) {
      put(el, { ty: 18 });
      tween(1950 + i * 90, 480, function (e) { put(el, { ty: 18 * (1 - e), o: e }); });
    });

    /* ---------- 4. fly into the logo in the header ---------- */
    var dockAt = 2900;
    call(dockAt, function () {
      var img = Array.prototype.filter.call(doc.querySelectorAll(".site-header .brand img"), function (i) { return getComputedStyle(i).display !== "none"; })[0];
      var r = img && img.getBoundingClientRect();
      var mark = union([seal, base].concat(letters)); /* the header mark: seal, KL and CIIE in one row */
      var light = root.getAttribute("data-bs-theme") !== "dark";
      fade(hint, 0);
      tween(dockAt, 260, function (e) { fade(deemed, 1 - e); fade(sub, 1 - e); });
      tween(dockAt, 620, function (e) { panelAlpha(1 - e); }, ease.inOutCubic);
      tween(dockAt, 640, function (e) { setShift(shift.x * (1 - e), shift.y * (1 - e)); }, ease.inOutCubic);
      if (light) tween(dockAt + 120, 520, function (e) { /* CIIE turns from white to the header logo's blue */
        var c = [Math.round(lerp(255, 51, e)), Math.round(lerp(255, 80, e)), Math.round(lerp(255, 220, e))];
        letters.forEach(function (l) { l.style.fill = "rgb(" + c.join(",") + ")"; });
      });
      if (r && r.width > 4 && r.bottom > 0) {
        var s = r.height / mark.h;
        var to = { s: s, cx: mark.x + (W / 2 - r.left) / s, cy: mark.y + (H / 2 - r.top) / s };
        camTo(dockAt, 820, to, ease.inOutQuart, { x: mark.x + mark.w / 2, y: mark.y + mark.h / 2 });
      } else {
        tween(dockAt, 600, function (e) { svg.style.opacity = 1 - e; }); /* no header logo in view: just fade */
      }
      call(dockAt + 840, function () { root.classList.remove("li-hide-brand"); });
      tween(dockAt + 840, 160, function (e) { svg.style.opacity = 1 - e; });
    });
    total = Math.max(total, dockAt + 1010);

    /* ---------- go ---------- */
    box.classList.add("is-ready");
    skipEvents.forEach(function (n) { window.addEventListener(n, skip, true); });
    var go = function () { raf = requestAnimationFrame(tick); };
    /* give the seal image a moment to arrive, but never hold the page for it */
    if (seal.href && seal.href.baseVal && typeof Image !== "undefined") {
      var im = new Image(), started = false;
      var once = function () { if (!started) { started = true; go(); } };
      im.onload = once; im.onerror = once;
      im.src = seal.href.baseVal;
      setTimeout(once, 450);
    } else {
      go();
    }
  }
})();
