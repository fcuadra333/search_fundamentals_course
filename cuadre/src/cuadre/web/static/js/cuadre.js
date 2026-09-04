/* Cuadre — the little that needs the browser.
   No framework, no CDN: the app has to work on a client's machine with no
   internet, and every dependency is one more thing to break there. */
(function () {
  "use strict";

  // Theme: follows the system unless the user says otherwise.
  var KEY = "cuadre-theme";
  try {
    var saved = localStorage.getItem(KEY);
    if (saved) document.documentElement.setAttribute("data-theme", saved);
  } catch (e) { /* private window: keep the system theme */ }

  document.addEventListener("click", function (event) {
    var toggle = event.target.closest("[data-theme-toggle]");
    if (!toggle) return;
    var root = document.documentElement;
    var current = root.getAttribute("data-theme");
    if (!current) {
      current = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    var next = current === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem(KEY, next); } catch (e) { /* ignore */ }
  });

  // Destructive actions ask once.
  document.addEventListener("submit", function (event) {
    var message = event.target.getAttribute("data-confirm");
    if (message && !window.confirm(message)) event.preventDefault();
  });

  // Manual matcher: the two sides have to add up to the same number, and the
  // button stays disabled until they do. Same rule the server enforces.
  var form = document.getElementById("manual-form");
  if (form) {
    var bankOut = document.getElementById("sum-bank");
    var ledgerOut = document.getElementById("sum-ledger");
    var verdict = document.getElementById("sum-verdict");
    var submit = document.getElementById("manual-submit");

    var format = function (cents) {
      var sign = cents < 0 ? "-" : "";
      var whole = Math.floor(Math.abs(cents) / 100).toLocaleString("es-NI");
      var frac = String(Math.abs(cents) % 100).padStart(2, "0");
      return sign + whole + "." + frac;
    };

    var recount = function () {
      var bank = 0, ledger = 0, bankCount = 0, ledgerCount = 0;
      form.querySelectorAll("input.pick:checked").forEach(function (box) {
        var cents = parseInt(box.dataset.amount, 10) || 0;
        if (box.dataset.side === "bank") { bank += cents; bankCount++; }
        else { ledger += cents; ledgerCount++; }
        var row = box.closest("tr");
        if (row) row.classList.add("selected");
      });
      form.querySelectorAll("input.pick:not(:checked)").forEach(function (box) {
        var row = box.closest("tr");
        if (row) row.classList.remove("selected");
      });

      bankOut.textContent = format(bank);
      ledgerOut.textContent = format(ledger);

      var ready = bankCount > 0 && ledgerCount > 0;
      var equal = ready && bank === ledger;
      submit.disabled = !equal;
      verdict.className = "pill " + (equal ? "ok" : ready ? "danger" : "");
      if (!ready) verdict.textContent = "Seleccione partidas de los dos lados";
      else if (equal) verdict.textContent = bankCount + ":" + ledgerCount + " cuadra";
      else verdict.textContent = "Diferencia " + format(bank - ledger);
    };

    form.addEventListener("change", function (event) {
      if (event.target.classList.contains("pick")) recount();
    });
    recount();
  }

  // Flash messages fade after a while; errors stay until dismissed.
  var flashes = document.getElementById("flashes");
  if (flashes) {
    flashes.addEventListener("click", function (e) {
      var flash = e.target.closest(".flash");
      if (flash) flash.remove();
    });
    window.setTimeout(function () {
      flashes.querySelectorAll(".flash:not(.error)").forEach(function (flash) {
        flash.remove();
      });
    }, 6000);
  }
})();
