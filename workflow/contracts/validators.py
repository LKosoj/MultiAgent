"""
Базовые валидаторы для артефактов workflow
"""
import json
import re
import logging
from typing import Dict, Any, List, Optional
from abc import ABC, abstractmethod
from datetime import datetime

logger = logging.getLogger(__name__)


class BaseValidator(ABC):
    """Базовый класс для валидаторов"""
    
    def __init__(self, name: str):
        self.name = name
        
    @abstractmethod
    async def validate(self, artifact: Any, contract: Dict[str, Any]) -> Dict[str, Any]:
        """Валидировать артефакт согласно контракту"""
        pass
        
    def _create_result(self, passed: bool, score: float, message: str, 
                      details: Dict = None) -> Dict[str, Any]:
        """Создать результат валидации"""
        return {
            "validator_name": self.name,
            "passed": passed,
            "score": score,
            "message": message,
            "details": details or {},
            "timestamp": datetime.now().isoformat()
        }


class StructuralValidator(BaseValidator):
    """Валидатор структуры и формата"""
    
    def __init__(self):
        super().__init__("structural")
        
    async def validate(self, artifact: Any, contract: Dict[str, Any]) -> Dict[str, Any]:
        """Проверить структуру артефакта"""
        try:
            config = contract.get("structural", {})
            
            if not config.get("enabled", True):
                return self._create_result(True, 1.0, "Structural validation disabled")
            
            score = 1.0
            issues = []
            
            # Проверка обязательных полей
            required_fields = config.get("required_fields", [])
            if isinstance(artifact, dict):
                for field in required_fields:
                    if field not in artifact:
                        issues.append(f"Missing required field: {field}")
                        score -= 0.2
                    elif not artifact[field]:
                        issues.append(f"Empty required field: {field}")
                        score -= 0.1
            elif isinstance(artifact, str):
                if "output" in required_fields and not artifact.strip():
                    issues.append("Empty output string")
                    score -= 0.5
            
            # Проверка минимальной длины
            min_length = config.get("min_length", 0)
            content_length = len(str(artifact))
            if content_length < min_length:
                issues.append(f"Content too short: {content_length} < {min_length}")
                score -= 0.3
            
            # Проверка формата JSON если требуется
            if config.get("json_format", False):
                try:
                    if isinstance(artifact, str):
                        json.loads(artifact)
                except json.JSONDecodeError as e:
                    issues.append(f"Invalid JSON format: {e}")
                    score -= 0.4
            
            score = max(0.0, score)
            passed = score >= 0.7 and len(issues) == 0
            
            message = "Structural validation passed" if passed else f"Issues found: {'; '.join(issues)}"
            
            return self._create_result(
                passed=passed,
                score=score,
                message=message,
                details={
                    "issues": issues,
                    "content_length": content_length,
                    "required_fields_checked": required_fields
                }
            )
            
        except Exception as e:
            logger.error(f"❌ Structural validation error: {e}")
            return self._create_result(False, 0.0, f"Validation error: {e}")


class CompletenessValidator(BaseValidator):
    """Валидатор полноты контента"""
    
    def __init__(self):
        super().__init__("completeness")
        
    async def validate(self, artifact: Any, contract: Dict[str, Any]) -> Dict[str, Any]:
        """Проверить полноту артефакта"""
        try:
            config = contract.get("completeness", {})
            
            if not config.get("enabled", True):
                return self._create_result(True, 1.0, "Completeness validation disabled")
            
            score = 1.0
            issues = []
            
            content = str(artifact)
            
            # Проверка минимального покрытия ключевых слов
            coverage_threshold = config.get("coverage_threshold", 0.8)
            expected_keywords = config.get("expected_keywords", [])
            
            if expected_keywords:
                found_keywords = []
                for keyword in expected_keywords:
                    if keyword.lower() in content.lower():
                        found_keywords.append(keyword)
                
                coverage = len(found_keywords) / len(expected_keywords)
                
                if coverage < coverage_threshold:
                    issues.append(f"Low keyword coverage: {coverage:.2f} < {coverage_threshold}")
                    score -= 0.3
            
            # Проверка наличия основных разделов
            expected_sections = config.get("expected_sections", [])
            if expected_sections:
                missing_sections = []
                for section in expected_sections:
                    # Ищем заголовки или ключевые фразы
                    section_patterns = [
                        f"## {section}",
                        f"# {section}", 
                        f"{section}:",
                        section.lower()
                    ]
                    
                    found = any(pattern in content.lower() for pattern in section_patterns)
                    if not found:
                        missing_sections.append(section)
                
                if missing_sections:
                    issues.append(f"Missing sections: {', '.join(missing_sections)}")
                    score -= 0.2 * len(missing_sections)
            
            # Проверка минимального количества предложений/строк
            min_sentences = config.get("min_sentences", 0)
            if min_sentences > 0:
                sentence_count = len(re.findall(r'[.!?]+', content))
                if sentence_count < min_sentences:
                    issues.append(f"Too few sentences: {sentence_count} < {min_sentences}")
                    score -= 0.2
            
            score = max(0.0, score)
            passed = score >= 0.7 and len(issues) == 0
            
            message = "Completeness validation passed" if passed else f"Issues found: {'; '.join(issues)}"
            
            return self._create_result(
                passed=passed,
                score=score,
                message=message,
                details={
                    "issues": issues,
                    "content_length": len(content),
                    "coverage_score": coverage if 'coverage' in locals() else None,
                    "found_keywords": found_keywords if 'found_keywords' in locals() else []
                }
            )
            
        except Exception as e:
            logger.error(f"❌ Completeness validation error: {e}")
            return self._create_result(False, 0.0, f"Validation error: {e}")


class SecurityValidator(BaseValidator):
    """Валидатор безопасности"""
    
    def __init__(self):
        super().__init__("security")
        
    async def validate(self, artifact: Any, contract: Dict[str, Any]) -> Dict[str, Any]:
        """Проверить безопасность артефакта"""
        try:
            config = contract.get("security", {})
            
            if not config.get("enabled", True):
                return self._create_result(True, 1.0, "Security validation disabled")
            
            content = str(artifact).lower()
            score = 1.0
            threats = []
            
            # SQL Injection проверки
            # Для контракта sql_query паттерны DML (SELECT/INSERT/DELETE/UPDATE/UNION)
            # являются ожидаемым выводом агента, а не инъекцией — пропускаем проверку.
            is_sql_output = contract.get("name") == "sql_query" or config.get("is_sql_output", False)
            if config.get("sql_injection_check", True) and not is_sql_output:
                sql_patterns = [
                    r"union\s+select",
                    r"drop\s+table",
                    r"delete\s+from",
                    r"insert\s+into",
                    r"update\s+.*\s+set",
                    r"exec\s*\(",
                    r"script\s*>",
                    r"<\s*script"
                ]

                for pattern in sql_patterns:
                    if re.search(pattern, content):
                        threats.append(f"Potential SQL injection: {pattern}")
                        score -= 0.3
            
            # XSS проверки
            if config.get("xss_check", True):
                xss_patterns = [
                    r"<\s*script",
                    r"javascript\s*:",
                    r"onerror\s*=",
                    r"onload\s*=",
                    r"onclick\s*="
                ]
                
                for pattern in xss_patterns:
                    if re.search(pattern, content):
                        threats.append(f"Potential XSS: {pattern}")
                        score -= 0.2
            
            # Проверка потенциально опасных команд
            if config.get("command_injection_check", True):
                dangerous_commands = [
                    "rm -rf", "del /f", "format c:",
                    "shutdown", "reboot", "halt",
                    "wget", "curl", "nc -l"
                ]
                
                for cmd in dangerous_commands:
                    if cmd in content:
                        threats.append(f"Dangerous command: {cmd}")
                        score -= 0.4
            
            # Проверка утечки чувствительных данных
            if config.get("data_leak_check", True):
                sensitive_patterns = [
                    r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b",  # Credit cards
                    r"\b\d{3}-\d{2}-\d{4}\b",  # SSN
                    r"password\s*[=:]\s*\w+",
                    r"api[_-]?key\s*[=:]\s*\w+",
                    r"secret\s*[=:]\s*\w+"
                ]
                
                for pattern in sensitive_patterns:
                    if re.search(pattern, content):
                        threats.append(f"Potential data leak: {pattern}")
                        score -= 0.5
            
            score = max(0.0, score)
            passed = len(threats) == 0
            
            message = "Security validation passed" if passed else f"Security threats found: {'; '.join(threats)}"
            
            return self._create_result(
                passed=passed,
                score=score,
                message=message,
                details={
                    "threats": threats,
                    "threat_count": len(threats),
                    "checks_performed": [
                        check for check in ["sql_injection", "xss", "command_injection", "data_leak"]
                        if config.get(f"{check}_check", True)
                    ]
                }
            )
            
        except Exception as e:
            logger.error(f"❌ Security validation error: {e}")
            return self._create_result(False, 0.0, f"Validation error: {e}")


class SemanticValidator(BaseValidator):
    """Валидатор семантической корректности (будет использовать LLM)"""
    
    def __init__(self):
        super().__init__("semantic")
        
    async def validate(self, artifact: Any, contract: Dict[str, Any]) -> Dict[str, Any]:
        """Проверить семантическую корректность"""
        try:
            config = contract.get("semantic", {})
            
            if not config.get("enabled", False):
                return self._create_result(True, 1.0, "Semantic validation disabled")
            
            # TODO: Реализовать LLM-based семантическую валидацию
            # Пока возвращаем заглушку
            return self._create_result(
                passed=True,
                score=0.8,
                message="Semantic validation not yet implemented",
                details={"note": "LLM-based validation will be implemented later"}
            )
            
        except Exception as e:
            logger.error(f"❌ Semantic validation error: {e}")
            return self._create_result(False, 0.0, f"Validation error: {e}")


# Реестр валидаторов
VALIDATOR_REGISTRY = {
    "structural": StructuralValidator(),
    "completeness": CompletenessValidator(), 
    "security": SecurityValidator(),
    "semantic": SemanticValidator()
}


def get_validator(name: str) -> Optional[BaseValidator]:
    """Получить валидатор по имени"""
    return VALIDATOR_REGISTRY.get(name)
