/* Admin side menu (templates/admin/nav_sidebar.html):
   - "Find a page" box: filters the menu as you type (group names count too), Enter opens the first match,
     and "/" jumps to it from anywhere on the page.
   - Phones: the Menu button in the header opens the menu as a drawer; tap outside or press Escape to close. */
(function () {
  "use strict";
  var root = document.documentElement;
  var nav = document.getElementById("nav-sidebar");
  var menuBtn = document.querySelector("[data-kl-menu]");
  if (!nav) return;
  var phone = window.matchMedia("(max-width: 767px)");

  function setOpen(open) {
    root.classList.toggle("kl-nav-open", open);
    if (menuBtn) menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (menuBtn) {
    menuBtn.hidden = false;
    menuBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      setOpen(!root.classList.contains("kl-nav-open"));
    });
  }
  document.addEventListener("click", function (e) {
    if (root.classList.contains("kl-nav-open") && !nav.contains(e.target)) setOpen(false);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && root.classList.contains("kl-nav-open")) {
      setOpen(false);
      if (menuBtn) menuBtn.focus();
    }
  });
  var onWidth = function () { if (!phone.matches) setOpen(false); };
  if (phone.addEventListener) phone.addEventListener("change", onWidth);

  // Keep the page you're on in view in a long menu.
  var current = nav.querySelector(".kl-nav-item.is-current");
  if (current && !phone.matches && current.offsetTop > nav.clientHeight - 80) {
    nav.scrollTop = current.offsetTop - nav.clientHeight / 2;
  }

  var find = nav.querySelector("[data-kl-nav-find]");
  if (!find) return;
  var items = Array.prototype.slice.call(nav.querySelectorAll("[data-kl-nav-item]"));
  var groups = Array.prototype.slice.call(nav.querySelectorAll("[data-kl-nav-group]"));
  var none = nav.querySelector("[data-kl-nav-none]");
  var norm = function (s) { return (s || "").toLowerCase().replace(/\s+/g, " ").trim(); };
  items.forEach(function (li) {
    var group = li.closest("[data-kl-nav-group]");
    var label = group ? group.querySelector(".kl-nav-label") : null;
    li.setAttribute("data-kl-words", norm(li.textContent + " " + (label ? label.textContent : "")));
  });

  function apply() {
    var q = norm(find.value);
    var any = false;
    items.forEach(function (li) {
      var hit = !q || li.getAttribute("data-kl-words").indexOf(q) !== -1;
      li.hidden = !hit;
      if (hit) any = true;
    });
    groups.forEach(function (g) {
      var shown = g.querySelector("[data-kl-nav-item]:not([hidden])");
      g.hidden = !shown;
      if (q && shown && g.tagName === "DETAILS") g.open = true;
    });
    if (none) none.hidden = any || !q;
  }
  find.addEventListener("input", apply);
  find.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && find.value.trim()) {
      var first = nav.querySelector("[data-kl-nav-item]:not([hidden]) a");
      if (first) { e.preventDefault(); window.location.href = first.href; }
    } else if (e.key === "Escape" && find.value) {
      e.stopPropagation();
      find.value = "";
      apply();
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
    var t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    e.preventDefault();
    if (phone.matches) {
      setOpen(true);
    } else {
      var main = document.getElementById("main");
      var toggle = document.getElementById("toggle-nav-sidebar");
      if (main && toggle && !main.classList.contains("shifted")) toggle.click();
    }
    find.focus();
    find.select();
  });
})();
