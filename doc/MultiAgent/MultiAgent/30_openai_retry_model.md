# Глава 30: RetryOpenAIServerModel

Надежный вызов LLM с повторными попытками и автоматическими фоллбеками на альтернативные модели.

## Зачем
- Переживать rate limit/сети/временную недоступность.
- Прозрачно переключаться на запасные модели.

## Инициализация
```python
model = RetryOpenAIServerModel(
  model_id="primary.model",
  fallback_models="alt1.model,alt2.model",
  max_retries=8,
)
```

## Алгоритм
- Для текущей модели: до `max_retries` попыток с паузами.
- На фатальные ошибки — переключение на следующую из `fallback_models`.
- Если все исчерпаны — исключение.

```python
def __call__(self, messages, **kw):
    while self.current_model_index < len(self.model_ids):
        for attempt in range(self.max_retries + 1):
            try:
                return self.model(messages, **kw)
            except Exception as e:
                if self._should_fallback(e):
                    break
                time.sleep(self._backoff(attempt))
        self._switch_to_fallback()
    raise RuntimeError("All models unavailable")
```

## Итого
RetryOpenAIServerModel повышает отказоустойчивость без усложнения кода агентов: один интерфейс — многие попытки и фоллбеки.
