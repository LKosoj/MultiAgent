#!/usr/bin/env python3
"""CLI для оптимизации промптов агентов"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

project_root = Path(__file__).parent.parent
sys.path.append(str(project_root))
os.chdir(project_root)

from prompt_optimizer import PromptOptimizer
from agent_command import AGENT_PROFILES

def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format='%(levelname)s - %(message)s')

def list_available_agents():
    import os
    import yaml
    
    # Читаем все агенты из файлов напрямую
    all_agents = {}
    profile_dir = project_root / "agent_profiles"
    
    for filename in os.listdir(profile_dir):
        if filename.endswith('.yaml'):
            agent_name = filename[:-5]
            with open(profile_dir / filename, 'r', encoding='utf-8') as f:
                profile_data = yaml.safe_load(f)
                all_agents[agent_name] = profile_data
    
    # Разделяем на включенных и отключенных
    enabled = [name for name, profile in all_agents.items() if profile.get('enable', True)]
    disabled = [name for name, profile in all_agents.items() if not profile.get('enable', True)]
    
    print(f"\nВключенные агенты ({len(enabled)}):")
    for agent in sorted(enabled):
        profile = all_agents[agent]
        # Для включенных агентов берем модель из AGENT_PROFILES (там она уже обработана)
        if agent in AGENT_PROFILES:
            model_id = getattr(AGENT_PROFILES[agent].get('model'), 'model_id', 'unknown') if AGENT_PROFILES[agent].get('model') else 'unknown'
        else:
            model_id = profile.get('model', 'unknown')
        agent_type = profile.get('type', 'code')
        print(f"  • {agent} ({agent_type}) - {model_id}")
    
    if disabled:
        print(f"\nОтключенные агенты ({len(disabled)}):")
        for agent in sorted(disabled):
            profile = all_agents[agent]
            model_id = profile.get('model', 'unknown')
            agent_type = profile.get('type', 'code')
            print(f"  • {agent} ({agent_type}) - {model_id}")

def validate_agents(agent_names: List[str]) -> List[str]:
    valid = [name for name in agent_names if name in AGENT_PROFILES]
    invalid = [name for name in agent_names if name not in AGENT_PROFILES]
    
    if invalid:
        print(f"Неизвестные агенты: {', '.join(invalid)}")
        return []
    
    return valid

def main():
    parser = argparse.ArgumentParser(description="Оптимизация промптов агентов")
    parser.add_argument('--list', '-l', action='store_true', help='Список агентов')
    parser.add_argument('--all', '-a', action='store_true', help='Оптимизировать всех')
    parser.add_argument('--agents', nargs='+', help='Конкретные агенты')
    parser.add_argument('--verbose', '-v', action='store_true', help='Подробный вывод')
    parser.add_argument('--dry-run', action='store_true', help='Предварительный просмотр без сохранения')
    args = parser.parse_args()
    setup_logging(args.verbose)
    
    if args.list:
        list_available_agents()
        return
    
    if not args.all and not args.agents:
        print("Необходимо указать --all или --agents")
        return
    
    agents_to_optimize = None
    if args.agents:
        agents_to_optimize = validate_agents(args.agents)
        if not agents_to_optimize:
            return
    
    if args.dry_run:
        print("🔍 РЕЖИМ ПРЕДВАРИТЕЛЬНОГО ПРОСМОТРА")
        print("Изменения НЕ будут сохранены в файлы профилей")
        print()
    
    try:
        optimizer = PromptOptimizer()
        results = optimizer.optimize_all_agents(agents_to_optimize, dry_run=args.dry_run)
        
        from datetime import datetime
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        mode_suffix = "_preview" if args.dry_run else ""
        report_path = f"prompt_optimization_report{mode_suffix}_{timestamp}.md"
        optimizer.generate_optimization_report(results, report_path)
        
        mode_text = "Предварительный просмотр" if args.dry_run else "Оптимизация"
        print(f"\n{mode_text} завершен:")
        print(f"- Успешно: {results['optimized_successfully']}")
        print(f"- Ошибки: {results['failed_optimizations']}")
        if results.get('skipped_already_optimized', 0) > 0:
            print(f"- Пропущено (уже оптимизированы): {results['skipped_already_optimized']}")
        print(f"- Отчет: {report_path}")
        
        if args.dry_run:
            print("\n💡 Для применения изменений запустите без --dry-run")
            print("\n" + "="*60)
            print("📄 СОДЕРЖАНИЕ ОТЧЕТА:")
            print("="*60)
            report_content = optimizer.generate_optimization_report(results)
            print(report_content)
        
    except Exception as e:
        print(f"Ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
