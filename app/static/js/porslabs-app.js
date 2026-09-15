const menuToggle = document.getElementById('menu-toggle');
const mobileDrawer = document.getElementById('mobile-drawer');

function setMenu(open) {
  if (!menuToggle || !mobileDrawer) return;
  menuToggle.classList.toggle('open', open);
  mobileDrawer.classList.toggle('open', open);
  menuToggle.setAttribute('aria-expanded', String(open));
  document.body.style.overflow = open ? 'hidden' : '';
}

if (menuToggle && mobileDrawer) {
  menuToggle.addEventListener('click', () => setMenu(!mobileDrawer.classList.contains('open')));
  mobileDrawer.querySelectorAll('a, button').forEach((element) => {
    element.addEventListener('click', () => setMenu(false));
  });
  window.addEventListener('resize', () => {
    if (window.innerWidth > 960) setMenu(false);
  });
}

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') setMenu(false);
});

const year = document.getElementById('copyright-year');
if (year) year.textContent = new Date().getFullYear();

document.querySelectorAll('[data-password-toggle]').forEach((button) => {
  const input = document.getElementById(button.dataset.passwordToggle);
  if (!input) return;
  button.addEventListener('click', () => {
    const showing = input.type === 'text';
    input.type = showing ? 'password' : 'text';
    button.classList.toggle('active', !showing);
    button.setAttribute('aria-label', showing ? 'Tampilkan password' : 'Sembunyikan password');
    input.focus({ preventScroll: true });
  });
});

function previewImage(input) {
  const area = input.closest('.upload-area');
  const preview = area?.querySelector('.upload-preview');
  const text = area?.querySelector('.upload-text');
  const icon = area?.querySelector('.upload-icon');
  if (!preview || !input.files?.[0]) return;
  const reader = new FileReader();
  reader.onload = (event) => {
    preview.src = event.target.result;
    preview.style.display = 'block';
    if (text) text.style.display = 'none';
    if (icon) icon.style.display = 'none';
  };
  reader.readAsDataURL(input.files[0]);
}

window.previewImage = previewImage;

const inboxBadges = document.querySelectorAll('[data-inbox-count]');
if (inboxBadges.length) {
  async function refreshInboxCount() {
    try {
      const response = await fetch('/api/inbox/unread', { cache: 'no-store' });
      if (!response.ok) return;
      const data = await response.json();
      inboxBadges.forEach((badge) => {
        badge.textContent = data.unread_count > 99 ? '99+' : data.unread_count;
        badge.hidden = !data.unread_count;
      });

      const connectedAccounts = new Set(data.connected_account_ids || []);
      document.querySelectorAll('[data-account-status]').forEach((indicator) => {
        const connected = connectedAccounts.has(Number(indicator.dataset.accountStatus));
        indicator.classList.toggle('is-online', connected);
        indicator.classList.toggle('is-offline', !connected);
        indicator.classList.remove('is-checking');
        indicator.querySelector('[data-account-status-label]').textContent = connected ? 'Aktif' : 'Nonaktif';
      });

      const previous = Number(sessionStorage.getItem('latestInboxMessageId') || 0);
      if (
        previous && data.latest_id > previous &&
        'Notification' in window && Notification.permission === 'granted'
      ) {
        const notification = new Notification(`${data.peer_name} via ${data.account_label}`, {
          body: data.latest_preview,
        });
        notification.onclick = () => { window.location.href = data.url; };
      }
      if (data.latest_id) sessionStorage.setItem('latestInboxMessageId', data.latest_id);
    } catch (_error) {
      // Poll berikutnya akan mencoba lagi.
    }
  }

  refreshInboxCount();
  setInterval(refreshInboxCount, 5000);
}
