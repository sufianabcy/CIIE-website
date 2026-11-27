// KL-CIIE portal: small interactions shared by every page.
// No framework. Each block checks for its elements first, so pages only pay for what they use.
(function () {
  "use strict";
  var root = document.documentElement;
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var finePointer = window.matchMedia("(hover: hover) and (pointer: fine)").matches;

  function store(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      localStorage.setItem(key, value);
    } catch (e) { return null; }
  }

  // Theme: follows the phone or laptop setting until someone picks one.
  document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var next = root.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-bs-theme", next);
      store("theme", next);
    });
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function (e) {
    var saved = store("theme");
    if (saved !== "light" && saved !== "dark") root.setAttribute("data-bs-theme", e.matches ? "dark" : "light");
  });

  // Mobile menu: full height panel, links arrive one after another.
  var toggle = document.querySelector("[data-menu-toggle]");
  var menu = document.getElementById("mobile-menu");
  function setMenu(open) {
    document.body.classList.toggle("menu-open", open);
    if (toggle) {
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    }
    if (menu) menu.toggleAttribute("inert", !open);
  }
  if (toggle && menu) {
    setMenu(false);
    toggle.addEventListener("click", function () { setMenu(!document.body.classList.contains("menu-open")); });
    menu.addEventListener("click", function (e) { if (e.target.closest("a")) setMenu(false); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") setMenu(false); });
    window.matchMedia("(min-width: 992px)").addEventListener("change", function () { setMenu(false); });
  }

  // Toasts from Django messages leave by themselves after a few seconds.
  document.querySelectorAll(".toast-stack .alert").forEach(function (el, i) {
    if (el.classList.contains("alert-danger")) return;
    setTimeout(function () {
      el.classList.add("is-leaving");
      setTimeout(function () { el.remove(); }, 400);
    }, 6000 + i * 600);
  });

  // Buttons on normal forms show they are working, so nobody taps twice.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.hasAttribute("hx-post") || form.hasAttribute("data-no-busy") || e.defaultPrevented) return;
    var btn = e.submitter || form.querySelector("button[type=submit], button:not([type])");
    if (btn && btn.classList.contains("btn")) {
      setTimeout(function () { btn.classList.add("is-loading"); btn.setAttribute("aria-busy", "true"); }, 0);
    }
  });

  // Reveal sections once as they scroll into view.
  var revealEls = document.querySelectorAll("[data-reveal]");
  if ("IntersectionObserver" in window && revealEls.length) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { entry.target.classList.add("is-in"); io.unobserve(entry.target); }
      });
    }, { rootMargin: "0px 0px -10% 0px", threshold: 0.12 });
    revealEls.forEach(function (el) { io.observe(el); });
  } else {
    revealEls.forEach(function (el) { el.classList.add("is-in"); });
    document.querySelectorAll(".seat-bar").forEach(function (el) { el.classList.add("is-in"); });
  }

  // Seat bars and counters animate when seen.
  var countEls = document.querySelectorAll("[data-count-to], .seat-bar");
  if ("IntersectionObserver" in window && countEls.length) {
    var cio = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        var el = entry.target;
        cio.unobserve(el);
        if (el.classList.contains("seat-bar")) { el.classList.add("is-in"); return; }
        var end = parseInt(el.getAttribute("data-count-to"), 10) || 0;
        if (reduce || end < 2) { el.textContent = end.toLocaleString("en-IN"); return; }
        var start = performance.now(), dur = 1400;
        (function tick(now) {
          var p = Math.min(1, (now - start) / dur);
          var eased = 1 - Math.pow(1 - p, 4);
          el.textContent = Math.round(end * eased).toLocaleString("en-IN");
          if (p < 1) requestAnimationFrame(tick);
        })(start);
      });
    }, { threshold: 0.4 });
    countEls.forEach(function (el) { cio.observe(el); });
  }

  // 3D tilt on event cards and the pass (mouse and trackpad only).
  function tilt(el, max) {
    var raf = 0, rx = 0, ry = 0;
    el.addEventListener("pointermove", function (e) {
      if (e.pointerType !== "mouse") return;
      var r = el.getBoundingClientRect();
      var px = (e.clientX - r.left) / r.width - 0.5;
      var py = (e.clientY - r.top) / r.height - 0.5;
      rx = (-py * max).toFixed(2); ry = (px * max).toFixed(2);
      el.classList.add("is-tilting");
      if (!raf) raf = requestAnimationFrame(function () {
        raf = 0;
        el.style.setProperty("--trx", rx + "deg");
        el.style.setProperty("--try", ry + "deg");
      });
    });
    el.addEventListener("pointerleave", function () {
      el.classList.remove("is-tilting");
      el.style.setProperty("--trx", "0deg");
      el.style.setProperty("--try", "0deg");
    });
  }
  if (finePointer && !reduce) {
    document.querySelectorAll("[data-tilt]").forEach(function (el) { tilt(el, parseFloat(el.getAttribute("data-tilt")) || 8); });
  }

  // Hero stage: follows the pointer, and turns a little as you scroll past it.
  var stage = document.querySelector("[data-stage]");
  if (stage && !reduce) {
    var rig = stage.querySelector(".stage-rig");
    var target = { x: -8, y: 14 }, cur = { x: -8, y: 14 }, scrollTurn = 0, running = false;
    function loop() {
      cur.x += (target.x - cur.x) * 0.08;
      cur.y += (target.y - cur.y) * 0.08;
      rig.style.setProperty("--rx", (cur.x + scrollTurn * 10).toFixed(2) + "deg");
      rig.style.setProperty("--ry", (cur.y - scrollTurn * 24).toFixed(2) + "deg");
      if (Math.abs(target.x - cur.x) > 0.05 || Math.abs(target.y - cur.y) > 0.05) { requestAnimationFrame(loop); } else { running = false; }
    }
    function kick() { if (!running) { running = true; requestAnimationFrame(loop); } }
    var hero = stage.closest(".hero") || stage;
    if (finePointer) {
      hero.addEventListener("pointermove", function (e) {
        var px = e.clientX / window.innerWidth - 0.5;
        var py = e.clientY / window.innerHeight - 0.5;
        target.y = 14 + px * 30;
        target.x = -8 - py * 20;
        kick();
      });
      hero.addEventListener("pointerleave", function () { target.x = -8; target.y = 14; kick(); });
    } else {
      // Touch screens: a slow sway so the scene still feels alive.
      var t0 = performance.now();
      (function sway(now) {
        var t = (now - t0) / 1000;
        target.y = 14 + Math.sin(t * 0.6) * 12;
        target.x = -8 + Math.cos(t * 0.45) * 6;
        kick();
        if (document.visibilityState === "visible") requestAnimationFrame(sway);
      })(t0);
    }
    var ticking = false;
    window.addEventListener("scroll", function () {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(function () {
        var r = hero.getBoundingClientRect();
        scrollTurn = Math.max(0, Math.min(1, -r.top / Math.max(1, r.height)));
        kick();
        ticking = false;
      });
    }, { passive: true });
    kick();
  }

  // Tagline: each word lights up as it crosses a line 65% down the screen.
  var tagline = document.querySelector("[data-tagline]");
  if (tagline) {
    var words = [];
    var accent = (tagline.getAttribute("data-accent") || "").split("|");
    var accentRed = (tagline.getAttribute("data-accent-red") || "").split("|");
    var text = tagline.textContent.trim().replace(/\s+/g, " ");
    tagline.textContent = "";
    text.split(" ").forEach(function (word, i, all) {
      var span = document.createElement("span");
      var clean = word.replace(/[^\w]/g, "");
      span.className = "w" + (accent.indexOf(clean) > -1 ? " accent" : "") + (accentRed.indexOf(clean) > -1 ? " accent-red" : "");
      span.textContent = word;
      tagline.appendChild(span);
      if (i < all.length - 1) tagline.appendChild(document.createTextNode(" "));
      words.push(span);
    });
    if (reduce) {
      words.forEach(function (w) { w.classList.add("on"); });
    } else {
      var pending = false;
      var update = function () {
        pending = false;
        var line = window.innerHeight * 0.65;
        words.forEach(function (w) { w.classList.toggle("on", w.getBoundingClientRect().top < line); });
      };
      window.addEventListener("scroll", function () { if (!pending) { pending = true; requestAnimationFrame(update); } }, { passive: true });
      window.addEventListener("resize", update);
      update();
    }
  }

  // How it works: three steps drive the phone. Auto plays until someone taps a step.
  var how = document.querySelector("[data-how]");
  if (how) {
    var steps = how.querySelectorAll("[data-step]");
    var screens = how.querySelectorAll("[data-screen]");
    var current = 0, timer = null, ms = 5000, auto = !reduce, visible = false;
    how.style.setProperty("--how-ms", ms + "ms");
    var typeTarget = how.querySelector("[data-type]");
    var typeTimer = null;
    function typeIt() {
      if (!typeTarget) return;
      clearInterval(typeTimer);
      var full = typeTarget.getAttribute("data-type"), n = 0;
      if (reduce) { typeTarget.textContent = full; return; }
      typeTarget.textContent = "";
      typeTimer = setInterval(function () {
        n += 1; typeTarget.textContent = full.slice(0, n);
        if (n >= full.length) clearInterval(typeTimer);
      }, 55);
    }
    function show(i) {
      current = i;
      steps.forEach(function (s, k) {
        s.classList.toggle("is-active", k === i);
        s.setAttribute("aria-selected", k === i ? "true" : "false");
        var bar = s.querySelector(".bar");
        if (bar) { bar.style.animation = "none"; void bar.offsetWidth; bar.style.animation = ""; }
      });
      screens.forEach(function (s, k) { s.classList.toggle("is-active", k === i); s.setAttribute("aria-hidden", k === i ? "false" : "true"); });
      if (i === 0) typeIt();
      clearTimeout(timer);
      if (auto && visible) timer = setTimeout(function () { show((current + 1) % steps.length); }, ms);
    }
    steps.forEach(function (s, i) {
      s.addEventListener("click", function () { auto = false; how.classList.add("is-paused"); show(i); });
    });
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          var was = visible;
          visible = entry.isIntersecting;
          if (visible && !was) show(current);
          if (!visible) clearTimeout(timer);
        });
      }, { threshold: 0.35 }).observe(how);
    } else { visible = true; show(0); }
    if (reduce) how.classList.add("is-paused");
  }

  // The week strip picks a free evening, again and again.
  var week = document.querySelector("[data-week]");
  if (week && !reduce) {
    var days = week.querySelectorAll(".day:not(.busy)");
    var k = 0;
    setInterval(function () {
      days.forEach(function (d) { d.classList.remove("pick"); });
      days[k % days.length].classList.add("pick");
      k += 1;
    }, 1600);
  }

  // Countdown on the next registered event.
  document.querySelectorAll("[data-countdown]").forEach(function (el) {
    var at = new Date(el.getAttribute("data-countdown")).getTime();
    function paint() {
      var s = Math.max(0, Math.floor((at - Date.now()) / 1000));
      var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
      el.textContent = s === 0 ? "Happening now" : (d > 0 ? d + "d " : "") + h + "h " + (m < 10 ? "0" : "") + m + "m";
    }
    paint();
    setInterval(paint, 30000);
  });
})();

// Client side checks for forms marked data-validate: required fields and email format, shown inline.
(function () {
  "use strict";
  var emailRe = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  document.querySelectorAll("form[data-validate]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      var firstBad = null;
      form.querySelectorAll("input, textarea, select").forEach(function (input) {
        if (input.type === "hidden" || input.disabled) return;
        var msg = "";
        var value = (input.value || "").trim();
        if (input.required && !value) msg = "Please fill this in.";
        else if (input.type === "email" && value && !emailRe.test(value)) msg = "That email doesn't look right. Check for a missing @ or dot.";
        var slot = form.querySelector('[data-error-for="' + input.id + '"]');
        input.classList.toggle("is-invalid", !!msg);
        input.setAttribute("aria-invalid", msg ? "true" : "false");
        if (slot) { slot.textContent = msg; slot.hidden = !msg; }
        if (msg && !firstBad) firstBad = input;
      });
      if (firstBad) {
        e.preventDefault();
        e.stopImmediatePropagation();
        firstBad.focus();
      }
    }, true);
  });
})();

// Phone sticky bar on the event page: hide it while the real ticket box is on screen.
(function () {
  "use strict";
  var bar = document.querySelector("[data-sticky-cta]");
  if (!bar || !("IntersectionObserver" in window)) return;
  var target = document.getElementById(bar.getAttribute("data-sticky-cta"));
  if (!target) return;
  new IntersectionObserver(function (entries) {
    bar.classList.toggle("is-hidden", entries[0].isIntersecting);
  }, { threshold: 0.2 }).observe(target);
})();

// Number boxes: the mouse wheel scrolls the page, it never changes the number in a focused box.
(function () {
  "use strict";
  document.addEventListener("wheel", function (e) {
    var el = document.activeElement;
    if (el && el.type === "number" && el === e.target) el.blur();
  }, { passive: true });
})();

// Faculty marking: a mark can't go above the criterion's maximum or below 0; add up the marks as they are typed.
(function () {
  "use strict";
  var form = document.querySelector("form[data-marks]");
  if (!form) return;
  form.addEventListener("input", function (e) {
    var input = e.target;
    var row = input.closest && input.closest(".mark-field");
    if (!row || input.type !== "number" || input.value === "") return;
    var v = parseFloat(input.value), max = parseFloat(row.getAttribute("data-max"));
    var note = row.querySelector("[data-max-note]");
    if (!note) {
      note = document.createElement("div");
      note.className = "invalid-feedback d-block";
      note.setAttribute("data-max-note", "");
      row.firstElementChild.appendChild(note);
    }
    note.textContent = "";
    if (!isNaN(max) && v > max) { input.value = max; note.textContent = "The most for this is " + max + "."; }
    else if (v < 0) { input.value = 0; note.textContent = "Marks can't be below 0."; }
  }, true);
  var out = form.querySelector("[data-marks-total]");
  if (!out) return;
  var box = out.closest(".mark-total");
  function sum() {
    var total = 0, over = false;
    form.querySelectorAll(".mark-field").forEach(function (row) {
      var input = row.querySelector("input");
      var v = parseFloat(input && input.value);
      var max = parseFloat(row.getAttribute("data-max"));
      if (!isNaN(v)) { total += v; if (!isNaN(max) && v > max) over = true; }
    });
    out.textContent = Math.round(total * 100) / 100;
    if (box) box.classList.toggle("is-over", over);
  }
  form.addEventListener("input", sum);
  sum();
})();
