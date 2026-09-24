# ATIP ML model lifecycle (W5)

A **model** (`ml_model`) is a named purpose: its type, task, label and feature set.
A **version** (`ml_model_version`, v1, v2, …) is one trained artifact. Lifecycle states apply to versions.

```
DRAFT → TRAINING → TRAINED → VALIDATION → APPROVED → ACTIVE ⇄ PAUSED
                 ↘ FAILED                         (VALIDATION → TRAINED, APPROVED → VALIDATION allowed)
most states → RETIRED → ARCHIVED (terminal)
```

| Rule | Enforcement |
|---|---|
| Training never activates a model | Training ends at TRAINED. VALIDATION, APPROVED and ACTIVE are separate owner actions with an actor and reason (`ml_model_event`) |
| No activation without an artifact | →VALIDATION / APPROVED / ACTIVE refused when there is no artifact |
| Exactly one ACTIVE version per model | Activating a version pauses the previous ACTIVE one (logged). `ml_model.active_version` always names it |
| Artifacts are immutable | `artifacts.save` refuses to overwrite. Artifacts are loaded only when their sha256 matches the registry |
| Historical versions stay identifiable | RETIRED / ARCHIVED keep their artifact; every prediction stores the model version + artifact hash |
| Only ACTIVE predictions reach strategies | `version_status='ACTIVE'` at prediction time, and only dates after the version's dataset end |
| No path to execution | `ml/` has no execution / orders import; the ML score is a strategy feature, and strategies go through W4 risk |

## How the owner takes a model live (paper)

1. `python -m ml sync`: registers the features and built-in feature sets.
2. `python -m ml create-model dir5 --type logistic_regression --label direction --feature-set atip_technical@1`
3. `python -m ml train dir5 dataset.json`: creates v1 as TRAINED. Check the training run and its descriptive metrics.
4. `python -m ml lifecycle dir5 v1 VALIDATION --reason "…"`, then `APPROVED`, then `ACTIVE`, or use the `/ml` page buttons.
5. In `config.json`, set `"ml": {"enabled": true, "default_model": "dir5"}`.
6. Move a strategy that reads `ml_score` (e.g. `ml_direction`) to PAPER. Its intents still pass the W4 risk engine; orders are sent only if execution is configured to.

Pause with `POST /api/ml/models/{id}/pause`, or `lifecycle … PAUSED`. Strategies then see no ML values, so their ML conditions are not met.
