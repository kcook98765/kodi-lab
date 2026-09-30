"""Fixed SQLite guards for development capability amendment receipts."""

DEVELOPMENT_CAPABILITY_TRIGGERS = (
    "CREATE TRIGGER trg_development_capability_amendment_insert BEFORE INSERT ON "
    "external_development_capability_amendments WHEN "
    "COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')<>"
    "'external_development' OR COALESCE((SELECT configured_capabilities_json "
    "FROM external_kodi_targets WHERE target_id=NEW.target_id),'')<>NEW.before_json "
    "OR COALESCE((SELECT pairing_state FROM external_bridge_pairings WHERE "
    "target_id=NEW.target_id),'')<>'paired' OR COALESCE((SELECT "
    "pinned_bridge_id FROM external_bridge_pairings WHERE target_id=NEW.target_id),"
    "'')<>NEW.bridge_id OR NOT json_valid(NEW.before_json) OR NOT "
    "json_valid(NEW.requested_json) OR json_type(NEW.before_json)<>'array' "
    "OR json_type(NEW.requested_json)<>'array' OR "
    "EXISTS(SELECT 1 FROM json_each(NEW.before_json) WHERE type<>'text' OR "
    "value NOT IN ('target_inspect','addon_inventory','semantic_remote',"
    "'screenshot','workspace_deployment','fixed_execution')) OR "
    "EXISTS(SELECT 1 FROM json_each(NEW.requested_json) WHERE type<>'text' OR "
    "value NOT IN ('target_inspect','addon_inventory','semantic_remote',"
    "'screenshot','workspace_deployment','fixed_execution')) OR "
    "(SELECT count(*) FROM json_each(NEW.before_json))<>(SELECT count(DISTINCT "
    "value) FROM json_each(NEW.before_json)) OR (SELECT count(*) FROM "
    "json_each(NEW.requested_json))<>(SELECT count(DISTINCT value) FROM "
    "json_each(NEW.requested_json)) BEGIN SELECT RAISE(ABORT, "
    "'development_capability_amendment_invalid'); END",
    "CREATE TRIGGER trg_development_capability_amendment_immutable BEFORE UPDATE ON "
    "external_development_capability_amendments BEGIN SELECT RAISE(ABORT, "
    "'development_capability_amendment_immutable'); END",
    "CREATE TRIGGER trg_development_capability_amendment_no_delete BEFORE DELETE ON "
    "external_development_capability_amendments BEGIN SELECT RAISE(ABORT, "
    "'development_capability_amendment_immutable'); END",
    "CREATE TRIGGER trg_development_configured_capability_amendment_required "
    "BEFORE UPDATE OF configured_capabilities_json ON external_kodi_targets WHEN "
    "NEW.configured_capabilities_json<>OLD.configured_capabilities_json AND "
    "COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')="
    "'external_development' AND EXISTS(SELECT 1 FROM external_bridge_pairings p "
    "WHERE p.target_id=NEW.target_id AND p.pairing_state='paired') AND "
    "NOT EXISTS(SELECT 1 FROM "
    "external_development_capability_amendments a WHERE a.target_id=NEW.target_id "
    "AND a.before_json=OLD.configured_capabilities_json AND "
    "a.resulting_json=NEW.configured_capabilities_json) BEGIN SELECT RAISE(ABORT, "
    "'development_capability_amendment_required'); END",
)


PRODUCTION_SCREENSHOT_TARGET_GUARD = (
    "COALESCE((SELECT target_type FROM targets WHERE id=NEW.target_id),'')="
    "'external_production' AND "
)
