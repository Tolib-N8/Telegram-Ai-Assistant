async function fetchStatus() {
    try {
        const response = await fetch('/api/status');
        const data = await response.json();
        renderAccounts(data.accounts);
    } catch (err) {
        console.error("Error fetching status:", err);
    }
}

async function fetchLogs() {
    try {
        const response = await fetch('/api/logs');
        const data = await response.json();
        const viewer = document.getElementById('log-viewer');
        viewer.innerHTML = data.logs.map(l => `<div>${l}</div>`).join('');
        viewer.scrollTop = viewer.scrollHeight;
    } catch (err) {
        console.error("Error fetching logs:", err);
    }
}

function renderAccounts(accounts) {
    const grid = document.getElementById('accounts-grid');
    if (!accounts || accounts.length === 0) {
        grid.innerHTML = `
            <div class="card" style="grid-column: 1/-1; text-align:center; padding: 4rem;">
                <div style="font-size: 4rem; margin-bottom: 1rem;">💤</div>
                <h3 style="margin:0; opacity:0.7;">Пока нет активных аккаунтов</h3>
                <p style="color:var(--text-dim);">Нажмите кнопку вверху, чтобы добавить первого бота</p>
            </div>`;
        return;
    }

    grid.innerHTML = accounts.map(acc => {
        const isOnline = acc.status.toLowerCase() === 'online';
        const statusColor = isOnline ? '#2ecc71' : '#ff4757';

        return `
        <div class="card">
            <div style="display:flex; justify-content:space-between; align-items:start; margin-bottom: 1.5rem;">
                <div>
                    <h3 style="margin:0; font-size:1.4rem;">👤 ${acc.name}</h3>
                    <div style="display:flex; align-items:center; gap:6px; margin-top:4px; font-size:0.8rem; color:var(--text-dim);">
                        <span class="status-indicator" style="background:${statusColor}; box-shadow: 0 0 10px ${statusColor}44;"></span> ${acc.status}
                    </div>
                </div>
                <div style="background:rgba(255,255,255,0.05); padding:8px; border-radius:12px;">
                    🤖 AI
                </div>
            </div>

            <div class="stats-grid">
                <div class="stat-box">
                    <span class="stat-label">Файлы</span>
                    <span class="stat-val" style="color:var(--accent);">${acc.stats.blocked_files}</span>
                </div>
                <div class="stat-box">
                    <span class="stat-label">Скам</span>
                    <span class="stat-val" style="color:var(--error);">${acc.stats.blocked_scams}</span>
                </div>
                <div class="stat-box" style="grid-column: span 2;">
                    <span class="stat-label">Новые контакты</span>
                    <span class="stat-val" style="color:var(--success); font-size:1.5rem;">${acc.stats.total_unknown}</span>
                </div>
            </div>

            <div style="margin-top:2rem; display:flex; gap:8px;">
                <button class="btn btn-primary" style="flex:1; justify-content:center; font-size:0.8rem;" onclick="openSettings('${acc.name}')">
                    ⚙️
                </button>
                <button class="btn" style="flex:1; justify-content:center; font-size:0.8rem; background:rgba(255,255,255,0.1);" onclick="resetAccountData('${acc.name}')" title="Сбросить историю">
                    🔄
                </button>
                <button class="btn btn-danger" style="padding:12px;" onclick="deleteAccount('${acc.name}')">
                    🗑️
                </button>
            </div>
        </div>`;
    }).join('');
}

async function deleteAccount(name) {
    if (!confirm(`Вы уверены, что хотите УДАЛИТЬ аккаунт ${name}? Это удалит все его данные (сессии, историю, статистику) навсегда.`)) return;
    try {
        const btn = event.target.closest('.btn');
        btn.innerHTML = "⏳";
        btn.disabled = true;

        await fetch(`/api/accounts/${name}`, { method: 'DELETE' });
        fetchStatus();
    } catch (err) {
        alert("Ошибка при удалении");
    }
}

async function resetAccountData(name) {
    if (!confirm(`Вы уверены, что хотите СБРОСИТЬ всю историю и статистику для ${name}? Это действие нельзя отменить.`)) return;
    try {
        const btn = event.target.closest('.btn');
        const oldHtml = btn.innerHTML;
        btn.innerHTML = "⏳";
        btn.disabled = true;

        await fetch(`/api/accounts/reset/${name}`, { method: 'POST' });

        btn.innerHTML = "✅";
        setTimeout(() => {
            btn.innerHTML = oldHtml;
            btn.disabled = false;
            fetchStatus();
        }, 1000);
    } catch (err) {
        alert("Ошибка при сбросе данных");
    }
}

function openSettings(name) {
    document.getElementById('settings-title').innerText = `Настройки: ${name}`;
    document.getElementById('settingsModal').style.display = 'flex';
}

function closeSettings() {
    document.getElementById('settingsModal').style.display = 'none';
}

let currentAuthName = null;
let authCheckInterval = null;

async function startAuth() {
    const nameInput = document.getElementById('account-name');
    const name = nameInput.value.trim();
    if (!name) return alert("Введите имя сессии");

    currentAuthName = name;
    const statusDiv = document.getElementById('auth-status');
    statusDiv.style.color = "var(--text)";
    statusDiv.innerText = "⏳ Инициализация клиента...";

    try {
        const response = await fetch('/api/accounts/auth/start', {
            method: 'POST',
            body: JSON.stringify({ name })
        });
        const data = await response.json();

        if (data.status === 'qr') {
            showQR(data.url);
            startPolling();
        } else if (data.status === 'success') {
            statusDiv.style.color = "var(--success)";
            statusDiv.innerText = "✅ Аккаунт уже авторизован!";
            setTimeout(closeModal, 2000);
        } else {
            statusDiv.style.color = "var(--error)";
            statusDiv.innerText = "❌ Ошибка: " + data.message;
        }
    } catch (err) {
        statusDiv.innerText = "❌ Сбой сети";
    }
}

function showQR(url) {
    document.getElementById('auth-actions').style.display = 'none';
    const container = document.getElementById('qr-container');
    container.style.display = 'block';

    const qrDiv = document.getElementById('qr-code');
    qrDiv.innerHTML = "";

    // Используем qrcode-generator
    const qr = qrcode(0, 'M');
    qr.addData(url);
    qr.make();
    qrDiv.innerHTML = qr.createImgTag(6);
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
                statusDiv.innerText = `✅ Успех! Привет, ${data.user}`;
                setTimeout(() => {
                    closeModal();
                    fetchStatus();
                    location.reload(); // Перезагрузка для чистоты
                }, 3000);
            } else if (data.status === '2fa_needed') {
                clearInterval(authCheckInterval);
                document.getElementById('qr-container').style.display = 'none';
                document.getElementById('2fa-container').style.display = 'block';
            } else if (data.status === 'error') {
                clearInterval(authCheckInterval);
                statusDiv.style.color = "var(--error)";
                statusDiv.innerText = "❌ Ошибка: " + data.message;
            }
        } catch (err) { }
    }, 2000);
}

async function submit2FA() {
    const password = document.getElementById('tf-password').value;
    const statusDiv = document.getElementById('auth-status');
    statusDiv.innerText = "⏳ Проверка пароля...";

    const res = await fetch('/api/accounts/auth/2fa', {
        method: 'POST',
        body: JSON.stringify({ name: currentAuthName, password })
    });
    const data = await res.json();

    if (data.status === 'success') {
        statusDiv.style.color = "var(--success)";
        statusDiv.innerText = `✅ Вход выполнен!`;
        setTimeout(() => { location.reload(); }, 2000);
    } else {
        statusDiv.style.color = "var(--error)";
        statusDiv.innerText = "❌ Ошибка пароля: " + data.message;
    }
}

function openModal() {
    document.getElementById('addModal').style.display = 'flex';
    document.getElementById('auth-actions').style.display = 'block';
    document.getElementById('qr-container').style.display = 'none';
    document.getElementById('2fa-container').style.display = 'none';
    document.getElementById('auth-status').innerText = "";
}
function closeModal() {
    document.getElementById('addModal').style.display = 'none';
    if (authCheckInterval) clearInterval(authCheckInterval);
}

// Запуск фоновых обновлений
setInterval(fetchStatus, 3000);
setInterval(fetchLogs, 2000);
fetchStatus();
fetchLogs();
