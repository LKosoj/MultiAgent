"""
Conditional Engine для безопасного выполнения условий в workflow
"""
import logging
import re
from typing import Dict, Any, List, Optional, Union
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class ConditionOperator(Enum):
    """Поддерживаемые операторы в условиях"""
    EQUALS = "=="
    NOT_EQUALS = "!="
    GREATER = ">"
    GREATER_EQUAL = ">="
    LESS = "<"
    LESS_EQUAL = "<="
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    IN = "in"
    NOT_IN = "not_in"
    AND = "and"
    OR = "or"
    NOT = "not"


class ConditionParser:
    """Безопасный парсер условий"""
    
    def __init__(self):
        # Разрешенные переменные в условиях
        self.allowed_variables = {
            "quality_score", "step_status", "error_count", "retry_count",
            "execution_time", "cost", "tokens_used", "validator_passed",
            "agent_available", "budget_remaining", "loop_detected"
        }
        
        # Разрешенные функции
        self.allowed_functions = {
            "len", "str", "int", "float", "bool", "max", "min", "sum"
        }
    
    def parse_condition(self, condition_str: str) -> Dict[str, Any]:
        """Разобрать строку условия в безопасную структуру"""
        
        try:
            # Нормализуем строку
            condition_str = condition_str.strip()
            
            # Простые условия вида "variable operator value"
            simple_pattern = r'^(\w+)\s*(==|!=|>=|<=|>|<|contains|in)\s*(.+)$'
            match = re.match(simple_pattern, condition_str, re.IGNORECASE)
            
            if match:
                variable, operator, value = match.groups()
                
                # Проверяем безопасность переменной
                if variable not in self.allowed_variables:
                    raise ValueError(f"Variable '{variable}' not allowed in conditions")
                
                # Парсим значение
                parsed_value = self._parse_value(value.strip())
                
                return {
                    "type": "simple",
                    "variable": variable,
                    "operator": operator.lower(),
                    "value": parsed_value,
                    "raw_condition": condition_str
                }
            
            # Сложные условия с AND/OR
            if " and " in condition_str.lower() or " or " in condition_str.lower():
                return self._parse_complex_condition(condition_str)
            
            # Если не удалось распарсить
            raise ValueError(f"Unable to parse condition: {condition_str}")
            
        except Exception as e:
            logger.error(f"❌ Failed to parse condition '{condition_str}': {e}")
            raise
    
    def _parse_value(self, value_str: str) -> Any:
        """Безопасно распарсить значение"""
        
        value_str = value_str.strip('\'"')  # Убираем кавычки
        
        # Булевы значения
        if value_str.lower() in ['true', 'false']:
            return value_str.lower() == 'true'
        
        # Числа
        try:
            if '.' in value_str:
                return float(value_str)
            else:
                return int(value_str)
        except ValueError:
            pass
        
        # Строки
        return value_str
    
    def _parse_complex_condition(self, condition_str: str) -> Dict[str, Any]:
        """Парсинг сложных условий с AND/OR"""
        
        # Упрощенный парсер для AND/OR
        # TODO: Реализовать полноценный парсер с поддержкой скобок
        
        if " and " in condition_str.lower():
            parts = re.split(r'\s+and\s+', condition_str, flags=re.IGNORECASE)
            subconditions = [self.parse_condition(part.strip()) for part in parts]
            
            return {
                "type": "and",
                "conditions": subconditions,
                "raw_condition": condition_str
            }
        
        elif " or " in condition_str.lower():
            parts = re.split(r'\s+or\s+', condition_str, flags=re.IGNORECASE)
            subconditions = [self.parse_condition(part.strip()) for part in parts]
            
            return {
                "type": "or", 
                "conditions": subconditions,
                "raw_condition": condition_str
            }
        
        else:
            raise ValueError(f"Complex condition not supported: {condition_str}")


class ConditionalEngine:
    """Движок для выполнения условий в workflow"""
    
    def __init__(self):
        self.parser = ConditionParser()
        self.evaluation_history: List[Dict[str, Any]] = []
        
    async def evaluate_condition(self, condition: str, context: Dict[str, Any]) -> bool:
        """Оценить условие в данном контексте"""
        
        try:
            # Парсим условие
            parsed_condition = self.parser.parse_condition(condition)
            
            # Оцениваем условие
            result = await self._evaluate_parsed_condition(parsed_condition, context)
            
            # Записываем в историю
            self._record_evaluation(condition, context, result, parsed_condition)
            
            logger.debug(f"🔍 Condition '{condition}' evaluated to: {result}")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Failed to evaluate condition '{condition}': {e}")
            # По умолчанию возвращаем False для безопасности
            return False
    
    async def _evaluate_parsed_condition(self, parsed_condition: Dict[str, Any], 
                                        context: Dict[str, Any]) -> bool:
        """Оценить распарсенное условие"""
        
        condition_type = parsed_condition["type"]
        
        if condition_type == "simple":
            return await self._evaluate_simple_condition(parsed_condition, context)
        elif condition_type == "and":
            return await self._evaluate_and_condition(parsed_condition, context)
        elif condition_type == "or":
            return await self._evaluate_or_condition(parsed_condition, context)
        else:
            logger.error(f"❌ Unknown condition type: {condition_type}")
            return False
    
    async def _evaluate_simple_condition(self, condition: Dict[str, Any], 
                                        context: Dict[str, Any]) -> bool:
        """Оценить простое условие"""
        
        variable = condition["variable"]
        operator = condition["operator"]
        expected_value = condition["value"]
        
        # Получаем значение переменной из контекста
        actual_value = self._get_variable_value(variable, context)
        
        if actual_value is None:
            logger.warning(f"⚠️ Variable '{variable}' not found in context")
            return False
        
        # Выполняем сравнение
        return self._compare_values(actual_value, operator, expected_value)
    
    async def _evaluate_and_condition(self, condition: Dict[str, Any],
                                     context: Dict[str, Any]) -> bool:
        """Оценить условие AND"""
        
        for subcondition in condition["conditions"]:
            result = await self._evaluate_parsed_condition(subcondition, context)
            if not result:
                return False  # Если хотя бы одно условие ложно
        
        return True  # Все условия истинны
    
    async def _evaluate_or_condition(self, condition: Dict[str, Any],
                                    context: Dict[str, Any]) -> bool:
        """Оценить условие OR"""
        
        for subcondition in condition["conditions"]:
            result = await self._evaluate_parsed_condition(subcondition, context)
            if result:
                return True  # Если хотя бы одно условие истинно
        
        return False  # Все условия ложны
    
    def _get_variable_value(self, variable: str, context: Dict[str, Any]) -> Any:
        """Получить значение переменной из контекста"""
        
        # Прямое обращение к переменной
        if variable in context:
            return context[variable]
        
        # Поиск в step_result если есть
        if "step_result" in context:
            step_result = context["step_result"]
            
            if variable == "quality_score":
                return getattr(step_result, 'quality_score', 0.0)
            elif variable == "step_status":
                return getattr(step_result, 'status', 'unknown')
            elif variable == "error_count":
                return 1 if getattr(step_result, 'error', None) else 0
            elif variable == "retry_count":
                return getattr(step_result, 'retry_count', 0)
            elif variable == "execution_time":
                return getattr(step_result, 'duration_seconds', 0.0)
        
        # Поиск в validation_result если есть
        if "validation_result" in context:
            validation_result = context["validation_result"]
            
            if variable == "validator_passed":
                return getattr(validation_result, 'validation_passed', False)
        
        # Поиск в budget_status если есть
        if "budget_status" in context:
            budget_status = context["budget_status"]
            
            if variable == "budget_remaining":
                budgets = budget_status.get("budgets", {})
                if budgets:
                    # Возвращаем минимальный остаток по всем типам бюджета
                    remaining_percentages = []
                    for budget_info in budgets.values():
                        remaining = budget_info.get("remaining", 0)
                        limit = budget_info.get("limit", 1)
                        percentage = (remaining / limit) * 100 if limit > 0 else 0
                        remaining_percentages.append(percentage)
                    
                    return min(remaining_percentages) if remaining_percentages else 0
        
        # Дефолтные значения для системных переменных
        defaults = {
            "quality_score": 0.0,
            "step_status": "unknown",
            "error_count": 0,
            "retry_count": 0,
            "execution_time": 0.0,
            "cost": 0.0,
            "tokens_used": 0,
            "validator_passed": False,
            "agent_available": True,
            "budget_remaining": 100.0,
            "loop_detected": False
        }
        
        return defaults.get(variable)
    
    def _compare_values(self, actual: Any, operator: str, expected: Any) -> bool:
        """Сравнить значения согласно оператору"""
        
        try:
            if operator == "==":
                return actual == expected
            elif operator == "!=":
                return actual != expected
            elif operator == ">":
                return float(actual) > float(expected)
            elif operator == ">=":
                return float(actual) >= float(expected)
            elif operator == "<":
                return float(actual) < float(expected)
            elif operator == "<=":
                return float(actual) <= float(expected)
            elif operator == "contains":
                return str(expected).lower() in str(actual).lower()
            elif operator == "not_contains":
                return str(expected).lower() not in str(actual).lower()
            elif operator == "in":
                if isinstance(expected, (list, tuple)):
                    return actual in expected
                else:
                    return str(actual) in str(expected)
            elif operator == "not_in":
                if isinstance(expected, (list, tuple)):
                    return actual not in expected
                else:
                    return str(actual) not in str(expected)
            else:
                logger.error(f"❌ Unknown operator: {operator}")
                return False
                
        except (ValueError, TypeError) as e:
            logger.error(f"❌ Error comparing {actual} {operator} {expected}: {e}")
            return False
    
    def _record_evaluation(self, condition: str, context: Dict[str, Any], 
                          result: bool, parsed_condition: Dict[str, Any]):
        """Записать результат оценки условия"""
        
        evaluation_record = {
            "timestamp": datetime.now().isoformat(),
            "condition": condition,
            "result": result,
            "parsed_condition": parsed_condition,
            "context_keys": list(context.keys())
        }
        
        self.evaluation_history.append(evaluation_record)
        
        # Ограничиваем размер истории
        if len(self.evaluation_history) > 1000:
            self.evaluation_history = self.evaluation_history[-500:]
    
    def get_evaluation_statistics(self) -> Dict[str, Any]:
        """Получить статистику оценки условий"""
        
        if not self.evaluation_history:
            return {"message": "No evaluation history available"}
        
        total_evaluations = len(self.evaluation_history)
        true_results = sum(1 for r in self.evaluation_history if r["result"])
        false_results = total_evaluations - true_results
        
        # Анализ по типам условий
        condition_types = {}
        for record in self.evaluation_history:
            condition_type = record["parsed_condition"]["type"]
            if condition_type not in condition_types:
                condition_types[condition_type] = {"total": 0, "true": 0}
            
            condition_types[condition_type]["total"] += 1
            if record["result"]:
                condition_types[condition_type]["true"] += 1
        
        # Добавляем процентные соотношения
        for stats in condition_types.values():
            stats["true_percentage"] = (stats["true"] / stats["total"]) * 100
        
        return {
            "total_evaluations": total_evaluations,
            "true_results": true_results,
            "false_results": false_results,
            "true_percentage": (true_results / total_evaluations) * 100,
            "condition_types": condition_types,
            "recent_evaluations": self.evaluation_history[-10:]  # Последние 10
        }
    
    async def determine_next_steps(self, current_step_result: Dict[str, Any],
                                  workflow_definition: Dict[str, Any],
                                  context: Dict[str, Any]) -> List[str]:
        """Определить следующие шаги на основе условий"""
        
        next_steps = []
        
        # Проходим по всем шагам в workflow
        for step in workflow_definition.get("steps", []):
            step_id = step.get("id")
            condition = step.get("condition")
            
            # Если у шага нет условия, добавляем его
            if not condition:
                next_steps.append(step_id)
                continue
            
            # Оцениваем условие
            try:
                should_execute = await self.evaluate_condition(condition, {
                    **context,
                    "step_result": current_step_result
                })
                
                if should_execute:
                    next_steps.append(step_id)
                    logger.info(f"✅ Step '{step_id}' condition passed: {condition}")
                else:
                    logger.info(f"⏸️ Step '{step_id}' condition failed: {condition}")
                    
            except Exception as e:
                logger.error(f"❌ Error evaluating condition for step '{step_id}': {e}")
                # По умолчанию не выполняем шаг при ошибке условия
        
        return next_steps
    
    def validate_condition_syntax(self, condition: str) -> Dict[str, Any]:
        """Валидировать синтаксис условия"""
        
        try:
            parsed = self.parser.parse_condition(condition)
            return {
                "valid": True,
                "parsed_condition": parsed,
                "message": "Condition syntax is valid"
            }
        except Exception as e:
            return {
                "valid": False,
                "error": str(e),
                "message": f"Invalid condition syntax: {e}"
            }
    
    def get_supported_variables(self) -> List[str]:
        """Получить список поддерживаемых переменных"""
        return list(self.parser.allowed_variables)
    
    def get_supported_operators(self) -> List[str]:
        """Получить список поддерживаемых операторов"""
        return [op.value for op in ConditionOperator]
