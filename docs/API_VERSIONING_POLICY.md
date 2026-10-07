# ATIP public API: versioning and deprecation policy

**Applies to:** the public API v1 (`/api/v1/...`), as listed in `enterprise/public_api.py` (`RESOURCES`) and published as `docs/api/openapi-v1.json`.

**Tracker item:** API-03.

**Status:** in force from W39 (2026-10-07). Nothing in v1 is deprecated today.

## What the contract covers

**The public API is `/api/v1/...` only.**
- It is the set of resources in `RESOURCES`, each with one permission. API keys are scoped to permissions.
- `python -m ops api-docs` writes its OpenAPI 3.1 document to `docs/api/openapi-v1.json`.

**Unversioned `/api/...` is not part of the contract.**
- These are the paths the dashboard pages use, and they may change with any release.
- If you call one with an API key, the response carries `Deprecation: true` and a `Link: </api/v1/...>; rel="successor-version"`.

**The error format is part of the contract.** Errors use the envelope `{"error": {"code", "message", "request_id", "retryable"}}`. The codes are listed in `ops/errors.py`.

## What changes keep v1

These are **additive changes**. They ship without notice and without a version change.
- A new resource, a new optional query parameter, or a new optional request field.
- A new field in a response object. Clients must ignore fields they do not know.
- A new error code for a new failure. Clients must treat an unknown code by its HTTP status.
- A new value in a field documented as open-ended, such as a status string the docs call "e.g.".
- Higher limits: page size, rate limits, quotas.

## What counts as breaking

These changes need a new version (`/api/v2/...`) or the deprecation process below.
- Removing or renaming a resource, a field or a parameter.
- Changing a field's type, unit, meaning or nullability.
- Making an optional parameter required.
- Changing a default that changes results, such as sort order, page size or the period a value covers.
- Narrowing an accepted value range.
- Changing the permission a resource needs.
- Lowering a documented limit.

## The deprecation process

Each step is mandatory.

1. **Announce.**
   - Add an entry to `DEPRECATIONS` in `enterprise/public_api.py`:
     - `deprecated`: the date the notice starts;
     - `sunset`: the date the resource stops answering;
     - `successor`: the replacement, if there is one;
     - `reason`.
   - Record it in the release notes / wave handoff.
   - `validate_deprecations()` runs in the test suite. It refuses an entry whose notice period is under **180 days** (`MIN_NOTICE_DAYS`), an unknown resource, or a missing reason.
2. **Warn, from the `deprecated` date.** Every response from the resource carries:
   - `Deprecation: @<unix time>` (RFC 9745);
   - `Sunset: <HTTP date>` (RFC 8594);
   - `Link: <successor>; rel="successor-version", </docs/API_VERSIONING_POLICY.md>; rel="deprecation"`.

   The OpenAPI document marks the operation `deprecated: true`, with `x-sunset`, `x-successor` and `x-deprecation-reason`.
3. **Retire, from the `sunset` date.**
   - The resource answers **410** with the error code `GONE`.
   - The message names the successor.
   - The handler can be removed in a later release; the 410 stays until v1 itself is retired.
4. **A new major version.**
   - `/api/v2` is served alongside v1.
   - v1 keeps working for **at least 6 months** after v2 is published, under the same deprecation headers.

## What clients should do

- Call `/api/v1/...`, never the unversioned paths.
- Ignore response fields you do not know.
- Log or alert on any `Deprecation` or `Sunset` response header. It is the only notice an integration receives automatically.
- Treat 410 as permanent; do not retry it.
- Send `X-Request-ID` and quote it, or the returned `request_id`, when reporting a problem.

## Where it is implemented

| Piece | File |
|---|---|
| The v1 resources, the registry, header values, the OpenAPI markers | `enterprise/public_api.py` (`RESOURCES`, `DEPRECATIONS`, `deprecation_for`, `validate_deprecations`, `openapi_v1`) |
| The `/api/v1` alias, the headers on responses, 410 after sunset | `ops/http.py` (`OpsMiddleware`) |
| Tests | `tests/test_w39_exec_data_tests.py`: `test_a_deprecated_v1_resource_warns_then_answers_410_after_sunset`, `test_api_key_reads_v1_within_its_scopes_only` |
