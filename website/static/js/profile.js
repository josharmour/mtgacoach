/* mtgacoach.com profile, deck builder, discover, and leaderboard client */
(function () {
    "use strict";

    var root = document.getElementById("profile-app");
    if (!root) return;

    var PAGE = root.dataset.page || "profile";
    var CSRF = root.dataset.csrf || "";
    var toastEl = document.getElementById("toast-region");

    function showToast(msg, isError) {
        if (!toastEl) return;
        toastEl.textContent = msg || "";
        toastEl.classList.toggle("is-error", Boolean(isError));
    }

    function api(url, opts) {
        var options = opts || {};
        var headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
        if (CSRF && options.method && options.method !== "GET") {
            headers["X-CSRF-Token"] = CSRF;
        }
        return fetch(url, Object.assign({ credentials: "same-origin" }, options, { headers: headers })).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (data) {
                if (!r.ok) {
                    throw new Error(data.detail || data.message || ("Request failed (" + r.status + ")"));
                }
                return data;
            });
        });
    }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function clear(node) {
        while (node && node.firstChild) node.removeChild(node.firstChild);
    }

    function fmtPct(val) {
        if (val === null || val === undefined) return "—";
        return Math.round(Number(val) * 1000) / 10 + "%";
    }

    function fmtCraftBadge(cinfo) {
        if (!cinfo || !cinfo.has_collection) return "Sync collection for cost";
        if (cinfo.owned) return "Owned (0 WCs)";
        var s = cinfo.spend || {};
        var costText = [
            s.mythic ? s.mythic + "M" : "",
            s.rare ? s.rare + "R" : "",
            s.uncommon ? s.uncommon + "U" : "",
            s.common ? s.common + "C" : "",
        ].filter(Boolean).join(" ");
        if (cinfo.affordable) return "Buildable (" + (costText || "0 WCs") + ")";
        if (cinfo.unknown && cinfo.unknown.length) return "Unknown rarity (" + cinfo.unknown.length + ")";
        return "Need " + cinfo.extra_wildcards_needed + " more WC (" + costText + ")";
    }

    // ---- Accessible Tabs -------------------------------------------------
    var tabs = Array.prototype.slice.call(root.querySelectorAll('[role="tab"]'));
    var panels = Array.prototype.slice.call(root.querySelectorAll('[role="tabpanel"]'));

    function activateTab(tabName) {
        if (!tabs.length) return;
        var chosen = tabs.find(function (t) { return t.dataset.tab === tabName; }) || tabs[0];
        tabs.forEach(function (tab) {
            var active = tab === chosen;
            tab.setAttribute("aria-selected", active ? "true" : "false");
            if (active) tab.setAttribute("focusgroupstart", "");
            else tab.removeAttribute("focusgroupstart");
        });
        panels.forEach(function (panel) {
            var isTarget = panel.id === "panel-" + chosen.dataset.tab;
            if (isTarget) {
                panel.removeAttribute("hidden");
                panel.setAttribute("tabindex", "0");
            } else {
                panel.setAttribute("hidden", "until-found");
                panel.removeAttribute("tabindex");
            }
        });
        if (chosen.dataset.tab === "discover") loadDiscover();
        if (chosen.dataset.tab === "leaderboard") loadLeaderboard();
        if (chosen.dataset.tab === "decks") loadProfile();
    }

    tabs.forEach(function (tab, idx) {
        tab.addEventListener("click", function () {
            activateTab(tab.dataset.tab);
            tab.scrollIntoView({ block: "nearest", inline: "nearest" });
        });
        tab.addEventListener("keydown", function (e) {
            if (e.key === "ArrowRight") {
                e.preventDefault();
                var next = tabs[(idx + 1) % tabs.length];
                next.focus();
                activateTab(next.dataset.tab);
            } else if (e.key === "ArrowLeft") {
                e.preventDefault();
                var prev = tabs[(idx - 1 + tabs.length) % tabs.length];
                prev.focus();
                activateTab(prev.dataset.tab);
            }
        });
    });

    panels.forEach(function (panel) {
        panel.addEventListener("beforematch", function () {
            activateTab(panel.id.replace(/^panel-/, ""));
        });
    });

    // ---- Export & Delete Dialogs -----------------------------------------
    var exportDialog = document.getElementById("export-dialog");
    var exportText = document.getElementById("export-dialog-text");
    var exportCopyBtn = document.getElementById("export-copy-btn");
    if (exportCopyBtn && exportText) {
        exportCopyBtn.addEventListener("click", function () {
            navigator.clipboard.writeText(exportText.value).then(function () {
                showToast("Copied Arena deck export to clipboard.");
            });
        });
    }
    function openExportDialog(name, text) {
        if (!exportDialog || !exportText) return;
        document.getElementById("export-dialog-title").textContent = "Arena Export — " + name;
        exportText.value = text || "";
        exportDialog.showModal();
    }

    var deleteDialog = document.getElementById("confirm-delete-dialog");
    var pendingDeleteId = null;
    if (deleteDialog) {
        deleteDialog.addEventListener("close", function () {
            var val = deleteDialog.returnValue;
            deleteDialog.returnValue = "";
            if (val === "delete" && pendingDeleteId) {
                var targetId = pendingDeleteId;
                pendingDeleteId = null;
                api("/v1/decks/" + encodeURIComponent(targetId), { method: "DELETE" })
                    .then(function () {
                        showToast("Deck deleted.");
                        loadProfile();
                    })
                    .catch(function (err) { showToast(err.message, true); });
            } else {
                pendingDeleteId = null;
            }
        });
    }

    // ---- Profile State & Overview ----------------------------------------
    var profileState = { profile: null, decks: [] };

    function renderProfile(data) {
        profileState = data;
        var matchesBody = document.getElementById("recent-matches-body");
        if (matchesBody) {
            clear(matchesBody);
            var matches = data.recent_matches || [];
            if (!matches.length) {
                var tr = el("tr");
                var td = el("td", "muted", "No matches synced yet. Play a game with Coach running!");
                td.colSpan = 6;
                tr.appendChild(td);
                matchesBody.appendChild(tr);
            } else {
                matches.forEach(function (m) {
                    var tr = el("tr");
                    var th = el("th", m.result === "win" ? "good-text" : "", m.result.toUpperCase());
                    tr.appendChild(th);
                    var tdDeck = el("td");
                    if (m.deck_slug) {
                        var dLink = el("a", "deck-link", m.deck_name || "View deck");
                        dLink.href = "/decks/" + encodeURIComponent(m.deck_slug);
                        tdDeck.appendChild(dLink);
                    } else {
                        tdDeck.textContent = m.deck_name || "—";
                    }
                    tdDeck.dataset.label = "Deck";
                    var tdFmt = el("td", "", m.format_name || m.format_bucket || "—");
                    tdFmt.dataset.label = "Format";
                    var tdMode = el("td", "", m.autopilot === true ? "Autopilot" : (m.autopilot === false ? "Manual" : "—"));
                    tdMode.dataset.label = "Mode";
                    var tdTurns = el("td", "", m.turns ? String(m.turns) : "—");
                    tdTurns.dataset.label = "Turns";
                    var tdDate = el("td", "muted", new Date(m.played_at * 1000).toLocaleDateString());
                    tdDate.dataset.label = "Played";
                    tr.append(tdDeck, tdFmt, tdMode, tdTurns, tdDate);
                    matchesBody.appendChild(tr);
                });
            }
        }

        var decksBody = document.getElementById("my-decks-body");
        if (decksBody) {
            clear(decksBody);
            var decks = data.decks || [];
            if (!decks.length) {
                var emptyTr = el("tr");
                var emptyTd = el("td", "muted", "No decks synced yet. Open Coach with Arena or build a deck in the Deck Builder.");
                emptyTd.colSpan = 6;
                emptyTr.appendChild(emptyTd);
                decksBody.appendChild(emptyTr);
            } else {
                decks.forEach(function (d) {
                    var tr = el("tr");
                    var th = el("th");
                    var dLink = el("a", "deck-link", d.name);
                    dLink.href = "/decks/" + encodeURIComponent(d.slug);
                    th.appendChild(dLink);
                    th.appendChild(el("span", "muted small", " (" + d.card_count + ")"));
                    tr.appendChild(th);

                    var tdFmt = el("td", "", d.format);
                    tdFmt.dataset.label = "Format";
                    var recText = d.games ? (fmtPct(d.win_rate) + " (" + d.wins + "W-" + d.losses + "L)") : "—";
                    var tdRec = el("td", "", recText);
                    tdRec.dataset.label = "Record";
                    var tdCost = el("td", "", fmtCraftBadge(d.craftability));
                    tdCost.dataset.label = "Wildcards";
                    var syncLabel = d.sync_to_arena_status === "pending"
                        ? "Queued for Coach"
                        : (d.sync_to_arena_status === "synced" ? "Synced" : (d.sync_to_arena_status || "Local"));
                    var tdSync = el("td", "", syncLabel);
                    tdSync.dataset.label = "Arena Sync";

                    var tdAct = el("td");
                    tdAct.dataset.label = "Actions";
                    var actWrap = el("div", "row-actions");

                    var sendBtn = el("button", "mini-btn accent", "Send to Arena");
                    sendBtn.type = "button";
                    sendBtn.addEventListener("click", function () {
                        api("/v1/decks/" + encodeURIComponent(d.id) + "/queue-arena", { method: "POST", body: "{}" })
                            .then(function () {
                                showToast("Queued '" + d.name + "' — open Coach Desktop to build or import it in Arena.");
                                loadProfile();
                            })
                            .catch(function (err) { showToast(err.message, true); });
                    });

                    var editBtn = el("button", "mini-btn", "Edit");
                    editBtn.type = "button";
                    editBtn.addEventListener("click", function () {
                        loadDeckIntoBuilder(d);
                        activateTab("builder");
                    });

                    var copyBtn = el("button", "mini-btn", "Export");
                    copyBtn.type = "button";
                    copyBtn.addEventListener("click", function () { openExportDialog(d.name, d.export_text); });

                    var delBtn = el("button", "mini-btn", "Delete");
                    delBtn.type = "button";
                    delBtn.addEventListener("click", function () {
                        if (!deleteDialog) return;
                        pendingDeleteId = d.id;
                        document.getElementById("delete-dialog-title").textContent = "Delete '" + d.name + "'?";
                        deleteDialog.showModal();
                    });

                    actWrap.append(sendBtn, editBtn, copyBtn);
                    if (d.is_public) {
                        var shareLink = el("a", "mini-btn", "Share");
                        shareLink.href = "/decks/" + encodeURIComponent(d.slug);
                        actWrap.appendChild(shareLink);
                    }
                    actWrap.appendChild(delBtn);
                    tdAct.appendChild(actWrap);
                    tr.append(tdFmt, tdRec, tdCost, tdSync, tdAct);
                    decksBody.appendChild(tr);
                });
            }
        }
    }

    function loadProfile() {
        if (PAGE !== "profile") return;
        api("/v1/profile/me").then(renderProfile).catch(function (err) { showToast(err.message, true); });
    }

    var settingsForm = document.getElementById("settings-form");
    if (settingsForm) {
        settingsForm.addEventListener("submit", function (e) {
            e.preventDefault();
            var payload = {
                display_name: document.getElementById("setting-handle").value.trim(),
                discord_handle: document.getElementById("setting-discord").value.trim(),
                leaderboard_opt_in: document.getElementById("setting-opt-in").checked,
            };
            api("/v1/profile/settings", { method: "PATCH", body: JSON.stringify(payload) })
                .then(function (res) {
                    showToast("Profile settings saved.");
                    var h1 = document.getElementById("profile-heading");
                    if (h1 && h1.firstChild) {
                        h1.firstChild.textContent = (res.profile.display_name || "Your Coach Profile") + " ";
                    }
                })
                .catch(function (err) { showToast(err.message, true); });
        });
    }

    // ---- Wildcard Deck Builder -------------------------------------------
    var builderState = {
        id: null,
        piles: { main: [], sideboard: [], commander: [], companion: [] },
    };

    var builderName = document.getElementById("builder-name");
    var builderFormat = document.getElementById("builder-format");
    var builderArch = document.getElementById("builder-archetype");
    var builderPublic = document.getElementById("builder-public");
    var builderListEl = document.getElementById("builder-card-list");
    var builderTotalEl = document.getElementById("builder-total-count");
    var builderCostBanner = document.getElementById("builder-cost-banner");

    function renderBuilder() {
        if (!builderListEl) return;
        clear(builderListEl);
        var total = 0;
        var spend = { common: 0, uncommon: 0, rare: 0, mythic: 0 };
        var unknown = 0;

        ["commander", "companion", "main", "sideboard"].forEach(function (pile) {
            (builderState.piles[pile] || []).forEach(function (card, idx) {
                if (pile === "main" || pile === "commander") total += card.count;
                if (card.rarity && spend[card.rarity] !== undefined) {
                    spend[card.rarity] += card.count;
                } else if (["Plains", "Island", "Swamp", "Mountain", "Forest"].indexOf(card.name) === -1) {
                    unknown += card.count;
                }

                var li = el("li");
                var label = el("span", "", "[" + pile + "] " + card.count + "x " + card.name + (card.rarity ? " (" + card.rarity + ")" : ""));
                var ctrls = el("div", "row-actions");
                var minus = el("button", "mini-btn", "-1");
                minus.type = "button";
                minus.addEventListener("click", function () {
                    card.count -= 1;
                    if (card.count <= 0) builderState.piles[pile].splice(idx, 1);
                    renderBuilder();
                });
                var plus = el("button", "mini-btn", "+1");
                plus.type = "button";
                plus.addEventListener("click", function () {
                    card.count += 1;
                    renderBuilder();
                });
                ctrls.append(minus, plus);
                li.append(label, ctrls);
                builderListEl.appendChild(li);
            });
        });

        if (builderTotalEl) builderTotalEl.textContent = String(total);
        if (builderCostBanner) {
            var wc = (profileState.profile && profileState.profile.wildcards) || { common: 0, uncommon: 0, rare: 0, mythic: 0 };
            builderCostBanner.textContent =
                "List rarity total: " + spend.mythic + "M · " + spend.rare + "R · " + spend.uncommon + "U · " + spend.common + "C" +
                (unknown ? " · " + unknown + " unpriced" : "") +
                "  |  Your wildcards: " + wc.mythic + "M · " + wc.rare + "R · " + wc.uncommon + "U · " + wc.common + "C (exact owned-card discount calculated on save)";
        }
    }

    function addCardToBuilder(pile, card) {
        var arr = builderState.piles[pile] || (builderState.piles[pile] = []);
        var existing = arr.find(function (c) { return c.name.toLowerCase() === card.name.toLowerCase(); });
        if (existing) {
            existing.count += 1;
        } else {
            arr.push({ name: card.name, count: 1, rarity: card.rarity || "", grp_id: card.grp_id || null });
        }
        renderBuilder();
    }

    function loadDeckIntoBuilder(d) {
        builderState.id = d.id || null;
        builderState.piles = {
            main: (d.main || []).map(function (c) { return Object.assign({}, c); }),
            sideboard: (d.sideboard || []).map(function (c) { return Object.assign({}, c); }),
            commander: (d.commander || []).map(function (c) { return Object.assign({}, c); }),
            companion: (d.companion || []).map(function (c) { return Object.assign({}, c); }),
        };
        if (builderName) builderName.value = d.name || "";
        if (builderFormat) builderFormat.value = d.format || "standard";
        if (builderArch) builderArch.value = d.archetype || "";
        if (builderPublic) builderPublic.checked = Boolean(d.is_public);
        renderBuilder();
    }

    var searchInput = document.getElementById("builder-card-search");
    var searchResultsEl = document.getElementById("builder-search-results");
    var searchTimer = null;
    if (searchInput && searchResultsEl) {
        searchInput.addEventListener("input", function () {
            clearTimeout(searchTimer);
            var q = searchInput.value.trim();
            if (q.length < 2) {
                clear(searchResultsEl);
                return;
            }
            searchTimer = setTimeout(function () {
                api("/v1/cards/search?q=" + encodeURIComponent(q) + "&limit=15")
                    .then(function (res) {
                        clear(searchResultsEl);
                        (res.cards || []).forEach(function (card) {
                            var li = el("li");
                            li.appendChild(el("span", "", card.name + (card.rarity ? " · " + card.rarity : "")));
                            var addBtn = el("button", "mini-btn accent", "+ Add");
                            addBtn.type = "button";
                            addBtn.addEventListener("click", function () {
                                var pile = document.getElementById("builder-pile").value || "main";
                                addCardToBuilder(pile, card);
                            });
                            li.appendChild(addBtn);
                            searchResultsEl.appendChild(li);
                        });
                    })
                    .catch(function () {});
            }, 220);
        });
    }

    function saveBuilderDeck(queueForArena) {
        var payload = {
            name: (builderName && builderName.value.trim()) || "New Deck",
            format: (builderFormat && builderFormat.value) || "standard",
            archetype: (builderArch && builderArch.value.trim()) || "",
            is_public: Boolean(builderPublic && builderPublic.checked),
            main: builderState.piles.main,
            sideboard: builderState.piles.sideboard,
            commander: builderState.piles.commander,
            companion: builderState.piles.companion,
            queue_for_arena: Boolean(queueForArena),
        };
        var url = builderState.id ? "/v1/decks/" + encodeURIComponent(builderState.id) : "/v1/decks";
        var method = builderState.id ? "PATCH" : "POST";
        api(url, { method: method, body: JSON.stringify(payload) })
            .then(function (res) {
                if (res.deck) loadDeckIntoBuilder(res.deck);
                var msg = queueForArena
                    ? "Saved and queued '" + payload.name + "' for Coach & Arena!"
                    : "Saved '" + payload.name + "'.";
                showToast(msg);
                loadProfile();
            })
            .catch(function (err) { showToast(err.message, true); });
    }

    var saveBtn = document.getElementById("builder-save-btn");
    if (saveBtn) saveBtn.addEventListener("click", function () { saveBuilderDeck(false); });
    var saveSyncBtn = document.getElementById("builder-save-sync-btn");
    if (saveSyncBtn) saveSyncBtn.addEventListener("click", function () { saveBuilderDeck(true); });

    var importBtn = document.getElementById("builder-import-btn");
    if (importBtn) {
        importBtn.addEventListener("click", function () {
            var text = document.getElementById("builder-import-text").value;
            api("/v1/decks", {
                method: "POST",
                body: JSON.stringify({
                    name: (builderName && builderName.value.trim()) || "Imported Deck",
                    format: (builderFormat && builderFormat.value) || "standard",
                    is_public: Boolean(builderPublic && builderPublic.checked),
                    import_text: text,
                }),
            }).then(function (res) {
                loadDeckIntoBuilder(res.deck);
                showToast("Imported deck list — exact wildcard craftability calculated.");
                loadProfile();
            }).catch(function (err) { showToast(err.message, true); });
        });
    }

    var resetBtn = document.getElementById("builder-reset-btn");
    if (resetBtn) {
        resetBtn.addEventListener("click", function () {
            loadDeckIntoBuilder({ name: "", format: "standard", archetype: "", is_public: false, main: [], sideboard: [], commander: [], companion: [] });
        });
    }

    var jumpBtn = document.getElementById("new-deck-jump");
    if (jumpBtn) jumpBtn.addEventListener("click", function () { activateTab("builder"); });
    var refreshDecksBtn = document.getElementById("refresh-decks-btn");
    if (refreshDecksBtn) refreshDecksBtn.addEventListener("click", loadProfile);

    var builderCopyBtn = document.getElementById("builder-copy-btn");
    if (builderCopyBtn) {
        builderCopyBtn.addEventListener("click", function () {
            var lines = [];
            [["Commander", "commander"], ["Companion", "companion"], ["Deck", "main"], ["Sideboard", "sideboard"]].forEach(function (pair) {
                var arr = builderState.piles[pair[1]] || [];
                if (arr.length) {
                    lines.push(pair[0] + "\n" + arr.map(function (c) { return c.count + " " + c.name; }).join("\n"));
                }
            });
            openExportDialog((builderName && builderName.value) || "Deck", lines.join("\n\n"));
        });
    }

    function cloneDeckAction(deckId, deckName, queueForArena) {
        return api("/v1/decks/" + encodeURIComponent(deckId) + "/clone", {
            method: "POST",
            body: JSON.stringify({ queue_for_arena: Boolean(queueForArena) }),
        }).then(function () {
            showToast(
                queueForArena
                    ? ("Added '" + deckName + "' to your profile & queued for Coach / Arena!")
                    : ("Added '" + deckName + "' to your profile!")
            );
            if (PAGE === "profile") loadProfile();
        }).catch(function (err) { showToast(err.message, true); });
    }

    // ---- Discover Decks --------------------------------------------------
    function loadDiscover() {
        var body = document.getElementById("discover-body");
        if (!body) return;
        var params = new URLSearchParams({
            format: (document.getElementById("discover-format") || {}).value || "all",
            craftability: (document.getElementById("discover-craft") || {}).value || "all",
            sort: (document.getElementById("discover-sort") || {}).value || "winrate",
            q: (document.getElementById("discover-search") || {}).value || "",
        });
        api("/v1/decks/discover?" + params.toString()).then(function (res) {
            clear(body);
            var decks = res.decks || [];
            if (!decks.length) {
                var tr = el("tr");
                var td = el("td", "muted", "No public decks matched these filters yet.");
                td.colSpan = 6;
                tr.appendChild(td);
                body.appendChild(tr);
                return;
            }
            decks.forEach(function (d) {
                var tr = el("tr");
                var th = el("th");
                var link = el("a", "deck-link", d.name);
                link.href = "/decks/" + encodeURIComponent(d.slug);
                th.appendChild(link);
                tr.appendChild(th);

                var tdFmt = el("td", "", d.format);
                tdFmt.dataset.label = "Format";
                var tdAuth = el("td");
                if (d.author_profile_url) {
                    var aLink = el("a", "deck-link", d.author_name || "Player");
                    aLink.href = d.author_profile_url;
                    tdAuth.appendChild(aLink);
                    if (d.author_is_patron) tdAuth.appendChild(document.createTextNode(" ★"));
                } else {
                    tdAuth.textContent = (d.author_name || "Player") + (d.author_is_patron ? " ★" : "");
                }
                tdAuth.dataset.label = "Player";
                var rec = d.games ? (fmtPct(d.win_rate) + " (" + d.wins + "W-" + d.losses + "L)") : "No games yet";
                var tdRec = el("td", "", rec);
                tdRec.dataset.label = "Record";
                var tdCost = el("td", "", res.authenticated ? fmtCraftBadge(d.craftability) : "Sign in to check");
                tdCost.dataset.label = "Wildcards";

                var tdAct = el("td");
                tdAct.dataset.label = "Actions";
                var wrap = el("div", "row-actions");
                var copyBtn = el("button", "mini-btn", "Export");
                copyBtn.type = "button";
                copyBtn.addEventListener("click", function () { openExportDialog(d.name, d.export_text); });
                wrap.appendChild(copyBtn);

                if (res.authenticated) {
                    var saveBtn = el("button", "mini-btn", "Add to Profile");
                    saveBtn.type = "button";
                    saveBtn.addEventListener("click", function () { cloneDeckAction(d.id, d.name, false); });
                    var cloneBtn = el("button", "mini-btn accent", "Send to Coach");
                    cloneBtn.type = "button";
                    cloneBtn.addEventListener("click", function () { cloneDeckAction(d.id, d.name, true); });
                    wrap.append(saveBtn, cloneBtn);
                }
                tdAct.appendChild(wrap);
                tr.append(tdFmt, tdAuth, tdRec, tdCost, tdAct);
                body.appendChild(tr);
            });
        }).catch(function (err) { showToast(err.message, true); });
    }

    ["discover-format", "discover-craft", "discover-sort"].forEach(function (id) {
        var ctrl = document.getElementById(id);
        if (ctrl) ctrl.addEventListener("change", loadDiscover);
    });
    var dSearch = document.getElementById("discover-search");
    if (dSearch) dSearch.addEventListener("input", loadDiscover);

    // ---- Leaderboard -----------------------------------------------------
    function loadLeaderboard() {
        var head = document.getElementById("lb-head");
        var body = document.getElementById("lb-body");
        if (!head || !body) return;

        var tab = (document.getElementById("lb-tab") || {}).value || "rank";
        var params = new URLSearchParams({
            tab: tab,
            format: (document.getElementById("lb-format") || {}).value || "all",
            mode: (document.getElementById("lb-mode") || {}).value || "all",
            window: (document.getElementById("lb-window") || {}).value || "30d",
        });

        api("/v1/leaderboard?" + params.toString()).then(function (res) {
            var standingEl = document.getElementById("lb-my-standing");
            if (standingEl) {
                if (res.my_standing && res.my_standing.position) {
                    standingEl.textContent = "Your standing: #" + res.my_standing.position + " (" + res.my_standing.handle + ")";
                } else if (res.my_standing && !res.my_standing.leaderboard_opt_in) {
                    standingEl.textContent = "Hidden from public leaderboard (enable on Overview to compete).";
                } else {
                    standingEl.textContent = "";
                }
            }

            clear(head);
            clear(body);
            var htr = el("tr");
            var cols = tab === "decks"
                ? ["#", "Deck", "Format", "Player", "Win Rate", "Wildcards", "Actions"]
                : ["#", "Player", "Rank", "Win Rate", "Games", "Top Deck"];
            cols.forEach(function (c) { htr.appendChild(el("th", "", c)); });
            head.appendChild(htr);

            var entries = res.entries || [];
            if (!entries.length) {
                var tr = el("tr");
                var td = el("td", "muted", tab === "winrate"
                    ? "No players with at least " + res.min_winrate_games + " decided games in this filter yet."
                    : "No entries in this leaderboard view yet.");
                td.colSpan = cols.length;
                tr.appendChild(td);
                body.appendChild(tr);
                return;
            }

            entries.forEach(function (item) {
                var tr = el("tr", item.is_me ? "is-me" : "");
                if (tab === "decks") {
                    tr.appendChild(el("th", "", "#" + item.position));
                    var tdName = el("td");
                    tdName.dataset.label = "Deck";
                    var a = el("a", "deck-link", item.name);
                    a.href = "/decks/" + encodeURIComponent(item.slug);
                    tdName.appendChild(a);
                    var tdFmt = el("td", "", item.format);
                    tdFmt.dataset.label = "Format";
                    var tdPl = el("td");
                    if (item.author_profile_url) {
                        var pLink = el("a", "deck-link", item.author_name || "Player");
                        pLink.href = item.author_profile_url;
                        tdPl.appendChild(pLink);
                        if (item.author_is_patron) tdPl.appendChild(document.createTextNode(" ★"));
                    } else {
                        tdPl.textContent = (item.author_name || "Player") + (item.author_is_patron ? " ★" : "");
                    }
                    tdPl.dataset.label = "Player";
                    var tdWr = el("td", "", fmtPct(item.display_win_rate) + " (" + item.display_wins + "W-" + item.display_losses + "L)");
                    tdWr.dataset.label = "Win Rate";
                    var tdWc = el("td", "", item.craftability ? fmtCraftBadge(item.craftability) : "—");
                    tdWc.dataset.label = "Wildcards";
                    var tdAct = el("td");
                    tdAct.dataset.label = "Actions";
                    var dWrap = el("div", "row-actions");
                    var btn = el("button", "mini-btn", "Export");
                    btn.type = "button";
                    btn.addEventListener("click", function () { openExportDialog(item.name, item.export_text); });
                    dWrap.appendChild(btn);
                    if (root.dataset.signedIn === "1" || PAGE === "profile") {
                        var addBtn = el("button", "mini-btn", "Add to Profile");
                        addBtn.type = "button";
                        addBtn.addEventListener("click", function () { cloneDeckAction(item.id, item.name, false); });
                        var coachBtn = el("button", "mini-btn accent", "Send to Coach");
                        coachBtn.type = "button";
                        coachBtn.addEventListener("click", function () { cloneDeckAction(item.id, item.name, true); });
                        dWrap.append(addBtn, coachBtn);
                    }
                    tdAct.appendChild(dWrap);
                    tr.append(tdName, tdFmt, tdPl, tdWr, tdWc, tdAct);
                } else {
                    tr.appendChild(el("th", "", "#" + item.position));
                    var tdH = el("td");
                    if (item.profile_url) {
                        var hLink = el("a", "deck-link", item.handle);
                        hLink.href = item.profile_url;
                        tdH.appendChild(hLink);
                        if (item.is_patron) tdH.appendChild(document.createTextNode(" ★"));
                    } else {
                        tdH.textContent = item.handle + (item.is_patron ? " ★" : "");
                    }
                    tdH.dataset.label = "Player";
                    var tdR = el("td", "", item.rank_label || "Unranked");
                    tdR.dataset.label = "Rank";
                    var tdW = el("td", "", fmtPct(item.win_rate));
                    tdW.dataset.label = "Win Rate";
                    var tdG = el("td", "", item.wins + "W-" + item.losses + "L (" + item.games + ")");
                    tdG.dataset.label = "Games";
                    var tdD = el("td");
                    if (item.top_deck_slug && item.top_deck) {
                        var tLink = el("a", "deck-link", item.top_deck);
                        tLink.href = "/decks/" + encodeURIComponent(item.top_deck_slug);
                        tdD.appendChild(tLink);
                        if (item.top_deck_games && item.top_deck_games >= 5 && item.top_deck_win_rate !== null) {
                            tdD.appendChild(el("span", "muted small", " (" + fmtPct(item.top_deck_win_rate) + ")"));
                        }
                    } else {
                        tdD.textContent = item.top_deck || "—";
                    }
                    tdD.dataset.label = "Top Deck";
                    tr.append(tdH, tdR, tdW, tdG, tdD);
                }
                body.appendChild(tr);
            });
        }).catch(function (err) { showToast(err.message, true); });
    }

    ["lb-tab", "lb-format", "lb-mode", "lb-window"].forEach(function (id) {
        var ctrl = document.getElementById(id);
        if (ctrl) ctrl.addEventListener("change", loadLeaderboard);
    });

    // ---- Deck Detail Actions ---------------------------------------------
    var detailCopyBtn = document.getElementById("detail-copy-btn");
    if (detailCopyBtn) {
        detailCopyBtn.addEventListener("click", function () {
            var src = document.getElementById("detail-export-source");
            if (src) {
                navigator.clipboard.writeText(src.value).then(function () {
                    showToast("Copied Arena deck export to clipboard.");
                });
            }
        });
    }
    var detailQueueBtn = document.getElementById("detail-queue-btn");
    if (detailQueueBtn) {
        detailQueueBtn.addEventListener("click", function () {
            api("/v1/decks/" + encodeURIComponent(detailQueueBtn.dataset.deckId) + "/queue-arena", { method: "POST", body: "{}" })
                .then(function () { showToast("Queued for Coach & Arena! Open the Coach desktop app to build/import it."); })
                .catch(function (err) { showToast(err.message, true); });
        });
    }
    var detailSaveBtn = document.getElementById("detail-save-profile-btn");
    if (detailSaveBtn) {
        detailSaveBtn.addEventListener("click", function () {
            var h1 = document.querySelector(".profile-hero h1");
            cloneDeckAction(detailSaveBtn.dataset.deckId, h1 ? h1.textContent.trim() : "Deck", false);
        });
    }
    var detailCloneBtn = document.getElementById("detail-clone-btn");
    if (detailCloneBtn) {
        detailCloneBtn.addEventListener("click", function () {
            var h1 = document.querySelector(".profile-hero h1");
            cloneDeckAction(detailCloneBtn.dataset.deckId, h1 ? h1.textContent.trim() : "Deck", true);
        });
    }
    root.addEventListener("click", function (e) {
        var btn = e.target && e.target.closest ? e.target.closest("button") : null;
        if (!btn) return;
        if (btn.classList.contains("js-export-deck")) {
            var srcEl = document.getElementById(btn.dataset.exportId || "");
            openExportDialog(btn.dataset.deckName || "Deck", srcEl ? srcEl.value : "");
        } else if (btn.classList.contains("js-add-profile")) {
            cloneDeckAction(btn.dataset.deckId, "Deck", false);
        } else if (btn.classList.contains("js-add-coach")) {
            cloneDeckAction(btn.dataset.deckId, "Deck", true);
        } else if (btn.classList.contains("js-queue-own")) {
            api("/v1/decks/" + encodeURIComponent(btn.dataset.deckId) + "/queue-arena", { method: "POST", body: "{}" })
                .then(function () { showToast("Queued for Coach & Arena! Open the Coach desktop app to build/import it."); })
                .catch(function (err) { showToast(err.message, true); });
        }
    });

    // ---- Initial Load ----------------------------------------------------
    if (PAGE === "profile") {
        var initialHash = (window.location.hash || "").replace(/^#/, "");
        if (initialHash) activateTab(initialHash);
        loadProfile();
        renderBuilder();
    } else if (PAGE === "leaderboard") {
        loadLeaderboard();
    }
})();
