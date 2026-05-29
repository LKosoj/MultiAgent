# Глава 7: Менеджер инструментов (ToolManager)

`ToolManager` — диспетчер вызовов инструментов: добавляет логи, телеметрию и единообразный контроль исполнения.

## Зачем нужен
- Централизованное логирование вызовов и ошибок.
- Трассировка (span) на каждый запуск инструмента.
- Простой способ «превратить» функцию в управляемый инструмент.

## Декоратор @with_telemetry
```python
from tool_manager import with_telemetry

@with_telemetry("image_analysis", "Анализ изображения")
def analyze_image_tool(image_path: str) -> str:
    # ... логика анализа ...
    return "{...json...}"
```
Каждый вызов будет логироваться и оборачиваться в телеметрию автоматически.

## Центральный метод run_tool (упрощённо)
```python
class ToolManager:
    def run_tool(self, tool_name: str, tool_function: Callable, task_description: str = None, session_id: str = None, **kwargs):
        span = None
        try:
            telemetry = get_telemetry_manager()
            span = telemetry.start_run_trace(run_id=session_id, agent_name=tool_name, task=task_description)
            result = tool_function(**kwargs)
            return result
        except Exception as e:
            # запись ошибки в телеметрию/логи
            raise
        finally:
            if span:
                telemetry.finish_run_trace(span, success=True)
```

## Контекстный менеджер tool_context
```python
with get_tool_manager().tool_context("complex_tool", "Сложная задача", session_id) as ctx:
    part = do_step()
    ctx.add_metadata("step", "done")
```
Дает тонкий контроль и возможность добавлять метаданные в ходе работы.

## Где используется
- Агенты (через `AgentFactory`) вызывают инструменты, обёрнутые в телеметрию.
- UI/Streamlit-страницы могут запускать инструменты напрямую с логированием.

## Вывод
`ToolManager` делает вызовы инструментов наблюдаемыми и предсказуемыми: меньше «чёрных ящиков», больше данных для диагностики и оптимизации.
