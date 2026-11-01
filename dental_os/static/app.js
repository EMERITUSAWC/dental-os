document.addEventListener("click", function (e) {
  if (e.target.closest("[data-print]")) { window.print(); }
});
