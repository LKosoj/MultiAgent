#!/usr/bin/env python3
"""
Демо редактора страниц
====================

Показывает работу специального редактора для страниц story.json
"""

import sys
import tkinter as tk
from tkinter import ttk
from pathlib import Path

# Добавляем путь к проекту
sys.path.insert(0, str(Path(__file__).parent))

from gui.universal_json_editor import UniversalFormGenerator


def main():
    """Демо редактора страниц"""
    root = tk.Tk()
    root.title("Демо - Редактор страниц сказки")
    root.geometry("1000x700")
    
    # Тестовые данные story
    story_data = {
        "title": "Колобок - новая версия",
        "pages": [
            {
                "page": 1,
                "title": "Начало истории",
                "body": "Жили-были дед да баба. Попросил дед бабу: \"Испеки мне, баба, колобок\". Баба по амбару помела, по сусечкам поскребла, набрала горсти две муки и испекла колобок."
            },
            {
                "page": 2,
                "title": "Побег колобка", 
                "body": "Положила баба колобок на окошко студиться. Надоело колобку лежать, он и покатился с окошка на лавку, с лавки на пол, по полу к двери, перепрыгнул через порог в сени, из сеней на крыльцо, с крыльца на двор, со двора в огород, из огорода в лес."
            },
            {
                "page": 3,
                "title": "Встреча с зайцем",
                "body": "Катится колобок по дороге, а навстречу ему заяц: \"Колобок, колобок! Я тебя съем!\" - \"Не ешь меня, косой зайчик! Я тебе песенку спою\", - сказал колобок и запел: \"Я колобок, колобок! Я по коробу скребён, по сусеку метён, на сметане мешон, на окошке стужон; я от дедушки ушёл, я от бабушки ушёл, и от тебя, зайца, не хитро уйти!\""
            }
        ]
    }
    
    def on_change():
        print("📝 Данные изменились")
    
    # Заголовок
    header_frame = ttk.Frame(root)
    header_frame.pack(fill="x", padx=10, pady=10)
    
    ttk.Label(header_frame, text="🎭 Редактор страниц сказки", style="Heading.TLabel").pack(side="left")
    
    # Кнопки
    button_frame = ttk.Frame(header_frame)
    button_frame.pack(side="right")
    
    def get_data():
        data = form_generator.get_form_data()
        print("📊 Данные из формы:")
        print(f"Заголовок: {data.get('title')}")
        print(f"Количество страниц: {len(data.get('pages', []))}")
        for i, page in enumerate(data.get('pages', [])):
            print(f"  Страница {page.get('page')}: '{page.get('title')}' ({len(page.get('body', ''))} символов)")
    
    def validate_data():
        errors = form_generator.validate_form()
        if errors:
            print("❌ Ошибки валидации:")
            for error in errors:
                print(f"  • {error}")
        else:
            print("✅ Валидация прошла успешно")
    
    ttk.Button(button_frame, text="📊 Получить данные", command=get_data).pack(side="left", padx=5)
    ttk.Button(button_frame, text="✓ Валидировать", command=validate_data).pack(side="left", padx=5)
    
    # Разделитель
    ttk.Separator(root, orient="horizontal").pack(fill="x", pady=5)
    
    # Скроллируемая область для формы
    canvas = tk.Canvas(root)
    scrollbar = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
    form_frame = ttk.Frame(canvas)
    
    form_frame.bind(
        "<Configure>",
        lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
    )
    
    canvas.create_window((0, 0), window=form_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    
    # Создаем генератор формы для story
    form_generator = UniversalFormGenerator(story_data, "story", on_change)
    form_generator.create_form(form_frame)
    
    # Привязка колесика мыши
    def _on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    
    canvas.bind("<MouseWheel>", _on_mousewheel)
    
    print("🚀 Демо редактора страниц запущено!")
    print("💡 Попробуйте:")
    print("   • Переключиться между вкладками страниц")
    print("   • Добавить новую страницу")
    print("   • Изменить порядок страниц")
    print("   • Отредактировать содержимое")
    print("   • Нажать 'Получить данные' для просмотра результата")
    
    root.mainloop()


if __name__ == "__main__":
    main()
