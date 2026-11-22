// Synthetic Panorama context for the transport tests. SPDX-License-Identifier: MIT
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const native = new Map();
let next = 1;
const received = [], errors = [];
const events = {
  Subscribe: function (name, callback) { const id = next++; native.set(id, {name, callback}); return id; },
  Unsubscribe: function (id) { native.delete(id); }
};
const context = vm.createContext({GameEvents: events, $: {Msg: function (message) { errors.push(message); }}});
const source = fs.readFileSync(path.join(__dirname, '../panorama/ui_transport.js'), 'utf8');
vm.runInContext(source, context);
vm.runInContext(source, context); // Repeated inclusion must not install two receivers.
const id = events.Subscribe(input.event, function (data) { received.push(data); });
if (input.unsubscribe) events.Unsubscribe(id);
function emit(name, data) {
  Array.from(native.values()).forEach(function (entry) { if (entry.name === name) entry.callback(data); });
}
input.messages.forEach(function (message) { emit(message.name, message.data); });
process.stdout.write(JSON.stringify({received, errors}));
