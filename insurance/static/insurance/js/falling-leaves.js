/* Spawns randomized falling leaves into any .autumn-leaves-layer container.
   Density is read from the container's data-density attribute (default 12).
   Skipped entirely under prefers-reduced-motion. */
(function () {
    const LEAF_COLORS = ["var(--leaf-color-1)", "var(--leaf-color-2)", "var(--leaf-color-3)", "var(--leaf-color-4)"];

    function randomBetween(min, max) {
        return Math.random() * (max - min) + min;
    }

    function spawnLeaves(container) {
        const density = parseInt(container.dataset.density, 10) || 12;
        for (let i = 0; i < density; i++) {
            const leaf = document.createElement("span");
            leaf.className = "autumn-leaf";
            leaf.style.setProperty("--leaf-x", `${randomBetween(0, 100)}%`);
            leaf.style.setProperty("--leaf-size", `${randomBetween(10, 22)}px`);
            leaf.style.setProperty("--leaf-color", LEAF_COLORS[i % LEAF_COLORS.length]);
            leaf.style.setProperty("--fall-duration", `${randomBetween(10, 20)}s`);
            leaf.style.setProperty("--fall-delay", `-${randomBetween(0, 20)}s`);
            leaf.style.setProperty("--sway-duration", `${randomBetween(3, 6)}s`);
            leaf.style.setProperty("--sway-delay", `-${randomBetween(0, 6)}s`);
            leaf.style.setProperty("--sway-x", `${randomBetween(10, 35)}px`);
            container.appendChild(leaf);
        }
    }

    document.addEventListener("DOMContentLoaded", function () {
        if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
        document.querySelectorAll(".autumn-leaves-layer").forEach(spawnLeaves);
    });
})();
