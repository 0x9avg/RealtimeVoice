"""Full catalogue of `.env` settings the wizard's "Расширенные настройки"
page can edit — everything in app/config.py's `Settings` that is actually
backed by an environment variable (i.e. every field whose default calls
`os.environ.get(...)`), grouped and labelled in Russian.

Deliberately excludes fields that AREN'T backed by an env var at all (e.g.
`ws_host`, `send_sample_rate_hz`, `language_code`, `wall_agent_timeout_s`) —
showing an editable box for one of those would be a worse trap than not
having it: the customer changes it, nothing happens, and there is no
indication why. Every key listed here corresponds to a real `os.environ.get`
call in app/config.py; keep this file in sync with that one by hand (the
installer deliberately does not import `app.config.Settings` itself — see
app/installer/server.py's module docstring for why).

This is plain data (JSON-serializable), not Pydantic — the installer must
tolerate a `.env` that doesn't parse as valid `Settings` at all yet, so this
schema (and its defaults) is independent of that model.
"""

from __future__ import annotations

from typing import Literal, TypedDict


class SettingField(TypedDict, total=False):
    key: str  # the .env / os.environ key
    label: str
    help: str
    type: Literal["text", "password", "number", "toggle", "select"]
    default: str
    options: list[dict[str, str]]  # for type == "select": [{"value": ..., "label": ...}]
    step: str  # for type == "number"
    profile: Literal["r1", "g1"] | None  # None == shown for both profiles


class SettingGroup(TypedDict):
    id: str
    title: str
    help: str
    fields: list[SettingField]


GROUPS: list[SettingGroup] = [
    {
        "id": "gemini",
        "title": "Модель и голос Gemini",
        "help": "Какую модель Gemini Live использует робот и как он звучит.",
        "fields": [
            {
                "key": "GEMINI_LIVE_MODEL",
                "label": "Модель Gemini Live",
                "help": (
                    "Обычно не нужно менять. Доступность моделей зависит от ключа/аккаунта — "
                    "меняйте, только если при запуске в журнале ошибка о недоступной модели."
                ),
                "type": "text",
                "default": "models/gemini-3.1-flash-live-preview",
                "profile": None,
            },
            {
                "key": "GEMINI_LIVE_VOICE",
                "label": "Голос",
                "help": (
                    "Имя голоса из списка Google (например Puck, Charon, Fenrir) — "
                    "полный список см. на ai.google.dev/gemini-api/docs/live. Пусто — "
                    "выбирается автоматически по роли робота (Puck для R1, Charon для G1)."
                ),
                "type": "text",
                "default": "",
                "profile": None,
            },
            {
                "key": "MAX_SESSION_DURATION_S",
                "label": "Плановое обновление сессии (сек)",
                "help": (
                    "Через сколько секунд робот сам обновляет сессию с Gemini (незаметно для "
                    "разговора, между репликами). Уменьшать безопасно и иногда нужно — Google сама "
                    "закрывает сессию раньше на некоторых установках (наблюдалось на G1: около "
                    "150 секунд), и тогда обрыв слышен как заминка/долгий ответ. Если это всё ещё "
                    "происходит — уменьшите дальше."
                ),
                "type": "number",
                "step": "10",
                "default": "90",
                "profile": None,
            },
            {
                "key": "TOOL_TIMEOUT_S",
                "label": "Таймаут вызова инструмента (сек)",
                "help": (
                    "Сколько робот ждёт ответа от одного действия (поиск в базе знаний, команда "
                    "видеостене) прежде чем сказать, что не получилось, вместо того чтобы "
                    "молчать дольше."
                ),
                "type": "number",
                "step": "1",
                "default": "8",
                "profile": None,
            },
            {
                "key": "CONTEXT_WINDOW_COMPRESSION_ENABLED",
                "label": "Сжатие контекста диалога включено",
                "help": (
                    "Собственный механизм Gemini для длинных разговоров: вместо принудительного "
                    "разрыва сессии старые реплики сжимаются автоматически. Дополняет плановое "
                    "обновление сессии выше, не заменяет его — оставьте включённым."
                ),
                "type": "toggle",
                "default": "true",
                "profile": None,
            },
            {
                "key": "CONTEXT_WINDOW_COMPRESSION_TRIGGER_TOKENS",
                "label": "Порог сжатия (токенов)",
                "help": (
                    "Пусто (рекомендуется) — Gemini сама выбирает разумный порог. Указывайте "
                    "число, только если в логе видно, что сжатие срабатывает слишком поздно "
                    "или слишком часто для вашей реальной нагрузки."
                ),
                "type": "number",
                "step": "1000",
                "default": "",
                "profile": None,
            },
        ],
    },
    {
        "id": "wake-word-advanced",
        "title": "Слово пробуждения — тонкая настройка",
        "help": (
            "Список самих слов настраивается в мастере (шаг «Пробуждение»). Здесь — более "
            "тонкие параметры распознавания."
        ),
        "fields": [
            {
                "key": "WAKE_WORD_ENABLED",
                "label": "Пробуждение по слову включено",
                "help": (
                    "Выключить, если аппаратный микрофон должен пересылать звук в Gemini "
                    "всегда, без локальной проверки слова (не рекомендуется — тогда все "
                    "разговоры рядом с роботом идут в облако)."
                ),
                "type": "toggle",
                "default": "true",
                "profile": None,
            },
            {
                "key": "WAKE_WORD_LANGUAGE",
                "label": "Язык распознавания слова пробуждения",
                "help": "Локальная модель Whisper слушает на этом языке.",
                "type": "select",
                "options": [
                    {"value": "ru", "label": "Русский"},
                    {"value": "kk", "label": "Казахский"},
                    {"value": "en", "label": "English"},
                ],
                "default": "ru",
                "profile": None,
            },
            {
                "key": "WAKE_WORD_WHISPER_MODEL",
                "label": "Размер локальной модели распознавания",
                "help": (
                    "Больше — точнее, но медленнее и требовательнее к процессору робота. "
                    "«tiny» — рекомендуемое значение по умолчанию."
                ),
                "type": "select",
                "options": [
                    {"value": "tiny", "label": "tiny (быстрее)"},
                    {"value": "base", "label": "base"},
                    {"value": "small", "label": "small (точнее, медленнее)"},
                ],
                "default": "tiny",
                "profile": None,
            },
            {
                "key": "WAKE_WORD_WINDOW_S",
                "label": "Окно проверки слова (сек)",
                "help": (
                    "Сколько секунд аудио анализируется за один раз. Больше — надёжнее ловит "
                    "длинные фразы, но чуть медленнее реагирует."
                ),
                "type": "number",
                "step": "0.1",
                "default": "1.5",
                "profile": None,
            },
            {
                "key": "WAKE_ACTIVE_WINDOW_S",
                "label": "Время прослушивания после пробуждения (сек)",
                "help": (
                    "Как долго после произнесённого слова (или последней реплики робота) "
                    "робот продолжает пересылать звук в Gemini, прежде чем снова перейти в режим "
                    "ожидания слова. Больше — посетитель может дольше молчать/думать между "
                    "вопросами без повторного «слова-будильника»; меньше — короче окно, в "
                    "которое может случайно попасть посторонний разговор."
                ),
                "type": "number",
                "step": "1",
                "default": "45",
                "profile": None,
            },
        ],
    },
    {
        "id": "presence-advanced",
        "title": "Определение посетителя — тонкая настройка",
        "help": (
            "Включение/выключение — в мастере (шаг «Пробуждение»). Здесь — чувствительность и "
            "частота проверки камеры."
        ),
        "fields": [
            {
                "key": "PRESENCE_THRESHOLD",
                "label": "Порог срабатывания (0–1)",
                "help": "Какая доля кадра должна измениться, чтобы считать, что кто-то подошёл. Меньше — чувствительнее.",
                "type": "number",
                "step": "0.01",
                "default": "0.02",
                "profile": None,
            },
            {
                "key": "PRESENCE_REARM_S",
                "label": "Пауза между приветствиями (сек)",
                "help": "Минимальное время между одним приветствием и следующим возможным.",
                "type": "number",
                "step": "5",
                "default": "60",
                "profile": None,
            },
            {
                "key": "PRESENCE_FPS",
                "label": "Частота проверки присутствия (кадр/с)",
                "help": "Как часто анализируется кадр на предмет движения.",
                "type": "number",
                "step": "0.5",
                "default": "2",
                "profile": None,
            },
            {
                "key": "PRESENCE_CAMERA_FPS",
                "label": "Частота опроса камеры (кадр/с)",
                "help": "Как часто вообще запрашивается кадр с камеры робота (влияет и на просмотр видео в браузере).",
                "type": "number",
                "step": "1",
                "default": "8",
                "profile": None,
            },
            {
                "key": "PRESENCE_CAMERA_IFACE",
                "label": "Сетевой интерфейс камеры",
                "help": "Обычно не нужно менять — только если камера физически подключена иначе, чем ожидается.",
                "type": "text",
                "default": "eth10",
                "profile": None,
            },
            {
                "key": "PRESENCE_ISOLATION",
                "label": "Изоляция обработки видео",
                "help": (
                    "«Отдельный процесс» — рекомендуется всегда: сбой в определении "
                    "посетителя не может повлиять на голосовой диалог. «В том же процессе» — "
                    "только для разработки/тестов."
                ),
                "type": "select",
                "options": [
                    {"value": "process", "label": "Отдельный процесс (рекомендуется)"},
                    {"value": "thread", "label": "В том же процессе"},
                ],
                "default": "process",
                "profile": None,
            },
        ],
    },
    {
        "id": "hardware-audio",
        "title": "Аппаратный микрофон и динамик робота (DDS)",
        "help": (
            "Актуально только когда включён аппаратный звук робота (раздел «Аудиоустройства»). Значения по "
            "умолчанию подобраны под типовую поставку — меняйте, только если знаете, что делаете, "
            "или так указано в документации на конкретного робота."
        ),
        "fields": [
            {
                "key": "ROBOT_SPEAKER_LEAD_MS",
                "label": "Запас воспроизведения (мс)",
                "help": (
                    "Сколько звука робот отдаёт динамику «вперёд», с запасом. Это то, что "
                    "сглаживает запинки между словами: отдельные обращения к динамику иногда "
                    "занимают 60-85 мс вместо обычных 3, и играть без запаса в такие моменты "
                    "нечего. Увеличьте, если запинки всё ещё слышны на длинных ответах; платой "
                    "будет чуть более длинный «хвост» речи после команды «Замолчи» или после "
                    "того, как робота перебили."
                ),
                "type": "number",
                "step": "50",
                "default": "200",
                "profile": None,
            },
            {
                "key": "ROBOT_MIC_GROUP",
                "label": "Multicast-группа микрофона",
                "help": "Сетевой адрес, на который робот транслирует звук со встроенного микрофона.",
                "type": "text",
                "default": "239.168.123.161",
                "profile": None,
            },
            {
                "key": "ROBOT_MIC_PORT",
                "label": "Порт микрофона",
                "help": "",
                "type": "number",
                "step": "1",
                "default": "5555",
                "profile": None,
            },
            {
                "key": "ROBOT_MIC_INTERFACE_IP",
                "label": "IP управляющего интерфейса DDS",
                "help": "",
                "type": "text",
                "default": "192.168.123.164",
                "profile": None,
            },
            {
                "key": "ROBOT_MIC_CHANNELS",
                "label": "Число каналов микрофона",
                "help": "1 — обычный моно-поток (типично для этого проекта); 6 — только если подтверждено для конкретного робота.",
                "type": "number",
                "step": "1",
                "default": "1",
                "profile": None,
            },
            {
                "key": "ROBOT_SPEAKER_IFACE",
                "label": "Сетевой интерфейс динамика",
                "help": "",
                "type": "text",
                "default": "eth10",
                "profile": None,
            },
            {
                "key": "ROBOT_SPEAKER_VOLUME",
                "label": "Громкость встроенного динамика (%)",
                "help": "",
                "type": "number",
                "step": "5",
                "default": "100",
                "profile": None,
            },
            {
                "key": "ROBOT_POST_SPEECH_GUARD_MS",
                "label": "Пауза после ответа робота (мс)",
                "help": (
                    "Микрофон не слушает это время после того, как робот закончил говорить — "
                    "нужно, чтобы робот не реагировал на собственный голос (нет подавления эха "
                    "на этом железе). Слишком маленькое значение — робот путает свой голос с "
                    "речью посетителя; слишком большое — не слышит начало быстрого ответа."
                ),
                "type": "number",
                "step": "50",
                "default": "300",
                "profile": None,
            },
        ],
    },
    {
        "id": "usb-audio-advanced",
        "title": "USB-звук — тонкая настройка",
        "help": "Выбор устройства — в мастере (шаг «Звук»). Здесь — громкость и частота дискретизации.",
        "fields": [
            {
                "key": "USB_SPEAKER_VOLUME",
                "label": "Громкость USB-динамика (%)",
                "help": "",
                "type": "number",
                "step": "5",
                "default": "100",
                "profile": None,
            },
            {
                "key": "USB_SPEAKER_SAMPLE_RATE",
                "label": "Частота дискретизации USB-динамика (Гц)",
                "help": (
                    "Не все USB-колонки честно воспроизводят на заявленной частоте — если голос "
                    "звучит слишком быстро и высоко (или слишком медленно и низко), попробуйте "
                    "другое значение, например 44100 или 48000. Проверено на Jabra Speak2 40 MS: "
                    "24000 играет на ~30% быстрее, чем нужно; 32000 — точно."
                ),
                "type": "number",
                "step": "1000",
                "default": "32000",
                "profile": None,
            },
        ],
    },
    {
        "id": "knowledge",
        "title": "База знаний / поиск по документам",
        "help": (
            "R1 отвечает на вопросы о проекте из папки knowledge/, G1 — дополнительно из "
            "справочника поста охраны. Обе используют один и тот же поиск."
        ),
        "fields": [
            {
                "key": "GEMINI_EMBEDDING_MODEL",
                "label": "Модель эмбеддингов",
                "help": "Обычно не нужно менять.",
                "type": "text",
                "default": "models/gemini-embedding-001",
                "profile": None,
            },
            {
                "key": "KNOWLEDGE_TOP_K",
                "label": "Сколько фрагментов подмешивать в ответ",
                "help": "Больше — полнее контекст, но длиннее и дороже каждый вопрос.",
                "type": "number",
                "step": "1",
                "default": "3",
                "profile": None,
            },
            {
                "key": "KNOWLEDGE_DIR",
                "label": "Папка с материалами базы знаний",
                "help": "По умолчанию — knowledge/ (R1) или knowledge_guard/ (G1) внутри проекта.",
                "type": "text",
                "default": "",
                "profile": None,
            },
        ],
    },
    {
        "id": "r1-wall",
        "title": "Видеостена",
        "help": "Адрес — в мастере (шаг «Дополнительно»). Здесь — токен, если агент видеостены его требует.",
        "fields": [
            {
                "key": "WALL_AGENT_TOKEN",
                "label": "Токен видеостены",
                "help": "Оставьте пустым, если агент видеостены не требует токен (обычный случай).",
                "type": "password",
                "default": "",
                "profile": "r1",
            },
        ],
    },
    {
        "id": "g1-visits",
        "title": "Посетители и справочник (G1)",
        "help": "",
        "fields": [
            {
                "key": "GUARD_KB_PATH",
                "label": "Путь к файлу справочника",
                "help": "Файл с кабинетами и частыми вопросами — редактируется отдельно, здесь только путь.",
                "type": "text",
                "default": "config/guard-kb.yaml",
                "profile": "g1",
            },
            {
                "key": "VISIT_JOURNAL_PATH",
                "label": "Путь к журналу визитов",
                "help": "Куда сохраняются оформленные карточки посетителей.",
                "type": "text",
                "default": "data/visits.jsonl",
                "profile": "g1",
            },
        ],
    },
]


def visible_groups(profile: str | None) -> list[SettingGroup]:
    """Groups/fields relevant to `profile` — drops the other profile's
    profile-specific fields (e.g. hide the wall-agent token on a g1
    install) rather than showing an input that does nothing there."""
    result: list[SettingGroup] = []
    for group in GROUPS:
        fields = [f for f in group["fields"] if f.get("profile") in (None, profile)]
        if fields:
            result.append({**group, "fields": fields})
    return result


def all_keys() -> list[str]:
    return [f["key"] for group in GROUPS for f in group["fields"]]


def defaults() -> dict[str, str]:
    return {f["key"]: f.get("default", "") for group in GROUPS for f in group["fields"]}


def field_meta(key: str) -> SettingField | None:
    for group in GROUPS:
        for f in group["fields"]:
            if f["key"] == key:
                return f
    return None
