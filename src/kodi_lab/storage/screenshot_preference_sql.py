"""SQLite guards shared by fresh schemas and the screenshot preference migration."""

SCREENSHOT_PREFERENCE_TRIGGERS = (
    "CREATE TRIGGER trg_bridge_capture_preference_immutable BEFORE UPDATE OF "
    "capture_preference_revision ON external_bridge_request_receipts WHEN "
    "NEW.capture_preference_revision<>OLD.capture_preference_revision BEGIN "
    "SELECT RAISE(ABORT,'bridge_capture_preference_immutable'); END",
    "CREATE TRIGGER trg_external_screenshot_preference_insert BEFORE INSERT ON "
    "external_screenshot_preferences WHEN NEW.revision<>1 OR EXISTS(SELECT 1 "
    "FROM external_screenshot_preferences WHERE target_id=NEW.target_id) OR "
    "NOT EXISTS(SELECT 1 FROM targets WHERE id=NEW.target_id AND target_type IN "
    "('external_development','external_production')) BEGIN SELECT "
    "RAISE(ABORT,'screenshot_preference_invalid'); END",
    "CREATE TRIGGER trg_external_screenshot_preference_revision BEFORE UPDATE ON "
    "external_screenshot_preferences WHEN NEW.target_id<>OLD.target_id OR "
    "NEW.revision<>OLD.revision+1 BEGIN SELECT "
    "RAISE(ABORT,'screenshot_preference_revision_invalid'); END",
    "CREATE TRIGGER trg_external_screenshot_preference_no_delete BEFORE DELETE ON "
    "external_screenshot_preferences WHEN EXISTS(SELECT 1 FROM "
    "external_kodi_targets WHERE target_id=OLD.target_id) BEGIN SELECT "
    "RAISE(ABORT,'screenshot_preference_required'); END",
    "CREATE TRIGGER trg_external_screenshot_preference_initialize AFTER INSERT ON "
    "external_kodi_targets BEGIN INSERT INTO external_screenshot_preferences "
    "(target_id,enabled,revision,updated_by,updated_at) VALUES (NEW.target_id,"
    "EXISTS(SELECT 1 FROM json_each(NEW.configured_capabilities_json) WHERE "
    "value='screenshot'),1,'system',NEW.created_at); END",
)
