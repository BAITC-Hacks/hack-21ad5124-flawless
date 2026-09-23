# logic/ — мозг ассистента ekt.kz (ветка `feature/logic`)

| Файл | Кому | Что это |
|---|---|---|
| `system_prompt.txt` | бэк | system-сообщение для LLM |
| `purchase_conditions.txt` | бэк | ответ `get_purchase_conditions()` (демо-текст) |
| `tools.json` | бэк | исходные описания базовых инструментов; актуальный источник правды — `TOOLS` в `agent.py` |
| `demo_questions.json` | фронт | сценарий для кнопки «Пример диалога» + негативные проверки |
| `TEAM_PLAN.md` | все | план работы команды |

Каталог (живой API и `catalog_demo.json` для `DEMO_MODE`) ведёт бэкенд — формат задаёт `normalize_product` в `catalog.py`.

## Договорённости с бэком

- Бэк читает `system_prompt.txt` и `purchase_conditions.txt` из `logic/`.
- `session_id` модели не передаётся — бэк подставляет его в `add_to_cart` / `get_cart` сам.
- Подтверждение клиента проверяет бэк (`purchase_confirmed` по последнему сообщению клиента); без него `add_to_cart` возвращает ошибку.
- `add_to_cart` при `qty` > остатка возвращает ошибку с доступным количеством, корзину не меняет.
- Аналог (`find_analogs`) = та же категория, в наличии.

## Контракт `/api/chat` (общий для всех веток)

```json
// запрос
{ "session_id": "abc", "messages": [ {"role":"user","content":"есть автомат на 25 А?"} ] }
// ответ
{ "reply": "...", "cart": [ {"article":"DEMO-AV-16","name":"...","qty":2,"price":2150} ], "cart_link": "https://ekt.kz/cart", "actions": [] }
```

`actions` — дополнительное обратноссовместимое поле с проверенными быстрыми действиями; базовые поля
`reply`, `cart`, `cart_link` сохраняются.
