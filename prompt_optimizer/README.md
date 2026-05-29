# Оптимизация промптов агентов

Система оптимизации промптов на основе [OpenAI Cookbook](https://github.com/openai/openai-cookbook/blob/main/examples/Prompt_migration_guide.ipynb).

## Использование

```bash
cd prompt_optimizer

# Список агентов
python optimize_agents.py --list

# Оптимизация всех
python optimize_agents.py --all

# Конкретные агенты  
python optimize_agents.py --agents manager analyst

# Предварительный просмотр (dry-run)
python optimize_agents.py --agents manager --dry-run

# Восстановление
python restore_agents.py --list
python restore_agents.py --agents manager
```

## Что оптимизируется

- **Промпт агента**: Оптимизирует собственная модель агента (принцип OpenAI Cookbook)
- **Описание агента**: Оптимизирует модель `hard` на основе функционала и инструментов агента

## Резервные копии

Автоматически создаются в `../agent_profiles_backup/` перед каждым изменением.
