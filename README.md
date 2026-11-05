# EventGate SL — Event publishing and secure digital ticketing

Django 5.2 LTS-compatible project for event discovery, ticket orders, offline/mobile-money payment verification, digital ticket records and authenticated multi-device gate scanning. Sierra Leone timezone and SLE/USD currencies are supported.

## Security model (read this)
- QR payload contains an unguessable random bearer secret; its SHA-256 hash is stored for validation and the token is encrypted at rest using a key derived from Django SECRET_KEY. Rotating SECRET_KEY requires a planned credential migration or ticket reissue. The included `Ticket.issue()` service creates the secret but payment-verification automation and secure delivery should be completed and tested before live use.
- QR codes are not technically readable only by one scanner app: any QR decoder can decode the payload. Security comes from high-entropy secrets, authenticated organizer authorization, server-side event validation, and a database-locked one-time state transition. Do not put personal data in QR payloads.
- `select_for_update()` plus a database transaction ensures concurrent online scans do not both consume the same ticket on PostgreSQL. SQLite is for local development only and does not provide equivalent row-locking behaviour.
- Multiple scanners must have an internet connection to the same server for immediate cross-device validation. Offline scanning cannot guarantee duplicate prevention without a more complex signed offline protocol and reconciliation process; this project deliberately fails closed rather than accepting unverified tickets.
- Current payment methods are manual/offline. No mobile money/card API is claimed as integrated. Staff must independently verify payment before marking an order paid and issuing tickets.

## Local development (Windows PowerShell)
```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(64))"
# Set DJANGO_SECRET_KEY in .env and set DJANGO_DEBUG=True for local development
python manage.py makemigrations events orders tickets advertisements
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Visit http://127.0.0.1:8000/ and /admin/. Create an organizer user in admin; set event status to Published after reviewing it. Create ticket tiers from the organizer dashboard. `manage.py test events` contains core one-time scan tests.

## Tailwind CSS (local build; no Tailwind CDN)

The project includes Tailwind CSS v3, a `package.json`, a Tailwind configuration, source CSS at `static/src/input.css`, and a pre-generated local stylesheet at `static/css/output.css`. The base template loads the local stylesheet using Django's `{% static %}` tag.

### Windows PowerShell

Run these commands from the folder containing `manage.py` (not the parent folder):

```powershell
node --version
npm --version
npm install
npm run build:css
```

For development, keep this command running in a separate terminal so CSS recompiles when templates change:

```powershell
npm run watch:css
```

Then run Django in another terminal:

```powershell
python manage.py runserver
```

The `build:css` command uses Tailwind CSS v3's CLI and writes `static/css/output.css`. Do not delete the `build:css` script or rename `static/src/input.css` without updating `package.json`. If dependencies were partially installed, remove `node_modules` and `package-lock.json`, then run `npm install` again. The pre-generated CSS lets the templates remain styled before you rebuild it locally.

For production, run `npm run build:css` before `python manage.py collectstatic --noinput`. Keep `node_modules` out of the deployed ZIP/server; only the compiled CSS is needed at runtime.

## Docker
1. Copy `.env.example` to `.env`.
2. Replace `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD` with strong unique secrets. Set `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, SMTP credentials and `DJANGO_SECURE_SSL_REDIRECT=True` for HTTPS deployment.
3. Run `docker compose up --build -d`.
4. Run `docker compose exec web python manage.py createsuperuser`.
5. Put a TLS reverse proxy (e.g. Caddy/Nginx) in front; do not expose the app publicly over plain HTTP. Configure backups, alerting and restore tests.

## Production checklist before real ticket sales
- Complete a secure QR issuance and delivery flow (email link with one-time claim, or store an encrypted credential with key rotation). Ticket detail displays a QR for valid tickets; used tickets no longer display it.
- The Django admin includes a staff-only action to mark pending orders paid and issue the ordered number of tickets. Test payment verification and ensure capacity is correctly released on rejection/cancellation/refunds before launch.
- Add signup, email verification, abuse throttling, organizer identity verification, audit logs, moderation workflow, refunds/cancellation policy, and transactional email/SMS delivery.
- Move media to private object storage where appropriate; validate and re-encode uploaded images, add antivirus scanning, enforce upload limits, and configure backups/retention.
- Keep Tailwind CSS self-hosted through the compiled local stylesheet. Replace the remaining scanner CDN with a pinned, self-hosted asset and configure a strict Content Security Policy for production.
- Add rate limiting (reverse proxy/app), monitoring, error reporting, log retention, vulnerability scanning, load tests, concurrency tests on PostgreSQL, and disaster recovery exercises.
- Test end-to-end purchase, payment verification, ticket delivery, scanner permissions, duplicate concurrent scans, ticket revocation, and event start/end boundaries on the production database.

## Scope / limitations
This is a production-oriented foundation, not a claim of fully certified or independently penetration-tested software. Run `python manage.py check --deploy`, automated tests, migration checks and an external security review before launching. Payment processors and SMS/email vendors require credentials and provider-side setup.

## Company advertising (added)
EventGate SL includes a separate paid advertising module. The platform administrator creates and publishes ad plans in **Admin → Advertisements → Ad plans**, setting the plan name, fee, currency, and duration in months (for example 1, 3, 6 or 12 months). Companies create an account, open **/advertising/**, choose a plan and submit an external payment reference (Orange Money, Afrimoney, bank transfer or other configured offline method). The website does not collect payment credentials or process mobile-money payments. An administrator verifies the payment from **Admin → Advertisements → Ad subscriptions**; only then is the subscription activated and its expiry date calculated.

Advertisers with an active verified subscription can submit image and/or short MP4/WebM video campaigns at **/advertising/campaigns/new/**. Image limit: 8 MB; video limit: 25 MB. Campaigns are separately reviewed by an administrator and only approved campaigns attached to an unexpired subscription are eligible to display. Placements include homepage/banner, sidebar, featured partner spotlight, and event-feed placement. Campaign dashboards show impressions, clicks, and click-through rate. Ad links pass through an internal click-tracking route before redirecting to the advertiser's destination. Impressions are counted when a rendered ad is requested by a browser; automated traffic and ad blockers can affect counts, so these metrics are directional rather than billing-grade audited analytics.

### Advertising setup and production notes
- After adding this module, create migrations with `python manage.py makemigrations advertisements` and apply them with `python manage.py migrate` (the Docker startup command runs migrations automatically).
- Add plans and set prices in Django Admin. Plans are configurable; the application does not hard-code 1/3/6 month options.
- Uploads are public promotional assets. For production, store them in a dedicated object-storage bucket/CDN, enforce file-size and MIME/content-signature checks at the edge, scan uploads, transcode videos to a supported format, and set bandwidth/cache limits. Do not store private payment proofs in a public media bucket.
- Use HTTPS and a reverse proxy in production. Configure rate limits and analytics retention. Consider an external analytics pipeline if traffic reaches thousands of concurrent viewers; database counters are suitable as a starter but may need queue-based aggregation under high load.
