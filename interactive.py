"""Interactive mode: `ioskit` (no arguments, in a terminal) or `ioskit menu`.

A persistent main menu driven by digits. Every menu item runs the SAME Typer command
that can be started directly (`ioskit battery`), so there is no second implementation.
Results are kept in memory for the session only; `clear` empties that history and the
screen and never touches files on disk (reports, --output logs, project files).
A stored result is replayed from memory; the device is not queried again.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from rich import box
from rich.panel import Panel
from rich.text import Text

from ioskit.core import connection
from ioskit.core.result import Status
from ioskit.modules import logs as logs_mod
from ioskit.ui.logo import render_logo

BACK = object()
EXIT_WORDS = {"0", "exit", "quit", "q", "выход"}
HELP_WORDS = {"help", "?", "h", "справка"}
CLEAR_WORDS = {"clear", "cls"}

_YES = {"y", "yes", "д", "да", "1", "true"}
_NO = {"n", "no", "н", "нет", "0", "false"}
_EXIT_MEANING = {0: "выполнено без отчёта", 1: "внутренняя ошибка команды", 2: "устройство не найдено или не выбрано",
                 3: "проблема связи с устройством", 130: "прервано (Ctrl+C)"}


# ---- menu model ------------------------------------------------------------------

@dataclass(frozen=True)
class Prompt:
    key: str
    text: str
    kind: str = "text"                    # bool | int | float | text | choice
    default: Any = None
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    when: Callable[[dict], bool] | None = None
    validate: Callable[[str], str] | None = None


@dataclass(frozen=True)
class MenuItem:
    number: str
    name: str
    label: str
    prompts: tuple[Prompt, ...] = ()
    build: Callable[[dict], list[str]] = lambda a: []
    udid: bool = True                      # command accepts --udid


def _level(text: str) -> str:
    return logs_mod.normalize_level(text)


MASK = Prompt("mask", "Скрыть идентификаторы (UDID, серийный номер, IMEI, MAC)?", "bool", False)


def _flags(a: dict, **mapping: str) -> list[str]:
    return [flag for key, flag in mapping.items() if a.get(key)]


def _logs_args(a: dict) -> list[str]:
    args = ["logs"]
    if a["follow"]:
        args.append("--follow")
    else:
        args += ["--duration", f"{a['duration']:g}"]
    if a["level"]:
        args += ["--level", a["level"]]
    if a["process"]:
        args += ["--process", a["process"]]
    if a["grep"]:
        args += ["--grep", a["grep"]]
    return args


def _not_follow(a: dict) -> bool:
    return not a.get("follow")


ITEMS: tuple[MenuItem, ...] = (
    MenuItem("1", "devices", "Устройства: список USB и состояние соединения",
             (Prompt("pair", "Запустить сопряжение (нужно нажать «Доверять» на iPhone)?", "bool", False),),
             lambda a: ["devices"] + _flags(a, pair="--pair"), udid=False),
    MenuItem("2", "info", "Информация об устройстве", (MASK,), lambda a: ["info"] + _flags(a, mask="--mask")),
    MenuItem("3", "hardware", "Оборудование: чип, память, дисплей", (MASK,),
             lambda a: ["hardware"] + _flags(a, mask="--mask")),
    MenuItem("4", "battery", "Батарея: заряд, циклы, ёмкость, здоровье", (), lambda a: ["battery"]),
    MenuItem("5", "storage", "Хранилище: объём, занято/свободно, приложения",
             (Prompt("apps", "Анализировать приложения (медленнее)?", "bool", True),),
             lambda a: ["storage"] + ([] if a["apps"] else ["--no-apps"])),
    MenuItem("6", "diagnostics", "Диагностика: источники, проверки, предупреждения", (MASK,),
             lambda a: ["diagnostics"] + _flags(a, mask="--mask")),
    MenuItem("7", "network", "Сеть: SIM, оператор, Wi-Fi, USB-соединение", (MASK,),
             lambda a: ["network"] + _flags(a, mask="--mask")),
    MenuItem("8", "firmware", "Прошивка: версия, совместимость, подпись",
             (MASK, Prompt("offline", "Без интернета (не проверять подпись онлайн)?", "bool", False)),
             lambda a: ["firmware"] + _flags(a, mask="--mask", offline="--offline")),
    MenuItem("9", "logs", "Системный журнал (syslog)",
             (Prompt("follow", "Живой поток до Ctrl+C (иначе — захват на несколько секунд)?", "bool", False),
              Prompt("duration", "Длительность захвата, сек", "float", 5.0, minimum=0.5, when=_not_follow),
              Prompt("level", "Минимальный уровень (Error, Warning, Notice, Info, Debug; Enter — любой)", "text", "",
                     validate=lambda t: _level(t)),
              Prompt("process", "Часть имени процесса (Enter — любой)", "text", ""),
              Prompt("grep", "Текст в строке (Enter — любой)", "text", "")),
             _logs_args),
    MenuItem("10", "crashes", "Отчёты о сбоях",
             (Prompt("limit", "Сколько новейших отчётов показать", "int", 20, minimum=1),
              Prompt("details", "Читать содержимое отчётов?", "bool", True)),
             lambda a: ["crashes", "--limit", str(a["limit"])] + ([] if a["details"] else ["--no-details"])),
    MenuItem("11", "apps", "Приложения",
             (Prompt("type", "Тип: user, system или any", "choice", "user", choices=("user", "system", "any")),
              Prompt("search", "Поиск по названию/bundle ID (Enter — без поиска)", "text", ""),
              Prompt("sizes", "Запросить размеры (медленно)?", "bool", False)),
             lambda a: ["apps", "--type", a["type"]] + (["--search", a["search"]] if a["search"] else [])
             + _flags(a, sizes="--sizes")),
)
BY_KEY = {k: item for item in ITEMS for k in (item.number, item.name)}

HELP_LINES = (
    ("1–11, имя", "запустить команду из меню (по номеру или по имени: battery, logs …)"),
    ("12, history", "история результатов сеанса; номер записи — показать сохранённый результат"),
    ("13, device", "выбрать устройство, если подключено несколько"),
    ("clear", "очистить историю сеанса и экран (файлы, отчёты и логи на диске не затрагиваются)"),
    ("help", "эта справка"),
    ("back", "вернуться в главное меню (из параметров, истории, выбора устройства)"),
    ("exit, 0", "выход из программы"),
    ("Ctrl+C", "в меню — выход; во время команды или ввода параметров — отмена и возврат в меню"),
)


# ---- history -----------------------------------------------------------------------

@dataclass(slots=True)
class HistoryEntry:
    n: int
    started: datetime
    command: str
    exit_code: int
    title: str | None
    report: Any | None
    render: Callable[[], None] | None
    summary: str

    @property
    def time_text(self) -> str:
        return self.started.strftime("%H:%M:%S")


def summarize(report: Any | None, exit_code: int, error: str | None = None) -> str:
    if error:
        return f"ошибка: {error}"
    if report is None:
        return _EXIT_MEANING.get(exit_code, f"код выхода {exit_code}")
    c = report.counts()
    text = (f"получено {c[Status.OK]}, недоступно {c[Status.UNAVAILABLE]}, не поддерж. {c[Status.UNSUPPORTED]}, "
            f"ошибок {c[Status.ERROR]}")
    if report.warnings:
        text += f", предупреждений {len(report.warnings)}"
    return text if exit_code == 0 else f"{text}; код выхода {exit_code}"


# ---- hook used by cli._run_report ----------------------------------------------------

_active: "Session | None" = None


def is_active() -> bool:
    return _active is not None


def emit_result(title: str, report: Any, render: Callable[[], None]) -> None:
    """Called by commands after they collected a report. No-op outside an interactive session."""
    if _active is not None:
        _active._captured = (title, report, render)


# ---- default command runner (real Typer app through Click) ------------------------------

def click_runner(args: list[str]) -> int:
    import click
    from typer.main import get_command

    from ioskit.cli import app

    try:
        get_command(app).main(args=list(args), prog_name="ioskit", standalone_mode=False)
        return 0
    except click.exceptions.Exit as exc:
        return int(exc.exit_code)
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code)
    except click.exceptions.Abort:
        raise KeyboardInterrupt


# ---- session -------------------------------------------------------------------------------

class Session:
    def __init__(self, console: Any, *, runner: Callable[[list[str]], int] | None = None,
                 input_fn: Callable[[Any], str] | None = None,
                 device_lister: Callable[[], Any] | None = None,
                 clock: Callable[[], datetime] = datetime.now) -> None:
        self.console = console
        self.runner = runner or click_runner
        self._input = input_fn or console.input
        self._lister = device_lister or connection.list_devices
        self._clock = clock
        self.history: list[HistoryEntry] = []
        self.udid: str | None = None
        self._captured: tuple[str, Any, Callable[[], None]] | None = None

    # -- output helpers --
    def say(self, message: str, style: str = "info") -> None:
        self.console.print(Text(message, style=style))

    def _read(self, prompt: str, style: str = "brand") -> str:
        return str(self._input(Text(prompt, style=style))).strip()

    # -- main loop --
    def run(self) -> int:
        global _active
        previous, _active = _active, self
        try:
            while True:
                self.show_menu()
                try:
                    line = self._read("ioskit> ")
                except KeyboardInterrupt:
                    self.say("\nВыход (Ctrl+C).", "muted")
                    return 0
                except EOFError:
                    self.say("\nВвод закрыт, выход.", "muted")
                    return 0
                if not self.handle(line):
                    self.say("До свидания.", "muted")
                    return 0
        finally:
            _active = previous

    def show_menu(self) -> None:
        body = Text()
        for item in ITEMS:
            body.append(f"{item.number:>3}  ", style="brand")
            body.append(f"{item.name:<12}", style="label")
            body.append(f"{item.label}\n")
        body.append(f"{'12':>3}  ", style="brand")
        body.append(f"{'history':<12}", style="label")
        body.append("История результатов сеанса\n")
        body.append(f"{'13':>3}  ", style="brand")
        body.append(f"{'device':<12}", style="label")
        body.append("Выбрать устройство\n")
        body.append(f"{'0':>3}  ", style="brand")
        body.append(f"{'exit':<12}", style="label")
        body.append("Выход")
        self.console.print(Panel(body, title="Главное меню", title_align="left", border_style="brand",
                                 box=box.ROUNDED, expand=False))
        self.console.print(Text(f"Устройство: {self.udid or 'авто'} · записей в истории: {len(self.history)} · "
                                "help — справка", style="muted"))

    def handle(self, raw: str) -> bool:
        """Process one line from the main menu. Returns False to leave the program."""
        cmd = raw.strip().lower()
        if not cmd:
            return True
        if cmd in EXIT_WORDS:
            return False
        if cmd in HELP_WORDS:
            self.print_help()
        elif cmd in CLEAR_WORDS:
            self.clear()
        elif cmd == "back":
            self.say("Вы уже в главном меню.", "muted")
        elif cmd in ("12", "history", "история"):
            return self.history_menu()
        elif cmd in ("13", "device", "устройство"):
            self.choose_device()
        elif cmd in BY_KEY:
            self.run_item(BY_KEY[cmd])
        else:
            self.say(f"Неизвестная команда «{raw.strip()}». Введите номер из меню или help.", "warn")
        return True

    # -- help / clear --
    def print_help(self) -> None:
        body = Text()
        for i, (key, text) in enumerate(HELP_LINES):
            body.append(("\n" if i else "") + f"{key:<11}", style="label")
            body.append(text)
        body.append("\n\nПрямой запуск без меню: ioskit <команда> (например, ioskit battery).", style="muted")
        self.console.print(Panel(body, title="Справка", title_align="left", border_style="info",
                                 box=box.ROUNDED, expand=False))

    def clear(self) -> None:
        """Empty the in-memory history and the screen. Nothing on disk is touched."""
        count = len(self.history)
        self.history.clear()
        self.console.clear()
        self.console.print(render_logo(self.console))
        self.say(f"История сеанса очищена (записей: {count}). Файлы, отчёты и логи на диске не затронуты.", "ok")

    # -- parameter prompts --
    def _parse(self, p: Prompt, text: str) -> Any:
        t = text.strip()
        if not t:
            return p.default
        if p.kind == "bool":
            low = t.lower()
            if low in _YES:
                return True
            if low in _NO:
                return False
            raise ValueError("ответьте «да» или «нет»")
        if p.kind in ("int", "float"):
            try:
                value = int(t) if p.kind == "int" else float(t.replace(",", "."))
            except ValueError:
                raise ValueError("нужно число") from None
            if p.minimum is not None and value < p.minimum:
                raise ValueError(f"не меньше {p.minimum:g}")
            return value
        if p.kind == "choice":
            if t.lower() not in p.choices:
                raise ValueError(f"допустимо: {', '.join(p.choices)}")
            return t.lower()
        return p.validate(t) if p.validate else t

    def ask(self, p: Prompt) -> Any:
        hint = {"bool": "да/Нет" if not p.default else "Да/нет"}.get(p.kind, f"{p.default}" if p.default not in (None, "") else "Enter — пропустить")
        while True:
            try:
                line = self._read(f"  {p.text} [{hint}] (back — отмена): ", "label")
            except KeyboardInterrupt:
                return BACK
            if line.lower() == "back":
                return BACK
            try:
                return self._parse(p, line)
            except ValueError as exc:
                self.say(f"  {exc}", "warn")

    def collect_params(self, item: MenuItem) -> dict | None:
        answers: dict[str, Any] = {}
        for p in item.prompts:
            if p.when is not None and not p.when(answers):
                answers[p.key] = p.default
                continue
            value = self.ask(p)
            if value is BACK:
                self.say("Возврат в главное меню.", "muted")
                return None
            answers[p.key] = value
        return answers

    # -- running commands --
    def run_item(self, item: MenuItem) -> None:
        answers = self.collect_params(item)
        if answers is None:
            return
        args = item.build(answers)
        if self.udid and item.udid:
            args += ["--udid", self.udid]
        self.say("Выполняется: ioskit " + " ".join(args), "muted")
        self._captured = None
        code, error = 0, None
        try:
            code = self.runner(args)
        except KeyboardInterrupt:
            code = 130
            self.say("\nКоманда прервана.", "warn")
        except Exception as exc:  # noqa: BLE001 - the menu must survive a failing command
            code, error = 1, f"{type(exc).__name__}: {exc}"
            self.say(f"Команда завершилась ошибкой: {error}", "err")
        title, report, render = self._captured or (None, None, None)
        self._captured = None
        self.history.append(HistoryEntry(len(self.history) + 1, self._clock(), " ".join(args), code, title,
                                         report, render, summarize(report, code, error)))

    # -- history --
    def history_menu(self) -> bool:
        """Browse stored results. Returns False if the user asked to exit the program."""
        while True:
            self.console.print(self._history_panel())
            if not self.history:
                return True
            try:
                line = self._read("Номер записи (back — в меню): ", "label").lower()
            except KeyboardInterrupt:
                return True
            if line in ("", "back"):
                return True
            if line in EXIT_WORDS:
                return False
            if line in CLEAR_WORDS:
                self.clear()
                return True
            if line in HELP_WORDS:
                self.print_help()
                continue
            entry = next((e for e in self.history if str(e.n) == line), None)
            if entry is None:
                self.say("Нет записи с таким номером.", "warn")
                continue
            self.show_entry(entry)

    def _history_panel(self) -> Panel:
        body = Text()
        if not self.history:
            body.append("История пуста: результаты появятся после запуска команд.", style="muted")
        for i, e in enumerate(self.history):
            body.append(("\n" if i else "") + f"{e.n:>3}  ", style="brand")
            body.append(f"{e.time_text}  ", style="muted")
            body.append(f"{e.command[:34]:<34}  ", style="label")
            body.append(e.summary, style="info" if e.exit_code == 0 else "warn")
        return Panel(body, title="История сеанса", title_align="left", border_style="brand",
                     box=box.ROUNDED, expand=False)

    def show_entry(self, entry: HistoryEntry) -> None:
        self.say(f"Сохранённый результат #{entry.n} от {entry.time_text} (ioskit {entry.command}); "
                 "устройство повторно не опрашивалось.", "muted")
        if entry.render is None:
            self.say(f"Отчёта нет: {entry.summary}.", "warn")
        else:
            entry.render()

    # -- device selection --
    def choose_device(self) -> None:
        res = self._lister()
        if not res.is_ok:
            self.say(f"Не удалось получить список устройств: {res.reason}", "err")
            if res.hint:
                self.say(f"→ {res.hint}", "info")
            return
        devices = list(res.value or [])
        if not devices:
            self.say("USB-устройства не найдены. Подключите iPhone и повторите.", "warn")
            return
        body = Text("  0  авто (единственное подключённое устройство)")
        for i, d in enumerate(devices, 1):
            body.append(f"\n{i:>3}  {d.udid}" + ("   ← выбрано" if d.udid == self.udid else ""))
        self.console.print(Panel(body, title="Устройства", title_align="left", border_style="brand",
                                 box=box.ROUNDED, expand=False))
        while True:
            try:
                line = self._read("Номер устройства (back — отмена): ", "label").lower()
            except KeyboardInterrupt:
                return
            if line in ("", "back"):
                self.say("Возврат в главное меню.", "muted")
                return
            if line == "0":
                self.udid = None
                self.say("Выбор устройства: авто.", "ok")
                return
            if line.isdigit() and 1 <= int(line) <= len(devices):
                self.udid = devices[int(line) - 1].udid
                self.say(f"Выбрано устройство {self.udid}.", "ok")
                return
            self.say("Введите номер из списка.", "warn")


def run_menu(console: Any, **kwargs: Any) -> int:
    return Session(console, **kwargs).run()
