/* ============================================
   manage page — list, edit (incl. slug rename),
   delete (with undo), push. Local-only.
   Depends on tools/tool.js (slugify, showResult).
   ============================================ */

const postsEl = document.getElementById('posts');
const countEl = document.getElementById('count');
const resultBox = document.getElementById('result');
const dirtyEl = document.getElementById('dirty');
const pushBtn = document.getElementById('push-btn');
const undoBtn = document.getElementById('undo-btn');
const deleteImagesChk = document.getElementById('delete-images');

let posts = [];

function markDirty() { dirtyEl.hidden = false; }

async function loadPosts() {
    try {
        const res = await fetch('/api/posts');
        const data = await res.json();
        if (!res.ok) {
            showResult(resultBox, `error: ${data.error || 'could not load posts'}`, true);
            return;
        }
        posts = data.posts;
        renderPosts(posts);
        updateUndoButton(data.undo || { count: 0 });
    } catch (e) {
        showResult(resultBox, 'error: ' + e.message + ' — is the local server running?', true);
    }
}

function updateUndoButton(undo) {
    if (undo.count > 0) {
        undoBtn.disabled = false;
        undoBtn.textContent = `restore "${undo.last_title}"`;
        undoBtn.title = `${undo.count} deletion(s) can be restored until you push`;
    } else {
        undoBtn.disabled = true;
        undoBtn.textContent = 'restore last deleted';
        undoBtn.title = '';
    }
}

function renderPosts(list) {
    countEl.textContent = `${list.length} post${list.length === 1 ? '' : 's'}`;
    postsEl.innerHTML = '';

    for (const post of list) {
        const row = document.createElement('section');
        row.className = 'post-row';
        row.dataset.slug = post.slug;

        // ---- summary ----
        const summary = document.createElement('div');
        summary.className = 'post-summary';

        const thumb = document.createElement('img');
        thumb.className = 'thumb';
        thumb.src = post.cover;
        thumb.alt = '';
        thumb.loading = 'lazy';

        const meta = document.createElement('div');
        meta.className = 'post-meta';
        const t = document.createElement('div');
        t.className = 'post-title';
        t.textContent = post.title;          // textContent: no escaping bugs
        const s = document.createElement('div');
        s.className = 'post-slug';
        s.textContent = post.slug;
        meta.append(t, s);

        const buttons = document.createElement('div');
        buttons.className = 'post-buttons';
        const viewLink = document.createElement('a');
        viewLink.href = `post.html?slug=${encodeURIComponent(post.slug)}`;
        viewLink.target = '_blank';
        viewLink.textContent = 'view';
        const editBtn = document.createElement('button');
        editBtn.type = 'button';
        editBtn.dataset.action = 'edit';
        editBtn.textContent = 'edit';
        const delBtn = document.createElement('button');
        delBtn.type = 'button';
        delBtn.dataset.action = 'delete';
        delBtn.textContent = 'delete';
        buttons.append(viewLink, editBtn, delBtn);

        summary.append(thumb, meta, buttons);

        // ---- hidden edit form ----
        const edit = document.createElement('div');
        edit.className = 'post-edit';
        edit.hidden = true;
        edit.append(
            field('title', 'text', post.title, 'edit-title'),
            field('slug', 'text', post.slug, 'edit-slug',
                  'changing this changes the post URL — old links stop working'),
            field('description', 'textarea', post.description, 'edit-description'),
            field('related (optional)', 'text', post.related.join(', '), 'edit-related',
                  'comma-separated slugs — only existing posts will link')
        );

        const actions = document.createElement('div');
        actions.className = 'actions';
        const saveBtn = document.createElement('button');
        saveBtn.type = 'button';
        saveBtn.dataset.action = 'save';
        saveBtn.textContent = 'save changes';
        const cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.dataset.action = 'cancel';
        cancelBtn.textContent = 'cancel';
        actions.append(saveBtn, cancelBtn);
        edit.appendChild(actions);

        row.append(summary, edit);
        postsEl.appendChild(row);
    }
}

function field(labelText, kind, value, cls, hintText) {
    const wrap = document.createElement('div');
    wrap.className = 'form-field';
    const label = document.createElement('label');
    label.textContent = labelText;
    const input = document.createElement(kind === 'textarea' ? 'textarea' : 'input');
    if (kind !== 'textarea') input.type = 'text';
    input.className = cls;
    input.value = value;
    wrap.append(label, input);
    if (hintText) {
        const hint = document.createElement('div');
        hint.className = 'hint';
        hint.textContent = hintText;
        wrap.appendChild(hint);
    }
    return wrap;
}

// ---------- edit ----------
async function saveChanges(row) {
    const slug = row.dataset.slug;
    const title = row.querySelector('.edit-title').value.trim();
    const slugInput = row.querySelector('.edit-slug').value.trim();
    const description = row.querySelector('.edit-description').value;
    const related = row.querySelector('.edit-related').value.trim();

    if (!title) { showResult(resultBox, 'error: title is required', true); return; }
    if (!description.trim()) { showResult(resultBox, 'error: description is required', true); return; }

    const newSlug = slugify(slugInput);
    if (newSlug !== slug &&
        !confirm(`change the slug from "${slug}" to "${newSlug}"?\n\n` +
                 `the old URL will stop working. related links pointing here are updated.`)) {
        return;
    }

    try {
        const res = await fetch('/api/edit', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ slug, new_slug: newSlug, title, description, related })
        });
        const data = await res.json();
        if (!res.ok) { showResult(resultBox, `error: ${data.error}`, true); return; }
        markDirty();
        showResult(resultBox, data.message);
        await loadPosts();
    } catch (e) {
        showResult(resultBox, 'error: ' + e.message, true);
    }
}

// ---------- delete ----------
async function deletePost(row) {
    const slug = row.dataset.slug;
    const post = posts.find(p => p.slug === slug);
    const label = post ? post.title : slug;
    const extra = deleteImagesChk.checked
        ? '\n\nits image files will be stashed (restorable until you push).'
        : '\n\nimage files will be left in place.';
    if (!confirm(`delete "${label}"?${extra}\n\nrestore it with the "restore last deleted" button.`)) return;

    try {
        const res = await fetch('/api/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ slug, delete_images: deleteImagesChk.checked })
        });
        const data = await res.json();
        if (!res.ok) { showResult(resultBox, `error: ${data.error}`, true); return; }
        markDirty();
        let msg = data.message;
        if (data.trashed && data.trashed.length) {
            msg += '\n\nstashed files:\n' + data.trashed.join('\n');
        }
        showResult(resultBox, msg);
        await loadPosts();
    } catch (e) {
        showResult(resultBox, 'error: ' + e.message, true);
    }
}

// ---------- undo ----------
undoBtn.addEventListener('click', async () => {
    undoBtn.disabled = true;
    try {
        const res = await fetch('/api/undo', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}'
        });
        const data = await res.json();
        if (!res.ok) { showResult(resultBox, `error: ${data.error}`, true); return; }
        markDirty();
        let msg = data.message;
        if (data.restored_files && data.restored_files.length) {
            msg += '\n\nrestored files:\n' + data.restored_files.join('\n');
        }
        showResult(resultBox, msg);
        await loadPosts();
    } catch (e) {
        showResult(resultBox, 'error: ' + e.message, true);
    } finally {
        undoBtn.disabled = false;
    }
});

// ---------- event delegation ----------
postsEl.addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-action]');
    if (!btn) return;
    const row = btn.closest('.post-row');
    const action = btn.dataset.action;

    if (action === 'edit') {
        row.querySelector('.post-edit').hidden = false;
    } else if (action === 'cancel') {
        row.querySelector('.post-edit').hidden = true;
        loadPosts();                       // discard unsaved field edits
    } else if (action === 'save') {
        saveChanges(row);
    } else if (action === 'delete') {
        deletePost(row);
    }
});

// ---------- push ----------
pushBtn.addEventListener('click', async () => {
    pushBtn.disabled = true;
    showResult(resultBox, 'checking git status…', false, true);
    try {
        const res = await fetch('/api/push', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: 'manage: update posts' })
        });
        const data = await res.json();
        if (!res.ok) {
            showResult(resultBox, `error: ${data.error}\n\n${data.output || ''}`, true);
            return;
        }
        showResult(resultBox, data.output || data.message);
        if (data.pushed) {
            dirtyEl.hidden = true;
            await loadPosts();             // undo history was cleared by the push
        }
    } catch (e) {
        showResult(resultBox, 'error: ' + e.message, true);
    } finally {
        pushBtn.disabled = false;
    }
});

loadPosts();
