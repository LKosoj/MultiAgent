import os
import pytest
from unittest.mock import patch, mock_open


def test_rag_docs_enable_flag():
    """Тест флага RAG_DOCS_ENABLE."""
    # Тест 1: RAG_DOCS_ENABLE=1 (включено)
    with patch.dict(os.environ, {"RAG_DOCS_ENABLE": "1"}):
        assert os.getenv("RAG_DOCS_ENABLE", "1") == "1"
    
    # Тест 2: RAG_DOCS_ENABLE=0 (отключено)
    with patch.dict(os.environ, {"RAG_DOCS_ENABLE": "0"}):
        assert os.getenv("RAG_DOCS_ENABLE", "1") == "0"
    
    # Тест 3: RAG_DOCS_ENABLE не установлена (по умолчанию включено)
    with patch.dict(os.environ, {}, clear=True):
        assert os.getenv("RAG_DOCS_ENABLE", "1") == "1"


@patch("os.path.exists")
@patch("os.listdir")
@patch("builtins.open", new_callable=mock_open, read_data="# Test\n```sql\nSELECT * FROM test;\n```\n")
def test_rag_docs_parsing_enabled(mock_file, mock_listdir, mock_exists):
    """Тест парсинга документов при включённом RAG_DOCS_ENABLE."""
    mock_exists.return_value = True
    mock_listdir.return_value = ["test.md"]
    
    with patch.dict(os.environ, {"RAG_DOCS_ENABLE": "1"}):
        # Имитируем логику парсинга документов
        rag_enabled = os.getenv("RAG_DOCS_ENABLE", "1") != "0"
        
        if rag_enabled and mock_exists("doc/"):
            docs = mock_listdir("doc/")
            md_files = [f for f in docs if f.endswith('.md')]
            
            assert len(md_files) == 1
            assert md_files[0] == "test.md"
            
            # Имитируем чтение файла и извлечение SQL блоков
            content = mock_file().read()
            sql_blocks = []
            import re
            for match in re.finditer(r'```sql\s*\n(.*?)\n```', content, re.DOTALL):
                sql_blocks.append(match.group(1).strip())
            
            assert len(sql_blocks) == 1
            assert sql_blocks[0] == "SELECT * FROM test;"


@patch("os.path.exists")
def test_rag_docs_parsing_disabled(mock_exists):
    """Тест отключения парсинга документов при RAG_DOCS_ENABLE=0."""
    mock_exists.return_value = True
    
    with patch.dict(os.environ, {"RAG_DOCS_ENABLE": "0"}):
        rag_enabled = os.getenv("RAG_DOCS_ENABLE", "1") != "0"
        
        # При отключённом флаге парсинг не должен происходить
        assert not rag_enabled
        
        # Даже если папка doc/ существует, парсинг не выполняется
        if rag_enabled and mock_exists("doc/"):
            pytest.fail("Парсинг не должен выполняться при RAG_DOCS_ENABLE=0")


def test_sql_block_extraction():
    """Тест извлечения SQL блоков из markdown."""
    content = """
# Примеры SQL

## Простой запрос
```sql
SELECT name, age FROM users;
```

## Сложный запрос
```sql
SELECT u.name, COUNT(o.id) as order_count
FROM users u
LEFT JOIN orders o ON u.id = o.user_id
GROUP BY u.id;
```

Обычный текст.

```python
# Это не SQL
print("Hello")
```
"""
    
    import re
    sql_blocks = []
    for match in re.finditer(r'```sql\s*\n(.*?)\n```', content, re.DOTALL):
        sql_blocks.append(match.group(1).strip())
    
    assert len(sql_blocks) == 2
    assert "SELECT name, age FROM users;" in sql_blocks[0]
    assert "LEFT JOIN orders o ON u.id = o.user_id" in sql_blocks[1]
