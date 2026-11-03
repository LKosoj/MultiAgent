"""Вызовы возможностей studio для экранов: общий помощник и идентификаторы (без tkinter)."""

from custom_tools.storybook.studio import capabilities

# Идентификаторы возможностей, которые использует Tk (по ним тест паритета ищет вызовы)
CAP_MEDIA_TIMELINE = "media_timeline"
CAP_MEDIA_SUBTITLES = "media_subtitles"
CAP_ARTIFACTS_LIST = "artifacts_list"
CAP_ARTIFACT_READ = "artifact_read"
CAP_ARTIFACT_VALIDATE = "artifact_validate"
CAP_ARTIFACT_SAVE = "artifact_save"
CAP_ARTIFACT_HISTORY = "artifact_history"
CAP_ARTIFACT_DIFF = "artifact_diff"
CAP_ARTIFACT_RESTORE = "artifact_restore"
CAP_BLOCKOUT_STATE = "blockout_state"
CAP_BLOCKOUT_DETAIL = "blockout_detail"
CAP_BLOCKOUT_PLAN = "blockout_plan"
CAP_BLOCKOUT_RUN = "blockout_run"
CAP_BLOCKOUT_SET_DURATION = "blockout_set_duration"
CAP_BLOCKOUT_LEAVE_AS_IS = "blockout_leave_as_is"
CAP_APP_SETTINGS_GET = "app_settings_get"
CAP_APP_SETTINGS_UPDATE = "app_settings_update"
CAP_APP_SETTINGS_RESET = "app_settings_reset"
CAP_APP_SETTINGS_CHECK_PATH = "app_settings_check_path"
CAP_VERSIONS_LIST = "versions_list"
CAP_REGENERATE_PREVIEW = "regenerate_preview"
CAP_REGENERATE_START = "regenerate_start"
CAP_IMAGE_EDIT_PREVIEW = "image_edit_preview"
CAP_IMAGE_EDIT_START = "image_edit_start"
CAP_MEDIA_INVENTORY = "media_inventory"


class StudioCalls:
    """Тонкая обёртка над мостом: если возможности нет в этой сборке, колбэк ошибки получит «unavailable»."""

    def __init__(self, bridge):
        self.bridge = bridge

    def call(self, cap_id, payload, on_success=None, on_error=None, key=None):
        if capabilities.find(cap_id) is None:
            if on_error:
                on_error({"status": "error", "error": {"code": "unavailable", "message": "Возможность недоступна", "details": {}}})
            return None
        return self.bridge.call(cap_id, payload, on_success=on_success, on_error=on_error, latest_only_key=key)
