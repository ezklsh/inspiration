/* ============================================
   shared helpers for local-only tool pages
   (upload, manage) — never deployed
   ============================================ */

function slugify(title) {
    return title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'post';
}

function showResult(resultBox, text, isError, isMuted) {
    resultBox.hidden = false;
    resultBox.innerHTML = '';
    const lines = String(text).split('\n');
    const isLink = lines.length === 1 && /^post\.html\?slug=/.test(text);
    if (isLink) {
        const a = document.createElement('a');
        a.href = text;
        a.textContent = 'view locally: ' + text;
        resultBox.appendChild(a);
        return;
    }
    const pre = document.createElement('pre');
    pre.textContent = text;
    if (isError) pre.classList.add('error');
    if (isMuted) pre.classList.add('muted');
    resultBox.appendChild(pre);
}
