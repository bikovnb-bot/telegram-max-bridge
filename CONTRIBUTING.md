# Как контрибьютить

Это небольшой личный проект-мост Telegram↔MAX, но PR и issue приветствуются.

## Разработка

```bash
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -r requirements-dev.txt
```

## Тесты

Перед PR убедитесь, что тесты проходят:

```bash
python -m pytest tests/ -v
```

CI (`.github/workflows/deploy.yml`) прогоняет тесты на каждый push/PR и не
задеплоит на сервер, если тесты падают.

## Стиль кода

- Без лишних абстракций и комментариев, объясняющих очевидное — только там,
  где не очевиден *почему*, а не *что*.
- Секреты (`.env`, токены, сессии MAX) никогда не коммитятся — см. `.gitignore`.
- Изменения в `bridge/config.py` (новые переменные окружения) — обновляйте
  заодно `.env.example` и README.
