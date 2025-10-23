# Building Management UI

Responsive Django/Tailwind UI for managing buildings, units, and work orders.

## Development Workflow

1. **Install tooling**
   ```bash
   npm install
   npm run build:css  # generates static/css/dist.css
   ```
2. **Tailwind watch mode** (during template work)
   ```bash
   npm run watch:css
   ```
3. **Run Django**
   ```bash
   python manage.py runserver
   ```

Tailwind is configured with purge-aware `content` globs so unused utilities are removed in the production bundle.

## Playwright Smoke Tests

Lightweight responsive smoke tests live in `tests/playwright/`.

```bash
npx playwright install  # downloads browsers
npm run test:ui         # runs smoke tests against http://localhost:8000
```

Set `BASE_URL` to point at non-default environments (e.g., `BASE_URL=https://staging.example.com npm run test:ui`).

The suite captures screenshots for mobile (iPhone 13 Mini) and desktop (Chrome) breakpoints and stores them in `tests/playwright/artifacts/`.

## Mobile UX Guidelines

- **Mobile first**: every template is designed for small screens first; wide layouts progressively enhance with larger breakpoints.
- **Accessible touch targets**: buttons and actionable elements use Tailwind utilities to keep minimum 44px height and adequate spacing.
- **Sticky filters**: search/filter toolbars on list screens stay visible on larger screens but remain collapsible on mobile.
- **Card-first data**: tabular data collapses into cards on small viewports for easy scanning; essential metadata surfaces at the top.
- **Badges for state**: priority and status indicators rely on color-coded badges paired with text to communicate state at a glance.
- **Session-safe forms**: shared `_form_layout.html` ensures consistent field spacing, error handling, and responsive alignment.

These patterns should be reused for future additions to maintain alignment with the mobile design system.
