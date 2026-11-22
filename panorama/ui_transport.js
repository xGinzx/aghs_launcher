// Reassemble large server UI events. SPDX-License-Identifier: MIT
(function () {
  if (GameEvents.__ahbUiTransport) return;
  GameEvents.__ahbUiTransport = true;
  var subscribe = GameEvents.Subscribe;
  var unsubscribe = GameEvents.Unsubscribe;
  var listeners = Object.create(null);
  var pending = Object.create(null);
  var completed = Object.create(null);
  var pendingBytes = 0;

  GameEvents.Subscribe = function (name, callback) {
    var id = subscribe.call(GameEvents, name, callback);
    listeners[id] = { name: name, callback: callback };
    return id;
  };
  GameEvents.Unsubscribe = function (id) {
    delete listeners[id];
    return unsubscribe.call(GameEvents, id);
  };

  function discard(id) {
    if (!pending[id]) return;
    pendingBytes -= pending[id].receivedBytes;
    delete pending[id];
  }
  function report() { $.Msg('[AGHS_UI] invalid or expired UI transfer'); }

  subscribe.call(GameEvents, 'ahb_ui_fragment', function (frame) {
    try {
      var now = Date.now();
      Object.keys(pending).forEach(function (id) {
        if (now - pending[id].started > 120000) discard(id);
      });
      Object.keys(completed).forEach(function (id) {
        if (now - completed[id] > 120000) delete completed[id];
      });
      var part = Number(frame.part), total = Number(frame.total), size = Number(frame.bytes);
      if (typeof frame.id !== 'string' || !/^u[0-9a-f]+_[0-9a-f]+$/.test(frame.id) || frame.id.length > 96 ||
          typeof frame.event !== 'string' || frame.event.length > 256 ||
          !Number.isInteger(part) || !Number.isInteger(total) || part < 1 || part > total || total > 8192 ||
          !Number.isInteger(size) || size < 1 || size > 4194304 || total !== Math.ceil(size / 512) ||
          typeof frame.hex !== 'string' || frame.hex.length > 1024 || frame.hex.length % 2 || /[^0-9a-f]/.test(frame.hex)) {
        report(); return;
      }
      var expected = part === total ? (size - (total - 1) * 512) * 2 : 1024;
      if (frame.hex.length !== expected) { report(); return; }
      if (completed[frame.id]) return;
      var record = pending[frame.id];
      if (!record) {
        if (Object.keys(pending).length >= 128 || pendingBytes + frame.hex.length > 16777216) { report(); return; }
        record = pending[frame.id] = { event: frame.event, total: total, size: size, parts: Object.create(null),
          count: 0, started: now, receivedBytes: 0 };
      }
      if (record.event !== frame.event || record.total !== total || record.size !== size) { discard(frame.id); report(); return; }
      if (Object.prototype.hasOwnProperty.call(record.parts, part)) {
        if (record.parts[part] !== frame.hex) { discard(frame.id); report(); }
        return;
      }
      if (pendingBytes + frame.hex.length > 16777216) { discard(frame.id); report(); return; }
      record.parts[part] = frame.hex;
      record.count += 1;
      record.receivedBytes += frame.hex.length;
      pendingBytes += frame.hex.length;
      if (record.count !== total) return;
      var segments = [];
      for (var index = 1; index <= total; index++) segments.push(record.parts[index]);
      var hex = segments.join('');
      discard(frame.id);
      if (hex.length !== size * 2) { report(); return; }
      var text = decodeURIComponent(hex.replace(/../g, function (pair) { return '%' + pair; }));
      var payload = JSON.parse(text);
      if (Object.keys(completed).length >= 1024) delete completed[Object.keys(completed)[0]];
      completed[frame.id] = now || 1;
      var recipients = Object.keys(listeners).map(function (id) { return listeners[id]; });
      recipients.forEach(function (listener) {
        if (listener.name !== record.event) return;
        try { listener.callback(payload); }
        catch (error) { $.Msg('[AGHS_UI] event callback raised an error'); }
      });
    } catch (error) { report(); }
  });
})();
