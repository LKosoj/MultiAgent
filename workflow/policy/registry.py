"""
Policy Registry для управления версиями политик
"""
import logging
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime

from ..models import Policy
from .defaults import get_default_policy

logger = logging.getLogger(__name__)


class PolicyRegistry:
    """Реестр политик с версионированием и Shadow Mode"""
    
    def __init__(self, config_dir: str = "workflow/config/policies"):
        self.config_dir = Path(config_dir)
        self.policies: Dict[str, Policy] = {}
        self.active_version = "default"
        self.shadow_versions: List[str] = []
        self.policy_usage_stats: Dict[str, Dict[str, Any]] = {}
        
        self._load_policies()
    
    def _load_policies(self):
        """Загрузка политик из конфигурационных файлов"""
        try:
            # Создаем директорию если не существует
            self.config_dir.mkdir(parents=True, exist_ok=True)
            
            # Загружаем дефолтную политику
            self.policies["default"] = get_default_policy()
            
            # Загружаем политики из YAML файлов
            for policy_file in self.config_dir.glob("*.yaml"):
                try:
                    with open(policy_file, 'r', encoding='utf-8') as f:
                        policy_data = yaml.safe_load(f)
                    
                    policy = Policy(**policy_data)
                    self.policies[policy.version] = policy
                    logger.info(f"📋 Loaded policy {policy.name} v{policy.version}")
                    
                except Exception as e:
                    logger.error(f"❌ Failed to load policy from {policy_file}: {e}")
                    
        except Exception as e:
            logger.error(f"❌ Failed to initialize PolicyRegistry: {e}")
            # Fallback к дефолтной политике
            self.policies["default"] = get_default_policy()
    
    def get_policy(self, version: str = None) -> Policy:
        """Получить политику по версии"""
        if version is None:
            version = self.active_version
            
        if version not in self.policies:
            logger.warning(f"⚠️ Policy version {version} not found, using default")
            version = "default"
            
        return self.policies[version]
    
    def set_active_version(self, version: str):
        """Установить активную версию политики"""
        if version not in self.policies:
            raise ValueError(f"Policy version {version} not found")
        
        old_version = self.active_version
        self.active_version = version
        logger.info(f"🔄 Changed active policy from {old_version} to {version}")
    
    def add_shadow_version(self, version: str):
        """Добавить версию в Shadow Mode для тестирования"""
        if version not in self.policies:
            raise ValueError(f"Policy version {version} not found")
            
        if version not in self.shadow_versions:
            self.shadow_versions.append(version)
            logger.info(f"🌒 Added policy {version} to shadow mode")
    
    def remove_shadow_version(self, version: str):
        """Убрать версию из Shadow Mode"""
        if version in self.shadow_versions:
            self.shadow_versions.remove(version)
            logger.info(f"🌕 Removed policy {version} from shadow mode")
    
    def evaluate_policy(self, version: str, context: Dict[str, Any], 
                       shadow_mode: bool = False) -> Dict[str, Any]:
        """Оценить политику и записать результат"""
        policy = self.get_policy(version)
        
        # Записываем статистику использования
        if version not in self.policy_usage_stats:
            self.policy_usage_stats[version] = {
                "usage_count": 0,
                "shadow_evaluations": 0,
                "last_used": None
            }
        
        stats = self.policy_usage_stats[version]
        
        if shadow_mode:
            stats["shadow_evaluations"] += 1
            logger.debug(f"🌒 [SHADOW] Evaluating policy {version}")
        else:
            stats["usage_count"] += 1
            stats["last_used"] = datetime.now().isoformat()
        
        # Здесь будет логика оценки политики
        # Пока возвращаем базовую структуру
        result = {
            "policy_version": version,
            "shadow_mode": shadow_mode,
            "evaluation_time": datetime.now().isoformat(),
            "context_hash": str(hash(str(context))),
            "quality_gates": policy.quality_gates,
            "applied_rules": []
        }
        
        return result
    
    def get_usage_stats(self) -> Dict[str, Any]:
        """Получить статистику использования политик"""
        return {
            "active_version": self.active_version,
            "shadow_versions": self.shadow_versions,
            "available_policies": list(self.policies.keys()),
            "usage_stats": self.policy_usage_stats
        }
    
    def save_policy(self, policy: Policy, filename: str = None):
        """Сохранить политику в файл"""
        if filename is None:
            filename = f"{policy.name}_v{policy.version}.yaml"
        
        filepath = self.config_dir / filename
        
        try:
            policy_dict = {
                "name": policy.name,
                "version": policy.version,
                "quality_gates": policy.quality_gates,
                "validation_rules": policy.validation_rules,
                "retry_policies": policy.retry_policies,
                "budgets": policy.budgets,
                "escalation": policy.escalation,
                "metadata": policy.metadata
            }
            
            with open(filepath, 'w', encoding='utf-8') as f:
                yaml.dump(policy_dict, f, default_flow_style=False, allow_unicode=True, indent=2)
            
            # Добавляем в реестр
            self.policies[policy.version] = policy
            logger.info(f"💾 Saved policy {policy.name} v{policy.version} to {filepath}")
            
        except Exception as e:
            logger.error(f"❌ Failed to save policy: {e}")
            raise
