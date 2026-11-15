# ATIP Dhan Deployment Specification

## Target environments

- Test URL: `https://test.bkesari.com/dhan`
- Production URL: `https://bkesari.com/dhan`
- Repository: `sanjaymorankar-debug/ATIP`
- Deployment must be reversible and must not overwrite unrelated bkesari.com functionality.

## Scope

This deployment packages the ATIP Dhan module as an isolated application/path. It includes:

1. Authentication and authorization.
2. User-specific Dhan account mapping.
3. Common Dhan market-data service.
4. User-specific portfolio/positions/orders/P&L.
5. Personalized ATIP dashboard settings.
6. Aggressive strategy: 50% exit at +3%, 50% runner, trailing stop, and momentum-aware exit.
7. Full automated test suite and regression checks.

## Environment separation

The test environment must use test-specific configuration and credentials. Production secrets must never be copied into the test environment.

Required configuration should be supplied through the hosting environment/secrets store, not committed to Git:

- `APP_ENV=test|production`
- `APP_BASE_PATH=/dhan`
- `APP_PUBLIC_URL=https://test.bkesari.com/dhan` or `https://bkesari.com/dhan`
- `DATABASE_URL`
- `SESSION_SECRET`
- `DHAN_DATA_API_BASE_URL`
- `DHAN_DATA_API_KEY` or the approved Dhan data authentication mechanism
- user-specific Dhan credential/token references via encrypted server-side storage

Never commit Dhan access tokens, API secrets, passwords, session secrets, or database credentials.

## Dhan architecture

Use one common Dhan market-data integration for ATIP market data. Dhan account/portfolio access must remain isolated per authenticated user.

```text
ATIP
├── Common Dhan Market Data
│   └── shared by signal/indicator services
│
└── User Context
    ├── User A -> Dhan A -> Portfolio A
    ├── User B -> Dhan B -> Portfolio B
    └── User C -> Dhan C -> Portfolio C
```

Market data is common; portfolio, orders, positions, holdings, P&L and credentials are private to the authenticated user.

## URL routing

The deployed module must be available at:

`/dhan`

The application must not break the existing root site or unrelated routes.

If the existing Hostinger deployment is a separate application, use a reverse proxy/subdirectory deployment. If it is a PHP/static site, deploy the ATIP application using the hosting-compatible runtime without replacing unrelated files.

## Deployment sequence

1. Build the application from the approved Git commit.
2. Run unit tests.
3. Run authentication/security tests.
4. Run aggressive-strategy tests.
5. Run integration tests.
6. Run the full existing regression suite.
7. Create a production-like test build.
8. Deploy to `test.bkesari.com/dhan`.
9. Perform smoke tests.
10. Verify authentication and user isolation.
11. Verify Dhan market-data connectivity.
12. Verify user-specific portfolio isolation.
13. Verify dashboard personalization.
14. Verify aggressive-strategy calculations in paper/simulation mode.
15. Only after explicit production approval, deploy the same validated artifact to `bkesari.com/dhan`.

## Trading safety

The first deployment must NOT enable unattended live order execution by default.

Use paper/simulation mode until:

- authentication is validated;
- Dhan account isolation is validated;
- portfolio reconciliation is validated;
- +3% partial exit is validated;
- runner/trailing-stop logic is validated;
- duplicate-order protection is validated;
- restart/recovery is validated;
- broker error handling is validated.

## Smoke tests

### `/dhan`

- Returns HTTP 200 after authentication bootstrap where appropriate.
- Login screen loads.
- Invalid login is rejected.
- Valid test user can login.
- Logout works.

### User isolation

- User A sees only User A portfolio.
- User B sees only User B portfolio.
- Changing a user ID in a request cannot expose another user's data.

### Dhan

- Common market data loads once through the shared service.
- User-specific Dhan connection resolves only the authenticated user's account.
- No Dhan secret appears in browser responses or logs.

### Aggressive strategy

- Before +3%: initial stop loss applies.
- At +3%: 50% is marked for partial exit.
- Remaining 50% becomes runner.
- At +6%: runner is not automatically closed.
- Trailing stop activates and never moves backward.
- Runner exits when the trailing stop/risk rule is triggered.

## Rollback

Keep the previous production deployment/artifact available. If any critical smoke/security/regression test fails, roll back the ATIP deployment without modifying unrelated bkesari.com files.

## Acceptance

Deployment is complete only when the exact test URL is reachable and the above smoke tests pass. Production deployment requires a separately validated test deployment and explicit approval.
