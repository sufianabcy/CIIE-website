// KL-CIIE portal motion layer (added 25 Nov).
// Pieces taken from the references Sufian picked: text roll, magnetic buttons, sliding numbers and
// spinning text (motion-primitives), background paths (kokonutui), scrambled labels and a live clock
// (2xa.studio), tapes that speed up with the scroll, and one shape that morphs through the whole event
// journey (prompt-motion.com). Smooth scrolling is Lenis and the path drawing is anime.js; both are
// vendored in static/vendor. The landing page's logo intro is separate, in static/js/logo-intro.js.
// Every piece checks for its elements first, stays still when the visitor asks for less motion,
// and stops working when it is off screen.
(function () {
  "use strict";
  var doc = document, root = doc.documentElement;
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var fine = window.matchMedia("(hover: hover) and (pointer: fine)").matches;
  var A = window.anime || null;
  var hasIO = "IntersectionObserver" in window;

  function onView(el, cb, opts) {
    if (!hasIO) { cb(true); return; }
    new IntersectionObserver(function (entries) { entries.forEach(function (e) { cb(e.isIntersecting, e); }); }, opts || {}).observe(el);
  }

  // Scroll speed shared by the tapes and the badge.
  var scrollV = 0, lastY = window.scrollY;
  function sampleScroll() {
    var y = window.scrollY, dy = y - lastY;
    lastY = y;
    scrollV += (Math.max(-80, Math.min(80, dy)) - scrollV) * 0.12;
    return scrollV;
  }

  // 1. Text roll on links and buttons (mouse and trackpad only).
  function roll(el) {
    if (el.getAttribute("data-rolled") || el.closest("[data-no-roll]")) return;
    var tn = null;
    for (var i = 0; i < el.childNodes.length; i++) {
      var n = el.childNodes[i];
      if (n.nodeType === 3 && n.nodeValue.trim()) { tn = n; break; }
    }
    if (!tn) return;
    var text = tn.nodeValue.trim();
    if (text.length > 32) return;
    var wrap = doc.createElement("span");
    wrap.className = "roll";
    wrap.setAttribute("aria-hidden", "true");
    ["roll-a", "roll-b"].forEach(function (cls) {
      var line = doc.createElement("span");
      line.className = cls;
      Array.prototype.forEach.call(text, function (ch, k) {
        var c = doc.createElement("span");
        c.textContent = ch;
        c.style.setProperty("--ci", k);
        line.appendChild(c);
      });
      wrap.appendChild(line);
    });
    var sr = doc.createElement("span");
    sr.className = "visually-hidden";
    sr.textContent = text;
    var lead = tn.nodeValue.match(/^\s*/)[0], trail = tn.nodeValue.match(/\s*$/)[0];
    var frag = doc.createDocumentFragment();
    if (lead) frag.appendChild(doc.createTextNode(" "));
    frag.appendChild(wrap);
    frag.appendChild(sr);
    if (trail) frag.appendChild(doc.createTextNode(" "));
    el.replaceChild(frag, tn);
    el.setAttribute("data-rolled", "1");
    el.classList.add("has-roll");
  }
  if (fine && !reduce) {
    doc.querySelectorAll(".nav-links a, .site-footer a, .text-link, .btn-lg, .hero-cta .btn, .section-head .btn, .cta-block .btn, .next-strip .btn, [data-roll]").forEach(roll);
  }

  // 2. Headings rise word by word as they come into view.
  function splitWords(el) {
    var i = 0;
    (function walk(node) {
      Array.prototype.slice.call(node.childNodes).forEach(function (n) {
        if (n.nodeType === 3) {
          var frag = doc.createDocumentFragment();
          n.nodeValue.split(/(\s+)/).forEach(function (part) {
            if (!part) return;
            if (/^\s+$/.test(part)) { frag.appendChild(doc.createTextNode(" ")); return; }
            var outer = doc.createElement("span"), inner = doc.createElement("span");
            outer.className = "sw"; inner.className = "si";
            inner.style.setProperty("--wi", i++);
            inner.textContent = part;
            outer.appendChild(inner);
            frag.appendChild(outer);
          });
          node.replaceChild(frag, n);
        } else if (n.nodeType === 1 && !/^(SVG|IMG|BR|SUP)$/i.test(n.nodeName)) {
          walk(n);
        }
      });
    })(el);
  }
  doc.querySelectorAll("[data-split], .section-head h2, .page-head h1, .event-hero h1").forEach(function (el) {
    if (reduce || el.querySelector(".sw")) return;
    el.setAttribute("data-split", "");
    splitWords(el);
    onView(el, function (inView) { if (inView) el.classList.add("is-split-in"); }, { rootMargin: "0px 0px -8% 0px" });
  });
  doc.querySelectorAll(".hero-title .li").forEach(function (el, i) { el.style.setProperty("--li", i); });

  // 3. Small labels scramble into place (2xa.studio).
  var glyphs = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789#%&+=/<>";
  function scramble(node) {
    var finalText = node.nodeValue, start = performance.now(), dur = 650;
    (function frame(now) {
      var p = Math.min(1, (now - start) / dur), shown = Math.floor(p * finalText.length), out = "";
      for (var k = 0; k < finalText.length; k++) {
        var ch = finalText[k];
        out += k < shown || /\s/.test(ch) ? ch : glyphs[(Math.random() * glyphs.length) | 0];
      }
      node.nodeValue = p < 1 ? out : finalText;
      if (p < 1) requestAnimationFrame(frame);
    })(start);
  }
  if (!reduce) {
    doc.querySelectorAll("[data-scramble], .section-head .eyebrow, .page-head .eyebrow").forEach(function (el) {
      onView(el, function (inView) {
        if (!inView || el.getAttribute("data-scrambled")) return;
        el.setAttribute("data-scrambled", "1");
        Array.prototype.forEach.call(el.childNodes, function (n) { if (n.nodeType === 3 && n.nodeValue.trim()) scramble(n); });
      }, { threshold: 0.6 });
    });
  }

  // 4. Magnetic buttons lean toward the pointer.
  if (fine && !reduce) {
    doc.querySelectorAll("[data-magnetic]").forEach(function (el) {
      var pull = parseFloat(el.getAttribute("data-magnetic")) || 0.3;
      el.addEventListener("pointermove", function (e) {
        var r = el.getBoundingClientRect();
        var x = (e.clientX - (r.left + r.width / 2)) * pull, y = (e.clientY - (r.top + r.height / 2)) * pull;
        el.style.translate = x.toFixed(1) + "px " + y.toFixed(1) + "px";
      });
      el.addEventListener("pointerleave", function () { el.style.translate = ""; });
    });
  }

  // 5. Tapes: keep moving, faster while you scroll, and turn round when you scroll back up.
  doc.querySelectorAll("[data-tape]").forEach(function (tape) {
    var track = tape.querySelector(".marquee-track");
    if (!track || reduce) return;
    var dir = tape.getAttribute("data-tape") === "right" ? 1 : -1;
    var x = 0, half = 0, visible = false, raf = 0, flip = 1;
    function measure() { half = track.scrollWidth / 2; if (dir > 0) x = -half / 2; }
    function frame() {
      var v = sampleScroll();
      if (v < -1.5) flip = -1; else if (v > 1.5) flip = 1;
      x += (0.55 + Math.abs(v) * 0.35) * dir * flip;
      if (half) { if (x <= -half) x += half; if (x > 0) x -= half; }
      track.style.transform = "translate3d(" + x.toFixed(2) + "px,0,0)";
      raf = visible && !doc.hidden ? requestAnimationFrame(frame) : 0;
    }
    onView(tape, function (inView) {
      visible = inView;
      if (visible && !raf) { if (!half) measure(); raf = requestAnimationFrame(frame); }
    });
    window.addEventListener("resize", measure);
    doc.addEventListener("visibilitychange", function () { if (!doc.hidden && visible && !raf) raf = requestAnimationFrame(frame); });
  });

  // 6. Spinning badge turns slowly, and faster with the scroll.
  doc.querySelectorAll("[data-spin]").forEach(function (el) {
    if (reduce) return;
    var angle = 0, visible = false, raf = 0;
    function frame() {
      angle = (angle + 0.12 + Math.abs(sampleScroll()) * 0.18) % 360;
      el.style.transform = "rotate(" + angle.toFixed(2) + "deg)";
      raf = visible && !doc.hidden ? requestAnimationFrame(frame) : 0;
    }
    onView(el, function (inView) { visible = inView; if (visible && !raf) raf = requestAnimationFrame(frame); });
  });

  // 7. Countdowns with sliding digits (motion-primitives Sliding Number).
  function digitEl() {
    var d = doc.createElement("span"), strip = doc.createElement("span");
    d.className = "digit";
    for (var k = 0; k < 10; k++) { var s = doc.createElement("span"); s.textContent = k; strip.appendChild(s); }
    d.appendChild(strip);
    return d;
  }
  doc.querySelectorAll("[data-slide-countdown]").forEach(function (el) {
    var at = new Date(el.getAttribute("data-slide-countdown")).getTime();
    if (isNaN(at)) return;
    var units = [["d", 86400], ["h", 3600], ["m", 60], ["s", 1]], cells = {};
    el.textContent = "";
    units.forEach(function (u, i) {
      var box = doc.createElement("span"), label = doc.createElement("small");
      box.className = "cd-unit";
      cells[u[0]] = [digitEl(), digitEl()];
      box.appendChild(cells[u[0]][0]); box.appendChild(cells[u[0]][1]);
      label.textContent = u[0];
      box.appendChild(label);
      el.appendChild(box);
      if (i < units.length - 1) { var sep = doc.createElement("span"); sep.className = "cd-sep"; sep.textContent = ":"; el.appendChild(sep); }
    });
    function set(cell, n) { cell.firstChild.style.transform = "translateY(" + (-n * 10) + "%)"; }
    var host = el.closest(".hero-count, .next-strip"), timer = 0;
    function paint() {
      var left = Math.max(0, Math.floor((at - Date.now()) / 1000));
      if (!left) {
        el.classList.add("is-done");
        if (host) host.classList.add("is-done");
        if (timer) clearInterval(timer);
      }
      units.forEach(function (u) {
        var v = Math.floor(left / u[1]); left -= v * u[1];
        v = Math.min(v, 99);
        set(cells[u[0]][0], Math.floor(v / 10)); set(cells[u[0]][1], v % 10);
      });
    }
    paint();
    if (!el.classList.contains("is-done")) timer = setInterval(paint, 1000);
  });

  // 8. Hero background paths draw themselves in, then light travels along a few of them.
  var paths = doc.querySelector("[data-paths]");
  if (paths && A && A.createDrawable && !reduce) {
    var base = A.createDrawable(paths.querySelectorAll("path:not(.comet)"));
    A.animate(base, { draw: ["0 0", "0 1"], duration: 2200, delay: A.stagger(90, { start: 300 }), ease: "inOutQuad" });
    var comets = A.createDrawable(paths.querySelectorAll(".comet"));
    var flow = A.animate(comets, { draw: ["0 0", "0 .12", ".88 1", "1 1"], duration: 5200, delay: A.stagger(700, { start: 1800 }), loop: true, ease: "inOutSine" });
    onView(paths, function (inView) { if (inView) flow.play(); else flow.pause(); });
  }

  // 9. One shape morphs through the whole journey: register, wait, done, pass, door, certificate.
  // Every style is worked out from the time alone, with closed form springs (a value that changes
  // target several times is the sum of one spring per change), so the loop never drifts.
  var stage = doc.querySelector("[data-morph]");
  if (stage) {
    var scene = stage.querySelector(".morph-scene"), shape = stage.querySelector(".morph-shape");
    var cursor = stage.querySelector(".morph-cursor"), caption = stage.querySelector("[data-morph-caption]");
    var layers = Array.prototype.slice.call(stage.querySelectorAll(".m-c"));
    var T = 12.4, W = 2 * Math.PI * 1.6, Z = 0.78;
    var crimson = [193, 18, 31], cream = [253, 240, 213], sky = [102, 155, 188];
    // [time, width, height, radius, colour, caption]
    var states = [
      [0.0, 210, 60, 30, crimson, "Register"],
      [1.35, 60, 60, 30, crimson, "Registering"],
      [2.35, 60, 60, 30, sky, "Done"],
      [3.05, 300, 60, 30, cream, "You're in"],
      [4.55, 224, 288, 22, cream, "Your pass"],
      [7.05, 286, 60, 30, sky, "At the door"],
      [8.55, 268, 168, 20, crimson, "Winners"],
      [10.75, 210, 60, 30, crimson, "Register"]
    ];
    function step(t) {
      if (t <= 0) return 0;
      var wd = W * Math.sqrt(1 - Z * Z);
      return 1 - Math.exp(-Z * W * t) * (Math.cos(wd * t) + (Z * W / wd) * Math.sin(wd * t));
    }
    function track(t, pick) {
      var v = pick(states[0]);
      for (var i = 1; i < states.length; i++) v += (pick(states[i]) - pick(states[i - 1])) * step(t - states[i][0]);
      return v;
    }
    function windowed(t, from, to, fadeIn, fadeOut) {
      if (t < from || t > to) return 0;
      return Math.min(1, (t - from) / fadeIn, (to - t) / fadeOut);
    }
    // the cursor walks in, clicks Register, and leaves
    var cursorKeys = [[0, 300, 250], [0.25, 300, 250], [1.0, 186, 166], [1.5, 186, 166], [2.1, 320, 290]];
    function cursorAt(t) {
      var x = cursorKeys[0][1], y = cursorKeys[0][2];
      for (var i = 1; i < cursorKeys.length; i++) {
        var s = step((t - cursorKeys[i][0]) * 1.2);
        x += (cursorKeys[i][1] - cursorKeys[i - 1][1]) * s;
        y += (cursorKeys[i][2] - cursorKeys[i - 1][2]) * s;
      }
      return [x, y];
    }
    function seek(t) {
      var w = track(t, function (s) { return s[1]; }), h = track(t, function (s) { return s[2]; });
      var r = track(t, function (s) { return s[3]; });
      var rgb = [0, 1, 2].map(function (c) { return Math.round(Math.max(0, Math.min(255, track(t, function (s) { return s[4][c]; })))); });
      var press = 1 - 0.06 * Math.max(0, 1 - Math.abs(t - 1.22) / 0.12);
      shape.style.width = w.toFixed(1) + "px";
      shape.style.height = h.toFixed(1) + "px";
      shape.style.borderRadius = Math.max(0, r).toFixed(1) + "px";
      shape.style.background = "rgb(" + rgb.join(",") + ")";
      shape.style.transform = "translate(-50%, -50%) scale(" + press.toFixed(3) + ")";
      layers.forEach(function (layer, i) {
        var from = states[i][0] + 0.16, to = (i + 1 < states.length ? states[i + 1][0] : T) - 0.04;
        if (i === layers.length - 1) { from = states[i][0] + 0.16; to = T + 1; }
        var o = windowed(t, from, to, 0.22, 0.12);
        if (i === 0) o = Math.max(o, windowed(t, -1, states[1][0] - 0.04, 1, 0.12));
        layer.style.opacity = o.toFixed(3);
        layer.style.filter = o < 1 ? "blur(" + ((1 - o) * 6).toFixed(1) + "px)" : "none";
      });
      if (cursor) {
        var c = cursorAt(t), co = windowed(t, 0.15, 2.0, 0.3, 0.4);
        cursor.style.opacity = co.toFixed(3);
        cursor.style.transform = "translate(" + c[0].toFixed(1) + "px," + c[1].toFixed(1) + "px) scale(" + (t > 1.12 && t < 1.32 ? 0.86 : 1) + ")";
      }
      if (caption) {
        var label = states[0][5];
        for (var i = 0; i < states.length; i++) if (t >= states[i][0]) label = states[i][5];
        if (caption.textContent !== label) caption.textContent = label;
      }
    }
    function fit() {
      var k = Math.min(1, stage.clientWidth / 360, stage.clientHeight / 320);
      scene.style.setProperty("--k", k.toFixed(3));
    }
    fit();
    window.addEventListener("resize", fit);
    if (reduce) {
      seek(5.6);
    } else {
      var t0 = performance.now(), running = false, visible = false;
      var loop = function (now) {
        seek(((now - t0) / 1000) % T);
        if (visible && !doc.hidden) requestAnimationFrame(loop); else running = false;
      };
      var start = function () { if (!running && visible && !doc.hidden) { running = true; requestAnimationFrame(loop); } };
      onView(stage, function (inView) { visible = inView; start(); }, { threshold: 0.2 });
      doc.addEventListener("visibilitychange", start);
      seek(0);
    }
  }

  // 10. Live clock in India time (2xa.studio keeps two city clocks in its header).
  var clock = doc.querySelector("[data-clock]");
  if (clock && window.Intl) {
    var fmt = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
    var tickClock = function () { clock.textContent = fmt.format(new Date()); };
    tickClock();
    setInterval(tickClock, 1000);
  }

  // 11. Footer name slides up letter by letter.
  doc.querySelectorAll(".footer-mark").forEach(function (el) {
    Array.prototype.forEach.call(el.querySelectorAll("span"), function (s, i) { s.style.setProperty("--fi", i); });
    onView(el, function (inView) { if (inView) el.classList.add("is-in"); }, { threshold: 0.2 });
  });

  // 12. Reading bar along the top of long pages.
  var bar = doc.querySelector("[data-progress]");
  if (bar && !reduce) {
    var pending = false;
    var paintBar = function () {
      pending = false;
      var max = root.scrollHeight - window.innerHeight;
      bar.style.transform = "scaleX(" + (max > 0 ? Math.min(1, window.scrollY / max) : 0).toFixed(4) + ")";
    };
    window.addEventListener("scroll", function () { if (!pending) { pending = true; requestAnimationFrame(paintBar); } }, { passive: true });
    paintBar();
  }

  // 13. Smooth scrolling on the public pages, for mouse and trackpad users.
  if (window.Lenis && fine && !reduce && doc.body.hasAttribute("data-smooth")) {
    var lenis = new window.Lenis({ lerp: 0.11, anchors: { offset: -96 } });
    var rafLenis = function (time) { lenis.raf(time); requestAnimationFrame(rafLenis); };
    requestAnimationFrame(rafLenis);
    window.klLenis = lenis;
  }
})();
