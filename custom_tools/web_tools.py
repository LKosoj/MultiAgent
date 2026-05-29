from requests.exceptions import RequestException
import requests
import json
from utils import get_clean_text
from agent_command import model_summary, model_big
from utils import call_openai_api

def webpage_content(url: str, query: str, query_type: str) -> str:
    """Visits a webpage at the given URL and returns its content as a markdown string.

  Args:
      url: The URL of the webpage to visit.
      query: The user query to search for on the webpage.
      query_type: The type of query to search for on the webpage. Possible values: fullcontent (return full content of the webpage), relevant (return summary with relevant information).

  Returns:
      The content of the webpage converted to Markdown, or an error message if the request fails.
  """
    try:
        markdown_content, title = get_clean_text(url)

        if query_type == "fullcontent":
            return markdown_content
        elif query_type == "relevant":
            system_prompt = f"Ты специалист по вычленению релевантной информации из текста. Ты должен найти в тексте информацию, релевантную запросу и вернуть ее в виде текста. Используй только информацию из источника, не придумывай и не добавляй никакой другой информации!"
            prompt = f"Найди в источнике информацию, релевантную запросу и верни только ее: {query}\n\nИсточник:\n{markdown_content}"
            model=model_summary
            # Делаем саммари из текста
            if len(prompt) > 80000:
                model = model_big
            else:
                model = model_summary

            return call_openai_api(prompt, system_prompt=system_prompt, model=model_summary)
        else:
            return markdown_content

    except RequestException as e:
        return f"Ошибка при загрузке веб-страницы: {str(e)}"
    except Exception as e:
        return f"Произошла непредвиденная ошибка: {str(e)}" 


def http_get(url: str, params: str = "", headers: str = "", timeout: int = 30, expect_json: bool = False) -> str:
    """Выполняет простой HTTP GET-запрос и возвращает содержимое без LLM-преобразований.

  Args:
      url: Полный URL для запроса.
      params: JSON-строка с query-параметрами (опционально).
      headers: JSON-строка с заголовками (опционально).
      timeout: Таймаут запроса в секундах (по умолчанию 30).
      expect_json: Если True, форсирует возврат красиво отформатированного JSON, если это возможно.

  Returns:
      Строка с ответом сервера. Для JSON возвращает pretty-printed JSON, для остальных типов — текст ответа.
    """
    try:
        parsed_params = {}
        parsed_headers = {}

        if params:
            try:
                parsed_params = json.loads(params)
                if not isinstance(parsed_params, dict):
                    parsed_params = {}
            except Exception:
                parsed_params = {}

        if headers:
            try:
                parsed_headers = json.loads(headers)
                if not isinstance(parsed_headers, dict):
                    parsed_headers = {}
            except Exception:
                parsed_headers = {}

        if expect_json and "Accept" not in parsed_headers:
            parsed_headers["Accept"] = "application/json"

        response = requests.get(url, params=parsed_params or None, headers=parsed_headers or None, timeout=timeout)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()

        # Если JSON ожидается или контент JSON — возвращаем красиво оформленный JSON
        if expect_json or ("application/json" in content_type):
            try:
                data = response.json()
                return json.dumps(data, ensure_ascii=False, indent=2)
            except ValueError:
                # Контент не валидный JSON — вернуть как текст
                return response.text

        # Для текстового контента используем response.text
        if any(t in content_type for t in ["text/", "application/xml", "application/xhtml+xml"]):
            return response.text

        # Для остальных типов — вернуть как текст попыткой декодирования, иначе base64? Оставим text
        return response.text

    except RequestException as e:
        return f"Ошибка HTTP-запроса: {str(e)}"
    except Exception as e:
        return f"Произошла непредвиденная ошибка: {str(e)}"