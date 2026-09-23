const SESSION_STORAGE_KEY = 'ekt-chat-session-id';
const MAX_API_MESSAGES = 40;
const CHAT_TIMEOUT_MS = 25_000;
const DEMO_QUESTIONS_TIMEOUT_MS = 5_000;

const CANONICAL_DEMO_QUESTIONS = Object.freeze([
  'Есть автомат на 25 А?',
  'DEMO-AV-25 нет в наличии? Какой аналог посоветуете?',
  'Как у вас с оплатой и доставкой по Алматы? Есть минимальная партия?',
  'Да, добавь 2 шт DEMO-AV-16 в корзину',
  'Покажи корзину и дай ссылку'
]);

function createSessionId() {
  try {
    const uuid = globalThis.crypto?.randomUUID?.();
    if (uuid) return uuid;
  } catch (error) {
    console.info('Не удалось создать UUID через Web Crypto:', error);
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function getSessionId() {
  const generated = createSessionId();
  try {
    const stored = window.sessionStorage.getItem(SESSION_STORAGE_KEY);
    if (stored && stored.length <= 128) return stored;
    window.sessionStorage.setItem(SESSION_STORAGE_KEY, generated);
  } catch (error) {
    console.info('sessionStorage недоступен, session_id хранится только в памяти вкладки:', error);
  }
  return generated;
}

const sessionId = getSessionId();
const history = [];
let waiting = false;
let demoQuestions = [...CANONICAL_DEMO_QUESTIONS];
let mobileCartRevealed = false;

const categoryQuestions = [
  ['Кабель / Провод', 'Подберите кабель для моего проекта'],
  ['Светильники / Лампы', 'Какие светильники и лампы есть в наличии?'],
  ['Низковольтная аппаратура', 'Покажите низковольтную аппаратуру'],
  ['Кабеленесущие системы', 'Что есть из кабеленесущих систем?'],
  ['Шкафы / Щиты', 'Помогите подобрать электрический шкаф или щит'],
  ['Инструмент / КИП', 'Какие инструменты и измерительные приборы есть?']
];

const elements = {
  form: document.querySelector('#chat-form'), input: document.querySelector('#question'), send: document.querySelector('#send-button'),
  messages: document.querySelector('#messages'), cartCard: document.querySelector('#cart-card'), cartEmpty: document.querySelector('#cart-empty'),
  cartContent: document.querySelector('#cart-content'), cartItems: document.querySelector('#cart-items'), cartCount: document.querySelector('#cart-count'),
  cartTotal: document.querySelector('#cart-total'), cartLink: document.querySelector('#cart-link'), connection: document.querySelector('#connection-label'),
  demoPanel: document.querySelector('#demo-panel'), demoQuestions: document.querySelector('#demo-questions'),
  categories: document.querySelector('#category-questions')
};

function normalizePrice(value) {
  if (value == null || (typeof value === 'string' && value.trim() === '')) return null;
  const price = Number(value);
  return Number.isFinite(price) ? price : null;
}

function normalizeQuantity(value) {
  const quantity = Number(value);
  return Number.isFinite(quantity) && quantity > 0 ? quantity : 0;
}

function formatMoney(value) {
  const price = normalizePrice(value);
  return price == null ? 'Цена неизвестна' : `${new Intl.NumberFormat('ru-RU').format(price)} ₸`;
}

function addMessage(role, content, options = {}) {
  const row = document.createElement('div');
  row.className = `flex ${role === 'user' ? 'justify-end' : 'justify-start'}`;
  if (options.id) row.id = options.id;
  if (options.alert) row.setAttribute('role', 'alert');

  const bubble = document.createElement('div');
  bubble.className = role === 'user'
    ? 'max-w-[88%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-ektDark px-4 py-3 text-sm leading-relaxed text-white sm:max-w-[75%] sm:text-[15px]'
    : 'max-w-[92%] whitespace-pre-wrap break-words rounded-2xl rounded-bl-md border border-slate-200 bg-white px-4 py-3 text-sm leading-relaxed text-slate-700 shadow-sm sm:max-w-[78%] sm:text-[15px]';
  bubble.textContent = content;
  row.append(bubble);
  elements.messages.append(row);
  elements.messages.scrollTop = elements.messages.scrollHeight;
  return row;
}

function showTyping() {
  const row = addMessage('assistant', '', { id: 'typing' });
  row.firstElementChild.innerHTML = '<span class="sr-only">Ассистент печатает</span><span class="typing-dot inline-block">●</span> <span class="typing-dot inline-block">●</span> <span class="typing-dot inline-block">●</span>';
}

function removeTyping() {
  document.querySelector('#typing')?.remove();
}

function isMobileViewport() {
  try {
    return window.matchMedia('(max-width: 1023px)').matches;
  } catch (error) {
    return window.innerWidth < 1024;
  }
}

function revealCartOnMobile() {
  if (mobileCartRevealed || !isMobileViewport()) return;
  mobileCartRevealed = true;
  window.setTimeout(() => {
    if (typeof elements.cartCard.scrollIntoView !== 'function') return;
    let behavior = 'smooth';
    try {
      if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) behavior = 'auto';
    } catch (error) {
      behavior = 'auto';
    }
    elements.cartCard.scrollIntoView({ behavior, block: 'start' });
  }, 0);
}

function updateCart(cart = [], cartLink = 'https://ekt.kz/cart') {
  const items = Array.isArray(cart) ? cart : [];
  const quantity = items.reduce((sum, item) => sum + normalizeQuantity(item.qty), 0);
  elements.cartCard.classList.toggle('hidden', items.length === 0);
  elements.cartCount.textContent = quantity;
  elements.cartLink.href = cartLink || 'https://ekt.kz/cart';
  elements.cartEmpty.classList.toggle('hidden', items.length > 0);
  elements.cartContent.classList.toggle('hidden', items.length === 0);
  elements.cartItems.replaceChildren();

  let knownTotal = 0;
  let knownPriceCount = 0;
  let hasUnknownPrice = false;
  items.forEach(item => {
    const price = normalizePrice(item.price);
    const itemQuantity = normalizeQuantity(item.qty);
    if (price == null) {
      hasUnknownPrice = true;
    } else {
      knownTotal += price * itemQuantity;
      knownPriceCount += 1;
    }

    const node = document.createElement('div');
    node.className = 'py-4';
    node.innerHTML = '<div class="flex justify-between gap-3"><div><p class="item-name text-sm font-semibold leading-snug"></p><p class="item-meta mt-1 text-xs text-slate-400"></p></div><p class="item-price shrink-0 text-sm font-bold"></p></div>';
    node.querySelector('.item-name').textContent = item.name;
    node.querySelector('.item-meta').textContent = `Арт. ${item.article || '—'} · ${item.qty} шт.`;
    node.querySelector('.item-price').textContent = price == null ? 'Цена неизвестна' : formatMoney(price * itemQuantity);
    elements.cartItems.append(node);
  });

  if (hasUnknownPrice) {
    elements.cartTotal.textContent = knownPriceCount > 0
      ? `${formatMoney(knownTotal)} + неизвестная цена`
      : 'Итог неизвестен';
  } else {
    elements.cartTotal.textContent = formatMoney(knownTotal);
  }

  if (items.length > 0) revealCartOnMobile();
}

function getApiMessages() {
  const messages = history.slice(-MAX_API_MESSAGES);
  if (messages.length > 1 && messages[0].role === 'assistant') messages.shift();
  return messages;
}

async function askAssistant() {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), CHAT_TIMEOUT_MS);
  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: controller.signal,
      body: JSON.stringify({
        session_id: sessionId,
        messages: getApiMessages()
      })
    });
    if (!response.ok) {
      const error = new Error(`API ${response.status}`);
      error.kind = 'http';
      error.status = response.status;
      throw error;
    }
    const data = await response.json();
    if (!data || typeof data.reply !== 'string' || !Array.isArray(data.cart)) {
      const error = new Error('Некорректный ответ API');
      error.kind = 'invalid-response';
      throw error;
    }
    elements.connection.textContent = 'На связи';
    return data;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function getRequestErrorMessage(error) {
  if (error?.name === 'AbortError') {
    return 'Сервис не ответил вовремя. Проверьте соединение и повторите отправку.';
  }
  if (error?.kind === 'http') {
    return `Сервис временно недоступен (ошибка ${error.status}). Повторите отправку позже.`;
  }
  if (error?.kind === 'invalid-response') {
    return 'Сервис вернул некорректный ответ. Корзина не изменена; повторите отправку позже.';
  }
  return 'Не удалось связаться с сервисом. Проверьте подключение и повторите отправку.';
}

async function sendMessage(content) {
  const question = content.trim();
  if (!question || waiting) return;

  waiting = true;
  elements.send.disabled = true;
  elements.connection.textContent = 'Связываемся…';
  history.push({ role: 'user', content: question });
  addMessage('user', question);
  showTyping();

  try {
    const data = await askAssistant();
    removeTyping();
    history.push({ role: 'assistant', content: data.reply });
    addMessage('assistant', data.reply);
    updateCart(data.cart, data.cart_link);
  } catch (error) {
    removeTyping();
    // Keep a failed turn out of API context. A manual retry then sends the same
    // request hash, so a response lost after a cart mutation cannot add twice.
    const failedTurn = history[history.length - 1];
    if (failedTurn?.role === 'user' && failedTurn.content === question) history.pop();
    elements.connection.textContent = error?.name === 'AbortError'
      ? 'Нет ответа'
      : (error?.kind ? 'Ошибка сервиса' : 'Нет связи');
    console.info('Запрос к API не выполнен; локальный ответ и корзина не создаются:', error);
    addMessage('status', getRequestErrorMessage(error), { alert: true });
  } finally {
    removeTyping();
    waiting = false;
    elements.send.disabled = false;
    elements.input.focus();
  }
}

elements.form.addEventListener('submit', event => {
  event.preventDefault();
  const content = elements.input.value;
  elements.input.value = '';
  elements.input.style.height = 'auto';
  sendMessage(content);
});

elements.input.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    elements.form.requestSubmit();
  }
});

elements.input.addEventListener('input', () => {
  elements.input.style.height = 'auto';
  elements.input.style.height = `${Math.min(elements.input.scrollHeight, 128)}px`;
});

function renderExampleQuestions() {
  elements.demoQuestions.replaceChildren();
  demoQuestions.forEach(question => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'shrink-0 rounded border border-sky-200 bg-white px-3 py-2 text-left text-xs font-medium text-ektDark transition hover:border-ekt hover:bg-sky-50';
    button.textContent = question;
    button.addEventListener('click', () => sendMessage(question));
    elements.demoQuestions.append(button);
  });
}

async function loadDemoQuestions() {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), DEMO_QUESTIONS_TIMEOUT_MS);
  try {
    const response = await fetch('/api/demo-questions', { signal: controller.signal });
    if (!response.ok) throw new Error(`API ${response.status}`);
    const data = await response.json();
    if (!Array.isArray(data?.questions) || data.questions.length !== CANONICAL_DEMO_QUESTIONS.length || data.questions.some(question => typeof question !== 'string' || !question.trim())) {
      throw new Error('Некорректный формат списка демо-вопросов');
    }
    demoQuestions = [...data.questions];
    if (!elements.demoPanel.classList.contains('hidden')) renderExampleQuestions();
  } catch (error) {
    demoQuestions = [...CANONICAL_DEMO_QUESTIONS];
    console.info('Используется канонический локальный список демо-вопросов:', error);
  } finally {
    window.clearTimeout(timeoutId);
  }
}

categoryQuestions.forEach(([label, question]) => {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'px-4 py-3 text-sm font-medium text-slate-600 transition hover:bg-ektLight hover:text-ektDark';
  button.textContent = label;
  button.addEventListener('click', () => sendMessage(question));
  elements.categories.append(button);
});

function openExamples() {
  renderExampleQuestions();
  elements.demoPanel.classList.remove('hidden');
  document.querySelector('#demo-button').textContent = 'Примеры вопросов';
  document.querySelector('#demo-button-mobile').textContent = 'Показать примеры вопросов';
}

document.querySelector('#demo-button').addEventListener('click', openExamples);
document.querySelector('#demo-button-mobile').addEventListener('click', openExamples);
document.querySelector('#close-demo').addEventListener('click', () => toggleDemo(true));

function toggleDemo(force) {
  elements.demoPanel.classList.toggle('hidden', force ?? !elements.demoPanel.classList.contains('hidden'));
}

addMessage('assistant', 'Здравствуйте! Я помогу найти электротехнический товар, проверить наличие и собрать корзину. Что вы ищете?');
updateCart();
loadDemoQuestions();
