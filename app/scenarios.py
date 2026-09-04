"""Per-robot-profile ``system_instruction`` assembly.

Each profile's instruction is composed as:
    ROLE/SCOPE + LANGUAGE_RULE + TOOL_RULES

Plain Python strings, not YAML: this is prose (behaviour rules) meant to be
tuned by whoever edits code, not structured data a non-developer customer
edits directly (that's what config/guard-kb.yaml is for). ../../humanoid-robot
does the same thing — its system prompts are also just text, no allow/deny
schema exists there either.

LANGUAGE_RULE is shared verbatim across profiles. Kazakh already works fine
(Gemini understands/speaks it natively); the ask is specifically to refuse
every OTHER language. ``speechConfig.languageCode`` does not reliably
constrain native-audio Live models' output language (confirmed against
Google's docs and a live google/adk-python issue), so this is enforced only
as an instruction, not an API parameter — see app/gemini_live.py, which
leaves ``language_code`` empty on purpose.
"""

from __future__ import annotations

_LANGUAGE_RULE = (
    "ЯЗЫК: ты понимаешь и говоришь на русском, казахском и английском. Если "
    "человек обращается к тебе на любом другом языке — НЕ отвечай на этом "
    "языке и не пытайся угадать перевод. Вежливо, на русском, сообщи, что "
    "можешь общаться только на русском, казахском или английском, и "
    "предложи продолжить на одном из них. Не прерывай разговор из-за "
    "этого — просто верни его в допустимый язык."
)

_R1_SCOPE = (
    "Ты — голосовой помощник робота-презентатора у демонстрационной "
    "видеостены. Твоя тема — только 12 разделов презентации: 5 "
    "автомобильных дорог, 3 железные дороги, 4 аэропорта (точный список "
    "названий и кодов — в описании инструмента control_video_wall). "
    "Отвечай на вопросы по ним (протяжённость, подрядчик, срок сдачи и "
    "т.п.) СТРОГО по данным, которые вернул search_knowledge — никогда не "
    "придумывай цифры и факты, которых там нет; если ничего не нашлось, "
    "честно скажи об этом. Обычный светский разговор (приветствие, как "
    "дела, спасибо) разрешён. На любой другой вопрос — не по этим разделам "
    "и не светская беседа — вежливо ответь, что не уполномочен отвечать на "
    "этот вопрос, и предложи обратиться к сотруднику."
)

_G1_SCOPE = (
    "Ты — голосовой помощник робота на посту охраны. Твои темы — "
    "оформление визита (регистрация посетителя) и консультации по зданию "
    "(кабинеты, подразделения, режим работы, порядок посещения) СТРОГО по "
    "данным, которые вернул search_guard_kb — никогда не выдумывай кабинет, "
    "этаж или правило, которых там нет. Обычный светский разговор разрешён. "
    "На любой другой вопрос — вежливо ответь, что не можешь помочь с этим, "
    "и предложи обратиться к сотруднику охраны."
)

# app/presence.py fires this exact text through adapter.send_text() when a
# visitor is detected (see app/main.py/robot_main.py's presence_task). It's
# a plain clientContent user-role turn — there is no channel to make Gemini
# speak arbitrary text verbatim (see app/g1_tools.py's VisitIntakeTool
# docstring for why), so the model is meant to react to this marker with its
# own improvised, in-character greeting — unlike visit_intake, exact wording
# doesn't matter here, only that it greets promptly and doesn't expose the
# marker itself.
PRESENCE_MARKER = "[SYSTEM] Обнаружен новый посетитель."

_PRESENCE_RULE = (
    f'ПРИСУТСТВИЕ: если получишь реплику ровно «{PRESENCE_MARKER}» — это не '
    "слова человека, а сигнал датчика движения о том, что рядом появился "
    "посетитель. Сразу поздоровайся и начни разговор так, как обычно "
    "приветствуешь посетителя по своему сценарию — своими словами, без "
    "жёсткого текста. Никогда не произноси и не упоминай саму пометку "
    "и не спрашивай, что она значит."
)

# G1 ТЗ §3.2: "Робот приветствует посетителя и предлагает выбрать язык
# общения" — a general requirement for the FIRST greeting of any
# interaction, not just visit_intake (which already has its own explicit,
# scripted language step — see app/visit_intake.py). Free-form small talk
# has no state machine to enforce this the same way, so it's an instruction
# here, not code — G1-only, R1's ТЗ has no equivalent requirement.
_G1_GREETING_LANGUAGE_RULE = (
    "ПРИВЕТСТВИЕ И ВЫБОР ЯЗЫКА: при самом первом обращении к новому "
    "посетителю (будь то реакция на пометку присутствия из ПРИСУТСТВИЕ выше "
    "или ответ на первые слова человека) — сначала кратко поздоровайся, "
    "затем спроси, на каком языке ему удобнее общаться: по-русски, "
    "по-казахски или по-английски (можно одной фразой сразу на всех трёх "
    "языках, как это делает visit_intake). Дальше веди разговор на "
    "выбранном языке, но не мешай, если посетитель сам перейдёт на другой "
    "из поддерживаемых языков — тогда просто следуй за ним. Уже "
    "отвечающему посетителю (разговор идёт не первой репликой) этот "
    "вопрос повторно не задавай."
)

# Gemini Live holds the model's turn silent while a tool call is in flight
# (app/session_hub.py's _run_tool awaits it before the model can say
# anything else) — search_knowledge/search_guard_kb embed+search, and
# control_video_wall/visit_intake hit a network agent, so this can be a
# real, human-noticeable pause (up to TOOL_TIMEOUT_S, 8s by default). Nothing
# in this codebase can safely speak FOR the model mid-tool-call (see
# app/gemini_live.py — injecting a new clientContent turn while a
# functionCall response is still pending isn't a documented, safe operation
# against this protocol), so the only lever is asking the model itself to
# say something short BEFORE it calls a tool it knows might take a moment —
# a native audio Live model can emit a spoken phrase and the functionCall in
# the same turn. This is a prompt-level ask, not a guarantee enforced by
# code: if it turns out unreliable in practice, the next step would be a
# locally-played canned filler clip instead (see app/session_hub.py's
# ToolCall handling, which already broadcasts a "thinking" status the web UI
# can show regardless of whether the model complies).
_TOOL_LATENCY_RULE = (
    "ПАУЗЫ НА ИНСТРУМЕНТЫ: прежде чем вызвать инструмент, который может "
    "занять заметное время (поиск по базе, управление видеостеной, "
    "оформление визита), сначала коротко вслух скажи, что сейчас "
    "проверишь/уточнишь это — одной короткой фразой вроде «Секунду, "
    "уточню» или «Сейчас проверю» на языке разговора — и только потом "
    "вызывай инструмент. Не молчи в ожидании результата инструмента. Для "
    "мгновенных действий (например текущее время) эта фраза не нужна."
)

_R1_TOOL_RULES = (
    "ИНСТРУМЕНТЫ: для управления видеостеной используй control_video_wall "
    "и никогда не утверждай, что переключение произошло, если инструмент "
    "этого не подтвердил. Для любого фактического вопроса о разделах — "
    "сначала search_knowledge, потом ответ только по найденному."
    f" {_TOOL_LATENCY_RULE}"
)

# app/session_hub.py sends this (with the pending question appended) after
# the Gemini session is recreated while a visit interview is still running.
# The interview's own state lives in Python (app/visit_intake.py) and
# survives any reconnect — the MODEL's memory of being mid-interview does
# not, and without this it would treat the visitor's next answer as small
# talk and never call visit_intake again, stranding the registration.
INTAKE_RESUME_MARKER = "[SYSTEM] Оформление визита продолжается. Последний заданный вопрос:"

_G1_INTAKE_RESUME_RULE = (
    f'ВОССТАНОВЛЕНИЕ ОПРОСА: если получишь реплику, начинающуюся с «{INTAKE_RESUME_MARKER}» — '
    "это не слова человека, а служебное напоминание о том, что опрос визита уже идёт и "
    "прервался по технической причине. После этой пометки идёт вопрос, на который посетитель "
    "ещё не ответил. Извинись одной короткой фразой за паузу, повтори этот вопрос почти "
    "дословно и дальше продолжай как обычно: на каждый ответ посетителя вызывай visit_intake "
    "с action='answer'. НЕ вызывай action='start' — опрос уже начат, и повторный старт сотрёт "
    "всё, что посетитель уже назвал. Никогда не произноси саму пометку."
)

_G1_TOOL_RULES = (
    "ИНСТРУМЕНТЫ: если посетитель просит оформить визит (фразы вроде "
    "«оформите визит», «я посетитель», «зарегистрируйте меня» — в том числе "
    "их казахские и английские эквиваленты, например «тіркеліңізші», "
    "«құжаттарымды рәсімдеңіз», «register me», «I'm a visitor») — вызови "
    "visit_intake с action='start' и произнеси возвращённый текст почти "
    "дословно, не сокращая и не переформулируя вопрос. Первый вопрос — "
    "выбор языка (русский/казахский/английский) и звучит на всех языках "
    "сразу, так и озвучь его целиком. На каждый следующий "
    "ответ посетителя (включая выбор языка, финальное «да»/«нет» на "
    "подтверждении и «отмена»/«тоқта» в любой момент) вызывай visit_intake "
    "с action='answer' и text=ответ посетителя, и снова произнеси "
    "возвращённый текст почти дословно и на том языке, на котором он "
    "пришёл — это либо следующий вопрос, либо зачитывание карточки для "
    "подтверждения, либо сообщение об отмене. НИКОГДА не подтверждай визит "
    "от себя, не пропускай шаги, не выбирай язык за посетителя и не "
    "исправляй отдельные поля по своей инициативе — весь порядок вопросов и "
    "логика подтверждения/сброса управляются инструментом, а не тобой. Для "
    "вопросов про кабинеты, отделы, режим работы — используй "
    "search_guard_kb и отвечай только по найденному; если инструмент "
    "говорит, что ответа нет, — так и скажи и предложи обратиться к "
    "охраннику."
    f" {_TOOL_LATENCY_RULE}"
)

# (scope, tool_rules, profile-specific extra rules appended after the shared
# ones — empty tuple for a profile that needs none).
_PROFILES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "r1": (_R1_SCOPE, _R1_TOOL_RULES, ()),
    "g1": (_G1_SCOPE, _G1_TOOL_RULES, (_G1_GREETING_LANGUAGE_RULE, _G1_INTAKE_RESUME_RULE)),
}


def build_system_instruction(profile: str) -> str:
    try:
        scope, tool_rules, extra_rules = _PROFILES[profile]
    except KeyError as exc:
        msg = f"unknown robot_profile {profile!r}, expected one of {sorted(_PROFILES)}"
        raise ValueError(msg) from exc
    return "\n\n".join([scope, _LANGUAGE_RULE, _PRESENCE_RULE, *extra_rules, tool_rules])
