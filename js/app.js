/* ============================================
   inspiration — app logic
   Tasks 4-7: grid render, detail render,
   dark mode, grid size controls
   ============================================ */

// ---------- Theme (Task 6) ----------

const THEME_KEY = 'inspiration-theme';

function getStoredTheme() {
    try {
        return localStorage.getItem(THEME_KEY);
    } catch (e) {
        return null;
    }
}

function applyTheme(theme) {
    document.body.classList.toggle('dark-mode', theme === 'dark');
}

function initTheme() {
    // Respect stored preference, else system preference
    const stored = getStoredTheme();
    if (stored) {
        applyTheme(stored);
        return;
    }
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    applyTheme(prefersDark ? 'dark' : 'light');
}

const themeToggle = document.getElementById('theme-toggle');
if (themeToggle) {
    themeToggle.addEventListener('click', () => {
        const isDark = document.body.classList.contains('dark-mode');
        const next = isDark ? 'light' : 'dark';
        applyTheme(next);
        try {
            localStorage.setItem(THEME_KEY, next);
        } catch (e) { /* storage unavailable */ }
    });
}

// ---------- Grid size (Task 7) ----------

const SIZE_KEY = 'inspiration-grid-size';

function getGridSize() {
    try {
        return localStorage.getItem(SIZE_KEY) || 'default';
    } catch (e) {
        return 'default';
    }
}

function applyGridSize(size) {
    const grid = document.getElementById('masonry-grid');
    if (!grid) return;
    grid.classList.remove('small', 'large');
    if (size !== 'default') {
        grid.classList.add(size);
    }
}

function initGridSize() {
    applyGridSize(getGridSize());
}

const gridSmaller = document.getElementById('grid-smaller');
const gridBigger = document.getElementById('grid-bigger');

if (gridSmaller) {
    gridSmaller.addEventListener('click', () => {
        const current = getGridSize();
        const next = current === 'large' ? 'default' : 'small';
        applyGridSize(next);
        try { localStorage.setItem(SIZE_KEY, next); } catch (e) {}
    });
}

if (gridBigger) {
    gridBigger.addEventListener('click', () => {
        const current = getGridSize();
        const next = current === 'small' ? 'default' : 'large';
        applyGridSize(next);
        try { localStorage.setItem(SIZE_KEY, next); } catch (e) {}
    });
}

// ---------- Homepage: render grid (Task 4) ----------

function renderGrid() {
    const grid = document.getElementById('masonry-grid');
    if (!grid) return;
    grid.innerHTML = POSTS.map(post => `
        <div class="grid-item">
            <a href="post.html?slug=${encodeURIComponent(post.slug)}">
                <img src="${post.cover}" alt="${post.title}" loading="lazy">
            </a>
        </div>
    `).join('');
}

// ---------- Detail page: render post (Task 5) ----------

function getSlugFromUrl() {
    const params = new URLSearchParams(window.location.search);
    return params.get('slug');
}

function renderDetail() {
    const slug = getSlugFromUrl();
    const post = POSTS.find(p => p.slug === slug);

    if (!post) {
        document.title = 'not found — inspiration';
        const title = document.getElementById('detail-title');
        const desc = document.getElementById('detail-description');
        const section = document.getElementById('related-section');
        if (title) title.textContent = 'post not found';
        if (desc) desc.innerHTML = `<a href="index.html">back to the grid</a>`;
        if (section) section.style.display = 'none';
        return;
    }

    document.title = `${post.title} — inspiration`;
    document.getElementById('detail-title').textContent = post.title;
    document.getElementById('detail-description').innerHTML = post.description;
    document.getElementById('detail-gallery').innerHTML = post.gallery.map(img =>
        `<img src="${img}" alt="${post.title}" loading="lazy">`
    ).join('');

    // Related posts: only those that exist, excluding current
    const related = (post.related || [])
        .map(s => POSTS.find(p => p.slug === s))
        .filter(Boolean);

    const relatedGrid = document.getElementById('related-grid');
    const section = document.getElementById('related-section');
    if (related.length === 0) {
        if (section) section.style.display = 'none';
    } else {
        if (section) section.style.display = '';
        relatedGrid.innerHTML = related.map(p => `
            <div class="grid-item">
                <a href="post.html?slug=${encodeURIComponent(p.slug)}">
                    <img src="${p.cover}" alt="${p.title}" loading="lazy">
                </a>
            </div>
        `).join('');
    }
}

// ---------- Init ----------

initTheme();
initGridSize();

if (document.getElementById('masonry-grid')) {
    renderGrid();
} else if (document.getElementById('detail-title')) {
    renderDetail();
}
