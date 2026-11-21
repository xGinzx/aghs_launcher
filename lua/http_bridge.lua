-- AHB/1 local HTTP adapter. SPDX-License-Identifier: MIT
local Bridge = {}
local Request = {}
Request.__index = Request

local state = { waiting = {}, sequence = 0, installed = false }
local verbs = { GET = 1, HEAD = 1, POST = 1, PUT = 1, PATCH = 1, DELETE = 1, OPTIONS = 1 }
local managed = { host = 1, ['content-length'] = 1, connection = 1,
    ['transfer-encoding'] = 1, upgrade = 1, ['proxy-authorization'] = 1,
    ['proxy-connection'] = 1 }
local output = print_Engine or print
local now = RealTime or Time or os.clock

local function bytes_to_hex(value)
    local encoded = {}
    for position = 1, #value do
        encoded[position] = string.format('%02x', value:byte(position))
    end
    return table.concat(encoded)
end

local function hex_to_bytes(value)
    if type(value) ~= 'string' or #value % 2 ~= 0 or value:find('[^0-9a-f]') then
        error('Malformed hexadecimal data')
    end
    local decoded = {}
    for position = 1, #value, 2 do
        decoded[#decoded + 1] = string.char(tonumber(value:sub(position, position + 1), 16))
    end
    return table.concat(decoded)
end

local function encode_fields(values)
    local encoded = { 'AHB1', string.format('%04x', #values) }
    for _, value in ipairs(values) do
        value = tostring(value)
        encoded[#encoded + 1] = string.format('%08x', #value)
        encoded[#encoded + 1] = value
    end
    return table.concat(encoded)
end

local function decode_fields(data)
    if #data > 4325376 or data:sub(1, 4) ~= 'AHB1' then error('Invalid response envelope') end
    local size = data:sub(5, 8)
    if #size ~= 4 or size:find('[^0-9a-f]') then error('Invalid response field count') end
    local count, cursor, values = tonumber(size, 16), 9, {}
    if count > 1024 then error('Too many response fields') end
    for _ = 1, count do
        size = data:sub(cursor, cursor + 7)
        if #size ~= 8 or size:find('[^0-9a-f]') then error('Invalid response field size') end
        local length = tonumber(size, 16)
        cursor = cursor + 8
        if cursor + length - 1 > #data then error('Incomplete response field') end
        values[#values + 1] = data:sub(cursor, cursor + length - 1)
        cursor = cursor + length
    end
    if cursor ~= #data + 1 then error('Unexpected response suffix') end
    return values
end

local function read_kv(path)
    local ok, result = pcall(LoadKeyValues, path)
    if ok and type(result) == 'table' then return result end
end

local function scalar(value)
    if type(value) == 'number' then return tostring(value) end
    if type(value) == 'string' then return value end
end

local function positive_integer(value, maximum)
    return type(value) == 'number' and value >= 1 and value <= maximum and value == math.floor(value)
end

local function item_count(values)
    local count = 0
    for _ in pairs(values) do count = count + 1 end
    return count
end

local function append_pairs(fields, values)
    local names = {}
    for name in pairs(values) do names[#names + 1] = name end
    table.sort(names)
    fields[#fields + 1] = #names
    for _, name in ipairs(names) do
        fields[#fields + 1] = name
        fields[#fields + 1] = values[name]
    end
end

function Request:SetHTTPRequestHeaderValue(name, value)
    value = scalar(value)
    if self.sent or type(name) ~= 'string' or #name < 1 or #name > 128 or
        not name:match("^[%w!#$%%&'*+%.%^_`|~%-]+$") or not value or #value > 8192 or
        value:find('[%z\r\n]') or managed[name:lower()] then return false end
    name = name:lower()
    if self.headers[name] == nil and item_count(self.headers) >= 64 then return false end
    self.headers[name] = value
    return true
end

function Request:SetHTTPRequestGetOrPostParameter(name, value)
    name, value = scalar(name), scalar(value)
    if self.sent or self.mode == 'raw' or not name or not value or #name > 1024 or
        (self.verb ~= 'GET' and self.verb ~= 'HEAD' and self.verb ~= 'POST') then return false end
    if self.parameters[name] == nil and item_count(self.parameters) >= 128 then return false end
    self.parameters[name] = value
    return true
end

function Request:SetHTTPRequestRawPostBody(content_type, body)
    if self.sent or self.mode == 'raw' or next(self.parameters) or type(body) ~= 'string' or
        #body > 1048576 or (self.verb ~= 'POST' and self.verb ~= 'PUT' and self.verb ~= 'PATCH') or
        type(content_type) ~= 'string' or #content_type < 1 or #content_type > 256 then return false end
    if not self:SetHTTPRequestHeaderValue('Content-Type', content_type) then return false end
    self.mode, self.body = 'raw', body
    return true
end

function Request:SetHTTPRequestAbsoluteTimeoutMS(milliseconds)
    if self.sent or not positive_integer(milliseconds, 120000) then return false end
    self.deadline_ms = milliseconds
    return true
end

function Request:SetHTTPRequestNetworkActivityTimeout(seconds)
    if self.sent or not positive_integer(seconds, 120) then return false end
    self.activity_ms = seconds * 1000
    return true
end

function Request:Send(callback)
    if self.sent or type(callback) ~= 'function' or item_count(state.waiting) >= 16 then return false end
    local fields = { self.verb, self.url, self.deadline_ms, self.activity_ms, self.mode, self.body }
    append_pairs(fields, self.headers)
    append_pairs(fields, self.parameters)
    local packet = encode_fields(fields)
    if #packet > 1048576 then return false end
    state.sequence = state.sequence + 1
    local request_id = 'q' .. state.boot .. '_' .. string.format('%x', state.sequence)
    self.sent = true
    state.waiting[request_id] = { callback = callback, expires = now() + self.deadline_ms / 1000 }
    local wire = bytes_to_hex(packet)
    local frame_count = math.ceil(#wire / 768)
    for frame = 1, frame_count do
        local payload = wire:sub((frame - 1) * 768 + 1, frame * 768)
        output(string.format('[AGHS_HTTP_REQUEST] %s:%s:%d/%d:%s', state.token, request_id, frame, frame_count, payload))
    end
    Bridge.attach()
    return true
end

local function response_for(request_id)
    local record = read_kv(state.directory .. '/' .. request_id .. '.kv')
    if not record or record.token ~= state.token or record.request ~= request_id then return end
    local count, pages, length = tonumber(record.chunks), tonumber(record.pages), tonumber(record.length)
    if not positive_integer(count, 11264) or not positive_integer(pages, 176) or
        pages ~= math.ceil(count / 64) or not positive_integer(length, 4325376) then
        error('Invalid response dimensions')
    end
    local segments = {}
    for page_number = 0, pages - 1 do
        local page = read_kv(state.directory .. '/' .. request_id .. '_' .. page_number .. '.kv')
        if not page or page.token ~= state.token or page.request ~= request_id or
            tonumber(page.page) ~= page_number or type(page.data) ~= 'table' then return end
        for index = 0, math.min(64, count - page_number * 64) - 1 do
            local segment = hex_to_bytes(page.data['b' .. index])
            if #segment > 384 or (#segment ~= 384 and page_number * 64 + index ~= count - 1) then
                error('Invalid response segment')
            end
            segments[#segments + 1] = segment
        end
    end
    local message = table.concat(segments)
    if #message ~= length then error('Response length mismatch') end
    local fields = decode_fields(message)
    local header_count = tonumber(fields[1])
    if not header_count or header_count < 0 or header_count > 100 or
        header_count ~= math.floor(header_count) or #fields ~= 2 * header_count + 2 then
        error('Invalid response headers')
    end
    local headers = {}
    for i = 1, header_count do headers[fields[2 * i]] = fields[2 * i + 1] end
    return { StatusCode = tonumber(record.code) or 0, Body = fields[#fields], Headers = headers,
        Error = record.error ~= '' and record.error or nil, ElapsedMS = tonumber(record.elapsed_ms) or 0 }
end

function Bridge.poll()
    local directory_index = read_kv(state.directory .. '/index.kv')
    local done = directory_index and directory_index.token == state.token and directory_index.done
    local callbacks = {}
    for request_id, request in pairs(state.waiting) do
        local result
        if type(done) == 'table' and done[request_id] then
            local ok, value = pcall(response_for, request_id)
            if ok then result = value else result = { StatusCode = 0, Body = '', Headers = {}, Error = 'invalid_response' } end
        end
        if not result and now() >= request.expires then
            result = { StatusCode = 0, Body = '', Headers = {}, Error = 'timeout' }
        end
        if result then
            state.waiting[request_id] = nil
            callbacks[#callbacks + 1] = { request_id, request.callback, result }
        end
    end
    for _, finished in ipairs(callbacks) do
        output('[AGHS_HTTP_ACK] ' .. state.token .. ':' .. finished[1])
        local ok = pcall(finished[2], finished[3])
        if not ok then output('[AGHS_HTTP] callback raised an error') end
    end
    if next(state.waiting) then return 0.05 end
end

function Bridge.attach()
    if not next(state.waiting) or not GameRules then return end
    local entity = GameRules:GetGameModeEntity()
    if entity then entity:SetContextThink('ahb_response_pump', Bridge.poll, 0) end
end

local function new_factory(native)
    return function(verb, url)
        if type(native) == 'function' then
            local native_request = native(verb, url)
            if native_request ~= nil then return native_request end
        end
        if type(verb) ~= 'string' or type(url) ~= 'string' or #url > 8192 or
            url:find('[%z%s\127]') or not url:match('^https?://') then return end
        verb = verb:upper()
        if not verbs[verb] then return end
        return setmetatable({ verb = verb, url = url, deadline_ms = 60000, activity_ms = 30000,
            mode = 'form', body = '', headers = {}, parameters = {}, sent = false }, Request)
    end
end

function Bridge.install()
    if state.installed then return true end
    if type(IsDedicatedServer) == 'function' and IsDedicatedServer() then return false end
    local config = read_kv('scripts/aghs_http_bridge.txt')
    if not config or type(config.token) ~= 'string' or #config.token ~= 32 or
        config.token:find('[^0-9a-f]') then error('Start the Aghs HTTP bridge before loading the addon') end
    state.token = config.token
    state.directory = 'scripts/ahb_' .. state.token
    state.probe = tonumber(config.probe) == 1
    state.boot = string.format('%x', math.floor(now() * 1000000))
    for _, name in ipairs({ 'CreateHTTPRequest', 'CreateHTTPRequestScriptVM' }) do
        _G[name] = new_factory(_G[name])
    end
    state.installed = true
    output('[AGHS_HTTP] adapter installed')
    return true
end

function Bridge.probe()
    if not state.probe or state.probed then return end
    state.probed = true
    local request = CreateHTTPRequestScriptVM('GET', 'https://store.steampowered.com/robots.txt')
    if request then
        request:Send(function(reply)
            output('[AGHS_HTTP_PROBE] status=' .. reply.StatusCode .. ' bytes=' .. #reply.Body)
        end)
    end
end

return Bridge
