# Agent rules

## Release policy (standing user instruction, 2026-11-28)

Every accepted change ships to production immediately: commit, push `master`, then deploy staging and production with `scripts/deploy_prod.sh`. Do not wait for a per-release confirmation.

Required steps:

1. Take a `pg_dump` of the target database and copy the target `.env` into `/root/backups/` before mutating anything.
2. Set the new `APP_BUILD_ID` in the target `.env`, recreate the app and worker with `docker compose up -d`, then publish the code with the guarded release.
3. Verify `/health`, `/ready`, the worker heartbeat/build id, and run the changed logic in-container against the live configuration.
4. Record the build, release ids, backups and verification in `docs/DEPLOY_TOOLING.md`, and update `~/personal-docs/README.md` (`## todo`) with the same facts.

`data/image_objects/*.json` and `data/check_engines/*.json` only seed a database once; after a code deploy, upsert changed object/engine definitions into `admin_module_registry` with a `system_audit_log` row, because the enabled database catalog overrides the files at runtime.

## UI language split

The admin and public UI default to Vietnamese (`vi`). Any surface that exposes the VI/EN toggle must translate all of its visible strings; never leave an English-only label or mix languages inside one sentence. Standalone admin pages without the toggle must still be Vietnamese-only, not Vietnamese chrome around English labels.

When adding or changing UI text:

1. Server-rendered HTML: write Vietnamese text as the default.
2. On toggle surfaces, add `data-i18n`, `data-i18n-placeholder`, or `data-i18n-template` keys for the English dictionary in `static/ui/admin_i18n.js` (admin) or the page-local dictionary (public forms).
3. Dynamic JS strings: call the existing translation helper instead of hardcoding English.
4. Do not translate data: IDs, slugs, enum codes, status tokens, API field names, model names, or provider names.
5. Update `tests/test_admin_accounts.py` or the relevant UI test with both the Vietnamese default and the English switch.
6. Update affected screenshots in `docs/images/` from the Vietnamese default UI; screenshots must not show a stale mixed-language page.
7. When a surface is intentionally still Vietnamese-only, state that explicitly in the handoff instead of claiming full localization.

## UI icons and emojis

Do not use emojis or dingbat characters (such as `🗑️`, `✓`, `⏳`, `📍`, `⚡`) anywhere in the web UI (`routers/`, `static/ui/`). Use plain localized text or inline SVG icons instead.

## Auth surfaces

Admin browser pages use the `/login` signed-cookie session. API clients may still use HTTP Basic Authentication. Do not document browser pages as Basic Auth. Cookie-session writes require exact same-origin Origin/Referer. Basic API writes allow headerless CLI clients but reject browser cross-site `Sec-Fetch-Site` and any present mismatched Origin/Referer. `/logout` is POST-only and same-origin protected.
