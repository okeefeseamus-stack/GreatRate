# 15-Year Rate Watch

Daily 15-year fixed mortgage rate dashboard. A GitHub Action runs on weekday mornings, pulls every source in `config.json`, averages the daily ones into one headline number, stores history, grabs news, redeploys the site on GitHub Pages, and emails you when the rate hits your target.

```
config.json               sources, alert threshold, news settings  <- the file you edit
scripts/update.py         the daily pipeline
docs/                     the website (GitHub Pages serves this folder)
  data/history.json       chart history (the "database")
  data/latest.json        today's snapshot
state.json                alert bookkeeping (so you don't get spammed)
.github/workflows/update.yml
```

## Setup (about 15 minutes)

1. **FRED key.** Free at https://fred.stlouisfed.org/docs/api/api_key.html.
2. **Gmail app password.** Google Account > Security > 2-Step Verification > App passwords. Create one called "Rate Watch". (Any SMTP works; set `SMTP_HOST`/`SMTP_PORT` secrets for non-Gmail.)
3. **Create a public repo** and push this folder to `main`.
4. **Settings > Secrets and variables > Actions**
   - Secrets: `FRED_API_KEY`, `SMTP_USER` (your Gmail address), `SMTP_PASS` (the app password), `ALERT_TO` (comma-separated emails)
   - Variables: `DASHBOARD_URL` (your Pages URL, added after step 6)
5. **Settings > Pages > Source: GitHub Actions.**
6. **Actions > Update rates > Run workflow.** First run replaces the sample data with two years of real history from FRED. Your link is `https://<username>.github.io/<repo>/`.
7. **Test the email:** Run workflow again with "Send a test alert email" checked.

## Changing the alert

Edit `config.json` on github.com (or in the GitHub phone app). Saving triggers a run right away.

```jsonc
"alert": {
  "enabled": true,
  "below": 5.50,       // email when the headline is at or below this
  "drop_bps": 15,      // also email on a one-day drop of 0.15 or more (0 = off)
  "cooldown_days": 7   // after an alert, stay quiet this long unless the rate falls another 0.05
}
```

Changing `below` re-arms the alert. (Comments above are for explanation; the real file is plain JSON.)

## Adding it to a phone or iPad home screen

Open the link in Safari > Share > Add to Home Screen. It launches full-screen with its own icon and a loading screen. Links open in the browser.

## Notes

- **Headline** = mean of the `in_average` sources. With 3+ sources, any source more than `outlier_pts` (0.40) from the median is left out that day and marked on the dashboard.
- **Backfilled history** is Optimal Blue only; live days are the multi-source average. If The Mortgage Reports runs consistently high, expect a small step in the chart on day one. Set its `in_average` to `false` if it bothers you.
- **Scrapers break when sites redesign.** A broken source shows "Not available today" and the rest keep working. Parsers are `parse_mnd` / `parse_tmr` in `update.py`.
- **Schedule:** weekdays 9:15 AM ET (8:15 during standard time). FRED posts Optimal Blue's prior-day value around 8 AM ET.
- Optimal Blue data is copyrighted; fine for a personal family dashboard, not for a commercial product.
- Run locally: `pip install -r requirements.txt && FRED_API_KEY=... python scripts/update.py`, then `python -m http.server -d docs`.
