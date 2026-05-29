#!/bin/bash
# Быстрая очистка памяти для Text-to-SQL сессии
# Использование: ./memory/quick_clear.sh

SESSION_ID="duckdb_users_kosoj_documents_multiagent_data_sber_index_prod_db"

echo "🔍 Быстрая очистка памяти для Text-to-SQL сессии..."
echo "📋 Session ID: $SESSION_ID"
echo ""

# Активируем виртуальную среду если она не активна
if [[ "$VIRTUAL_ENV" == "" ]]; then
    echo "🐍 Активируем виртуальную среду..."
    source .venv/bin/activate
fi

# Показываем статистику ДО очистки
echo "📊 Статистика ПЕРЕД очисткой:"
python memory/clear_memory.py --stats "$SESSION_ID"

echo ""
echo "🧹 Выполняем очистку..."

# Очищаем память
python memory/clear_memory.py --clear "$SESSION_ID"

echo ""
echo "📊 Статистика ПОСЛЕ очистки:"
python memory/clear_memory.py --stats "$SESSION_ID"

echo ""
echo "✅ Готово! Теперь можно запускать Text-to-SQL пайплайн с чистой памятью."
