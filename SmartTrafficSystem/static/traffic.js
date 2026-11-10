(function () {
  const mapEl = document.getElementById('traffic-map');
  if (!mapEl || typeof L === 'undefined') return;
  const map = L.map('traffic-map', {scrollWheelZoom: false}).setView([22.5726, 88.3639], 12);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'}).addTo(map);
  map.on('click', () => map.scrollWheelZoom.enable());
  const layer = L.layerGroup().addTo(map);
  const list = document.getElementById('traffic-alert-list');
  const count = document.getElementById('incident-count');
  const updated = document.getElementById('traffic-updated');
  const SEVERE = ['Accident', 'Road closed', 'Lane closed', 'Flooding', 'Dangerous conditions'];
  let known = new Set(), first = true, filter = 'all', cache = [];

  const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const notice = html => { list.innerHTML = '<div class="traffic-empty">' + html + '</div>'; };

  function render() {
    layer.clearLayers(); list.innerHTML = '';
    const items = cache.filter(i => filter === 'all' || (filter === 'severe') === SEVERE.includes(i.category));
    count.textContent = items.length + (items.length === 1 ? ' alert' : ' alerts');
    if (!items.length) { notice(cache.length ? 'No alerts in this category.' : 'No active incidents were reported for this map area. This does not guarantee every road is clear.'); return; }
    items.forEach(item => {
      const severe = SEVERE.includes(item.category), color = severe ? '#d94351' : '#e89a37';
      const marker = L.circleMarker([item.lat, item.lon], {radius: 8, color, fillColor: color, fillOpacity: .85, weight: 2}).addTo(layer);
      marker.bindPopup('<strong>' + esc(item.category) + '</strong><br>' + esc(item.description) + '<br>' + esc(item.from));
      const delay = item.delay ? Math.max(1, Math.round(item.delay / 60)) + ' min delay' : 'Delay not supplied';
      const el = document.createElement('div');
      el.className = 'traffic-alert'; el.tabIndex = 0; el.setAttribute('role', 'button');
      el.innerHTML = '<span class="alert-tag ' + (severe ? 'high' : '') + '">' + esc(item.category) + '</span><strong>' + esc(item.from) + (item.to ? ' → ' + esc(item.to) : '') + '</strong><p>' + esc(item.description) + ' · ' + esc(delay) + '</p>';
      const go = () => { map.setView([item.lat, item.lon], 15); marker.openPopup(); mapEl.scrollIntoView({behavior: 'smooth', block: 'center'}); };
      el.addEventListener('click', go); el.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
      list.appendChild(el);
    });
  }

  function alertNew(incidents) {
    const fresh = incidents.filter(i => i.id && !known.has(i.id));
    if (!first && fresh.length) {
      const msg = 'New traffic alert: ' + fresh[0].category + ' near ' + fresh[0].from;
      if (window.showToast) window.showToast(msg, 'error');
      if ('Notification' in window && Notification.permission === 'granted') new Notification('New traffic alert', {body: msg});
    }
    known = new Set(incidents.filter(i => i.id).map(i => i.id)); first = false;
  }

  async function refresh() {
    try {
      const res = await fetch('/api/traffic-incidents', {cache: 'no-store', credentials: 'same-origin'});
      const data = await res.json();
      if (!data.configured) {
        notice(esc(data.message) + '<br><br>Set the <code>TOMTOM_API_KEY</code> environment variable to enable real incidents.');
        count.textContent = 'Not set up'; updated.textContent = 'API key required'; return;
      }
      if (!res.ok || !data.updated) throw new Error(data.message || 'Traffic data unavailable');
      cache = data.incidents || []; alertNew(cache); render();
      updated.textContent = 'Updated ' + new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    } catch (err) {
      updated.textContent = 'Connection issue';
      if (!cache.length) notice('Could not load live traffic data. Check your internet connection and TomTom API key.');
    }
  }

  const chips = document.getElementById('alert-filters');
  if (chips) chips.addEventListener('click', e => {
    const c = e.target.closest('.chip'); if (!c) return;
    chips.querySelectorAll('.chip').forEach(x => x.classList.toggle('active', x === c));
    filter = c.dataset.f; render();
  });
  if ('Notification' in window && Notification.permission === 'default')
    document.addEventListener('click', () => Notification.requestPermission(), {once: true});
  refresh(); setInterval(refresh, 60000);
  setTimeout(() => map.invalidateSize(), 300);
})();
