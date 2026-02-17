# UI Tokens & Components

The UI layer shares a small set of design tokens defined in
`static/css/theme-overrides.css`. Use these helpers when building new pages
or components so spacing, typography, and colors stay consistent.

## Design Tokens

| Token | Usage |
| --- | --- |
| `--ui-font-size-xs` → `--ui-font-size-2xl` | Heading/body text helpers (`.heading-xl`, `.body-sm`, `.text-muted`). |
| `--ui-space-1` … `--ui-space-8` | Vertical rhythm inside cards/panels – align padding/margin multiples to these values. |
| `--ui-surface`, `--ui-surface-alt`, `--ui-surface-muted` | Backgrounds for `.ui-panel` and `.ui-card`. |
| `--ui-border`, `--ui-border-strong` | Border colors for cards, dividers, and form fields. |
| `--ui-success`, `--ui-warning`, `--ui-info`, `--ui-danger` (+ `*-soft`) | Feed into `.status-chip--*` and badge states. |

Dark-mode equivalents are set automatically when `html` has the `dark` class—
no need for manual overrides.

## Shared Components

| Class | Description |
| --- | --- |
| `.ui-panel` | High-level containers (page shells, filter blocks). Includes radius, blur, padding, and default surface color. |
| `.ui-card` | Compact cards for dashboard widgets or list/grid items. Animated hover elevation is built in. |
| `.status-chip` + `--success / --warning / --danger / --info` | Accessible pill badges for statuses/priority. Use in tables/cards instead of hard-coded colors. |
| `.btn`, `.btn-secondary`, `.btn-ghost` | Base button states. All CTA buttons should reuse these classes to keep padding, rounding, and focus rings consistent. |
| `.heading-xl`, `.heading-lg`, `.heading-md`, `.body-sm`, `.text-muted` | Typography helpers for headline hierarchies and supporting text. |
| `.filter-panel`, `__grid`, `__actions` | Common filter forms. Wrap inputs inside `__grid` (supports responsive `grid-cols-*`) and keep Reset/Apply buttons inside `__actions` to mirror every listing page. |
| `.sortable-link` | Macro output for sortable table headings. Use the `{% sortable_heading %}` tag so labels + chevrons stay consistent instead of hand-coding up/down arrows. |
| `.meta-badge` | Neutral pill used for contextual metadata (owners, tags) so mobile cards and desktop rows share the same visual treatment. |

### Buttons

```html
<button class="btn">Primary</button>
<button class="btn btn-secondary">Secondary</button>
<button class="btn btn-ghost">Ghost</button>
```

Buttons already include gradient backgrounds, hover transitions, and focus
styles. Add modifier classes on top (e.g. `.btn-danger`) when a semantic
color change is required.

### Cards & Panels

```html
<section class="ui-panel">
  <h2 class="heading-lg">Filters</h2>
  …
</section>

<article class="ui-card">
  <span class="status-chip status-chip--info">Open</span>
  …
</article>
```

## Implementation Notes

- Keep custom background photos/gradients out of templates; rely on the shared
  surface tokens so dark/light themes remain legible.
- When introducing new badges or alerts, base their colors on the semantic
  token pairs above to guarantee contrast in both themes.
- The tokens are pure CSS; no Tailwind rebuild is required after consuming them.
