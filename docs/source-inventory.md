# Source Inventory

## Enabled Sources

| Company | ATS | Adapter | Endpoint | Job Count | Verified | Notes |
|---|---|---|---|---|---|---|
| Razorpay | Greenhouse | `greenhouse` | `boards-api.greenhouse.io/v1/boards/razorpaysoftwareprivatelimited/jobs` | 25 | 2026-08-21 | JSON API, slug is the full legal entity name |
| PhonePe | Greenhouse | `greenhouse` | `boards-api.greenhouse.io/v1/boards/phonepe/jobs` | 72 | 2026-08-21 | JSON API |
| Groww | Greenhouse (EU) | `greenhouse` | `boards.eu.greenhouse.io/v1/boards/groww/jobs` | 4 | 2026-08-21 | EU instance; low count is real — tested `growwindia`, `nextbillion`, `nextbilliontechnology` slugs, all returned 0 |
| Paytm | Lever | `lever` | `api.lever.co/v0/postings/paytm` | 463 | 2026-08-21 | JSON API |
| Zeta | Lever | `lever` | `api.lever.co/v0/postings/zeta` | 23 | 2026-08-21 | JSON API; all roles in Bangalore |
| Lendingkart | Darwinbox | `darwinbox` | `hrlendingkart.darwinbox.in` | 6 | 2026-08-21 | No Darwinbox JSON API; scrape job links from WordPress page at `lendingkart.com/job/` (returns 403 without browser User-Agent) |
| Jupiter | Keka | `keka` | `jupiter.keka.com/careers` | — | 2026-08-21 | No Keka JSON API confirmed; HTML scrape required |
| CRED | Custom | `custom` | `careers.cred.club` | — | 2026-08-21 | Next.js SPA; requires headless browser to render; no ATS platform detected |

## Disabled Sources (ATS unidentified)

| Company | Careers URL | Notes |
|---|---|---|
| Zerodha | `careers.zerodha.com` | JS-rendered SPA, no ATS detected; not on Greenhouse or Lever |
| BharatPe | `bharatpe.com/career` | Minimal page, no ATS links found |
| Pine Labs | `pinelabs.com/careers/open-jobs` | Own site, currently 0 active postings |
| Slice (India) | `slice.bank.in/careers` | Not on Greenhouse or Lever; GH slug `slice` belongs to a US/EU company. Static marketing page with no job links. Formerly sliceit/slicepay |

## Excluded

| Company | Reason |
|---|---|
| Rupeek | Postings are hosted on LinkedIn. Scraping LinkedIn job listings would violate their Terms of Service (Section 8.2 — prohibition on scraping, crawling, or automated data collection). Removed from pipeline. |

## Adapter Requirements

- **`greenhouse`** — HTTP GET to JSON API. Append `?content=true` for full descriptions.
- **`lever`** — HTTP GET to JSON API. Supports `?mode=json` for structured data.
- **`darwinbox`** — No public API. Scrape job links from the company's own careers page (WordPress in Lendingkart's case). Individual job details at `darwinbox.in/ms/candidatev2/main/careers/jobDetails/{id}`.
- **`keka`** — No public JSON API (tested `/careers/api/*`, `/api/v1/jobs`, `/api/careers/getjobs` — all 404). HTML scrape required.
- **`custom`** — Company-specific scraper needed. CRED uses a Next.js SPA that requires JS rendering.

## Reconnaissance Notes

Tested the following candidate companies against both Greenhouse (`boards-api.greenhouse.io`) and Lever (`api.lever.co`) APIs: navi, cashfree, juspay, setu, m2p, yubi, fimoney, axio, kreditbee, perfios, signzy, decentro, chargebee. None returned valid job boards on either platform. Navi uses TurboHire (JS SPA, no public API). Chargebee uses LinkedIn for hiring. Cashfree directs applicants to email.
