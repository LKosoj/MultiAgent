"""
Contract Registry для управления схемами артефактов и валидаторами
"""
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

from ..models import Contract
from .validators import get_validator, VALIDATOR_REGISTRY
from .schemas import get_default_schemas

logger = logging.getLogger(__name__)


class ContractRegistry:
    """Реестр контрактов и валидаторов"""
    
    def __init__(self):
        self.contracts: Dict[str, Contract] = {}
        self.validation_results_cache: Dict[str, Dict[str, Any]] = {}
        
        self._load_default_contracts()
    
    def _load_default_contracts(self):
        """Загрузка дефолтных контрактов"""
        try:
            default_schemas = get_default_schemas()
            
            for name, schema_data in default_schemas.items():
                contract = Contract(
                    name=schema_data["name"],
                    version=schema_data["version"],
                    schema=schema_data["schema"],
                    business_rules=schema_data["business_rules"],
                    quality_thresholds=schema_data["quality_thresholds"],
                    validators=schema_data["validators"]
                )
                
                self.contracts[name] = contract
                logger.info(f"📋 Loaded contract {name} v{contract.version}")
                
        except Exception as e:
            logger.error(f"❌ Failed to load default contracts: {e}")
    
    def get_contract(self, name: str) -> Optional[Contract]:
        """Получить контракт по имени"""
        return self.contracts.get(name)
    
    def register_contract(self, contract: Contract):
        """Зарегистрировать новый контракт"""
        self.contracts[contract.name] = contract
        logger.info(f"📋 Registered contract {contract.name} v{contract.version}")
    
    def get_contract_for_step(self, step_type: str, agent_type: str) -> Contract:
        """Получить подходящий контракт для типа шага/агента"""
        
        # Маппинг agent_type -> contract_name
        agent_contract_mapping = {
            "sql_generator_agent": "sql_query",
            "analyst": "analysis_report", 
            "researcher": "research_output",
            "code_executor": "text_output"
        }
        
        contract_name = agent_contract_mapping.get(agent_type, "text_output")
        contract = self.get_contract(contract_name)
        
        if not contract:
            # Fallback к базовому контракту
            contract = self.get_contract("text_output")
            
        return contract
    
    async def validate_artifact(self, artifact: Any, contract: Contract,
                               enabled_validators: List[str] = None) -> Dict[str, Any]:
        """Валидировать артефакт согласно контракту"""
        
        if enabled_validators is None:
            enabled_validators = contract.validators
        
        validation_results = []
        overall_score = 0.0
        total_weight = 0.0
        validation_passed = True
        
        # Подготавливаем контракт для валидаторов
        contract_dict = {
            "schema": contract.schema,
            "business_rules": contract.business_rules,
            "quality_thresholds": contract.quality_thresholds
        }
        
        # Запускаем каждый валидатор
        for validator_name in enabled_validators:
            validator = get_validator(validator_name)
            if not validator:
                logger.warning(f"⚠️ Validator {validator_name} not found")
                continue
            
            try:
                # Добавляем конфигурацию валидатора в контракт
                contract_dict[validator_name] = self._get_validator_config(
                    validator_name, contract
                )
                
                result = await validator.validate(artifact, contract_dict)
                validation_results.append(result)
                
                # Вычисляем взвешенную оценку
                weight = self._get_validator_weight(validator_name)
                overall_score += result["score"] * weight
                total_weight += weight
                
                if not result["passed"]:
                    validation_passed = False
                    
                logger.debug(f"✅ {validator_name}: {result['score']:.2f} - {result['message']}")
                
            except Exception as e:
                logger.error(f"❌ Validator {validator_name} failed: {e}")
                validation_results.append({
                    "validator_name": validator_name,
                    "passed": False,
                    "score": 0.0,
                    "message": f"Validator error: {e}",
                    "timestamp": datetime.now().isoformat()
                })
                validation_passed = False
        
        # Финальная оценка
        if total_weight > 0:
            overall_score = overall_score / total_weight
        else:
            overall_score = 0.0
            
        # Проверяем соответствие порогам качества
        min_threshold = contract.quality_thresholds.get("min_score", 0.7)
        threshold_met = overall_score >= min_threshold
        
        result = {
            "contract_name": contract.name,
            "contract_version": contract.version,
            "overall_score": overall_score,
            "validation_passed": validation_passed and threshold_met,
            "threshold_met": threshold_met,
            "min_threshold": min_threshold,
            "validator_results": validation_results,
            "validation_time": datetime.now().isoformat(),
            "artifact_hash": str(hash(str(artifact)))
        }
        
        # Кэшируем результат
        cache_key = f"{contract.name}:{result['artifact_hash']}"
        self.validation_results_cache[cache_key] = result
        
        return result
    
    def _get_validator_config(self, validator_name: str, contract: Contract) -> Dict[str, Any]:
        """Получить конфигурацию для валидатора"""
        
        # Базовые конфигурации валидаторов
        base_configs = {
            "structural": {
                "enabled": True,
                "required_fields": ["output"],
                "min_length": 10,
                "json_format": False
            },
            "completeness": {
                "enabled": True,
                "coverage_threshold": 0.8,
                "expected_keywords": [],
                "expected_sections": [],
                "min_sentences": 3
            },
            "security": {
                "enabled": True,
                "sql_injection_check": True,
                "xss_check": True,
                "command_injection_check": True,
                "data_leak_check": True
            },
            "semantic": {
                "enabled": False,
                "fact_check": False,
                "hallucination_check": False
            }
        }
        
        # Получаем базовую конфигурацию
        config = base_configs.get(validator_name, {"enabled": True})
        
        # Адаптируем под конкретный контракт
        if contract.name == "sql_query" and validator_name == "structural":
            config.update({
                "required_fields": ["query", "explanation"],
                "json_format": True
            })
        elif contract.name == "analysis_report" and validator_name == "completeness":
            config.update({
                "expected_sections": ["summary", "findings", "methodology"],
                "min_sentences": 10
            })
        elif contract.name == "research_output" and validator_name == "completeness":
            config.update({
                "expected_sections": ["key_findings", "sources"],
                "min_sentences": 5
            })
        
        return config
    
    def _get_validator_weight(self, validator_name: str) -> float:
        """Получить вес валидатора для общей оценки"""
        weights = {
            "structural": 0.2,
            "completeness": 0.3,
            "security": 0.3,
            "semantic": 0.2
        }
        return weights.get(validator_name, 0.1)
    
    def get_available_validators(self) -> List[str]:
        """Получить список доступных валидаторов"""
        return list(VALIDATOR_REGISTRY.keys())
    
    def get_available_contracts(self) -> List[str]:
        """Получить список доступных контрактов"""
        return list(self.contracts.keys())
    
    def get_validation_stats(self) -> Dict[str, Any]:
        """Получить статистику валидации"""
        if not self.validation_results_cache:
            return {"message": "No validation results yet"}
        
        results = list(self.validation_results_cache.values())
        
        total_validations = len(results)
        passed_validations = sum(1 for r in results if r["validation_passed"])
        avg_score = sum(r["overall_score"] for r in results) / total_validations
        
        # Статистика по валидаторам
        validator_stats = {}
        for result in results:
            for validator_result in result["validator_results"]:
                validator_name = validator_result["validator_name"]
                if validator_name not in validator_stats:
                    validator_stats[validator_name] = {
                        "count": 0,
                        "passed": 0,
                        "total_score": 0.0
                    }
                
                stats = validator_stats[validator_name]
                stats["count"] += 1
                stats["total_score"] += validator_result["score"]
                if validator_result["passed"]:
                    stats["passed"] += 1
        
        # Вычисляем средние значения
        for validator_name, stats in validator_stats.items():
            stats["pass_rate"] = stats["passed"] / stats["count"]
            stats["avg_score"] = stats["total_score"] / stats["count"]
        
        return {
            "total_validations": total_validations,
            "pass_rate": passed_validations / total_validations,
            "average_score": avg_score,
            "validator_performance": validator_stats,
            "available_contracts": self.get_available_contracts(),
            "available_validators": self.get_available_validators()
        }
