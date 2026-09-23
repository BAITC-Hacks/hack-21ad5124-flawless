const sessionId = crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
const history = [];
let waiting = false;
let localCart = [];

const examplePool = [
  'Есть модульный автомат Schneider Electric ACTI 9 на 16 А?',
  'Подберите аналог автомата Schneider на 16 А из наличия',
  'Нужен медный силовой кабель для стационарной прокладки',
  'Покажите алюминиевый бронированный кабель АВБШВ',
  'Какие LED-панели есть в наличии?',
  'Нужен накладной светодиодный спот для офиса',
  'Подберите лампу E27 нейтрального света 4000 К',
  'Есть выключатель-разъединитель ВРТ IEK?',
  'Покажите автоматы EASY9 Schneider Electric',
  'Нужен перфорированный кабель-канал IEK',
  'Какие аксессуары есть для кабель-канала?',
  'Покажите розетки и выключатели серии VITA',
  'Нужен электрический шкаф или щит для автоматики',
  'Есть мультиметры и измерительные приборы?',
  'Подберите токоизмерительные клещи',
  'Что есть для видеонаблюдения и СКУД?',
  'Как у вас с оплатой и доставкой по Караганде?',
  'Да, добавь 2 штуки выбранного товара в корзину',
  'Какая замена есть для товара, которого нет в наличии?',
  'Дайте ссылку на мою корзину'
];

const categoryQuestions = [
  ['Кабель / Провод', 'Подберите кабель для моего проекта'],
  ['Светильники / Лампы', 'Какие светильники и лампы есть в наличии?'],
  ['Низковольтная аппаратура', 'Покажите низковольтную аппаратуру'],
  ['Кабеленесущие системы', 'Что есть из кабеленесущих систем?'],
  ['Шкафы / Щиты', 'Помогите подобрать электрический шкаф или щит'],
  ['Инструмент / КИП', 'Какие инструменты и измерительные приборы есть?']
];

const demoProducts = [
  { article: 'DEMO-AV-16', name: 'Автоматический выключатель 1P 16 А', qty: 1, price: 2150 },
  { article: 'DEMO-LED-12', name: 'Светодиодная лампа EKT 12 Вт E27 4000 К', qty: 1, price: 1290 }
];

const elements = {
  form: document.querySelector('#chat-form'), input: document.querySelector('#question'), send: document.querySelector('#send-button'),
  messages: document.querySelector('#messages'), cartCard: document.querySelector('#cart-card'), cartEmpty: document.querySelector('#cart-empty'),
  cartContent: document.querySelector('#cart-content'), cartItems: document.querySelector('#cart-items'), cartCount: document.querySelector('#cart-count'),
  cartTotal: document.querySelector('#cart-total'), cartLink: document.querySelector('#cart-link'), connection: document.querySelector('#connection-label'),
  demoPanel: document.querySelector('#demo-panel'), demoQuestions: document.querySelector('#demo-questions'),
  categories: document.querySelector('#category-questions')
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
    ? 'max-w-[88%] rounded-2xl rounded-br-md bg-ektDark px-4 py-3 text-sm leading-relaxed text-white sm:max-w-[75%] sm:text-[15px]'
    : 'max-w-[92%] rounded-2xl rounded-bl-md border border-slate-200 bg-white px-4 py-3 text-sm leading-relaxed text-slate-700 shadow-sm sm:max-w-[78%] sm:text-[15px]';
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
  const response = await fetch('/api/chat', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, messages: history })
  });
  if (!response.ok) throw new Error(`API ${response.status}`);
  elements.connection.textContent = 'На связи';
  return response.json();
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

function renderExampleQuestions() {
  const shuffled = [...examplePool].sort(() => Math.random() - 0.5).slice(0, 4);
  elements.demoQuestions.replaceChildren();
  shuffled.forEach(question => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'shrink-0 rounded border border-sky-200 bg-white px-3 py-2 text-left text-xs font-medium text-ektDark transition hover:border-ekt hover:bg-sky-50';
    button.textContent = question;
    button.addEventListener('click', () => sendMessage(question));
    elements.demoQuestions.append(button);
  });
}

categoryQuestions.forEach(([label, question]) => {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'px-4 py-3 text-sm font-medium text-slate-600 transition hover:bg-ektLight hover:text-ektDark';
  button.textContent = label;
  button.addEventListener('click', () => sendMessage(question));
  elements.categories.append(button);
});

function openFreshExamples() {
  renderExampleQuestions();
  elements.demoPanel.classList.remove('hidden');
  document.querySelector('#demo-button').textContent = 'Другие примеры';
  document.querySelector('#demo-button-mobile').textContent = 'Показать другие примеры';
}

document.querySelector('#demo-button').addEventListener('click', openFreshExamples);
document.querySelector('#demo-button-mobile').addEventListener('click', openFreshExamples);
document.querySelector('#close-demo').addEventListener('click', () => toggleDemo(true));

function toggleDemo(force) { elements.demoPanel.classList.toggle('hidden', force ?? !elements.demoPanel.classList.contains('hidden')); }

addMessage('assistant', 'Здравствуйте! Я помогу найти электротехнический товар, проверить наличие и собрать корзину. Что вы ищете?');
updateCart();
