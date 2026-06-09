"""
core/subscriptions.py — Подписки пользователей на уведомления бота.

Хранилище: data/subscribers.json
    {
      "<chat_id>": {
        "name":   "Иван",
        "joined": "2026-06-09T10:00:00",
        "subs":   {"daily_pulse": false, "meetings": true, ...}
      }, ...
    }

Запись атомарная (tmp + os.replace). Для сотен–тысяч пользователей JSON ок;
при росте — миграция на БД (Supabase, см. TODO в ARCHITECTURE).
"""

import os
import json
import threading
from pathlib import Path
from datetime import datetime

# Куда складывать состояние подписок. По умолчанию — data/ в репозитории.
# На проде задайте STATE_DIR на смонтированный том (Railway volume), иначе при
# редеплое эфемерная ФС обнулит подписки.
STATE_DIR = Path(os.getenv("STATE_DIR") or (Path(__file__).resolve().parent.parent / "data"))
DATA_DIR  = STATE_DIR          # совместимость с прежним именем / тестами
STORE     = STATE_DIR / "subscribers.json"
_LOCK     = threading.Lock()

# ─────────────────────────────────────────────
# РЕЕСТР ТИПОВ УВЕДОМЛЕНИЙ
# key — стабильный идентификатор, default — вкл/выкл при первой подписке
# ─────────────────────────────────────────────
NOTIFICATION_TYPES = [
    {"key": "daily_pulse",   "emoji": "📊", "title": "Ежедневный пульс",
     "desc": "Краткая сводка рынка каждое утро (09:00 МСК)", "default": False},
    {"key": "meetings",      "emoji": "🏛", "title": "Заседания ЦБ",
     "desc": "Напоминания за 3 дня, 1 день и в день заседания", "default": True},
    {"key": "entry_signal",  "emoji": "🚨", "title": "Сигнал входа",
     "desc": "Когда bid-to-cover > 1.5× на длинных ОФЗ", "default": True},
    {"key": "auctions",      "emoji": "🏦", "title": "Аукционы Минфина",
     "desc": "Итоги аукционов (среда)", "default": False},
    {"key": "inflation",     "emoji": "📈", "title": "Инфляция",
     "desc": "Свежие данные ИПЦ и инФОМ", "default": False},
    {"key": "weekly_digest", "emoji": "📰", "title": "Недельный дайджест",
     "desc": "Полный обзор по пятницам", "default": True},
]

_KEYS     = [t["key"] for t in NOTIFICATION_TYPES]
_DEFAULTS = {t["key"]: t["default"] for t in NOTIFICATION_TYPES}


def type_meta(key):
    for t in NOTIFICATION_TYPES:
        if t["key"] == key:
            return t
    return None


# ─────────────────────────────────────────────
# ХРАНИЛИЩЕ
# ─────────────────────────────────────────────

def _load() -> dict:
    if not STORE.exists():
        return {}
    try:
        with open(STORE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError) as e:
        # Файл есть, но битый — НЕ затираем его молча: бэкапим и громко логируем,
        # иначе следующий _save() уничтожит всех подписчиков.
        try:
            backup = STORE.with_suffix(".json.corrupt")
            os.replace(STORE, backup)
            print(f"[subscriptions] ПОВРЕЖДЁН {STORE}: {e} → бэкап {backup}")
        except Exception:
            pass
        return {}
    except OSError:
        return {}


def _save(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE)


def _default_subs() -> dict:
    return dict(_DEFAULTS)


def _normalize(subs: dict) -> dict:
    """Гарантирует наличие всех ключей (на случай добавления новых типов)."""
    return {k: bool(subs.get(k, _DEFAULTS[k])) for k in _KEYS}


# ─────────────────────────────────────────────
# ПУБЛИЧНОЕ API
# ─────────────────────────────────────────────

def register(chat_id, name: str = "") -> dict:
    """Регистрирует пользователя с подписками по умолчанию (если ещё нет)."""
    cid = str(chat_id)
    with _LOCK:
        data = _load()
        if cid not in data:
            data[cid] = {
                "name":   name,
                "joined": datetime.now().isoformat(timespec="seconds"),
                "subs":   _default_subs(),
            }
        else:
            data[cid]["subs"] = _normalize(data[cid].get("subs", {}))
            if name:
                data[cid]["name"] = name
        _save(data)
        return dict(data[cid]["subs"])


def get_subs(chat_id) -> dict:
    """Текущие подписки пользователя (или дефолтные, если не зарегистрирован)."""
    data = _load()
    rec = data.get(str(chat_id))
    return _normalize(rec["subs"]) if rec else _default_subs()


def toggle(chat_id, key: str) -> bool:
    """Переключает подписку, возвращает новое значение. Регистрирует при необходимости."""
    if key not in _KEYS:
        raise KeyError(key)
    cid = str(chat_id)
    with _LOCK:
        data = _load()
        if cid not in data:
            data[cid] = {"name": "", "joined": datetime.now().isoformat(timespec="seconds"),
                         "subs": _default_subs()}
        subs = _normalize(data[cid].get("subs", {}))
        subs[key] = not subs[key]
        data[cid]["subs"] = subs
        _save(data)
        return subs[key]


def set_sub(chat_id, key: str, value: bool) -> None:
    if key not in _KEYS:
        raise KeyError(key)
    cid = str(chat_id)
    with _LOCK:
        data = _load()
        if cid not in data:
            data[cid] = {"name": "", "joined": datetime.now().isoformat(timespec="seconds"),
                         "subs": _default_subs()}
        subs = _normalize(data[cid].get("subs", {}))
        subs[key] = bool(value)
        data[cid]["subs"] = subs
        _save(data)


def subscribers_for(key: str) -> list:
    """Список chat_id (int), подписанных на данный тип уведомления."""
    data = _load()
    out = []
    for cid, rec in data.items():
        subs = _normalize(rec.get("subs", {}))
        if subs.get(key):
            try:
                out.append(int(cid))
            except ValueError:
                # Невалидный ключ (повреждение/ручная правка) — пропускаем
                continue
    return out


def unsubscribe_all(chat_id) -> None:
    """Отписывает пользователя от всех уведомлений (запись сохраняется)."""
    cid = str(chat_id)
    with _LOCK:
        data = _load()
        if cid not in data:
            data[cid] = {"name": "", "joined": datetime.now().isoformat(timespec="seconds")}
        data[cid]["subs"] = {k: False for k in _KEYS}
        _save(data)


def remove(chat_id) -> None:
    """Удаляет пользователя (например, если он заблокировал бота)."""
    cid = str(chat_id)
    with _LOCK:
        data = _load()
        if cid in data:
            del data[cid]
            _save(data)


def count() -> int:
    return len(_load())
