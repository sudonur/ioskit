"""iOSKit command line interface (Typer): devices, info, hardware, battery, storage,
diagnostics, network, firmware, logs, crashes, apps, menu."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional

import typer

from ioskit import __version__, interactive
from ioskit.core import connection
from ioskit.core.device import Device, select_udid
from ioskit.core.collector import Entry
from ioskit.core.result import Result, Status
from ioskit.modules import apps as apps_mod
from ioskit.modules import battery as battery_mod
from ioskit.modules import crashes as crashes_mod
from ioskit.modules import diagnostics as diagnostics_mod
from ioskit.modules import firmware as firmware_mod
from ioskit.modules import network as network_mod
from ioskit.modules import hardware as hardware_mod
from ioskit.modules import info as info_mod
from ioskit.modules import logs as logs_mod
from ioskit.modules import storage as storage_mod
from ioskit.modules.common import Report, static_field
from ioskit.ui.logo import render_logo
from ioskit.ui.records import apps_table, crashes_table, log_line, logs_table
from ioskit.ui.tables import (devices_table, diagnosis_panel, entries_table, header_panel,
                              message_panel, notes_panel, summary_panel, warnings_panel)
from ioskit.ui.theme import make_console
from ioskit.utils.errors import ConnState, classify, diagnosis_for
from ioskit.utils.trace import set_verbose

console = make_console()
app = typer.Typer(add_completion=False, invoke_without_command=True,
                  help="iOSKit - диагностика iPhone по USB.", rich_markup_mode=None)

EXIT_NO_DEVICE, EXIT_CONN_PROBLEM = 2, 3
VerboseOpt = typer.Option(False, "--verbose", "-v", help="Показывать источники данных и ошибки.")


def _is_interactive_tty() -> bool:
    """`ioskit` without arguments opens the menu only when a person is at the terminal."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


@app.callback()
def root(ctx: typer.Context, verbose: bool = VerboseOpt,
         version: bool = typer.Option(False, "--version", help="Версия iOSKit.")) -> None:
    set_verbose(verbose)
    if version:
        console.print(f"iOSKit {__version__}")
        raise typer.Exit()
    if console.is_terminal and not interactive.is_active():
        console.print(render_logo(console))
    if ctx.invoked_subcommand is None:
        if _is_interactive_tty():
            interactive.run_menu(console)
        else:
            console.print(ctx.get_help())
        raise typer.Exit()


@app.command()
def devices(
    pair: bool = typer.Option(False, "--pair", help="Запустить сопряжение (нужно «Доверять» на iPhone)."),
    pair_timeout: float = typer.Option(60.0, "--pair-timeout", help="Сколько ждать подтверждения, сек."),
    verbose: bool = VerboseOpt,
) -> None:
    """Список подключённых по USB устройств и состояние соединения."""
    set_verbose(verbose)
    listed = connection.list_devices()
    if not listed.is_ok:
        state = ConnState(listed.code or "unknown")
        console.print(diagnosis_panel(diagnosis_for(state), console, detail=listed.reason))
        raise typer.Exit(EXIT_CONN_PROBLEM)

    entries = listed.value or []
    if not entries:
        console.print(diagnosis_panel(diagnosis_for(ConnState.NO_DEVICE), console))
        check = connection.crosscheck_idevice_id()
        if check.is_ok and check.value:
            console.print(message_panel(
                f"libimobiledevice видит {len(check.value)} устройство(а), а pymobiledevice3 — нет. "
                "Запустите с --verbose.", console, title="Расхождение"))
        raise typer.Exit(EXIT_NO_DEVICE)

    probes = []
    for entry in entries:
        if pair:
            with console.status("Ожидание подтверждения «Доверять» на iPhone…", spinner="dots"):
                probes.append(connection.probe(entry.udid, pair=True, pair_timeout=pair_timeout))
        else:
            probes.append(connection.probe(entry.udid))

    shown: set[ConnState] = set()
    for p in probes:
        if p.diagnosis and p.state not in shown:
            shown.add(p.state)

    def render() -> None:
        console.print(devices_table(probes, console))
        seen: set[ConnState] = set()
        for p in probes:
            if p.diagnosis and p.state not in seen:
                seen.add(p.state)
                console.print(diagnosis_panel(p.diagnosis, console))

    interactive.emit_result("Устройства", _devices_report(probes), render)
    render()
    if shown:
        raise typer.Exit(EXIT_CONN_PROBLEM)


def _state(res) -> ConnState:
    try:
        return ConnState(res.code or "unknown")
    except ValueError:
        return ConnState.UNKNOWN


def _devices_report(probes) -> Report:
    """History record of `devices`: one entry per device, built from the probe results."""
    entries = []
    for i, p in enumerate(probes, 1):
        if p.diagnosis is None:
            res = Result.ok(f"{p.udid} · iOS {p.os_version.unwrap_or('—')}", "usbmuxd + lockdownd")
        else:
            res = Result.unavailable(p.diagnosis.title, source="usbmuxd", code=p.state.value)
        label = p.model.unwrap_or(None) or p.product_type.unwrap_or(None) or f"устройство {i}"
        entries.append(Entry(static_field(f"dev{i}", label, "Устройства", res), res))
    return Report(entries, None, ("Устройства",))


UdidOpt = typer.Option(None, "--udid", help="UDID (если подключено несколько устройств).")
PairOpt = typer.Option(False, "--pair", help="Запустить сопряжение («Доверять» на iPhone).")
PairTimeoutOpt = typer.Option(60.0, "--pair-timeout", help="Ожидание подтверждения, сек.")
TimeoutOpt = typer.Option(15.0, "--timeout", help="Тайм-аут ответа устройства, сек.")
MaskOpt = typer.Option(False, "--mask", help="Скрыть UDID, серийный номер, IMEI, MAC, ECID.")


def _run_report(*, title: str, header: tuple, collect: Callable[[Device], Report], busy: str,
                udid: Optional[str], pair: bool, pair_timeout: float, verbose: bool,
                mask: bool = False, extras: Optional[Callable[[Report, object], list]] = None) -> None:
    """Shared flow of every device command: select -> connect -> collect -> render."""
    set_verbose(verbose)
    sel = select_udid(udid)
    if not sel.is_ok:
        state = _state(sel)
        console.print(diagnosis_panel(diagnosis_for(state), console, detail=sel.reason))
        raise typer.Exit(EXIT_NO_DEVICE if state in (ConnState.NO_DEVICE, ConnState.MULTIPLE_DEVICES)
                         else EXIT_CONN_PROBLEM)

    dev = Device(sel.value, pair=pair, pair_timeout=pair_timeout)
    try:
        wait = "Ожидание подтверждения «Доверять» на iPhone…" if pair else "Подключение…"
        with console.status(wait, spinner="dots"):
            opened = dev.connect()
        if not opened.is_ok:
            console.print(diagnosis_panel(diagnosis_for(_state(opened)), console, detail=opened.reason))
            raise typer.Exit(EXIT_CONN_PROBLEM)
        with console.status(busy, spinner="dots"):
            report = collect(dev)
    finally:
        dev.close()

    def render() -> None:
        console.print(header_panel(report, console, header, title))
        for section, entries in report.sections():
            console.print(entries_table(section, entries, console, mask=mask))
        for renderable in (extras(report, console) if extras else []):
            console.print(renderable)
        warn = warnings_panel(report, console)
        if warn is not None:
            console.print(warn)
        console.print(summary_panel(report, console))
        notes = notes_panel(report, console)
        if notes is not None:
            console.print(notes)

    interactive.emit_result(title, report, render)   # session history (no-op outside the menu)
    render()
    if report.aborted is not None or report.counts()[Status.OK] == 0:
        raise typer.Exit(EXIT_CONN_PROBLEM)


@app.command()
def info(udid: Optional[str] = UdidOpt, pair: bool = PairOpt, pair_timeout: float = PairTimeoutOpt,
         timeout: float = TimeoutOpt, mask: bool = MaskOpt, verbose: bool = VerboseOpt) -> None:
    """Подробная информация об устройстве: значение, источник и статус каждого параметра."""
    _run_report(title=info_mod.TITLE, header=info_mod.HEADER, busy="Чтение данных устройства…",
                collect=lambda d: info_mod.collect(d, timeout=timeout),
                udid=udid, pair=pair, pair_timeout=pair_timeout, verbose=verbose, mask=mask)


@app.command()
def hardware(udid: Optional[str] = UdidOpt, pair: bool = PairOpt, pair_timeout: float = PairTimeoutOpt,
             timeout: float = TimeoutOpt, mask: bool = MaskOpt, verbose: bool = VerboseOpt) -> None:
    """Оборудование: чип, модель, RAM (справочно), накопитель, дисплей, аппаратные ID."""
    _run_report(title=hardware_mod.TITLE, header=hardware_mod.HEADER, busy="Чтение данных оборудования…",
                collect=lambda d: hardware_mod.collect(d, timeout=timeout),
                udid=udid, pair=pair, pair_timeout=pair_timeout, verbose=verbose, mask=mask)


@app.command()
def battery(udid: Optional[str] = UdidOpt, pair: bool = PairOpt, pair_timeout: float = PairTimeoutOpt,
            timeout: float = TimeoutOpt, verbose: bool = VerboseOpt) -> None:
    """Батарея: заряд, циклы, ёмкости, напряжение, температура, расчётное здоровье."""
    _run_report(title=battery_mod.TITLE, header=battery_mod.HEADER, busy="Чтение данных батареи…",
                collect=lambda d: battery_mod.collect(d, timeout=timeout),
                udid=udid, pair=pair, pair_timeout=pair_timeout, verbose=verbose)


@app.command()
def storage(udid: Optional[str] = UdidOpt, pair: bool = PairOpt, pair_timeout: float = PairTimeoutOpt,
            timeout: float = TimeoutOpt,
            apps: bool = typer.Option(True, "--apps/--no-apps", help="Размеры приложений (может быть медленно)."),
            top: int = typer.Option(5, "--top", help="Сколько крупнейших приложений показать."),
            apps_timeout: float = typer.Option(30.0, "--apps-timeout", help="Тайм-аут опроса приложений, сек."),
            verbose: bool = VerboseOpt) -> None:
    """Хранилище: объём, занято/свободно, файловая система (AFC), данные приложений."""
    _run_report(title=storage_mod.TITLE, header=storage_mod.HEADER, busy="Анализ хранилища…",
                collect=lambda d: storage_mod.collect(d, timeout=timeout, include_apps=apps, top=top,
                                                      apps_timeout=apps_timeout),
                udid=udid, pair=pair, pair_timeout=pair_timeout, verbose=verbose)


@app.command()
def diagnostics(udid: Optional[str] = UdidOpt, pair: bool = PairOpt, pair_timeout: float = PairTimeoutOpt,
                timeout: float = TimeoutOpt, mask: bool = MaskOpt, verbose: bool = VerboseOpt) -> None:
    """Диагностика: доступность источников, компоненты, датчики, проверки согласованности, предупреждения."""
    _run_report(title=diagnostics_mod.TITLE, header=diagnostics_mod.HEADER, busy="Диагностика устройства…",
                collect=lambda d: diagnostics_mod.collect(d, timeout=timeout),
                udid=udid, pair=pair, pair_timeout=pair_timeout, verbose=verbose, mask=mask)


@app.command()
def network(udid: Optional[str] = UdidOpt, pair: bool = PairOpt, pair_timeout: float = PairTimeoutOpt,
            timeout: float = TimeoutOpt, mask: bool = MaskOpt,
            host: bool = typer.Option(True, "--host/--no-host", help="Проверять сторону хоста (интерфейс USB-модема)."),
            verbose: bool = VerboseOpt) -> None:
    """Сеть: SIM и оператор, baseband, Wi-Fi/Bluetooth, IP хоста (USB-модем), диагностика соединения."""
    _run_report(title=network_mod.TITLE, header=network_mod.HEADER, busy="Сетевая диагностика…",
                collect=lambda d: network_mod.collect(d, timeout=timeout, host=host),
                udid=udid, pair=pair, pair_timeout=pair_timeout, verbose=verbose, mask=mask)


@app.command()
def firmware(udid: Optional[str] = UdidOpt, pair: bool = PairOpt, pair_timeout: float = PairTimeoutOpt,
             timeout: float = TimeoutOpt, mask: bool = MaskOpt,
             offline: bool = typer.Option(False, "--offline", help="Не обращаться к интернету (без статуса подписи)."),
             online_timeout: float = typer.Option(8.0, "--online-timeout", help="Тайм-аут запроса к ipsw.me, сек."),
             verbose: bool = VerboseOpt) -> None:
    """Прошивка: версия, сборка, baseband, метаданные, совместимость, статус подписи (ipsw.me)."""
    _run_report(title=firmware_mod.TITLE, header=firmware_mod.HEADER, busy="Анализ прошивки…",
                collect=lambda d: firmware_mod.collect(d, timeout=timeout, online=not offline,
                                                       online_timeout=online_timeout),
                udid=udid, pair=pair, pair_timeout=pair_timeout, verbose=verbose, mask=mask)


def _bad(message: str):
    raise typer.BadParameter(message)


def _logs_filter(level, process, pid, grep, regex) -> logs_mod.LogFilter:
    try:
        return logs_mod.build_filter(level=level, process=process, pid=pid, grep=grep, regex=regex)
    except ValueError as exc:
        _bad(str(exc))


def _check_choice(value: str, allowed, name: str) -> str:
    if value not in allowed:
        _bad(f"{name}: допустимо {', '.join(allowed)}")
    return value


def _follow_logs(*, udid, pair, pair_timeout, backend, flt) -> None:
    """Live log stream until Ctrl-C. Shares device selection and error panels with other commands."""
    sel = select_udid(udid)
    if not sel.is_ok:
        state = _state(sel)
        console.print(diagnosis_panel(diagnosis_for(state), console, detail=sel.reason))
        raise typer.Exit(EXIT_NO_DEVICE if state in (ConnState.NO_DEVICE, ConnState.MULTIPLE_DEVICES)
                         else EXIT_CONN_PROBLEM)
    dev = Device(sel.value, pair=pair, pair_timeout=pair_timeout)
    shown = 0
    stream = None
    try:
        with console.status("Подключение…", spinner="dots"):
            opened = dev.connect()
        if not opened.is_ok:
            console.print(diagnosis_panel(diagnosis_for(_state(opened)), console, detail=opened.reason))
            raise typer.Exit(EXIT_CONN_PROBLEM)
        res = logs_mod.open_stream(dev, backend=backend)
        if not res.is_ok:
            console.print(diagnosis_panel(diagnosis_for(_state(res)), console, detail=res.reason))
            raise typer.Exit(EXIT_CONN_PROBLEM)
        stream = res.value
        console.print(message_panel(f"Источник: {stream.source}\nФильтры: {flt.describe()}\nОстановка: Ctrl-C",
                                    console, style="info", title="Поток журнала"))
        try:
            for raw in stream.lines:
                rec = logs_mod.parse_line(raw, stream.source)
                if flt.matches(rec):
                    console.print(log_line(rec))
                    shown += 1
        except KeyboardInterrupt:
            pass
        except Exception as exc:  # noqa: BLE001 - stream broke: show a typed diagnosis
            diag = classify(exc)
            console.print(diagnosis_panel(diag, console, detail=f"{type(exc).__name__}: {exc}"))
            raise typer.Exit(EXIT_CONN_PROBLEM)
    except KeyboardInterrupt:
        pass
    finally:
        if stream is not None:
            stream.close()
        dev.close()
    console.print(f"Показано записей: {shown}")


@app.command()
def logs(
    udid: Optional[str] = UdidOpt, pair: bool = PairOpt, pair_timeout: float = PairTimeoutOpt,
    timeout: float = TimeoutOpt,
    duration: float = typer.Option(5.0, "--duration", "-d", help="Сколько секунд читать поток."),
    max_lines: int = typer.Option(100, "--max-lines", help="Остановиться после стольких подходящих строк."),
    level: Optional[str] = typer.Option(None, "--level", help="Минимальный уровень: Error, Warning, Notice, Info, Debug…"),
    process: Optional[str] = typer.Option(None, "--process", help="Часть имени процесса."),
    pid: Optional[int] = typer.Option(None, "--pid", help="PID процесса."),
    grep: Optional[str] = typer.Option(None, "--grep", help="Текст в строке (без учёта регистра)."),
    regex: bool = typer.Option(False, "--regex", help="Считать --grep регулярным выражением."),
    backend: str = typer.Option("auto", "--backend", help="auto | pymobiledevice3 | libimobiledevice"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Непрерывный поток до Ctrl-C."),
    output: Optional[Path] = typer.Option(None, "--output", help="Записать подходящие строки в НОВЫЙ файл."),
    verbose: bool = VerboseOpt,
) -> None:
    """Системный журнал (syslog): захват потока с фильтрами; --follow — живой режим."""
    set_verbose(verbose)
    _check_choice(backend, logs_mod.BACKENDS, "--backend")
    flt = _logs_filter(level, process, pid, grep, regex)
    if follow:
        _follow_logs(udid=udid, pair=pair, pair_timeout=pair_timeout, backend=backend, flt=flt)
        return
    _run_report(title=logs_mod.TITLE, header=logs_mod.HEADER, busy=f"Чтение журнала ({duration:g} с)…",
                collect=lambda d: logs_mod.collect(d, timeout=timeout, duration=duration, max_lines=max_lines,
                                                   flt=flt, backend=backend, output=output),
                udid=udid, pair=pair, pair_timeout=pair_timeout, verbose=verbose,
                extras=lambda r, c: ([logs_table(r.extra[0], c, source=r.extra[1])]
                                     if r.extra and r.extra[0] else []))


@app.command()
def crashes(
    udid: Optional[str] = UdidOpt, pair: bool = PairOpt, pair_timeout: float = PairTimeoutOpt,
    timeout: float = TimeoutOpt,
    limit: int = typer.Option(20, "--limit", help="Сколько новейших отчётов показать."),
    process: Optional[str] = typer.Option(None, "--process", help="Только отчёты процесса (часть имени)."),
    details: bool = typer.Option(True, "--details/--no-details", help="Читать содержимое отчётов."),
    max_bytes: int = typer.Option(crashes_mod.DEFAULT_MAX_BYTES, "--max-bytes", help="Лимит размера читаемого файла."),
    verbose: bool = VerboseOpt,
) -> None:
    """Отчёты о сбоях на устройстве (crashreportcopymobile, только чтение)."""
    _run_report(title=crashes_mod.TITLE, header=crashes_mod.HEADER, busy="Чтение отчётов о сбоях…",
                collect=lambda d: crashes_mod.collect(d, timeout=timeout, limit=limit, details=details,
                                                      process=process, max_bytes=max_bytes),
                udid=udid, pair=pair, pair_timeout=pair_timeout, verbose=verbose,
                extras=lambda r, c: ([crashes_table(r.extra[0], c, source=r.extra[1])]
                                     if r.extra and r.extra[0] else []))


@app.command()
def apps(
    udid: Optional[str] = UdidOpt, pair: bool = PairOpt, pair_timeout: float = PairTimeoutOpt,
    timeout: float = TimeoutOpt,
    type_: str = typer.Option("user", "--type", help="user | system | any"),
    sizes: bool = typer.Option(False, "--sizes/--no-sizes", help="Запросить размеры (медленно)."),
    search: Optional[str] = typer.Option(None, "--search", help="Поиск по названию и bundle ID."),
    sort: str = typer.Option("name", "--sort", help="name | bundle | size"),
    limit: int = typer.Option(50, "--limit", help="Сколько строк показать (0 — все)."),
    bundle: Optional[str] = typer.Option(None, "--bundle", help="Подробности одного приложения по bundle ID."),
    crosscheck: bool = typer.Option(True, "--crosscheck/--no-crosscheck", help="Сверить число приложений с ideviceinstaller."),
    sizes_timeout: float = typer.Option(60.0, "--sizes-timeout", help="Тайм-аут запроса размеров, сек."),
    verbose: bool = VerboseOpt,
) -> None:
    """Установленные приложения (installation_proxy): список, поиск, размеры, детали по bundle ID."""
    _check_choice(type_, tuple(apps_mod.TYPES), "--type")
    _check_choice(sort, apps_mod.SORTS, "--sort")

    def extras(report: Report, c) -> list:
        if not report.extra:
            return []
        records, source, with_sizes = report.extra
        cut = records if limit <= 0 else records[:limit]
        out = [apps_table(cut, c, source=source, show_sizes=with_sizes)] if cut else []
        if len(cut) < len(records):
            out.append(f"Показано {len(cut)} из {len(records)} (--limit 0 покажет все).")
        return out

    _run_report(title=apps_mod.TITLE, header=apps_mod.HEADER, busy="Чтение списка приложений…",
                collect=lambda d: apps_mod.collect(d, timeout=timeout, app_type=type_, sizes=sizes, search=search,
                                                   sort=sort, bundle=bundle, sizes_timeout=sizes_timeout,
                                                   crosscheck=crosscheck),
                udid=udid, pair=pair, pair_timeout=pair_timeout, verbose=verbose, extras=extras)


@app.command()
def menu() -> None:
    """Интерактивное меню (то же, что запуск `ioskit` без аргументов в терминале)."""
    interactive.run_menu(console)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
