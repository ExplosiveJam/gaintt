"""Create the small workbook used by the browser smoke and README."""

from pathlib import Path

from openpyxl import Workbook


def main() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "План"
    sheet.append(["задача", "описание", "исполнитель", "длительность", "предшественники", "Pinned Start", "Due Date"])
    sheet.append(["Исследование", "Собрать вводные", "Иванов", 2, "", "2026-09-01", "2026-09-04"])
    sheet.append(["Дизайн", "Подготовить макет", "Петров", 3, "Исследование", "", "2026-09-09"])
    sheet.append(["Демо", "Показать результат", "Сидорова", 1, "Дизайн", "", "2026-09-15"])
    output = Path(__file__).resolve().parents[1] / "examples" / "kanbain-example.xlsx"
    output.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output)


if __name__ == "__main__":
    main()
