// =============================================================================
// DOM References
// =============================================================================
const ngrokInput = document.getElementById("ngrokInput");
const saveNgrokBtn = document.getElementById("saveNgrokBtn");
const ngrokStatus = document.getElementById("ngrokStatus");

const phoneInput = document.getElementById("phoneInput");
const normalCallBtn = document.getElementById("normalCallBtn");
const agentCallBtn = document.getElementById("agentCallBtn");

const activeCallPanel = document.getElementById("activeCallPanel");
const callStatusBadge = document.getElementById("callStatusBadge");
const callPhone = document.getElementById("callPhone");
const callTypeBadge = document.getElementById("callTypeBadge");
const callTimer = document.getElementById("callTimer");
const endCallBtn = document.getElementById("endCallBtn");
const transcriptArea = document.getElementById("transcriptArea");

const historyBody = document.getElementById("historyBody");

// =============================================================================
// State
// =============================================================================
let ngrokSaved = false;
let activeCall = null; // { call_uuid, phone_number, call_type, status, startTime }
let timerInterval = null;
let evtSource = null;

// =============================================================================
// SSE Connection
// =============================================================================
function connectSSE() {
    if (evtSource) evtSource.close();
    evtSource = new EventSource("/api/events");

    evtSource.addEventListener("call_status", (e) => {
        const data = JSON.parse(e.data);
        handleCallStatus(data);
    });

    evtSource.addEventListener("transcript", (e) => {
        const data = JSON.parse(e.data);
        handleTranscript(data);
    });

    evtSource.onerror = () => {
        // Auto-reconnect is built into EventSource
    };
}

// =============================================================================
// Call Status Handler
// =============================================================================
function handleCallStatus(data) {
    const status = data.status;

    if (status === "initiated" || status === "ringing") {
        if (!activeCall || activeCall.call_uuid === data.call_uuid || activeCall.call_uuid === data.request_uuid) {
            activeCall = activeCall || {};
            activeCall.call_uuid = data.call_uuid || activeCall.call_uuid;
            activeCall.phone_number = data.phone_number || activeCall.phone_number;
            activeCall.call_type = data.call_type || activeCall.call_type;
            activeCall.status = status;
            showActiveCall();
            updateStatus(status);
        }
    } else if (status === "connected") {
        if (activeCall) {
            activeCall.call_uuid = data.call_uuid || activeCall.call_uuid;
            activeCall.call_id = data.call_id;
            activeCall.status = "connected";
            activeCall.call_type = data.call_type || activeCall.call_type;
            activeCall.startTime = Date.now();
            updateStatus("connected");
            startTimer();
        }
    } else if (status === "ended") {
        if (activeCall && activeCall.call_uuid === data.call_uuid) {
            updateStatus("ended");
            stopTimer();
            setCallControlsEnabled(true);
            // Clear active call after a short delay so user can see "ended"
            setTimeout(() => {
                activeCall = null;
                hideActiveCall();
                loadHistory();
            }, 2000);
        }
    }
}

// =============================================================================
// Transcript Handler
// =============================================================================
function handleTranscript(data) {
    if (!activeCall) return;

    // Remove placeholder
    const placeholder = transcriptArea.querySelector(".transcript-placeholder");
    if (placeholder) placeholder.remove();

    const line = document.createElement("div");
    line.className = "transcript-line";
    const roleClass = data.role === "agent" ? "role-agent" : "role-user";
    line.innerHTML = `<span class="role ${roleClass}">${data.role}:</span> ${escapeHtml(data.text)}`;
    transcriptArea.appendChild(line);
    transcriptArea.scrollTop = transcriptArea.scrollHeight;
}

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

// =============================================================================
// UI Helpers
// =============================================================================
function showActiveCall() {
    activeCallPanel.classList.remove("hidden");
    callPhone.textContent = activeCall.phone_number || "--";
    callTypeBadge.textContent = activeCall.call_type || "--";
    transcriptArea.innerHTML = '<p class="transcript-placeholder">Live transcript will appear here...</p>';
    if (activeCall.call_type === "normal") {
        transcriptArea.innerHTML = '<p class="transcript-placeholder">No transcript for normal calls</p>';
    }
}

function hideActiveCall() {
    activeCallPanel.classList.add("hidden");
    callTimer.textContent = "00:00";
}

function updateStatus(status) {
    callStatusBadge.textContent = status;
    callStatusBadge.className = "badge badge-" + status;
}

function setCallControlsEnabled(enabled) {
    phoneInput.disabled = !enabled;
    normalCallBtn.disabled = !enabled;
    agentCallBtn.disabled = !enabled;
}

function startTimer() {
    stopTimer();
    timerInterval = setInterval(() => {
        if (!activeCall || !activeCall.startTime) return;
        const elapsed = Math.floor((Date.now() - activeCall.startTime) / 1000);
        const mins = String(Math.floor(elapsed / 60)).padStart(2, "0");
        const secs = String(elapsed % 60).padStart(2, "0");
        callTimer.textContent = `${mins}:${secs}`;
    }, 1000);
}

function stopTimer() {
    if (timerInterval) {
        clearInterval(timerInterval);
        timerInterval = null;
    }
}

// =============================================================================
// Ngrok Config
// =============================================================================
saveNgrokBtn.addEventListener("click", async () => {
    const url = ngrokInput.value.trim();
    if (!url) {
        ngrokStatus.textContent = "Please enter a URL";
        ngrokStatus.className = "hint error";
        return;
    }

    try {
        const res = await fetch("/api/ngrok-url", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url }),
        });
        const data = await res.json();
        if (data.success) {
            ngrokStatus.textContent = "Saved: " + data.url;
            ngrokStatus.className = "hint success";
            ngrokSaved = true;
            phoneInput.disabled = false;
            normalCallBtn.disabled = false;
            agentCallBtn.disabled = false;
        } else {
            ngrokStatus.textContent = data.error || "Failed to save";
            ngrokStatus.className = "hint error";
        }
    } catch (err) {
        ngrokStatus.textContent = "Network error";
        ngrokStatus.className = "hint error";
    }
});

// =============================================================================
// Make Call
// =============================================================================
async function makeCall(callType) {
    const phone = phoneInput.value.trim();
    if (!phone) {
        alert("Enter a phone number first");
        return;
    }

    setCallControlsEnabled(false);

    try {
        const res = await fetch("/api/make-call", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                phone_number: phone,
                call_type: callType,
                ngrok_url: ngrokInput.value.trim(),
            }),
        });
        const data = await res.json();
        if (data.success) {
            activeCall = {
                call_uuid: data.request_uuid,
                phone_number: data.phone_number,
                call_type: data.call_type,
                status: "initiated",
                startTime: null,
            };
            showActiveCall();
            updateStatus("initiated");
        } else {
            alert("Call failed: " + (data.error || "Unknown error"));
            setCallControlsEnabled(true);
        }
    } catch (err) {
        alert("Network error: " + err.message);
        setCallControlsEnabled(true);
    }
}

normalCallBtn.addEventListener("click", () => makeCall("normal"));
agentCallBtn.addEventListener("click", () => makeCall("agent"));

// =============================================================================
// End Call
// =============================================================================
endCallBtn.addEventListener("click", async () => {
    if (!activeCall || !activeCall.call_uuid) return;

    try {
        await fetch("/api/end-call", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ call_uuid: activeCall.call_uuid }),
        });
    } catch (err) {
        console.error("End call error:", err);
    }
});

// =============================================================================
// Call History
// =============================================================================
async function loadHistory() {
    try {
        const res = await fetch("/logs");
        const logs = await res.json();

        if (logs.length === 0) {
            historyBody.innerHTML = '<tr><td colspan="7" class="empty-row">No calls yet</td></tr>';
            return;
        }

        historyBody.innerHTML = logs.map((log) => {
            const time = log.started_at ? new Date(log.started_at).toLocaleString() : "--";
            const dur = log.duration_sec != null ? log.duration_sec + "s" : "--";
            const dirClass = log.direction === "inbound" ? "dir-inbound" : "dir-outbound";
            return `<tr>
                <td>${escapeHtml(time)}</td>
                <td>${escapeHtml(log.caller || "--")}</td>
                <td><span class="${dirClass}">${escapeHtml(log.direction || "inbound")}</span></td>
                <td>${escapeHtml(log.call_type || "agent")}</td>
                <td>${dur}</td>
                <td>${log.turn_count != null ? log.turn_count : "--"}</td>
                <td>${escapeHtml(log.status || "--")}</td>
            </tr>`;
        }).join("");
    } catch (err) {
        console.error("Failed to load history:", err);
    }
}

// =============================================================================
// Init
// =============================================================================
async function init() {
    // Load saved ngrok URL
    try {
        const res = await fetch("/api/ngrok-url");
        const data = await res.json();
        if (data.url) {
            ngrokInput.value = data.url;
            ngrokStatus.textContent = "Loaded: " + data.url;
            ngrokStatus.className = "hint success";
            ngrokSaved = true;
            phoneInput.disabled = false;
            normalCallBtn.disabled = false;
            agentCallBtn.disabled = false;
        }
    } catch (e) { /* ignore */ }

    // Check for active calls (page refresh recovery)
    try {
        const res = await fetch("/api/active-calls");
        const calls = await res.json();
        if (calls.length > 0) {
            const c = calls[0];
            activeCall = {
                call_uuid: c.call_uuid,
                phone_number: c.caller,
                call_type: c.call_type,
                status: "connected",
                startTime: new Date(c.started_at).getTime(),
            };
            showActiveCall();
            updateStatus("connected");
            startTimer();
            setCallControlsEnabled(false);
        }
    } catch (e) { /* ignore */ }

    connectSSE();
    loadHistory();
}

init();
