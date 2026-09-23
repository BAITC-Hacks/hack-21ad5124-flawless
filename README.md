# EKT AI Assistant

FastAPI backend and a small browser demo for searching electrical products, checking stock, finding alternatives, answering purchase questions, and keeping a local cart.

## Run locally

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:DEMO_MODE = "1"
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

Open `http://127.0.0.1:8000`. Demo catalog prices, stock, and certificates are synthetic. `GET /health` reports `catalog_source: "demo"` in this mode.

For the live catalog, set `DEMO_MODE=0`, `EKT_API_USER`, and `EKT_API_PASSWORD`. Set `OPENAI_API_KEY` to enable the OpenAI agent; without it the offline assistant runs only in demo mode. If the live catalog cannot be loaded, `/health` returns 503 and chat says that the catalog is unavailable. It never substitutes synthetic products for live products.

The backend uses `logic/system_prompt.txt` after the `feature/logic` branch is merged. Its `logic/purchase_conditions.txt` contains demo terms and is used only in demo mode; live mode uses the cautious root-level conditions text. The demo catalog stays at the repository root, and the backend's `TOOLS` definitions remain the tool contract. Catalog normalization also supports warehouse stock and `specs` fields.

## API

`POST /api/chat` accepts:

```json
{"session_id":"browser-session-id","messages":[{"role":"user","content":"Есть лампа 12 Вт?"}]}
```

It returns `reply`, `cart` (article, name, qty, price), and `cart_link`. The caller sends the conversation history in `messages`. The latest message must be from the user. The cart is held in process memory per `session_id`; it disappears on restart and is not synchronized with ekt.kz. The link opens the EKT cart page, where the customer must create the order separately.

Adding a product requires a current user instruction or acceptance of a specific assistant offer. The backend checks the target article, quantity, available stock, and repeated requests independently of the model. The model cannot select another session's cart through a tool call.

## Checks

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
```

Tests cover catalog normalization, search and alternatives, confirmation and stock limits, repeated cart requests, session isolation, the agent tool cycle, and demo/live HTTP behavior.
