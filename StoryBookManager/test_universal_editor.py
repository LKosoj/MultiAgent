#!/usr/bin/env python3
"""
Тест универсального JSON редактора
================================

Демонстрирует работу универсального редактора JSON файлов.
"""

import sys
import os
import tkinter as tk
from tkinter import ttk, messagebox
import json
from pathlib import Path

# Добавляем текущую директорию в путь
sys.path.insert(0, str(Path(__file__).parent))

from gui.universal_json_editor import UniversalFormGenerator, SchemaIntrospector
# Удален импорт schemas.py - теперь используется гибридная генерация схем
# from config.schemas import SCHEMA_MAPPING


class UniversalEditorDemo:
    """Демо приложение для тестирования универсального редактора"""
    
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Тест универсального JSON редактора")
        self.root.geometry("900x700")
        
        # Тестовые данные
        self.test_data = {
            "brief": {
                "title": "Колобок - новая версия",
                "genre": "сказка",
                "target_age": "3-5 лет",
                "language": "ru",
                "description": "Современная интерпретация классической русской сказки про Колобка",
                "main_characters": ["Колобок", "Лиса", "Медведь", "Волк"],
                "main_locations": ["Дом бабушки", "Лес", "Тропинка"],
                "pages_min": 5,
                "pages_max": 10,
                "words_per_page_min": 100,
                "words_per_page_max": 200,
                "moral": "Не стоит быть слишком самоуверенным",
                "storybook_prompt": "Напиши добрую сказку про Колобка для детей 3-5 лет"
            },
            "characters": [
                {
                    "name": "Колобок",
                    "age": "новорожденный",
                    "role": "главный герой",
                    "immutable_attributes": {
                        "face_shape": "круглое",
                        "eye_color": "черные точки",
                        "skin_tone": "золотистый",
                        "body_proportions": "идеально круглый"
                    },
                    "variable_attributes": {
                        "base_clothing": "без одежды",
                        "base_hairstyle": "лысый"
                    },
                    "gesture_set": ["катится", "улыбается", "поет"],
                    "speech_patterns": ["я от дедушки ушел", "я от бабушки ушел"]
                }
            ],
            "story": {
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
                    }
                ]
            },
            "screenplay": {
                "concept": "Современная интерпретация сказки про Колобка",
                "director_notes": "Акцент на визуальной составляющей и музыкальном сопровождении",
                "characters": [
                    {
                        "name": "Колобок",
                        "age": "новорожденный",
                        "role": "главный герой"
                    },
                    {
                        "name": "Лиса",
                        "age": "взрослая",
                        "role": "антагонист"
                    }
                ],
                "world_description": "Волшебный лес с яркими красками и мультяшным стилем",
                "screenplay": [
                    {
                        "location_time": "Дом бабушки, утром",
                        "camera_plan": "Общий план",
                        "timing": "00:00:05",
                        "dialogue": [
                            {
                                "character": "Дед",
                                "text": "Испеки мне, баба, колобок!"
                            }
                        ],
                        "storyboard": [
                            {
                                "camera_plan_field": "Средний план",
                                "timing_field": "00:00:02"
                            }
                        ]
                    }
                ]
            }
        }
        
        self.current_schema_type = "screenplay"
        self.form_generator = None
        
        self.create_ui()
        self.load_test_data()
    
    def create_ui(self):
        """Создание интерфейса"""
        # Верхняя панель
        top_frame = ttk.Frame(self.root)
        top_frame.pack(fill="x", padx=10, pady=5)
        
        ttk.Label(top_frame, text="Тест универсального редактора", style="Heading.TLabel").pack(side="left")
        
        # Выбор схемы
        schema_frame = ttk.Frame(top_frame)
        schema_frame.pack(side="right")
        
        ttk.Label(schema_frame, text="Схема:").pack(side="left")
        self.schema_var = tk.StringVar(value=self.current_schema_type)
        self.schema_var.trace_add('write', self.on_schema_changed)
        
        schema_combo = ttk.Combobox(
            schema_frame, 
            textvariable=self.schema_var,
            values=["brief", "story", "characters", "screenplay", "synopsis", "beats"],
            state="readonly",
            width=15
        )
        schema_combo.pack(side="left", padx=5)
        
        # Кнопки
        button_frame = ttk.Frame(self.root)
        button_frame.pack(fill="x", padx=10, pady=5)
        
        ttk.Button(button_frame, text="📊 Получить данные", command=self.get_form_data).pack(side="left", padx=5)
        ttk.Button(button_frame, text="✓ Валидировать", command=self.validate_form).pack(side="left", padx=5)
        ttk.Button(button_frame, text="🔄 Перечитать UI конфиг", command=self.reload_ui_config).pack(side="left", padx=5)
        ttk.Button(button_frame, text="💾 Сохранить в файл", command=self.save_to_file).pack(side="left", padx=5)
        ttk.Button(button_frame, text="📂 Загрузить из файла", command=self.load_from_file).pack(side="left", padx=5)
        
        # Разделитель
        ttk.Separator(self.root, orient="horizontal").pack(fill="x", pady=5)
        
        # Основная область
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        # Создаем скроллируемую область для формы
        self.create_scrollable_area(main_frame)
        
        # Область результатов
        result_frame = ttk.LabelFrame(self.root, text="Результаты", padding=5)
        result_frame.pack(fill="x", padx=10, pady=(0, 10))
        
        self.result_text = tk.Text(result_frame, height=6, wrap=tk.WORD)
        result_scrollbar = ttk.Scrollbar(result_frame, command=self.result_text.yview)
        self.result_text.configure(yscrollcommand=result_scrollbar.set)
        
        self.result_text.pack(side="left", fill="both", expand=True)
        result_scrollbar.pack(side="right", fill="y")
        
        # Теги для раскраски
        self.result_text.tag_configure("success", foreground="#008000")
        self.result_text.tag_configure("error", foreground="#FF0000")
        self.result_text.tag_configure("warning", foreground="#FF8000")
    
    def create_scrollable_area(self, parent):
        """Создание скроллируемой области для формы"""
        self.canvas = tk.Canvas(parent)
        self.scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.canvas.yview)
        self.form_frame = ttk.Frame(self.canvas)
        
        self.form_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        
        self.canvas.create_window((0, 0), window=self.form_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        
        # Привязка колесика мыши
        def _on_mousewheel(event):
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        
        self.canvas.bind("<MouseWheel>", _on_mousewheel)
    
    def load_test_data(self):
        """Загрузка тестовых данных"""
        try:
            data = self.test_data.get(self.current_schema_type, {})
            
            # Создаем новый генератор формы
            self.form_generator = UniversalFormGenerator(
                data, 
                self.current_schema_type, 
                self.on_form_changed
            )
            
            # Создаем форму
            self.form_generator.create_form(self.form_frame)
            
            self.log(f"Загружены тестовые данные для схемы '{self.current_schema_type}'", "success")
            
        except Exception as e:
            self.log(f"Ошибка загрузки тестовых данных: {e}", "error")
    
    def on_schema_changed(self, *args):
        """Обработчик изменения схемы"""
        new_schema = self.schema_var.get()
        if new_schema != self.current_schema_type:
            self.current_schema_type = new_schema
            self.load_test_data()
    
    def on_form_changed(self):
        """Обработчик изменения данных в форме"""
        self.log("Данные формы изменились", "success")
    
    def get_form_data(self):
        """Получение данных из формы"""
        if not self.form_generator:
            self.log("Форма не создана", "error")
            return
        
        try:
            data = self.form_generator.get_form_data()
            json_str = json.dumps(data, indent=2, ensure_ascii=False)
            
            self.log("Данные из формы:", "success")
            self.log(json_str)
            
        except Exception as e:
            self.log(f"Ошибка получения данных: {e}", "error")
    
    def validate_form(self):
        """Валидация формы"""
        if not self.form_generator:
            self.log("Форма не создана", "error")
            return
        
        try:
            errors = self.form_generator.validate_form()
            
            if not errors:
                self.log("✓ Валидация прошла успешно", "success")
            else:
                self.log("❌ Ошибки валидации:", "error")
                for error in errors:
                    self.log(f"  • {error}", "error")
                    
        except Exception as e:
            self.log(f"Ошибка валидации: {e}", "error")
    
    def reload_ui_config(self):
        """Перечитывает UI конфигурацию и пересоздает форму"""
        try:
            # Очищаем кеш модулей для перечитывания конфигурации
            import importlib
            import sys
            
            # Удаляем из кеша модули связанные с UI конфигурацией
            modules_to_reload = [
                'gui.universal_json_editor'
            ]
            
            for module_name in modules_to_reload:
                if module_name in sys.modules:
                    importlib.reload(sys.modules[module_name])
            
            # Пересоздаем форму с новой конфигурацией
            from gui.universal_json_editor import UniversalFormGenerator
            
            # Сохраняем текущие данные
            current_data = {}
            if self.form_generator:
                try:
                    current_data = self.form_generator.get_form_data()
                except:
                    current_data = self.test_data.get(self.current_schema_type, {})
            
            # Создаем новый генератор формы
            self.form_generator = UniversalFormGenerator(
                current_data or self.test_data.get(self.current_schema_type, {}), 
                self.current_schema_type, 
                self.on_form_changed
            )
            
            # Пересоздаем форму
            self.form_generator.create_form(self.form_frame)
            
            self.log("✅ UI конфигурация перечитана и форма обновлена", "success")
            
        except Exception as e:
            self.log(f"❌ Ошибка перечитывания UI конфигурации: {e}", "error")
    
    def save_to_file(self):
        """Сохранение в файл"""
        if not self.form_generator:
            self.log("Форма не создана", "error")
            return
        
        try:
            from tkinter import filedialog
            
            filename = filedialog.asksaveasfilename(
                title="Сохранить данные",
                defaultextension=".json",
                filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
            )
            
            if filename:
                data = self.form_generator.get_form_data()
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                
                self.log(f"Данные сохранены в файл: {filename}", "success")
                
        except Exception as e:
            self.log(f"Ошибка сохранения: {e}", "error")
    
    def load_from_file(self):
        """Загрузка из файла"""
        try:
            from tkinter import filedialog
            
            filename = filedialog.askopenfilename(
                title="Загрузить данные",
                filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
            )
            
            if filename:
                with open(filename, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # Пересоздаем форму с новыми данными
                self.form_generator = UniversalFormGenerator(
                    data, 
                    self.current_schema_type, 
                    self.on_form_changed
                )
                
                self.form_generator.create_form(self.form_frame)
                self.log(f"Данные загружены из файла: {filename}", "success")
                
        except Exception as e:
            self.log(f"Ошибка загрузки: {e}", "error")
    
    def log(self, message: str, tag: str = None):
        """Вывод сообщения в область результатов"""
        self.result_text.insert(tk.END, f"{message}\n", tag)
        self.result_text.see(tk.END)
    
    def run(self):
        """Запуск приложения"""
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    # Запуск демо
    demo = UniversalEditorDemo()
    demo.run()
