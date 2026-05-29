import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from StoryBookManager.config.settings import app_settings

class SettingsDialog(tk.Toplevel):
    """Диалог настроек приложения"""
    def __init__(self, parent, on_save=None):
        super().__init__(parent)
        self.on_save = on_save
        self.title("Настройки")
        self.geometry("500x400")
        self.transient(parent)
        self.grab_set()
        
        self.create_widgets()
        self.load_current_settings()
        
    def create_widgets(self):
        # Notebook for tabs
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)
        
        # Общие (General) tab
        general_frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(general_frame, text="Общие")
        
        # Директория проектов
        ttk.Label(general_frame, text="Директория проектов:").grid(row=0, column=0, sticky="w", pady=5)
        self.projects_dir_var = tk.StringVar()
        ttk.Entry(general_frame, textvariable=self.projects_dir_var, width=35).grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        ttk.Button(general_frame, text="Обзор...", command=self.browse_projects_dir).grid(row=0, column=2, pady=5)
        general_frame.grid_columnconfigure(1, weight=1)
        
        # Интервал автосохранения
        ttk.Label(general_frame, text="Интервал автосохранения (сек):").grid(row=1, column=0, sticky="w", pady=5)
        self.auto_save_var = tk.StringVar()
        ttk.Entry(general_frame, textvariable=self.auto_save_var, width=10).grid(row=1, column=1, sticky="w", padx=5, pady=5)
        
        # Максимум бэкапов
        ttk.Label(general_frame, text="Максимум бэкапов:").grid(row=2, column=0, sticky="w", pady=5)
        self.max_backup_var = tk.StringVar()
        ttk.Entry(general_frame, textvariable=self.max_backup_var, width=10).grid(row=2, column=1, sticky="w", padx=5, pady=5)
        
        # Логирование (Logging) tab
        logging_frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(logging_frame, text="Логирование")
        
        ttk.Label(logging_frame, text="Уровень логирования:").grid(row=0, column=0, sticky="w", pady=5)
        self.log_level_var = tk.StringVar()
        log_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        self.log_level_combo = ttk.Combobox(logging_frame, textvariable=self.log_level_var, values=log_levels, state="readonly", width=15)
        self.log_level_combo.grid(row=0, column=1, sticky="w", padx=5, pady=5)
        
        # Buttons frame
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=10)
        ttk.Button(btn_frame, text="Сохранить", command=self.save_settings).pack(side="right", padx=5)
        ttk.Button(btn_frame, text="Отмена", command=self.destroy).pack(side="right")
        
    def load_current_settings(self):
        self.projects_dir_var.set(app_settings.get("projects_directory", ""))
        self.auto_save_var.set(str(app_settings.get("auto_save_interval", 30)))
        self.max_backup_var.set(str(app_settings.get("max_backup_files", 10)))
        self.log_level_var.set(app_settings.get("log_level", "INFO"))
        
    def browse_projects_dir(self):
        dir_path = filedialog.askdirectory(initialdir=self.projects_dir_var.get(), parent=self)
        if dir_path:
            self.projects_dir_var.set(dir_path)
            
    def save_settings(self):
        try:
            auto_save = int(self.auto_save_var.get())
            max_backup = int(self.max_backup_var.get())
        except ValueError:
            messagebox.showerror("Ошибка", "Интервал автосохранения и максимум бэкапов должны быть целыми числами.", parent=self)
            return
            
        app_settings.set("projects_directory", self.projects_dir_var.get())
        app_settings.set("auto_save_interval", auto_save)
        app_settings.set("max_backup_files", max_backup)
        app_settings.set("log_level", self.log_level_var.get())
        
        if app_settings.save_settings():
            on_save = getattr(self, "on_save", None)
            if on_save:
                on_save()
            messagebox.showinfo("Успех", "Настройки успешно сохранены.", parent=self)
            self.destroy()
        else:
            messagebox.showerror("Ошибка", "Не удалось сохранить настройки.", parent=self)
