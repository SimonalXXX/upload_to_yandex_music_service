# Спецификация SSE: `GET /api/progress`

**Формат транспорта:** `text/event-stream`, каждое событие — строка `data: <одна строка JSON>\n\n`.  
**Keep-alive:** раз в ~25 с может приходить событие с `type: "ping"` при отсутствии других сообщений.

Все поля ниже — объект после парсинга JSON из тела `data`.

## Жизненный цикл задачи

Каждая задача (`scan`, `download`, `upload`, `sync`) начинается событием `task_start` и
**всегда** заканчивается `task_end`. Между ними — события шагов; перед `task_end` приходит ровно
одно итоговое событие: `scan_complete` (задача `scan`), `all_done` (остальные), `cancelled` или `error`.
Клиент считает приложение «занятым» от `task_start` до `task_end`; после переподключения
состояние восстанавливается по `hello`.

## Общие поля

| Поле | Тип | Описание |
|------|-----|----------|
| `type` | string | Дискриминатор типа события |

---

## Состояние и задачи

### `hello`

Первое событие каждого подключения (в т.ч. переподключения после сна/обрыва связи).

| Поле | Тип | Описание |
|------|-----|----------|
| `type` | `"hello"` |
| `active_task` | string \| null | `"scan"`, `"download"`, `"upload"`, `"sync"` или `null` |
| `trigger` | string \| null | `"manual"` или `"auto"` (автосинхронизация) |

### `task_start`

| Поле | Тип | Описание |
|------|-----|----------|
| `type` | `"task_start"` |
| `task` | string | `scan` \| `download` \| `upload` \| `sync` |
| `trigger` | string | `manual` \| `auto` |

### `task_end`

| Поле | Тип | Описание |
|------|-----|----------|
| `type` | `"task_end"` |
| `task` | string |
| `status` | string | `ok` \| `warn` \| `error` \| `cancelled` |
| `history_id` | string | id записи в `/api/history` |

### `sync_step`

Шаг синхронизации (только задача `sync`).

| Поле | Тип | Описание |
|------|-----|----------|
| `type` | `"sync_step"` |
| `step` | string | `scan` → `download` → `upload` → `verify` (шаги могут пропускаться) |

---

## Проверка лайков

### `scanning`

| Поле | Тип |
|------|-----|
| `type` | `"scanning"` |

### `scan_progress`

Лайки приходят страницами; событие — после каждых 50 и в конце.

| Поле | Тип |
|------|-----|
| `type` | `"scan_progress"` |
| `found` | number |

### `scan_complete`

| Поле | Тип | Описание |
|------|-----|----------|
| `type` | `"scan_complete"` |
| `total` | number | Всего лайков в выдаче |
| `new_count` | number | Новых относительно базы |
| `already` | number | Уже учтённых |
| `not_in_likes` | number | Переведено в статус `not_in_likes` |

Список треков клиент перечитывает через `GET /api/tracks` (поле `all_tracks` удалено в 2.0).

---

## Скачивание

### `dl_start`

| Поле | Тип |
|------|-----|
| `type` | `"dl_start"` |
| `total` | number |

### `downloading`

| Поле | Тип | Описание |
|------|-----|----------|
| `type` | `"downloading"` |
| `id` | string | sc_id трека |
| `title` | string | Обрезанное имя файла/трека |
| `stage` | string | `"download"` — идёт скачивание; `"convert"` — исходник скачан, работает ffmpeg |
| `downloaded` | number | Сколько треков уже обработано (успешно или с ошибкой) |
| `saved` | number | Успешно сохранено |
| `total` | number |

### `track_done`

Трек сохранён на диск (после конвертации и записи в базу) или файл уже был.

| Поле | Тип | Описание |
|------|-----|----------|
| `type` | `"track_done"` |
| `id` | string | sc_id трека |
| `title` | string |
| `status` | string | итоговый статус: `"downloaded"`, либо `"uploaded"`, если трек уже был в ЯМ |
| `downloaded` | number |
| `saved` | number |
| `total` | number |

### `track_status`

Статус или ошибка трека изменились без успешного скачивания: неустранимая ошибка → `unavailable`
с причиной (DRM, удалён, недоступен в стране); прочие ошибки сохраняются в `error`, статус прежний.
Сопровождается событием `log` с `level: "error"`.

| Поле | Тип |
|------|-----|
| `type` | `"track_status"` |
| `id` | string |
| `status` | string |
| `error` | string |

### `dl_complete`

| Поле | Тип | Описание |
|------|-----|----------|
| `type` | `"dl_complete"` |
| `downloaded` | number | Фактически сохранённые треки |
| `errors` | number | Ошибки логгера + сбои скачивания |
| `failures` | number | Число треков без файла |

---

## Загрузка в ЯМ

### `upload_start`

| Поле | Тип |
|------|-----|
| `type` | `"upload_start"` |
| `total` | number |
| `skipped` | number |

### `uploading`

| Поле | Тип |
|------|-----|
| `type` | `"uploading"` |
| `id` | string |
| `title` | string |
| `uploaded` | number |
| `total` | number |

### `track_uploaded`

Трек принят ЯМ и отмечен «В ЯМ» (в отличие от `uploading` — начала попытки).

| Поле | Тип |
|------|-----|
| `type` | `"track_uploaded"` |
| `id` | string |

---

## Итоги

### `all_done`

Итог задач `download`, `upload`, `sync`.

| Поле | Тип | Описание |
|------|-----|----------|
| `type` | `"all_done"` |
| `task` | string | `download` \| `upload` \| `sync` |
| `new_count` | number | *(sync)* новых лайков |
| `downloaded` | number | *(опционально)* |
| `failures` | number | *(опционально)* не скачано |
| `uploaded` | number | *(опционально)* |
| `upload_errors` | number | *(опционально)* |
| `missing` | number | *(sync)* не найдено в плейлисте после сверки |
| `duplicates` | number | *(sync)* лишних копий в плейлисте |
| `message` | string | *(опционально)* пояснение (нет плейлиста, нет авторизации и т.д.) |
| `level` | string | *(опционально)* `"error"` — загрузка в ЯМ не состоялась; `"warn"` — пропущена, частичные ошибки или сверка не выполнена. Нет поля — успех |

### `cancelled`

Пользователь нажал «Остановить». Поля — что успели сделать (любые из `new_count`,
`downloaded`, `failures`, `uploaded`).

| Поле | Тип |
|------|-----|
| `type` | `"cancelled"` |

### `error`

Задача прервана ошибкой (профиль не задан или не найден, пустой ответ SoundCloud — статусы не меняются,
нет выбранных треков, непредвиденная ошибка). Дальше — только `task_end`.

| Поле | Тип |
|------|-----|
| `type` | `"error"` |
| `message` | string |

---

## Прочее

### `log`

Сообщение для журнала задачи.

| Поле | Тип |
|------|-----|
| `type` | `"log"` |
| `id` | string *(опционально)* — sc_id трека |
| `level` | string — `"info"`, `"warn"` или `"error"` |
| `message` | string |

### `ping`

| Поле | Тип |
|------|-----|
| `type` | `"ping"` |

---

*Согласовывать с `_broadcast` в `web_app.py` и `handleEvent` в `static/js/tasks.js`.
События задачи (кроме прогресса: `downloading`, `uploading`, `scan_progress`, `ping`, `hello`)
сохраняются в историю — `GET /api/history/{id}`.*
