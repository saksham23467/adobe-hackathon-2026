# Fetch budget (render-extract)

Live audits fetch with **curl** (GET, http/https). Playwright and Selenium
are not used. Constraints keep the audit under 5 minutes:

- Crawl at most `max_pages` URLs (default 40), polite delay between hosts.
- Hard per-URL timeout (`request_timeout_s`, default 10s).
- Byte cap per response (`max_bytes_per_page`).
- Empty-shell / canvas / image-only checks run on the curl HTML. There is no
  headless DOM, so JS-added text is not invented.

Fixture tests may include a second HTML snapshot to compare a known
client-rendered view. That snapshot is never produced by a browser in the
runtime.
