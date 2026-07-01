let token = localStorage.getItem('nova_token');
let activeTab = 'overview';
let ws = null;

// DOM Elements
const loginContainer = document.getElementById('login-container');
const dashboardContainer = document.getElementById('dashboard-container');
const loginForm = document.getElementById('login-form');
const adminPasswordInput = document.getElementById('admin-password');
const loginError = document.getElementById('login-error');

const navItems = document.querySelectorAll('.nav-item');
const tabTitle = document.getElementById('current-tab-title');
const tabPanels = document.querySelectorAll('.tab-content');
const logoutBtn = document.getElementById('logout-btn');

const consoleLogs = document.getElementById('console-logs');
const consoleForm = document.getElementById('console-form');
const consoleInput = document.getElementById('console-input');

const contextList = document.getElementById('context-list');
const deviceList = document.getElementById('device-list');
const rulesList = document.getElementById('rules-list');
const historyTableBody = document.getElementById('history-table-body');
const conflictList = document.getElementById('conflict-list');

const ruleForm = document.getElementById('rule-form');
const triggerTypeSelect = document.getElementById('trigger-type');
const timeParams = document.getElementById('time-params');
const notificationParams = document.getElementById('notification-params');

// Initialization
document.addEventListener('DOMContentLoaded', () => {
    if (token) {
        showDashboard();
    } else {
        showLogin();
    }
});

// Auth routing
function showLogin() {
    loginContainer.classList.remove('hidden');
    dashboardContainer.classList.add('hidden');
    document.body.style.display = 'flex';
}

function showDashboard() {
    loginContainer.classList.add('hidden');
    dashboardContainer.classList.remove('hidden');
    document.body.style.display = 'block';
    
    // Trigger workspace queries
    fetchWorkspaceData();
    connectWebSocket();
}

loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const password = adminPasswordInput.value;
    
    try {
        const response = await fetch('/auth/token', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: 'admin', password: password })
        });
        
        if (response.ok) {
            const data = await response.json();
            token = data.access_token;
            localStorage.setItem('nova_token', token);
            loginError.classList.add('hidden');
            showDashboard();
        } else {
            loginError.classList.remove('hidden');
        }
    } catch (err) {
        loginError.classList.remove('hidden');
    }
});

logoutBtn.addEventListener('click', () => {
    localStorage.removeItem('nova_token');
    token = null;
    if (ws) ws.close();
    showLogin();
});

// Navigation Toggle
navItems.forEach(item => {
    item.addEventListener('click', () => {
        navItems.forEach(i => i.classList.remove('active'));
        item.classList.add('active');
        
        activeTab = item.dataset.tab;
        tabTitle.textContent = item.textContent;
        
        tabPanels.forEach(panel => {
            if (panel.id === `tab-${activeTab}`) {
                panel.classList.remove('hidden');
            } else {
                panel.classList.add('hidden');
            }
        });

        fetchWorkspaceData();
    });
});

// Dynamic trigger fields toggle
triggerTypeSelect.addEventListener('change', (e) => {
    const trigger = e.target.value;
    if (trigger === 'time') {
        timeParams.classList.remove('hidden');
        notificationParams.classList.add('hidden');
    } else if (trigger === 'notification') {
        timeParams.classList.add('hidden');
        notificationParams.classList.remove('hidden');
    } else {
        timeParams.classList.add('hidden');
        notificationParams.classList.add('hidden');
    }
});

// REST & Websocket Operations
async function fetchWorkspaceData() {
    if (!token) return;
    
    const headers = { 'Authorization': `Bearer ${token}` };
    
    if (activeTab === 'overview') {
        // 1. Get presence devices
        try {
            const res = await fetch('/capabilities', { headers });
            if (res.ok) {
                const data = await res.json();
                renderDeviceList(data.devices || []);
            }
        } catch(e){}

        // 2. Get active context diagnostics
        try {
            const res = await fetch('/context/dump', { headers });
            if (res.ok) {
                const data = await res.json();
                renderContextList(data);
            }
        } catch(e){}

        // 3. Get context conflicts
        try {
            const res = await fetch('/context/conflicts', { headers });
            if (res.ok) {
                const data = await res.json();
                renderConflictList(data);
            }
        } catch(e){}
    }
    
    if (activeTab === 'rules') {
        fetchRules();
    }
    
    if (activeTab === 'history') {
        fetchHistory();
    }
}

async function fetchRules() {
    const headers = { 'Authorization': `Bearer ${token}` };
    try {
        const response = await fetch('/automation/rules', { headers });
        if (response.ok) {
            const data = await response.json();
            renderRules(data.rules || []);
        }
    } catch(e) {}
}

async function fetchHistory() {
    const headers = { 'Authorization': `Bearer ${token}` };
    try {
        const response = await fetch('/history', { headers });
        if (response.ok) {
            const data = await response.json();
            renderHistory(data.history || []);
        }
    } catch(e) {}
}

function renderRules(rules) {
    if (rules.length === 0) {
        rulesList.innerHTML = '<p class="empty-msg">No active rules compiled.</p>';
        return;
    }
    
    rulesList.innerHTML = rules.map(rule => `
        <div class="list-item">
            <div>
                <strong>${rule.name}</strong><br>
                <small style="color: var(--text-secondary)">Condition: ${JSON.stringify(rule.condition)}</small><br>
                <small style="color: var(--text-secondary)">Action: ${rule.action_template.action_type}</small>
            </div>
            <button onclick="deleteRule('${rule.id}')" style="background:transparent; border:none; color:var(--error); cursor:pointer; font-weight:600">Delete</button>
        </div>
    `).join('');
}

async function deleteRule(id) {
    const headers = { 'Authorization': `Bearer ${token}` };
    try {
        const response = await fetch(`/automation/rules/${id}`, { method: 'DELETE', headers });
        if (response.ok) {
            fetchRules();
        }
    } catch(e){}
}

function renderHistory(logs) {
    if (logs.length === 0) {
        historyTableBody.innerHTML = '<tr><td colspan="5" class="empty-msg">No commands tracked in database.</td></tr>';
        return;
    }
    
    historyTableBody.innerHTML = logs.map(log => {
        const date = new Date(log.command_created_at).toLocaleString();
        const status = log.executed ? '<span style="color:var(--success)">Success</span>' : '<span style="color:var(--text-secondary)">Pending</span>';
        return `
            <tr>
                <td>${date}</td>
                <td>${log.source_device_id}</td>
                <td>${log.raw_text}</td>
                <td>${log.routed_path}</td>
                <td>${status}</td>
            </tr>
        `;
    }).join('');
}

// Post direct commands
consoleForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const text = consoleInput.value.trim();
    if (!text) return;
    
    appendConsoleLog(`You: ${text}`, 'user');
    consoleInput.value = '';
    
    const headers = { 
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
    };
    
    try {
        const response = await fetch('/command', {
            method: 'POST',
            headers,
            body: JSON.stringify({ raw_text: text, source_device_id: 'web_dashboard' })
        });
        
        if (response.ok) {
            const data = await response.json();
            appendConsoleLog(`Nova: ${data.response_text || 'Acknowledged'} (Routed: ${data.routed_path})`, 'nova');
        } else {
            appendConsoleLog('Error: Failed to process command.', 'error');
        }
    } catch (err) {
        appendConsoleLog('Error: Core server unreachable.', 'error');
    }
});

function appendConsoleLog(message, type) {
    const logEl = document.createElement('div');
    logEl.textContent = message;
    if (type === 'user') logEl.style.color = 'var(--text-primary)';
    if (type === 'nova') logEl.style.color = 'var(--accent)';
    if (type === 'error') logEl.style.color = 'var(--error)';
    
    consoleLogs.appendChild(logEl);
    consoleLogs.scrollTop = consoleLogs.scrollHeight;
}

// Add rule submissions
ruleForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    
    const name = document.getElementById('rule-name').value;
    const trigger = triggerTypeSelect.value;
    
    let condition = { type: trigger };
    if (trigger === 'time') {
        condition.interval_minutes = parseInt(document.getElementById('interval-minutes').value);
    } else if (trigger === 'notification') {
        condition.source = document.getElementById('notification-source').value;
        condition.contains = document.getElementById('notification-contains').value;
    }
    
    const action_template = {
        action_type: document.getElementById('action-type').value,
        category: document.getElementById('action-category').value,
        params: {},
        source_device_id: 'desktop_agent_client',
        origin: 'cloud_llm'
    };
    
    const headers = { 
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
    };
    
    try {
        const response = await fetch('/automation/rules', {
            method: 'POST',
            headers,
            body: JSON.stringify({ name, condition, action_template })
        });
        
        if (response.ok) {
            ruleForm.reset();
            fetchRules();
        }
    } catch(e){}
});

// Live updates
function renderDeviceList(devices) {
    if (!devices || devices.length === 0) {
        deviceList.innerHTML = '<p class="empty-msg">No active devices registered.</p>';
        return;
    }
    deviceList.innerHTML = devices.map(device => {
        const statusText = device.online ? 'Online' : 'Offline';
        const statusColor = device.online ? 'var(--success)' : 'var(--text-secondary)';
        return `
            <div class="list-item">
                <span><strong>${device.name}</strong> (${device.platform})</span>
                <span style="color:${statusColor}">${statusText}</span>
            </div>
        `;
    }).join('');
}

function renderContextList(contextData) {
    const details = contextData.details || [];
    if (details.length === 0) {
        contextList.innerHTML = '<p class="empty-msg">No active contexts found.</p>';
        return;
    }
    contextList.innerHTML = details.map(c => `
        <div class="list-item" style="flex-direction: column; align-items: flex-start; gap: 4px;">
            <div style="display: flex; justify-content: space-between; width: 100%;">
                <span style="font-weight: 600; color: var(--accent);">${c.key}</span>
                <span class="badge" style="background: rgba(46, 143, 255, 0.15); color: var(--accent); padding: 2px 6px; border-radius: 4px; font-size: 0.8rem;">${c.priority} Priority</span>
            </div>
            <div style="font-size: 0.85rem; color: var(--text-secondary); display: flex; gap: 15px; margin-top: 4px;">
                <span>Size: ${c.value_length_chars} chars</span>
                <span>TTL: ${Math.round(c.ttl_remaining_seconds)}s</span>
            </div>
        </div>
    `).join('');
}

function renderConflictList(conflicts) {
    if (!conflicts || conflicts.length === 0) {
        conflictList.innerHTML = '<p class="empty-msg" style="color: var(--success)">✓ No sync conflicts detected. System healthy.</p>';
        return;
    }
    conflictList.innerHTML = conflicts.map(c => {
        const date = new Date(c.created_at).toLocaleString();
        return `
            <div class="list-item" style="flex-direction: column; align-items: flex-start; gap: 6px; border-color: rgba(248, 81, 73, 0.2);">
                <div style="display: flex; justify-content: space-between; width: 100%;">
                    <span style="color: var(--error); font-weight: 600;">Conflict on: ${c.key}</span>
                    <span style="font-size: 0.8rem; color: var(--text-secondary);">${date}</span>
                </div>
                <div style="font-size: 0.85rem; color: var(--text-secondary);">
                    Winner: <span style="color: var(--success); font-weight: 600;">${c.winning_device_id}</span> | 
                    Losing Request: <span style="color: var(--error);">${c.losing_device_id}</span>
                </div>
                <div style="font-size: 0.85rem; color: var(--text-secondary); font-style: italic; margin-top: 2px; padding-left: 8px; border-left: 2px solid rgba(248, 81, 73, 0.3);">
                    ${c.conflict_details}
                </div>
            </div>
        `;
    }).join('');
}

// WebSockets link
function connectWebSocket() {
    const wsScheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const wsUrl = `${wsScheme}://${window.location.host}/ws/web_dashboard?token=${token}`;
    
    ws = new WebSocket(wsUrl);
    
    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.event === 'presence.changed' || data.event === 'context.update') {
                fetchWorkspaceData();
            }
        } catch(e){}
    };
    
    ws.onclose = () => {
        // Try reconnecting in 5s
        setTimeout(() => {
            if (token) connectWebSocket();
        }, 5000);
    };
}
