"""Тексты подтверждений из таблицы 5.3 UX-спеки (без tkinter)."""

from dataclasses import dataclass

from StoryBookManager.gui.shell.run_status import owner_phrase


@dataclass(frozen=True)
class ConfirmSpec:
    title: str                      # заголовок-вопрос
    body: str                       # одна строка последствий
    confirm: str                    # кнопка действия (справа)
    cancel: str = "Отмена"          # слева
    default_cancel: bool = True     # Enter по умолчанию не подтверждает


def stop_remote_run_spec(owner_name: str = "") -> ConfirmSpec:
    """Остановка прогона из другого приложения (сценарий 4.3)."""
    where = owner_phrase(owner_name)
    return ConfirmSpec(
        title="Остановить прогон?",
        body=(f"Прогон идёт {where}. Он остановится после текущего шага; "
              "уже оплаченные отправки видео завершатся."),
        confirm="Остановить",
        cancel="Не останавливать",
    )


def stop_run_spec() -> ConfirmSpec:
    """Остановка своего прогона в Tk (таблица 5.3, строка «Остановка»)."""
    return ConfirmSpec(
        title="Остановить прогон?",
        body=("Прогон остановится после текущего шага. Уже отправленные платные задачи видео "
              "завершатся и будут оплачены. Готовые файлы сохранятся."),
        confirm="Остановить",
        cancel="Не останавливать",
    )
