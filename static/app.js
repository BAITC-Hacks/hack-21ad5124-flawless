const sessionId = crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
const history = [];
let waiting = false;
let localCart = [];

const demoQuestions = [
  'Есть автомат ABB на 16А?',
  'Нет в наличии? А какой аналог посоветуете?',
  'Как у вас с оплатой и доставкой по Алматы?',
  'Да, добавь 2 штуки автомата на 16А в корзину',
  'Дай ссылку на корзину'
];

let demoProducts = [
  { article: 'DEMO-AV-16', name: 'Автоматический выключатель 1P 16 А', qty: 1, price: 2150 },
  { article: 'DEMO-LED-12', name: 'Светодиодная лампа EKT 12 Вт E27 4000 К', qty: 1, price: 1290 }
];

const elements = {
  form: document.querySelector('#chat-form'), input: document.querySelector('#question'), send: document.querySelector('#send-button'),
  messages: document.querySelector('#messages'), cartCard: document.querySelector('#cart-card'), cartEmpty: document.querySelector('#cart-empty'),
  cartContent: document.querySelector('#cart-content'), cartItems: document.querySelector('#cart-items'), cartCount: document.querySelector('#cart-count'),
  cartTotal: document.querySelector('#cart-total'), cartLink: document.querySelector('#cart-link'), connection: document.querySelector('#connection-label'),
  demoPanel: document.querySelector('#demo-panel'), demoQuestions: document.querySelector('#demo-questions'),
  catalogGrid: document.querySelector('#catalog-grid')
};

function formatMoney(value) {
  return value == null ? 'Цена по запросу' : `${new Intl.NumberFormat('ru-RU').format(value)} ₸`;
}

function addMessage(role, content, options = {}) {
  const row = document.createElement('div');
  row.className = `flex ${role === 'user' ? 'justify-end' : 'justify-start'}`;
  if (options.id) row.id = options.id;

  const bubble = document.createElement('div');
  bubble.className = role === 'user'
    ? 'max-w-[88%] rounded-2xl rounded-br-md bg-ink px-4 py-3 text-sm leading-relaxed text-white sm:max-w-[75%] sm:text-[15px]'
    : 'max-w-[92%] rounded-2xl rounded-bl-md bg-slate-100 px-4 py-3 text-sm leading-relaxed text-slate-700 sm:max-w-[78%] sm:text-[15px]';
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

function updateCart(cart = [], cartLink = 'https://ekt.kz/cart') {
  const items = Array.isArray(cart) ? cart : [];
  const quantity = items.reduce((sum, item) => sum + Number(item.qty || 0), 0);
  elements.cartCard.classList.toggle('hidden', items.length === 0);
  elements.cartCount.textContent = quantity;
  elements.cartLink.href = cartLink || 'https://ekt.kz/cart';
  elements.cartEmpty.classList.toggle('hidden', items.length > 0);
  elements.cartContent.classList.toggle('hidden', items.length === 0);
  elements.cartItems.replaceChildren();

  let total = 0;
  items.forEach(item => {
    const price = Number(item.price);
    if (Number.isFinite(price)) total += price * Number(item.qty || 0);
    const node = document.createElement('div');
    node.className = 'py-4';
    node.innerHTML = `<div class="flex justify-between gap-3"><div><p class="item-name text-sm font-semibold leading-snug"></p><p class="item-meta mt-1 text-xs text-slate-400"></p></div><p class="item-price shrink-0 text-sm font-bold"></p></div>`;
    node.querySelector('.item-name').textContent = item.name;
    node.querySelector('.item-meta').textContent = `Арт. ${item.article || '—'} · ${item.qty} шт.`;
    node.querySelector('.item-price').textContent = formatMoney(Number.isFinite(price) ? price * item.qty : null);
    elements.cartItems.append(node);
  });
  elements.cartTotal.textContent = formatMoney(total);
}

function renderCatalog(products) {
  elements.catalogGrid.replaceChildren();
  products.forEach(product => {
    const card = document.createElement('article');
    card.className = 'rounded-xl border border-slate-200 p-3 transition hover:border-orange-200 hover:bg-orange-50/40';
    const available = Number(product.stock) > 0;
    card.innerHTML = `
      <div class="flex items-start justify-between gap-3">
        <div class="min-w-0">
          <p class="product-name text-sm font-semibold leading-snug"></p>
          <p class="product-article mt-1 text-[11px] text-slate-400"></p>
        </div>
        <span class="product-status shrink-0 rounded-full px-2 py-1 text-[10px] font-semibold"></span>
      </div>
      <div class="mt-3 flex items-end justify-between gap-3">
        <div><p class="product-price font-bold"></p><p class="product-location text-[11px] text-slate-400"></p></div>
        <button type="button" class="ask-product rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-xs font-semibold text-slate-600 hover:border-brand hover:text-brand">Спросить</button>
      </div>`;
    card.querySelector('.product-name').textContent = product.name;
    card.querySelector('.product-article').textContent = `Арт. ${product.article}`;
    card.querySelector('.product-price').textContent = formatMoney(product.price);
    card.querySelector('.product-location').textContent = available ? `${product.availability} · ${product.stock} шт.` : 'Ожидается поставка';
    const status = card.querySelector('.product-status');
    status.textContent = available ? 'В наличии' : 'Нет в наличии';
    status.className += available ? ' bg-emerald-50 text-emerald-700' : ' bg-slate-100 text-slate-500';
    card.querySelector('.ask-product').addEventListener('click', () => sendMessage(`Расскажи про товар ${product.name}, артикул ${product.article}`));
    elements.catalogGrid.append(card);
  });
}

async function loadDemoCatalog() {
  try {
    const response = await fetch('/static/catalog-demo.json');
    if (!response.ok) throw new Error(`Catalog ${response.status}`);
    const products = await response.json();
    if (!Array.isArray(products) || products.length === 0) throw new Error('Catalog is empty');
    demoProducts = products.map(product => ({ ...product, qty: 1 }));
    renderCatalog(products);
  } catch (error) {
    console.info('Демо-каталог не загрузился, используется встроенный набор:', error.message);
    renderCatalog(demoProducts.map(product => ({ ...product, stock: 1, availability: 'Демо' })));
  }
}

function offlineReply(question) {
  const text = question.toLowerCase();
  if (/(добав|корзин).*(2|две|два)|да,? добав/.test(text)) {
    const circuitBreaker = demoProducts.find(product => product.article === 'DEMO-AV-16') || demoProducts[0];
    localCart = [{ ...circuitBreaker, qty: 2 }];
    return { reply: 'Готово — добавил 2 автоматических выключателя 1P 16 А. Проверьте локальную корзину справа.', cart: localCart, cart_link: 'https://ekt.kz/cart' };
  }
  if (/ссылк.*корз|перейти.*корз/.test(text)) {
    return { reply: 'Корзина готова. Нажмите «Перейти в корзину» справа, чтобы продолжить оформление на ekt.kz.', cart: localCart, cart_link: 'https://ekt.kz/cart' };
  }
  if (/достав|оплат|парт/.test(text)) {
    return { reply: 'Условия оплаты и доставки по Алматы уточняет менеджер EKT. Цены и остатки в каталоге ориентировочные, минимальную партию также нужно подтвердить перед заказом.', cart: localCart, cart_link: 'https://ekt.kz/cart' };
  }
  if (/аналог|нет в наличии/.test(text)) {
    return { reply: 'В качестве доступного аналога могу предложить автоматический выключатель 1P 16 А за 2 150 ₸. На демо-складе осталось 8 штук. Добавить в корзину?', cart: localCart, cart_link: 'https://ekt.kz/cart' };
  }
  if (/автомат|16\s*а|abb/.test(text)) {
    return { reply: 'Нашёл автоматический выключатель 1P 16 А. Цена — 2 150 ₸, в наличии 8 штук в Астане. Это демо-позиция; хотите подобрать аналог или добавить товар?', cart: localCart, cart_link: 'https://ekt.kz/cart' };
  }
  if (/ламп|свет/.test(text)) {
    return { reply: 'Есть светодиодная лампа EKT 12 Вт, E27, 4000 К — 1 290 ₸, в наличии 24 штуки в Алматы. Добавить?', cart: localCart, cart_link: 'https://ekt.kz/cart' };
  }
  return { reply: 'Сейчас я работаю в автономном демо-режиме. Спросите про автомат на 16 А, лампу E27, условия доставки или корзину.', cart: localCart, cart_link: 'https://ekt.kz/cart' };
}

async function askAssistant() {
  const data = await window.EktApi.sendChat(sessionId, history);
  elements.connection.textContent = 'На связи';
  return data;
}

async function sendMessage(content) {
  const question = content.trim();
  if (!question || waiting) return;
  waiting = true;
  elements.send.disabled = true;
  history.push({ role: 'user', content: question });
  addMessage('user', question);
  showTyping();

  let data;
  try {
    data = await askAssistant();
  } catch (error) {
    console.info('API недоступен, используется автономный демо-режим:', error.message);
    elements.connection.textContent = 'Демо-режим';
    await new Promise(resolve => setTimeout(resolve, 450));
    data = offlineReply(question);
  }

  document.querySelector('#typing')?.remove();
  history.push({ role: 'assistant', content: data.reply });
  addMessage('assistant', data.reply);
  updateCart(data.cart, data.cart_link);
  waiting = false;
  elements.send.disabled = false;
  elements.input.focus();
}

elements.form.addEventListener('submit', event => {
  event.preventDefault();
  const content = elements.input.value;
  elements.input.value = '';
  elements.input.style.height = 'auto';
  sendMessage(content);
});

elements.input.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); elements.form.requestSubmit(); }
});
elements.input.addEventListener('input', () => {
  elements.input.style.height = 'auto';
  elements.input.style.height = `${Math.min(elements.input.scrollHeight, 128)}px`;
});

demoQuestions.forEach((question, index) => {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'shrink-0 rounded-full border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-600 transition hover:border-brand hover:text-brand';
  button.textContent = `${index + 1}. ${question}`;
  button.addEventListener('click', () => sendMessage(question));
  elements.demoQuestions.append(button);
});

function toggleDemo(force) { elements.demoPanel.classList.toggle('hidden', force ?? !elements.demoPanel.classList.contains('hidden')); }
document.querySelector('#demo-button').addEventListener('click', () => toggleDemo());
document.querySelector('#demo-button-mobile').addEventListener('click', () => toggleDemo(false));
document.querySelector('#close-demo').addEventListener('click', () => toggleDemo(true));

addMessage('assistant', 'Здравствуйте! Я помогу найти электротехнический товар, проверить наличие и собрать корзину. Что вы ищете?');
updateCart();
loadDemoCatalog();
