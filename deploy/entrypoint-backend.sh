#!/bin/sh
set -eu

load_secret() {
    variable_name="$1"
    secret_path="$2"
    if [ -z "$secret_path" ]; then
        return
    fi
    if [ ! -r "$secret_path" ]; then
        echo "required secret is unreadable: $secret_path" >&2
        exit 78
    fi
    secret_value=$(cat "$secret_path")
    export "$variable_name=$secret_value"
}

load_secret AG_UI_AUTH_TOKEN_MAP "${AG_UI_AUTH_TOKEN_MAP_FILE:-}"
load_secret OPENAI_API_KEY "${OPENAI_API_KEY_FILE:-}"
load_secret OPENAI_API_KEY_DB "${OPENAI_API_KEY_DB_FILE:-}"
load_secret HF_TOKEN "${HF_TOKEN_FILE:-}"

mode="${1:-}"
case "$mode" in
    api)
        shift
        exec uvicorn backend.fastapi_app.main:app \
            --host 0.0.0.0 \
            --port "${PORT:-8000}" \
            --workers "${WEB_CONCURRENCY:-1}" \
            --timeout-graceful-shutdown "${API_GRACEFUL_TIMEOUT_SECONDS:-45}" \
            "$@"
        ;;
    worker)
        shift
        exec python3 deploy/worker.py "$@"
        ;;
    migrate)
        shift
        exec python3 scripts/migrate_text2sql_state.py \
            --manifest config/text_to_sql/state_schema.yaml \
            "$@"
        ;;
    *)
        echo "usage: entrypoint-backend.sh api|worker|migrate" >&2
        exit 64
        ;;
esac

