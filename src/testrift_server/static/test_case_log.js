
// ============================================================================
// CONFIGURATION AND CONSTANTS
// ============================================================================

// Protocol decoder for compact binary protocol
const protocolDecoder = typeof ProtocolDecoder !== 'undefined' ? new ProtocolDecoder() : null;

// Color and styling constants
const GOLDEN_ANGLE = 137.508;

// Device and message processing constants
const DevMode = { Normal: 0, Setup: 1, Teardown: 2 };
const MSG_JOIN_TMO_MS = 10;
let logTimeRefreshPending = false;

// Counter for generating unique IDs
let lblId = 0;

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

// Notify the comments UI that log row visibility changed (Source checkbox
// toggle or text filter) so comment threads whose lines are now entirely
// hidden get hidden too, and threads with any visible line reappear.
function refreshCommentThreadVisibility() {
    if (window.CommentsUI && window.CommentsUI.Log && typeof window.CommentsUI.Log.applyCommentedFilter === 'function') {
        window.CommentsUI.Log.applyCommentedFilter();
    } else {
        refreshLogTimeDisplay();
    }
}

document.addEventListener('logvisibilitychange', refreshLogTimeDisplay);

/**
 * Generate a unique color for each index using the golden angle.
 * This ensures colors are well-distributed and readable with white text.
 */
function selectColor(i) {
    // Use golden angle to spread hues evenly
    const hue = (i * GOLDEN_ANGLE) % 360;
    // Use 45% saturation and 40% lightness for good contrast with white text
    return `hsl(${hue}, 65%, 40%)`;
}

function generateLabelId() {
    return 'id' + lblId++;
}

function generateBadge(entry, labelName) {
    return `<span class="badge badge-custom" style="background-color: ${entry.color};">${labelName}</span>`;
}

function updateStatusBadge() {
    const statusBadge = document.getElementById('tc-status-badge');
    if (!statusBadge) return;

    const status = templateConfig.testCaseStatus || 'unknown';
    const result = templateConfig.testCaseResult || '';

    let displayStatus, statusClass, showSpinner = false;
    const s = status.toLowerCase();

    if (s === 'passed') {
        displayStatus = 'PASSED';
        statusClass = 'status-passed';
    } else if (s === 'error') {
        displayStatus = 'ERROR';
        statusClass = 'status-error';
    } else if (s === 'failed') {
        displayStatus = 'FAILED';
        statusClass = 'status-failed';
    } else if (s === 'skipped') {
        displayStatus = 'SKIPPED';
        statusClass = 'status-skipped';
    } else if (s === 'aborted') {
        displayStatus = 'ABORTED';
        statusClass = 'status-failed';
    } else if (s === 'running') {
        displayStatus = 'Running';
        statusClass = 'status-running';
        showSpinner = true;
    } else {
        displayStatus = 'UNKNOWN';
        statusClass = 'status-unknown';
    }

    statusBadge.className = `status-badge ${statusClass}`;
    statusBadge.innerHTML = displayStatus + (showSpinner ? '<span class="spinner"></span>' : '');
}

// ============================================================================
// LIVE INDICATOR FUNCTIONS
// ============================================================================

// Define as variables to ensure they're available immediately
var removeLiveIndicator = function() {
    const liveIndicator = document.querySelector('.live-indicator');
    if (liveIndicator) {
        liveIndicator.remove();
    }
};

var addLiveIndicator = function() {
    const tableBody = document.querySelector('#msg_table tbody');
    if (tableBody) {
        // Remove existing indicator first
        removeLiveIndicator();

        const liveRow = document.createElement('tr');
        liveRow.className = 'live-indicator';
        liveRow.innerHTML = '<td colspan="4" style="text-align: left; color: #666; font-style: italic;"><span class="dots"><span class="dot">.</span><span class="dot">.</span><span class="dot">.</span></span></td>';
        tableBody.appendChild(liveRow);
    }
};

// ============================================================================
// DEVICE AND SOURCE LABEL MANAGEMENT
// ============================================================================

function addComponentLabel(compMap, row, labelName) {
    let component;
    if (!compMap.has(labelName)) {
        component = {
            color: selectColor(colorIndex++),
            channels: new Map(),
            mode: DevMode.Normal,
            labelUid: generateLabelId()
        };
        compMap.set(labelName, component);
    } else {
        component = compMap.get(labelName);
    }

    if (row) row.addClass(component.labelUid);
    return [component, generateBadge(component, labelName)];
}

function addChannelLabel(row, parentComp, labelName) {
    let channel;
    if (!parentComp.channels.has(labelName)) {
        channel = {
            color: selectColor(colorIndex++),
            labelUid: generateLabelId()
        };
        parentComp.channels.set(labelName, channel);
    } else {
        channel = parentComp.channels.get(labelName);
    }

    row.addClass(channel.labelUid);
    return generateBadge(channel, labelName);
}

// ============================================================================
// AT COMMAND PROCESSING
// ============================================================================

function generateAtLine(atLookupTable, isRx, atLine) {
    const directionTooltip = isRx
        ? "Response received by host from DUT"
        : "Command sent by host to DUT";
    const directionClass = isRx ? "at-rx" : "at-tx";
    let helpHtml = "";
    atLine = atLine.trim()
    const plainAtLine = $("<div>").html(atLine).text().trim();
    if (atLookupTable !== null) {
        const parsed_cmd = parseAtCommand(atLine);
        const info = atLookupTable[parsed_cmd.commandWithOp];
        if (info) {
            helpHtml = `<span class="at_help">${info.brief}`;
            if (parsed_cmd.params.length > 0) {
                const params = [];

                parsed_cmd.params.forEach((paramVal, index) => {
                    const param = info.params[index];
                    if (param) {
                        if (paramVal in param.values) {
                            params.push(`${param.name}: ${param.values[paramVal]} (${paramVal})`);
                        } else {
                            params.push(`${param.name}: ${paramVal}`);
                        }
                    }
                });

                if (params.length > 0) {
                    helpHtml += ` [${params.join(", ")}]`;
                }

            }
            helpHtml += "</span>";
        }
    }
    const helpLayoutClass = !helpHtml
        ? " at-no-help"
        : plainAtLine.length > 24
            ? " at-help-below"
            : "";
    return `<span class="log-msg-line"><span class="at-command-detail ${directionClass}${helpLayoutClass}" title="${directionTooltip}"><span class="at_cmd">${atLine}</span>${helpHtml}</span></span>`;
}

// ============================================================================
// ANSI COLOR PARSING
// ============================================================================

/**
 * Parse ANSI escape codes and convert to HTML with inline styles.
 * When ANSI codes are detected, wraps the entire message in a near-black background box.
 */
function parseAnsiColors(text) {
    // Check if text contains ANSI codes
    if (!text.includes('\x1b[') && !text.includes('\u001b[')) {
        return text;
    }

    const ansiRegex = /\x1b\[(\d+(?:;\d+)*)m/g;
    let result = '';
    let lastIndex = 0;
    let currentStyles = [];

    // ANSI color mappings
    const colorMap = {
        30: '#000000', 31: '#cd3131', 32: '#0dbc79', 33: '#e5e510',
        34: '#2472c8', 35: '#bc3fbc', 36: '#11a8cd', 37: '#e5e5e5',
        90: '#666666', 91: '#f14c4c', 92: '#23d18b', 93: '#f5f543',
        94: '#3b8eea', 95: '#d670d6', 96: '#29b8db', 97: '#e5e5e5'
    };

    const bgColorMap = {
        40: '#000000', 41: '#cd3131', 42: '#0dbc79', 43: '#e5e510',
        44: '#2472c8', 45: '#bc3fbc', 46: '#11a8cd', 47: '#e5e5e5',
        100: '#666666', 101: '#f14c4c', 102: '#23d18b', 103: '#f5f543',
        104: '#3b8eea', 105: '#d670d6', 106: '#29b8db', 107: '#e5e5e5'
    };

    text.replace(ansiRegex, (match, codes, offset) => {
        // Add text before this escape code
        if (offset > lastIndex) {
            const textBefore = text.substring(lastIndex, offset);
            if (currentStyles.length > 0) {
                result += `<span style="${currentStyles.join('; ')}">${textBefore}</span>`;
            } else {
                result += textBefore;
            }
        }

        // Process the escape codes
        const codeList = codes.split(';').map(c => parseInt(c));
        for (const code of codeList) {
            if (code === 0) {
                // Reset all styles
                currentStyles = [];
            } else if (code === 1) {
                // Bold
                currentStyles.push('font-weight: bold');
            } else if (code >= 30 && code <= 37 || code >= 90 && code <= 97) {
                // Foreground color
                currentStyles = currentStyles.filter(s => !s.startsWith('color:'));
                currentStyles.push(`color: ${colorMap[code]}`);
            } else if (code >= 40 && code <= 47 || code >= 100 && code <= 107) {
                // Background color
                currentStyles = currentStyles.filter(s => !s.startsWith('background-color:'));
                currentStyles.push(`background-color: ${bgColorMap[code]}`);
            }
        }

        lastIndex = offset + match.length;
        return '';
    });

    // Add remaining text
    if (lastIndex < text.length) {
        const textAfter = text.substring(lastIndex);
        if (currentStyles.length > 0) {
            result += `<span style="${currentStyles.join('; ')}">${textAfter}</span>`;
        } else {
            result += textAfter;
        }
    }

    // Wrap entire result in near-black background box since ANSI was detected
    return `<span style="background-color: #1a1a1a; padding: 2px 4px; border-radius: 3px; display: inline-block;">${result}</span>`;
}

// ============================================================================
// LOG MESSAGE PROCESSING
// ============================================================================

function processLogMessage(d, compMap, chanList, atLookupTable) {
    const msgTableBody = $("#msg_table tbody");

    // --- Teardown collapsible group (based on d.phase === "teardown") ---
    // Only appears if at least one teardown-phase log is actually rendered.
    if (!window.__teardownGroupState) {
        window.__teardownGroupState = {
            headerRow: null,
            collapsed: true
        };
    }
    const teardownGroup = window.__teardownGroupState;

    let msgIsEcho = false;
    const originalTime = d.timestamp || '';
    const time = formatLogTime(originalTime);
    let chanName = d.channel || '';
    let compName = d.component || '';
    let messageText = d.message || '';
    const dir = d.dir || null; // Optional explicit direction: 'tx' / 'rx'
    const phase = d.phase || null; // Optional phase marker ("teardown")
    const kind = d.kind || null; // Optional kind marker (e.g. "exception")

    if (compName == "") {
        compName = chanName;
        chanName = "";
    }
    if (compName == "") {
        compName = "Unknown";
    }

    // Direction is determined solely from the explicit dir field when present.
    const isReceived = !!dir && dir.toLowerCase() === 'rx';
    const isTransmitted = !!dir && dir.toLowerCase() === 'tx';

    // Echo detection uses the normalized message text and explicit direction only.
    if (isReceived) {
        if (atMap.has(chanName) && (messageText == atMap.get(chanName))) {
            msgIsEcho = true;
        }
    } else if (isTransmitted) {
        atMap.set(chanName, messageText);
    }
    if (messageText.trim() == "") {
        // Skip empty lines
        return
    }

    // Parse ANSI color codes before filter check
    messageText = parseAnsiColors(messageText);

    // Apply current filter to the clean message text before any HTML processing
    if (currentFilter) {
        const matches = currentFilter.regex.test(messageText);
        const shouldShow = currentFilter.mode === 'include' ? matches : !matches;

        if (!shouldShow) {
            return; // Skip adding this row entirely
        }
    }

    if (msgIsEcho) {
        messageText = `<span style=\"color: #777;\">${messageText}</span>`
    }

    const row = $("<tr>").addClass(chanName).addClass("log-entry-row");
    if (typeof window.__logLineIndex !== "number") {
        window.__logLineIndex = 0;
    }
    const logIndex = window.__logLineIndex++;
    row.attr("data-log-index", logIndex);
    const gutterCell = $('<td class="log-gutter">').html('<span class="tr-comment-gutter-add">+</span>');
    gutterCell.appendTo(row);
    if (phase === "teardown") {
        row.addClass("teardown-log-row");
    }
    if (kind === "exception") {
        row.addClass("stacktrace-log-row");
    }

    // Create time cell with original timestamp stored as data attribute
    const timeCell = $(`<td class="fit log-time">`).css("padding", "3px");
    timeCell.attr('data-original-time', originalTime);

    timeCell.text(time);

    timeCell.appendTo(row);

    let badges = "";
    const [component, compBadge] = addComponentLabel(compMap, row, compName);
    badges += compBadge;

    if (chanName == "Teardown") {
        component.mode = DevMode.Teardown;
    } else if (component.mode == DevMode.Teardown) {
        // We're currently in the component teardown stage, so add a "Teardown" label
        const teardownBadge = addChannelLabel(row, component, "Teardown");
        badges += teardownBadge
    }

    if (chanName == "Setup") {
        if (messageText.includes("SETUP") && messageText.includes("DONE")) {
            component.mode = DevMode.Normal;
        } else {
            component.mode = DevMode.Setup;
        }
    } else if (component.mode == DevMode.Setup) {
        // We're currently in the component setup stage, so add a "Setup" label
        const setupBadge = addChannelLabel(row, component, "Setup");
        badges += setupBadge
    }

    if (chanName != "") {
        const channelBadge = addChannelLabel(row, component, chanName);
        badges += channelBadge
    }

    if (messageText.includes("ASSERT FAILURE")) {
        badges += addChannelLabel(row, component, "Assert");
    }

    if (badges.includes(">Assert<")) {
        let li = $(`<li class="list-group-item">`)
        .html(`<strong>Assert failure:</strong> ${messageText}`)
        .css('cursor', 'pointer')
        .on("mousedown", function () {
            const w = $(window);
            $('html,body').animate({ scrollTop: row.offset().top - (w.height() / 2) }, 200);
        });
        $("#test_info_list").append(li);
        messageText = `<span class="log-assert">${messageText}</span>`;
    }

    $(`<td class="fit">`)
    .html(badges)
    .appendTo(row);

    const currentMsgTime = new Date(originalTime);
    const groupingKey = LogGrouping.key(badges, dir);
    const isDirected = isReceived || isTransmitted;
    const messageHtml = isDirected
        ? generateAtLine(atLookupTable, isReceived, messageText)
        : messageText;

    if (LogGrouping.shouldJoin(lastMsgGroup, {
        key: groupingKey,
        time: currentMsgTime,
        kind: kind
    }, MSG_JOIN_TMO_MS)) {
        const continuationHtml = isDirected
            ? generateAtLine(atLookupTable, isReceived, messageText)
            : messageHtml;
        lastTd.append(`<br>${continuationHtml}`);
        window.__logLineIndex -= 1;
        return;
    }

    lastMsgGroup = {
        key: groupingKey,
        startTime: currentMsgTime
    };
    lastTd = $(`<td class="log-message">`).html(messageHtml).appendTo(row);

    if (phase === "teardown") {
        if (!teardownGroup.headerRow) {
            const header = $(`
                <tr class="teardown-header-row collapsed">
                    <td colspan="4">
                        <span class="teardown-header-content">
                            <span class="teardown-toggle-icon" role="button" tabindex="0" aria-label="Toggle teardown logs">▸</span>
                            <span class="teardown-header-label">Teardown</span>
                        </span>
                    </td>
                </tr>
            `);

            const applyVisibility = () => {
                const isCollapsed = teardownGroup.collapsed;
                header.toggleClass("collapsed", isCollapsed);
                $("#msg_table tbody tr.teardown-log-row").toggle(!isCollapsed);
                // Update the glyph so it never "disappears" due to pseudo-element/CSS quirks
                header.find(".teardown-toggle-icon").text(isCollapsed ? "▸" : "▾");
                refreshCommentThreadVisibility();
            };

            const toggle = () => {
                teardownGroup.collapsed = !teardownGroup.collapsed;
                applyVisibility();
            };

            // Revert to simple behavior: clicking anywhere on the header toggles.
            header.on("click", toggle);
            header.on("keydown", (e) => {
                if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    toggle();
                }
            });

            teardownGroup.headerRow = header;
            teardownGroup.collapsed = true;
            msgTableBody.append(header);
            applyVisibility();
        }

        row.appendTo(msgTableBody);
        if (teardownGroup.collapsed) {
            row.hide();
        }
    } else {
        row.appendTo(msgTableBody);
    }

    if (showDeltaTime && !logTimeRefreshPending) {
        logTimeRefreshPending = true;
        queueMicrotask(() => {
            logTimeRefreshPending = false;
            refreshCommentThreadVisibility();
        });
    }

    // Always ensure live indicator is at the bottom if test case is still running
    if (testCaseStatus === 'running' || testCaseStatus === 'unknown') {
        // Remove existing indicator first
        removeLiveIndicator();
        // Add it back at the bottom
        addLiveIndicator();
    }
}

// ============================================================================
// UI EVENT HANDLERS
// ============================================================================

function handleCBChange(ev) {
    const checkbox = $(ev.target);
    const labelUid = checkbox.attr("labelUid");
    const isChecked = checkbox.prop("checked");

    // Show/hide rows with this label
    if (isChecked) {
        $(`.${labelUid}`).show();
    } else {
        $(`.${labelUid}`).hide();
    }

    // Update child checkboxes
    checkbox.closest('li').find('ul .tree-checkbox').each(function () {
        $(this).prop('checked', isChecked);
    });

    refreshCommentThreadVisibility();
}

function updateChannelList(compMap, chanList) {
    for (let [compName, component] of compMap) {
        const listItem = $("<li>").appendTo(chanList);
        const checkbox = $("<input>")
        .attr("type", "checkbox")
        .prop("checked", true)
        .attr("labelUid", component.labelUid)
        .addClass("tree-checkbox")
        .on("change", handleCBChange);
        const label = `<span class="badge badge-custom" style="background-color: ${component.color};">${compName}</span>`;
        listItem.append(checkbox).append(" ").append(label);
        for (let [chanName, channel] of component.channels) {
            const chanUl = $("<ul>").appendTo(listItem);
            const chanLi = $("<li>").appendTo(chanUl);
            const chanCheckbox = $("<input>")
            .attr("type", "checkbox")
            .prop("checked", true)
            .attr("labelUid", channel.labelUid)
            .addClass("tree-checkbox")
            .on("change", handleCBChange);
            const chanLabel = `<span class="badge badge-custom" style="background-color: ${channel.color};">${chanName}</span>`;
            chanLi.append(chanCheckbox).append(" ").append(chanLabel);
        }
    }
}

// ============================================================================
// TIME UTILITIES
// ============================================================================

function parseUtcDate(utcTimeString) {
    if (!utcTimeString) {
        return null;
    }
    let fixedTimeString = utcTimeString;
    if (fixedTimeString.includes('+00:00Z')) {
        fixedTimeString = fixedTimeString.replace('+00:00Z', 'Z');
    } else if (fixedTimeString.includes('+00:00') && !fixedTimeString.endsWith('Z')) {
        fixedTimeString = fixedTimeString.replace('+00:00', 'Z');
    }
    const utcDate = new Date(fixedTimeString);
    if (isNaN(utcDate.getTime())) {
        return null;
    }
    return utcDate;
}

function convertToLocalTime(utcTimeString) {
    const utcDate = parseUtcDate(utcTimeString);
    if (!utcDate) {
        return utcTimeString;
    }
    const localTime = utcDate.toLocaleString();
    const ms = String(utcDate.getMilliseconds()).padStart(3, '0');
    return `${localTime}.${ms}`;
}

function formatLogTime(utcTimeString) {
    const utcDate = parseUtcDate(utcTimeString);
    if (!utcDate) {
        return utcTimeString || '';
    }
    const ms = String(utcDate.getMilliseconds()).padStart(3, '0');
    const time = utcDate.toLocaleTimeString(undefined, {
        hour: 'numeric',
        minute: '2-digit',
        second: '2-digit'
    });
    return time.replace(/(\d+:\d+:\d+)/, `$1.${ms}`);
}

function calculateDeltaTime(currentTimeString) {
    if (!lastVisibleMessageTime) {
        // Fix malformed timestamp format
        let fixedTimeString = currentTimeString;
        if (fixedTimeString.includes('+00:00Z')) {
            fixedTimeString = fixedTimeString.replace('+00:00Z', 'Z');
        } else if (fixedTimeString.includes('+00:00') && !fixedTimeString.endsWith('Z')) {
            fixedTimeString = fixedTimeString.replace('+00:00', 'Z');
        }
        lastVisibleMessageTime = new Date(fixedTimeString);
        return "0ms";
    }

    // Fix malformed timestamp format
    let fixedTimeString = currentTimeString;
    if (fixedTimeString.includes('+00:00Z')) {
        fixedTimeString = fixedTimeString.replace('+00:00Z', 'Z');
    } else if (fixedTimeString.includes('+00:00') && !fixedTimeString.endsWith('Z')) {
        fixedTimeString = fixedTimeString.replace('+00:00', 'Z');
    }

    const currentTime = new Date(fixedTimeString);
    const delta = currentTime - lastVisibleMessageTime;
    lastVisibleMessageTime = currentTime;

    if (delta < 0) {
        // Handle case where time goes backwards (shouldn't happen but just in case)
        return "0ms";
    } else if (delta < 1000) {
        return `+${delta}ms`;
    } else if (delta < 60000) {
        return `+${(delta / 1000).toFixed(2)}s`;
    } else {
        const minutes = Math.floor(delta / 60000);
        const seconds = ((delta % 60000) / 1000).toFixed(1);
        return `+${minutes}m ${seconds}s`;
    }
}

function toggleTimeDisplay() {
    showDeltaTime = !showDeltaTime;
    const button = document.getElementById('time-display-toggle');
    button.textContent = showDeltaTime ? 'Show Absolute Time' : 'Show Delta Time';
    refreshLogTimeDisplay();

    // Save user preference
    saveTimeDisplayPreference();
}

function refreshLogTimeDisplay() {
    lastVisibleMessageTime = null;
    const messageRows = Array.from(document.querySelectorAll('#msg_table tbody tr.log-entry-row'));
    const rowsToUpdate = showDeltaTime
        ? messageRows.filter(row => !row.hidden && getComputedStyle(row).display !== 'none')
        : messageRows;
    rowsToUpdate.forEach(row => {
        const timeCell = row.querySelector('.log-time');
        if (!timeCell) return;
        const originalTime = timeCell.getAttribute('data-original-time');
        if (!originalTime) return;
        if (showDeltaTime) {
            timeCell.textContent = calculateDeltaTime(originalTime);
            timeCell.style.fontFamily = 'monospace';
        } else {
            timeCell.textContent = formatLogTime(originalTime);
            timeCell.style.fontFamily = '';
            timeCell.style.color = '';
        }
    });
}

function convertTimesToLocal() {
    // Convert start time and end time in info items
    document.querySelectorAll('.info-item').forEach(item => {
        const strong = item.querySelector('strong');
        if (strong && (strong.textContent === 'Start Time' || strong.textContent === 'End Time')) {
            const valueElement = strong.nextSibling;
            if (valueElement && valueElement.textContent) {
                const text = valueElement.textContent.trim();
                if (text && (text.includes('T') || text.includes('Z'))) {
                    valueElement.textContent = ' ' + convertToLocalTime(text);
                }
            }
        }
    });

    refreshLogTimeDisplay();
}

// Load time display preference from localStorage
function loadTimeDisplayPreference() {
    const savedTimeMode = localStorage.getItem('tcLogTimeDisplayMode');
    if (savedTimeMode === 'delta') {
        showDeltaTime = true;
    }
}

// Save time display preference to localStorage
function saveTimeDisplayPreference() {
    localStorage.setItem('tcLogTimeDisplayMode', showDeltaTime ? 'delta' : 'absolute');
}

// Load sidebar state preference from localStorage
function loadSidebarPreference() {
    const savedSidebarState = localStorage.getItem('tcLogSidebarState');
    if (savedSidebarState === 'collapsed') {
        sidebarCollapsed = true;
    }
}

// Save sidebar state preference to localStorage
function saveSidebarPreference() {
    localStorage.setItem('tcLogSidebarState', sidebarCollapsed ? 'collapsed' : 'expanded');
}

// Apply loaded sidebar preference to UI
function applySidebarPreference() {
    if (sidebarCollapsed) {
        devicesSidebar.classList.add('collapsed');
        mainContent.classList.add('sidebar-collapsed');
    } else {
        devicesSidebar.classList.remove('collapsed');
        mainContent.classList.remove('sidebar-collapsed');
    }
}

// Apply loaded time display preference to UI
function applyTimeDisplayPreference() {
    const button = document.getElementById('time-display-toggle');
    button.textContent = showDeltaTime ? 'Show Absolute Time' : 'Show Delta Time';
    refreshLogTimeDisplay();
}


// ============================================================================
// EXECUTION TIME TRACKING
// ============================================================================

function startTcExecutionTimer() {
  if (tcExecutionTimer) return; // Already running

  tcExecutionTimer = setInterval(() => {
    updateTcExecutionTime();
  }, 1000); // Update every second
}

function stopTcExecutionTimer() {
  if (tcExecutionTimer) {
    clearInterval(tcExecutionTimer);
    tcExecutionTimer = null;
  }
}

function updateTcExecutionTime() {
  if (tcStartTime) {
    const now = Date.now();
    const elapsed = now - tcStartTime;
    const formattedTime = formatTcExecutionTime(elapsed);

    const executionTimeElement = document.getElementById('tc-execution-time');
    if (executionTimeElement) {
      executionTimeElement.textContent = `⏱️ ${formattedTime}`;
      executionTimeElement.style.display = 'inline';
    }
  }
}

function showTcExecutionTime() {
  const executionTimeElement = document.getElementById('tc-execution-time');
  if (executionTimeElement) {
    executionTimeElement.style.display = 'inline';
  }
}

function hideTcExecutionTime() {
  const executionTimeElement = document.getElementById('tc-execution-time');
  if (executionTimeElement) {
    executionTimeElement.style.display = 'none';
  }
}

function showFinalTcExecutionTime(tc_meta) {
  if (tc_meta && tc_meta.start_time && tc_meta.end_time) {
    // Test case start/end times are local time strings - use them as-is
    const startTimeStr = tc_meta.start_time;
    const endTimeStr = tc_meta.end_time;

    const startTime = new Date(startTimeStr);
    const endTime = new Date(endTimeStr);
    const duration = endTime - startTime;

    if (duration > 0) {
      const formattedTime = formatTcExecutionTime(duration);
      const executionTimeElement = document.getElementById('tc-execution-time');
      if (executionTimeElement) {
        executionTimeElement.textContent = `⏱️ ${formattedTime}`;
        executionTimeElement.style.display = 'inline';
      }
    }
  }
}

function formatTcExecutionTime(milliseconds) {
    const totalSeconds = Math.floor(milliseconds / 1000);
    const ms = Math.floor(milliseconds % 1000);
    const seconds = totalSeconds % 60;
    const minutes = Math.floor(totalSeconds / 60) % 60;
    const hours = Math.floor(totalSeconds / 3600);
    const msStr = String(ms).padStart(3, '0');

    if (hours > 0) {
        return `${hours}h ${minutes}m ${seconds}.${msStr}s`;
    } else if (minutes > 0) {
        return `${minutes}m ${seconds}.${msStr}s`;
    }
    return `${seconds}.${msStr}s`;
}

// ============================================================================
// GLOBAL VARIABLES
// ============================================================================
// These will be initialized when templateConfig is available

let runId, testCaseId, testCaseStatus, compMap, chanList, atLookupTable;
let showDeltaTime, lastVisibleMessageTime, tcStartTime, tcExecutionTimer;
let currentFilter, sidebarCollapsed;
let atMap, lastTd, lastMsgTime, lastMsgGroup, colorIndex;

// DOM element references
let sidebarToggle, devicesSidebar, mainContent;

// Initialize the test case log when templateConfig is available
function initializeTestCaseLog() {
    // Initialize global variables
    runId = templateConfig.runId;
    testCaseId = templateConfig.testCaseId;
    testCaseStatus = templateConfig.testCaseStatus;
    compMap = new Map();
    chanList = $("#src_list");
    atLookupTable = typeof buildAtSyntaxLookupTable !== 'undefined' ? buildAtSyntaxLookupTable() : null;

    // Time display mode
    showDeltaTime = false;
    lastVisibleMessageTime = null;
    tcStartTime = null;
    tcExecutionTimer = null;

    // Initialize other global variables
    colorIndex = 0;
    atMap = new Map();
    lastTd = null;
    lastMsgTime = NaN;
    lastMsgGroup = null;



    // Filter functionality - make currentFilter accessible to processLogMessage
    currentFilter = null;

    // Sidebar toggle functionality
    sidebarCollapsed = false;
    sidebarToggle = document.getElementById('sidebarToggle');
    devicesSidebar = document.getElementById('devicesSidebar');
    mainContent = document.querySelector('.main-content');

    sidebarToggle.addEventListener('click', function() {
        sidebarCollapsed = !sidebarCollapsed;

        if (sidebarCollapsed) {
            devicesSidebar.classList.add('collapsed');
            mainContent.classList.add('sidebar-collapsed');
        } else {
            devicesSidebar.classList.remove('collapsed');
            mainContent.classList.remove('sidebar-collapsed');
        }

        // Save user preference
        saveSidebarPreference();
    });

    // Tab switching functionality
    const devicesTab = document.getElementById('devicesTab');
    const filterTab = document.getElementById('filterTab');
    const devicesContent = document.getElementById('devicesContent');
    const filterContent = document.getElementById('filterContent');

    devicesTab.addEventListener('click', function() {
        devicesTab.classList.add('active');
        filterTab.classList.remove('active');
        devicesContent.classList.add('active');
        filterContent.classList.remove('active');
    });

    filterTab.addEventListener('click', function() {
        filterTab.classList.add('active');
        devicesTab.classList.remove('active');
        filterContent.classList.add('active');
        devicesContent.classList.remove('active');
    });

    // Filter functionality
    const filterInput = document.getElementById('filterInput');
    const filterClearBtn = document.getElementById('filterClearBtn');
    const filterStatus = document.getElementById('filterStatus');
    const caseSensitive = document.getElementById('caseSensitive');
    const fullWord = document.getElementById('fullWord');
    const useWildcards = document.getElementById('useWildcards');
    const filterModeRadios = document.querySelectorAll('input[name="filterMode"]');

    function createFilterRegex(text, caseSensitive, fullWord, useWildcards) {
        if (!text.trim()) return null;

        let pattern = text;

        if (useWildcards) {
            // Convert wildcards to regex
            pattern = pattern.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); // Escape special regex chars
            pattern = pattern.replace(/\\\*/g, '.*'); // * becomes .*
            pattern = pattern.replace(/\\\?/g, '.');  // ? becomes .
        } else {
            // Escape special regex characters
            pattern = pattern.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        }

        if (fullWord) {
            pattern = '\\b' + pattern + '\\b';
        }

        const flags = caseSensitive ? 'g' : 'gi';
        return new RegExp(pattern, flags);
    }

    function applyFilter() {
        const filterText = filterInput.value.trim();
        const isCaseSensitive = caseSensitive.checked;
        const isFullWord = fullWord.checked;
        const isWildcards = useWildcards.checked;
        const mode = document.querySelector('input[name="filterMode"]:checked').value;

        if (!filterText) {
            currentFilter = null;
            filterStatus.textContent = 'No filter applied';
            filterStatus.className = 'filter-status';
            showAllMessages();
            return;
        }

        currentFilter = {
            regex: createFilterRegex(filterText, isCaseSensitive, isFullWord, isWildcards),
            mode: mode
        };

        if (currentFilter.regex) {
            reapplyFilterToAllMessages();
        }
    }

    function reapplyFilterToAllMessages() {
        if (!currentFilter) {
            showAllMessages();
            return;
        }

        const messageRows = document.querySelectorAll('#msg_table tbody tr.log-entry-row');
        let visibleCount = 0;
        let totalCount = messageRows.length;

        messageRows.forEach((row, index) => {
            // Get the original message text from the data attribute or reconstruct it
            let messageText = '';
            const messageCell = row.querySelector('.log-message');
            if (messageCell) {
                messageText = messageCell.textContent || messageCell.innerText;
            }

            // Reset regex lastIndex to fix the regex reuse bug
            currentFilter.regex.lastIndex = 0;

            const matches = currentFilter.regex.test(messageText);
            const shouldShow = currentFilter.mode === 'include' ? matches : !matches;

            if (shouldShow) {
                row.style.display = '';
                visibleCount++;
            } else {
                row.style.display = 'none';
            }
        });

        const modeText = currentFilter.mode === 'include' ? 'Including' : 'Excluding';
        const filterText = filterInput.value;
        filterStatus.textContent = `${modeText}: "${filterText}" (${visibleCount}/${totalCount} messages)`;
        filterStatus.className = `filter-status ${currentFilter.mode === 'exclude' ? 'exclude' : 'active'}`;
        refreshCommentThreadVisibility();
    }

    function filterMessages() {
        if (!currentFilter) return;

        const messageRows = document.querySelectorAll('#msg_table tbody tr.log-entry-row');
        let visibleCount = 0;
        let totalCount = messageRows.length;

        messageRows.forEach(row => {
            const messageCell = row.querySelector('.log-message');
            const messageText = messageCell ? messageCell.textContent : '';
            const matches = currentFilter.regex.test(messageText);
            const shouldShow = currentFilter.mode === 'include' ? matches : !matches;

            if (shouldShow) {
                row.style.display = '';
                visibleCount++;
            } else {
                row.style.display = 'none';
            }
        });

        refreshCommentThreadVisibility();
        return { visible: visibleCount, total: totalCount };
    }

    function showAllMessages() {
        const messageRows = document.querySelectorAll('#msg_table tbody tr.log-entry-row');
        messageRows.forEach(row => {
            row.style.display = '';
        });
        refreshCommentThreadVisibility();
    }

    function updateFilterStatus() {
        if (!currentFilter) return;

        const stats = filterMessages();
        const modeText = currentFilter.mode === 'include' ? 'Including' : 'Excluding';
        const filterText = filterInput.value;

        filterStatus.textContent = `${modeText}: "${filterText}" (${stats.visible}/${stats.total} messages)`;
        filterStatus.className = `filter-status ${currentFilter.mode === 'exclude' ? 'exclude' : 'active'}`;
    }

    // Event listeners for filter controls
    filterInput.addEventListener('input', applyFilter);
    filterClearBtn.addEventListener('click', function() {
        filterInput.value = '';
        applyFilter();
    });


    caseSensitive.addEventListener('change', applyFilter);
    fullWord.addEventListener('change', applyFilter);
    useWildcards.addEventListener('change', applyFilter);

    filterModeRadios.forEach(radio => {
        radio.addEventListener('change', applyFilter);
    });



    // Helper functions
    function selectColor(index) {
        const colors = [
            "#FF6B6B", "#4ECDC4", "#45B7D1", "#96CEB4", "#FFEAA7",
            "#DDA0DD", "#98D8C8", "#F7DC6F", "#BB8FCE", "#85C1E9"
        ];
        return colors[index % colors.length];
    }


    // Status updates
    const ws = templateConfig.liveRun ? new WebSocket(`ws://${location.host}/ws/ui`) : null;

    // Add live indicator when WebSocket connects


    if (templateConfig.liveRun && ws) {
        ws.binaryType = 'arraybuffer';

        ws.onopen = () => {
            // Add live indicator if test case is still running
            if (testCaseStatus === 'running' || testCaseStatus === 'unknown') {
                addLiveIndicator();
            }
        };

        ws.onclose = () => {
            removeLiveIndicator();
        };

        ws.onmessage = (event) => {
            try {
                // Decode MessagePack binary data
                const msg = msgpack.decode(new Uint8Array(event.data));
                // Stack traces are delivered via the per-test-case /ws/logs socket (not /ws/ui).
                if ((msg.type === 'test_case_started' || msg.type === 'test_case_finished' || msg.type === 'test_case_updated') && msg.run_id === runId && msg.test_case_id === testCaseId) {
                    if (msg.tc_meta) {
                        if (msg.tc_meta.status) {
                            // Update status badge
                            const statusBadge = document.getElementById('tc-status-badge');
                            if (statusBadge) {
                                const status = msg.tc_meta.status.toLowerCase();
                                let statusClass = 'status-unknown';
                                let displayText = status.toUpperCase();

                                if (status === 'running') {
                                    statusClass = 'status-running';
                                    displayText = 'Running';
                                } else if (status === 'passed') {
                                    statusClass = 'status-passed';
                                    displayText = 'PASSED';
                                } else if (status === 'failed') {
                                    statusClass = 'status-failed';
                                    displayText = 'FAILED';
                                } else if (status === 'skipped') {
                                    statusClass = 'status-skipped';
                                    displayText = 'SKIPPED';
                                } else if (status === 'aborted') {
                                    statusClass = 'status-failed';
                                    displayText = 'ABORTED';
                                } else {
                                    statusClass = 'status-unknown';
                                    displayText = 'UNKNOWN';
                                }

                                statusBadge.className = `status-badge ${statusClass}`;
                                statusBadge.textContent = displayText;

                                // Add or remove spinner for running status
                                const existingSpinner = statusBadge.querySelector('.spinner');
                                if (status === 'running') {
                                    if (!existingSpinner) {
                                        const spinner = document.createElement('span');
                                        spinner.className = 'spinner';
                                        statusBadge.appendChild(spinner);
                                    }
                                    // Start real-time execution time tracking
                                    tcStartTime = Date.now();
                                    showTcExecutionTime();
                                    startTcExecutionTimer();
                                    // Add live indicator when test case is running
                                    addLiveIndicator();
                                } else {
                                    if (existingSpinner) {
                                        existingSpinner.remove();
                                    }
                                    // Stop real-time execution time tracking and show final time
                                    stopTcExecutionTimer();
                                    showFinalTcExecutionTime(msg.tc_meta);
                                    // Remove live indicator when test case finishes
                                    removeLiveIndicator();
                                }
                            }

                            // Calculate and display execution time if test case has finished
                            if (status !== 'running' && msg.tc_meta.start_time && msg.tc_meta.end_time) {
                                // Ensure both times are in proper ISO format
                                let startTimeStr = msg.tc_meta.start_time;
                                let endTimeStr = msg.tc_meta.end_time;

                                // Add 'Z' to endTime if it doesn't have timezone info
                                if (!endTimeStr.includes('Z') && !endTimeStr.includes('+') && !endTimeStr.includes('-', 10)) {
                                    endTimeStr += 'Z';
                                }

                                const startTime = new Date(startTimeStr);
                                const endTime = new Date(endTimeStr);
                                const duration = endTime - startTime;

                                if (duration > 0) {
                                    let executionTimeText = '';
                                    if (duration < 1000) {
                                        executionTimeText = `⏱️ ${duration}ms`;
                                    } else if (duration < 60000) {
                                        executionTimeText = `⏱️ ${(duration / 1000).toFixed(2)}s`;
                                    } else {
                                        const minutes = Math.floor(duration / 60000);
                                        const seconds = ((duration % 60000) / 1000).toFixed(1);
                                        executionTimeText = `⏱️ ${minutes}m ${seconds}s`;
                                    }

                                    const executionTimeElement = document.getElementById('tc-execution-time');
                                    if (executionTimeElement) {
                                        executionTimeElement.textContent = executionTimeText;
                                        executionTimeElement.style.display = 'inline';
                                    }
                                }
                            }
                        }

                        // Update end time if available
                        if (msg.tc_meta && msg.tc_meta.end_time) {
                            const infoItems = document.querySelectorAll('.info-item');
                            infoItems.forEach(item => {
                                const strong = item.querySelector('strong');
                                if (strong && strong.textContent === 'End Time') {
                                    const valueElement = strong.nextSibling;
                                    if (valueElement) {
                                        valueElement.textContent = ' ' + convertToLocalTime(msg.tc_meta.end_time);
                                    }
                                }
                            });
                        }
                    }
                }
            } catch(e) { console.error('WS message parse error', e); }
        };
    }


    // For live runs, all logs come via WebSocket (including existing ones)
    // For non-live runs, logs are embedded in HTML
    console.log('Template config for live run detection:', {
        liveRun: templateConfig.liveRun,
        testCaseStatus: templateConfig.testCaseStatus,
        runId: templateConfig.runId,
        testCaseId: templateConfig.testCaseId
    });

    if (templateConfig.liveRun) {
        console.log('Establishing live log WebSocket connection...');
        const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${scheme}//${location.host}/ws/logs/${runId}/${testCaseId}`;
        console.log('WebSocket URL:', wsUrl);
        const wsLogs = new WebSocket(wsUrl);
        wsLogs.binaryType = 'arraybuffer';

        wsLogs.onopen = () => {
            console.log('Live log WebSocket connected successfully');
        };

        wsLogs.onerror = (error) => {
            console.error('Live log WebSocket error:', error);
        };

        wsLogs.onclose = (event) => {
            console.log('Live log WebSocket closed:', event.code, event.reason);
        };

        wsLogs.onmessage = (e) => {
            try {
                // Decode MessagePack binary data
                const raw = msgpack.decode(new Uint8Array(e.data));

                // Use protocol decoder if available for compact protocol
                const d = protocolDecoder ? protocolDecoder.decodeMessage(raw) : raw;

                if (d.type === 'error') { console.warn('Log WS error:', d.message); return; }

                if (d.type === 'exception') {
                    handleIncomingStackTrace({
                        timestamp: d.timestamp,
                        message: d.message,
                        exception_type: d.exception_type,
                        stack_trace: d.stack_trace
                    });
                    return;
                }

                // Handle log batch (multiple entries)
                if (d.type === 'log_batch' && Array.isArray(d.entries)) {
                    d.entries.forEach(entry => {
                        if (entry && entry.timestamp) {
                            processLogMessage(entry, compMap, chanList, atLookupTable);
                        }
                    });
                    chanList.empty();
                    updateChannelList(compMap, chanList);
                    return;
                }

                // Handle individual log entry (compact format - decode it)
                const entry = protocolDecoder ? protocolDecoder.decodeLogEntry(raw) : d;
                if (entry && entry.timestamp) {
                    processLogMessage(entry, compMap, chanList, atLookupTable);
                    chanList.empty();
                    updateChannelList(compMap, chanList);
                }

            } catch(err) { console.error('Error processing log entry:', err); }
        };
    }

    // Load initial logs only for non-live runs (live runs get all logs via WebSocket)
    if (!templateConfig.liveRun) {
        const initialLogs = templateConfig.initialLogs;
        const initialTraces = templateConfig.stackTraces || [];

        // Clear the table first to avoid duplication
        $("#msg_table tbody").empty();
        window.__logLineIndex = 0;

        const merged = [];

        // Normal logs
        (Array.isArray(initialLogs) ? initialLogs : []).forEach(logEntry => {
            merged.push({
                timestamp: logEntry.timestamp, // Keep original timestamp for delta calculation
                message: logEntry.message,
                component: logEntry.component || '',
                channel: logEntry.channel || '',
                dir: logEntry.dir || null,
                phase: logEntry.phase || null,
                kind: null
            });
        });

        // Stack traces as message-log entries (same layout as the stack trace cards, minus timestamp)
        (Array.isArray(initialTraces) ? initialTraces : []).forEach(trace => {
            merged.push({
                timestamp: trace.timestamp || '',
                message: createInlineStackTraceHtml(trace),
                component: 'NUnit',
                channel: 'Exception',
                dir: null,
                phase: null,
                kind: 'exception'
            });
        });

        // Sort by timestamp so exceptions appear in the right place in the log table
        merged.sort((a, b) => {
            try { return new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime(); } catch { return 0; }
        });

        merged.forEach(entry => processLogMessage(entry, compMap, chanList, atLookupTable));
    } else {
        // For live runs, clear the table since WebSocket will populate it
        $("#msg_table tbody").empty();
        window.__logLineIndex = 0;
    }




    // Load preferences and apply them
    loadTimeDisplayPreference();
    loadSidebarPreference();
    applyTimeDisplayPreference();
    applySidebarPreference();

    // Convert times to local time on page load
    convertTimesToLocal();

    if (window.CommentsUI && window.CommentsUI.Log) {
        const openFirst = /[?&]openFirst=1/.test(window.location.search) || window.location.hash.indexOf("comment=") !== -1;
        window.CommentsUI.Log.init({
            runId: templateConfig.runId,
            tcId: templateConfig.testCaseId,
            authEnabled: !!templateConfig.authEnabled,
            currentUser: templateConfig.currentUser || null,
            openFirst: openFirst,
            readOnly: !templateConfig.serverMode,
            embeddedComments: templateConfig.comments || []
        }).catch(function (err) {
            console.error("Failed to load comments", err);
        });
    }

    // Calculate and display execution time on page load if test case has finished
    function calculateInitialExecutionTime() {
        const tcStatus = templateConfig.testCaseStatus;
        const tcStartTime = templateConfig.testCaseStartTime;
        const tcEndTime = templateConfig.testCaseEndTime;

        if (tcStatus && tcStatus.toLowerCase() !== 'running' && tcStartTime && tcEndTime) {
            // Test case start/end times are local time strings - use them as-is
            const startTimeStr = tcStartTime;
            const endTimeStr = tcEndTime;

            const startTime = new Date(startTimeStr);
            const endTime = new Date(endTimeStr);
            const duration = endTime - startTime;

            if (duration > 0) {
                let executionTimeText = '';
                if (duration < 1000) {
                    executionTimeText = `⏱️ ${duration}ms`;
                } else if (duration < 60000) {
                    executionTimeText = `⏱️ ${(duration / 1000).toFixed(2)}s`;
                } else {
                    const minutes = Math.floor(duration / 60000);
                    const seconds = ((duration % 60000) / 1000).toFixed(1);
                    executionTimeText = `⏱️ ${minutes}m ${seconds}s`;
                }

                const executionTimeElement = document.getElementById('tc-execution-time');
                if (executionTimeElement) {
                    executionTimeElement.textContent = executionTimeText;
                    executionTimeElement.style.display = 'inline';
                }
            }
        }
    }

    function convertTcTimestampsToLocal() {
        // Display test case start time (from testCaseStartTime, not run start time)
        const startTimeDisplay = document.getElementById('tc-start-time-display');
        if (startTimeDisplay) {
            const tcStartTimeStr = templateConfig.testCaseStartTime;
            if (tcStartTimeStr) {
                startTimeDisplay.textContent = convertToLocalTime(tcStartTimeStr);
            }
        }
    }

    function displayRetentionNotice() {
        const retentionNotice = document.getElementById('retention-notice');
        if (!retentionNotice) return;

        const retentionDays = templateConfig.retentionDays;
        const runStartTime = templateConfig.runStartTime;

        if (!retentionDays || retentionDays <= 0 || !runStartTime) {
            retentionNotice.style.display = 'none';
            return;
        }

        const runStart = new Date(runStartTime);
        const expirationDate = new Date(runStart.getTime() + retentionDays * 24 * 60 * 60 * 1000);
        const now = new Date();
        const daysRemaining = Math.ceil((expirationDate - now) / (24 * 60 * 60 * 1000));

        if (daysRemaining <= 0) {
            retentionNotice.textContent = '⚠️ This log is scheduled for deletion';
            retentionNotice.style.display = 'block';
        } else if (daysRemaining === 1) {
            retentionNotice.textContent = '⏳ 1 day remaining until deletion';
            retentionNotice.style.display = 'block';
        } else {
            retentionNotice.textContent = `⏳ ${daysRemaining} days remaining until deletion`;
            retentionNotice.style.display = 'block';
        }
    }


    calculateInitialExecutionTime();

    // Convert timestamps to local time and display retention notice
    convertTcTimestampsToLocal();
    displayRetentionNotice();

    // Add live indicator if test case is running
    if (testCaseStatus === 'running' || testCaseStatus === 'unknown') {
        addLiveIndicator();

        // Start real-time execution time tracking if test is running
        if (testCaseStatus === 'running') {
            showTcExecutionTime();
            const tcStartTimeStr = templateConfig.testCaseStartTime;
            if (tcStartTimeStr) {
                tcStartTime = new Date(tcStartTimeStr).getTime();
                startTcExecutionTimer();
            } else {
                // If no start time available, use current time
                tcStartTime = Date.now();
                startTcExecutionTimer();
            }
        }
    }

    // Add event listener for time display toggle
    document.getElementById('time-display-toggle').addEventListener('click', toggleTimeDisplay);

    // Update source list after loading initial logs
    chanList.empty();
    updateChannelList(compMap, chanList);

    // Update status badge
    updateStatusBadge();
}

// ============================================================================
// ATTACHMENT MANAGEMENT
// ============================================================================

function formatFileSize(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function formatFileIcon(filename) {
    const ext = filename.split('.').pop().toLowerCase();
    const iconMap = {
        'pdf': '📄',
        'doc': '📄', 'docx': '📄',
        'txt': '📝', 'log': '📝',
        'jpg': '🖼️', 'jpeg': '🖼️', 'png': '🖼️', 'gif': '🖼️', 'bmp': '🖼️',
        'mp4': '🎥', 'avi': '🎥', 'mov': '🎥',
        'mp3': '🎵', 'wav': '🎵',
        'zip': '📦', 'rar': '📦', '7z': '📦',
        'xls': '📊', 'xlsx': '📊', 'csv': '📊',
        'xml': '📋', 'json': '📋',
        'html': '🌐', 'htm': '🌐',
        'exe': '⚙️', 'msi': '⚙️',
        'dll': '🔧', 'so': '🔧'
    };
    return iconMap[ext] || '📎';
}

function renderAttachmentItem(attachment) {
    const icon = formatFileIcon(attachment.filename);
    const size = formatFileSize(attachment.size);
    const runId = templateConfig.runId;
    const testCaseId = templateConfig.testCaseId;
    const downloadUrl = `/api/attachments/${runId}/${testCaseId}/download/${encodeURIComponent(attachment.filename)}`;

    return `
        <a href="${downloadUrl}" class="attachment-item" data-filename="${attachment.filename}" title="Click to download ${attachment.filename}">
            <div class="attachment-icon">${icon}</div>
            <div class="attachment-name">${attachment.filename}</div>
            <div class="attachment-size">(${size})</div>
        </a>
    `;
}

function setAttachmentsContent(html, visible) {
    const attachmentsList = document.getElementById('attachmentsList');
    const attachmentsBlock = document.getElementById('attachmentsBlock');
    if (attachmentsList) {
        attachmentsList.innerHTML = html || '';
    }
    if (attachmentsBlock) {
        attachmentsBlock.hidden = !visible;
    }
}

function loadAttachments() {
    if (!templateConfig) {
        console.error('templateConfig not available');
        return;
    }

    // In offline mode (zip file), use embedded attachment data
    if (!templateConfig.serverMode) {
        loadOfflineAttachments();
        return;
    }

    const runId = templateConfig.runId;
    const testCaseId = templateConfig.testCaseId;

    fetch(`/api/attachments/${runId}/${testCaseId}/list`)
        .then(response => response.json())
        .then(data => {
            if (data.attachments && data.attachments.length > 0) {
                setAttachmentsContent(data.attachments.map(renderAttachmentItem).join(''), true);
            } else {
                setAttachmentsContent('', false);
            }
        })
        .catch(error => {
            console.error('Error loading attachments:', error);
            setAttachmentsContent('<div class="no-attachments">Error loading attachments.</div>', true);
        });
}

function loadOfflineAttachments() {
    if (templateConfig.attachments && templateConfig.attachments.length > 0) {
        setAttachmentsContent(templateConfig.attachments.map(renderOfflineAttachmentItem).join(''), true);
    } else {
        setAttachmentsContent('', false);
    }
}

function renderOfflineAttachmentItem(attachment) {
    const icon = formatFileIcon(attachment.filename);
    const size = formatFileSize(attachment.size);
    const testCaseId = templateConfig.testCaseId;
    // Create relative path to attachment in zip file
    const downloadUrl = `../attachments/${testCaseId}/${encodeURIComponent(attachment.filename)}`;

    return `
        <a href="${downloadUrl}" class="attachment-item" data-filename="${attachment.filename}" title="Click to download ${attachment.filename}">
            <div class="attachment-icon">${icon}</div>
            <div class="attachment-name">${attachment.filename}</div>
            <div class="attachment-size">(${size})</div>
        </a>
    `;
}

// downloadAttachment function removed - now using direct links

// Removed toggleAttachments function - attachments are now always visible in test details

function initializeAttachments() {
    // Automatically load attachments when page loads
    loadAttachments();
}

// ============================================================================
// STACK TRACE MANAGEMENT
// ============================================================================

const stackTraceList = document.getElementById('stackTraceList');
const stackTraceEmptyState = document.getElementById('stackTraceEmptyState');

function refreshStackTraceEmptyState() {
    const hasItems = stackTraceList && stackTraceList.children.length > 0;
    const section = document.getElementById('stackTraceSection');
    if (section) {
        section.hidden = !hasItems;
    }
    if (stackTraceEmptyState) {
        stackTraceEmptyState.style.display = 'none';
    }
}

function createStackTraceCard(trace, includeTimestamp = true) {
    const card = document.createElement('div');
    card.className = 'stack-trace-card';

    const header = document.createElement('div');
    header.className = 'stack-trace-card-header';

    const title = document.createElement('div');
    title.className = 'stack-trace-title';
    const isError = !!trace.is_error;
    if (isError) {
        title.textContent = trace.exception_type || 'Error';
    } else {
        title.textContent = trace.exception_type || 'Failure';
    }

    header.appendChild(title);
    if (includeTimestamp) {
        const timestamp = document.createElement('div');
        timestamp.className = 'stack-trace-timestamp';
        timestamp.textContent = convertToLocalTime(trace.timestamp || '');
        header.appendChild(timestamp);
    }
    card.appendChild(header);

    if (trace.message) {
        const messageEl = document.createElement('div');
        messageEl.className = 'stack-trace-message';
        messageEl.textContent = trace.message;
        card.appendChild(messageEl);
    }

    const pre = document.createElement('pre');
    pre.className = 'stack-trace-body';
    const code = document.createElement('code');

    // stack_trace is expected to be a list of strings (one per stack frame/line)
    let stackText = '';
    if (Array.isArray(trace.stack_trace)) {
        stackText = trace.stack_trace.join('\n');
    } else if (typeof trace.stack_trace === 'string' && trace.stack_trace.trim().length > 0) {
        stackText = trace.stack_trace;
    }

    code.textContent = stackText || 'No exception details provided.';
    pre.appendChild(code);
    card.appendChild(pre);

    return card;
}

function createInlineStackTraceHtml(trace) {
    // Same layout as the stack trace cards, except WITHOUT the timestamp (table already has a time column).
    const card = createStackTraceCard(trace, false);
    card.classList.add('inline-in-log');
    return card.outerHTML;
}

function appendStackTrace(trace, suppressRefresh = false) {
    if (!stackTraceList || !trace) return;
    const card = createStackTraceCard(trace, true);
    stackTraceList.appendChild(card);
    if (!suppressRefresh) {
        refreshStackTraceEmptyState();
    }
}

function initializeStackTraces(initialTraces) {
    if (!stackTraceList) return;
    stackTraceList.innerHTML = '';

    if (Array.isArray(initialTraces)) {
        initialTraces.forEach(trace => appendStackTrace(trace, true));
    }

    refreshStackTraceEmptyState();
}

function handleIncomingStackTrace(trace) {
    appendStackTrace(trace);

    // Also render stack traces into the main message log table.
    // Important: do NOT include timestamps inside the message body (time column already exists).
    try {
        const processedLog = {
            timestamp: trace.timestamp || '',
            message: createInlineStackTraceHtml(trace),
            component: 'NUnit',
            channel: 'Exception',
            dir: null,
            phase: null,
            kind: 'exception'
        };

        processLogMessage(processedLog, compMap, chanList, atLookupTable);
        chanList.empty();
        updateChannelList(compMap, chanList);
    } catch (e) {
        console.warn('Failed to add stack trace to message log:', e);
    }
}

function initializeStackTraceSection() {
    const traces = templateConfig.stackTraces || [];
    initializeStackTraces(traces);
}

function initializeMetricsSummary() {
    const metrics = templateConfig.metrics || [];
    const tcStartTime = templateConfig.tcStartTime;
    const tcEndTime = templateConfig.tcEndTime;

    if (!metrics || metrics.length === 0) {
        return; // No metrics to show
    }

    // Keep a little context around the TC so start/stop marks are not
    // glued to the chart edges. Stats still use only the TC window.
    let chartMetrics = metrics;
    let statMetrics = metrics;
    if (tcStartTime && tcEndTime) {
        const startMs = new Date(tcStartTime).getTime();
        const endMs = new Date(tcEndTime).getTime();
        const duration = Math.max(endMs - startMs, 1);
        const pad = Math.min(Math.max(duration * 0.15, 2000), 30000);
        statMetrics = metrics.filter(m => {
            const ts = m.ts || 0;
            return ts >= startMs && ts <= endMs;
        });
        chartMetrics = metrics.filter(m => {
            const ts = m.ts || 0;
            return ts >= startMs - pad && ts <= endMs + pad;
        });
    }

    if (statMetrics.length === 0) {
        return; // No metrics during this test case
    }
    if (chartMetrics.length === 0) {
        chartMetrics = statMetrics;
    }

    // Calculate statistics
    const cpuValues = statMetrics.map(m => m.cpu || 0);
    const memValues = statMetrics.map(m => m.mem || 0);
    const netValues = statMetrics.map(m => m.net || 0);

    const cpuMin = Math.min(...cpuValues);
    const cpuMax = Math.max(...cpuValues);
    const cpuAvg = cpuValues.reduce((a, b) => a + b, 0) / cpuValues.length;

    const memMin = Math.min(...memValues);
    const memMax = Math.max(...memValues);
    const memAvg = memValues.reduce((a, b) => a + b, 0) / memValues.length;

    const netMin = Math.min(...netValues);
    const netMax = Math.max(...netValues);
    const netAvg = netValues.reduce((a, b) => a + b, 0) / netValues.length;

    // Update the stats text
    document.getElementById('tc-cpu-min').textContent = cpuMin.toFixed(1) + '%';
    document.getElementById('tc-cpu-avg').textContent = cpuAvg.toFixed(1) + '%';
    document.getElementById('tc-cpu-max').textContent = cpuMax.toFixed(1) + '%';
    document.getElementById('tc-mem-min').textContent = memMin.toFixed(1) + '%';
    document.getElementById('tc-mem-avg').textContent = memAvg.toFixed(1) + '%';
    document.getElementById('tc-mem-max').textContent = memMax.toFixed(1) + '%';
    document.getElementById('tc-net-min').textContent = netMin.toFixed(1) + '%';
    document.getElementById('tc-net-avg').textContent = netAvg.toFixed(1) + '%';
    document.getElementById('tc-net-max').textContent = netMax.toFixed(1) + '%';

    // Render the chart
    renderTcMetricsChart(chartMetrics);

    // Show the section
    document.getElementById('metricsSummarySection').style.display = 'block';
}

let tcMetricsResizeTimer = null;
let tcMetricsResizeObserver = null;
let tcMetricsChartDrawnWidth = 0;

function tcMetricsChartLayoutWidth(canvas) {
    const container = canvas && canvas.closest('.metrics-chart-container');
    if (!container) return 0;
    const cs = window.getComputedStyle(container);
    return Math.max(0, Math.floor(
        container.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0)
    ));
}

function scheduleTcMetricsChartResize() {
    if (tcMetricsResizeTimer) {
        clearTimeout(tcMetricsResizeTimer);
    }
    tcMetricsResizeTimer = setTimeout(function () {
        tcMetricsResizeTimer = null;
        if (tcMetricsDataRef && tcMetricsDataRef.length) {
            renderTcMetricsChart(tcMetricsDataRef);
        }
    }, 50);
}

function initializeTcMetricsChartResize(canvas) {
    if (!canvas) return;
    if (!tcMetricsResizeObserver && typeof ResizeObserver !== 'undefined') {
        const container = canvas.closest('.metrics-chart-container');
        if (container) {
            tcMetricsResizeObserver = new ResizeObserver(scheduleTcMetricsChartResize);
            tcMetricsResizeObserver.observe(container);
        }
    }
    if (!canvas.dataset.resizeBound) {
        canvas.dataset.resizeBound = '1';
        window.addEventListener('resize', scheduleTcMetricsChartResize);
    }
}

function renderTcMetricsChart(metricsData) {
    const canvas = document.getElementById('tc-metrics-chart');
    if (!canvas || !metricsData || metricsData.length === 0) return;

    const section = document.getElementById('metricsSummarySection');
    section.style.display = 'block';

    const ctx = canvas.getContext('2d');
    let width = tcMetricsChartLayoutWidth(canvas);
    if (width <= 0) {
        if (!canvas.dataset.metricsRetry) {
            canvas.dataset.metricsRetry = '1';
            requestAnimationFrame(function () {
                canvas.dataset.metricsRetry = '';
                if (metricsData && metricsData.length) renderTcMetricsChart(metricsData);
            });
        }
        return;
    }
    tcMetricsChartDrawnWidth = width;
    const height = 100;

    // Set canvas size with device pixel ratio for sharpness
    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.height = height + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // Clear canvas
    ctx.clearRect(0, 0, width, height);

    // Plot 0-100% above a separate test-activity strip
    const padding = { top: 5, right: 5, bottom: 5, left: 5, gap: 6, band: 8 };
    const chartWidth = width - padding.left - padding.right;
    const chartHeight = height - padding.top - padding.bottom - padding.gap - padding.band;

    // Y axis: 0-100%
    const yScale = (v) => padding.top + chartHeight - (v / 100) * chartHeight;
    const xScale = (i) => padding.left + (i / (metricsData.length - 1 || 1)) * chartWidth;

    // Draw background grid lines
    ctx.strokeStyle = '#c5ccd4';
    ctx.lineWidth = 0.5;
    for (let p = 0; p <= 100; p += 25) {
        const y = yScale(p);
        ctx.beginPath();
        ctx.moveTo(padding.left, y);
        ctx.lineTo(width - padding.right, y);
        ctx.stroke();
    }

    // Draw System CPU line
    ctx.strokeStyle = '#667eea';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    metricsData.forEach((m, i) => {
        const x = xScale(i);
        const y = yScale(m.cpu || 0);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    });
    ctx.stroke();

    // Draw Memory line
    ctx.strokeStyle = '#28a745';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    metricsData.forEach((m, i) => {
        const x = xScale(i);
        const y = yScale(m.mem || 0);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    });
    ctx.stroke();

    // Draw Network line
    ctx.strokeStyle = '#e67e22';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    metricsData.forEach((m, i) => {
        const x = xScale(i);
        const y = yScale(m.net || 0);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    });
    ctx.stroke();

    drawTcMetricsTestActivity(ctx, padding, chartWidth, padding.top + chartHeight, metricsData);

    // Initialize hover tooltip for this chart
    tcMetricsDataRef = metricsData;
    window.tcMetricsDataRef = metricsData;
    initializeTcMetricsChartHover(metricsData);
    initializeTcMetricsChartResize(canvas);
}

function collectTcMetricsInterval(metricsData) {
    const cfg = typeof templateConfig !== 'undefined' ? templateConfig : (window.templateConfig || {});
    const startRaw = cfg.tcStartTime || cfg.testCaseStartTime;
    const endRaw = cfg.tcEndTime || cfg.testCaseEndTime;
    let start = startRaw ? new Date(startRaw).getTime() : NaN;
    let end = endRaw ? new Date(endRaw).getTime() : NaN;
    if (!Number.isFinite(start) && metricsData && metricsData.length) {
        start = metricsData[0].ts;
    }
    if (!Number.isFinite(end) && metricsData && metricsData.length) {
        end = metricsData[metricsData.length - 1].ts;
    }
    if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return [];
    return [{ start: start, end: end }];
}

function drawTcMetricsTestActivity(ctx, padding, chartWidth, plotBottom, metricsData) {
    const samples = metricsData;
    if (!samples || samples.length === 0) return;
    const t0 = samples[0].ts;
    const t1 = samples[samples.length - 1].ts;
    const span = t1 - t0;
    if (!Number.isFinite(t0) || span <= 0) return;

    const intervals = collectTcMetricsInterval(samples);
    if (!intervals.length) return;

    const cols = Math.max(1, Math.floor(chartWidth));
    const counts = new Uint16Array(cols);
    for (let column = 0; column < cols; column++) {
        const columnStart = t0 + (column / cols) * span;
        const columnEnd = t0 + ((column + 1) / cols) * span;
        let c = 0;
        for (let i = 0; i < intervals.length; i++) {
            if (intervals[i].end >= columnStart && intervals[i].start <= columnEnd) c++;
        }
        counts[column] = c;
    }

    const bandTop = plotBottom + padding.gap;
    const bandH = padding.band;
    ctx.strokeStyle = '#d8dee6';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padding.left, plotBottom + padding.gap / 2);
    ctx.lineTo(padding.left + chartWidth, plotBottom + padding.gap / 2);
    ctx.stroke();

    for (let i = 0; i < cols; i++) {
        const c = counts[i];
        if (!c) continue;
        ctx.fillStyle = 'rgba(100, 116, 139, 0.45)';
        ctx.fillRect(padding.left + i, bandTop, 1, bandH);
    }

    drawTcMetricsBoundaryMarks(ctx, padding, chartWidth, t0, span, intervals, bandTop, bandH);
}

function drawTcMetricsBoundaryMarks(ctx, padding, chartWidth, t0, span, intervals, bandTop, bandH) {
    if (!intervals || !intervals.length || span <= 0) return;
    const columns = Math.max(1, Math.floor(chartWidth));
    const dark = document.documentElement.classList.contains('dark-theme');
    const startColor = dark ? '#e2e8f0' : '#1e293b';
    const endColor = dark ? '#cbd5e1' : '#334155';
    const interval = intervals[0];
    const startColumn = Math.floor(((interval.start - t0) / span) * columns);
    const endColumn = Math.floor(((interval.end - t0) / span) * columns);
    if (startColumn >= 0 && startColumn < columns) {
        ctx.fillStyle = startColor;
        ctx.fillRect(padding.left + startColumn, bandTop, 1, 2);
    }
    if (endColumn >= 0 && endColumn < columns) {
        ctx.fillStyle = endColor;
        ctx.fillRect(padding.left + endColumn, bandTop + bandH - 2, 1, 2);
    }
}

// TC metrics chart hover tooltip
let tcMetricsTooltipInitialized = false;
let tcMetricsDataRef = null;

function initializeTcMetricsChartHover(metricsData) {
    tcMetricsDataRef = metricsData;
    window.tcMetricsDataRef = metricsData;
    if (tcMetricsTooltipInitialized) return;
    tcMetricsTooltipInitialized = true;

    const canvas = document.getElementById('tc-metrics-chart');
    const tooltip = document.getElementById('tc-metrics-tooltip');
    if (!canvas || !tooltip) return;

    const padding = { top: 5, right: 5, bottom: 5, left: 5, gap: 6, band: 8 };

    canvas.addEventListener('mousemove', function(e) {
        if (!tcMetricsDataRef || tcMetricsDataRef.length === 0) {
            tooltip.style.display = 'none';
            return;
        }

        const rect = canvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const chartWidth = rect.width - padding.left - padding.right;

        // Determine which sample index we're hovering over
        const ratio = (x - padding.left) / chartWidth;
        const sampleIndex = Math.round(ratio * (tcMetricsDataRef.length - 1));
        if (sampleIndex < 0 || sampleIndex >= tcMetricsDataRef.length) {
            tooltip.style.display = 'none';
            return;
        }

        const sample = tcMetricsDataRef[sampleIndex];
        const sampleTs = sample.ts;

        // Build tooltip content
        const time = new Date(sampleTs).toLocaleTimeString();
        let html = `<div style="margin-bottom: 4px;"><strong>${time}</strong></div>`;
        html += `<div>CPU: <span style="color: #a3b3f4">${(sample.cpu || 0).toFixed(1)}%</span></div>`;
        html += `<div>Memory: <span style="color: #68d391">${(sample.mem || 0).toFixed(1)}%</span></div>`;
        html += `<div>Network: <span style="color: #f5a860">${(sample.net || 0).toFixed(1)}%</span>`;
        // Show per-interface details if available
        if (sample.ni && sample.ni.length > 0) {
            html += ` <span style="color: #888; font-size: 11px;">(`;
            html += sample.ni.map(iface => `${iface.n}: ↑${(iface.tx || 0).toFixed(1)}% ↓${(iface.rx || 0).toFixed(1)}%`).join(', ');
            html += `)</span>`;
        }
        html += `</div>`;

        tooltip.innerHTML = html;
        tooltip.style.display = 'block';

        // Position tooltip
        const containerRect = canvas.closest('.metrics-chart-container').getBoundingClientRect();
        let tooltipX = e.clientX - containerRect.left + 12;
        let tooltipY = 5;

        // Keep tooltip within container bounds
        if (tooltipX + tooltip.offsetWidth > containerRect.width - 5) {
            tooltipX = e.clientX - containerRect.left - tooltip.offsetWidth - 12;
        }
        if (tooltipX < 5) tooltipX = 5;

        tooltip.style.left = tooltipX + 'px';
        tooltip.style.top = tooltipY + 'px';
    });

    canvas.addEventListener('mouseleave', function() {
        tooltip.style.display = 'none';
    });

    // Click to scroll to closest log entry
    canvas.style.cursor = 'pointer';
    canvas.addEventListener('click', function(e) {
        if (!tcMetricsDataRef || tcMetricsDataRef.length === 0) return;

        const rect = canvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const chartWidth = rect.width - padding.left - padding.right;

        // Determine which sample index we clicked on
        const ratio = (x - padding.left) / chartWidth;
        const sampleIndex = Math.round(ratio * (tcMetricsDataRef.length - 1));
        if (sampleIndex < 0 || sampleIndex >= tcMetricsDataRef.length) return;

        const targetTs = tcMetricsDataRef[sampleIndex].ts;
        scrollToClosestLogEntry(targetTs);
    });
}

/**
 * Scroll to the log entry closest to the given timestamp.
 */
function scrollToClosestLogEntry(targetTs) {
    const rows = document.querySelectorAll('#msg_table tbody tr.log-entry-row');
    if (rows.length === 0) return;

    let closestRow = null;
    let closestDiff = Infinity;

    rows.forEach(row => {
        const timeCell = row.querySelector('.log-time');
        if (!timeCell) return;

        const originalTime = timeCell.getAttribute('data-original-time');
        if (!originalTime) return;

        const rowTs = new Date(originalTime).getTime();
        const diff = Math.abs(rowTs - targetTs);
        if (diff < closestDiff) {
            closestDiff = diff;
            closestRow = row;
        }
    });

    if (closestRow) {
        // Remove any existing highlight
        document.querySelectorAll('#msg_table tbody tr.timestamp-highlight').forEach(r => {
            r.classList.remove('timestamp-highlight');
        });

        // Add highlight to target row
        closestRow.classList.add('timestamp-highlight');

        // Scroll into view with some offset from top
        closestRow.scrollIntoView({ behavior: 'smooth', block: 'center' });

        // Remove highlight after animation
        setTimeout(() => {
            closestRow.classList.remove('timestamp-highlight');
        }, 2000);
    }
}

// ============================================================================
// AI FAILURE ANALYSIS
// ============================================================================

function loadAiAnalysis() {
    if (!templateConfig || !templateConfig.serverMode) return;

    const section = document.getElementById('aiAnalysisSection');
    const card = document.getElementById('aiAnalysisCard');
    if (!section || !card) return;

    const runId = templateConfig.runId;
    const tcFullName = templateConfig.testCaseFullName;

    fetch(`/api/runs/${encodeURIComponent(runId)}/analysis/${encodeURIComponent(tcFullName)}`)
        .then(response => {
            if (response.status === 404) return null;
            return response.json();
        })
        .then(data => {
            if (!data || !data.success) return;
            section.style.display = '';
            card.innerHTML = renderAiAnalysis(data);
            const reBtn = document.getElementById('aiReanalyzeBtn');
            if (reBtn) reBtn.style.display = '';
        })
        .catch(error => {
            console.error('Error loading AI analysis:', error);
        });
}

function renderAiAnalysis(data) {
    let html = '';

    // Summary (use summary_html if available)
    if (data.summary_html) {
        html += `<div class="ai-analysis-summary">${data.summary_html}</div>`;
    } else if (data.summary) {
        html += `<div class="ai-analysis-summary">${escapeHtml(data.summary)}</div>`;
    }

    // Deep analysis
    if (data.deep_html) {
        html += `<div class="ai-analysis-deep">${data.deep_html}</div>`;
    }

    // Deep analysis button (only if no deep_html yet)
    if (!data.deep_html) {
        html += `<div style="margin: 8px 0;"><button id="aiDeepAnalysisBtn" class="btn btn-sm btn-info" onclick="triggerDeepAnalysis()">🔍 Deep Analysis</button></div>`;
    }

    // References
    if (data.references && data.references.length > 0) {
        html += '<div class="ai-analysis-references">';
        html += '<div class="ai-analysis-references-title">References</div>';
        html += '<ul class="ai-analysis-ref-list">';
        for (const ref of data.references) {
            if (ref.url) {
                html += `<li><a href="${escapeHtml(ref.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(ref.title || ref.url)}</a></li>`;
            } else if (ref.title) {
                html += `<li>${escapeHtml(ref.title)}</li>`;
            }
        }
        html += '</ul></div>';
    }

    // Meta badges
    const badges = [];
    if (data.category) {
        badges.push(`<span class="ai-analysis-badge category">${escapeHtml(data.category)}</span>`);
    }
    if (data.confidence) {
        const level = data.confidence >= 0.7 ? 'high' : data.confidence >= 0.4 ? 'medium' : 'low';
        badges.push(`<span class="ai-analysis-badge confidence-${level}">Confidence: ${Math.round(data.confidence * 100)}%</span>`);
    }
    if (data.model_used) {
        badges.push(`<span class="ai-analysis-badge model">${escapeHtml(data.model_used)}</span>`);
    }
    if (badges.length > 0) {
        html += `<div class="ai-analysis-meta">${badges.join('')}</div>`;
    }

    // Reasoning (collapsible)
    if (data.reasoning) {
        html += '<div class="ai-analysis-reasoning">';
        html += '<button class="ai-analysis-reasoning-toggle" onclick="toggleAnalysisReasoning(this)">▶ Show reasoning</button>';
        html += `<div class="ai-analysis-reasoning-content">${escapeHtml(data.reasoning)}</div>`;
        html += '</div>';
    }

    return html;
}

function toggleAnalysisReasoning(btn) {
    const content = btn.nextElementSibling;
    if (content.classList.contains('expanded')) {
        content.classList.remove('expanded');
        btn.textContent = '▶ Show reasoning';
    } else {
        content.classList.add('expanded');
        btn.textContent = '▼ Hide reasoning';
    }
}

function reanalyzeTestCase() {
    if (!templateConfig) return;
    const btn = document.getElementById('aiReanalyzeBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Analyzing...'; }

    fetch(`/api/runs/${encodeURIComponent(templateConfig.runId)}/analyze`, {method: 'POST'})
        .then(r => r.json())
        .then(data => {
            if (!data.success) {
                alert('Failed: ' + (data.error || 'Unknown'));
                if (btn) { btn.disabled = false; btn.textContent = 'Re-analyze'; }
                return;
            }
            // Poll for completion then reload
            const poll = setInterval(() => {
                fetch(`/api/runs/${encodeURIComponent(templateConfig.runId)}/analysis`)
                    .then(r => r.json())
                    .then(s => {
                        if (s.success && s.status === 'completed') {
                            clearInterval(poll);
                            loadAiAnalysis();
                            if (btn) { btn.disabled = false; btn.textContent = 'Re-analyze'; }
                        } else if (s.success && s.status === 'failed') {
                            clearInterval(poll);
                            alert('Analysis failed: ' + (s.error || 'Unknown error'));
                            if (btn) { btn.disabled = false; btn.textContent = 'Re-analyze'; }
                        }
                    });
            }, 2000);
        })
        .catch(e => {
            alert('Error: ' + e.message);
            if (btn) { btn.disabled = false; btn.textContent = 'Re-analyze'; }
        });
}

function triggerDeepAnalysis() {
    if (!templateConfig) return;
    const btn = document.getElementById('aiDeepAnalysisBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Analyzing...'; }

    const runId = encodeURIComponent(templateConfig.runId);
    const tcName = encodeURIComponent(templateConfig.testCaseFullName);

    fetch(`/api/runs/${runId}/analyze/${tcName}/deep`, {method: 'POST'})
        .then(r => r.json())
        .then(data => {
            if (!data.success) {
                alert('Failed: ' + (data.error || 'Unknown'));
                if (btn) { btn.disabled = false; btn.textContent = '🔍 Deep Analysis'; }
                return;
            }
            const poll = setInterval(() => {
                fetch(`/api/runs/${runId}/analyze/${tcName}/deep`)
                    .then(r => r.json())
                    .then(s => {
                        if (s.success && s.status === 'completed') {
                            clearInterval(poll);
                            loadAiAnalysis();
                        } else if (s.success && s.status === 'failed') {
                            clearInterval(poll);
                            alert('Deep analysis failed: ' + (s.error || 'Unknown error'));
                            if (btn) { btn.disabled = false; btn.textContent = '🔍 Deep Analysis'; }
                        }
                    });
            }, 3000);
        })
        .catch(e => {
            alert('Error: ' + e.message);
            if (btn) { btn.disabled = false; btn.textContent = '🔍 Deep Analysis'; }
        });
}

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// Initialize when DOM is ready and templateConfig is available
if (typeof templateConfig !== 'undefined') {
    initializeTestCaseLog();
    initializeAttachments();
    initializeStackTraceSection();
    initializeMetricsSummary();
    loadAiAnalysis();
} else {
    // Wait for templateConfig to be defined
    document.addEventListener('DOMContentLoaded', function() {
        if (typeof templateConfig !== 'undefined') {
            initializeTestCaseLog();
            initializeAttachments();
            initializeStackTraceSection();
            initializeMetricsSummary();
            loadAiAnalysis();
        }
    });
}
