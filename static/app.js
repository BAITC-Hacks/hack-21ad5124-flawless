const SESSION_STORAGE_KEY = 'ekt-chat-session-id';
const CART_TOKEN_STORAGE_KEY = 'ekt-cart-token';
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
let cartToken = null;
try {
  cartToken = window.sessionStorage.getItem(CART_TOKEN_STORAGE_KEY);
} catch (error) {
  console.info('sessionStorage недоступен для корзины:', error);
}
const history = [];
let waiting = false;
let demoQuestions = [...CANONICAL_DEMO_QUESTIONS];

const ALLOWED_ACTION_TYPES = new Set([
  'add_to_cart', 'show_analogs', 'show_details', 'show_availability',
  'show_certificates', 'compare', 'delivery', 'payment', 'cheaper_options',
  'other_brand', 'clarify', 'contact_manager', 'continue_search',
  'change_quantity', 'remove_from_cart', 'clear_cart'
]);

const ACTION_LABELS = {
  ru: {
    add_to_cart: 'Добавить', show_analogs: 'Похожие варианты', show_details: 'Характеристики',
    show_availability: 'Наличие по городам', show_certificates: 'Сертификаты', compare: 'Сравнить',
    delivery: 'Доставка', payment: 'Оплата', cheaper_options: 'Подобрать дешевле',
    other_brand: 'Другой производитель', clarify: 'Уточнить параметры', contact_manager: 'Спросить менеджера',
    continue_search: 'Продолжить поиск', change_quantity: 'Изменить количество',
    remove_from_cart: 'Удалить товар', clear_cart: 'Очистить корзину', more: 'Ещё', confirm: 'Подтвердить'
  },
  kk: {
    add_to_cart: 'Себетке қосу', show_analogs: 'Ұқсас нұсқалар', show_details: 'Сипаттамалар',
    show_availability: 'Қалалардағы қор', show_certificates: 'Сертификаттар', compare: 'Салыстыру',
    delivery: 'Жеткізу', payment: 'Төлем', cheaper_options: 'Арзанырақ нұсқа',
    other_brand: 'Басқа өндіруші', clarify: 'Параметрлерді нақтылау', contact_manager: 'Менеджерден сұрау',
    continue_search: 'Іздеуді жалғастыру', change_quantity: 'Санын өзгерту',
    remove_from_cart: 'Тауарды жою', clear_cart: 'Себетті тазалау', more: 'Тағы', confirm: 'Растау'
  }
};

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
  categories: document.querySelector('#category-questions'), mobileCartButton: document.querySelector('#mobile-cart-button'),
  mobileCartCount: document.querySelector('#mobile-cart-count'), cartCloseButton: document.querySelector('#cart-close-button'),
  cartOverlay: document.querySelector('#cart-overlay')
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

function responseLanguage(text) {
  return /[әғқңөұүһі]|\b(?:бар|қанша|калай|қалай|керек|рахмет|иә|жоқ|дана|себет|тауар)\b/i.test(text) ? 'kk' : 'ru';
}

function actionMessage(action, quantity, language) {
  if (typeof action.message === 'string' && action.message.trim()) {
    return action.message.replaceAll('{qty}', String(quantity));
  }
  const article = String(action.article || '').trim();
  if (action.type === 'add_to_cart') {
    return language === 'kk' ? `${article} тауарынан ${quantity} дана себетке қос` : `Добавь ${quantity} шт ${article} в корзину`;
  }
  return String(action.prompt || action.label || ACTION_LABELS[language][action.type] || '').trim();
}

function createQuantityControl(action, language) {
  const control = document.createElement('div');
  control.className = 'chat-action flex max-w-full items-center gap-1 overflow-hidden rounded-lg border border-ekt bg-white p-1 shadow-sm';
  const reportedMax = Number(action.max_qty);
  const max = Number.isSafeInteger(reportedMax) && reportedMax > 0 ? reportedMax : 1;
  let quantity = Math.max(1, Math.min(max, Number(action.qty) || 1));
  control.innerHTML = `
    <button type="button" class="quantity-minus grid h-8 w-8 place-items-center rounded text-lg font-bold text-ektDark hover:bg-ektLight" aria-label="Уменьшить количество">−</button>
    <span class="quantity-value min-w-8 text-center text-sm font-bold text-ektDark"></span>
    <button type="button" class="quantity-plus grid h-8 w-8 place-items-center rounded text-lg font-bold text-ektDark hover:bg-ektLight" aria-label="Увеличить количество">+</button>
    <button type="button" class="quantity-confirm h-8 rounded bg-accent px-3 text-xs font-bold text-ektDark hover:bg-yellow-400"></button>
    <button type="button" class="quantity-cancel grid h-8 w-7 place-items-center rounded text-slate-400 hover:bg-slate-100 hover:text-slate-700" aria-label="Отменить">×</button>`;
  const value = control.querySelector('.quantity-value');
  const refresh = () => { value.textContent = quantity; };
  refresh();
  control.querySelector('.quantity-confirm').textContent = ACTION_LABELS[language].confirm;
  control.querySelector('.quantity-minus').addEventListener('click', () => { quantity = Math.max(1, quantity - 1); refresh(); });
  control.querySelector('.quantity-plus').addEventListener('click', () => { quantity = Math.min(max, quantity + 1); refresh(); });
  control.querySelector('.quantity-confirm').addEventListener('click', () => sendMessage(actionMessage(action, quantity, language)));
  control.querySelector('.quantity-cancel').addEventListener('click', () => control.replaceWith(createActionButton(action, language)));
  return control;
}

function createActionButton(action, language) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'chat-action rounded-lg border border-sky-200 bg-white px-3 py-2 text-xs font-bold text-ektDark shadow-sm hover:border-ekt hover:bg-ektLight';
  button.textContent = String(action.label || ACTION_LABELS[language][action.type] || action.type);
  button.addEventListener('click', () => {
    if (action.type === 'add_to_cart' || action.type === 'change_quantity') {
      const startWidth = button.getBoundingClientRect().width;
      const control = createQuantityControl(action, language);
      button.replaceWith(control);
      const endWidth = control.scrollWidth;
      control.animate?.(
        [{ width: `${startWidth}px`, opacity: .75 }, { width: `${endWidth}px`, opacity: 1 }],
        { duration: 240, easing: 'ease-out' }
      );
      return;
    }
    const message = actionMessage(action, Number(action.qty) || 1, language);
    if (message) sendMessage(message);
  });
  return button;
}

function renderActions(messageRow, rawActions, reply) {
  if (!Array.isArray(rawActions)) return;
  const actions = rawActions.filter(action => {
    if (!action || !ALLOWED_ACTION_TYPES.has(action.type)) return false;
    return action.type !== 'add_to_cart' || Boolean(action.article);
  });
  if (!actions.length) return;
  const language = responseLanguage(reply);
  const wrapper = document.createElement('div');
  wrapper.className = 'ml-1 flex max-w-[92%] flex-wrap gap-2';
  const extra = [];
  actions.forEach((action, index) => {
    const button = createActionButton(action, language);
    if (index < 3) wrapper.append(button); else extra.push(button);
  });
  if (extra.length) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'chat-action rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs font-bold text-slate-600 hover:border-ekt hover:text-ektDark';
    more.textContent = `${ACTION_LABELS[language].more} · ${extra.length}`;
    more.addEventListener('click', () => { more.remove(); extra.forEach(button => wrapper.append(button)); });
    wrapper.append(more);
  }
  messageRow.insertAdjacentElement('afterend', wrapper);
  elements.messages.scrollTop = elements.messages.scrollHeight;
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

function pulseMobileCart() {
  if (!isMobileViewport()) return;
  elements.mobileCartButton.animate?.(
    [{ transform: 'translateY(-50%) scale(1)' }, { transform: 'translateY(-50%) scale(1.08)' }, { transform: 'translateY(-50%) scale(1)' }],
    { duration: 420, easing: 'ease-out' }
  );
}

function setCartDrawer(open) {
  if (!isMobileViewport()) return;
  elements.cartCard.classList.toggle('translate-x-full', !open);
  elements.cartCard.classList.toggle('translate-x-0', open);
  elements.cartCard.setAttribute('aria-hidden', String(!open));
  elements.mobileCartButton.setAttribute('aria-expanded', String(open));
  elements.mobileCartButton.classList.toggle('pointer-events-none', open);
  elements.mobileCartButton.classList.toggle('opacity-0', open);
  elements.cartOverlay.classList.toggle('pointer-events-none', !open);
  elements.cartOverlay.classList.toggle('opacity-0', !open);
  elements.cartCloseButton.classList.toggle('pointer-events-none', !open);
  elements.cartCloseButton.classList.toggle('opacity-0', !open);
  elements.cartCloseButton.style.transform = open ? 'translate(0, -50%)' : 'translate(100vw, -50%)';
  document.body.classList.toggle('overflow-hidden', open);
  if (open) elements.cartCloseButton.focus(); else elements.mobileCartButton.focus();
}

function updateCart(cart = [], cartLink = null) {
  const items = Array.isArray(cart) ? cart : [];
  const quantity = items.reduce((sum, item) => sum + normalizeQuantity(item.qty), 0);
  elements.cartCount.textContent = quantity;
  elements.mobileCartCount.textContent = quantity;
  if (cartLink) elements.cartLink.href = cartLink;
  elements.cartLink.classList.toggle('hidden', !cartLink);
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

  if (items.length > 0) pulseMobileCart();
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
        messages: getApiMessages(),
        cart_token: cartToken
      })
    });
    if (!response.ok) {
      if (response.status === 400 && cartToken) {
        cartToken = null;
        try { window.sessionStorage.removeItem(CART_TOKEN_STORAGE_KEY); } catch (error) {
          console.info('Не удалось удалить устаревшее состояние корзины:', error);
        }
        const error = new Error('Состояние корзины недействительно');
        error.kind = 'cart-expired';
        throw error;
      }
      const error = new Error(`API ${response.status}`);
      error.kind = 'http';
      error.status = response.status;
      throw error;
    }
    const data = await response.json();
    if (!data || typeof data.reply !== 'string' || !Array.isArray(data.cart) || (data.actions != null && !Array.isArray(data.actions))) {
      const error = new Error('Некорректный ответ API');
      error.kind = 'invalid-response';
      throw error;
    }
    if (typeof data.cart_token === 'string') {
      cartToken = data.cart_token;
      try {
        window.sessionStorage.setItem(CART_TOKEN_STORAGE_KEY, cartToken);
      } catch (error) {
        console.info('Не удалось сохранить состояние корзины в sessionStorage:', error);
      }
    }
    elements.connection.textContent = data.assistant_source === 'openai' ? 'ИИ на связи'
      : data.assistant_source === 'demo' ? 'Демо-режим' : 'На связи';
    return data;
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function getRequestErrorMessage(error) {
  if (error?.kind === 'cart-expired') {
    return 'Корзина из предыдущей версии каталога больше недействительна. Начните новый диалог и добавьте товары снова.';
  }
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
    const assistantRow = addMessage('assistant', data.reply);
    renderActions(assistantRow, data.actions, data.reply);
    updateCart(data.cart, data.cart_link);
  } catch (error) {
    removeTyping();
    if (error?.kind === 'cart-expired') {
      history.length = 0;
      updateCart([], null);
    }
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

elements.mobileCartButton.addEventListener('click', () => setCartDrawer(true));
elements.cartCloseButton.addEventListener('click', () => setCartDrawer(false));
elements.cartOverlay.addEventListener('click', () => setCartDrawer(false));
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && elements.mobileCartButton.getAttribute('aria-expanded') === 'true') setCartDrawer(false);
});
window.addEventListener('resize', () => {
  if (!isMobileViewport()) {
    document.body.classList.remove('overflow-hidden');
    elements.cartCard.setAttribute('aria-hidden', 'false');
  } else if (elements.mobileCartButton.getAttribute('aria-expanded') !== 'true') {
    elements.cartCard.setAttribute('aria-hidden', 'true');
  }
});

elements.cartCard.setAttribute('aria-hidden', String(isMobileViewport()));
addMessage('assistant', 'Здравствуйте! Я помогу найти электротехнический товар, проверить наличие и собрать корзину. Что вы ищете?');
updateCart();
loadDemoQuestions();
