/* TestRift comments UI: composer, threads, navigator, list icons. */
(function (global) {
    const NAME_KEY = "trCommentAuthorName";
    const BODY_MAX = 8000;

    function iconSvg() {
        return '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M2.5 2A1.5 1.5 0 0 0 1 3.5v7A1.5 1.5 0 0 0 2.5 12H4v2.3a.4.4 0 0 0 .66.3L8.2 12H13.5A1.5 1.5 0 0 0 15 10.5v-7A1.5 1.5 0 0 0 13.5 2h-11z"/></svg>';
    }

    function iconEl(title, href) {
        const node = document.createElement(href ? "a" : "span");
        node.className = "tr-comment-icon";
        node.title = title || "Comments";
        node.setAttribute("aria-label", title || "Comments");
        if (href) {
            node.href = href;
        }
        node.innerHTML = iconSvg();
        return node;
    }

    function toast(message) {
        let el = document.querySelector(".tr-comment-toast");
        if (!el) {
            el = document.createElement("div");
            el.className = "tr-comment-toast";
            document.body.appendChild(el);
        }
        el.textContent = message;
        el.classList.add("show");
        clearTimeout(el._hide);
        el._hide = setTimeout(function () {
            el.classList.remove("show");
        }, 1600);
    }

    function formatTime(iso) {
        if (!iso) {
            return "";
        }
        try {
            return new Date(iso).toLocaleString();
        } catch (err) {
            return iso;
        }
    }

    function currentName() {
        try {
            return (localStorage.getItem(NAME_KEY) || "").trim();
        } catch (err) {
            return "";
        }
    }

    function rememberName(name) {
        try {
            localStorage.setItem(NAME_KEY, name);
        } catch (err) { /* ignore */ }
    }

    function ensureAuthorName(authEnabled, currentUser) {
        if (authEnabled && currentUser) {
            return currentUser.display_name || currentUser.username || "User";
        }
        let name = currentName();
        if (name) {
            return name;
        }
        name = (window.prompt("Display name for comments", "User") || "").trim();
        if (!name) {
            return null;
        }
        rememberName(name);
        return name;
    }

    function canEdit(comment, ctx) {
        if (ctx.readOnly) {
            return false;
        }
        if (ctx.authEnabled) {
            return !!(ctx.currentUser && comment.author_user_id === ctx.currentUser.id);
        }
        return currentName() && comment.author_name === currentName();
    }

    function canDelete(comment, ctx) {
        if (ctx.readOnly) {
            return false;
        }
        if (canEdit(comment, ctx)) {
            return true;
        }
        return !!(ctx.currentUser && ctx.currentUser.role === "admin");
    }

    function wrapSelection(textarea, before, after) {
        const start = textarea.selectionStart;
        const end = textarea.selectionEnd;
        const value = textarea.value;
        const selected = value.slice(start, end) || "text";
        textarea.value = value.slice(0, start) + before + selected + after + value.slice(end);
        textarea.focus();
        textarea.selectionStart = start + before.length;
        textarea.selectionEnd = start + before.length + selected.length;
    }

    function tokenAtCursor(textarea) {
        const pos = textarea.selectionStart;
        const before = textarea.value.slice(0, pos);
        const match = before.match(/:([A-Za-z0-9_+-]*)$/);
        if (!match) {
            return null;
        }
        return { start: pos - match[0].length, prefix: match[1], query: match[0] };
    }

    function createComposer(options) {
        const wrap = document.createElement("div");
        wrap.className = "tr-comment-composer";
        wrap.innerHTML =
            '<div class="tr-comment-tabs">' +
            '<button type="button" class="tr-comment-tab active" data-tab="write">Write</button>' +
            '<button type="button" class="tr-comment-tab" data-tab="preview">Preview</button>' +
            "</div>" +
            '<div class="tr-comment-write">' +
            '<div class="tr-comment-toolbar">' +
            '<button type="button" data-wrap="**">B</button>' +
            '<button type="button" data-wrap="*">I</button>' +
            '<button type="button" data-wrap="`">&lt;/&gt;</button>' +
            "</div>" +
            '<textarea maxlength="' + BODY_MAX + '" placeholder="Leave a comment"></textarea>' +
            "</div>" +
            '<div class="tr-comment-preview" hidden></div>' +
            '<div class="tr-comment-composer-actions">' +
            '<button type="button" class="tr-comment-btn tr-comment-cancel">Cancel</button>' +
            '<button type="button" class="tr-comment-btn tr-comment-btn-primary tr-comment-save">' + (options.saveLabel || "Comment") + "</button>" +
            "</div>";
        const textarea = wrap.querySelector("textarea");
        const preview = wrap.querySelector(".tr-comment-preview");
        const writePane = wrap.querySelector(".tr-comment-write");
        textarea.value = options.initialBody || "";
        let ac = null;

        function hideAutocomplete() {
            if (ac) {
                ac.remove();
                ac = null;
            }
        }

        function showAutocomplete() {
            hideAutocomplete();
            const token = tokenAtCursor(textarea);
            if (!token || !global.CommentsMarkdown) {
                return;
            }
            const matches = global.CommentsMarkdown.matchingShortcodes(token.prefix, 8);
            if (!matches.length) {
                return;
            }
            ac = document.createElement("div");
            ac.className = "tr-comment-autocomplete";
            matches.forEach(function (item, index) {
                const btn = document.createElement("button");
                btn.type = "button";
                if (index === 0) {
                    btn.className = "active";
                }
                btn.innerHTML = "<span>" + item.glyph + "</span><span>:" + item.name + ":</span>";
                btn.addEventListener("mousedown", function (event) {
                    event.preventDefault();
                    const value = textarea.value;
                    textarea.value = value.slice(0, token.start) + ":" + item.name + ":" + value.slice(textarea.selectionStart);
                    hideAutocomplete();
                    textarea.focus();
                });
                ac.appendChild(btn);
            });
            wrap.style.position = "relative";
            wrap.appendChild(ac);
        }

        wrap.querySelectorAll(".tr-comment-tab").forEach(function (tab) {
            tab.addEventListener("click", function () {
                wrap.querySelectorAll(".tr-comment-tab").forEach(function (other) {
                    other.classList.remove("active");
                });
                tab.classList.add("active");
                const isPreview = tab.getAttribute("data-tab") === "preview";
                writePane.hidden = isPreview;
                preview.hidden = !isPreview;
                if (isPreview) {
                    hideAutocomplete();
                    const source = textarea.value.trim();
                    if (!source) {
                        preview.innerHTML = '<div class="tr-comment-preview-empty">Nothing to preview</div>';
                    } else if (global.CommentsMarkdown) {
                        preview.innerHTML = global.CommentsMarkdown.render(source);
                    }
                }
            });
        });
        wrap.querySelectorAll("[data-wrap]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                const mark = btn.getAttribute("data-wrap");
                wrapSelection(textarea, mark, mark);
            });
        });
        textarea.addEventListener("input", showAutocomplete);
        textarea.addEventListener("blur", function () {
            setTimeout(hideAutocomplete, 150);
        });
        wrap.querySelector(".tr-comment-cancel").addEventListener("click", function () {
            hideAutocomplete();
            if (options.onCancel) {
                options.onCancel();
            }
        });
        wrap.querySelector(".tr-comment-save").addEventListener("click", function () {
            hideAutocomplete();
            const body = textarea.value.trim();
            if (!body) {
                toast("Comment cannot be empty");
                return;
            }
            if (options.onSave) {
                options.onSave(body);
            }
        });
        return wrap;
    }

    function renderComment(comment, ctx) {
        const item = document.createElement("div");
        item.className = "tr-comment-item";
        item.setAttribute("data-comment-id", String(comment.id));
        const meta = document.createElement("div");
        meta.className = "tr-comment-meta";
        meta.innerHTML =
            '<span class="tr-comment-author"></span>' +
            '<span class="tr-comment-time"></span>' +
            (comment.edited ? '<span class="tr-comment-edited">edited</span>' : "") +
            '<span class="tr-comment-actions"></span>';
        meta.querySelector(".tr-comment-author").textContent = comment.author_name || "User";
        meta.querySelector(".tr-comment-time").textContent = formatTime(comment.updated_at || comment.created_at);
        const actions = meta.querySelector(".tr-comment-actions");
        const copyBtn = document.createElement("button");
        copyBtn.type = "button";
        copyBtn.textContent = "Copy link";
        copyBtn.addEventListener("click", function () {
            const url = ctx.linkFor(comment);
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(url).then(function () {
                    toast("Link copied");
                }).catch(function () {
                    toast(url);
                });
            } else {
                toast(url);
            }
        });
        actions.appendChild(copyBtn);
        if (canEdit(comment, ctx)) {
            const editBtn = document.createElement("button");
            editBtn.type = "button";
            editBtn.textContent = "Edit";
            editBtn.addEventListener("click", function () {
                ctx.onEdit(comment, item);
            });
            actions.appendChild(editBtn);
        }
        if (canDelete(comment, ctx)) {
            const delBtn = document.createElement("button");
            delBtn.type = "button";
            delBtn.textContent = "Delete";
            delBtn.addEventListener("click", function () {
                if (window.confirm("Delete this comment?")) {
                    ctx.onDelete(comment);
                }
            });
            actions.appendChild(delBtn);
        }
        const body = document.createElement("div");
        body.className = "tr-comment-body";
        body.innerHTML = global.CommentsMarkdown ? global.CommentsMarkdown.render(comment.body) : "";
        item.appendChild(meta);
        item.appendChild(body);
        return item;
    }

    async function api(method, url, payload) {
        const opts = { method: method, headers: { "Accept": "application/json" } };
        if (payload !== undefined) {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(payload);
        }
        const response = await fetch(url, opts);
        let data = {};
        try {
            data = await response.json();
        } catch (err) { /* ignore */ }
        if (!response.ok || data.success === false) {
            throw new Error((data && data.error) || ("Request failed (" + response.status + ")"));
        }
        return data;
    }

    function logRows() {
        return Array.from(document.querySelectorAll("#msg_table tbody tr.log-entry-row"));
    }

    function rowByIndex(index) {
        return document.querySelector('#msg_table tbody tr.log-entry-row[data-log-index="' + index + '"]');
    }

    function isRangeVisible(start, end) {
        for (let i = start; i <= end; i++) {
            const row = rowByIndex(i);
            if (row && row.style.display !== "none") {
                return true;
            }
        }
        return false;
    }

    function threadKey(comment) {
        return String(comment.line_start) + "-" + String(comment.line_end);
    }

    function formatLineRange(start, end) {
        const from = start + 1;
        const to = end + 1;
        if (from === to) {
            return "Line " + from;
        }
        return "Lines " + from + "–" + to;
    }

    const CommentsLog = {
        comments: [],
        ctx: null,
        selection: null,
        currentNav: 0,
        commentedOnly: false,

        async init(config) {
            this.config = config;
            this.ctx = {
                authEnabled: !!config.authEnabled,
                currentUser: config.currentUser || null,
                readOnly: !!config.readOnly,
                linkFor: function (comment) {
                    return window.location.origin + "/testRun/" + config.runId + "/log/" + encodeURIComponent(config.tcId) + ".html#comment=" + comment.id;
                },
                onEdit: this.startEdit.bind(this),
                onDelete: this.remove.bind(this)
            };
            if (global.CommentsMarkdown) {
                await global.CommentsMarkdown.loadShortcodes();
            }
            this.bindTable();
            this.bindNavigator();
            this.bindKeys();
            if (config.readOnly) {
                this.comments = config.embeddedComments || [];
                this.renderAll();
                this.applyHash();
                return;
            }
            await this.reload();
            this.applyHash();
        },

        bindTable() {
            const table = document.getElementById("msg_table");
            if (!table || table._commentsBound) {
                return;
            }
            table._commentsBound = true;
            const self = this;

            function rowIndexFromEvent(event) {
                const row = event.target.closest("tr.log-entry-row");
                if (!row) {
                    return null;
                }
                const index = Number(row.getAttribute("data-log-index"));
                return Number.isNaN(index) ? null : index;
            }

            function openGutterThread(event) {
                const icon = event.target.closest(".log-gutter .tr-comment-icon");
                if (!icon) {
                    return false;
                }
                const index = rowIndexFromEvent(event);
                if (index === null) {
                    return true;
                }
                const comment = self.comments.find(function (item) {
                    return index >= item.line_start && index <= item.line_end;
                });
                if (comment) {
                    const ordered = self.orderedComments();
                    self.goto(ordered.findIndex(function (item) { return item.id === comment.id; }));
                }
                return true;
            }

            table.addEventListener("click", function (event) {
                if (self._justDragged) {
                    self._justDragged = false;
                    return;
                }
                if (openGutterThread(event)) {
                    return;
                }
                if (self.ctx.readOnly) {
                    return;
                }
                const row = event.target.closest("tr.log-entry-row");
                if (!row || event.target.closest(".comment-thread-row, .comment-composer-row, a, button, textarea")) {
                    return;
                }
                const index = Number(row.getAttribute("data-log-index"));
                if (Number.isNaN(index)) {
                    return;
                }
                if (event.shiftKey && self.selection) {
                    self.selection.end = index;
                    self.paintSelection();
                    self.openComposer();
                    return;
                }
                self.selection = { start: index, end: index };
                self.paintSelection();
                self.openComposer();
            });
            table.addEventListener("mousedown", function (event) {
                if (event.button !== 0) {
                    return;
                }
                const row = event.target.closest("tr.log-entry-row");
                if (row) {
                    if (event.shiftKey) {
                        event.preventDefault();
                    }
                }
                if (self.ctx.readOnly || event.shiftKey) {
                    return;
                }
                if (!row || event.target.closest("a, button, textarea, .tr-comment-icon")) {
                    return;
                }
                const index = Number(row.getAttribute("data-log-index"));
                if (Number.isNaN(index)) {
                    return;
                }
                self._drag = { start: index };
            });
            table.addEventListener("mousemove", function (event) {
                if (!self._drag || self.ctx.readOnly) {
                    return;
                }
                const index = rowIndexFromEvent(event);
                if (index === null) {
                    return;
                }
                if (index !== self._drag.start) {
                    self._drag.moved = true;
                    self.selection = { start: self._drag.start, end: index };
                    self.paintSelection();
                }
            });
            document.addEventListener("mouseup", function () {
                if (!self._drag) {
                    return;
                }
                if (self._drag.moved) {
                    self._justDragged = true;
                    self.openComposer();
                }
                self._drag = null;
            });
        },

        bindNavigator() {
            const first = document.getElementById("commentNavFirst");
            const self = this;
            if (first) {
                first.addEventListener("click", function () { self.goto(0); });
            }
            const filter = document.getElementById("commentedLinesOnly");
            if (filter) {
                filter.addEventListener("change", function () {
                    self.commentedOnly = filter.checked;
                    self.applyCommentedFilter();
                });
            }
        },

        bindKeys() {
            const self = this;
            document.addEventListener("keydown", function (event) {
                const tag = (event.target && event.target.tagName) || "";
                if (tag === "TEXTAREA" || tag === "INPUT" || event.target.isContentEditable) {
                    return;
                }
                if (event.key === "n" || event.key === "j") {
                    event.preventDefault();
                    self.goto(self.currentNav + 1);
                } else if (event.key === "p" || event.key === "k") {
                    event.preventDefault();
                    self.goto(self.currentNav - 1);
                }
            });
        },

        paintSelection() {
            logRows().forEach(function (row) {
                row.classList.remove("log-row-selected");
            });
            if (!this.selection) {
                return;
            }
            const start = Math.min(this.selection.start, this.selection.end);
            const end = Math.max(this.selection.start, this.selection.end);
            for (let i = start; i <= end; i++) {
                const row = rowByIndex(i);
                if (row) {
                    row.classList.add("log-row-selected");
                }
            }
        },

        clearComposer() {
            document.querySelectorAll(".comment-composer-row").forEach(function (row) {
                row.remove();
            });
        },

        async postLogComment(start, end, body) {
            const author = ensureAuthorName(this.ctx.authEnabled, this.ctx.currentUser);
            if (!author) {
                return null;
            }
            await api("POST", "/api/runs/" + this.config.runId + "/comments", {
                scope: "log",
                tc_id: this.config.tcId,
                line_start: start,
                line_end: end,
                body: body,
                author_name: author
            });
            await this.reload();
            const ordered = this.orderedComments();
            const index = ordered.findIndex(function (item) {
                return item.line_start === start && item.line_end === end && item.body === body;
            });
            this.goto(index >= 0 ? index : ordered.length - 1);
            return true;
        },

        openComposer() {
            this.clearComposer();
            if (!this.selection) {
                return;
            }
            const start = Math.min(this.selection.start, this.selection.end);
            const end = Math.max(this.selection.start, this.selection.end);
            const last = rowByIndex(end) || rowByIndex(start);
            if (!last) {
                return;
            }
            const tr = document.createElement("tr");
            tr.className = "comment-composer-row";
            const td = document.createElement("td");
            td.colSpan = 4;
            const self = this;
            td.appendChild(createComposer({
                saveLabel: "Comment",
                onCancel: function () {
                    self.clearComposer();
                    self.selection = null;
                    self.paintSelection();
                },
                onSave: async function (body) {
                    try {
                        const saved = await self.postLogComment(start, end, body);
                        if (saved) {
                            self.clearComposer();
                            self.selection = null;
                            self.paintSelection();
                        }
                    } catch (err) {
                        toast(err.message || "Could not save comment");
                    }
                }
            }));
            tr.appendChild(td);
            last.after(tr);
        },

        openReply(comment, threadEl) {
            if (this.ctx.readOnly || !threadEl) {
                return;
            }
            const existing = threadEl.querySelector(".tr-comment-composer");
            if (existing) {
                const textarea = existing.querySelector("textarea");
                if (textarea) {
                    textarea.focus();
                }
                return;
            }
            const start = comment.line_start;
            const end = comment.line_end;
            const self = this;
            const host = document.createElement("div");
            host.className = "tr-comment-reply-host";
            host.appendChild(createComposer({
                saveLabel: "Reply",
                onCancel: function () { host.remove(); },
                onSave: async function (body) {
                    try {
                        await self.postLogComment(start, end, body);
                    } catch (err) {
                        toast(err.message || "Could not save comment");
                    }
                }
            }));
            const bar = threadEl.querySelector(".tr-comment-thread-bar");
            if (bar) {
                threadEl.insertBefore(host, bar);
            } else {
                threadEl.appendChild(host);
            }
            const textarea = host.querySelector("textarea");
            if (textarea) {
                textarea.focus();
            }
        },

        async reload() {
            const data = await api("GET", "/api/runs/" + this.config.runId + "/comments/log/" + encodeURIComponent(this.config.tcId));
            if (data.current_user) {
                this.ctx.currentUser = data.current_user;
            }
            this.ctx.authEnabled = !!data.auth_enabled;
            this.comments = data.comments || [];
            this.renderAll();
        },

        renderAll() {
            document.querySelectorAll(".comment-thread-row").forEach(function (row) {
                row.remove();
            });
            logRows().forEach(function (row) {
                const gutter = row.querySelector(".log-gutter");
                if (gutter) {
                    gutter.innerHTML = '<span class="tr-comment-gutter-add">+</span>';
                }
                row.classList.remove(
                    "has-comment",
                    "comment-range-span",
                    "comment-range-focus",
                    "comment-range-focus-start",
                    "comment-range-focus-end"
                );
            });
            const grouped = {};
            this.comments.forEach(function (comment) {
                const key = threadKey(comment);
                if (!grouped[key]) {
                    grouped[key] = [];
                }
                grouped[key].push(comment);
                const start = comment.line_start;
                const end = comment.line_end;
                for (let i = start; i <= end; i++) {
                    const row = rowByIndex(i);
                    if (!row) {
                        continue;
                    }
                    row.classList.add("has-comment");
                    if (end > start) {
                        row.classList.add("comment-range-span");
                    }
                    const gutter = row.querySelector(".log-gutter");
                    if (gutter && !gutter.querySelector(".tr-comment-icon")) {
                        gutter.innerHTML = "";
                        gutter.appendChild(iconEl("Comments on this line"));
                    }
                }
            });
            const self = this;
            Object.keys(grouped).forEach(function (key) {
                const comments = grouped[key];
                const end = comments[0].line_end;
                const last = rowByIndex(end);
                const start = comments[0].line_start;
                const tr = document.createElement("tr");
                tr.className = "comment-thread-row";
                tr.setAttribute("data-thread-key", key);
                tr.setAttribute("data-thread-start", String(start));
                tr.setAttribute("data-thread-end", String(end));
                const td = document.createElement("td");
                td.colSpan = 4;
                const thread = document.createElement("div");
                thread.className = "tr-comment-thread";
                const range = document.createElement("div");
                range.className = "tr-comment-thread-range";
                range.textContent = formatLineRange(start, end);
                thread.appendChild(range);
                comments.forEach(function (comment) {
                    thread.appendChild(renderComment(comment, self.ctx));
                });
                const bar = document.createElement("div");
                bar.className = "tr-comment-thread-bar";
                if (!self.ctx.readOnly) {
                    const reply = document.createElement("button");
                    reply.type = "button";
                    reply.className = "tr-comment-btn";
                    reply.textContent = "Reply";
                    reply.addEventListener("click", function () {
                        self.openReply(comments[0], thread);
                    });
                    bar.appendChild(reply);
                }
                const jump = document.createElement("span");
                jump.className = "tr-thread-jump";
                jump.hidden = Object.keys(grouped).length < 2;
                const prevThread = document.createElement("button");
                prevThread.type = "button";
                prevThread.setAttribute("data-thread-jump-dir", "-1");
                prevThread.title = "Previous comment thread";
                prevThread.textContent = "Prev thread";
                const label = document.createElement("span");
                label.className = "tr-thread-jump-label";
                const nextThread = document.createElement("button");
                nextThread.type = "button";
                nextThread.setAttribute("data-thread-jump-dir", "1");
                nextThread.title = "Next comment thread";
                nextThread.textContent = "Next thread";
                prevThread.addEventListener("click", function () { self.gotoThread(key, -1); });
                nextThread.addEventListener("click", function () { self.gotoThread(key, 1); });
                jump.appendChild(prevThread);
                jump.appendChild(label);
                jump.appendChild(nextThread);
                bar.appendChild(jump);
                thread.appendChild(bar);
                td.appendChild(thread);
                tr.appendChild(td);
                if (last) {
                    last.after(tr);
                }
            });
            this.updateNavigator();
            this.applyCommentedFilter();
            const filterWrap = document.getElementById("commentedLinesFilterWrap");
            if (filterWrap) {
                filterWrap.style.display = this.comments.length ? "" : "none";
            }
        },

        startEdit(comment, item) {
            const self = this;
            item.innerHTML = "";
            item.appendChild(createComposer({
                initialBody: comment.body,
                saveLabel: "Save changes",
                onCancel: function () { self.renderAll(); },
                onSave: async function (body) {
                    try {
                        await api("PATCH", "/api/comments/" + comment.id, { body: body });
                        await self.reload();
                    } catch (err) {
                        toast(err.message || "Could not update comment");
                    }
                }
            }));
        },

        async remove(comment) {
            try {
                await api("DELETE", "/api/comments/" + comment.id);
                await this.reload();
            } catch (err) {
                toast(err.message || "Could not delete comment");
            }
        },

        orderedComments() {
            return this.comments.slice().sort(function (a, b) {
                if (a.line_end !== b.line_end) {
                    return a.line_end - b.line_end;
                }
                if (a.line_start !== b.line_start) {
                    return a.line_start - b.line_start;
                }
                return String(a.created_at).localeCompare(String(b.created_at));
            });
        },

        orderedThreads() {
            const seen = {};
            const threads = [];
            this.orderedComments().forEach(function (comment) {
                const key = threadKey(comment);
                if (!seen[key]) {
                    seen[key] = true;
                    threads.push(comment);
                }
            });
            return threads;
        },

        gotoThread(key, delta) {
            const threads = this.orderedThreads();
            const index = threads.findIndex(function (item) { return threadKey(item) === key; });
            const target = threads[index + delta];
            if (index < 0 || !target) {
                return;
            }
            const ordered = this.orderedComments();
            this.goto(ordered.findIndex(function (item) { return item.id === target.id; }));
        },

        updateNavigator() {
            const bar = document.getElementById("commentNavigator");
            const label = document.getElementById("commentNavLabel");
            const total = this.comments.length;
            if (!bar) {
                return;
            }
            bar.classList.toggle("is-visible", total > 0);
            if (label) {
                if (!total) {
                    label.textContent = "";
                } else {
                    const current = Math.min(Math.max(this.currentNav, 0), total - 1) + 1;
                    label.textContent = current + " / " + total;
                }
            }
            const first = document.getElementById("commentNavFirst");
            if (first) {
                first.disabled = !total;
            }
            this.updateThreadBars();
        },

        updateThreadBars() {
            const threads = this.orderedThreads();
            document.querySelectorAll(".comment-thread-row").forEach(function (row) {
                const key = row.getAttribute("data-thread-key");
                const jump = row.querySelector(".tr-thread-jump");
                if (!jump) {
                    return;
                }
                jump.hidden = threads.length < 2;
                const pos = threads.findIndex(function (item) { return threadKey(item) === key; });
                const label = jump.querySelector(".tr-thread-jump-label");
                if (label) {
                    label.textContent = (pos + 1) + " / " + threads.length;
                }
                const prev = jump.querySelector('[data-thread-jump-dir="-1"]');
                const next = jump.querySelector('[data-thread-jump-dir="1"]');
                if (prev) {
                    prev.disabled = pos <= 0;
                }
                if (next) {
                    next.disabled = pos >= threads.length - 1;
                }
            });
        },

        highlightRange(comment) {
            logRows().forEach(function (row) {
                row.classList.remove(
                    "comment-range-focus",
                    "comment-range-focus-start",
                    "comment-range-focus-end"
                );
            });
            document.querySelectorAll(".comment-thread-row.is-focused-thread").forEach(function (row) {
                row.classList.remove("is-focused-thread");
            });
            if (!comment) {
                return;
            }
            const start = comment.line_start;
            const end = comment.line_end;
            for (let i = start; i <= end; i++) {
                const row = rowByIndex(i);
                if (!row) {
                    continue;
                }
                row.classList.add("comment-range-focus");
                if (i === start) {
                    row.classList.add("comment-range-focus-start");
                }
                if (i === end) {
                    row.classList.add("comment-range-focus-end");
                }
            }
            const thread = document.querySelector('.comment-thread-row[data-thread-key="' + threadKey(comment) + '"]');
            if (thread) {
                thread.classList.add("is-focused-thread");
            }
        },

        expandThread(comment) {
            document.querySelectorAll(".tr-comment-item.is-focused").forEach(function (item) {
                item.classList.remove("is-focused");
            });
            const thread = document.querySelector('.comment-thread-row[data-thread-key="' + threadKey(comment) + '"]');
            const item = document.querySelector('.tr-comment-item[data-comment-id="' + comment.id + '"]');
            const row = rowByIndex(comment.line_end);
            if (item) {
                item.classList.add("is-focused");
            }
            this.highlightRange(comment);
            const visible = isRangeVisible(comment.line_start, comment.line_end);
            const target = item || thread || row;
            if (visible && target && target.scrollIntoView) {
                target.scrollIntoView({ block: "center" });
            }
            this.highlightMetrics(comment);
            this.updateThreadBars();
            return visible;
        },

        goto(index) {
            const ordered = this.orderedComments();
            if (!ordered.length) {
                return;
            }
            const next = (index + ordered.length) % ordered.length;
            this.currentNav = next;
            this.updateNavigator();
            const comment = ordered[next];
            const visible = this.expandThread(comment);
            this.showHiddenHint(!visible, comment);
        },

        showHiddenHint(hidden, comment) {
            let hint = document.getElementById("commentHiddenHint");
            if (!hint) {
                hint = document.createElement("div");
                hint.id = "commentHiddenHint";
                hint.className = "comment-hidden-hint";
                const header = document.querySelector(".log-header");
                if (header) {
                    header.after(hint);
                }
            }
            if (!hidden) {
                hint.style.display = "none";
                return;
            }
            hint.style.display = "";
            hint.textContent = "Comment is on lines hidden by the current filter. ";
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "btn btn-xs btn-default";
            btn.textContent = "Clear filter and show";
            const self = this;
            btn.addEventListener("click", function () {
                const filterInput = document.getElementById("filterInput");
                if (filterInput) {
                    filterInput.value = "";
                    filterInput.dispatchEvent(new Event("input"));
                }
                const only = document.getElementById("commentedLinesOnly");
                if (only) {
                    only.checked = false;
                    self.commentedOnly = false;
                    self.applyCommentedFilter();
                }
                hint.style.display = "none";
                self.expandThread(comment);
            });
            hint.appendChild(btn);
        },

        applyCommentedFilter() {
            const ranges = this.comments.map(function (comment) {
                return { start: Math.max(0, comment.line_start - 2), end: comment.line_end + 2 };
            });
            logRows().forEach(function (row) {
                if (!this.commentedOnly || !ranges.length) {
                    if (row.dataset.commentFilterHide === "1") {
                        row.style.display = "";
                        delete row.dataset.commentFilterHide;
                    }
                    return;
                }
                const index = Number(row.getAttribute("data-log-index"));
                const keep = ranges.some(function (range) {
                    return index >= range.start && index <= range.end;
                });
                if (!keep) {
                    row.style.display = "none";
                    row.dataset.commentFilterHide = "1";
                } else if (row.dataset.commentFilterHide === "1") {
                    row.style.display = "";
                    delete row.dataset.commentFilterHide;
                }
            }, this);
            document.querySelectorAll(".comment-thread-row").forEach(function (thread) {
                const start = Number(thread.getAttribute("data-thread-start"));
                const end = Number(thread.getAttribute("data-thread-end"));
                thread.style.display = isRangeVisible(start, end) ? "" : "none";
            });
            document.dispatchEvent(new Event("logvisibilitychange"));
        },

        highlightMetrics(comment) {
            const row = rowByIndex(comment.line_start);
            if (!row) {
                return;
            }
            const timeCell = row.querySelector(".log-time");
            const ts = timeCell && timeCell.getAttribute("data-original-time");
            if (!ts) {
                return;
            }
            const canvas = document.getElementById("tc-metrics-chart");
            if (!canvas || !global.tcMetricsDataRef || !global.tcMetricsDataRef.length) {
                return;
            }
            const samples = global.tcMetricsDataRef;
            const target = new Date(ts).getTime();
            let best = 0;
            let bestDiff = Infinity;
            samples.forEach(function (sample, i) {
                const diff = Math.abs(sample.ts - target);
                if (diff < bestDiff) {
                    bestDiff = diff;
                    best = i;
                }
            });
            const ctx = canvas.getContext("2d");
            if (!ctx) {
                return;
            }
            const padding = { left: 5, right: 5 };
            const ratio = samples.length === 1 ? 0 : best / (samples.length - 1);
            const x = padding.left + ratio * (canvas.getBoundingClientRect().width - padding.left - padding.right);
            const mark = document.getElementById("tc-comment-metric-mark");
            const container = canvas.closest(".metrics-chart-container");
            if (!container) {
                return;
            }
            let line = mark;
            if (!line) {
                line = document.createElement("div");
                line.id = "tc-comment-metric-mark";
                line.style.cssText = "position:absolute;top:0;bottom:0;width:2px;background:#667eea;opacity:0.85;pointer-events:none;transition:left 0.15s;";
                container.appendChild(line);
            }
            line.style.left = x + "px";
        },

        applyHash() {
            const match = (window.location.hash || "").match(/comment=(\d+)/);
            if (!match) {
                if (this.config.openFirst && this.comments.length) {
                    this.goto(0);
                }
                return;
            }
            const id = Number(match[1]);
            const ordered = this.orderedComments();
            const index = ordered.findIndex(function (comment) { return comment.id === id; });
            if (index >= 0) {
                this.goto(index);
            }
        }
    };

    const CommentsRun = {
        async init(config) {
            this.config = config;
            this.ctx = {
                authEnabled: !!config.authEnabled,
                currentUser: config.currentUser || null,
                readOnly: !!config.readOnly,
                linkFor: function (comment) {
                    return window.location.origin + "/testRun/" + config.runId + "/index.html#comment=" + comment.id;
                },
                onEdit: this.startEdit.bind(this),
                onDelete: this.remove.bind(this)
            };
            if (global.CommentsMarkdown) {
                await global.CommentsMarkdown.loadShortcodes();
            }
            if (config.readOnly) {
                const embedded = config.embeddedComments || {};
                this.runComments = embedded.run_comments || [];
                this.testCases = embedded.test_cases || {};
                this.renderRunComments();
                this.decorateTree();
                this.updateHeaderIcon();
                this.applyHash();
                return;
            }
            await this.reload();
            this.applyHash();
        },

        async reload() {
            const data = await api("GET", "/api/runs/" + this.config.runId + "/comments");
            this.ctx.authEnabled = !!data.auth_enabled;
            if (data.current_user) {
                this.ctx.currentUser = data.current_user;
            }
            this.runComments = data.run_comments || [];
            this.testCases = data.test_cases || {};
            this.renderRunComments();
            this.decorateTree();
            this.updateHeaderIcon();
        },

        renderRunComments() {
            const list = document.getElementById("runCommentsList");
            const empty = document.getElementById("runCommentsEmpty");
            const add = document.getElementById("runCommentsAdd");
            if (!list) {
                return;
            }
            list.innerHTML = "";
            this.runComments.forEach(function (comment) {
                list.appendChild(renderComment(comment, this.ctx));
            }, this);
            if (empty) {
                empty.style.display = this.runComments.length ? "none" : "";
            }
            if (add && !add._bound) {
                add._bound = true;
                const self = this;
                add.addEventListener("click", function () {
                    self.openRunComposer();
                });
            }
        },

        openRunComposer() {
            const host = document.getElementById("runCommentsComposer");
            if (!host) {
                return;
            }
            host.innerHTML = "";
            const self = this;
            host.appendChild(createComposer({
                saveLabel: "Comment",
                onCancel: function () { host.innerHTML = ""; },
                onSave: async function (body) {
                    const author = ensureAuthorName(self.ctx.authEnabled, self.ctx.currentUser);
                    if (!author) {
                        return;
                    }
                    try {
                        await api("POST", "/api/runs/" + self.config.runId + "/comments", {
                            scope: "run",
                            body: body,
                            author_name: author
                        });
                        host.innerHTML = "";
                        await self.reload();
                    } catch (err) {
                        toast(err.message || "Could not save comment");
                    }
                }
            }));
        },

        startEdit(comment, item) {
            const self = this;
            item.innerHTML = "";
            item.appendChild(createComposer({
                initialBody: comment.body,
                saveLabel: "Save changes",
                onCancel: function () { self.renderRunComments(); },
                onSave: async function (body) {
                    try {
                        await api("PATCH", "/api/comments/" + comment.id, { body: body });
                        await self.reload();
                    } catch (err) {
                        toast(err.message || "Could not update comment");
                    }
                }
            }));
        },

        async remove(comment) {
            try {
                await api("DELETE", "/api/comments/" + comment.id);
                await this.reload();
            } catch (err) {
                toast(err.message || "Could not delete comment");
            }
        },

        decorateTree() {
            const root = document.getElementById("test-cases-list");
            if (!root) {
                return;
            }
            root.querySelectorAll(".tr-comment-icon").forEach(function (el) { el.remove(); });
            const self = this;
            const leaves = root.querySelectorAll("li.test-case-node");
            const commented = this.testCases || {};
            leaves.forEach(function (li) {
                const storageId = li.getAttribute("data-storage-id") || li.getAttribute("data-tc-id");
                const info = storageId && commented[storageId];
                if (!info) {
                    return;
                }
                const cluster = li.querySelector(".comment-container") || li.querySelector(".tc-right, .list-view-right");
                const href = "/testRun/" + self.config.runId + "/log/" + encodeURIComponent(storageId) + ".html#comment=" + info.first_comment_id;
                const icon = iconEl("Comments on this test", href);
                if (cluster) {
                    cluster.appendChild(icon);
                } else {
                    li.appendChild(icon);
                }
            });
        },

        updateHeaderIcon() {
            const header = document.getElementById("runCommentsHeaderIcon");
            if (!header) {
                return;
            }
            header.innerHTML = "";
            if (this.runComments.length) {
                header.appendChild(iconEl("Run comments"));
            }
        },

        applyHash() {
            const match = (window.location.hash || "").match(/comment=(\d+)/);
            if (!match) {
                return;
            }
            const id = Number(match[1]);
            const runHit = (this.runComments || []).find(function (comment) { return comment.id === id; });
            if (runHit) {
                const section = document.getElementById("runCommentsSection");
                if (section) {
                    section.scrollIntoView({ block: "center" });
                }
                const item = document.querySelector('#runCommentsList [data-comment-id="' + id + '"]');
                if (item) {
                    item.classList.add("is-focused");
                }
                return;
            }
            const entries = Object.entries(this.testCases || {});
            for (let i = 0; i < entries.length; i++) {
                if (entries[i][1].first_comment_id === id) {
                    const node = document.querySelector('li[data-storage-id="' + CSS.escape(entries[i][0]) + '"]');
                    if (node && node.scrollIntoView) {
                        node.scrollIntoView({ block: "center" });
                        node.classList.add("log-row-selected");
                    }
                    return;
                }
            }
        }
    };

    function attachRunListIcon(anchor, runId, firstCommentId) {
        if (!anchor || anchor.parentElement.querySelector(":scope > .tr-comment-icon")) {
            return;
        }
        let href = "/testRun/" + runId + "/index.html";
        if (firstCommentId) {
            href += "#comment=" + firstCommentId;
        }
        const icon = iconEl("This run has comments", href);
        anchor.parentElement.insertBefore(icon, anchor.nextSibling);
    }

    async function decorateRunLists(root) {
        const scope = root || document;
        const rows = scope.querySelectorAll("[data-runid], [data-run-id]");
        const ids = [];
        rows.forEach(function (row) {
            const id = row.getAttribute("data-runid") || row.getAttribute("data-run-id");
            if (id && ids.indexOf(id) === -1) {
                ids.push(id);
            }
        });
        if (!ids.length) {
            return;
        }
        let presence = {};
        try {
            const data = await api("GET", "/api/comments/presence?run_ids=" + encodeURIComponent(ids.join(",")));
            presence = data.data || {};
        } catch (err) {
            return;
        }
        rows.forEach(function (row) {
            const id = row.getAttribute("data-runid") || row.getAttribute("data-run-id");
            const info = presence[id];
            if (!info || !info.has_comments) {
                return;
            }
            const link = row.querySelector(".run-link, a[href*='/testRun/']");
            if (link && /\/log\//.test(link.getAttribute("href") || "")) {
                const match = (link.getAttribute("href") || "").match(/\/testRun\/([^/]+)\/log\/([^/?#]+)/);
                if (match) {
                    const tcId = decodeURIComponent(match[2].replace(/\.html$/, ""));
                    const tcInfo = info.test_cases && info.test_cases[tcId];
                    if (tcInfo) {
                        const href = "/testRun/" + id + "/log/" + encodeURIComponent(tcId) + ".html#comment=" + tcInfo.first_comment_id;
                        if (!link.parentElement.querySelector(":scope > .tr-comment-icon")) {
                            const icon = iconEl("Comments on this test", href);
                            link.parentElement.insertBefore(icon, link.nextSibling);
                        }
                        return;
                    }
                }
            }
            if (link) {
                attachRunListIcon(link, id, info.first_comment_id);
            }
        });
    }

    global.CommentsUI = {
        iconEl: iconEl,
        attachRunListIcon: attachRunListIcon,
        decorateRunLists: decorateRunLists,
        Log: CommentsLog,
        Run: CommentsRun
    };
})(window);
