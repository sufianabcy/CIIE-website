// Door screen: one box for camera, handheld scanner, typed code or name.
(function () {
  "use strict";
  var form = document.getElementById("scan-form");
  var box = document.getElementById("q");
  var gate = document.getElementById("gate");
  var gateField = document.getElementById("gate-field");
  var result = document.getElementById("result");
  var dismissTimer = null;

  try { gate.value = gate.value || localStorage.getItem("door-gate") || ""; } catch (e) {}
  gateField.value = gate.value;
  gate.addEventListener("input", function () {
    gateField.value = gate.value;
    try { localStorage.setItem("door-gate", gate.value); } catch (e) {}
  });

  function dismiss() {
    clearTimeout(dismissTimer);
    result.innerHTML = "";
    box.value = "";
    box.focus();
  }

  document.body.addEventListener("htmx:afterSwap", function (evt) {
    if (evt.detail.target !== result) return;
    var panel = result.querySelector("[data-tone]");
    if (!panel) { return; }
    var tone = panel.getAttribute("data-tone");
    if (navigator.vibrate) {
      navigator.vibrate(tone === "ok" ? 120 : [200, 100, 200]);
    }
    panel.focus();
    clearTimeout(dismissTimer);
    // Green clears itself so the line keeps moving; anything else waits for a tap.
    if (tone === "ok") { dismissTimer = setTimeout(dismiss, 2500); }
  });

  document.body.addEventListener("click", function (evt) {
    if (evt.target.closest("[data-dismiss-result]")) { dismiss(); }
  });
  document.addEventListener("keydown", function (evt) {
    if (result.querySelector("[data-tone]") && (evt.key === "Enter" || evt.key === "Escape")) {
      evt.preventDefault();
      dismiss();
    }
  });
  document.body.addEventListener("htmx:afterRequest", function (evt) {
    if (evt.detail.elt === form) { box.value = ""; }
  });
  document.body.addEventListener("htmx:sendError", function () {
    result.innerHTML = '<div class="door-result door-stop" data-tone="stop" tabindex="-1"><div class="door-word">No connection</div>' +
      '<div class="door-detail">Check them against the printed door list and carry on.</div>' +
      '<div class="door-actions"><button type="button" class="btn btn-light btn-lg" data-dismiss-result>Next</button></div></div>';
  });

  // Camera: the browser's own barcode detector where it exists, jsQR elsewhere (iPhone).
  var cameraBtn = document.getElementById("camera-btn");
  var wrap = document.getElementById("camera-wrap");
  var video = document.getElementById("camera");
  var canvas = document.getElementById("camera-canvas");
  var stream = null;
  var detector = null;
  var lastCode = "";
  var lastAt = 0;

  function submitCode(code) {
    var now = Date.now();
    if (code === lastCode && now - lastAt < 4000) return; // same pass still in view
    if (result.querySelector("[data-tone]")) return; // wait until the last result is cleared
    lastCode = code; lastAt = now;
    box.value = code;
    htmx.trigger(form, "submit");
  }

  function scanFrame() {
    if (!stream) return;
    if (video.readyState >= 2) {
      if (detector) {
        detector.detect(video).then(function (codes) {
          if (codes.length) submitCode(codes[0].rawValue);
        }).catch(function () {});
      } else if (window.jsQR) {
        var w = video.videoWidth, h = video.videoHeight;
        var scale = Math.min(1, 640 / Math.max(w, h));
        canvas.width = Math.round(w * scale); canvas.height = Math.round(h * scale);
        var ctx = canvas.getContext("2d", { willReadFrequently: true });
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        var img = ctx.getImageData(0, 0, canvas.width, canvas.height);
        var found = window.jsQR(img.data, img.width, img.height, { inversionAttempts: "dontInvert" });
        if (found && found.data) submitCode(found.data);
      }
    }
    setTimeout(function () { requestAnimationFrame(scanFrame); }, 150);
  }

  cameraBtn.addEventListener("click", function () {
    if (stream) {
      stream.getTracks().forEach(function (t) { t.stop(); });
      stream = null; wrap.hidden = true; cameraBtn.textContent = "Use camera"; box.focus();
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      alert("This browser cannot use the camera. Type the code or use a handheld scanner.");
      return;
    }
    if ("BarcodeDetector" in window) {
      try { detector = new window.BarcodeDetector({ formats: ["qr_code"] }); } catch (e) { detector = null; }
    }
    navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false }).then(function (s) {
      stream = s; video.srcObject = s; video.play();
      wrap.hidden = false; cameraBtn.textContent = "Stop camera";
      scanFrame();
    }).catch(function () {
      alert("Camera permission was refused. Type the code or use a handheld scanner.");
    });
  });
})();
