"""
Утилиты для улучшения работы со скроллбарами
===========================================

Модуль содержит функции для добавления поддержки прокрутки колесом мыши
к различным виджетам Tkinter.
"""

import tkinter as tk
from tkinter import ttk
import sys
import logging

logger = logging.getLogger(__name__)


def bind_mousewheel_to_widget(widget, canvas=None, orientation="vertical"):
    """
    Привязывает прокрутку колесом мыши к виджету.
    
    Args:
        widget: Виджет, к которому привязывается прокрутка (Treeview, Text, Listbox, Canvas и т.д.)
        canvas: Canvas для скроллируемых фреймов (опционально)
        orientation: Ориентация прокрутки ("vertical" или "horizontal")
    """
    try:
        def on_mousewheel(event):
            # Определяем направление прокрутки в зависимости от платформы
            if sys.platform == "win32":
                delta = int(-1 * (event.delta / 120))
            elif sys.platform == "darwin":  # macOS
                delta = int(-1 * event.delta)
            else:  # Linux
                if event.num == 4:
                    delta = -1
                elif event.num == 5:
                    delta = 1
                else:
                    return
            
            # Выполняем прокрутку
            if canvas:
                # Для скроллируемых фреймов с Canvas
                if orientation == "vertical":
                    canvas.yview_scroll(delta, "units")
                else:
                    canvas.xview_scroll(delta, "units")
            else:
                # Для обычных виджетов
                if hasattr(widget, 'yview_scroll') and orientation == "vertical":
                    widget.yview_scroll(delta, "units")
                elif hasattr(widget, 'xview_scroll') and orientation == "horizontal":
                    widget.xview_scroll(delta, "units")
        
        # Привязываем события для разных платформ
        widget.bind("<MouseWheel>", on_mousewheel)  # Windows и macOS
        widget.bind("<Button-4>", on_mousewheel)    # Linux (прокрутка вверх)
        widget.bind("<Button-5>", on_mousewheel)    # Linux (прокрутка вниз)
        
        logger.debug(f"Привязана прокрутка колесом мыши к {type(widget).__name__}")
        
    except Exception as e:
        logger.error(f"Ошибка привязки прокрутки к виджету: {e}")


def bind_mousewheel_to_treeview(treeview):
    """
    Специализированная функция для привязки прокрутки к Treeview.
    Поддерживает как вертикальную, так и горизонтальную прокрутку.
    """
    try:
        def on_mousewheel(event):
            # Проверяем, нажата ли клавиша Shift для горизонтальной прокрутки
            if event.state & 0x1:  # Shift нажат
                # Горизонтальная прокрутка
                if sys.platform == "win32":
                    delta = int(-1 * (event.delta / 120))
                elif sys.platform == "darwin":
                    delta = int(-1 * event.delta)
                else:
                    if event.num == 4:
                        delta = -1
                    elif event.num == 5:
                        delta = 1
                    else:
                        return
                
                if hasattr(treeview, 'xview_scroll'):
                    treeview.xview_scroll(delta, "units")
            else:
                # Вертикальная прокрутка
                if sys.platform == "win32":
                    delta = int(-1 * (event.delta / 120))
                elif sys.platform == "darwin":
                    delta = int(-1 * event.delta)
                else:
                    if event.num == 4:
                        delta = -1
                    elif event.num == 5:
                        delta = 1
                    else:
                        return
                
                if hasattr(treeview, 'yview_scroll'):
                    treeview.yview_scroll(delta, "units")
        
        # Привязываем события
        treeview.bind("<MouseWheel>", on_mousewheel)
        treeview.bind("<Button-4>", on_mousewheel)
        treeview.bind("<Button-5>", on_mousewheel)
        
        logger.debug(f"Привязана расширенная прокрутка к Treeview")
        
    except Exception as e:
        logger.error(f"Ошибка привязки прокрутки к Treeview: {e}")


def bind_mousewheel_to_canvas_frame(canvas, scrollable_frame=None):
    """
    Привязывает прокрутку колесом мыши к Canvas со скроллируемым фреймом.
    
    Args:
        canvas: Canvas виджет
        scrollable_frame: Фрейм внутри Canvas (опционально)
    """
    try:
        def on_mousewheel(event):
            if sys.platform == "win32":
                delta = int(-1 * (event.delta / 120))
            elif sys.platform == "darwin":
                delta = int(-1 * event.delta)
            else:
                if event.num == 4:
                    delta = -1
                elif event.num == 5:
                    delta = 1
                else:
                    return
            
            # Прокручиваем Canvas
            canvas.yview_scroll(delta, "units")
        
        # Привязываем к Canvas
        canvas.bind("<MouseWheel>", on_mousewheel)
        canvas.bind("<Button-4>", on_mousewheel)
        canvas.bind("<Button-5>", on_mousewheel)
        
        # Делаем Canvas фокусируемым для получения событий мыши
        canvas.focus_set()
        
        # Если есть фрейм, привязываем и к нему
        if scrollable_frame:
            # Рекурсивно привязываем ко всем дочерним виджетам
            def bind_to_children(widget):
                try:
                    if not widget.winfo_exists():
                        return
                    widget.bind("<MouseWheel>", on_mousewheel)
                    widget.bind("<Button-4>", on_mousewheel)
                    widget.bind("<Button-5>", on_mousewheel)

                    def on_enter(event, w=widget):
                        canvas.focus_set()

                    def on_leave(event, w=widget):
                        pass

                    try:
                        widget.bind("<Enter>", on_enter)
                        widget.bind("<Leave>", on_leave)
                    except tk.TclError:
                        pass

                    for child in widget.winfo_children():
                        bind_to_children(child)
                except tk.TclError:
                    pass
                except Exception as e:
                    logger.debug(f"Не удалось привязать прокрутку к виджету {type(widget).__name__}: {e}")

            bind_to_children(scrollable_frame)

            _rebind_after_id = [None]

            def schedule_rebind_children():
                if _rebind_after_id[0] is not None:
                    try:
                        canvas.after_cancel(_rebind_after_id[0])
                    except tk.TclError:
                        pass
                    _rebind_after_id[0] = None

                def deferred():
                    _rebind_after_id[0] = None
                    try:
                        if not scrollable_frame.winfo_exists():
                            return
                        bind_to_children(scrollable_frame)
                    except tk.TclError:
                        pass

                try:
                    _rebind_after_id[0] = canvas.after_idle(deferred)
                except tk.TclError:
                    pass

            def update_bindings_on_configure(event):
                schedule_rebind_children()

            scrollable_frame.bind("<Configure>", update_bindings_on_configure)
        
        logger.debug(f"Привязана прокрутка к Canvas и его содержимому")
        
    except Exception as e:
        logger.error(f"Ошибка привязки прокрутки к Canvas: {e}")


def setup_universal_mousewheel(parent_widget):
    """
    Универсальная функция для настройки прокрутки колесом мыши
    для всех подходящих виджетов в родительском контейнере.
    
    Args:
        parent_widget: Родительский виджет (например, Frame, Toplevel)
    """
    try:
        def setup_for_widget(widget):
            widget_class = type(widget).__name__
            
            # Настраиваем прокрутку в зависимости от типа виджета
            if widget_class == "Treeview":
                bind_mousewheel_to_treeview(widget)
            elif widget_class in ["Text", "Listbox"]:
                bind_mousewheel_to_widget(widget)
            elif widget_class == "Canvas":
                # Для Canvas ищем связанные скроллируемые фреймы
                try:
                    # Проверяем, есть ли у Canvas дочерние элементы
                    children = widget.find_all()
                    if children:
                        bind_mousewheel_to_canvas_frame(widget)
                except:
                    pass
            
            # Рекурсивно обрабатываем дочерние виджеты
            try:
                for child in widget.winfo_children():
                    setup_for_widget(child)
            except:
                pass  # Игнорируем ошибки
        
        setup_for_widget(parent_widget)
        logger.info(f"Настроена универсальная прокрутка для {type(parent_widget).__name__}")
        
    except Exception as e:
        logger.error(f"Ошибка настройки универсальной прокрутки: {e}")


def bind_mousewheel_to_text_with_scrollbar(text_widget, scrollbar=None):
    """
    Специализированная функция для Text виджетов со скроллбарами.
    
    Args:
        text_widget: Text виджет
        scrollbar: Связанный Scrollbar (опционально)
    """
    try:
        def on_mousewheel(event):
            if sys.platform == "win32":
                delta = int(-1 * (event.delta / 120))
            elif sys.platform == "darwin":
                delta = int(-1 * event.delta)
            else:
                if event.num == 4:
                    delta = -1
                elif event.num == 5:
                    delta = 1
                else:
                    return
            
            # Прокручиваем Text виджет
            text_widget.yview_scroll(delta, "units")
        
        # Привязываем к Text виджету
        text_widget.bind("<MouseWheel>", on_mousewheel)
        text_widget.bind("<Button-4>", on_mousewheel)
        text_widget.bind("<Button-5>", on_mousewheel)
        
        # Привязываем к скроллбару, если он есть
        if scrollbar:
            scrollbar.bind("<MouseWheel>", on_mousewheel)
            scrollbar.bind("<Button-4>", on_mousewheel)
            scrollbar.bind("<Button-5>", on_mousewheel)
        
        logger.debug(f"Привязана прокрутка к Text виджету и скроллбару")
        
    except Exception as e:
        logger.error(f"Ошибка привязки прокрутки к Text виджету: {e}")


def enable_smooth_scrolling(widget, smooth_factor=3):
    """
    Включает плавную прокрутку для виджета.
    
    Args:
        widget: Виджет для которого включается плавная прокрутка
        smooth_factor: Фактор плавности (чем больше, тем плавнее)
    """
    try:
        def smooth_scroll(event):
            if sys.platform == "win32":
                delta = int(-1 * (event.delta / 120 / smooth_factor))
            elif sys.platform == "darwin":
                delta = int(-1 * event.delta / smooth_factor)
            else:
                if event.num == 4:
                    delta = int(-1 / smooth_factor)
                elif event.num == 5:
                    delta = int(1 / smooth_factor)
                else:
                    return
            
            # Выполняем несколько небольших прокруток для плавности
            for _ in range(smooth_factor):
                if hasattr(widget, 'yview_scroll'):
                    widget.yview_scroll(delta, "units")
                widget.update_idletasks()
        
        widget.bind("<MouseWheel>", smooth_scroll)
        widget.bind("<Button-4>", smooth_scroll)
        widget.bind("<Button-5>", smooth_scroll)
        
        logger.debug(f"Включена плавная прокрутка для {type(widget).__name__}")
        
    except Exception as e:
        logger.error(f"Ошибка включения плавной прокрутки: {e}")


def bind_mousewheel_to_canvas_frame_advanced(canvas, scrollable_frame=None, parent_window=None):
    """
    Улучшенная привязка прокрутки колесом мыши к Canvas со скроллируемым фреймом.
    Использует глобальный перехват событий для более надёжной работы.
    
    Args:
        canvas: Canvas виджет
        scrollable_frame: Фрейм внутри Canvas (опционально)
        parent_window: Родительское окно для глобального перехвата (опционально)
    """
    try:
        def on_mousewheel(event):
            # Проверяем, находится ли курсор над canvas или его содержимым
            try:
                x, y = canvas.winfo_pointerxy()
                widget_under_cursor = canvas.winfo_containing(x, y)
                
                # Проверяем, является ли виджет под курсором частью нашего скроллируемого содержимого
                is_over_scrollable_content = False
                
                if widget_under_cursor:
                    # Проверяем, является ли виджет canvas или его потомком
                    current_widget = widget_under_cursor
                    while current_widget:
                        if current_widget == canvas:
                            is_over_scrollable_content = True
                            break
                        if scrollable_frame and current_widget == scrollable_frame:
                            is_over_scrollable_content = True
                            break
                        # Проверяем, является ли виджет потомком scrollable_frame
                        if scrollable_frame:
                            try:
                                parent = current_widget.master
                                while parent:
                                    if parent == scrollable_frame:
                                        is_over_scrollable_content = True
                                        break
                                    parent = parent.master if hasattr(parent, 'master') else None
                                if is_over_scrollable_content:
                                    break
                            except:
                                pass
                        
                        try:
                            current_widget = current_widget.master if hasattr(current_widget, 'master') else None
                        except:
                            break
                
                if not is_over_scrollable_content:
                    return  # Не прокручиваем, если курсор не над нашим содержимым
                
            except Exception as e:
                logger.debug(f"Ошибка проверки позиции курсора: {e}")
                # В случае ошибки, всё равно выполняем прокрутку
            
            # Выполняем прокрутку
            if sys.platform == "win32":
                delta = int(-1 * (event.delta / 120))
            elif sys.platform == "darwin":
                delta = int(-1 * event.delta)
            else:
                if event.num == 4:
                    delta = -1
                elif event.num == 5:
                    delta = 1
                else:
                    return
            
            try:
                canvas.yview_scroll(delta, "units")
            except Exception as e:
                logger.debug(f"Ошибка прокрутки canvas: {e}")
        
        # Привязываем к родительскому окну для глобального перехвата
        target_widget = parent_window if parent_window else canvas.winfo_toplevel()
        
        # Глобальная привязка событий мыши
        # Используем bind_all, чтобы ловить колесо мыши даже если фокус на дочерних виджетах
        target_widget.bind_all("<MouseWheel>", on_mousewheel, add=True)
        target_widget.bind_all("<Button-4>", on_mousewheel, add=True)  
        target_widget.bind_all("<Button-5>", on_mousewheel, add=True)
        
        # Также привязываем к canvas и фрейму для надёжности
        canvas.bind("<MouseWheel>", on_mousewheel)
        canvas.bind("<Button-4>", on_mousewheel)
        canvas.bind("<Button-5>", on_mousewheel)
        
        if scrollable_frame:
            scrollable_frame.bind("<MouseWheel>", on_mousewheel)
            scrollable_frame.bind("<Button-4>", on_mousewheel)
            scrollable_frame.bind("<Button-5>", on_mousewheel)
        
        logger.debug(f"Привязана улучшенная прокрутка к Canvas с глобальным перехватом")
        
    except Exception as e:
        logger.error(f"Ошибка привязки улучшенной прокрутки к Canvas: {e}")
        # Fallback к обычной привязке
        bind_mousewheel_to_canvas_frame(canvas, scrollable_frame)


def bind_mousewheel_to_canvas_frame_simple(canvas, scrollable_frame=None):
    """
    Простая и надёжная привязка прокрутки колесом мыши к Canvas со скроллируемым фреймом.
    Использует глобальный обработчик событий на уровне корневого окна.
    
    Args:
        canvas: Canvas виджет
        scrollable_frame: Фрейм внутри Canvas (опционально)
    """
    try:
        def on_mousewheel(event):
            # Получаем координаты мыши
            try:
                x, y = event.x_root, event.y_root
                # Преобразуем в координаты canvas
                canvas_x = canvas.winfo_rootx()
                canvas_y = canvas.winfo_rooty()
                canvas_width = canvas.winfo_width()
                canvas_height = canvas.winfo_height()
                
                # Проверяем, находится ли мышь над canvas
                if (canvas_x <= x <= canvas_x + canvas_width and 
                    canvas_y <= y <= canvas_y + canvas_height):
                    
                    # Выполняем прокрутку
                    if sys.platform == "win32":
                        delta = int(-1 * (event.delta / 120))
                    elif sys.platform == "darwin":
                        delta = int(-1 * event.delta)
                    else:
                        if event.num == 4:
                            delta = -1
                        elif event.num == 5:
                            delta = 1
                        else:
                            return
                    
                    try:
                        canvas.yview_scroll(delta, "units")
                    except:
                        pass
                        
            except Exception as e:
                logger.debug(f"Ошибка обработки прокрутки: {e}")
        
        # Получаем корневое окно
        root_window = canvas.winfo_toplevel()
        
        # Привязываем обработчик к корневому окну
        # Используем bind_all на корневом окне
        root_window.bind_all("<MouseWheel>", on_mousewheel, add=True)
        root_window.bind_all("<Button-4>", on_mousewheel, add=True)
        root_window.bind_all("<Button-5>", on_mousewheel, add=True)
        
        logger.debug(f"Привязана простая прокрутка к Canvas через корневое окно")
        
    except Exception as e:
        logger.error(f"Ошибка привязки простой прокрутки: {e}")


def bind_mousewheel_to_all_children(parent_widget, scroll_command):
    """
    Рекурсивно привязывает прокрутку ко всем дочерним виджетам.
    
    Args:
        parent_widget: Родительский виджет
        scroll_command: Функция для обработки прокрутки
    """
    try:
        def on_mousewheel(event):
            if sys.platform == "win32":
                delta = int(-1 * (event.delta / 120))
            elif sys.platform == "darwin":
                delta = int(-1 * event.delta)
            else:
                if event.num == 4:
                    delta = -1
                elif event.num == 5:
                    delta = 1
                else:
                    return
            
            scroll_command(delta)
        
        # Привязываем к самому виджету
        try:
            parent_widget.bind("<MouseWheel>", on_mousewheel)
            parent_widget.bind("<Button-4>", on_mousewheel)
            parent_widget.bind("<Button-5>", on_mousewheel)
        except:
            pass
        
        # Рекурсивно привязываем к дочерним виджетам
        def bind_to_children(widget):
            try:
                widget.bind("<MouseWheel>", on_mousewheel)
                widget.bind("<Button-4>", on_mousewheel)
                widget.bind("<Button-5>", on_mousewheel)
                
                for child in widget.winfo_children():
                    bind_to_children(child)
            except Exception as e:
                logger.debug(f"Не удалось привязать к {type(widget).__name__}: {e}")
        
        bind_to_children(parent_widget)
        
        logger.debug(f"Привязана прокрутка ко всем дочерним виджетам")
        
    except Exception as e:
        logger.error(f"Ошибка рекурсивной привязки: {e}")


def bind_mousewheel_to_canvas_frame_ultimate(canvas, scrollable_frame=None):
    """
    Простой и надёжный способ - привязка напрямую ко всем виджетам.
    
    Args:
        canvas: Canvas виджет
        scrollable_frame: Фрейм внутри Canvas (опционально)
    """
    try:
        def on_mousewheel(event):
            if sys.platform == "win32":
                delta = -1 * (event.delta // 120)
            elif sys.platform == "darwin":
                delta = -1 * event.delta
            else:
                if event.num == 4:
                    delta = -1
                elif event.num == 5:
                    delta = 1
                else:
                    return
            
            try:
                canvas.yview_scroll(delta, "units")
            except:
                pass
        
        def bind_to_all_widgets(widget):
            """Привязывает прокрутку ко всем виджетам рекурсивно"""
            try:
                if not widget.winfo_exists():
                    return
                widget.bind("<MouseWheel>", on_mousewheel)
                widget.bind("<Button-4>", on_mousewheel)
                widget.bind("<Button-5>", on_mousewheel)
                for child in widget.winfo_children():
                    bind_to_all_widgets(child)
            except tk.TclError:
                pass
            except Exception as e:
                logger.debug(f"Не удалось привязать к {type(widget).__name__}: {e}")
        
        # Привязываем к Canvas
        bind_to_all_widgets(canvas)
        
        # Привязываем ко всему содержимому скроллируемого фрейма
        if scrollable_frame:
            bind_to_all_widgets(scrollable_frame)

            # Не вызывать bind_to_all_widgets синхронно из <Configure>: при массовом
            # widget.destroy() это реентрантно и даёт TclError «bad window path name».
            _rebind_after_id = [None]

            def schedule_rebind():
                if _rebind_after_id[0] is not None:
                    try:
                        canvas.after_cancel(_rebind_after_id[0])
                    except tk.TclError:
                        pass
                    _rebind_after_id[0] = None

                def deferred_rebind():
                    _rebind_after_id[0] = None
                    try:
                        if not scrollable_frame.winfo_exists():
                            return
                        bind_to_all_widgets(scrollable_frame)
                    except tk.TclError:
                        pass

                try:
                    _rebind_after_id[0] = canvas.after_idle(deferred_rebind)
                except tk.TclError:
                    pass

            def on_configure(event):
                schedule_rebind()

            scrollable_frame.bind("<Configure>", on_configure, add=True)
        
        logger.debug(f"Применена простая привязка прокрутки ко всем виджетам")
        
    except Exception as e:
        logger.error(f"Ошибка простой привязки прокрутки: {e}")
