"""
Wrapper для OpenAIServerModel с встроенным механизмом повторных попыток и fallback моделями.
Создано для решения проблем с ошибками HTTP 400 "Model not found" и автоматического
переключения на запасные модели при недоступности основной.

Новый функционал fallback:
- Поддержка запасных моделей через параметр fallback_models
- Автоматическое переключение при критических ошибках (rate limit, quota exceeded)
- Формат fallback_models: "model1, model2, model3" (строка через запятую)

Пример использования:
    model = RetryOpenAIServerModel(
        model_id="основная-модель",
        fallback_models="запасная-модель1, запасная-модель2",
        max_retries=3
    )
"""

import time
import os
import json
import random
import logging
import re
import copy
from typing import Any, Dict, List, Optional
from smolagents import OpenAIServerModel, ChatMessage, logger
import httpx
from httpx import HTTPStatusError, ReadTimeout, ConnectTimeout, TimeoutException
from types import SimpleNamespace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def _compute_backoff_delay(retry_delay_base: float, attempt: int) -> float:
    """Экспоненциальная задержка с пропорциональным джиттером (≤10% от base-delay).

    Единая формула для HTTP transport и model-level retry, чтобы не было двойного
    джиттера (абсолютный 0..1 сек поверх пропорционального), приводившего к
    несогласованным интервалам между повторами.
    """
    base = retry_delay_base * (2 ** attempt)
    return base + random.uniform(0.0, base * 0.1)


def _parse_retry_after(response) -> Optional[float]:
    """Парсит заголовок Retry-After (секунды или HTTP-дата). Возвращает None, если не задан/некорректен."""
    try:
        raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
    except Exception:
        return None
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delay = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delay)
    except Exception:
        return None


class RetryOpenAIServerModel:
    """
    Wrapper для OpenAIServerModel с автоматическими повторными попытками
    при возникновении определенных ошибок HTTP и поддержкой fallback моделей.
    """
    
    def __init__(
        self,
        model_id: str,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        max_retries: int = 6,
        retry_delay_base: float = 2.0,
        retry_on_errors: Optional[List[str]] = None,
        fallback_models: Optional[str] = None,
        **kwargs
    ):
        """
        Инициализация wrapper'а с настройками retry и fallback моделями.
        
        Args:
            model_id: Идентификатор основной модели
            api_base: Базовый URL API
            api_key: API ключ
            max_retries: Максимальное количество повторных попыток
            retry_delay_base: Базовая задержка между попытками (в секундах)
            retry_on_errors: Список ключевых слов ошибок для retry
            fallback_models: Строка с запасными моделями через запятую (например: "model1, model2")
            **kwargs: Дополнительные параметры для OpenAIServerModel
        """
        # Сначала устанавливаем атрибуты экземпляра
        self.max_retries = max_retries
        self.retry_delay_base = retry_delay_base
        self.api_base = api_base
        self.api_key = api_key
        self.kwargs = kwargs
        
        # Парсим fallback модели
        self.model_ids = [model_id.strip()]
        if fallback_models:
            fallback_list = [m.strip() for m in fallback_models.split(',') if m.strip()]
            self.model_ids.extend(fallback_list)
        
        self.current_model_index = 0
        self.connection_error_count = 0  # Счетчик connection errors подряд
        
        # Извлекаем client_kwargs из kwargs, если есть
        client_kwargs = kwargs.pop('client_kwargs', {})
        
        # Создаем кастомный HTTP клиент с расширенной retry логикой
        custom_http_client = self._create_custom_http_client(max_retries)
        client_kwargs['http_client'] = custom_http_client
        
        # Создаем текущую модель
        self.model = self._create_model(self.model_ids[0], client_kwargs)
        
        # По умолчанию повторяем попытки для следующих ошибок
        self.retry_on_errors = retry_on_errors or [
            'model not found', '404', 'bad request', '400',
            'timeout', 'connection', 'network', 'server error', 
            '500', '502', '503', '429', 'rate limit',
            'internal server error', 'service unavailable',
            'вернула пустой ответ', 'empty response', 'does not contain any json blob',
            'the model output does not contain any json blob',
            "'nonetype' object has no attribute", 'nonetype object has no attribute', 
            'object has no attribute choices', 'вернула none', 'переходить на fallback модель'
        ]
        
        # Ошибки, при которых нужно переключиться на fallback модель
        self.fallback_errors = [
            '429', 'rate limit', 'http 429', 'connection error',
            'quota exceeded', 'billing', 'insufficient funds',
            '503', 'service unavailable', 'http 503',
            # Добавляем 404 ошибки - часто означают недоступность endpoint или модели
            '404', 'not found', 'http 404',
            'вернула пустой ответ', 'empty response', 'does not contain any json blob',
            'the model output does not contain any json blob',
            "'nonetype' object has no attribute", 'nonetype object has no attribute',
            'object has no attribute choices', 'вернула none', 'переходить на fallback модель'
        ]
        
        fallback_info = f" с fallback моделями: {self.model_ids[1:]}" if len(self.model_ids) > 1 else ""
        
        init_details = {
            "primary_model": model_id,
            "all_models": self.model_ids,
            "max_retries": max_retries,
            "retry_delay_base": retry_delay_base,
            "api_base": api_base,
            "has_fallbacks": len(self.model_ids) > 1,
            "retry_on_errors_count": len(self.retry_on_errors),
            "fallback_errors_count": len(self.fallback_errors)
        }
        
        logger.info(
            f"Инициализирован RetryOpenAIServerModel{fallback_info}:\n"
            f"Детали инициализации: {json.dumps(init_details, ensure_ascii=False, indent=2)}"
        )
    
    def _create_model(self, model_id: str, client_kwargs: Dict[str, Any]) -> OpenAIServerModel:
        """
        Создает экземпляр OpenAIServerModel для указанной модели.
        
        Args:
            model_id: Идентификатор модели
            client_kwargs: Параметры для HTTP клиента
            
        Returns:
            OpenAIServerModel: Настроенная модель
        """
        return OpenAIServerModel(
            model_id=model_id,
            api_base=self.api_base,
            api_key=self.api_key,
            client_kwargs=client_kwargs,
            **self.kwargs
        )
    
    def _is_empty_response(self, response) -> bool:
        """
        Проверяет, является ли ответ "пустым" (нет content и tool_calls).
        Такие ответы вызывают ошибку "The model output does not contain any JSON blob" в ToolCallingAgent.
        
        Args:
            response: Ответ от модели для проверки
            
        Returns:
            bool: True если ответ пустой (нужен retry)
        """
        try:
            # Если response вообще None
            if response is None:
                logger.debug("Обнаружен None response")
                return True
            
            # Для ChatMessage
            if isinstance(response, ChatMessage):
                content = getattr(response, 'content', None)
                tool_calls = getattr(response, 'tool_calls', None)
                
                # Пустой если content пустой/None и нет tool_calls
                is_empty = (not content or content.strip() == "") and (not tool_calls)
                if is_empty:
                    logger.debug(f"Обнаружен пустой ChatMessage: content='{content}', tool_calls={tool_calls}")
                return is_empty
            
            # Для объектов с choices
            if hasattr(response, 'choices'):
                choices = getattr(response, 'choices', None)
                if not choices or len(choices) == 0:
                    logger.debug("Обнаружен response с пустыми choices")
                    return True
                    
                first_choice = choices[0]
                if hasattr(first_choice, 'message'):
                    message = getattr(first_choice, 'message', None)
                    if not message:
                        logger.debug("Обнаружен choice без message")
                        return True
                    
                    content = getattr(message, 'content', None)
                    tool_calls = getattr(message, 'tool_calls', None)
                    
                    # Пустой если content пустой/None и нет tool_calls
                    is_empty = (not content or content.strip() == "") and (not tool_calls)
                    if is_empty:
                        logger.debug(f"Обнаружен пустой message в choices: content='{content}', tool_calls={tool_calls}")
                    return is_empty
                else:
                    # У choice нет message атрибута - возможно это другой формат
                    logger.debug("Обнаружен choice без атрибута message")
                    return True
            
            # Для словарного типа response
            if isinstance(response, dict):
                if 'choices' in response:
                    choices = response['choices']
                    if not choices or len(choices) == 0:
                        logger.debug("Обнаружен dict response с пустыми choices")
                        return True
                        
                    first_choice = choices[0]
                    if 'message' in first_choice:
                        message = first_choice['message']
                        content = message.get('content')
                        tool_calls = message.get('tool_calls')
                        
                        # Пустой если content пустой/None и нет tool_calls
                        is_empty = (not content or content.strip() == "") and (not tool_calls)
                        if is_empty:
                            logger.debug(f"Обнаружен пустой message в dict response: content='{content}', tool_calls={tool_calls}")
                        return is_empty
                    else:
                        logger.debug("Обнаружен dict choice без message")
                        return True
                else:
                    # Это dict, но без choices - проверим на общие признаки пустоты
                    if len(response) == 0:
                        logger.debug("Обнаружен пустой dict response")
                        return True
            
            # Для строкового ответа - если пустая строка, считаем пустым
            if isinstance(response, str):
                is_empty = not response.strip()
                if is_empty:
                    logger.debug("Обнаружен пустой string response")
                return is_empty
            
            # Если не удалось определить структуру, но объект пустой
            if hasattr(response, '__len__') and len(response) == 0:
                logger.debug("Обнаружен response с нулевой длиной")
                return True
            
            return False
            
        except Exception as e:
            logger.debug(f"Ошибка при проверке пустого ответа: {e}")
            return False

    def _is_valid_response(self, response) -> bool:
        """
        Проверяет корректность структуры response объекта.
        
        Args:
            response: Ответ от модели для проверки
            
        Returns:
            bool: True если response объект корректен
        """
        try:
            # Проверяем базовые варианты корректного response
            if isinstance(response, ChatMessage):
                # ChatMessage объект - это корректный ответ
                return True
            
            # Проверяем наличие атрибута choices для объектов типа OpenAI response
            if hasattr(response, 'choices'):
                choices = getattr(response, 'choices', None)
                if choices is None:
                    return False
                # Проверяем, что choices не пустой
                if not choices or len(choices) == 0:
                    return False
                # Проверяем первый choice на наличие message
                first_choice = choices[0]
                if hasattr(first_choice, 'message'):
                    message = getattr(first_choice, 'message', None)
                    return message is not None and hasattr(message, 'content')
                return True
            
            # Проверяем для словарного типа response
            if isinstance(response, dict):
                if 'choices' in response:
                    choices = response['choices']
                    if not choices or len(choices) == 0:
                        return False
                    first_choice = choices[0]
                    if 'message' in first_choice:
                        message = first_choice['message']
                        return 'content' in message
                    return True
                # Если это словарь без choices, но с другими полями - может быть корректным
                return len(response) > 0
            
            # Для строкового ответа
            if isinstance(response, str):
                return True
            
            # Если ни один из стандартных форматов не подходит, считаем некорректным
            return False
            
        except Exception as e:
            logger.debug(f"Ошибка при проверке корректности response: {e}")
            return False
    
    def _should_fallback(self, error: Exception) -> bool:
        """
        Определяет, нужно ли переключиться на fallback модель.
        
        Args:
            error: Исключение для анализа
            
        Returns:
            bool: True если нужно переключиться на fallback модель
        """
        error_str = str(error).lower()
        
        # Для connection error требуем несколько ошибок подряд
        if "connection error" in error_str:
            self.connection_error_count += 1
            # Переключаемся на fallback только после 3 connection errors подряд
            return self.connection_error_count >= 3
        else:
            # Сбрасываем счетчик, если ошибка не connection error
            self.connection_error_count = 0
            # Для других ошибок переключаемся сразу
            fallback_keywords = [
                '429', 'rate limit', 'http 429', 'quota exceeded', 'billing', 'insufficient funds',
                # Добавляем 400 ошибки "Model not found" - retry бессмысленны, нужен fallback
                'model not found', '404: model not found', 'http 400', '400 bad request',
                # Добавляем 404 ошибки "Not Found" - часто означают недоступность endpoint или модели
                '404', 'not found', 'http 404', '404 not found',
                # Добавляем 503 ошибки "Service Unavailable" - часто означают проблемы с моделью
                '503', 'service unavailable', 'http 503',
                # Добавляем ошибки некорректного response объекта - retry бессмысленны, нужен fallback
                'вернула некорректный response объект', 'отсутствует атрибут', "'nonetype' object has no attribute",
                "'nonetype' object is not subscriptable",  # response.choices = None
                'response object has no attribute', 'invalid response structure',
                # Добавляем ошибки пустых ответов - retry бессмысленны, нужен fallback
                'вернула пустой ответ', 'empty response', 'does not contain any json blob',
                'the model output does not contain any json blob', 'вернула none'
            ]
            return any(keyword in error_str for keyword in fallback_keywords)
    
    def _switch_to_fallback(self) -> bool:
        """
        Переключается на следующую модель по списку (циклически).

        Поведение:
        - Если доступна следующая модель в списке — переключаемся на неё.
        - Если текущая модель последняя в списке — переключаемся на основную (индекс 0) и начинаем цикл заново.
        - Если в списке только одна модель — переключение невозможно.
        
        Returns:
            bool: True если переключение успешно, False если переключиться нельзя
        """
        if len(self.model_ids) <= 1:
            return False

        prev_index = self.current_model_index
        prev_model_id = self.model_ids[prev_index]

        next_index = (self.current_model_index + 1) % len(self.model_ids)
        new_model_id = self.model_ids[next_index]
        wrapped_to_primary = next_index == 0 and prev_index == (len(self.model_ids) - 1)
        wrapping_note = " (достигли конца fallback, возвращаемся на основную модель)" if wrapped_to_primary else ""
        
        # Создаем новую модель с теми же параметрами
        # Для новой модели создаем новый HTTP клиент с нуля
        custom_http_client = self._create_custom_http_client(self.max_retries)
        client_kwargs = {'http_client': custom_http_client}
        try:
            self.model = self._create_model(new_model_id, client_kwargs)
            self.current_model_index = next_index
            self.connection_error_count = 0  # Сбрасываем счетчик при переключении
            logger.warning(
                f"Успешно переключились на следующую модель (циклически){wrapping_note}:\n"
                f"Предыдущая модель: {prev_model_id}\n"
                f"Новая модель: {new_model_id}\n"
                f"Индекс модели: {self.current_model_index} (предыдущий: {prev_index})\n"
                f"Все модели в цикле: {self.model_ids}"
            )
            return True
        except Exception as e:
            logger.error(
                f"Ошибка при создании модели {new_model_id} при переключении (циклически):\n"
                f"Тип ошибки: {type(e).__name__}\n"
                f"Сообщение: {e}\n"
                f"API Base: {self.api_base}\n"
                f"Предыдущая модель: {prev_model_id}\n"
                f"Новая модель: {new_model_id}\n"
                f"Индексы: prev={prev_index}, next={next_index}"
            )
            return False
    
    def _create_custom_http_client(self, max_retries: int):
        """
        Создает кастомный HTTP клиент с расширенной retry логикой для 404/400 ошибок.
        """
        import httpx
        
        # Создаем кастомный transport с retry логикой
        class CustomRetryTransport(httpx.HTTPTransport):
            def __init__(self, max_retries: int, retry_delay_base: float = 1.0, **kwargs):
                # Обязательно передаем все kwargs в родительский класс
                super().__init__(**kwargs)
                self.max_retries = max_retries
                self.retry_delay_base = retry_delay_base
                # Убираем 429 и 503 из retry_status_codes, чтобы эти ошибки передавались в fallback логику
                self.retry_status_codes = {400, 404, 500, 502, 504}
            
            def handle_request(self, request):
                last_exception = None
                
                for attempt in range(self.max_retries + 1):
                    try:
                        response = super().handle_request(request)
                        
                        # Проверяем статус код ответа
                        if response.status_code in (429, 503):
                            # Сначала пробуем retry на текущей модели (уважаем Retry-After).
                            # Только после исчерпания попыток пробрасываем ошибку наверх
                            # для fallback-логики переключения модели.
                            status = response.status_code
                            status_label = "Rate Limit Exceeded" if status == 429 else "Service Unavailable"
                            try:
                                response.read()
                            except Exception:
                                pass
                            retry_after = _parse_retry_after(response)
                            if attempt < self.max_retries:
                                if retry_after is not None:
                                    delay = max(0.0, float(retry_after))
                                else:
                                    delay = _compute_backoff_delay(self.retry_delay_base, attempt)
                                logger.warning(
                                    f"HTTP {status} {status_label} на попытке {attempt + 1}/{self.max_retries + 1}. "
                                    f"Retry-After={retry_after}. Повтор через {delay:.2f}s."
                                )
                                time.sleep(delay)
                                continue
                            from httpx import HTTPStatusError
                            raise HTTPStatusError(
                                f"HTTP {status} {status_label}",
                                request=request,
                                response=response
                            )
                        elif response.status_code in self.retry_status_codes:
                            # Подробное логирование для всех retry статус кодов
                            full_error_text = ""
                            try:
                                # Гарантируем загрузку стримингового тела ответа
                                try:
                                    response.read()
                                except Exception:
                                    pass
                                try:
                                    full_error_text = response.text
                                except Exception:
                                    # Фолбэк: пробуем декодировать сырое содержимое
                                    try:
                                        full_error_text = response.content.decode(response.encoding or 'utf-8', errors='replace')
                                    except Exception as inner:
                                        full_error_text = f"Не удалось прочитать тело ответа: {inner}"
                            except Exception as read_err:
                                full_error_text = f"Не удалось прочитать тело ответа: {read_err}"
                            
                            # Логируем детали запроса для диагностики
                            request_details = {
                                "method": request.method,
                                "url": str(request.url),
                                "headers": dict(request.headers),
                            }
                            
                            # Для POST запросов также логируем размер тела
                            if hasattr(request, 'content') and request.content:
                                try:
                                    content_length = len(request.content)
                                    request_details["content_length"] = content_length
                                    # Всегда показываем превью первых 1000 символов для диагностики
                                    try:
                                        content_text = request.content.decode('utf-8', errors='replace')
                                        request_details["content_preview"] = content_text[:1000]
                                        if len(content_text) > 1000:
                                            request_details["content_truncated"] = True
                                        
                                        # Пытаемся извлечь model из JSON запроса
                                        try:
                                            import json
                                            content_json = json.loads(content_text)
                                            if 'model' in content_json:
                                                request_details["requested_model"] = content_json['model']
                                            if 'messages' in content_json:
                                                request_details["messages_count"] = len(content_json['messages'])
                                        except Exception:
                                            pass
                                    except Exception:
                                        request_details["content_preview"] = str(request.content)[:500]
                                except Exception:
                                    request_details["content_length"] = "unknown"
                            
                            # Улучшенное отображение ответа сервера
                            if full_error_text.strip():
                                server_response = full_error_text[:2000] + ('...' if len(full_error_text) > 2000 else '')
                            else:
                                server_response = "(пустой ответ - сервер не вернул описание ошибки)"
                            
                            # Специальная обработка для HTTP 404 ошибок
                            if response.status_code == 404:
                                current_url = str(request.url)
                                suggested_fixes = []
                                
                                # Проверяем распространенные проблемы с URL
                                if '/api/chat/completions' in current_url:
                                    base_url = current_url.replace('/api/chat/completions', '')
                                    suggested_fixes.append(f"Попробуйте: {base_url}/v1/chat/completions")
                                elif '/chat/completions' in current_url and '/v1/' not in current_url:
                                    base_url = current_url.replace('/chat/completions', '')
                                    suggested_fixes.append(f"Попробуйте: {base_url}/v1/chat/completions")
                                
                                if 'requested_model' in request_details:
                                    suggested_fixes.append(f"Проверьте, что модель '{request_details['requested_model']}' существует на сервере")
                                
                                fix_suggestions = "\n".join([f"  • {fix}" for fix in suggested_fixes]) if suggested_fixes else "  • Проверьте правильность URL API"
                                
                                logger.warning(
                                    f"🚨 HTTP 404 NOT FOUND - Endpoint не найден!\n"
                                    f"Попытка {attempt + 1}/{self.max_retries + 1}\n"
                                    f"Текущий URL: {current_url}\n"
                                    f"Возможные решения:\n{fix_suggestions}\n"
                                    f"Детали запроса: {json.dumps(request_details, ensure_ascii=False, indent=2)}\n"
                                    f"Ответ сервера: {server_response}"
                                )
                            else:
                                logger.warning(
                                    f"HTTP {response.status_code} ошибка, попытка {attempt + 1}/{self.max_retries + 1}.\n"
                                    f"Детали запроса: {json.dumps(request_details, ensure_ascii=False, indent=2)}\n"
                                    f"Ответ сервера: {server_response}"
                                )
                            
                            # Если это последняя попытка — поднимем исключение с полным описанием
                            if attempt >= self.max_retries:
                                from httpx import HTTPStatusError
                                raise HTTPStatusError(
                                    f"HTTP {response.status_code} ошибка после {self.max_retries + 1} попыток.\n"
                                    f"URL: {request.url}\n"
                                    f"Ответ: {full_error_text}",
                                    request=request,
                                    response=response
                                )

                            if attempt < self.max_retries:
                                # Вычисляем задержку
                                delay = _compute_backoff_delay(self.retry_delay_base, attempt)
                                logger.warning(
                                    f"Повторная попытка через {delay:.2f} секунд..."
                                )
                                time.sleep(delay)
                                continue
                        
                        return response
                        
                    except Exception as e:
                        last_exception = e
                        # Если это 429, 503 ошибка или connection error, передаем ее наверх для fallback логики
                        error_str = str(e).lower()
                        if any(keyword in error_str for keyword in ["429", "rate limit", "503", "service unavailable", "connection error"]):
                            raise e
                        
                        if attempt < self.max_retries:
                            delay = _compute_backoff_delay(self.retry_delay_base, attempt)
                            logger.warning(
                                f"Ошибка HTTP запроса, попытка {attempt + 1}/{self.max_retries + 1}: {e}. "
                                f"Повторная попытка через {delay:.2f} секунд..."
                            )
                            time.sleep(delay)
                            continue
                        raise
                
                # Если дошли сюда, все попытки исчерпаны
                if last_exception:
                    raise last_exception
                return response
        
        # Создаем HTTP клиент с кастомным transport
        # HTTPTransport не принимает timeout напрямую - это параметр Client
        transport = CustomRetryTransport(
            max_retries=max_retries, 
            retry_delay_base=self.retry_delay_base
        )
        return httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(
                connect=10.0,   # Время на установку соединения
                read=600.0,     # Время на чтение ответа (10 минут)
                write=10.0,     # Время на отправку запроса
                pool=10.0       # Время на получение соединения из пула
            )
        )

    def _normalize_response_for_logging(self, response: Any) -> Any:
        """
        Преобразует ответ модели в сериализуемый вид для логирования.
        Максимально сохраняет структуру, но не ломает основной поток при сбоях сериализации.
        """
        try:
            # Ветка ChatMessage
            if isinstance(response, ChatMessage):
                data: Dict[str, Any] = {
                    "type": "ChatMessage",
                    "role": getattr(response, "role", None),
                    "content": getattr(response, "content", None),
                }
                if hasattr(response, "tool_calls"):
                    tool_calls = getattr(response, "tool_calls")
                    if tool_calls:
                        try:
                            # Пытаемся сериализовать tool_calls
                            data["tool_calls"] = [
                                {
                                    "id": getattr(tc, "id", None),
                                    "type": getattr(tc, "type", None),
                                    "function": {
                                        "name": getattr(getattr(tc, "function", None), "name", None),
                                        "arguments": getattr(getattr(tc, "function", None), "arguments", None),
                                    } if hasattr(tc, "function") else None
                                } for tc in tool_calls
                            ]
                        except Exception as tc_err:
                            data["tool_calls"] = f"tool_calls_serialize_error: {tc_err}"
                    else:
                        data["tool_calls"] = None
                if hasattr(response, "raw"):
                    raw_value = getattr(response, "raw")
                    try:
                        json.dumps(raw_value)
                        data["raw"] = raw_value
                    except Exception:
                        data["raw"] = str(raw_value)[:10000]
                return data

            # Ветка dict
            if isinstance(response, dict):
                return response

            # Ветка OpenAI-подобного ответа с choices
            if hasattr(response, "choices"):
                choices = []
                try:
                    for ch in getattr(response, "choices", []) or []:
                        message = getattr(ch, "message", None)
                        if message is not None:
                            msg_data = {
                                "role": getattr(message, "role", None),
                                "content": getattr(message, "content", None),
                            }
                            
                            # Обрабатываем tool_calls отдельно
                            if hasattr(message, "tool_calls") and getattr(message, "tool_calls"):
                                try:
                                    tool_calls = getattr(message, "tool_calls")
                                    msg_data["tool_calls"] = [
                                        {
                                            "id": getattr(tc, "id", None),
                                            "type": getattr(tc, "type", None),
                                            "function": {
                                                "name": getattr(getattr(tc, "function", None), "name", None),
                                                "arguments": getattr(getattr(tc, "function", None), "arguments", None),
                                            } if hasattr(tc, "function") else None
                                        } for tc in tool_calls
                                    ]
                                except Exception as tc_err:
                                    msg_data["tool_calls"] = f"tool_calls_serialize_error: {tc_err}"
                            else:
                                msg_data["tool_calls"] = None
                                
                            choices.append({"message": msg_data})
                except Exception as choices_err:
                    choices = [{"choices_parsing_error": str(choices_err)}]
                return {
                    "type": type(response).__name__,
                    "choices": choices if choices else str(response)[:10000]
                }

            # Fallback: строковое представление
            return {
                "type": type(response).__name__,
                "text": str(response)[:10000]
            }
        except Exception as e:
            return {"normalize_error": str(e), "text": str(response)[:500]}

    def _write_response_log(self, response: Any, attempt: int, model_id: str) -> None:
        """
        Записывает ответ модели в файл JSON. Один ответ = один файл с меткой времени в имени.
        """
        base_dir = os.path.dirname(os.path.abspath(__file__))
        logs_dir = os.path.join(base_dir, "logs", "llm_responses")
        os.makedirs(logs_dir, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_model = (model_id or "unknown").replace('/', '_').replace(':', '_')
        filename = f"{ts}_attempt{attempt + 1}_{safe_model}.json"
        file_path = os.path.join(logs_dir, filename)

        try:
            payload = {
                "timestamp": ts,
                "model_id": model_id,
                "attempt": attempt + 1,
                "current_model_index": self.current_model_index,
                "response": self._normalize_response_for_logging(response),
            }

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            # Если не удалось записать нормальный лог, записываем лог об ошибке
            try:
                error_payload = {
                    "timestamp": ts,
                    "model_id": model_id,
                    "attempt": attempt + 1,
                    "current_model_index": self.current_model_index,
                    "logging_error": str(e),
                    "response_type": type(response).__name__,
                    "response_str": str(response)
                }
                
                error_filename = f"{ts}_attempt{attempt + 1}_{safe_model}_ERROR.json"
                error_file_path = os.path.join(logs_dir, error_filename)
                
                with open(error_file_path, "w", encoding="utf-8") as f:
                    json.dump(error_payload, f, ensure_ascii=False, indent=2)
                    
                logger.debug(f"Записан лог об ошибке логирования: {error_filename}")
            except Exception as final_e:
                # Если и это не сработало, только тогда логируем в debug
                logger.debug(f"Критическая ошибка логирования ответа модели: {e}, финальная ошибка: {final_e}")
    
    def _should_retry(self, error: Exception) -> bool:
        """
        Определяет, нужно ли повторять попытку на основе типа ошибки.
        
        Args:
            error: Исключение для анализа
            
        Returns:
            bool: True если нужно повторить попытку
        """
        error_str = str(error).lower()
        return any(keyword in error_str for keyword in self.retry_on_errors)

    def _extract_error_message_text(self, error: Exception) -> str:
        """
        Пытается извлечь человекочитаемое сообщение об ошибке из:
        - самого исключения
        - error.response.text (если это HTTPStatusError/похожее)
        - JSON вида {"error":{"message":...}}
        """
        parts: List[str] = []
        try:
            parts.append(str(error) or "")
        except Exception:
            pass

        # Если у ошибки есть HTTP-ответ — добавим тело
        try:
            resp = getattr(error, "response", None)
            if resp is not None:
                # Попробуем взять текст/контент
                body_text = None
                try:
                    body_text = resp.text
                except Exception:
                    try:
                        body_text = resp.content.decode(getattr(resp, "encoding", None) or "utf-8", errors="replace")
                    except Exception:
                        body_text = None
                if body_text:
                    parts.append(body_text)

                    # Попробуем распарсить JSON ошибок OpenAI-подобного формата
                    try:
                        payload = json.loads(body_text)
                        if isinstance(payload, dict):
                            msg = None
                            if "error" in payload and isinstance(payload["error"], dict):
                                msg = payload["error"].get("message")
                            if isinstance(msg, str) and msg.strip():
                                parts.append(msg)
                    except Exception:
                        pass
        except Exception:
            pass

        # Склеим и уберём лишнее
        combined = "\n".join([p for p in parts if p])
        return combined

    def _maybe_clamp_max_tokens_from_context_error(self, error: Exception, call_kwargs: Dict[str, Any]) -> bool:
        """
        Обрабатывает частый кейс 400:
        "'max_tokens' or 'max_completion_tokens' is too large: X. This model's maximum context length is C tokens
         and your request has I input tokens (X > C - I)."

        Если можем вычислить допустимый максимум — уменьшаем call_kwargs["max_tokens"] и просим повторить запрос.

        Returns:
            bool: True если max_tokens был уменьшен и нужно сделать retry.
        """
        # Нечего клампить, если max_tokens вообще не задан
        if "max_tokens" not in call_kwargs:
            return False

        # Не ломаемся, если max_tokens не число
        try:
            current_max = int(call_kwargs["max_tokens"])
        except Exception:
            return False

        text = self._extract_error_message_text(error)
        if not text:
            return False

        low = text.lower()
        if ("max_tokens" not in low and "max_completion_tokens" not in low) or "too large" not in low:
            return False

        # Пытаемся извлечь числа (макс контекст и входные токены)
        # Делаем максимально терпимую регулярку, т.к. сообщения могут отличаться.
        max_ctx = None
        input_tokens = None
        try:
            m_ctx = re.search(r"maximum context length is\s+(\d+)\s+tokens", text, flags=re.IGNORECASE)
            m_in = re.search(r"request has\s+(\d+)\s+input tokens", text, flags=re.IGNORECASE)
            if m_ctx:
                max_ctx = int(m_ctx.group(1))
            if m_in:
                input_tokens = int(m_in.group(1))
        except Exception:
            max_ctx = None
            input_tokens = None

        if not max_ctx or input_tokens is None:
            return False

        # Оставим небольшой safety margin (на служебные токены/разметку)
        safety_margin = 256
        allowed = max_ctx - input_tokens - safety_margin

        # Если allowed <= 0 — проблема уже во входе, кламп не спасёт
        if allowed <= 0:
            return False

        new_max = max(1, min(current_max, allowed))
        if new_max >= current_max:
            return False

        call_kwargs["max_tokens"] = new_max
        logger.warning(
            f"⚠️ max_tokens слишком большой для текущего контекста. "
            f"Уменьшаем max_tokens: {current_max} -> {new_max} "
            f"(max_context={max_ctx}, input_tokens={input_tokens}, safety_margin={safety_margin})"
        )
        return True
    
    def _get_retry_delay(self, attempt: int) -> float:
        """Экспоненциальная задержка с пропорциональным джиттером (общий helper)."""
        return _compute_backoff_delay(self.retry_delay_base, attempt)
    
    def _extract_response_content(self, response) -> str:
        """
        Извлекает текстовое содержимое из ответа модели.
        
        Args:
            response: Ответ от модели
            
        Returns:
            str: Текстовое содержимое ответа или пустая строка
        """
        try:
            if hasattr(response, 'content'):
                return self._coerce_content_to_text(getattr(response, 'content', None))
            elif hasattr(response, 'choices') and response.choices is not None and len(response.choices) > 0:
                first_choice = response.choices[0]
                if hasattr(first_choice, 'message') and hasattr(first_choice.message, 'content'):
                    return self._coerce_content_to_text(first_choice.message.content)
            elif isinstance(response, dict):
                if 'choices' in response and response['choices'] is not None and len(response['choices']) > 0:
                    choice = response['choices'][0]
                    if 'message' in choice and 'content' in choice['message']:
                        return self._coerce_content_to_text(choice['message']['content'])
            elif isinstance(response, str):
                return response
            
            # Fallback: пытаемся преобразовать в строку
            return str(response)
        except Exception as e:
            logger.warning(f"Не удалось извлечь содержимое ответа: {e}")
            return ""

    def _coerce_content_to_text(self, content: Any) -> str:
        """
        Преобразует content (str/list/dict/None) к безопасной строке.
        Нужен для совместимости со smolagents, где в некоторых местах ожидается str.
        """
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                    else:
                        parts.append(str(item))
                else:
                    parts.append(str(item))
            return "\n".join(p for p in parts if p).strip()
        if isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str):
                return text
        return str(content)

    def _normalize_messages_for_model(self, messages: List[ChatMessage]) -> List[ChatMessage]:
        """
        Нормализует сообщения перед вызовом модели:
        content приводится к списку text-блоков (формат smolagents/openai chat parts),
        чтобы избежать AssertionError в get_clean_message_list при слиянии сообщений.
        """
        normalized: List[ChatMessage] = []
        for msg in messages or []:
            try:
                content = getattr(msg, "content", None)
                role = getattr(msg, "role", "user")
                content_blocks: List[Dict[str, Any]]
                if isinstance(content, list):
                    content_blocks = []
                    for item in content:
                        if isinstance(item, dict):
                            # Нормализуем text-элементы к ожидаемому виду.
                            if item.get("type") == "text":
                                content_blocks.append({"type": "text", "text": self._coerce_content_to_text(item.get("text"))})
                            else:
                                # image/image_url и другие поддерживаем как есть
                                content_blocks.append(item)
                        else:
                            content_blocks.append({"type": "text", "text": self._coerce_content_to_text(item)})
                else:
                    content_blocks = [{"type": "text", "text": self._coerce_content_to_text(content)}]

                # Сохраняем tool_calls, если есть
                tool_calls = getattr(msg, "tool_calls", None)
                if tool_calls:
                    normalized.append(ChatMessage(role=role, content=content_blocks, tool_calls=tool_calls))
                else:
                    normalized.append(ChatMessage(role=role, content=content_blocks))
            except Exception:
                # Крайний fallback: стараемся не ронять основной поток
                normalized.append(msg)
        return normalized

    def _normalize_response_content(self, response: Any) -> Any:
        """
        Нормализует content в ответе модели к строке.
        Это предотвращает падения вида `list` has no attribute `strip`.
        """
        try:
            if isinstance(response, ChatMessage):
                normalized_content = self._coerce_content_to_text(getattr(response, "content", None))
                tool_calls = getattr(response, "tool_calls", None)
                if tool_calls:
                    return ChatMessage(role=getattr(response, "role", "assistant"), content=normalized_content, tool_calls=tool_calls, raw=getattr(response, "raw", None))
                return ChatMessage(role=getattr(response, "role", "assistant"), content=normalized_content, raw=getattr(response, "raw", None))

            if hasattr(response, "choices") and response.choices is not None and len(response.choices) > 0:
                # Пытаемся исправить in-place, иначе создаем копию
                try:
                    message = response.choices[0].message
                    if message is not None and hasattr(message, "content"):
                        message.content = self._coerce_content_to_text(message.content)
                    return response
                except Exception:
                    cloned = copy.deepcopy(response)
                    try:
                        message = cloned.choices[0].message
                        if message is not None and hasattr(message, "content"):
                            message.content = self._coerce_content_to_text(message.content)
                    except Exception:
                        pass
                    return cloned
        except Exception:
            pass
        return response

    def _inject_usage_defaults(self, response: Any) -> Any:
        """
        Гарантирует наличие usage/token_usage в ответе.

        - Если это ChatMessage и token_usage отсутствует, подставляет нули.
        - Если у ответа есть атрибут usage и он None, подставляет нули для
          prompt_tokens/completion_tokens.
        """
        try:
            # Ветка для ChatMessage
            if isinstance(response, ChatMessage):
                if getattr(response, "token_usage", None) is None:
                    response.token_usage = SimpleNamespace(input_tokens=0, output_tokens=0)
                else:
                    # Подстрахуемся, если поля отсутствуют
                    if not hasattr(response.token_usage, "input_tokens"):
                        response.token_usage.input_tokens = 0
                    if not hasattr(response.token_usage, "output_tokens"):
                        response.token_usage.output_tokens = 0
                return response

            # Общая ветка: если это объект с полем usage
            if hasattr(response, "usage"):
                usage = getattr(response, "usage", None)
                if usage is None:
                    try:
                        setattr(response, "usage", SimpleNamespace(prompt_tokens=0, completion_tokens=0))
                    except Exception:
                        # Если объект иммутабельный (например, pydantic), просто пропустим
                        pass
                else:
                    if not hasattr(usage, "prompt_tokens"):
                        try:
                            usage.prompt_tokens = 0
                        except Exception:
                            pass
                    if not hasattr(usage, "completion_tokens"):
                        try:
                            usage.completion_tokens = 0
                        except Exception:
                            pass

            # Специально для ChatMessage: правим raw.usage если доступен
            if hasattr(response, "raw") and response.raw is not None:
                raw_obj = response.raw
                try:
                    # Вариант 1: объект с атрибутом usage
                    if hasattr(raw_obj, "usage"):
                        ru = getattr(raw_obj, "usage", None)
                        if ru is None:
                            try:
                                setattr(raw_obj, "usage", SimpleNamespace(prompt_tokens=0, completion_tokens=0))
                            except Exception:
                                pass
                        else:
                            if not hasattr(ru, "prompt_tokens"):
                                try:
                                    ru.prompt_tokens = 0
                                except Exception:
                                    pass
                            if not hasattr(ru, "completion_tokens"):
                                try:
                                    ru.completion_tokens = 0
                                except Exception:
                                    pass
                    # Вариант 2: словарь
                    elif isinstance(raw_obj, dict):
                        usage = raw_obj.get("usage")
                        if usage is None:
                            raw_obj["usage"] = {"prompt_tokens": 0, "completion_tokens": 0}
                        else:
                            usage.setdefault("prompt_tokens", 0)
                            usage.setdefault("completion_tokens", 0)
                except Exception:
                    pass
            return response
        except Exception:
            # Никогда не ломаем основной поток из-за вспомогательной подстановки
            return response
    
    def __call__(self, messages: List[ChatMessage], **kwargs) -> Any:
        """
        Выполняет запрос к модели с автоматическими повторными попытками и fallback.
        
        Args:
            messages: Список сообщений для модели
            **kwargs: Дополнительные параметры для модели
            
        Returns:
            Ответ от модели
            
        Raises:
            Exception: Если все попытки и fallback модели исчерпаны
        """
        original_model_index = self.current_model_index
        clamped_max_tokens_once = False
        attempted_model_indices = set()

        while True:
            last_exception = None
            attempted_model_indices.add(self.current_model_index)
            current_model_id = self.model_ids[self.current_model_index]
            
            # Пытаемся выполнить запрос с текущей моделью
            switched_model = False
            for attempt in range(self.max_retries + 1):
                try:
                    # Логируем попытку (кроме первой)
                    if attempt > 0:
                        logger.info(f"Повторная попытка {attempt + 1}/{self.max_retries + 1} для модели {current_model_id}")
                    
                    # Выполняем запрос
                    #logger.info(f"Запрос: {messages}, {kwargs}")
                    normalized_messages = self._normalize_messages_for_model(messages)
                    response = self.model(normalized_messages, **kwargs)
                    response = self._normalize_response_content(response)

                    # Пишем лог ответа (один файл на ответ)
                    if 1 == 1:
                        try:
                            self._write_response_log(response, attempt, current_model_id)
                        except Exception as log_err:
                            logger.debug(f"Не удалось записать лог ответа модели: {log_err}")
                    
                    # Проверяем, что модель вернула не None
                    if response is None:
                        raise Exception(f"Модель {current_model_id} вернула None")
                    
                    # Проверяем корректность структуры response объекта
                    if not self._is_valid_response(response):
                        raise Exception(f"Модель {current_model_id} вернула некорректный response объект: отсутствует атрибут 'choices' или другие обязательные поля")
                    
                    # Проверяем на "пустой" ответ (нет content и tool_calls) - это вызывает ошибку парсинга в ToolCallingAgent
                    if self._is_empty_response(response):
                        error_msg = f"Модель {current_model_id} вернула пустой ответ (нет content и tool_calls), что приводит к ошибке 'The model output does not contain any JSON blob' - нужно в этом случае переходить на fallback модель"
                        logger.warning(error_msg)
                        raise Exception(error_msg)
                    
                    # Проверяем содержимое ответа на наличие текстовых ошибок
                    response_content = self._extract_response_content(response)
                    if response_content and response_content.startswith("Error:"):
                        # Создаем исключение из текстовой ошибки
                        error_exception = Exception(f"API вернул текстовую ошибку: {response_content}")
                        
                        # Проверяем, нужно ли переключиться на fallback модель
                        if self._should_fallback(error_exception) and len(self.model_ids) > 1:
                            logger.warning(f"Обнаружена текстовая ошибка для модели {current_model_id}: {response_content}")
                            next_index = (self.current_model_index + 1) % len(self.model_ids)
                            if next_index in attempted_model_indices:
                                logger.error("Все fallback модели уже были попробованы для текущего запроса")
                                raise error_exception
                            if self._switch_to_fallback():
                                logger.info(f"Переключаемся на fallback модель из-за текстовой ошибки")
                                switched_model = True
                                break  # Выходим из цикла retry для текущей модели
                            else:
                                logger.error(f"Не удалось переключиться на fallback модель")
                        
                        # Если fallback не нужен или недоступен, выбрасываем исключение для retry
                        raise error_exception
                    
                    # Логируем finish_reason для диагностики обрезанных ответов
                    try:
                        _fr = None
                        if hasattr(response, 'choices') and response.choices and len(response.choices) > 0:
                            _fr = getattr(response.choices[0], 'finish_reason', None)
                        elif isinstance(response, dict) and 'choices' in response and response.get('choices'):
                            _fr = response['choices'][0].get('finish_reason')
                        if _fr and _fr != 'stop':
                            logger.warning(
                                f"⚠️ [{current_model_id}] finish_reason='{_fr}' "
                                f"(попытка {attempt + 1}). "
                                f"Ответ мог быть обрезан."
                            )
                    except Exception:
                        pass

                    # Если дошли до этой точки - запрос успешен
                    if attempt > 0:
                        logger.info(f"Запрос успешен после {attempt + 1} попыток для модели {current_model_id}")
                    elif self.current_model_index != original_model_index:
                        logger.info(f"Запрос успешен с fallback моделью {current_model_id}")
                    
                    # Нормализуем usage/token_usage перед возвратом
                    return self._inject_usage_defaults(response)
                    
                except Exception as e:
                    last_exception = e

                    # Специальная обработка: max_tokens слишком большой при огромном входе.
                    # Это НЕ повод переключать модель/ждать — просто уменьшаем max_tokens и повторяем.
                    if not clamped_max_tokens_once:
                        try:
                            if self._maybe_clamp_max_tokens_from_context_error(e, kwargs):
                                clamped_max_tokens_once = True
                                continue
                        except Exception:
                            # Никогда не ломаем поток из-за эвристики
                            pass
                    
                    # Проверяем, нужно ли переключиться на fallback модель
                    if self._should_fallback(e) and len(self.model_ids) > 1:
                        next_index = (self.current_model_index + 1) % len(self.model_ids)
                        wrapped_to_primary = next_index == 0 and self.current_model_index == (len(self.model_ids) - 1)
                        if next_index in attempted_model_indices:
                            logger.error(
                                "Все fallback модели уже были попробованы для текущего запроса; "
                                "останавливаем цикл fallback"
                            )
                            break
                        fallback_details = {
                            "current_model": current_model_id,
                            "current_model_index": self.current_model_index,
                            "error_type": type(e).__name__,
                            "error_message": str(e),
                            "next_fallback": self.model_ids[next_index],
                            "next_fallback_index": next_index,
                            "wrapped_to_primary": wrapped_to_primary,
                            "all_models_cycle": self.model_ids,
                            "attempt": f"{attempt + 1}/{self.max_retries + 1}"
                        }
                        
                        # Для HTTP ошибок добавляем статус код и URL
                        if hasattr(e, 'response'):
                            if hasattr(e.response, 'status_code'):
                                fallback_details["http_status"] = e.response.status_code
                            if hasattr(e.response, 'url'):
                                fallback_details["request_url"] = str(e.response.url)
                            if hasattr(e.response, 'text'):
                                try:
                                    fallback_details["response_preview"] = e.response.text[:500]
                                except Exception:
                                    fallback_details["response_preview"] = "Не удалось прочитать ответ"
                        
                        logger.warning(
                            f"Критическая ошибка для модели {current_model_id}, переключаемся на fallback:\n"
                            f"Детали fallback: {json.dumps(fallback_details, ensure_ascii=False, indent=2)}"
                        )
                        
                        if self._switch_to_fallback():
                            logger.info(f"Успешно переключились на следующую модель: {self.model_ids[self.current_model_index]} и начинаем заново")
                            switched_model = True
                            break  # Выходим из цикла retry для текущей модели
                        else:
                            logger.error(f"Не удалось переключиться на следующую модель (циклически): {self.model_ids[next_index] if len(self.model_ids) > 1 else 'отсутствует'}")
                    
                    # Логируем ошибку для обычного retry
                    if attempt < self.max_retries:
                        if self._should_retry(e):
                            delay = self._get_retry_delay(attempt)
                            
                            # Более подробное логирование ошибки
                            error_details = {
                                "model_id": current_model_id,
                                "attempt": f"{attempt + 1}/{self.max_retries + 1}",
                                "error_type": type(e).__name__,
                                "error_message": str(e),
                                "model_index": self.current_model_index,
                                "available_fallbacks": self.model_ids[1:] if len(self.model_ids) > 1 else []
                            }
                            
                            # Для HTTP ошибок добавляем статус код
                            if hasattr(e, 'response') and hasattr(e.response, 'status_code'):
                                error_details["http_status"] = e.response.status_code
                                if hasattr(e.response, 'text'):
                                    try:
                                        error_details["response_text"] = e.response.text[:1000]  # Первые 1000 символов
                                    except Exception:
                                        error_details["response_text"] = "Не удалось прочитать текст ответа"
                            
                            logger.warning(
                                f"Попытка неудачна для модели {current_model_id}:\n"
                                f"Детали ошибки: {json.dumps(error_details, ensure_ascii=False, indent=2)}\n"
                                f"Повторная попытка через {delay:.2f} секунд..."
                            )
                            time.sleep(delay)
                            continue
                        else:
                            logger.error(
                                f"Критическая ошибка для модели {current_model_id}, повторные попытки не помогут:\n"
                                f"Тип ошибки: {type(e).__name__}\n"
                                f"Сообщение: {e}\n"
                                f"Доступные fallback модели: {self.model_ids[1:] if len(self.model_ids) > 1 else 'отсутствуют'}"
                            )
                            break
                    else:
                        logger.error(
                            f"Все попытки исчерпаны для модели {current_model_id}:\n"
                            f"Общее количество попыток: {self.max_retries + 1}\n"
                            f"Последняя ошибка: {type(e).__name__}: {e}\n"
                            f"Доступные fallback модели: {self.model_ids[1:] if len(self.model_ids) > 1 else 'отсутствуют'}"
                        )
                        
                        # Пытаемся переключиться на fallback модель после исчерпания retry
                        if self._should_fallback(e):
                            next_index = (self.current_model_index + 1) % len(self.model_ids)
                            if len(self.model_ids) > 1 and next_index in attempted_model_indices:
                                logger.error(
                                    "Все fallback модели уже были попробованы для текущего запроса; "
                                    "останавливаем цикл fallback"
                                )
                            elif self._switch_to_fallback():
                                logger.info(f"Переключаемся на fallback модель после исчерпания retry")
                                switched_model = True
                                break  # Выходим из цикла retry для текущей модели
            
            # Если переключились на другую модель — продолжаем цикл
            if switched_model:
                continue
            
            # Если дошли до этой точки - все модели и попытки исчерпаны
            break
        
        # Финальная ошибка
        all_models = ", ".join(self.model_ids)
        raise last_exception or Exception(f"Неизвестная ошибка при выполнении запроса. Попробованы модели: {all_models}")
    
    def __getattr__(self, name):
        """
        Проксирует все остальные атрибуты к оригинальной модели.
        """
        return getattr(self.model, name)
    
    def generate(self, messages: List[ChatMessage], **kwargs) -> Any:
        """
        Выполняет запрос к модели с автоматическими повторными попытками.
        """
        return self.__call__(messages, **kwargs)


def create_retry_model(
    model_id: str,
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    max_retries: int = 3,
    fallback_models: Optional[str] = None,
    **kwargs
) -> RetryOpenAIServerModel:
    """
    Фабричная функция для создания модели с retry механизмом и fallback поддержкой.
    
    Args:
        model_id: Идентификатор основной модели
        api_base: Базовый URL API  
        api_key: API ключ
        max_retries: Максимальное количество повторных попыток
        fallback_models: Строка с запасными моделями через запятую
        **kwargs: Дополнительные параметры
        
    Returns:
        RetryOpenAIServerModel: Настроенная модель с retry и fallback
    """
    return RetryOpenAIServerModel(
        model_id=model_id,
        api_base=api_base,
        api_key=api_key,
        max_retries=max_retries,
        fallback_models=fallback_models,
        **kwargs
    )
