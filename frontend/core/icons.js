/**
 * Shared SVG icons and markup helpers for AutoCC Studio.
 *
 * Provides a central dictionary of SVG icon paths to avoid duplicating inline
 * markup across JS modules and HTML templates.
 */

export const ICONS = {
  play: '<path d="M9 7.5 17 12l-8 4.5Z"/>',
  pause: '<path d="M7 6h3v12H7zm7 0h3v12h-3z"/>',
  subtitle: '<path d="M5 7h14M5 11h9M5 15h12"/>',
  video: '<rect x="3" y="5" width="14" height="14" rx="2"/><polygon points="17 9 22 6 22 18 17 15"/>',
  trash: '<path d="M5 7h14M10 7V5h4v2M7 7l1 12h8l1-12"/>',
  check: '<polyline points="20 6 9 17 4 12"/>',
  alert: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
  plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  search: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
  chevronDown: '<polyline points="6 9 12 15 18 9"/>',
  chevronRight: '<polyline points="9 18 15 12 9 6"/>',
  sparkles: '<path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/>',
  refresh: '<path d="M3 12a9 9 0 0 1 15-6.7L21 8M21 3v5h-5M21 12a9 9 0 0 1-15 6.7L3 16M3 21v-5h5"/>',
};

/**
 * Returns an SVG icon markup string.
 *
 * @param {keyof typeof ICONS} name - The icon key in `ICONS`.
 * @param {string} [className="w-4 h-4"] - CSS classes applied to the SVG.
 * @param {Record<string, string>} [attributes={}] - Additional attributes (e.g. `aria-hidden="true"`).
 * @returns {string} SVG HTML markup string.
 */
export function icon(name, className = "w-4 h-4", attributes = {}) {
  const content = ICONS[name] || "";
  const attrs = Object.entries({
    class: className,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "2",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
    ...attributes,
  })
    .map(([key, val]) => `${key}="${val}"`)
    .join(" ");

  return `<svg ${attrs}>${content}</svg>`;
}

/**
 * Creates and returns an SVG DOM element.
 *
 * @param {keyof typeof ICONS} name
 * @param {string} [className="w-4 h-4"]
 * @param {Record<string, string>} [attributes={}]
 * @returns {SVGElement}
 */
export function createIcon(name, className = "w-4 h-4", attributes = {}) {
  const temp = document.createElement("div");
  temp.innerHTML = icon(name, className, attributes);
  return temp.firstElementChild;
}
