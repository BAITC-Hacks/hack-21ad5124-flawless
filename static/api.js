/*
 * Единственная точка связи фронтенда с бэкендом.
 * Готовые методы уже используются интерфейсом, а null-поля ниже — задачи,
 * для которых бэкенд ещё должен отдать маршруты.
 */
(function createEktApi(global) {
  const config = {
    chatEndpoint: '/api/chat',

    // BACKEND TODO: поиск/фильтрация каталога вне LLM-чата.
    catalogSearchEndpoint: null,

    // BACKEND TODO (необязательно): загрузка фото товара или PDF.
    attachmentUploadEndpoint: null,

    // BACKEND TODO (необязательно): события для аналитики демо.
    analyticsEndpoint: null
  };

  async function requestJson(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }
    });
    if (!response.ok) throw new Error(`API ${response.status}`);
    return response.json();
  }

  function sendChat(sessionId, messages) {
    return requestJson(config.chatEndpoint, {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId, messages: messages.slice(-40) })
    });
  }

  async function searchCatalog(query) {
    if (!config.catalogSearchEndpoint) throw new Error('BACKEND_TODO: catalogSearchEndpoint');
    return requestJson(`${config.catalogSearchEndpoint}?q=${encodeURIComponent(query)}`);
  }

  async function uploadAttachment(file) {
    if (!config.attachmentUploadEndpoint) throw new Error('BACKEND_TODO: attachmentUploadEndpoint');
    const body = new FormData();
    body.append('file', file);
    const response = await fetch(config.attachmentUploadEndpoint, { method: 'POST', body });
    if (!response.ok) throw new Error(`API ${response.status}`);
    return response.json();
  }

  async function trackEvent(name, payload = {}) {
    if (!config.analyticsEndpoint) return;
    await requestJson(config.analyticsEndpoint, {
      method: 'POST', body: JSON.stringify({ name, payload })
    });
  }

  global.EktApi = { config, sendChat, searchCatalog, uploadAttachment, trackEvent };
})(window);
