"""
Тул для поиска релевантных статей по смысловому запросу.
Использует OpenAI для генерации поисковых запросов и DuckDuckGo для поиска ссылок.
Поддерживает многоязычный поиск и интеграцию с Jina AI.
Асинхронная версия для максимальной производительности.
"""
from typing import List, Optional, Dict, Any
import logging
import urllib.parse
import asyncio
import aiohttp
import httpx
import time
from bs4 import BeautifulSoup
import requests
from ddgs import DDGS
import random
from ddgs.exceptions import DDGSException
from agent_command import model_summary, model_big
import os
from dataclasses import dataclass
from memory import save_memory
from utils import get_clean_text, call_openai_api
import concurrent.futures
logger = logging.getLogger(__name__)

def _safe_run_async(coro):
    """
    Безопасно запускает асинхронную корутину.
    Если event loop уже запущен, запускает в executor.
    Иначе использует asyncio.run().
    """
    try:
        # Проверяем, есть ли уже запущенный event loop
        asyncio.get_running_loop()
        # Если loop уже запущен, запускаем в отдельном потоке с новым loop-ом.
        # Таймаут = 10 минут, как и у остальных внешних вызовов в проекте, чтобы
        # избежать бессрочной блокировки при зависании удалённых сервисов.
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result(timeout=600)
    except RuntimeError:
        # Если event loop не запущен, используем asyncio.run()
        return asyncio.run(coro)

# Настройки
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
}

SEARCH_PROMPT = """
Ты — ассистент, который помогает искать релевантные статьи в интернете по смысловому запросу пользователя. 
Преобразуй следующий текст в {n} поисковых запросов для поисковой системы, чтобы найти наиболее релевантные статьи. 
Ответь только списком поисковых запросов, по одному на строку, без нумерации.

Запрос пользователя: {query}
"""

@dataclass
class SearchResult:
    """Результат поиска с метаданными."""
    url: str
    title: str = ""
    snippet: str = ""
    source: str = ""
    relevance_score: float = 0.0

class WebResearchTool:
    """Основной класс для веб-исследований с асинхронной поддержкой."""
    
    def __init__(self, jina_api_key: Optional[str] = None, max_concurrent: int = 10):
        """
        Инициализация инструмента веб-исследований.
        
        Args:
            jina_api_key: API ключ для Jina AI
            max_concurrent: Максимальное количество одновременных запросов
        """
        self.jina_api_key = jina_api_key or os.getenv('JINA_API_KEY', '')
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.max_concurrent = max_concurrent
        self._semaphore: Optional[asyncio.Semaphore] = None  # Будет создан в нужном event loop
        self._semaphore_loop: Optional[asyncio.AbstractEventLoop] = None

    def close(self) -> None:
        """Закрывает сессию requests во избежание утечки соединений."""
        session = getattr(self, "session", None)
        if session is not None:
            try:
                session.close()
            finally:
                self.session = None  # type: ignore[assignment]

    def _get_semaphore(self):
        """Получает семафор для текущего event loop.

        Не опирается на приватный атрибут Semaphore._loop (его может не быть
        в новых версиях asyncio); сохраняет ссылку на loop явно.
        """
        try:
            loop = asyncio.get_running_loop()
            if self._semaphore is None or self._semaphore_loop is not loop:
                self._semaphore = asyncio.Semaphore(self.max_concurrent)
                self._semaphore_loop = loop
        except RuntimeError:
            # Если event loop не запущен, создаем семафор без привязки к loop
            if self._semaphore is None:
                self._semaphore = asyncio.Semaphore(self.max_concurrent)
                self._semaphore_loop = None
        return self._semaphore
        
    def generate_search_queries(self, user_query: str, n: int = 5) -> List[str]:
        """Генерация поисковых запросов на основе пользовательского ввода."""
        prompt = SEARCH_PROMPT.format(query=user_query, n=n)
        try:
            result = call_openai_api(prompt)
            queries = [line.strip().lstrip('0123456789.- ').strip() 
                      for line in result.split('\n') if line.strip()]
            return queries[:n]
        except Exception as e:
            logger.error(f"Ошибка генерации поисковых запросов: {e}")
            return [user_query]

    def generate_search_queries_lang(self, user_query: str, lang: str, n: int = 3) -> List[str]:
        """Генерация поисковых запросов для конкретного языка."""
        prompts = {
            "ru": (
                f"Сформулируй {n} коротких поисковых запроса (ключевые фразы, не длиннее 5-6 слов) на русском языке по теме: {user_query}. "
                "Не используй номера, не добавляй лишних слов, только сами поисковые фразы.",
                "Ты — эксперт по поисковым системам. Отвечай только списком поисковых фраз на русском языке."
            ),
            "en": (
                f"Generate {n} short search queries (keywords, no more than 5-6 words each) in English for the topic: {user_query}. "
                "No numbering, just the queries.",
                "You are a search engine expert. Reply with a list of short search queries in English only."
            ),
            "zh": (
                f"请用中文为主题\"{user_query}\"生成{n}个简短的搜索引擎关键词（每个不超过6个字），不要编号，只列出关键词。",
                "你是一名搜索引擎专家。只用中文列出搜索关键词，每行一个。"
            )
        }
        
        if lang not in prompts:
            lang = "en"
            
        prompt, system_prompt = prompts[lang]
        result = call_openai_api(prompt, system_prompt=system_prompt)
        
        # Убираем номера и лишние символы
        queries = [line.strip().lstrip('0123456789.- ').strip() 
                  for line in result.split('\n') if line.strip()]
        return queries[:n]

    def duckduckgo_search(self, query: str, max_results: int = 10) -> List[str]:
        """Поиск через DuckDuckGo API."""
        for attempt in range(3):
            results = []
            try:
                logger.info(f"DuckDuckGo поиск #{attempt + 1} для запроса: {query}")
                with DDGS() as ddgs:
                    for r in ddgs.text(query, region='wt-wt', safesearch='off', max_results=max_results):
                        if r.get('href'):
                            results.append(r['href'])
                        if len(results) >= max_results:
                            break
                
                if results:
                    logger.info(f"DuckDuckGo: получено {len(results)} ссылок для запроса: '{query}'")
                    return results
                else:
                    logger.warning(f"DuckDuckGo попытка #{attempt + 1}: результаты не найдены для запроса: '{query}'")
                    
            except DDGSException as e:
                logger.warning(f"DuckDuckGo попытка #{attempt + 1} - ошибка API: {e}")
                if attempt < 2:
                    time.sleep(random.uniform(1, 3))
            except Exception as e:
                logger.warning(f"DuckDuckGo попытка #{attempt + 1} - неожиданная ошибка: {e}")
                if attempt < 2:
                    time.sleep(random.uniform(1, 3))
        
        logger.warning(f"DuckDuckGo не нашёл ссылок по запросу '{query}' после 3 попыток.")
        return []

    async def jina_search_async(self, query: str, max_results: int = 10) -> List[str]:
        """Асинхронный поиск через Jina AI API."""
        if not self.jina_api_key:
            logger.warning("Jina AI API ключ не настроен")
            return []
        
        semaphore = self._get_semaphore()
        async with semaphore:
            try:
                url = f"https://s.jina.ai/?q={urllib.parse.quote(query)}"
                headers = {
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.jina_api_key}",
                    "X-Respond-With": "no-content"
                }
                
                logger.info(f"Jina AI поиск запроса: '{query}'")
                
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                        response.raise_for_status()
                        data = await response.json()
                        
                        if data.get('code') != 200:
                            logger.error(f"Jina AI API ошибка: {data.get('status')}")
                            return []
                        
                        results = []
                        for item in data.get('data', [])[:max_results]:
                            url = item.get('url', '')
                            if url:
                                results.append(url)
                        
                        usage = data.get('meta', {}).get('usage', {})
                        tokens_used = usage.get('tokens', 0)
                        logger.info(f"Jina AI: найдено {len(results)} результатов, токенов: {tokens_used}")
                        
                        return results
                        
            except Exception as e:
                logger.error(f"Jina AI поиск ошибка: {e}")
                return []

    def jina_search(self, query: str, max_results: int = 10) -> List[str]:
        """Синхронная обертка для обратной совместимости."""
        return _safe_run_async(self.jina_search_async(query, max_results))

    async def search_with_fallback_async(self, query: str, max_results: int = 10) -> List[str]:
        """Асинхронный поиск с автоматическим переключением между поисковыми системами."""
        # Сначала пробуем DuckDuckGo
        loop = asyncio.get_event_loop()
        logger.info(f"Поиск в DuckDuckGo: '{query}'")
        ddg_results = await loop.run_in_executor(None, self.duckduckgo_search, query, max_results)
        
        # Если результатов достаточно, возвращаем их
        if len(ddg_results) >= max_results // 2:
            logger.info(f"DuckDuckGo вернул достаточно результатов ({len(ddg_results)}), Jina AI не используется")
            return ddg_results[:max_results]
        
        # Если результатов мало, дополняем через Jina AI
        logger.info(f"DuckDuckGo вернул мало результатов ({len(ddg_results)}), дополняем через Jina AI")
        jina_results = await self.jina_search_async(query, max_results - len(ddg_results))
        
        # Объединяем результаты
        all_results = list(ddg_results) + list(jina_results)
        
        # Удаляем дубликаты, сохраняя порядок
        seen = set()
        unique_results = []
        for url in all_results:
            if url not in seen:
                seen.add(url)
                unique_results.append(url)
        
        logger.info(f"Итого для запроса '{query}': DuckDuckGo={len(ddg_results)}, Jina={len(jina_results)}, уникальных={len(unique_results)}")
        return unique_results[:max_results]

    def search_with_fallback(self, query: str, max_results: int = 10) -> List[str]:
        """Синхронная обертка для обратной совместимости."""
        return _safe_run_async(self.search_with_fallback_async(query, max_results))

    async def extract_content_preview_async(self, url: str) -> Dict[str, str]:
        """Асинхронное извлечение содержимого страницы."""
        semaphore = self._get_semaphore()
        async with semaphore:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10), 
                                         allow_redirects=True, headers=HEADERS) as response:
                        response.raise_for_status()
                        content = await response.read()
                        
                        soup = BeautifulSoup(content, 'html.parser')
                        
                        # Извлекаем title
                        title = soup.find('title')
                        title_text = title.get_text().strip() if title else ""
                        
                        # Извлекаем description
                        meta_desc = soup.find('meta', attrs={'name': 'description'})
                        if not meta_desc:
                            meta_desc = soup.find('meta', attrs={'property': 'og:description'})
                        description = meta_desc.get('content', '').strip() if meta_desc else ""
                        
                        # Если нет description, берем первый абзац
                        if not description:
                            first_p = soup.find('p')
                            if first_p:
                                description = first_p.get_text().strip()
                        
                        return {
                            'title': title_text,
                            'description': description,
                            'url': url
                        }
                        
            except Exception as e:
                logger.warning(f"Не удалось извлечь содержимое из {url}: {e}")
                return {'title': '', 'description': '', 'url': url}

    def extract_content_preview(self, url: str) -> Dict[str, str]:
        """Синхронная обертка для обратной совместимости."""
        return _safe_run_async(self.extract_content_preview_async(url))

    async def fetch_url_content_async(self, url: str) -> Dict[str, str]:
        """Асинхронное извлечение полного содержимого статьи."""
        try:
            # get_clean_text синхронная, но мы можем запустить её в executor
            loop = asyncio.get_event_loop()
            content, title = await loop.run_in_executor(None, get_clean_text, url)
            
            return {
                'title': title or 'Без заголовка',
                'content': content,
                'url': url
            }
            
        except Exception as e:
            logger.warning(f"Не удалось извлечь содержимое из {url} через content_parser: {e}")
            return await self.extract_content_preview_async(url)

    def fetch_url_content(self, url: str) -> Dict[str, str]:
        """Синхронная обертка для обратной совместимости."""
        return _safe_run_async(self.fetch_url_content_async(url))

    def round_robin_merge(self, lists: List[List[str]]) -> List[str]:
        """Объединение списков методом round-robin без дубликатов."""
        merged = []
        seen = set()
        max_len = max(len(lst) for lst in lists) if lists else 0
        
        for i in range(max_len):
            for lst in lists:
                if i < len(lst):
                    link = lst[i]
                    if link not in seen:
                        merged.append(link)
                        seen.add(link)
        return merged

    async def find_relevant_articles_async(self, user_query: str, include_content: bool = False) -> Dict[str, Any]:
        """
        Асинхронно находит релевантные статьи по смысловому запросу пользователя на русском, английском и китайском языках.
        
        Args:
            user_query: Фраза или абзац пользователя
            include_content: Включить ли краткое содержимое страниц
            
        Returns:
            dict: Результаты поиска по языкам с метаданными
        """
        # Генерируем запросы для разных языков
        queries_ru = self.generate_search_queries_lang(user_query, "ru", 2)
        queries_en = self.generate_search_queries_lang(user_query, "en", 3)
        queries_zh = self.generate_search_queries_lang(user_query, "zh", 2)
        
        logger.info(f"Поисковые запросы RU: {queries_ru}")
        logger.info(f"Поисковые запросы EN: {queries_en}")
        logger.info(f"Поисковые запросы ZH: {queries_zh}")
        
        # Создаем задачи для параллельного поиска
        search_tasks = []
        
        # Русские запросы
        for query in queries_ru:
            task = self.search_with_fallback_async(query, max_results=5)
            search_tasks.append(('ru', task))
        
        # Английские запросы
        for query in queries_en:
            task = self.search_with_fallback_async(query, max_results=5)
            search_tasks.append(('en', task))
        
        # Китайские запросы
        for query in queries_zh:
            task = self.search_with_fallback_async(query, max_results=5)
            search_tasks.append(('zh', task))
        
        # Ждем завершения всех поисковых задач
        search_results = await asyncio.gather(*[task for _, task in search_tasks], return_exceptions=True)
        
        # Группируем результаты по языкам
        ru_results = []
        en_results = []
        zh_results = []
        
        for i, (lang, _) in enumerate(search_tasks):
            result = search_results[i]
            if isinstance(result, Exception):
                logger.error(f"Ошибка поиска для языка {lang}: {result}")
                result = []
            
            if lang == 'ru':
                ru_results.append(result)
            elif lang == 'en':
                en_results.append(result)
            elif lang == 'zh':
                zh_results.append(result)
        
        # Объединяем результаты методом round-robin
        links_ru = self.round_robin_merge(ru_results)
        links_en = self.round_robin_merge(en_results)
        links_zh = self.round_robin_merge(zh_results)
        
        result = {
            'query': user_query,
            'search_queries': {
                'ru': queries_ru,
                'en': queries_en,
                'zh': queries_zh
            },
            'results': {
                'ru': links_ru[:15],  # Ограничиваем количество результатов
                'en': links_en[:15],
                'zh': links_zh[:15]
            },
            'total_results': len(links_ru) + len(links_en) + len(links_zh)
        }
        
        # Добавляем содержимое страниц, если запрошено
        if include_content:
            content_tasks = []
            for lang in ['ru', 'en', 'zh']:
                for url in result['results'][lang][:5]:  # Ограничиваем для скорости
                    task = self.extract_content_preview_async(url)
                    content_tasks.append((lang, task))
            
            # Ждем завершения всех задач извлечения контента
            content_results = await asyncio.gather(*[task for _, task in content_tasks], return_exceptions=True)
            
            # Группируем результаты по языкам
            content_by_lang = {'ru': [], 'en': [], 'zh': []}
            for i, (lang, _) in enumerate(content_tasks):
                content = content_results[i]
                if isinstance(content, Exception):
                    logger.error(f"Ошибка извлечения контента: {content}")
                    content = {'title': '', 'description': '', 'url': ''}
                content_by_lang[lang].append(content)
            
            for lang in ['ru', 'en', 'zh']:
                result['results'][lang + '_content'] = content_by_lang[lang]
        
        logger.info(f"Итого найдено: RU: {len(links_ru)}, EN: {len(links_en)}, ZH: {len(links_zh)} уникальных ссылок")
        return result

    def find_relevant_articles(self, user_query: str, include_content: bool = False) -> Dict[str, Any]:
        """Синхронная обертка для обратной совместимости."""
        return _safe_run_async(self.find_relevant_articles_async(user_query, include_content))

    def search_academic_papers(self, query: str, max_results: int = 10) -> List[str]:
        """Поиск академических статей и научных публикаций."""
        academic_queries = [
            f"site:arxiv.org {query}",
            f"site:scholar.google.com {query}",
            f"site:researchgate.net {query}",
            f"site:pubmed.ncbi.nlm.nih.gov {query}",
            f"filetype:pdf {query} research paper"
        ]
        
        all_results = []
        for academic_query in academic_queries:
            results = self.search_with_fallback(academic_query, max_results=max_results//len(academic_queries))
            all_results.extend(results)
        
        return list(dict.fromkeys(all_results))[:max_results]  # Удаляем дубликаты

    def search_news(self, query: str, max_results: int = 10) -> List[str]:
        """Поиск новостных статей."""
        news_queries = [
            f"{query} news",
            f"{query} latest",
            f"site:bbc.com {query}",
            f"site:reuters.com {query}",
            f"site:cnn.com {query}"
        ]
        
        all_results = []
        for news_query in news_queries:
            results = self.search_with_fallback(news_query, max_results=max_results//len(news_queries))
            all_results.extend(results)
        
        return list(dict.fromkeys(all_results))[:max_results]

    async def web_research_async(self, session_id: str, user_query: str) -> str:
        """
        Асинхронно выполняет веб-исследование и возвращает форматированную строку с результатами.
        
        Args:
            session_id: Идентификатор сессии для сохранения в памяти
            user_query: Запрос пользователя
            
        Returns:
            str: Форматированная строка со всеми найденными статьями
        """
        result = await self.find_relevant_articles_async(user_query, include_content=True)
        
        # Формируем строку с результатами, включая содержимое статей
        output_lines = []
        article_counter = 0
                
        # Добавляем найденные статьи по языкам с их содержимым
        round_robin_merge = self.round_robin_merge([result['results']['ru'], result['results']['en'], result['results']['zh']])
        
        # Создаем задачи для параллельного извлечения полного контента
        content_tasks = []
        for url in round_robin_merge[:15]:
            task = self.fetch_url_content_async(url)
            content_tasks.append((url, task))
        
        # Ждем завершения всех задач извлечения контента
        content_results = await asyncio.gather(*[task for _, task in content_tasks], return_exceptions=True)
        
        # Формируем результат
        for i, (url, _) in enumerate(content_tasks):
            article_counter += 1
            logger.info(f"Обработка статьи {article_counter}: {url}")
            
            content_info = content_results[i]
            if isinstance(content_info, Exception):
                logger.error(f"Ошибка извлечения контента из {url}: {content_info}")
                content_info = {'title': 'Ошибка загрузки', 'content': '', 'url': url}
            
            output_lines.append(f"<article_start_{article_counter}>")
            output_lines.append(f"Статья {article_counter}:")
            output_lines.append(f"URL: {content_info.get('url', '')}")
            output_lines.append(f"Заголовок: {content_info.get('title', 'Без заголовка')}")
            if content_info.get('content'):
                output_lines.append(f"Полное содержание: {content_info.get('content', '')}")
            output_lines.append(f"<article_end_{article_counter}>")
            output_lines.append("")  # Пустая строка для разделения
                
        text = "\n".join(output_lines)
        logger.info(f"Сформирован текст для анализа, общее количество статей: {article_counter}, длина текста: {len(text)}, текст: {text[:1000]}")

        model = model_summary
        # Делаем саммари из текста
        if len(text) > 80000:
            model = model_big
        else:
            model = model_summary
        prompt = f"Сделай максимально подробный обзор текста, сохранив все детали и факты, математические формулы и т.д. Сохрани ссылки на статьи, из которых ты составляешь обзор. Обзор должен соответствовать запросу пользователя: {user_query}. Не включай в обозр информацию, не относящуюся к запросу пользователя! Обзор должен быть на русском языке. Текст:\n {text}"
        
        # Выполняем саммари в executor, так как call_openai_api синхронная
        loop = asyncio.get_event_loop()
        summary = await loop.run_in_executor(
            None, 
            lambda: call_openai_api(
                prompt=prompt,
                system_prompt="Ты специалист по созданию подробных обзоров. На входе ты получаешь несколько статей, на основе которых ты составляешь общий обзор, соответствующий запросу пользователя. Ты составляешь обзоры на русском языке, сохраняя все детали и факты, математические формулы и т.д! Обязательно сохраняй ссылки на статьи, из которых ты составляешь обзор!",
                max_tokens=60000,
                model=model
            )
        )

        # Сохраняем в память (память всегда включена)
        await loop.run_in_executor(
            None, 
            lambda: save_memory(
                session_id=session_id, 
                agent_name="researcher", 
                data={
                    "task": f"Веб-исследование по запросу: {user_query}",
                    "summary": summary
                }
            )
        )
        return summary

    def web_research(self, session_id: str, user_query: str) -> str:
        """Синхронная обертка для обратной совместимости."""
        return _safe_run_async(self.web_research_async(session_id, user_query))

# Создаем глобальный экземпляр для удобства использования
web_research_tool = WebResearchTool()

async def web_research_async(session_id: str, user_query: str) -> str:
    """
    Асинхронная версия веб-исследования.
    Возвращает форматированную строку с результатами веб-исследования.
    
    Args:
        session_id: Идентификатор сессии для сохранения результатов в память
        user_query: Поисковый запрос пользователя
        
    Returns:
        Форматированная строка с результатами исследования
    """
    result = await web_research_tool.web_research_async(session_id, user_query)
    return result

def web_research(session_id: str, user_query: str) -> str:
    """
    Синхронная обертка для обратной совместимости.
    Возвращает форматированную строку с результатами веб-исследования.
    
    Args:
        session_id: Идентификатор сессии для сохранения результатов в память
        user_query: Поисковый запрос пользователя
        
    Returns:
        Форматированная строка с результатами исследования
    """
    return _safe_run_async(web_research_tool.web_research_async(session_id, user_query))
