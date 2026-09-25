#!/usr/bin/env python3
"""SoundCloud Likes Downloader — скачивает лайки с SoundCloud в локальную библиотеку."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import schedule
import yaml
import yt_dlp
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

console = Console()

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"
DEFAULT_ARCHIVE_PATH = Path(__file__).parent / "archive.txt"


def load_config(path: Path) -> dict:
    if not path.exists():
        console.print(f"[red]Конфиг не найден:[/red] {path}")
        console.print("Скопируйте config.yaml.example -> config.yaml и заполните profile_url.")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def count_archived(archive_path: Path) -> int:
    if not archive_path.exists():
        return 0
    with open(archive_path, encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


class DownloadLogger:
    """Bridge between yt-dlp's logging and rich console."""

    _IGNORED_PATTERNS = ("Deprecated Feature:", "Support for Python version")

    def __init__(self):
        self.downloaded = []
        self.errors = []

    def _is_noise(self, msg: str) -> bool:
        return any(p in msg for p in self._IGNORED_PATTERNS)

    def debug(self, msg):
        if msg.startswith('[download]') and 'Downloading item' in msg:
            console.print(f"  [dim]{msg}[/dim]")

    def info(self, msg):
        pass

    def warning(self, msg):
        if not self._is_noise(msg):
            console.print(f"  [yellow]Предупреждение:[/yellow] {msg}")

    def error(self, msg):
        if self._is_noise(msg):
            return
        console.print(f"  [red]Ошибка:[/red] {msg}")
        self.errors.append(msg)


class ProgressHook:
    """Track download progress with a rich progress bar."""

    def __init__(self):
        self.current_title = ""
        self.downloaded_titles = []
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
        )
        self.task_id = None
        self._started = False

    def start(self):
        if not self._started:
            self.progress.start()
            self._started = True

    def stop(self):
        if self._started:
            self.progress.stop()
            self._started = False

    def __call__(self, d):
        status = d.get("status")
        if status == "downloading":
            filename = d.get("filename", "")
            title = Path(filename).stem if filename else "..."
            if title != self.current_title:
                self.current_title = title
                display = title[:50] + "..." if len(title) > 50 else title
                if self.task_id is not None:
                    self.progress.update(self.task_id, description=display)
        elif status == "finished":
            title = self.current_title or Path(d.get("filename", "")).stem
            self.downloaded_titles.append(title)
            if self.task_id is not None:
                self.progress.advance(self.task_id)


def build_yt_dlp_opts(config: dict, archive_path: Path, force: bool, logger, hook) -> dict:
    """Construct the yt-dlp options dict from config."""
    sc = config.get("soundcloud", {})
    out = config.get("output", {})

    output_dir = Path(out.get("directory", "./Music")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    audio_fmt = out.get("format", "mp3")
    quality = out.get("quality", "320")
    template = str(output_dir / "%(uploader)s - %(title)s.%(ext)s")

    opts = {
        "format": "bestaudio/best",
        "extract_flat": False,
        "outtmpl": template,
        "restrictfilenames": False,
        "windowsfilenames": True,
        "ignoreerrors": True,
        "no_warnings": False,
        "quiet": True,
        "no_color": True,
        "logger": logger,
        "progress_hooks": [hook],
        "sleep_requests": sc.get("sleep_requests", 1.5),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_fmt,
                "preferredquality": quality,
            },
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
        "writethumbnail": True,
        "embedthumbnail": True,
        "parse_metadata": [
            "%(uploader)s:%(meta_artist)s",
            "%(title)s:%(meta_title)s",
        ],
    }

    if not force:
        opts["download_archive"] = str(archive_path)

    cookies_browser = sc.get("cookies_browser")
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)

    max_tracks = sc.get("max_tracks", 0)
    if max_tracks and max_tracks > 0:
        opts["playlistend"] = max_tracks

    return opts


def extract_info_count(url: str, opts: dict) -> Optional[int]:
    """Pre-extract playlist info to get total count without downloading."""
    flat_opts = {
        **opts,
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "progress_hooks": [],
        "postprocessors": [],
        "logger": DownloadLogger(),
    }
    flat_opts.pop("download_archive", None)
    flat_opts.pop("writethumbnail", None)
    flat_opts.pop("embedthumbnail", None)

    try:
        with yt_dlp.YoutubeDL(flat_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info and "entries" in info:
                entries = list(info["entries"])
                return len(entries)
    except Exception:
        pass
    return None


def run_upload(config: dict, music_dir: Optional[Path] = None):
    """Upload downloaded tracks to Yandex Music playlist."""
    from ym_uploader import upload_to_yandex, collect_music_files

    ym = config.get("yandex_music", {})
    playlist_url = ym.get("playlist_url", "")

    if not playlist_url:
        console.print("[red]Укажите playlist_url в секции yandex_music в config.yaml[/red]")
        return

    out = config.get("output", {})
    folder = music_dir or Path(out.get("directory", "./Music")).resolve()
    files = collect_music_files(folder)

    if not files:
        console.print(f"[yellow]Нет аудиофайлов в {folder}[/yellow]")
        return

    upload_to_yandex(
        playlist_url=playlist_url,
        files=files,
        upload_delay=ym.get("upload_delay", 3),
    )


def run_download(config: dict, url: Optional[str] = None, force: bool = False):
    """Main download routine."""
    sc = config.get("soundcloud", {})
    target_url = url or sc.get("profile_url", "")

    if not target_url or "YOUR_USERNAME" in target_url:
        console.print("[red]Укажите URL профиля в config.yaml или через --url[/red]")
        return

    archive_path = DEFAULT_ARCHIVE_PATH
    already_downloaded = count_archived(archive_path)

    console.print()
    console.print(
        Panel(
            f"[bold]SoundCloud Likes Downloader[/bold]\n"
            f"[dim]URL:[/dim]  {target_url}\n"
            f"[dim]Уже скачано:[/dim]  {already_downloaded} треков\n"
            f"[dim]Режим:[/dim]  {'Полная перезагрузка' if force else 'Только новые'}",
            border_style="blue",
        )
    )
    console.print()

    logger = DownloadLogger()
    hook = ProgressHook()

    opts = build_yt_dlp_opts(config, archive_path, force, logger, hook)

    console.print("[bold blue]Сканирование...[/bold blue]")
    total = extract_info_count(target_url, opts)

    if total is not None:
        console.print(f"  Найдено треков: [bold]{total}[/bold]")
        if not force:
            new_estimate = max(0, total - already_downloaded)
            console.print(f"  Примерно новых: [bold green]~{new_estimate}[/bold green]")
    else:
        console.print("  [dim]Не удалось определить количество (скачиваем всё доступное)[/dim]")

    console.print()
    console.print("[bold blue]Скачивание...[/bold blue]")

    hook.start()
    hook.task_id = hook.progress.add_task("Подготовка...", total=total or 0)

    start_time = time.time()

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([target_url])
    except yt_dlp.utils.DownloadError as e:
        if "cookies database" in str(e).lower() and "cookiesfrombrowser" in opts:
            # Браузер не установлен / профиль не найден — публичным лайкам
            # cookies не нужны, повторяем без них.
            console.print("[yellow]Cookies браузера недоступны — повторяю без них[/yellow]")
            opts.pop("cookiesfrombrowser", None)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([target_url])
            except yt_dlp.utils.DownloadError as e2:
                console.print(f"\n[red]Ошибка загрузки:[/red] {e2}")
        else:
            console.print(f"\n[red]Ошибка загрузки:[/red] {e}")
    except KeyboardInterrupt:
        console.print("\n[yellow]Прервано пользователем[/yellow]")
    finally:
        hook.stop()

    elapsed = time.time() - start_time
    new_count = len(hook.downloaded_titles)
    total_now = count_archived(archive_path)

    console.print()

    table = Table(title="Результат", border_style="green")
    table.add_column("Параметр", style="bold")
    table.add_column("Значение")
    table.add_row("Скачано новых", f"[green]{new_count}[/green]")
    table.add_row("Всего в библиотеке", str(total_now))
    table.add_row("Время", f"{elapsed:.0f} сек")
    if logger.errors:
        table.add_row("Ошибки", f"[red]{len(logger.errors)}[/red]")

    console.print(table)

    if new_count > 0:
        console.print("\n[bold green]Новые треки:[/bold green]")
        for i, title in enumerate(hook.downloaded_titles[:20], 1):
            console.print(f"  {i}. {title}")
        if new_count > 20:
            console.print(f"  ... и ещё {new_count - 20}")

    console.print()


def run_scheduled(config: dict):
    """Run in scheduler mode — periodically check for new likes."""
    sched_config = config.get("schedule", {})
    interval = sched_config.get("interval_hours", 6)

    console.print(
        Panel(
            f"[bold]Режим планировщика[/bold]\n"
            f"Интервал: каждые {interval} ч.\n"
            f"Нажмите Ctrl+C для остановки",
            border_style="magenta",
        )
    )

    def job():
        console.print(f"\n[dim]--- Запуск: {datetime.now():%Y-%m-%d %H:%M:%S} ---[/dim]")
        try:
            run_download(config)
        except Exception as e:
            # Планировщик не должен умирать от единичного сбоя (сеть, диск и т.п.)
            console.print(f"[red]Ошибка запуска по расписанию:[/red] {e}")

    job()

    schedule.every(interval).hours.do(job)

    try:
        while True:
            schedule.run_pending()
            time.sleep(60)
    except KeyboardInterrupt:
        console.print("\n[yellow]Планировщик остановлен[/yellow]")


def main():
    parser = argparse.ArgumentParser(
        description="SoundCloud Likes Downloader — скачивает лайки в локальную библиотеку",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  python sc_downloader.py                              # скачать новые лайки\n"
            "  python sc_downloader.py --url https://soundcloud.com/artist/track\n"
            "  python sc_downloader.py --schedule                   # режим планировщика\n"
            "  python sc_downloader.py --force                      # перекачать всё\n"
        ),
    )
    parser.add_argument(
        "--url",
        help="URL трека, плейлиста или профиля (вместо config.yaml)",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Путь к config.yaml (по умолчанию: ./config.yaml)",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        dest="use_schedule",
        help="Запуск в режиме планировщика (по расписанию)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Игнорировать archive.txt — скачать всё заново",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Показать статистику библиотеки и выйти",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Загрузить скачанную музыку в плейлист Яндекс Музыки",
    )

    args = parser.parse_args()
    config = load_config(Path(args.config))

    if args.stats:
        show_stats(config)
        return

    if args.upload:
        run_upload(config)
        return

    schedule_on = args.use_schedule or bool(config.get("schedule", {}).get("enabled"))
    if schedule_on:
        run_scheduled(config)
    else:
        run_download(config, url=args.url, force=args.force)

        ym = config.get("yandex_music", {})
        if ym.get("auto_upload") and ym.get("playlist_url"):
            console.print("\n[bold magenta]Автозагрузка в Яндекс Музыку...[/bold magenta]")
            run_upload(config)


def show_stats(config: dict):
    """Display library statistics."""
    out = config.get("output", {})
    music_dir = Path(out.get("directory", "./Music")).resolve()
    archive_path = DEFAULT_ARCHIVE_PATH

    archived = count_archived(archive_path)

    track_count = 0
    total_size = 0

    if music_dir.exists():
        for f in music_dir.rglob("*"):
            if f.is_file() and f.suffix in (".mp3", ".m4a", ".opus", ".flac"):
                track_count += 1
                total_size += f.stat().st_size

    size_mb = total_size / (1024 * 1024)

    table = Table(title="Статистика библиотеки", border_style="blue")
    table.add_column("Параметр", style="bold")
    table.add_column("Значение")
    table.add_row("Папка", str(music_dir))
    table.add_row("Треков (файлов)", str(track_count))
    table.add_row("В архиве (ID)", str(archived))
    table.add_row("Размер", f"{size_mb:.1f} МБ")

    console.print()
    console.print(table)
    console.print()


if __name__ == "__main__":
    main()
