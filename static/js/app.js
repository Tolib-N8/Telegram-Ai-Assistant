// --- Constants & Global State ---
let currentLogFilter = 'all';
let allLogs = [];
let autoScrollEnabled = true;
let currentSettingsAccount = null;
let settingsAiEnabled = true;
let currentAuthName = null;
let authCheckInterval = null;

// --- Utility Functions ---
function updateIcons() {
    if (window.lucide) {
        window.lucide.createIcons();
    }
}

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;

    let icon = 'info';
    let color = 'var(--primary)';
    if (type === 'success') { icon = 'check-circle'; color = 'var(--success)'; }
    if (type === 'error') { icon = 'alert-circle'; color = 'var(--error)'; }
    if (type === 'warning') { icon = 'alert-triangle'; color = 'var(--warning)'; }

    toast.innerHTML = `
        <i data-lucide="${icon}" style="color:${color}; width:20px; height:20px;"></i>
        <div style="font-size:0.9rem; font-weight:600;">${message}</div>
    `;

    container.appendChild(toast);
    updateIcons();

    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(20px) scale(0.9)';
        setTimeout(() => toast.remove(), 500);
    }, 4000);
}

// --- Data Fetching ---
// --- Config Editor ---
async function openConfig() {
    document.getElementById('modal-config').style.display = 'flex';
    try {
        const res = await fetch('/api/config');
        const config = await res.text();
        document.getElementById('config-editor').value = config;
    } catch (err) {
        showToast("Ошибка загрузки конфига", "error");
    }
}

function closeConfig() {
    document.getElementById('modal-config').style.display = 'none';
}

async function saveConfig() {
    const editor = document.getElementById('config-editor');
    const content = editor.value;

    try {
        const res = await fetch('/api/config', {
            method: 'POST',
            body: content
        });
        const result = await res.json();
        if (result.status === 'success') {
            showToast("Конфиг (.env) сохранен", "success");
            closeConfig();
        }
    } catch (err) {
        showToast("Ошибка сохранения", "error");
    }
}

// --- AI Playground ---
function openAiPlayground() {
    document.getElementById('drawer-ai').style.display = 'flex';
}

function closeAiPlayground() {
    document.getElementById('drawer-ai').style.display = 'none';
}

async function runAiTest() {
    const prompt = document.getElementById('ai-test-prompt').value;
    const model = document.getElementById('ai-test-model').value;
    const resultContainer = document.getElementById('ai-test-result-container');
    const resultViewer = document.getElementById('ai-test-result');

    if (!prompt) {
        showToast("Введите текст запроса", "warning");
        return;
    }

    resultContainer.style.display = 'block';
    resultViewer.innerHTML = '<div class="log-line">⏳ Обработка...</div>';

    try {
        const res = await fetch('/api/ai/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt, model })
        });
        const data = await res.json();
        if (data.status === 'success') {
            resultViewer.innerHTML = `<div class="log-line">${data.response}</div>`;
        } else {
            resultViewer.innerHTML = `<div class="log-line" style="color:var(--error);">Ошибка: ${data.message}</div>`;
        }
    } catch (err) {
        resultViewer.innerHTML = '<div class="log-line" style="color:var(--error);">Сетевая ошибка</div>';
    }
}

async function fetchStatus() {

    try {
        const response = await fetch('/api/status');
        const data = await response.json();
        renderAccounts(data.accounts);
    } catch (err) {
        console.error("Error fetching status:", err);
    }
}

// --- WebSocket Log Logic ---
let logWs = null;

function initLogWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/logs`;

    logWs = new WebSocket(wsUrl);

    logWs.onopen = () => {
        console.log("Log WebSocket connected");
        allLogs = []; // Clear local logs to avoid duplicates when reconnecting
    };

    logWs.onmessage = (event) => {
        const line = event.data;
        if (line) {
            allLogs.push(line);
            // Cap local logs for memory
            if (allLogs.length > 1000) {
                allLogs = allLogs.slice(-1000);
            }
            applyLogFiltering();
        }
    };

    logWs.onclose = () => {
        console.log("Log WebSocket disconnected, retrying in 3s...");
        setTimeout(initLogWebSocket, 3000);
    };

    logWs.onerror = (err) => {
        console.error("Log WebSocket error", err);
        logWs.close();
    };
}

async function fetchLogs() {
    // This is now a fallback or initial fetch if needed, 
    // but with the current logic we use WS from the start.
    try {
        const response = await fetch('/api/logs?limit=500');
        const data = await response.json();
        allLogs = data.logs;
        applyLogFiltering();
    } catch (err) { }
}

function exportLogs() {
    window.location.href = '/api/logs/export';
}

function toggleAutoscroll() {
    autoScrollEnabled = !autoScrollEnabled;
    const toggle = document.getElementById('autoscroll-toggle');
    const knob = document.getElementById('autoscroll-knob');
    if (autoScrollEnabled) {
        toggle.style.background = 'var(--primary)';
        knob.style.right = '3px';
        knob.style.left = 'auto';
    } else {
        toggle.style.background = 'rgba(255,255,255,0.1)';
        knob.style.right = 'auto';
        knob.style.left = '3px';
    }
}

function setLogFilter(level) {
    currentLogFilter = level;
    document.querySelectorAll('.severity-tab').forEach(tab => {
        tab.classList.toggle('active', tab.getAttribute('data-level') === level);
    });
    applyLogFiltering();
}

function filterLogs() {
    applyLogFiltering();
}

function applyLogFiltering() {
    const search = document.getElementById('log-search').value.toLowerCase();
    const viewer = document.getElementById('log-viewer');

    const oldHtml = viewer.innerHTML;

    const filtered = allLogs.filter(line => {
        const matchesSearch = line.toLowerCase().includes(search);
        let matchesLevel = true;

        if (currentLogFilter === 'error') matchesLevel = line.includes('ERROR');
        else if (currentLogFilter === 'warning') matchesLevel = line.includes('WARNING');
        else if (currentLogFilter === 'info') matchesLevel = line.includes('INFO');

        return matchesSearch && matchesLevel;
    });

    const newHtml = filtered.map(l => {
        let color = 'var(--text)';
        let border = 'transparent';
        let content = l;

        // Enhanced highlighting
        if (l.includes('ERROR')) { color = 'var(--error)'; border = 'var(--error)'; }
        else if (l.includes('WARNING')) { color = 'var(--warning)'; border = 'var(--warning)'; }
        else if (l.includes('INFO') || l.includes('System')) { color = 'var(--success)'; border = 'transparent'; }

        // Highlight account names [Name]
        content = content.replace(/\[([^\]]+)\]/g, '<span style="color:var(--primary); font-weight:700;">[$1]</span>');

        // Highlight dates/times
        content = content.replace(/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})/g, '<span style="opacity:0.4; font-size:0.75rem;">$1</span>');

        return `<div class="log-line" style="color: ${color}; border-left-color: ${border};">${content}</div>`;
    }).join('');

    if (oldHtml !== newHtml) {
        viewer.innerHTML = newHtml;
        if (autoScrollEnabled) {
            viewer.scrollTop = viewer.scrollHeight;
        }
    }
}

async function clearLogs() {
    // Replace confirm with custom check if needed, but browser confirm is okay for safety
    if (!confirm("Вы действительно хотите полностью очистить журнал системных логов?")) return;
    try {
        const res = await fetch('/api/logs/clear', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success') {
            showToast("Журнал логов очищен", "success");
            allLogs = [];
            applyLogFiltering();
        }
    } catch (err) {
        showToast("Ошибка при очистке логов", "error");
    }
}

// --- Account Management ---
let renderedAccountNames = new Set();
let lastAccountsHtml = "";

function renderAccounts(accounts) {
    const grid = document.getElementById('accounts-grid');
    if (!accounts || accounts.length === 0) {
        grid.innerHTML = `
            <div class="card card-entrance" style="grid-column: 1/-1; text-align:center; padding: 6rem 2rem;">
                <div style="font-size: 4rem; margin-bottom: 2rem; opacity:0.3; filter: drop-shadow(0 0 20px rgba(139, 92, 246, 0.4));">🛰️</div>
                <h3 style="margin:0; font-size:1.75rem; font-weight:800; letter-spacing:-0.03em;">Нет активных подключений</h3>
                <p style="color:var(--text-dim); margin-top:12px; font-weight:500;">Добавьте ваш первый Telegram аккаунт для старта</p>
            </div>`;
        renderedAccountNames.clear();
        return;
    }

    const newHtml = accounts.map(acc => {
        const isOnline = acc.status.toLowerCase() === 'online';
        const colorClass = isOnline ? 'var(--success)' : 'var(--error)';
        const isNew = !renderedAccountNames.has(acc.name);
        if (isNew) renderedAccountNames.add(acc.name);

        return `
        <div class="card ${isNew ? 'card-entrance' : ''}">
            <div style="display:flex; justify-content:space-between; align-items:start; margin-bottom: 2rem;">
                <div>
                    <h3 style="margin:0; font-size:1.5rem; font-weight:800; letter-spacing:-0.04em; color:#fff;">${acc.name}</h3>
                    <div style="display:flex; align-items:center; gap:10px; margin-top:10px; font-size:0.75rem; font-weight:700; color:var(--text-dim); text-transform:uppercase; letter-spacing:0.05em;">
                        <span class="status-indicator" style="background:${colorClass};"></span> 
                        ${acc.status}
                    </div>
                </div>
                <div style="background:rgba(139, 92, 246, 0.15); color:var(--primary); padding:8px 14px; border-radius:12px; font-size:0.7rem; font-weight:800; border:1px solid rgba(139, 92, 246, 0.2); letter-spacing:0.1em;">
                    AI MODE
                </div>
            </div>

            <div class="stats-grid" style="display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 2.5rem;">
                <div class="stat-box">
                    <span class="stat-label">Файлы</span>
                    <span class="stat-val">${acc.stats.blocked_files}</span>
                </div>
                <div class="stat-box">
                    <span class="stat-label">Скам</span>
                    <span class="stat-val" style="color:var(--error);">${acc.stats.blocked_scams}</span>
                </div>
                <div class="stat-box" style="grid-column: span 2; display:flex; justify-content:space-between; align-items:center; background:linear-gradient(135deg, rgba(34, 197, 94, 0.05), transparent);">
                    <span class="stat-label">Новые контакты</span>
                    <span class="stat-val" style="color:var(--success); font-size:1.75rem;">${acc.stats.total_unknown}</span>
                </div>
            </div>

            <div style="margin-top:auto; display:flex; gap:12px; flex-wrap:wrap;">
                <button class="btn" style="flex:1; min-width:45px;" onclick="openHistory('${acc.name}')" title="История">
                    <i data-lucide="message-square" style="width:18px;"></i>
                </button>
                <button class="btn" style="flex:1; min-width:45px;" onclick="openLists('${acc.name}')" title="Списки">
                    <i data-lucide="list" style="width:18px;"></i>
                </button>
                <button class="btn" style="flex:1; min-width:45px;" onclick="openSettings('${acc.name}')" title="Настройки">
                    <i data-lucide="settings" style="width:18px;"></i>
                </button>
                <button class="btn btn-danger" style="width:50px; flex-shrink:0;" onclick="deleteAccount('${acc.name}')" title="Удалить">
                    <i data-lucide="trash-2" style="width:18px;"></i>
                </button>
            </div>
        </div>`;
    }).join('');

    if (newHtml !== lastAccountsHtml) {
        grid.innerHTML = newHtml;
        lastAccountsHtml = newHtml;
        updateIcons();
    }
}

async function deleteAccount(name) {
    if (!confirm(`Вы действительно хотите БЕЗВОЗВРАТНО удалить аккаунт ${name}?`)) return;
    try {
        const response = await fetch(`/api/accounts/${name}`, { method: 'DELETE' });
        const data = await response.json();
        if (data.status === 'success') {
            showToast(`Аккаунт ${name} удален`, "success");
            fetchStatus();
        }
    } catch (err) { showToast("Ошибка при удалении", "error"); }
}

async function restartAccount(name, event) {
    const btn = event.currentTarget;
    const icon = btn.querySelector('i');
    icon.classList.add('animate-spin');

    try {
        await fetch(`/api/accounts/restart/${name}`, { method: 'POST' });
        showToast(`Аккаунт ${name} перезагружается`, "info");
        setTimeout(fetchStatus, 2000);
    } catch (err) {
        showToast("Ошибка перезапуска", "error");
    } finally {
        setTimeout(() => icon.classList.remove('animate-spin'), 1000);
    }
}

async function resetAccountData(name) {
    if (!confirm(`Сбросить все статистические данные для ${name}?`)) return;
    try {
        const response = await fetch(`/api/accounts/reset/${name}`, { method: 'POST' });
        const data = await response.json();
        if (data.status === 'success') {
            showToast("Данные аккаунта сброшены", "success");
            fetchStatus();
        }
    } catch (err) { showToast("Ошибка при сбросе", "error"); }
}

// --- Settings ---
function toggleSettingsAI() {
    settingsAiEnabled = !settingsAiEnabled;
    updateSettingsUI();
}

function updateSettingsUI() {
    const toggle = document.getElementById('settings-ai-toggle');
    const knob = document.getElementById('settings-ai-knob');
    if (settingsAiEnabled) {
        toggle.style.background = 'var(--primary)';
        toggle.style.boxShadow = '0 0 15px var(--primary-glow)';
        knob.style.right = '4px';
        knob.style.left = 'auto';
    } else {
        toggle.style.background = 'rgba(255,255,255,0.1)';
        toggle.style.boxShadow = 'none';
        knob.style.right = 'auto';
        knob.style.left = '4px';
    }
}

async function openSettings(name) {
    currentSettingsAccount = name;
    document.getElementById('settings-title').innerText = `${name} Настройки`;

    try {
        const response = await fetch(`/api/accounts/settings/${name}`);
        const settings = await response.json();

        document.getElementById('settings-prompt').value = settings.gemini_prompt;
        settingsAiEnabled = settings.ai_enabled;
        document.getElementById('settings-model').value = settings.gemini_model || "gemini-2.0-flash";
        updateSettingsUI();
    } catch (err) {
        showToast("Не удалось загрузить настройки", "error");
    }

    document.getElementById('settingsModal').style.display = 'flex';
    updateIcons();
}

async function saveSettings() {
    if (!currentSettingsAccount) return;

    const prompt = document.getElementById('settings-prompt').value;
    const model = document.getElementById('settings-model').value;
    const settings = {
        gemini_prompt: prompt,
        ai_enabled: settingsAiEnabled,
        gemini_model: model
    };

    try {
        const response = await fetch(`/api/accounts/settings/${currentSettingsAccount}`, {
            method: 'POST',
            body: JSON.stringify(settings)
        });
        const result = await response.json();
        if (result.status === 'success') {
            showToast("Настройки сохранены", "success");
            closeSettings();
        } else {
            showToast("Ошибка сохранения: " + result.message, "error");
        }
    } catch (err) {
        showToast("Ошибка сети", "error");
    }
}

function closeSettings() {
    document.getElementById('settingsModal').style.display = 'none';
}

// --- Authentication Flow ---
async function startAuth() {
    const nameInput = document.getElementById('account-name');
    const name = nameInput.value.trim();
    if (!name) return showToast("Введите имя аккаунта", "warning");

    currentAuthName = name;
    const statusDiv = document.getElementById('auth-status');
    statusDiv.style.color = "var(--text)";
    statusDiv.innerHTML = `<span style="opacity:0.6">⏳ Инициализация клиента для </span> ${name}...`;

    try {
        const response = await fetch('/api/accounts/auth/start', {
            method: 'POST',
            body: JSON.stringify({ name })
        });
        const data = await response.json();

        if (data.status === 'qr') {
            showQR(data.url);
            startPolling();
            showToast("QR-код сгенерирован", "info");
        } else if (data.status === 'success') {
            showToast("Аккаунт уже авторизован!", "success");
            setTimeout(closeModal, 1500);
        } else {
            statusDiv.style.color = "var(--error)";
            statusDiv.innerText = "❌ " + data.message;
            showToast(data.message, "error");
        }
    } catch (err) {
        statusDiv.innerText = "❌ Сбой сети";
        showToast("Ошибка подключения", "error");
    }
}

let currentAuthMethod = 'qr';

function switchAuthTab(method) {
    currentAuthMethod = method;
    const tabQr = document.getElementById('tab-qr');
    const tabPhone = document.getElementById('tab-phone');
    const secQr = document.getElementById('section-qr');
    const secPhone = document.getElementById('section-phone');
    const statusDiv = document.getElementById('auth-status');
    const qrContainer = document.getElementById('qr-container');

    // Reset UI state
    qrContainer.style.display = 'none';
    statusDiv.innerText = "";
    if (authCheckInterval) clearInterval(authCheckInterval);

    if (method === 'qr') {
        tabQr.classList.add('active');
        tabQr.style.background = 'var(--primary)';
        tabQr.style.color = '#fff';

        tabPhone.classList.remove('active');
        tabPhone.style.background = 'rgba(255,255,255,0.05)';
        tabPhone.style.color = 'var(--text-dim)';

        secQr.style.display = 'block';
        secPhone.style.display = 'none';
        document.getElementById('auth-actions').style.display = 'block';
    } else {
        tabPhone.classList.add('active');
        tabPhone.style.background = 'var(--primary)';
        tabPhone.style.color = '#fff';

        tabQr.classList.remove('active');
        tabQr.style.background = 'rgba(255,255,255,0.05)';
        tabQr.style.color = 'var(--text-dim)';

        secPhone.style.display = 'block';
        secQr.style.display = 'none';

        // Reset phone form
        document.getElementById('phone-code-group').style.display = 'none';
        document.getElementById('btn-phone-start').style.display = 'block';
        document.getElementById('btn-phone-verify').style.display = 'none';
    }
}

async function startPhoneAuth() {
    const name = document.getElementById('account-name').value.trim();
    const phone = document.getElementById('auth-phone').value.trim();

    if (!name) return showToast("Введите имя аккаунта", "warning");
    if (!phone) return showToast("Введите номер телефона", "warning");

    currentAuthName = name;

    const btn = document.getElementById('btn-phone-start');
    const oldText = btn.innerHTML;
    btn.innerText = "Отправка...";
    btn.disabled = true;

    try {
        const res = await fetch('/api/accounts/auth/phone/start', {
            method: 'POST',
            body: JSON.stringify({ name, phone })
        });
        const data = await res.json();

        if (data.status === 'sent') {
            showToast("Код отправлен в Telegram", "success");
            document.getElementById('phone-code-group').style.display = 'block';
            document.getElementById('btn-phone-start').style.display = 'none';
            document.getElementById('btn-phone-verify').style.display = 'block';
        } else if (data.status === 'error') {
            showToast(data.message, "error");
        } else {
            showToast("Неизвестный статус: " + data.status, "warning");
        }
    } catch (e) {
        showToast("Ошибка сети", "error");
    } finally {
        btn.innerHTML = oldText;
        btn.disabled = false;
    }
}

async function submitPhoneCode() {
    const code = document.getElementById('auth-code').value.trim();
    const phone = document.getElementById('auth-phone').value.trim();

    if (!code) return showToast("Введите код", "warning");

    const btn = document.getElementById('btn-phone-verify');
    btn.innerText = "Проверка...";
    btn.disabled = true;

    try {
        const res = await fetch('/api/accounts/auth/phone/verify', {
            method: 'POST',
            body: JSON.stringify({ name: currentAuthName, code, phone })
        });
        const data = await res.json();

        if (data.status === 'success') {
            showToast(`Успешно! Привет, ${data.user}`, "success");
            setTimeout(() => { location.reload(); }, 1500);
        } else if (data.status === '2fa_needed') {
            // Reuse existing 2FA UI but hide phone section
            document.getElementById('section-phone').style.display = 'none';
            document.getElementById('2fa-container').style.display = 'block';
            showToast("Введите пароль 2FA", "info");
        } else {
            showToast(data.message, "error");
        }
    } catch (e) {
        showToast("Ошибка сети", "error");
    } finally {
        btn.innerText = "Войти";
        btn.disabled = false;
    }
}

function showQR(url) {
    document.getElementById('auth-actions').style.display = 'none';
    document.getElementById('qr-container').style.display = 'block';
    const qrDiv = document.getElementById('qr-code');
    qrDiv.innerHTML = "";
    const qr = qrcode(0, 'M');
    qr.addData(url);
    qr.make();
    qrDiv.innerHTML = qr.createImgTag(8);
}

function startPolling() {
    if (authCheckInterval) clearInterval(authCheckInterval);
    authCheckInterval = setInterval(async () => {
        try {
            const res = await fetch(`/api/accounts/auth/check/${currentAuthName}`);
            const data = await res.json();
            const statusDiv = document.getElementById('auth-status');

            if (data.status === 'success') {
                clearInterval(authCheckInterval);
                statusDiv.style.color = "var(--success)";
                statusDiv.innerHTML = `✅ Авторизовано! Привет, <span style="color:#fff">${data.user}</span>`;
                showToast(`Успешный вход: ${data.user}`, "success");
                setTimeout(() => { location.reload(); }, 2000);
            } else if (data.status === '2fa_needed') {
                clearInterval(authCheckInterval);
                document.getElementById('qr-container').style.display = 'none';
                document.getElementById('2fa-container').style.display = 'block';
                showToast("Требуется пароль 2FA", "warning");
            } else if (data.status === 'error') {
                clearInterval(authCheckInterval);
                statusDiv.style.color = "var(--error)";
                statusDiv.innerText = "❌ " + data.message;
                showToast(data.message, "error");
            }
        } catch (err) { }
    }, 2000);
}

async function submit2FA() {
    const password = document.getElementById('tf-password').value;
    const statusDiv = document.getElementById('auth-status');
    statusDiv.innerHTML = "⏳ Проверка пароля...";

    const res = await fetch('/api/accounts/auth/2fa', {
        method: 'POST',
        body: JSON.stringify({ name: currentAuthName, password })
    });
    const data = await res.json();

    if (data.status === 'success') {
        showToast("2FA подтвержден!", "success");
        setTimeout(() => { location.reload(); }, 1500);
    } else {
        showToast("Ошибка пароля", "error");
        statusDiv.innerHTML = `<span style="color:var(--error)">❌ Неверный пароль</span>`;
    }
}

function openModal() {
    document.getElementById('addModal').style.display = 'flex';
    document.getElementById('auth-status').innerText = "";

    // Reset inputs
    document.getElementById('account-name').value = "";
    document.getElementById('auth-phone').value = "";
    document.getElementById('auth-code').value = "";

    // Default to QR tab
    switchAuthTab('qr');

    // Hide 2FA
    document.getElementById('2fa-container').style.display = 'none';

    updateIcons();
}
function closeModal() {
    document.getElementById('addModal').style.display = 'none';
    if (authCheckInterval) clearInterval(authCheckInterval);
}

// --- History & Lists & Systemd ---
async function openHistory(name) {
    currentSettingsAccount = name;
    const drawer = document.getElementById('historyDrawer');
    const items = document.getElementById('history-items');
    document.getElementById('history-title').innerText = `История: ${name}`;

    items.innerHTML = '<div style="text-align:center; padding:2rem; opacity:0.5;">Загрузка истории...</div>';
    drawer.style.display = 'flex';

    try {
        const res = await fetch(`/api/accounts/history/${name}`);
        const data = await res.json();

        if (!data.history || data.history.length === 0) {
            items.innerHTML = '<div style="text-align:center; padding:2rem; opacity:0.3;">История чатов пуста</div>';
        } else {
            items.innerHTML = data.history.map(msg => {
                const isSent = msg.type === 'sent';
                const timeStr = new Date(msg.time * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                return `
                    <div style="align-self: ${isSent ? 'flex-end' : 'flex-start'}; max-width: 85%; background: ${isSent ? 'var(--primary)' : 'rgba(255,255,255,0.08)'}; padding: 12px 16px; border-radius: 18px; position:relative; box-shadow: 0 4px 12px rgba(0,0,0,0.1); margin-bottom:4px;">
                        <div style="font-size:0.75rem; color:var(--text-dim); margin-bottom:4px; font-weight:600;">${msg.user_id}</div>
                        <div style="font-size:0.9rem; line-height:1.4; word-break: break-word;">${msg.text}</div>
                        <div style="font-size:0.65rem; color:var(--text-dim); text-align:right; margin-top:4px;">${timeStr}</div>
                    </div>
                `;
            }).join('');
            setTimeout(() => { items.scrollTop = items.scrollHeight; }, 100);
        }
    } catch (err) {
        items.innerHTML = '<div style="color:var(--error); text-align:center;">Ошибка загрузки истории</div>';
    }
    updateIcons();
}

function closeHistory() {
    document.getElementById('historyDrawer').style.display = 'none';
}

async function openLists(name) {
    currentSettingsAccount = name;
    document.getElementById('list-title').innerText = `Списки: ${name}`;
    document.getElementById('listModal').style.display = 'flex';

    try {
        const res = await fetch(`/api/accounts/lists/${name}`);
        const data = await res.json();
        document.getElementById('list-whitelist').value = (data.whitelist || []).join(', ');
        document.getElementById('list-blacklist').value = (data.blacklist || []).join(', ');
    } catch (err) {
        showToast("Ошибка загрузки списков", "error");
    }
}

function closeLists() {
    document.getElementById('listModal').style.display = 'none';
}

async function saveLists() {
    const whitelist = document.getElementById('list-whitelist').value.split(',').map(s => s.trim()).filter(s => s);
    const blacklist = document.getElementById('list-blacklist').value.split(',').map(s => s.trim()).filter(s => s);

    try {
        const res = await fetch(`/api/accounts/lists/${currentSettingsAccount}`, {
            method: 'POST',
            body: JSON.stringify({ whitelist, blacklist })
        });
        const data = await res.json();
        if (data.status === 'success') {
            showToast("Списки обновлены", "success");
            closeLists();
        } else {
            showToast(data.message, "error");
        }
    } catch (err) {
        showToast("Ошибка сети", "error");
    }
}

async function showSystemd() {
    try {
        const res = await fetch('/api/system/systemd');
        const data = await res.json();
        document.getElementById('systemd-config').innerText = data.config;
        document.getElementById('systemd-path').innerText = `Конфиг будет здесь: ${data.path}`;
        document.getElementById('systemdModal').style.display = 'flex';
        updateIcons();
    } catch (err) {
        showToast("Не удалось получить конфиг systemd", "error");
    }
}

function closeSystemd() {
    document.getElementById('systemdModal').style.display = 'none';
}

// --- Main Process Control ---
async function fetchMainStatus() {
    try {
        const response = await fetch('/api/main/status');
        const data = await response.json();

        const text = document.getElementById('server-status-text');
        const indicator = document.getElementById('server-indicator');
        const btnStart = document.getElementById('btn-main-start');
        const btnStop = document.getElementById('btn-main-stop');
        const btnRestart = document.getElementById('btn-main-restart');

        if (data.running) {
            text.innerHTML = `<span style="color:#fff">Active</span> | PID: ${data.pid} | MEM: ${data.memory}MB`;
            indicator.style.background = "var(--success)";
            btnStart.disabled = true;
            btnStop.disabled = false;
            btnRestart.disabled = false;
        } else {
            text.innerText = "Остановлен";
            indicator.style.background = "var(--error)";
            btnStart.disabled = false;
            btnStop.disabled = true;
            btnRestart.disabled = true;
        }
    } catch (err) { }
}

async function controlMain(action) {
    const btn = event.target.closest('.btn');
    const oldHtml = btn.innerHTML;
    btn.innerHTML = "⏳";
    btn.disabled = true;

    try {
        const response = await fetch(`/api/main/${action}`, { method: 'POST' });
        const data = await response.json();
        if (data.status === 'success') {
            showToast(`Команда ${action} выполнена`, "success");
        } else {
            showToast(data.message, "error");
        }
    } catch (err) { showToast("Сбой сети при управлении", "error"); }
    finally {
        setTimeout(() => {
            btn.innerHTML = oldHtml;
            btn.disabled = false;
            fetchMainStatus();
            updateIcons();
        }, 1000);
    }
}

// --- Charts ---
let cpuChart, ramChart, tokenChart;
const MAX_DATA_POINTS = 30;

function initCharts() {
    const chartOptions = (color) => ({
        type: 'line',
        data: {
            labels: Array(MAX_DATA_POINTS).fill(''),
            datasets: [{
                data: Array(MAX_DATA_POINTS).fill(0),
                borderColor: color,
                borderWidth: 3,
                pointRadius: 0,
                fill: true,
                backgroundColor: color + '11',
                tension: 0.4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                x: { display: false },
                y: {
                    min: 0, max: 100,
                    grid: { color: 'rgba(255,255,255,0.03)' },
                    ticks: { display: false }
                }
            },
            animation: { duration: 0 }
        }
    });

    const cpuCtx = document.getElementById('cpuChart').getContext('2d');
    const ramCtx = document.getElementById('ramChart').getContext('2d');

    cpuChart = new Chart(cpuCtx, chartOptions('#8B5CF6'));
    ramChart = new Chart(ramCtx, chartOptions('#3B82F6'));

    const tokenCtx = document.getElementById('tokenChart').getContext('2d');
    tokenChart = new Chart(tokenCtx, {
        type: 'bar',
        data: {
            labels: [],
            datasets: [
                {
                    label: 'Input',
                    data: [],
                    backgroundColor: '#10B981',
                    borderRadius: 4
                },
                {
                    label: 'Output',
                    data: [],
                    backgroundColor: '#F59E0B',
                    borderRadius: 4
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                x: {
                    stacked: true,
                    grid: { display: false },
                    ticks: { display: false }
                },
                y: {
                    stacked: true,
                    grid: { color: 'rgba(255,255,255,0.03)' },
                    ticks: { display: false }
                }
            },
            animation: { duration: 500 }
        }
    });
}

async function updateSystemStats() {
    try {
        const response = await fetch('/api/system/stats');
        const data = await response.json();
        updateChart(cpuChart, data.cpu);
        updateChart(ramChart, data.ram);
    } catch (err) { }
}

async function updateTokenStats() {
    try {
        const response = await fetch('/api/ai/token-stats');
        const data = await response.json();

        tokenChart.data.labels = data.dates;
        tokenChart.data.datasets[0].data = data.input;
        tokenChart.data.datasets[1].data = data.output;
        tokenChart.update();
    } catch (err) { }
}

function updateChart(chart, value) {
    chart.data.datasets[0].data.push(value);
    chart.data.datasets[0].data.shift();
    chart.update();
}

// --- Initialization ---
setInterval(fetchStatus, 3000);
// setInterval(fetchLogs, 2000); // Replaced by WebSocket
setInterval(fetchMainStatus, 4000);
setInterval(updateSystemStats, 2000);
setInterval(updateTokenStats, 10000); // Tokens update less frequently

const getWebAuthnHelper = () => {
    return window.webauthnJSON || window.WebAuthnJSON || (typeof webauthnJSON !== 'undefined' ? webauthnJSON : (typeof WebAuthnJSON !== 'undefined' ? WebAuthnJSON : null));
};

async function registerPasskey() {
    const helper = getWebAuthnHelper();
    if (!helper) {
        showToast("Ошибка: библиотека WebAuthn не загружена", "error");
        return;
    }

    try {
        const optRes = await fetch('/api/auth/register/options');
        if (optRes.status === 401) {
            window.location.href = '/login';
            return;
        }
        const options = await optRes.json();
        const credential = await helper.create({ publicKey: options });

        const verRes = await fetch('/api/auth/register/verify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(credential)
        });

        if (verRes.ok) {
            showToast("Passkey успешно зарегистрирован!", "success");
        } else {
            const err = await verRes.json();
            showToast("Ошибка регистрации: " + err.message, "error");
        }
    } catch (err) {
        console.error(err);
        if (err.name !== 'NotAllowedError') {
            showToast("Ошибка при регистрации Passkey", "error");
        }
    }
}

async function logout() {
    try {
        await fetch('/api/auth/logout', { method: 'POST' });
        window.location.href = '/login';
    } catch (err) {
        window.location.href = '/login';
    }
}

// Global fetch utility to handle 401
async function apiFetch(url, options = {}) {
    const res = await fetch(url, options);
    if (res.status === 401) {
        window.location.href = '/login';
        throw new Error("Unauthorized");
    }
    return res;
}

// Single start
(function init() {
    fetchStatus();
    initLogWebSocket();
    fetchMainStatus();
    initCharts();
    updateSystemStats();
    updateTokenStats();
    updateIcons();

    // Check auth on start
    fetch('/api/status').then(res => {
        if (res.status === 401) window.location.href = '/login';
    });

    setTimeout(() => showToast("Панель управления готова", "success"), 500);
})();
