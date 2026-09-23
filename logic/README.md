# logic/ — мозг ассистента ekt.kz (ветка `feature/logic`)

| Файл | Кому | Что это |
|---|---|---|
| `system_prompt.txt` | бэк | system-сообщение для LLM |
| `tools.json` | бэк | 6 инструментов в формате OpenAI function calling — можно передавать в `tools=` как есть |
| `purchase_conditions.txt` | бэк | ответ `get_purchase_conditions()` (демо-текст) |
| `catalog_demo.json` | бэк | синтетический каталог для `DEMO_MODE=1` |
| `demo_questions.json` | фронт | сценарий для кнопки «Пример диалога» + негативные проверки |

## Договорённости с бэком

- `session_id` **не** передаётся модели — бэк сам подставляет его в `add_to_cart` / `get_cart` из запроса `/api/chat`. Так модель не может трогать чужую корзину.
- `add_to_cart` требует `user_confirmation` — дословную фразу клиента. Бэк проверяет, что она есть в последнем сообщении `role:user`; если нет — возвращает модели ошибку и корзину не меняет.
- `add_to_cart` при `qty` > остатка возвращает ошибку с доступным количеством, корзину не меняет.
- Ответы инструментов `add_to_cart` / `get_cart` содержат `cart_link`.
- Аналог (`find_analogs`) = та же категория, в наличии, ближе по характеристикам.

## Контракт `/api/chat` (общий для всех веток)

```json
// запрос
{ "session_id": "abc", "messages": [ {"role":"user","content":"есть автомат ABB на 16А?"} ] }
// ответ
{ "reply": "...", "cart": [ {"article":"515292","name":"...","qty":2,"price":2900} ], "cart_link": "https://ekt.kz/cart" }
```
