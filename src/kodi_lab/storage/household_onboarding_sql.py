"""Endpoint reservations also constrain independent administrator registration."""

HOUSEHOLD_ONBOARDING_TRIGGERS = (
    *(
        f"CREATE TRIGGER trg_household_endpoint_{suffix} BEFORE {operation} ON "
        "external_kodi_targets WHEN EXISTS(SELECT 1 FROM "
        "household_onboarding_operations o WHERE o.host=NEW.host AND "
        "o.kodi_port=NEW.port AND o.stage<>'abandoned' AND "
        "o.target_id<>NEW.target_id) BEGIN "
        "SELECT RAISE(ABORT,'household_endpoint_reserved'); END"
        for suffix, operation in (
            ("insert", "INSERT"),
            ("update", "UPDATE OF host,port"),
        )
    ),
    "CREATE TRIGGER trg_household_abandon_pending_retirements BEFORE UPDATE OF "
    "stage ON household_onboarding_operations WHEN NEW.stage='abandoned' AND "
    "EXISTS(SELECT 1 FROM external_secret_retirements r WHERE "
    "r.target_id=NEW.target_id) BEGIN "
    "SELECT RAISE(ABORT,'household_secret_retirement_pending'); END",
)

EXTERNAL_SECRET_RETIREMENT_TRIGGERS = (
    "CREATE TRIGGER trg_external_secret_retirement_immutable BEFORE UPDATE ON "
    "external_secret_retirements BEGIN "
    "SELECT RAISE(ABORT,'external_secret_retirement_immutable'); END",
)
