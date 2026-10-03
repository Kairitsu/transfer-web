// Runs before the stylesheet paints so the saved theme never flashes.
(() => {
  let savedTheme = null;
  try {
    const value = localStorage.getItem("transfer-web-theme");
    if (value === "light" || value === "dark") savedTheme = value;
  } catch (error) {
    // Fall back to the system preference when browser storage is unavailable.
  }
  document.documentElement.dataset.theme = savedTheme ||
    (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
})();
