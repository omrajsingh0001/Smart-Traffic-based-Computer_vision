/**
 * STMS Interactive UI/UX & Night Theme Engine
 * - Dynamic Ripple Physics
 * - Real-Time Multi-Theme Night Switcher (Midnight / Tokyo / Tactical)
 * - Persistent Theme Memory via LocalStorage
 */

(function () {
    // --- 1. Theme Configuration & Persistence ---
    const THEMES = [
        { id: 'midnight', label: '🌌 Midnight', title: 'Cyber Midnight Command' },
        { id: 'tokyo', label: '⚡ Tokyo', title: 'Tokyo Synthwave Cyberpunk' },
        { id: 'tactical', label: '🛡️ Tactical', title: 'Tactical Stealth OLED' }
    ];

    function applyTheme(themeId) {
        document.documentElement.setAttribute('data-theme', themeId);
        try {
            localStorage.setItem('stms-theme', themeId);
        } catch (e) {}

        // Update active class on switcher pills
        document.querySelectorAll('.theme-pill-btn').forEach(btn => {
            if (btn.dataset.themeTarget === themeId) {
                btn.classList.add('active');
            } else {
                btn.classList.remove('active');
            }
        });
    }

    // Apply saved theme immediately before render
    const savedTheme = (function () {
        try {
            return localStorage.getItem('stms-theme') || 'midnight';
        } catch (e) {
            return 'midnight';
        }
    })();
    applyTheme(savedTheme);

    // --- 2. Mount Floating Futuristic Theme Switcher ---
    function mountThemeSwitcher() {
        if (document.querySelector('.stms-theme-switcher')) return;

        const switcher = document.createElement('div');
        switcher.className = 'stms-theme-switcher';
        switcher.title = 'Switch Night Theme';

        const label = document.createElement('span');
        label.style.fontSize = '0.75rem';
        label.style.color = 'var(--text-muted)';
        label.style.fontFamily = 'var(--font-mono)';
        label.style.paddingLeft = '6px';
        label.style.fontWeight = 'bold';
        label.innerHTML = '🎨';
        switcher.appendChild(label);

        THEMES.forEach(t => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'theme-pill-btn' + (t.id === savedTheme ? ' active' : '');
            btn.dataset.themeTarget = t.id;
            btn.title = t.title;
            btn.innerText = t.label;
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                applyTheme(t.id);
            });
            switcher.appendChild(btn);
        });

        document.body.appendChild(switcher);
    }

    // --- 3. Dynamic Click Ripple Physics ---
    function createRipple(event) {
        const button = event.currentTarget;
        
        const computedStyle = window.getComputedStyle(button);
        if (computedStyle.position === 'static') {
            button.style.position = 'relative';
        }
        button.style.overflow = 'hidden';

        const circle = document.createElement('span');
        const diameter = Math.max(button.clientWidth, button.clientHeight);
        const radius = diameter / 2;

        const rect = button.getBoundingClientRect();
        circle.style.width = circle.style.height = `${diameter}px`;
        circle.style.left = `${event.clientX - rect.left - radius}px`;
        circle.style.top = `${event.clientY - rect.top - radius}px`;
        circle.classList.add('stms-ripple');

        const existingRipple = button.querySelector('.stms-ripple');
        if (existingRipple) {
            existingRipple.remove();
        }

        button.appendChild(circle);

        setTimeout(() => {
            circle.remove();
        }, 600);
    }

    // Attach to interactive elements
    function initInteractiveButtons() {
        const selectors = [
            '.button', 
            '.btn-interactive', 
            '.nav-pill-btn', 
            '.filter-btn', 
            '.btn-action',
            'button[type="submit"]',
            '.theme-pill-btn'
        ];

        document.querySelectorAll(selectors.join(', ')).forEach(el => {
            if (!el.dataset.interactiveBound) {
                el.addEventListener('click', createRipple);
                el.dataset.interactiveBound = 'true';
            }
        });
    }

    // Initialize
    function init() {
        mountThemeSwitcher();
        initInteractiveButtons();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    setInterval(initInteractiveButtons, 2000);
})();
