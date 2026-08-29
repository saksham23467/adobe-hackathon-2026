# Browser budget (render-extract)

Rendering is the runtime tent-pole. Constraints keep the audit < 5 minutes and
deterministic:

- Render at most `max_render` URLs (default 8): home + 1–2 per page-class.
- Hard per-URL render timeout (default ~8s); on timeout → `inconclusive`.
- Capped concurrency (default 4).
- Renderer is optional. Preferred: Playwright Chromium. If unavailable, the
  skill degrades to raw-HTTP-only and marks render checks `inconclusive`.
- The renderer never executes site actions beyond loading and waiting for
  network idle; no clicks that submit forms, no auth, no downloads.

## Installing the optional renderer
```bash
pip install playwright && python -m playwright install chromium
```
The marketplace runs fully without it.
