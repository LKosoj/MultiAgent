"""Text-to-SQL слой проекта MultiAgent.

Поверхностный пакет: реальная реализация распределена по подпакетам.

Основные точки входа:
    - ``custom_tools.text_to_sql.core`` — фасадные функции для AG-UI (sql_explain, secure_db_executor и др.).
    - ``custom_tools.text_to_sql.validators`` — валидация SQL и лимитирование схемы.
    - ``custom_tools.text_to_sql.rag`` — RAG search для примеров и описаний колонок.
    - ``custom_tools.text_to_sql.schema_linking`` — связывание сущностей и таблиц.

Подробное описание публичного API: ``doc/TEXT_TO_SQL_API.md``.
Конфигурация yaml-файлов: ``doc/TEXT_TO_SQL_CONFIG.md``.
"""
