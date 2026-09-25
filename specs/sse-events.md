# Спецификация SSE: `GET /api/progress`

**Формат транспорта:** `text/event-stream`, каждое событие — строка `data: <одна строка JSON>\n\n`.  
**Keep-alive:** раз в ~25 с может приходить событие с `type: "ping"` при отсутствии других сообщений.

Все поля ниже — объект после парсинга JSON из тела `data`.

## Общие поля

| Поле | Тип | Описание |
|------|-----|----------|
| `type` | string | Дискриминатор типа события |

---

## Типы событий

### `scanning`

Сканирование списка лайков запущено.

| Поле | Тип |
|------|-----|
| `type` | `"scanning"` |

---

### `scan_complete`

Скан завершён.

| Поле | Тип | Описание |
|------|-----|----------|
| `type` | `"scan_complete"` |
| `total` | number | Всего записей в выдаче лайков |
| `new_count` | number | Новых относительно БД |
| `already` | number | Уже учтённых |
| `not_in_likes` | number | Переведено в статус `not_in_likes` |
| `all_tracks` | array | Элементы `{ id, title, artist, status }` для списка в UI |

---

### `error`

| Поле | Тип |
|------|-----|
| `type` | `"error"` |
| `message` | string |

---

### `dl_start`

| Поле | Тип |
|------|-----|
| `type` | `"dl_start"` |
| `total` | number |

---

### `downloading`

| Поле | Тип | Описание |
|------|-----|----------|
| `type` | `"downloading"` |
| `id` | string | sc_id трека — для точечного обновления статуса в UI |
| `title` | string | Обрезанное имя файла/трека |
| `downloaded` | number | Прогресс по хукам yt-dlp |
| `saved` | number | *(опционально)* успешно сохранено в CSV |
| `total` | number |

---

### `track_done`

| Поле | Тип |
|------|-----|
| `type` | `"track_done"` |
| `id` | string | sc_id трека |
| `title` | string |
| `downloaded` | number |
| `saved` | number *(опционально)* |
| `total` | number |

---

### `dl_complete`

| Поле | Тип |
|------|-----|
| `type` | `"dl_complete"` |
| `downloaded` | number | Фактически сохранённые треки |
| `errors` | number | Суммарные ошибки логгера + сбои скачивания |
| `failures` | number | Число треков без успешного файла |

---

### `upload_start`

| Поле | Тип |
|------|-----|
| `type` | `"upload_start"` |
| `total` | number |
| `skipped` | number |

---

### `uploading`

| Поле | Тип |
|------|-----|
| `type` | `"uploading"` |
| `id` | string | sc_id трека |
| `title` | string |
| `uploaded` | number |
| `total` | number |

---

### `track_uploaded`

Точечное подтверждение: конкретный трек успешно долетел до плейлиста ЯМ
(в отличие от `uploading`, который просто сигнализирует начало попытки).

| Поле | Тип |
|------|-----|
| `type` | `"track_uploaded"` |
| `id` | string | sc_id трека |

---

### `all_done`

| Поле | Тип | Описание |
|------|-----|----------|
| `type` | `"all_done"` |
| `downloaded` | number | *(опционально)* |
| `uploaded` | number | *(опционально)* |
| `upload_errors` | number | *(опционально)* |
| `failures` | number | *(опционально)* ошибки скачивания |
| `message` | string | *(опционально)* пояснение (нет плейлиста, не авторизован и т.д.) |

---

### `cancelled`

| Поле | Тип |
|------|-----|
| `type` | `"cancelled"` |
| `downloaded` | number *(опционально)* |
| `uploaded` | number *(опционально)* |

---

### `log`

Служебное сообщение для блока лога в UI.

| Поле | Тип |
|------|-----|
| `type` | `"log"` |
| `id` | string *(опционально)* — sc_id трека, если сообщение к нему привязано |
| `level` | string — например `"error"` |
| `message` | string |

---

### `ping`

| Поле | Тип |
|------|-----|
| `type` | `"ping"` |

---

*Версия документа должна согласовываться с реализацией `_broadcast` в `web_app.py` и обработчиком `handleEvent` во встроенном JS.*
