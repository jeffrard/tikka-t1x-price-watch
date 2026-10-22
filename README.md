# Tikka price watch

Watches prices for the **Tikka T1x MTR, 17 HMR, 20" barrel (right hand, UPC 082442898360)** and pushes notifications to your phone through the free **ntfy** app:

- **Right away**: a high-priority alert if any listing shows up under **$500**. Tap it to open the listing. Each listing alerts only once.
- **Every day after noon Central**: a quiet summary with the 3 cheapest listings and how many sources are working. If this summary stops arriving, the watcher is broken.

It runs free on GitHub Actions every 30 minutes and uses no Claude credits.

**What it checks:** gun.deals and GunPrime searches for this exact UPC/model number, Gun Made (often blocks GitHub, which is reported as "blocked", not broken), and this rifle's product page at 25 retailers. Out-of-stock pages and hidden "MAP" prices are skipped.

## Setup (about 5 minutes)

### 1. Set up ntfy on your phone
1. Install **ntfy** from the App Store or Google Play.
2. Tap **+** (Subscribe to topic) and enter a random topic name, such as `yourname-tikka-<random letters and numbers>`.
   - The topic name is the only "password." Anyone who knows it can read or send your alerts, so make it hard to guess.
3. Leave the server as the default (`ntfy.sh`) and tap **Subscribe**.
4. **iPhone:** allow notifications when it asks.

### 2. Create the repo
1. On GitHub, click **New repository**. Name it `tikka-price-watch`.
   - **Public** is simplest: Actions minutes are unlimited, and nothing private is in the code. Your topic name lives only in an encrypted secret.
   - **Private** also works, but switch the schedule to hourly (see the workflow file) to stay well inside the 2,000 free monthly minutes.
2. Click **uploading an existing file** and drag in **everything from the unzipped folder**, including the hidden `.github` folder.
   - If your computer hides `.github`, create the workflow on GitHub directly: **Add file → Create new file**, name it `.github/workflows/price-watch.yml`, and paste in that file's contents.

### 3. Add the secret
Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `NTFY_TOPIC` | your topic name from step 1, exactly as typed (for example `yourname-tikka-<random letters and numbers>`) |

### 4. Test it
**Actions** tab → **Tikka price watch** → **Run workflow** → tick **Send the daily summary notification right now** → **Run workflow**.

Within a minute or two your phone should get a "Tikka daily" notification. If the run shows a red ✗, open it and read the "Check prices" step.

After that it runs on its own.

## Changing things
Everything you might want to change is in `config.json`:

- `target_price`: alert threshold.
- `digest_hour`: hour for the daily summary (24-hour clock, Central time).
- `upcs`: add `082442932880` to also watch the **left-hand** 20".
- `retailer_pages`: add any retailer product page for this rifle. The script reads the price from the page's built-in product data, which most big retailers include.
- `aggregator_pages`: search-result pages (gun.deals, GunPrime, Gun Made) that list many stores at once.

To change how often it checks, edit the `cron` line in `.github/workflows/price-watch.yml`. To pause it: **Actions → Tikka price watch → ⋯ → Disable workflow**.

## Good to know
- Many dealers hide the price as "MAP" (minimum advertised price). Those listings are skipped because there's no number to compare.
- Prices don't include shipping or FFL transfer fees.
- The daily summary shows how many sources were checked, how many had a price, which sites blocked the check, and any that have been broken for 3+ hours.
- GitHub turns off scheduled workflows in repos with no activity for 60 days. The daily state update counts as activity, so this shouldn't happen. If it ever does, re-enable the workflow from the Actions tab.
