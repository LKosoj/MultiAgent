from utils import validate_mermaid

def validate_mermaid_diagram(diagram_content: str) -> str:
    """Проверяет корректность синтаксиса Mermaid диаграммы.
    
    Args:
        diagram_content: Содержимое Mermaid диаграммы для проверки
    Returns:
        str: Результат валидации - "КОРРЕКТНАЯ" если диаграмма валидна, иначе сообщение об ошибке
    """
    valid, error = validate_mermaid(diagram_content)
    
    if valid:
        return "КОРРЕКТНАЯ: Синтаксис диаграммы корректен"
    else:
        return f"ОШИБКА: {error}" 