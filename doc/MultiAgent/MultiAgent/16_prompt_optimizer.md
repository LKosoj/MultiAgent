# Глава 16: Оптимизатор промптов (PromptOptimizer)

Автоматически улучшает системные инструкции агентов под конкретную модель (семейство/версию), повышая качество и стабильность ответов.

## Зачем
- Снимает рутину промпт‑инжиниринга и адаптации под разные LLM.
- Делает инструкции структурированными и воспроизводимыми.
- Хранит метаданные оптимизаций и бэкапит профили.

## Режимы запуска
- Автоматически при старте UI (optimize_all_agents()).
- Из CLI c предпросмотром:
```bash
python prompt_optimizer/optimize_agents.py --list
python prompt_optimizer/optimize_agents.py --agents researcher --dry-run
python prompt_optimizer/optimize_agents.py --agents researcher
```

## Как работает
Идея: «ИИ улучшает инструкции для ИИ». Оптимизатор строит мета‑промпт с контекстом (модель, инструменты, лучшие практики) и просит модель переписать базовый промпт.

Итерация по агентам и защита от повторной оптимизации:
```python
def optimize_all_agents(self):
    for agent_name in agents_to_optimize:
        profile = AGENT_PROFILES[agent_name]
        model_info = self.get_model_info(profile.get('model'))
        if profile.get('optimization_metadata', {}).get('optimizer_model') == model_info['model_name']:
            continue
        # ... выполнить optimize_prompt и update_profile ...
```

Мета‑промпт (идея):
```text
## Task
Re-write Baseline Prompt into Revised Prompt using Best Practices.
CRITICAL: You optimize a prompt FOR YOURSELF (your model family/version).
## Agent Context
- Agent Name: <name>
- Model: <family/version>
- Tools: <list>
## Best Practices
- структура, формат вывода, примеры, валидация...
## Baseline Prompt
<original_prompt>
```

Сохранение и бэкап:
```python
def update_profile(...):
    backup(original_yaml)
    profile['prompt_templates'] = revised_prompt
    profile['optimization_metadata'] = {
        'optimizer_model': model_info['model_name'],
        'optimized_at': now_iso,
    }
    save_yaml(profile_path, profile)
```

## Практика
- Используйте --dry-run для сравнения «до/после» без записи.
- Запускайте повторно при смене модели — оптимизатор адаптирует промпт под новое семейство.

## Вывод
PromptOptimizer автоматизирует улучшение инструкций: быстрее, качественнее и с учётом конкретной LLM, сохраняя историю изменений.
