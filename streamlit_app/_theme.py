"""
Тёмная «mission-control» тема для Streamlit-админки MultiAgent.

Инъектирует единый CSS-блок (шрифты, типографика, компоненты, статус-бейджи)
через st.markdown с unsafe_allow_html=True. Функция безопасна для вызова на каждом
цикле рендера Streamlit — повторная инъекция идемпотентна по эффекту.
"""

import streamlit as st

_THEME_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');

/* Типографика: основной текст */
body, [class*="css"] {
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 14px;
}

/* Заголовки */
h1, h2, h3 {
    font-family: 'Bricolage Grotesque', sans-serif;
    color: #E6EAF2;
}
h1 { font-size: 28px; font-weight: 700; }
h2 { font-size: 20px; font-weight: 600; }
h3 { font-size: 16px; font-weight: 600; }

/* Метрики */
[data-testid="stMetricLabel"] {
    font-size: 12px;
    color: #8792A6;
}
[data-testid="stMetricValue"] {
    font-family: 'JetBrains Mono', monospace;
    font-size: 24px;
    font-weight: 600;
    color: #E6EAF2;
}

/* Кнопки */
.stButton > button {
    border-radius: 8px;
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 13px;
    font-weight: 500;
    border: 1px solid #262D3B;
    background-color: #12161F;
    color: #E6EAF2;
    transition: border-color 0.2s, box-shadow 0.2s, background-color 0.2s;
}
.stButton > button:hover {
    border-color: #19E3B1;
    box-shadow: 0 0 0 2px rgba(25, 227, 177, 0.18);
}
.stButton > button[kind="primary"] {
    background-color: #19E3B1;
    border-color: #19E3B1;
    color: #090B12;
    font-weight: 600;
}
.stButton > button[kind="primary"]:hover {
    background-color: #2DD4BF;
    border-color: #2DD4BF;
}

/* Вкладки */
.stTabs [data-baseweb="tab-list"] {
    border-bottom: 1px solid #262D3B;
}
.stTabs [data-baseweb="tab"] {
    font-size: 13px;
    color: #8792A6;
}
.stTabs [data-baseweb="tab"][aria-selected="true"] {
    color: #19E3B1;
    border-bottom: 2px solid #19E3B1;
}

/* Разделитель */
hr {
    border-color: #262D3B;
}

/* Алерты */
.stAlert {
    border-radius: 8px;
}

/* Раскрывашки */
.streamlit-expanderHeader {
    font-family: 'IBM Plex Sans', sans-serif;
    font-size: 13px;
    font-weight: 500;
}

/* Боковая панель */
[data-testid="stSidebar"] {
    border-right: 1px solid #262D3B;
}

/* Скроллбар */
::-webkit-scrollbar {
    width: 6px;
    height: 6px;
}
::-webkit-scrollbar-track {
    background: transparent;
}
::-webkit-scrollbar-thumb {
    background: #262D3B;
    border-radius: 3px;
}
::-webkit-scrollbar-thumb:hover {
    background: #19E3B1;
}

/* Статус-бейджи */
.status-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
}
.status-running {
    background-color: rgba(52, 211, 153, 0.15);
    color: #34D399;
}
.status-completed {
    background-color: rgba(25, 227, 177, 0.15);
    color: #19E3B1;
}
.status-failed {
    background-color: rgba(242, 85, 90, 0.15);
    color: #F2555A;
}
.status-cancelled {
    background-color: rgba(135, 146, 166, 0.15);
    color: #8792A6;
}
</style>
"""


def inject_theme() -> None:
    """Инъектирует CSS тёмной mission-control темы в текущую страницу Streamlit.

    Вызывать сразу после st.set_page_config(). Безопасно вызывать повторно —
    браузер применяет последний идентичный блок стилей без побочных эффектов.
    """
    st.markdown(_THEME_CSS, unsafe_allow_html=True)
